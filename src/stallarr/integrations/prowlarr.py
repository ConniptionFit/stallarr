"""Import tracker credentials from Prowlarr.

Prowlarr already holds the list of trackers you are on and the API keys for
them, so retyping those into stallarr is pure duplication — and duplication that
goes stale the moment a key is rotated.

What can and cannot come across, measured against a real Prowlarr rather than
assumed:

  * **API keys arrive in the clear.** `GET /api/v1/indexer` returns the `apikey`
    field verbatim. This is the valuable one: a UNIT3D token alone is enough for
    stallarr to answer "is this torrent still registered?" and to read account
    stats including the tracker's own hit-and-run counter.
  * **Cookies do not.** Prowlarr masks password-class fields as ``********``.
    Cookie-authenticated sites (IPTorrents) still need the cookie pasted in by
    hand, and we must never write the mask into config as if it were real.
  * **Announce passkeys are not there at all.** Prowlarr never needs one — it
    searches, it does not seed. On UNIT3D the download link exposes an *rsskey*,
    which is a different secret from the announce passkey, so it cannot be
    substituted. Passkeys stay manual.

Sites with no plugin file are still importable: stock UNIT3D is common enough
that a config entry pointing at the generic `unit3d` base plugin is a complete,
working tracker. We only propose that after confirming the site actually speaks
UNIT3D. A site that does not is still brought in, on the `generic` base: it makes
no API calls at all, so it cannot fail lookups, but its announce hosts route
torrents to it and stallarr monitors them from client data. Leaving it out made
"every tracker in Prowlarr" silently mean "every UNIT3D tracker in Prowlarr".

A `generic` verdict never downgrades a tracker already running on `unit3d`: the
probe is one request to a site that can be having a bad minute, and a site that
answered once has not stopped running UNIT3D.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Config, ProwlarrConfig, TrackerConfig
from ..trackers import discover, registry
from ..utils import normalise_host, redact_secrets

log = logging.getLogger(__name__)

#: Marker written into config by an import, so a re-sync knows it owns the entry.
SOURCE = "prowlarr"

#: Prowlarr redacts password-class fields to this rather than omitting them.
_MASK = re.compile(r"^\*+$")

#: Prowlarr field name -> stallarr TrackerConfig field.
_FIELD_MAP = {
    "apikey": "api_key",
    "api_key": "api_key",
    "cookie": "cookie",
    "passkey": "passkey",
    "torrent_pass": "passkey",
    # Deliberately absent: "rsskey". On UNIT3D it is a different secret from the
    # announce passkey, and with the recurring sync overwriting, mapping it would
    # replace a hand-entered passkey with one that fails every announce.
}


class ProwlarrError(RuntimeError):
    pass


@dataclass
class Proposal:
    """One Prowlarr indexer, resolved against stallarr's trackers."""

    tracker_id: str
    name: str
    url: str
    hosts: list[str] = field(default_factory=list)
    #: Field name -> value we would write. Secrets; never send to the browser.
    values: dict[str, str] = field(default_factory=dict)
    #: Base plugin for a config-defined tracker; empty when a plugin file exists.
    plugin: str = ""
    #: False when we could not work out how to talk to this site.
    supported: bool = True
    #: Human explanation of the match, shown in the UI.
    detail: str = ""

    def public(self) -> dict[str, Any]:
        """Browser-safe view: which secrets we have, never the secrets."""
        return {
            "tracker_id": self.tracker_id,
            "name": self.name,
            "url": self.url,
            "hosts": self.hosts,
            "plugin": self.plugin,
            "supported": self.supported,
            "detail": self.detail,
            "provides": sorted(self.values),
        }


# --- Prowlarr API ------------------------------------------------------------


class ProwlarrClient:
    def __init__(self, cfg: ProwlarrConfig) -> None:
        self.base_url = (cfg.url or "").rstrip("/")
        self.api_key = cfg.api_key

    async def indexers(self) -> list[dict[str, Any]]:
        if not self.base_url:
            raise ProwlarrError("no Prowlarr URL configured")
        if not self.api_key:
            raise ProwlarrError("no Prowlarr API key configured")

        url = f"{self.base_url}/api/v1/indexer"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                resp = await client.get(url, headers={"X-Api-Key": self.api_key})
        except httpx.HTTPError as exc:
            detail = redact_secrets(f"could not reach Prowlarr at {self.base_url}: {exc}")
            raise ProwlarrError(detail) from exc

        if resp.status_code in (401, 403):
            raise ProwlarrError("Prowlarr rejected the API key")
        if resp.status_code >= 400:
            raise ProwlarrError(f"Prowlarr returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProwlarrError("Prowlarr did not return JSON — is that URL Prowlarr?") from exc
        if not isinstance(payload, list):
            raise ProwlarrError("unexpected Prowlarr response shape")
        return payload


# --- planning ----------------------------------------------------------------


def _fields(indexer: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for item in indexer.get("fields") or []:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = item.get("value")
    return out


def _usable(value: Any) -> str | None:
    """A secret we can actually store, or None for blank and masked values."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _MASK.match(text):
        return None
    return text


def _hosts_of(indexer: dict[str, Any], fields: dict[str, Any]) -> list[str]:
    urls = list(indexer.get("indexerUrls") or [])
    for candidate in (indexer.get("baseUrl"), fields.get("baseUrl")):
        if isinstance(candidate, str) and candidate:
            urls.append(candidate)

    hosts: list[str] = []
    for url in urls:
        host = normalise_host(urlparse(url).hostname or "")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _site_url(indexer: dict[str, Any], fields: dict[str, Any]) -> str:
    for candidate in (fields.get("baseUrl"), indexer.get("baseUrl")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().rstrip("/")
    urls = indexer.get("indexerUrls") or []
    return str(urls[0]).rstrip("/") if urls else ""


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    # Prowlarr names the UNIT3D API definitions "<site>-api"; the suffix is an
    # artefact of how it talks to the site, not part of the site's name.
    return re.sub(r"api$", "", slug) or slug


def _match_existing(hosts: list[str], slug: str) -> str | None:
    """Find the plugin file that owns this indexer, by host then by name."""
    discover()
    for plugin_id, plugin_cls in registry.items():
        for known in plugin_cls.hosts:
            for host in hosts:
                if host == known or host.endswith("." + known) or known.endswith("." + host):
                    return plugin_id
    return slug if slug in registry else None


async def _speaks_unit3d(url: str, api_key: str) -> bool:
    """Ask the site directly rather than guessing from Prowlarr metadata.

    Prowlarr's own fields cannot tell us this: a Cardigann definition looks the
    same whether the site runs UNIT3D or scrapes HTML. Enabling a tracker that
    turns out not to speak UNIT3D would mean every lookup fails silently, so we
    spend one request to be sure.
    """
    if not url or not api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            resp = await client.get(
                f"{url}/api/torrents",
                params={"perPage": 1},
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                follow_redirects=True,
            )
    except httpx.HTTPError as exc:
        log.debug("UNIT3D probe of %s failed: %s", url, exc)
        return False
    if resp.status_code >= 400:
        return False
    try:
        payload = resp.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("data"), list)


async def plan(
    indexers: list[dict[str, Any]],
    *,
    probe: bool = True,
    known: dict[str, str] | None = None,
) -> list[Proposal]:
    """Resolve Prowlarr indexers into things stallarr could configure.

    `known` maps tracker id -> base plugin for trackers already in config. A
    site already confirmed as UNIT3D is not probed again: the recurring sync
    would otherwise spend a request per site per run re-learning what it knew.
    """
    known = known or {}
    proposals: list[Proposal] = []

    for indexer in indexers:
        if indexer.get("protocol") not in (None, "torrent"):
            continue  # usenet indexers have nothing to do with seeding

        fields = _fields(indexer)
        hosts = _hosts_of(indexer, fields)
        name = str(indexer.get("name") or indexer.get("definitionName") or "unknown")
        slug = _slug(str(indexer.get("definitionName") or name))
        url = _site_url(indexer, fields)

        values: dict[str, str] = {}
        for raw_name, raw_value in fields.items():
            target = _FIELD_MAP.get(raw_name.lower())
            usable = _usable(raw_value)
            if target and usable:
                values.setdefault(target, usable)

        matched = _match_existing(hosts, slug)
        if matched:
            proposals.append(
                Proposal(
                    tracker_id=matched,
                    name=name,
                    url=url,
                    hosts=hosts,
                    values=values,
                    detail=f"matches the {matched} plugin",
                )
            )
            continue

        has_key = bool(values.get("api_key"))
        already_unit3d = known.get(slug) == "unit3d"
        is_unit3d = has_key and (not probe or already_unit3d or await _speaks_unit3d(url, values["api_key"]))
        if is_unit3d:
            proposals.append(
                Proposal(
                    tracker_id=slug,
                    name=name,
                    url=url,
                    hosts=hosts,
                    values=values,
                    plugin="unit3d",
                    detail="no plugin file — UNIT3D API confirmed, will use the generic driver",
                )
            )
        elif hosts:
            proposals.append(
                Proposal(
                    tracker_id=slug,
                    name=name,
                    url=url,
                    hosts=hosts,
                    # Kept even though nothing uses them yet: a later sync that
                    # confirms UNIT3D, or a plugin file, picks them straight up.
                    values=values,
                    plugin="generic",
                    detail=(
                        "no plugin file and no UNIT3D API answered — added for monitoring "
                        "from client data; site lookups need a plugin (ADDING_A_TRACKER.md)"
                    ),
                )
            )
        else:
            proposals.append(
                Proposal(
                    tracker_id=slug,
                    name=name,
                    url=url,
                    hosts=hosts,
                    values={},
                    supported=False,
                    detail="Prowlarr lists no URL for this indexer, so its torrents cannot be matched",
                )
            )

    return proposals


# --- applying ----------------------------------------------------------------


def apply(
    cfg: Config,
    proposals: list[Proposal],
    *,
    overwrite: bool = False,
    enable: bool = True,
) -> dict[str, Any]:
    """Write proposals into `cfg` in place. Returns a summary for the UI.

    Blank fields are filled; existing ones are left alone unless `overwrite` is
    set. That default matters — the import must not clobber a cookie or passkey
    you entered by hand, since Prowlarr cannot supply either.

    `enable` applies to newly created trackers only. A tracker you already have
    keeps whatever enabled state you gave it — there is no way to tell "disabled
    because it was never set up" from "disabled on purpose", so a re-sync must
    not decide that for you.
    """
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for proposal in proposals:
        if not proposal.supported:
            skipped.append({"tracker_id": proposal.tracker_id, "reason": proposal.detail})
            continue

        existing = cfg.trackers.get(proposal.tracker_id)
        tracker = existing.model_copy(deep=True) if existing else TrackerConfig()
        changed: list[str] = []

        for field_name, value in proposal.values.items():
            current = getattr(tracker, field_name, "")
            if current and not overwrite:
                continue
            if current == value:
                continue
            setattr(tracker, field_name, value)
            changed.append(field_name)

        if proposal.url and (not tracker.url or overwrite) and tracker.url != proposal.url:
            tracker.url = proposal.url
            changed.append("url")

        # A config-defined tracker needs its identity written down; one backed by
        # a plugin file gets the extra announce domains Prowlarr knows about.
        if proposal.plugin:
            # One failed probe must not demote a site that answered as UNIT3D
            # before; see the module docstring.
            # An existing entry with no base set already runs on unit3d (that is
            # build_plugin's default), so it counts as confirmed too.
            downgrade = proposal.plugin == "generic" and existing is not None and tracker.plugin != "generic"
            if not downgrade and tracker.plugin != proposal.plugin:
                tracker.plugin = proposal.plugin
                if existing is not None:
                    changed.append("plugin")
            tracker.name = tracker.name or proposal.name
            merged = list(dict.fromkeys([*tracker.hosts, *proposal.hosts]))
            if merged != tracker.hosts:
                tracker.hosts = merged
                changed.append("hosts")
        else:
            merged = list(dict.fromkeys([*tracker.extra_hosts, *proposal.hosts]))
            if merged != tracker.extra_hosts:
                tracker.extra_hosts = merged
                changed.append("extra_hosts")

        tracker.source = SOURCE
        if existing is None:
            tracker.enabled = enable
            changed.append("created")

        cfg.trackers[proposal.tracker_id] = tracker
        imported.append(
            {
                "tracker_id": proposal.tracker_id,
                "name": proposal.name,
                "changed": changed,
                "enabled": tracker.enabled,
                "detail": proposal.detail,
            }
        )

    return {"imported": imported, "skipped": skipped}


async def sync(
    cfg: Config, *, overwrite: bool = False, probe: bool = True, enable: bool = True
) -> dict[str, Any]:
    """Fetch, plan and apply in one go. Mutates `cfg`; the caller saves it."""
    indexers = await ProwlarrClient(cfg.prowlarr).indexers()
    # A blank base means unit3d to build_plugin, so it means unit3d here too.
    known = {tracker_id: t.plugin or "unit3d" for tracker_id, t in cfg.trackers.items()}
    proposals = await plan(indexers, probe=probe, known=known)
    result = apply(cfg, proposals, overwrite=overwrite, enable=enable)
    result["added"] = [i["tracker_id"] for i in result["imported"] if "created" in i["changed"]]
    result["updated"] = [
        i["tracker_id"] for i in result["imported"] if i["changed"] and "created" not in i["changed"]
    ]
    result["proposals"] = [p.public() for p in proposals]
    return result
