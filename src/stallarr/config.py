"""Configuration: JSON file in DATA_DIR, overridable by STALLARR_* env vars.

The WebUI writes this file; env vars win at load time so a compose file can pin
secrets. Unknown keys are preserved on save so a newer schema never eats config.

That precedence has a sharp edge worth knowing about: a field pinned by an env
var *cannot* be changed from the WebUI — the save succeeds, and then the next
load quietly puts the env value back. :func:`env_pinned_paths` exists so the UI
can show those fields as read-only instead of pretending the edit took.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import DEFAULT_LADDER

DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/config" if Path("/config").exists() else "./data"))
CONFIG_PATH = DATA_DIR / "config.json"

_lock = threading.Lock()


class QbitConfig(BaseModel):
    url: str = "http://localhost:8080"
    username: str = ""
    password: str = ""
    verify_ssl: bool = True
    enabled: bool = True


class RuleConfig(BaseModel):
    """Thresholds that decide when a torrent counts as sick."""

    #: A downloading torrent with no progress for this long is stalled.
    stalled_download_minutes: int = 45
    #: A seeding torrent with zero peers and no upload for this long is stalled.
    stalled_upload_minutes: int = 240
    #: Grace period after a torrent is added before any rule can fire.
    new_torrent_grace_minutes: int = 15
    #: Torrents whose last announce is older than this are "not announcing".
    announce_timeout_minutes: int = 60
    #: Warn when seed time is within this fraction of the tracker's requirement.
    hnr_margin: float = 0.15
    #: Only inspect these categories (empty = all).
    only_categories: list[str] = Field(default_factory=list)
    ignore_categories: list[str] = Field(default_factory=list)
    #: Torrents carrying any of these tags are skipped entirely.
    ignore_tags: list[str] = Field(default_factory=lambda: ["stallarr:ignore"])
    #: Skip torrents that are still queued by the client.
    ignore_queued: bool = True
    #: How often to re-ask the tracker about a torrent stallarr has given up on.
    #: Escalated torrents are otherwise never looked up again, so a site that
    #: deletes one later — the usual fate of a single episode once a season pack
    #: lands — would go unnoticed, and the torrent would sit in Open Issues under
    #: whatever it was last diagnosed with. 0 disables the re-check.
    recheck_escalated_days: int = 7
    #: Ceiling on those re-checks per scan. The age gate above is the real
    #: limiter; this one stops the first scan after a restart from looking up
    #: every escalated torrent at once.
    recheck_escalated_max_per_scan: int = 5
    #: Treat any paused torrent as a fault. Off by default so stallarr does not
    #: fight you over torrents you paused on purpose; it always acts on pauses
    #: it caused itself.
    flag_user_paused: bool = False


class RecoveryConfig(BaseModel):
    enabled: bool = True
    dry_run: bool = False
    #: Which recovery steps are permitted at all. Removing one disables it
    #: everywhere. The order steps run in is decided per-ailment in
    #: models.AILMENT_ACTIONS, not here.
    ladder: list[str] = Field(default_factory=lambda: [str(step) for step in DEFAULT_LADDER])
    #: Which generation of the ladder this config was written against. A saved
    #: `ladder` is a permission list, so a step added to stallarr later is absent
    #: from it and would be silently disabled forever on every existing install.
    #: `load()` appends steps introduced since this version and bumps it — which
    #: only works because it can tell "did not exist yet" from "deliberately
    #: removed". Defaults to 1 so pre-versioning configs migrate too.
    ladder_version: int = 1
    #: Minimum minutes between two attempts of the same step on one torrent.
    cooldown_minutes: dict[str, int] = Field(
        default_factory=lambda: {
            "resume": 10,
            "reannounce": 15,
            "refresh_announce": 120,
            "recheck": 360,
            "toggle": 60,
            "replace_torrent": 720,
            "redownload": 720,
            "escalate": 1440,
        }
    )
    #: How many times a step may run before we escalate to the next rung.
    max_attempts_per_step: int = 3
    #: Seconds to wait between pause and resume in the TOGGLE step.
    toggle_pause_seconds: int = 10
    #: Tag applied to torrents stallarr has given up on.
    escalation_tag: str = "stallarr:needs-help"
    #: Tag applied while a torrent is under active repair.
    working_tag: str = "stallarr:repairing"
    #: Apply tags to torrents in the client.
    tagging_enabled: bool = True

    # --- re-acquiring torrents the client does not hold ------------------------
    # When a tracker says we owe seeding on something that is not in the client,
    # no repair step can help: there is nothing to resume, reannounce or
    # recheck. Re-adding it from the tracker is the only way to seed it, and
    # seeding it is the only way the debt clears.

    #: Fetch a fresh .torrent and add it back when a seeding requirement matches
    #: nothing locally. Off by default: it puts data on your disk that you had
    #: removed, and if the files are gone it costs the download again.
    redownload_missing: bool = False
    #: Ceiling per scan. A tracker layout change that broke name matching would
    #: make every requirement look unmatched; this is what stops that turning
    #: into a bulk download.
    redownload_max_per_scan: int = 3
    #: Category for re-added torrents. With `redownload_use_auto_tmm` on, this
    #: also decides where they land — set it to the category the originals used
    #: and qBittorrent will find the existing files instead of downloading again.
    redownload_category: str = ""
    #: Explicit save path. Overrides automatic management when set.
    redownload_save_path: str = ""
    #: Let qBittorrent place the torrent from its category. This is the setting
    #: that makes a re-add free when the data is still on disk.
    redownload_use_auto_tmm: bool = True
    #: Tag applied to re-added torrents, so they are obvious in the client.
    redownload_tag: str = "stallarr:redownloaded"

    #: Delete torrents the tracker has itself deleted. Off by default: it is the
    #: only irreversible thing stallarr does.
    delete_unregistered: bool = False
    #: Delete the data too, not just the torrent entry. Independent of the
    #: switch above so "stop seeding it" and "erase it" stay separate decisions.
    delete_unregistered_files: bool = False
    #: How long a torrent must have been *continuously* diagnosed unregistered
    #: before it can be deleted. This is the main defence against a tracker API
    #: having a bad minute: an empty search result reads exactly like "deleted",
    #: and without a soak period one bad answer would erase a library.
    delete_min_age_hours: int = 24
    #: Ceiling on deletions per scan, so a fault that survives the soak period
    #: still costs a handful of torrents rather than all of them.
    delete_max_per_scan: int = 5
    #: Refuse to delete anything for a tracker when more than this fraction of
    #: its torrents are unregistered. Deliberately loose, because of what the
    #: two cases actually look like: a tracker whose lookup is broken answers
    #: "not found" for *everything*, landing near 1.0, while a site that simply
    #: prunes hard — superseded singles, trumped rips — lands anywhere from 0.05
    #: to 0.3 and is telling the truth. A threshold set down in that second
    #: range never fires on the fault it exists for and constantly fires on
    #: healthy libraries, which is how a safety rail ends up switched off.
    delete_max_tracker_fraction: float = 0.50


class TrackerConfig(BaseModel):
    """Per-tracker settings. `options` holds plugin-specific fields."""

    enabled: bool = False
    #: Site base URL, e.g. https://darkpeers.org. Plugin defaults apply if blank.
    url: str = ""
    #: UNIT3D API token / Torznab apikey / etc.
    api_key: str = ""
    #: Announce passkey, when the site exposes one separately.
    passkey: str = ""
    #: Raw Cookie header for sites without a usable API. Escape hatch for
    #: "copy the whole Cookie line out of devtools"; wins over the split fields
    #: below because it can carry cookies beyond uid/pass that a site needs.
    cookie: str = ""
    #: The same cookie as two separate values, which is how a browser's storage
    #: inspector presents it. Assembling the header ourselves stops the pasted
    #: string being rejected for a missing separator.
    cookie_uid: str = ""
    cookie_pass: str = ""
    #: Extra announce hostnames to match against, beyond the plugin defaults.
    extra_hosts: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)

    # --- config-defined trackers ----------------------------------------------
    # Set only when there is no plugin file for this tracker. Most private sites
    # run stock UNIT3D, so `plugin: "unit3d"` plus a URL and token is a complete,
    # working tracker with no code at all — which is how the Prowlarr import can
    # bring in sites nobody has written a plugin for. Write a plugin file when a
    # site needs real behaviour; these fields are ignored once one exists.

    #: Base plugin to drive this tracker with. See trackers.BASE_PLUGINS.
    plugin: str = ""
    #: Display name. Falls back to the config key.
    name: str = ""
    #: Announce hostnames identifying this tracker, matched as suffixes.
    hosts: list[str] = Field(default_factory=list)
    #: Set by the Prowlarr import so a re-sync knows what it may overwrite.
    source: str = ""


class ProwlarrConfig(BaseModel):
    """Prowlarr already knows every tracker you are on, and holds the API keys.

    Importing from it beats retyping them, and a re-sync picks up keys that were
    rotated on the Prowlarr side.
    """

    enabled: bool = False
    url: str = ""
    api_key: str = ""
    #: Re-import on every config reload, so rotated keys land without a click.
    auto_sync: bool = False


class NotifyConfig(BaseModel):
    discord_webhook_url: str = ""
    discord_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    ntfy_enabled: bool = False
    #: Server base URL. Self-hosted instances just change this.
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    #: Access token for a protected topic (ntfy calls these `tk_...`).
    ntfy_token: str = ""
    #: Username/password auth, for self-hosted servers that use it. A token
    #: wins when both are set.
    ntfy_username: str = ""
    ntfy_password: str = ""
    #: One digest per scan listing every torrent every tracker still wants
    #: seeded, grouped by tracker. On by default: it is the alert that maps
    #: directly onto losing an account, and the dedupe below means it only fires
    #: again when the list of torrents actually changes.
    seeding_digest_enabled: bool = True
    #: Only notify at or above this severity (2 = WARNING, 3 = CRITICAL).
    min_severity: int = 3
    #: Suppress repeat alerts for the same torrent+ailment for this long.
    dedupe_hours: int = 12


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8383
    #: Blank disables password login. With SSO configured that is the normal
    #: state; with neither set, the API is open.
    password: str = ""
    session_secret: str = ""
    #: External URL the dashboard is reached on. Also the default source of the
    #: OIDC redirect URI, so set it before turning SSO on behind a proxy.
    base_url: str = ""

    # --- single sign-on --------------------------------------------------------
    # OIDC authorization-code flow, run by stallarr itself rather than by a
    # reverse proxy: a container is usually reachable on its published port and
    # over the docker network as well as through the proxy, and forward-auth
    # headers protect only the last of those three.

    oidc_enabled: bool = False
    #: Shown on the sign-in button.
    oidc_display_name: str = "Single sign-on"
    #: Issuer base URL — Authentik's is https://<host>/application/o/<slug>/
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    #: Overrides the redirect URI derived from `base_url`. Needed only when the
    #: dashboard is reached on a different URL than it advertises.
    oidc_redirect_url: str = ""
    #: Restrict access to members of these IdP groups. Empty = anyone the IdP
    #: admits. Requires the `groups` claim; see web/oidc.py::_check_groups.
    oidc_allowed_groups: list[str] = Field(default_factory=list)
    #: Skip the sign-in page and bounce straight to the IdP.
    oidc_auto_redirect: bool = False


class Config(BaseModel):
    scan_interval_minutes: int = 10
    log_level: str = "INFO"
    history_retention_days: int = 30
    qbittorrent: QbitConfig = Field(default_factory=QbitConfig)
    rules: RuleConfig = Field(default_factory=RuleConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    prowlarr: ProwlarrConfig = Field(default_factory=ProwlarrConfig)
    #: Keyed by tracker plugin id, e.g. "iptorrents", "darkpeers".
    trackers: dict[str, TrackerConfig] = Field(default_factory=dict)


#: env var suffix -> dotted config path
_ENV_MAP = {
    "QBIT_URL": "qbittorrent.url",
    "QBIT_USERNAME": "qbittorrent.username",
    "QBIT_PASSWORD": "qbittorrent.password",
    "SCAN_INTERVAL_MINUTES": "scan_interval_minutes",
    "LOG_LEVEL": "log_level",
    "DRY_RUN": "recovery.dry_run",
    "WEB_PASSWORD": "web.password",
    "WEB_PORT": "web.port",
    "WEB_BASE_URL": "web.base_url",
    "OIDC_ENABLED": "web.oidc_enabled",
    "OIDC_ISSUER": "web.oidc_issuer",
    "OIDC_CLIENT_ID": "web.oidc_client_id",
    "OIDC_CLIENT_SECRET": "web.oidc_client_secret",
    "OIDC_REDIRECT_URL": "web.oidc_redirect_url",
    "DISCORD_WEBHOOK_URL": "notify.discord_webhook_url",
    "TELEGRAM_BOT_TOKEN": "notify.telegram_bot_token",
    "TELEGRAM_CHAT_ID": "notify.telegram_chat_id",
    "NTFY_URL": "notify.ntfy_url",
    "NTFY_TOPIC": "notify.ntfy_topic",
    "NTFY_TOKEN": "notify.ntfy_token",
    "NTFY_ENABLED": "notify.ntfy_enabled",
    "PROWLARR_URL": "prowlarr.url",
    "PROWLARR_API_KEY": "prowlarr.api_key",
    "PROWLARR_ENABLED": "prowlarr.enabled",
}

#: Tracker fields settable per tracker as STALLARR_TRACKER_<ID>_<FIELD>.
_TRACKER_ENV_FIELDS = (
    "api_key",
    "passkey",
    "cookie_uid",
    "cookie_pass",
    "cookie",
    "url",
    "enabled",
)


def _coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in ("true", "yes", "1"):
        return True
    if low in ("false", "no", "0"):
        return False
    try:
        return int(raw)
    except ValueError:
        return raw


def _assign(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor = data
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _env_assignments() -> Iterator[tuple[str, str, Any]]:
    """Yield (dotted path, env var name, value) for every STALLARR_* override.

    Single source of truth for both `_apply_env` and `env_pinned_paths` — if
    these two ever disagree the UI starts lying about which fields it can edit.
    """
    for suffix, dotted in _ENV_MAP.items():
        name = f"STALLARR_{suffix}"
        raw = os.environ.get(name)
        if raw is not None:
            yield dotted, name, _coerce(raw)

    # Per-tracker secrets: STALLARR_TRACKER_<ID>_API_KEY / _PASSKEY / _COOKIE / _URL
    for name, raw in os.environ.items():
        if not name.startswith("STALLARR_TRACKER_"):
            continue
        remainder = name[len("STALLARR_TRACKER_") :].lower()
        for field_name in _TRACKER_ENV_FIELDS:
            if remainder.endswith("_" + field_name):
                tracker_id = remainder[: -(len(field_name) + 1)]
                if tracker_id:
                    yield f"trackers.{tracker_id}.{field_name}", name, _coerce(raw)
                break


def _apply_env(data: dict) -> dict:
    for dotted, _name, value in _env_assignments():
        _assign(data, dotted, value)
    return data


def env_pinned_paths() -> dict[str, str]:
    """Dotted config path -> the env var that pins it.

    A pinned path cannot be changed from the WebUI: `load()` reapplies the env
    value over whatever was saved. The UI reads this to disable those inputs and
    name the variable responsible, rather than accepting an edit it will lose.
    """
    return {dotted: name for dotted, name, _value in _env_assignments()}


def load() -> Config:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = CONFIG_PATH.with_suffix(".json.corrupt")
            try:
                CONFIG_PATH.replace(backup)
            except OSError:
                pass
            data = {}

    cfg = Config.model_validate(_apply_env(data))
    dirty = _migrate_ladder(cfg)
    if not cfg.web.session_secret:
        cfg.web.session_secret = secrets.token_hex(32)
        dirty = True
    if dirty:
        save(cfg)
    return cfg


#: Steps added to DEFAULT_LADDER after a given `ladder_version`. Add an entry
#: here whenever you add a recovery step, or it will be disabled on every
#: config that already exists.
_LADDER_ADDITIONS: dict[int, list[str]] = {
    2: ["lift_share_limits"],
}
LADDER_VERSION = max(_LADDER_ADDITIONS, default=1)


def _migrate_ladder(cfg: Config) -> bool:
    """Grant newly-added recovery steps to an older config.

    Only steps introduced *after* the config was written are added, so a step
    the user deliberately removed stays removed. Returns True if anything
    changed and the config should be written back.
    """
    if cfg.recovery.ladder_version >= LADDER_VERSION:
        return False
    for version, steps in sorted(_LADDER_ADDITIONS.items()):
        if version <= cfg.recovery.ladder_version:
            continue
        for step in steps:
            if step not in cfg.recovery.ladder:
                cfg.recovery.ladder.insert(max(len(cfg.recovery.ladder) - 1, 0), step)
    cfg.recovery.ladder_version = LADDER_VERSION
    return True


def save(cfg: Config) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.model_dump(mode="json"), indent=2)
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(CONFIG_PATH)
