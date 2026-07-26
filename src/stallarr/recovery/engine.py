"""Escalation ladder.

One scan performs at most one repair step per torrent. That is deliberate: most
fixes need an announce cycle to take effect, so firing the whole ladder at once
would just burn through it and escalate a torrent that a reannounce would have
fixed. Each step gets `max_attempts_per_step` tries with a cooldown between
them; when it runs out, the next rung takes over.
"""

from __future__ import annotations

import logging
import time

from .. import db
from ..clients.base import ClientError, TorrentClient
from ..config import RecoveryConfig
from ..models import (
    AILMENT_ACTIONS,
    TERMINAL_AILMENTS,
    ActionKind,
    ActionResult,
    Ailment,
    Diagnosis,
)
from ..trackers import TrackerRouter
from ..trackers.base import TrackerPlugin
from ..utils import redact_secrets
from .actions import HANDLERS, REQUIRED_CAPABILITY, blocking_limits

log = logging.getLogger(__name__)


class RecoveryEngine:
    def __init__(self, client: TorrentClient, router: TrackerRouter, cfg: RecoveryConfig) -> None:
        self.client = client
        self.router = router
        self.cfg = cfg

    async def repair(self, diagnosis: Diagnosis) -> ActionResult | None:
        """Run at most one recovery step. Returns None when nothing was due."""
        finding = diagnosis.primary
        if finding is None:
            return None

        torrent = diagnosis.torrent
        plugin = self.router.get(diagnosis.tracker_id) if diagnosis.tracker_id else None

        attempts = await db.get_attempts(torrent.infohash, finding.ailment)
        step = self._forced_step(diagnosis, attempts) or self._next_step(
            finding.ailment, attempts, plugin
        )
        if step is None:
            return None

        if self.cfg.dry_run:
            result = ActionResult(step, True, f"would run {step}", dry_run=True)
            await self._log(diagnosis, result)
            return result

        await db.record_attempt(torrent.infohash, finding.ailment, step)
        await self._mark_working(diagnosis, step)

        handler = HANDLERS[step]
        try:
            result = await handler(
                self.client, torrent, plugin=plugin, recovery_cfg=self.cfg
            )
        except ClientError as exc:
            result = ActionResult(step, False, f"client rejected {step}: {exc}")
        except Exception as exc:
            log.exception("recovery step %s crashed for %s", step, torrent.name)
            result = ActionResult(step, False, f"{step} raised {type(exc).__name__}: {exc}")

        await self._log(diagnosis, result)
        return result

    # --- ladder logic ---------------------------------------------------------

    def _next_step(
        self, ailment, attempts: dict[str, dict[str, int]], plugin: TrackerPlugin | None
    ) -> ActionKind | None:
        now = time.time()
        enabled = self._enabled_steps()

        # Walk the ailment's own order, not the config's — the config only says
        # which steps are permitted at all.
        for step in AILMENT_ACTIONS.get(ailment, ()):
            if step not in enabled:
                continue
            if not self._capability_ok(step, plugin):
                continue

            record = attempts.get(step, {"count": 0, "last_at": 0})
            if record["count"] >= self.cfg.max_attempts_per_step:
                continue  # exhausted — fall through to the next rung

            cooldown = self.cfg.cooldown_minutes.get(step, 30) * 60
            if record["count"] and now - record["last_at"] < cooldown:
                return None  # this step is still cooling down; wait, don't escalate

            return step

        return None

    def _forced_step(
        self, diagnosis: Diagnosis, attempts: dict[str, dict[str, int]]
    ) -> ActionKind | None:
        """Get a stopped torrent running again when the tracker demands it.

        This is the one case that overrides the attempt limit. Everywhere else,
        exhausting the ladder and waiting for a human is right — but here the
        torrent is stopped, the site is counting down towards a hit-and-run, and
        the fix is a local API call that cannot make anything worse. Letting the
        ladder exhaust would leave it stopped forever, which is precisely how a
        seeding obligation turns into a penalty.

        Which step depends on *why* it is stopped: a torrent sitting on its own
        share limit is re-stopped seconds after any resume, so resuming it is
        not a fix, it is a lie in the history table.

        The cooldown still applies, so this is at most one action per cooldown
        window rather than one per scan.
        """
        torrent = diagnosis.torrent
        if not torrent.is_paused:
            return None
        if Ailment.HIT_AND_RUN_RISK not in diagnosis.ailments:
            return None

        step = (
            ActionKind.LIFT_SHARE_LIMITS
            if blocking_limits(torrent)
            else ActionKind.RESUME
        )
        if step not in self._enabled_steps():
            return None

        record = attempts.get(step, {"count": 0, "last_at": 0})
        cooldown = self.cfg.cooldown_minutes.get(step, 10) * 60
        if record["count"] and time.time() - record["last_at"] < cooldown:
            return None
        return step

    def _enabled_steps(self) -> set[ActionKind]:
        """Config `ladder` as a permission list. Escalation is never disabled —
        without it a torrent could be retried forever with no way out."""
        steps = {ActionKind.ESCALATE}
        for name in self.cfg.ladder:
            try:
                steps.add(ActionKind(name))
            except ValueError:
                log.warning("unknown recovery step %r in config ladder — ignoring", name)
        return steps

    def _capability_ok(self, step: ActionKind, plugin: TrackerPlugin | None) -> bool:
        required = REQUIRED_CAPABILITY.get(step)
        if required is None:
            return True
        return plugin is not None and plugin.can(required)

    async def _mark_working(self, diagnosis: Diagnosis, step: ActionKind) -> None:
        if not self.cfg.tagging_enabled or step is ActionKind.ESCALATE:
            return
        try:
            await self.client.add_tags(diagnosis.torrent.infohash, [self.cfg.working_tag])
        except ClientError as exc:
            log.debug("could not tag %s: %s", diagnosis.torrent.name, exc)

    async def _log(self, diagnosis: Diagnosis, result: ActionResult) -> None:
        finding = diagnosis.primary
        await self.log_action(
            infohash=diagnosis.torrent.infohash,
            name=diagnosis.torrent.name,
            tracker=diagnosis.tracker_id or "",
            ailment=str(finding.ailment) if finding else "",
            result=result,
        )

    async def log_action(
        self, *, infohash: str, name: str, tracker: str, ailment: str, result: ActionResult
    ) -> None:
        """Record an action. The single path to both the history table and the
        log, and therefore the single place redaction is enforced.

        Action messages quote announce URLs and tracker errors quote download
        links; both carry passkeys, and a step that forgets to redact would
        otherwise write one straight to disk. Redacting here means no action
        *can* leak, however it is written — which is why re-downloads come
        through this door too rather than logging themselves.
        """
        message = redact_secrets(result.message)

        await db.add_history(
            infohash=infohash,
            name=name,
            tracker=tracker,
            ailment=ailment,
            action=result.kind,
            ok=result.ok,
            dry_run=result.dry_run,
            message=message,
        )
        level = logging.INFO if result.ok else logging.WARNING
        log.log(
            level,
            "%s [%s] %s -> %s",
            "DRY-RUN" if result.dry_run else "REPAIR",
            ailment or "?",
            name[:60],
            message,
        )

    async def on_recovered(self, diagnosis: Diagnosis) -> None:
        """A previously sick torrent came back healthy: reset its ladder."""
        torrent = diagnosis.torrent
        await db.clear_attempts(torrent.infohash)
        if not self.cfg.tagging_enabled:
            return
        stale = [tag for tag in (self.cfg.working_tag, self.cfg.escalation_tag) if tag in torrent.tags]
        if stale:
            try:
                await self.client.remove_tags(torrent.infohash, stale)
            except ClientError as exc:
                log.debug("could not clear tags on %s: %s", torrent.name, exc)


def is_terminal(diagnosis: Diagnosis) -> bool:
    """True when no amount of retrying will help (e.g. unregistered)."""
    return any(ailment in TERMINAL_AILMENTS for ailment in diagnosis.ailments)
