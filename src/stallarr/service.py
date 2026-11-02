"""The supervisor: owns the client, plugins and scan loop, and holds the state
the WebUI reads.

One instance lives for the process lifetime. `reload()` rebuilds everything from
config without a restart, so saving settings in the UI takes effect immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config as config_module
from . import db
from .clients import ClientError, TorrentClient, build_client
from .config import Config
from .diagnosis import diagnose_local, enrich, should_skip
from .discovery import Discovery
from .models import (
    ActionResult,
    Ailment,
    Diagnosis,
    Finding,
    SeedingRequirement,
    Severity,
    Torrent,
    TrackerFreeleechEntry,
)
from .notify import Notifier
from .recovery import RecoveryEngine, breaker_report, reap, redownload, tracker_of
from .recovery.redownload import attempt_key as redownload_key
from .stats import download_headroom, library_stats, problems_by_ailment
from .trackers import TrackerRouter, load_plugins
from .trackers.base import Capability, TrackerError
from .utils import normalise_release

log = logging.getLogger(__name__)

#: Window the dashboard's seeding metrics cover. A week is roughly the span of a
#: hit-and-run deadline, and short enough that the numbers describe now; a day
#: would read as zero most of the time on a library that is behaving.
SEEDING_WINDOW_DAYS = 7

#: How long a torrent keeps its "recently recovered" badge on the Seeding tab
#: after a successful repair. Same rationale as SEEDING_WINDOW_DAYS: long
#: enough that the badge is still meaningful, short enough that it describes
#: now rather than the torrent's whole history.
RECENTLY_RECOVERED_DAYS = 7


@dataclass
class ScanSummary:
    started_at: float = 0.0
    finished_at: float = 0.0
    total: int = 0
    skipped: int = 0
    healthy: int = 0
    sick: int = 0
    actions: int = 0
    #: Previously-sick torrents that passed every check on this scan. The
    #: outcome the repair ladder exists for; nothing else records it.
    recovered: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(self.finished_at - self.started_at, 0.0)


class StallarrService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client: TorrentClient | None = None
        self.router = TrackerRouter({})
        self.engine: RecoveryEngine | None = None
        self.notifier = Notifier(cfg.notify, cfg.web.base_url)

        self.last_scan = ScanSummary()
        self.last_diagnoses: list[Diagnosis] = []
        self.client_status: dict[str, Any] = {"connected": False, "detail": "not started"}
        self.tracker_status: dict[str, dict[str, Any]] = {}
        #: Requirements the tracker raised that match nothing in the client.
        self.seeding_unmatched: list[dict[str, str]] = []
        #: Every outstanding seeding obligation, matched or not. Survives across
        #: scans so a debt clearing can be told apart from a fresh start.
        self.seeding_report: list[dict[str, str]] = []
        #: Trackers whose deletions were refused on the last scan, with the
        #: evidence. Held so the UI can explain a breaker that has quietly
        #: stopped the feature working.
        self.deletion_blocks: list[dict[str, Any]] = []
        #: Free space reported by the client, refreshed each scan.
        self.disk: dict[str, Any] = {}
        #: Whole-library totals from the last scan's torrent list. Computed
        #: from data already fetched, so it costs no extra client or tracker call.
        self.library: dict[str, Any] = library_stats([])
        self.scanning = False
        #: The ratio builder. Owns its own loop; see discovery/__init__.py.
        self.discovery = Discovery(self)

        #: Outcome of the last Prowlarr sync, shown beside the Sync now button.
        #: Empty until one has run.
        self.prowlarr_sync: dict[str, Any] = {}

        self._scan_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._prowlarr_task: asyncio.Task | None = None
        self._stopping = False

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        await db.init()
        await self._auto_sync_prowlarr()
        await self._build()
        self._task = asyncio.create_task(self._loop(), name="stallarr-scan-loop")
        self._prowlarr_task = asyncio.create_task(
            self._prowlarr_loop(), name="stallarr-prowlarr-sync"
        )
        self.discovery.start()

    async def stop(self) -> None:
        self._stopping = True
        await self.discovery.stop()
        self._wake.set()
        for task in (self._task, self._prowlarr_task):
            if not task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._teardown()
        await self.notifier.close()

    async def _teardown(self) -> None:
        if self.client:
            try:
                await self.client.close()
            except Exception:
                log.debug("error closing client", exc_info=True)
        await self.router.close()

    async def _build(self) -> None:
        """(Re)create the client, plugins and engine from current config."""
        await self._teardown()

        self.client = build_client(self.cfg)
        self.router = TrackerRouter(load_plugins(self.cfg))
        self.engine = RecoveryEngine(self.client, self.router, self.cfg.recovery)

        try:
            await self.client.connect()
            version = await self.client.version()
            self.client_status = {"connected": True, "detail": f"qBittorrent {version}"}
        except ClientError as exc:
            self.client_status = {"connected": False, "detail": str(exc)}
            log.error("torrent client unavailable: %s", exc)

        await self.refresh_tracker_status()

    async def reload(self, *, sync_prowlarr: bool = True) -> None:
        """Apply a saved config change without restarting the process.

        `sync_prowlarr=False` is for callers that have just synced and saved:
        syncing again would repeat the Prowlarr call and every probe for nothing.
        """
        from .web import oidc

        self.cfg = config_module.load()
        # A corrected issuer must take effect on save, not an hour later when
        # the discovery cache happens to expire.
        oidc.forget_discovery()
        if sync_prowlarr:
            await self._auto_sync_prowlarr()
        await self.notifier.close()
        self.notifier = Notifier(self.cfg.notify, self.cfg.web.base_url)
        logging.getLogger("stallarr").setLevel(self.cfg.log_level.upper())
        await self._build()
        self.trigger_scan()
        # A changed tracker list or filter should show up now, not next hour.
        self.discovery.trigger()

    async def _auto_sync_prowlarr(self) -> bool:
        """Pull new trackers and refreshed keys from Prowlarr, if switched on.

        Best-effort by design: Prowlarr being down must never stop stallarr from
        starting with the credentials it already has. Returns True when config
        changed (and was saved), so the caller knows plugins need rebuilding.
        """
        prowlarr_cfg = self.cfg.prowlarr
        if not (prowlarr_cfg.enabled and prowlarr_cfg.auto_sync and prowlarr_cfg.url):
            return False

        from .integrations import prowlarr

        try:
            result = await prowlarr.sync(self.cfg, overwrite=True)
        except prowlarr.ProwlarrError as exc:
            log.warning("Prowlarr auto-sync skipped: %s", exc)
            self.record_prowlarr_sync(error=str(exc), trigger="auto")
            return False
        except Exception as exc:
            log.exception("Prowlarr auto-sync failed")
            self.record_prowlarr_sync(error=f"sync failed: {exc}", trigger="auto")
            return False

        self.record_prowlarr_sync(result=result, trigger="auto")
        changed = [item for item in result["imported"] if item["changed"]]
        if not changed:
            return False
        config_module.save(self.cfg)
        log.info(
            "Prowlarr auto-sync: %d added, %d updated (%s)",
            len(result["added"]),
            len(result["updated"]),
            ", ".join(item["tracker_id"] for item in changed),
        )
        return True

    def record_prowlarr_sync(
        self,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
        trigger: str = "manual",
    ) -> None:
        """Remember how the last sync went. Ids and counts only, never secrets."""
        result = result or {}
        self.prowlarr_sync = {
            "at": time.time(),
            "trigger": trigger,
            "ok": not error,
            "error": error,
            "added": list(result.get("added", [])),
            "updated": list(result.get("updated", [])),
            "skipped": [item["tracker_id"] for item in result.get("skipped", [])],
            "total": len(result.get("proposals", [])),
        }

    async def _prowlarr_loop(self) -> None:
        """Re-sync from Prowlarr on its own clock.

        Separate from the scan loop: a sync is about config, not torrents, and
        running it inside the scan would tie a Prowlarr timeout to scan latency.
        Startup and every config save already sync, so this sleeps first.
        """
        while not self._stopping:
            minutes = max(
                self.cfg.prowlarr.sync_interval_minutes, config_module.MIN_PROWLARR_SYNC_MINUTES
            )
            await asyncio.sleep(minutes * 60)
            try:
                if await self._auto_sync_prowlarr():
                    await self.reload(sync_prowlarr=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Prowlarr sync loop iteration failed")

    def trigger_scan(self) -> None:
        """Ask the loop to scan now instead of waiting for the interval."""
        self._wake.set()

    # --- tracker status -------------------------------------------------------

    async def refresh_tracker_status(self) -> None:
        async def probe(plugin) -> tuple[str, dict[str, Any]]:
            try:
                health = await plugin.health()
            except TrackerError as exc:
                return plugin.id, {"name": plugin.name, "ok": False, "detail": str(exc)}
            return plugin.id, {
                "name": plugin.name,
                "ok": health.ok,
                "detail": health.detail,
                "latency_ms": health.latency_ms,
                "capabilities": sorted(str(c) for c in plugin.capabilities),
            }

        plugins = list(self.router.plugins.values())
        if not plugins:
            self.tracker_status = {}
            return
        results = await asyncio.gather(*(probe(p) for p in plugins), return_exceptions=True)
        status: dict[str, dict[str, Any]] = {}
        for item in results:
            if isinstance(item, BaseException):
                log.warning("tracker health probe failed: %s", item)
                continue
            plugin_id, payload = item
            status[plugin_id] = payload
        self.tracker_status = status

    # --- scan loop ------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.scan()
                await db.prune_history(self.cfg.history_retention_days)
                await db.prune_seeding_obligations(self.cfg.history_retention_days)
                await db.prune_tracker_checks(self.cfg.history_retention_days)
                # Snapshots are kept far longer than history: they are tiny
                # (one row per scan) and their whole value is showing drift
                # over a span that a 30-day window would cut in half.
                await db.prune_snapshots(max(self.cfg.history_retention_days * 6, 180))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scan loop iteration failed")

            interval = max(self.cfg.scan_interval_minutes, 1) * 60
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def scan(self) -> ScanSummary:
        if self._scan_lock.locked():
            log.debug("scan already running, skipping this trigger")
            return self.last_scan

        async with self._scan_lock:
            self.scanning = True
            try:
                return await self._scan_once()
            finally:
                self.scanning = False

    async def _scan_once(self) -> ScanSummary:
        summary = ScanSummary(started_at=time.time())

        if self.client is None or self.engine is None:
            summary.errors.append("service is not initialised")
            summary.finished_at = time.time()
            self.last_scan = summary
            return summary

        try:
            torrents = await self.client.list_torrents()
            self.client_status = {"connected": True, "detail": self.client_status.get("detail", "ok")}
        except ClientError as exc:
            self.client_status = {"connected": False, "detail": str(exc)}
            summary.errors.append(str(exc))
            summary.finished_at = time.time()
            self.last_scan = summary
            log.error("could not list torrents: %s", exc)
            return summary

        ignored = await db.ignored_hashes()
        summary.total = len(torrents)
        self.library = library_stats(torrents, self.router)
        diagnoses: list[Diagnosis] = []
        # Chosen before the loop so the oldest checks go first: picking as we go
        # would spend the budget on whatever the client happened to list first.
        recheck = await self._recheck_candidates(torrents, ignored)
        #: Torrents per tracker, healthy included — the denominator the deletion
        #: circuit breaker measures "how much of this site went missing" against.
        tracker_totals: dict[str, int] = {}

        # One request per tracker, before the loop. This is what makes seeding
        # requirements affordable where per-torrent enrichment is not.
        requirements, answered = await self._seeding_requirements()
        # Before any repair runs, so an obligation exists to credit the repair
        # against — and so a scan that dies partway still recorded what is owed.
        await self._record_obligations(requirements, answered)
        #: (tracker, normalised name) -> what the local torrent is doing about it
        matched_requirements: dict[tuple[str, str], str] = {}

        for torrent in torrents:
            reason = should_skip(torrent, self.cfg.rules, ignored)
            if reason:
                summary.skipped += 1
                continue

            plugin = self.router.for_hosts(torrent.tracker_hosts)
            diagnosis = diagnose_local(torrent, self.cfg.rules, plugin)
            bucket = tracker_of(diagnosis)
            tracker_totals[bucket] = tracker_totals.get(bucket, 0) + 1

            # A torrent stallarr has given up on is normally not worth a tracker
            # call — no further repair will be attempted either way. The
            # exception is confirming it still exists at all: sites delete
            # torrents that have been superseded, and that verdict turns a
            # permanently stuck "problem" into "safe to remove".
            worth_asking = not self._already_escalated(torrent) or torrent.infohash in recheck
            if not diagnosis.healthy and worth_asking and await enrich(
                diagnosis, plugin, self.cfg.rules
            ):
                await db.mark_verified(torrent.infohash)

            # Applied after enrichment so it can overrule it, and outside the
            # escalation guard: a torrent we gave up on is exactly the one most
            # likely to be sitting stopped while the tracker counts down.
            _apply_seeding_requirement(diagnosis, requirements, matched_requirements)

            if diagnosis.healthy:
                summary.healthy += 1
                if _was_previously_sick(torrent, self.cfg):
                    summary.recovered += 1
                    await self.engine.on_recovered(diagnosis)
                continue

            summary.sick += 1
            diagnoses.append(diagnosis)

        # A requirement with no local torrent cannot be repaired by any step in
        # the ladder: there is nothing to resume, reannounce or recheck. The
        # only remedy is to fetch the torrent back, which is what
        # `_redownload_missing` does when it is switched on.
        unmatched = [
            (tracker_id, req)
            for tracker_id, by_name in requirements.items()
            for key, req in by_name.items()
            if (tracker_id, key) not in matched_requirements
        ]
        if unmatched:
            log.warning(
                "%d torrent(s) require seeding but are not in the client: %s",
                len(unmatched),
                ", ".join(req.name[:40] for _, req in unmatched[:5]),
            )
        recovered = await self._redownload_missing(unmatched, summary)
        # Reported after the attempt so the dashboard shows what is still
        # outstanding, not what was outstanding a moment ago.
        self.seeding_unmatched = [
            {"tracker": tracker_id, "name": req.name, "detail": req.summary()}
            for tracker_id, req in unmatched
            if (tracker_id, req.torrent_id) not in recovered
        ]

        # Held across scans so the digest can tell "still owed" from "just
        # cleared" — a resolution nobody is told about is a problem nobody
        # believes is fixed.
        was_owed = bool(self.seeding_report)
        self.seeding_report = self._seeding_report(requirements, matched_requirements, recovered)
        if self.seeding_report:
            await self.notifier.seeding_digest(self.seeding_report)
        elif was_owed:
            await self.notifier.seeding_cleared()

        # Before repairs: a torrent the site has deleted has nothing to repair,
        # and escalating it first would tag and alert on something about to go.
        deleted = await self._reap(diagnoses, tracker_totals, summary)
        diagnoses = [d for d in diagnoses if d.torrent.infohash not in deleted]

        # Repair worst-first so a burst of problems fixes the critical ones even
        # if the client starts rate-limiting us partway through.
        diagnoses.sort(key=lambda d: d.severity, reverse=True)
        for diagnosis in diagnoses:
            result = await self._handle(diagnosis, summary)
            await self.notifier.alert(diagnosis, result)

        summary.finished_at = time.time()
        self.last_scan = summary
        self.last_diagnoses = diagnoses
        await self._refresh_disk()
        await self._record_snapshot(summary)
        log.info(
            "scan complete: %d torrents, %d healthy, %d sick, %d actions in %.1fs",
            summary.total,
            summary.healthy,
            summary.sick,
            summary.actions,
            summary.duration,
        )
        return summary

    async def _seeding_requirements(
        self,
    ) -> tuple[dict[str, dict[str, SeedingRequirement]], set[str]]:
        """Each tracker's "you still owe seeding" list, keyed by tracker then
        by normalised release name.

        Failures are logged and dropped rather than raised: a site being down
        must not stop the scan, and an empty result here only means we fall back
        to the local-only diagnosis we had before.

        The second return value is the set of trackers that actually answered,
        and the two are not interchangeable. A healthy tracker returning nothing
        means "you owe nothing", which must settle the obligations on record; a
        tracker that errored means nothing at all. Reading the second as the
        first would mark every live debt settled — and, worse, credit stallarr
        with averting them.
        """
        out: dict[str, dict[str, SeedingRequirement]] = {}
        answered: set[str] = set()
        for plugin in self.router.plugins.values():
            if not plugin.can(Capability.SEEDING_REQUIREMENTS):
                continue
            try:
                rows = await plugin.seeding_requirements()
            except Exception as exc:  # noqa: BLE001 - a sick tracker must not fail the scan
                log.warning("%s: could not read seeding requirements: %s", plugin.id, exc)
                continue
            answered.add(plugin.id)
            if rows:
                out[plugin.id] = {normalise_release(r.name): r for r in rows}
                log.info("%s: %d torrent(s) require seeding", plugin.id, len(rows))
        return out, answered

    async def _freeleech_status(self) -> dict[str, dict[str, TrackerFreeleechEntry]]:
        """Each tracker's freeleech status for our own torrents, keyed by
        tracker then by normalised release name.

        Best-effort and opt-in per plugin (`Capability.FREELEECH_STATUS`).
        Failures are logged and dropped rather than raised, same as
        `_seeding_requirements`: this feeds a dashboard view, not recovery, so
        a sick tracker must not break the page.
        """
        out: dict[str, dict[str, TrackerFreeleechEntry]] = {}
        for plugin in self.router.plugins.values():
            if not plugin.can(Capability.FREELEECH_STATUS):
                continue
            try:
                rows = await plugin.freeleech_status()
            except Exception as exc:  # noqa: BLE001 - a sick tracker must not break the view
                log.warning("%s: could not read freeleech status: %s", plugin.id, exc)
                continue
            if rows:
                out[plugin.id] = {normalise_release(r.name): r for r in rows}
        return out

    async def _record_obligations(
        self, requirements: dict[str, dict[str, SeedingRequirement]], answered: set[str]
    ) -> None:
        """Persist what is owed, and settle what no longer is.

        This is the whole basis of the dashboard's seeding metrics: an
        obligation is only observable as "averted" if we knew it existed, knew
        we acted on it, and then saw the tracker stop asking.
        """
        for tracker_id in answered:
            by_name = requirements.get(tracker_id, {})
            await db.record_seeding_obligations(
                tracker_id, [(key, req.name) for key, req in by_name.items()]
            )
            settled = await db.clear_seeding_obligations(tracker_id, set(by_name))
            if settled:
                log.info("%s: %d seeding obligation(s) no longer listed", tracker_id, settled)

    async def _note_intervention(self, tracker: str, name: str, result: ActionResult | None) -> None:
        """Credit a repair against the seeding debt it was aimed at.

        Only a successful, real action counts. A dry run changed nothing and a
        failed step changed nothing, so counting either would let the dashboard
        claim hit-and-runs it did not avert.
        """
        if result is None or not result.ok or result.dry_run or not tracker:
            return
        await db.note_seeding_intervention(tracker, normalise_release(name), str(result.kind))

    def _seeding_report(
        self,
        requirements: dict[str, dict[str, SeedingRequirement]],
        matched: dict[tuple[str, str], str],
        recovered: set[tuple[str, str]],
    ) -> list[dict[str, str]]:
        """Every outstanding seeding obligation, with what is being done about it.

        Flat rather than grouped, and unsorted here: the notifier groups and
        sorts for display, and the dashboard may want a different order. This is
        the data, not a rendering of it.
        """
        rows: list[dict[str, str]] = []
        for tracker_id, by_name in requirements.items():
            plugin = self.router.get(tracker_id)
            for key, req in by_name.items():
                status = matched.get((tracker_id, key))
                if status is None:
                    status = (
                        "re-added from the tracker"
                        if (tracker_id, req.torrent_id) in recovered
                        else "not in the client"
                    )
                rows.append(
                    {
                        "tracker": tracker_id,
                        "tracker_name": (plugin.name if plugin else "") or tracker_id,
                        "name": req.name,
                        "detail": req.summary(),
                        "status": status,
                    }
                )
        return rows

    async def _redownload_missing(
        self, unmatched: list[tuple[str, SeedingRequirement]], summary: ScanSummary
    ) -> set[tuple[str, str]]:
        """Re-acquire torrents the tracker wants seeded but the client lacks.

        Returns the (tracker, torrent id) pairs that were re-added, so they stop
        being reported as unfixable. Capped per scan on purpose: if name
        matching ever broke — a tracker changing how it renders release names is
        all it would take — every requirement would look unmatched at once, and
        an uncapped loop would answer that by downloading the lot.
        """
        recovered: set[tuple[str, str]] = set()
        cfg = self.cfg.recovery
        if not (cfg.enabled and cfg.redownload_missing and self.client and self.engine):
            return recovered

        budget = max(cfg.redownload_max_per_scan, 0)
        for tracker_id, requirement in unmatched:
            if budget <= 0:
                log.info(
                    "re-download budget of %d used up; %s and the rest wait for the next scan",
                    cfg.redownload_max_per_scan,
                    requirement.name[:40],
                )
                break

            plugin = self.router.get(tracker_id)
            if plugin is None:
                continue
            try:
                result = await redownload(self.client, plugin, requirement, cfg)
            except Exception:  # one bad row must not end the scan
                log.exception("re-download of %s failed", requirement.name[:60])
                continue
            if result is None:
                continue  # already tried recently, or tried too often

            budget -= 1
            summary.actions += 1
            if result.ok:
                recovered.add((tracker_id, requirement.torrent_id))
            else:
                summary.errors.append(f"{requirement.name[:40]}: {result.message}")

            await self.engine.log_action(
                infohash=redownload_key(tracker_id, requirement.torrent_id),
                name=requirement.name,
                tracker=tracker_id,
                ailment=str(Ailment.HIT_AND_RUN_RISK),
                result=result,
            )
            await self._note_intervention(tracker_id, requirement.name, result)
        return recovered

    async def _recheck_candidates(self, torrents: list[Torrent], ignored: set[str]) -> set[str]:
        """Escalated torrents due for a "does this still exist" lookup.

        Escalation is otherwise the end of the line: `_already_escalated` stops
        enrichment forever, which is right for saving API calls and wrong for
        one question. A tracker that deletes a torrent after we gave up on it —
        the routine fate of a single episode once the season pack lands — leaves
        it sitting under a stale diagnosis that no repair can ever fix. Asking
        again occasionally is what turns that into `unregistered`, which is the
        one verdict that means "you can delete this".

        Least-recently-checked first, capped per scan, so this adds a handful of
        calls to a scan rather than a burst of hundreds.
        """
        rules = self.cfg.rules
        budget = max(rules.recheck_escalated_max_per_scan, 0)
        if rules.recheck_escalated_days <= 0 or not budget:
            return set()

        verified = await db.verified_map()
        cutoff = time.time() - rules.recheck_escalated_days * 86400
        due = [
            torrent
            for torrent in torrents
            if self._already_escalated(torrent)
            and not should_skip(torrent, rules, ignored)
            and verified.get(torrent.infohash, 0) < cutoff
        ]
        due.sort(key=lambda t: verified.get(t.infohash, 0))
        chosen = {torrent.infohash for torrent in due[:budget]}
        if due:
            log.info(
                "re-checking %d of %d escalated torrent(s) against their tracker",
                len(chosen),
                len(due),
            )
        return chosen

    async def _reap(
        self, diagnoses: list[Diagnosis], totals: dict[str, int], summary: ScanSummary
    ) -> set[str]:
        """Delete torrents the tracker has deleted. Returns what went.

        The ledger behind the soak period is maintained even when deleting is
        off, so `reap` runs unconditionally and decides for itself. Failures are
        contained here: a client that refuses one delete must not stop the scan.
        """
        if self.client is None or self.engine is None:
            return set()

        # Computed here as well as inside `reap` — it is a cheap pure function
        # over the same inputs, and the alternative is an out-parameter.
        self.deletion_blocks = [
            dict(detail)
            for detail in breaker_report(
                diagnoses, totals, self.cfg.recovery.delete_max_tracker_fraction
            ).values()
        ]

        try:
            outcomes = await reap(self.client, diagnoses, self.cfg.recovery, totals)
        except Exception:
            log.exception("deleting unregistered torrents failed")
            return set()

        gone: set[str] = set()
        for diagnosis, result in outcomes:
            summary.actions += 1
            if result.ok and not result.dry_run:
                gone.add(diagnosis.torrent.infohash)
            elif not result.ok:
                summary.errors.append(f"{diagnosis.torrent.name[:40]}: {result.message}")
            await self.engine.log_action(
                infohash=diagnosis.torrent.infohash,
                name=diagnosis.torrent.name,
                tracker=diagnosis.tracker_id or "",
                ailment=str(Ailment.UNREGISTERED),
                result=result,
            )
            await self.notifier.alert(diagnosis, result)
        if gone:
            log.warning(
                "deleted %d unregistered torrent(s)%s",
                len(gone),
                " and their files" if self.cfg.recovery.delete_unregistered_files else "",
            )
        return gone

    async def _record_snapshot(self, summary: ScanSummary) -> None:
        """One row per finished scan, so the dashboard can show drift.

        Never allowed to fail a scan: this is bookkeeping for a graph, and a
        graph is not worth losing a repair run over.
        """
        try:
            await db.record_snapshot(
                ts=int(summary.finished_at),
                total=summary.total,
                healthy=summary.healthy,
                sick=summary.sick,
                skipped=summary.skipped,
                actions=summary.actions,
                condemned=len(await db.list_condemned()),
                owed=len(self.seeding_report),
                duration=summary.duration,
                uploaded=self.library.get("uploaded", 0),
                downloaded=self.library.get("downloaded", 0),
                upspeed=self.library.get("upspeed", 0),
                seeding=self.library.get("seeding", 0),
                uploading=self.library.get("uploading", 0),
                recovered=summary.recovered,
            )
        except Exception:
            log.debug("could not record scan snapshot", exc_info=True)

    async def _refresh_disk(self) -> None:
        """Free space where the client writes, if it will tell us."""
        if self.client is None:
            return
        try:
            free = await self.client.free_space()
        except ClientError as exc:
            log.debug("could not read free space: %s", exc)
            return
        if free is not None:
            self.disk = {"free": free}

    def _already_escalated(self, torrent: Torrent) -> bool:
        """Has stallarr already given up on this torrent and told a human?

        Such a torrent is re-diagnosed locally (free) but not re-enriched. The
        enrichment already happened on the scan that escalated it, and repeating
        it buys nothing: no further repair will be attempted either way. On a
        large library that difference is thousands of private-tracker API calls
        a day against a permanently dead torrent — a good way to get banned.

        Clearing the tag, which "Reset" in the UI does, brings it back.
        """
        tag = self.cfg.recovery.escalation_tag
        return bool(tag) and tag in torrent.tags

    async def _handle(self, diagnosis: Diagnosis, summary: ScanSummary) -> ActionResult | None:
        if not self.cfg.recovery.enabled or self.engine is None:
            return None
        result = await self.engine.repair(diagnosis)
        if result is not None:
            summary.actions += 1
            if not result.ok:
                summary.errors.append(f"{diagnosis.torrent.name[:40]}: {result.message}")
            finding = diagnosis.primary
            if finding and finding.ailment is Ailment.HIT_AND_RUN_RISK:
                await self._note_intervention(
                    diagnosis.tracker_id or "", diagnosis.torrent.name, result
                )
        return result

    async def repair_now(self, infohash: str) -> dict[str, Any]:
        """Force one recovery step on a single torrent, from the WebUI."""
        if self.client is None or self.engine is None:
            return {"ok": False, "message": "service is not initialised"}

        torrent = await self.client.get_torrent(infohash)
        if torrent is None:
            return {"ok": False, "message": "torrent not found in client"}

        plugin = self.router.for_hosts(torrent.tracker_hosts)
        diagnosis = diagnose_local(torrent, self.cfg.rules, plugin)
        if diagnosis.healthy:
            return {"ok": True, "message": "torrent looks healthy — nothing to do"}

        if await enrich(diagnosis, plugin, self.cfg.rules):
            await db.mark_verified(infohash)
        result = await self.engine.repair(diagnosis)
        if result is None:
            return {"ok": False, "message": "no recovery step is due (cooling down or exhausted)"}
        return {"ok": result.ok, "action": str(result.kind), "message": result.message}

    async def reset_ladder(self, infohash: str) -> None:
        """Clear attempt history so recovery starts from the top again."""
        await db.clear_attempts(infohash)
        if self.client and self.cfg.recovery.tagging_enabled:
            try:
                await self.client.remove_tags(
                    infohash, [self.cfg.recovery.escalation_tag, self.cfg.recovery.working_tag]
                )
            except ClientError as exc:
                log.debug("could not clear tags: %s", exc)

    # --- state for the UI -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "scanning": self.scanning,
            "client": self.client_status,
            "trackers": self.tracker_status,
            "last_scan": {
                **asdict(self.last_scan),
                "duration": round(self.last_scan.duration, 2),
            },
            "scan_interval_minutes": self.cfg.scan_interval_minutes,
            "recovery": {
                "enabled": self.cfg.recovery.enabled,
                "dry_run": self.cfg.recovery.dry_run,
            },
            "issues": [_diagnosis_json(d) for d in self.last_diagnoses],
            "library": self.library,
            "problems_by_ailment": problems_by_ailment(self.last_diagnoses),
            # Seeding owed on torrents the client does not hold — nothing here
            # can repair these, so they need to be visible rather than counted.
            "seeding_unmatched": self.seeding_unmatched,
            # Trackers where deletion is currently refused, and on what evidence.
            "deletion_blocks": self.deletion_blocks,
            "disk": self.disk,
            "prowlarr": {
                "auto_sync": self.cfg.prowlarr.enabled and self.cfg.prowlarr.auto_sync,
                "interval_minutes": max(
                    self.cfg.prowlarr.sync_interval_minutes,
                    config_module.MIN_PROWLARR_SYNC_MINUTES,
                ),
                "last": self.prowlarr_sync,
            },
        }

    async def dashboard(self) -> dict[str, Any]:
        """`snapshot()` plus the parts that need a database read.

        Kept separate from `snapshot()` so that stays synchronous and cheap for
        every other caller.
        """
        return {
            **self.snapshot(),
            "seeding": await self.seeding_metrics(),
            "deletions": await db.deletion_totals(SEEDING_WINDOW_DAYS),
            "repairs": await db.repair_stats(SEEDING_WINDOW_DAYS),
            "condemned": await self.pending_deletions(),
        }

    async def pending_deletions(self) -> list[dict[str, Any]]:
        """Torrents queued for deletion, soonest-first, with why they are not
        gone yet.

        This is the one screen that has to exist before a destructive feature is
        trustworthy: without it the only way to know what is about to be erased
        is to read the log.
        """
        cfg = self.cfg.recovery
        soak = max(cfg.delete_min_age_hours, 0) * 3600
        blocked = {str(block["tracker"]) for block in self.deletion_blocks}
        now = time.time()

        rows = []
        for row in await db.list_condemned():
            eligible_at = row["first_at"] + soak
            tracker = row["tracker"]
            if not cfg.enabled or not cfg.delete_unregistered:
                held = "deletion is switched off"
            elif tracker in blocked:
                held = "held: too many unregistered on this tracker to believe"
            elif now < eligible_at:
                held = f"waiting out the {cfg.delete_min_age_hours}h soak period"
            else:
                held = "eligible on the next scan"
            plugin = self.router.get(tracker)
            rows.append(
                {
                    **row,
                    "tracker_name": (plugin.name if plugin else "") or tracker,
                    "eligible_at": eligible_at,
                    "status": held,
                }
            )
        rows.sort(key=lambda r: r["eligible_at"])
        return rows

    async def seeding_metrics(self, days: int = SEEDING_WINDOW_DAYS) -> dict[str, Any]:
        """Obligation counts, with tracker ids resolved to display names.

        Naming happens here rather than in the database because a tracker's
        display name comes from the live plugin and can change; only its id is
        stable enough to store.
        """
        metrics = await db.seeding_metrics(days)
        for row in (*metrics["by_tracker"], *metrics["recent"]):
            plugin = self.router.get(row["tracker"])
            row["tracker_name"] = (plugin.name if plugin else "") or row["tracker"]
        return metrics

    async def tracker_stats(self) -> dict[str, Any]:
        """Per-tracker account stats, cross-checked against the local client.

        The cross-check is the point. A tracker reporting that you are seeding
        16 torrents while qBittorrent is seeding 20 of that tracker's torrents
        means four announces are not landing — those four earn you nothing and
        are exactly the "seeds that aren't uploading" worth chasing. Neither
        number reveals that on its own.
        """
        local = await self._local_seed_counts()
        library = {row["tracker"]: row for row in self.library.get("by_tracker", [])}
        out: dict[str, Any] = {}

        for plugin_id, plugin in self.router.plugins.items():
            entry: dict[str, Any] = {
                "name": plugin.name,
                "local_seeding": local.get(plugin_id, 0),
                # What this tracker's torrents are doing in the client, from
                # the last scan. Labelled "in client" in the UI: it is not the
                # account figure above it and must not be read as one.
                "local": library.get(plugin_id),
            }

            if plugin.can(Capability.USER_STATS):
                try:
                    stats = await plugin.user_stats()
                except TrackerError as exc:
                    entry["error"] = str(exc)
                    stats = None
                if stats:
                    entry.update(
                        {
                            "uploaded": stats.uploaded,
                            "downloaded": stats.downloaded,
                            "ratio": stats.ratio,
                            "bonus": stats.bonus,
                            "hit_and_runs": stats.hit_and_runs,
                            "seeding": stats.seeding,
                        }
                    )
                    if stats.seeding is not None:
                        entry["unseen_seeds"] = max(entry["local_seeding"] - stats.seeding, 0)
                    tracker_cfg = self.cfg.trackers.get(plugin_id)
                    floor = getattr(tracker_cfg, "min_ratio", 0.0) or 0.0
                    # Which floor the headroom was measured against, so a
                    # number with no floor configured is not mistaken for one
                    # measured against a rule the user set.
                    entry["ratio_floor"] = floor if floor > 0 else 1.0
                    entry["ratio_floor_configured"] = floor > 0
                    entry["headroom"] = download_headroom(stats.uploaded, stats.downloaded, floor)
                    entry["buffer"] = (
                        stats.uploaded - stats.downloaded
                        if stats.uploaded is not None and stats.downloaded is not None
                        else None
                    )
                    entry["warnings"] = _account_warnings(stats, tracker_cfg)

            out[plugin_id] = entry
        return out

    async def seeding_view(self) -> dict[str, Any]:
        """Every actively-seeding torrent, grouped by tracker, for the Seeding tab.

        Ratio, uploaded, seeding time and tags come free off the local
        `Torrent` — no tracker call needed. Freeleech is best-effort and
        opt-in per tracker (`Capability.FREELEECH_STATUS`); trackers without it
        report `None`, not `False`. "Recently recovered" is inferred from
        `history` rather than read off a flag, because tags are cleared the
        moment a torrent actually recovers (`RecoveryEngine.on_recovered`) —
        there is nothing left to read by the time this runs.

        Called on demand from the route, not from the scan loop: this is
        display data, not recovery state, so it should not inherit the scan's
        cadence or error budget, the same reasoning `tracker_stats` already
        follows for `user_stats()`.
        """
        if self.client is None:
            return {"trackers": {}}
        try:
            torrents = await self.client.list_torrents()
        except ClientError as exc:
            log.debug("could not list torrents for seeding view: %s", exc)
            return {"trackers": {}}

        freeleech, recovered = await asyncio.gather(
            self._freeleech_status(),
            db.recently_recovered_hashes(RECENTLY_RECOVERED_DAYS),
        )

        active_tags = {self.cfg.recovery.escalation_tag, self.cfg.recovery.working_tag}
        out: dict[str, list[dict[str, Any]]] = {}
        for torrent in torrents:
            if not torrent.is_complete or torrent.is_paused:
                continue
            plugin = self.router.for_hosts(torrent.tracker_hosts)
            if plugin is None:
                continue

            fl_entry = freeleech.get(plugin.id, {}).get(normalise_release(torrent.name))
            recently_recovered = torrent.infohash in recovered and not (
                active_tags & set(torrent.tags)
            )
            out.setdefault(plugin.id, []).append(
                {
                    "infohash": torrent.infohash,
                    "name": torrent.name,
                    "tracker": plugin.id,
                    "tracker_name": plugin.name,
                    "ratio": round(torrent.ratio, 3),
                    "uploaded": torrent.uploaded,
                    "seeding_time": torrent.seeding_time,
                    "tags": torrent.tags,
                    "freeleech": fl_entry.freeleech if fl_entry else None,
                    "recently_recovered": recently_recovered,
                }
            )
        return {"trackers": out}

    async def _local_seed_counts(self) -> dict[str, int]:
        """How many torrents we are actually seeding, per tracker."""
        if self.client is None:
            return {}
        try:
            torrents = await self.client.list_torrents()
        except ClientError as exc:
            log.debug("could not count local seeds: %s", exc)
            return {}

        counts: dict[str, int] = {}
        for torrent in torrents:
            if not torrent.is_complete or torrent.is_paused:
                continue
            plugin = self.router.for_hosts(torrent.tracker_hosts)
            if plugin is not None:
                counts[plugin.id] = counts.get(plugin.id, 0) + 1
        return counts


def _account_warnings(stats: Any, tracker_cfg: Any) -> list[str]:
    """Account-level trouble that no per-torrent finding would ever surface.

    Everything else in stallarr reacts to a torrent that has already gone wrong.
    These two look at where the account is heading, which is the only warning
    that arrives while there is still time to do something about it.
    """
    if tracker_cfg is None:
        return []

    warnings: list[str] = []
    floor = getattr(tracker_cfg, "min_ratio", 0.0) or 0.0
    if floor > 0 and stats.ratio is not None and stats.ratio <= floor:
        warnings.append(f"ratio {stats.ratio:.2f} is at or below your {floor:.2f} floor")

    limit = getattr(tracker_cfg, "max_hit_and_runs", 0) or 0
    if limit > 0 and stats.hit_and_runs is not None and stats.hit_and_runs >= limit:
        warnings.append(f"{stats.hit_and_runs} hit-and-run(s), at your limit of {limit}")

    return warnings


def _local_state(torrent: Torrent) -> str:
    """Why this torrent is or is not currently paying off its seeding debt.

    Goes straight into the digest notification, where "stopped at its share
    limit" and "stopped" send you to two completely different places — the first
    is a setting to change, the second a torrent to start.
    """
    from .recovery.actions import blocking_limits

    if torrent.upspeed > 0:
        return "uploading"
    if torrent.is_paused:
        blocking = blocking_limits(torrent)
        return f"stopped at its {' and '.join(blocking)} limit" if blocking else "stopped"
    if not torrent.is_complete:
        return f"still downloading ({torrent.progress:.0%})"
    return "seeding, no peers"


def _apply_seeding_requirement(
    diagnosis: Diagnosis,
    requirements: dict[str, dict[str, SeedingRequirement]],
    matched: dict[tuple[str, str], str],
) -> None:
    """Fold a tracker's "you owe seeding on this" row into the diagnosis.

    The site naming a torrent is stronger evidence than anything inferred
    locally, and it cuts both ways. It is a live hit-and-run countdown — and it
    is proof the torrent still exists there, so it also clears an `unregistered`
    finding. That combination matters: `unregistered` escalates immediately and
    terminally, which is exactly how a torrent ends up stopped while the tracker
    counts down against it.

    A torrent that is already uploading is left alone. It is doing the one thing
    that clears the debt, and reannouncing it would burn the ladder for nothing.
    """
    by_name = requirements.get(diagnosis.tracker_id or "")
    if not by_name:
        return

    key = normalise_release(diagnosis.torrent.name)
    requirement = by_name.get(key)
    if requirement is None:
        return

    matched[(diagnosis.tracker_id or "", key)] = _local_state(diagnosis.torrent)
    if diagnosis.torrent.upspeed > 0 and not diagnosis.torrent.is_paused:
        return

    diagnosis.findings = [f for f in diagnosis.findings if f.ailment != Ailment.UNREGISTERED]
    # Front of the list so it wins the severity tie in `Diagnosis.primary`
    # against another CRITICAL finding and drives the ladder itself.
    diagnosis.findings.insert(
        0, Finding(Ailment.HIT_AND_RUN_RISK, f"tracker requires seeding — {requirement.summary()}")
    )


def _was_previously_sick(torrent: Torrent, cfg: Config) -> bool:
    return any(tag.startswith("stallarr:") for tag in torrent.tags)


def _diagnosis_json(diagnosis: Diagnosis) -> dict[str, Any]:
    torrent = diagnosis.torrent
    return {
        "infohash": torrent.infohash,
        "name": torrent.name,
        "state": str(torrent.state),
        "raw_state": torrent.raw_state,
        "progress": round(torrent.progress, 4),
        "size": torrent.size,
        "ratio": round(torrent.ratio, 3),
        "upspeed": torrent.upspeed,
        "dlspeed": torrent.dlspeed,
        "category": torrent.category,
        "tags": torrent.tags,
        "tracker": diagnosis.tracker_id,
        "tracker_hosts": torrent.tracker_hosts,
        "severity": int(diagnosis.severity),
        "severity_name": Severity(diagnosis.severity).name,
        "findings": [
            {"ailment": str(f.ailment), "detail": f.detail, "severity": int(f.severity)}
            for f in diagnosis.findings
        ],
    }
