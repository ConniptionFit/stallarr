"""Core domain types shared by clients, diagnosis, recovery and trackers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from urllib.parse import urlparse

from .utils import human_bytes


class TorrentState(StrEnum):
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    CHECKING = "checking"
    QUEUED = "queued"
    STALLED_DL = "stalled_dl"
    STALLED_UP = "stalled_up"
    MOVING = "moving"
    ERROR = "error"
    MISSING_FILES = "missing_files"
    UNKNOWN = "unknown"


#: qBittorrent tracker `status` field.
TRACKER_DISABLED = 0
TRACKER_NOT_CONTACTED = 1
TRACKER_WORKING = 2
TRACKER_UPDATING = 3
TRACKER_NOT_WORKING = 4


@dataclass(slots=True)
class TrackerEntry:
    url: str
    status: int = TRACKER_NOT_CONTACTED
    message: str = ""
    num_peers: int = 0
    num_seeds: int = -1
    num_leeches: int = -1

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").lower()

    @property
    def is_special(self) -> bool:
        """DHT/PeX/LSD pseudo-entries qBittorrent injects into the tracker list."""
        return not self.url.startswith(("http://", "https://", "udp://"))

    @property
    def working(self) -> bool:
        return self.status in (TRACKER_WORKING, TRACKER_UPDATING)


@dataclass(slots=True)
class Torrent:
    infohash: str
    name: str
    state: TorrentState
    raw_state: str = ""
    progress: float = 0.0
    size: int = 0
    amount_left: int = 0
    downloaded: int = 0
    uploaded: int = 0
    ratio: float = 0.0
    num_seeds: int = 0
    num_leechs: int = 0
    dlspeed: int = 0
    upspeed: int = 0
    added_on: int = 0
    completion_on: int = 0
    last_activity: int = 0
    time_active: int = 0
    seeding_time: int = 0
    #: Per-torrent share limits. -1 means "never stop", -2 "use the global
    #: setting"; any positive value is a cap the client enforces by *stopping*
    #: the torrent, which is invisible in the state name and looks exactly like
    #: someone pausing it by hand.
    ratio_limit: float = -2.0
    seeding_time_limit: int = -2
    inactive_seeding_time_limit: int = -2
    #: What the client does when a limit is reached ("Default", "Stop",
    #: "Remove", …). qBittorrent 5 requires it on every share-limit write, so it
    #: has to be read back and resent — omitting it is rejected outright, and
    #: guessing a value would silently rewrite a torrent's disposal policy.
    #: Empty on qBittorrent 4, which has no such field.
    share_limit_action: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    save_path: str = ""
    eta: int = 0
    availability: float = 0.0
    trackers: list[TrackerEntry] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0

    @property
    def is_paused(self) -> bool:
        return self.state is TorrentState.PAUSED

    @property
    def real_trackers(self) -> list[TrackerEntry]:
        return [t for t in self.trackers if not t.is_special]

    @property
    def tracker_hosts(self) -> list[str]:
        return [t.host for t in self.real_trackers if t.host]

    @property
    def primary_tracker(self) -> TrackerEntry | None:
        real = self.real_trackers
        return real[0] if real else None


class Severity(IntEnum):
    OK = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3


class Ailment(StrEnum):
    """A specific, actionable thing wrong with a torrent."""

    UNREGISTERED = "unregistered"
    TRACKER_DOWN = "tracker_down"
    TRACKER_AUTH = "tracker_auth"
    NOT_ANNOUNCING = "not_announcing"
    STALLED_DOWNLOAD = "stalled_download"
    STALLED_UPLOAD = "stalled_upload"
    MISSING_FILES = "missing_files"
    CLIENT_ERROR = "client_error"
    UNEXPECTEDLY_PAUSED = "unexpectedly_paused"
    HIT_AND_RUN_RISK = "hit_and_run_risk"


#: Ailments that can never be fixed by retrying — escalate to a human instead.
TERMINAL_AILMENTS = frozenset({Ailment.UNREGISTERED})

AILMENT_SEVERITY: dict[Ailment, Severity] = {
    Ailment.UNREGISTERED: Severity.CRITICAL,
    Ailment.MISSING_FILES: Severity.CRITICAL,
    Ailment.HIT_AND_RUN_RISK: Severity.CRITICAL,
    Ailment.TRACKER_AUTH: Severity.CRITICAL,
    Ailment.CLIENT_ERROR: Severity.WARNING,
    Ailment.NOT_ANNOUNCING: Severity.WARNING,
    Ailment.STALLED_DOWNLOAD: Severity.WARNING,
    Ailment.UNEXPECTEDLY_PAUSED: Severity.WARNING,
    Ailment.STALLED_UPLOAD: Severity.INFO,
    Ailment.TRACKER_DOWN: Severity.INFO,
}


@dataclass(slots=True)
class Finding:
    ailment: Ailment
    detail: str

    @property
    def severity(self) -> Severity:
        return AILMENT_SEVERITY.get(self.ailment, Severity.WARNING)


@dataclass(slots=True)
class Diagnosis:
    torrent: Torrent
    findings: list[Finding] = field(default_factory=list)
    tracker_id: str | None = None

    @property
    def healthy(self) -> bool:
        return not self.findings

    @property
    def severity(self) -> Severity:
        return max((f.severity for f in self.findings), default=Severity.OK)

    @property
    def ailments(self) -> list[Ailment]:
        return [f.ailment for f in self.findings]

    @property
    def primary(self) -> Finding | None:
        """The finding we drive recovery from: highest severity wins."""
        return max(self.findings, key=lambda f: f.severity, default=None)


class ActionKind(StrEnum):
    """Recovery steps, roughly ordered from cheapest to most invasive."""

    RESUME = "resume"
    LIFT_SHARE_LIMITS = "lift_share_limits"
    REANNOUNCE = "reannounce"
    REFRESH_ANNOUNCE = "refresh_announce"
    RECHECK = "recheck"
    TOGGLE = "toggle"
    REPLACE_TORRENT = "replace_torrent"
    #: Re-acquire a torrent that is not in the client at all. Deliberately *not*
    #: a ladder step: every other action repairs a torrent the client holds, and
    #: this one has no torrent to act on — it is driven by an unmatched seeding
    #: requirement, gated by `recovery.redownload_missing`, and has no entry in
    #: `HANDLERS` or `AILMENT_ACTIONS`. It exists in this enum so its history
    #: rows read like every other action's.
    REDOWNLOAD = "redownload"
    #: Remove a torrent the tracker has itself deleted, optionally with its
    #: files. Like REDOWNLOAD it is not a ladder step and has no entry in
    #: `HANDLERS` or `AILMENT_ACTIONS` — but for a stronger reason: every step
    #: in the ladder is an attempt to *keep* a torrent, and the engine picking
    #: this one off a list would make destruction just another rung. It runs
    #: only from `recovery/reap.py`, behind its own switch.
    DELETE = "delete"
    #: Add a torrent the ratio builder picked (`discovery/grab.py`). Not a
    #: repair at all, so like REDOWNLOAD and DELETE it is absent from the
    #: ladder, `HANDLERS` and `AILMENT_ACTIONS`; it is here so grabs show up
    #: in history and the history filter like every other action.
    GRAB = "grab"
    ESCALATE = "escalate"


#: Every available step, cheapest first. Config lists these to enable or disable
#: steps globally; it does not decide the order — see AILMENT_ACTIONS for that.
#: REDOWNLOAD is absent on purpose: it is not selectable by the ladder, and
#: listing it here would offer a second switch for something that already has
#: one, on top of a step the engine can never run.
DEFAULT_LADDER: list[ActionKind] = [
    ActionKind.RESUME,
    ActionKind.LIFT_SHARE_LIMITS,
    ActionKind.REANNOUNCE,
    ActionKind.REFRESH_ANNOUNCE,
    ActionKind.RECHECK,
    ActionKind.TOGGLE,
    ActionKind.REPLACE_TORRENT,
    ActionKind.ESCALATE,
]

#: The escalation order *per ailment*, first step first.
#:
#: Order is per-ailment on purpose. A single global cheapest-first order would
#: fire a reannounce at a torrent whose passkey is wrong — that fails in exactly
#: the same way and burns an attempt for nothing. An auth failure has to have
#: its announce URL rewritten first. Read each entry as "what a human would try,
#: in the order they would try it".
AILMENT_ACTIONS: dict[Ailment, tuple[ActionKind, ...]] = {
    # Nothing to retry: the site no longer has this torrent.
    Ailment.UNREGISTERED: (ActionKind.ESCALATE,),
    # Wrong credentials: fix the URL first, then announce with it.
    Ailment.TRACKER_AUTH: (
        ActionKind.REFRESH_ANNOUNCE,
        ActionKind.REANNOUNCE,
        ActionKind.ESCALATE,
    ),
    # The site is having a bad day; retrying is genuinely all we can do.
    Ailment.TRACKER_DOWN: (ActionKind.REANNOUNCE, ActionKind.ESCALATE),
    Ailment.NOT_ANNOUNCING: (
        ActionKind.RESUME,
        ActionKind.REANNOUNCE,
        ActionKind.REFRESH_ANNOUNCE,
        ActionKind.TOGGLE,
        ActionKind.ESCALATE,
    ),
    Ailment.STALLED_UPLOAD: (
        ActionKind.REANNOUNCE,
        ActionKind.REFRESH_ANNOUNCE,
        ActionKind.TOGGLE,
        ActionKind.ESCALATE,
    ),
    Ailment.STALLED_DOWNLOAD: (
        ActionKind.REANNOUNCE,
        ActionKind.REFRESH_ANNOUNCE,
        ActionKind.RECHECK,
        ActionKind.TOGGLE,
        ActionKind.REPLACE_TORRENT,
        ActionKind.ESCALATE,
    ),
    # A data problem, not a network one — verify the files before anything else.
    Ailment.MISSING_FILES: (ActionKind.RECHECK, ActionKind.ESCALATE),
    Ailment.CLIENT_ERROR: (
        ActionKind.RESUME,
        ActionKind.RECHECK,
        ActionKind.TOGGLE,
        ActionKind.ESCALATE,
    ),
    Ailment.UNEXPECTEDLY_PAUSED: (ActionKind.RESUME, ActionKind.ESCALATE),
    # Seed time is running out: get it connected again, fastest first.
    # LIFT_SHARE_LIMITS comes before RESUME because the reverse does not work —
    # a torrent stopped by its own share limit is re-stopped by the client
    # within seconds of being resumed, so the resume reports success and
    # changes nothing.
    Ailment.HIT_AND_RUN_RISK: (
        ActionKind.LIFT_SHARE_LIMITS,
        ActionKind.RESUME,
        ActionKind.REANNOUNCE,
        ActionKind.REFRESH_ANNOUNCE,
        ActionKind.ESCALATE,
    ),
}


@dataclass(slots=True)
class ActionResult:
    kind: ActionKind
    ok: bool
    message: str
    dry_run: bool = False


@dataclass(slots=True)
class UserStats:
    """Account-level numbers a tracker can report, for the dashboard."""

    uploaded: int | None = None
    downloaded: int | None = None
    ratio: float | None = None
    bonus: float | None = None
    hit_and_runs: int | None = None
    seeding: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class SeedingRequirement:
    """One row from a tracker's "you still owe seeding on this" list.

    This is the authoritative source for hit-and-run exposure: the site is
    naming the torrents it will penalise you for, rather than us inferring risk
    from seed time. It is also proof the torrent still exists there, which is
    why a requirement overrides an `unregistered` finding.

    Sizes are bytes and `None` when the site did not say. `gap` is what is still
    owed — upload that much, or seed past the site's time requirement, and the
    row disappears.
    """

    torrent_id: str
    name: str
    uploaded: int | None = None
    downloaded: int | None = None
    gap: int | None = None
    seeded_hours: float | None = None

    def summary(self) -> str:
        bits = []
        if self.gap:
            bits.append(f"{human_bytes(self.gap)} still owed")
        if self.seeded_hours is not None:
            bits.append(f"seeded {self.seeded_hours:g}h")
        return ", ".join(bits) or "seeding required"


@dataclass(slots=True)
class TrackerTorrentInfo:
    """What a tracker knows about one of our torrents."""

    infohash: str
    torrent_id: str | None = None
    name: str | None = None
    seeders: int | None = None
    leechers: int | None = None
    exists: bool = True
    seedtime_required: int | None = None
    seedtime_current: int | None = None
    hit_and_run: bool = False
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class TrackerFreeleechEntry:
    """One row from a tracker's bulk "my torrents" listing, for freeleech status.

    Best-effort: sites vary in what they expose here, so only `freeleech` is
    load-bearing. `name` is matched against the client by
    `utils.normalise_release`, same as `SeedingRequirement`.
    """

    name: str
    torrent_id: str | None = None
    freeleech: bool | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class TrackerListing:
    """One row from a tracker's browse/search listing, for the ratio builder.

    Unlike `TrackerTorrentInfo` this describes a torrent we probably do *not*
    hold, so the tracker's own id is the identity and the infohash is a bonus.
    Every swarm field is optional: a site that omits one must read as
    "unknown", never as zero, or a missing `leechers` would score as no demand
    and a missing `free` as "costs full ratio" on a freeleech torrent.
    """

    tracker: str
    torrent_id: str
    name: str
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    times_completed: int | None = None
    #: Unix timestamp the torrent was uploaded.
    created_at: int | None = None
    #: Freeleech discount, 0-100. 100 = the download does not count at all.
    free_pct: int | None = None
    double_upload: bool | None = None
    featured: bool | None = None
    #: Seeded from a registered seedbox; those take most of the upload.
    highspeed: bool | None = None
    infohash: str | None = None
    category: str | None = None
    #: The site's own download link, when it hands one out. Contains the
    #: rsskey, so it is never shown in the UI or written to a log.
    download_url: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class TrackerHealth:
    ok: bool
    detail: str = ""
    latency_ms: int | None = None
