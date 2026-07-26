# Adding a tracker

One new file in `src/stallarr/trackers/`. Nothing else — no registry edits, no
frontend changes. The package imports every module inside it at startup, and
`@register` puts your class in the registry; the settings UI builds its form
from that registry automatically.

## First: do you need a file at all?

If the site runs stock UNIT3D, you don't. A config entry naming the generic
driver is a complete, working tracker:

```json
"mysite": {
  "enabled": true,
  "plugin": "unit3d",
  "name": "My Site",
  "url": "https://mysite.org",
  "hosts": ["mysite.org"],
  "api_key": "..."
}
```

The Prowlarr import writes exactly this for sites it confirms speak UNIT3D, so
you may find the tracker already configured. Write a file when the site needs
real behaviour — HTML scraping, an unusual auth scheme, capabilities that depend
on which credentials are present. A plugin file always wins over a config entry
of the same id, so adding one later is a silent upgrade: no config migration.

## The 30-second version (UNIT3D sites)

Most private trackers run UNIT3D. If yours does, the whole plugin is:

```python
"""MySite — UNIT3D."""
from . import register
from .unit3d import Unit3dPlugin


@register
class MySite(Unit3dPlugin):
    id = "mysite"                      # config key; lowercase, stable
    name = "My Site"                   # shown in the UI
    default_url = "https://mysite.org"
    hosts = ("mysite.org",)            # announce hostnames, matched as suffixes
```

That inherits announce-URL repair, torrent lookups, `.torrent` downloads and
health checks. Add a test to `tests/test_trackers.py` asserting it registers and
matches its hosts, and you're done.

**How to tell if a site is UNIT3D:** its torrent pages live at
`/torrents/<id>`, it has an API token under Settings → Security, and
`/api/torrents/filter` returns JSON rather than HTML.

## When the site drifts from stock UNIT3D

Don't fork the class — everything is overridable from config `options`, so a
mismatch is a settings change rather than a code change:

```json
"mysite": {
  "options": {
    "announce_template": "https://tracker.mysite.org/announce/{passkey}",
    "filter_path": "/api/torrents/filter",
    "info_hash_param": "info_hash",
    "download_template": "{base}/torrent/download/{id}.{passkey}",
    "user_stats_path": "/api/user"
  }
}
```

Only if the site's *shape* differs (different auth scheme, different JSON
envelope) should you override a method.

## Non-UNIT3D sites

Subclass `TrackerPlugin` directly and implement what the site actually supports.
`iptorrents.py` is the worked example of a site with no JSON API at all.

```python
from ..models import TrackerTorrentInfo
from . import register
from .base import Capability, TrackerPlugin


@register
class MySite(TrackerPlugin):
    id = "mysite"
    name = "My Site"
    default_url = "https://mysite.org"
    hosts = ("mysite.org",)
    capabilities = frozenset({Capability.ANNOUNCE_URL, Capability.TORRENT_INFO})
    rate_limit_seconds = 3.0

    async def announce_url(self, infohash, current=None):
        ...

    async def torrent_info(self, infohash):
        ...
```

### The rules that matter

**Declare capabilities honestly.** The recovery engine gates steps on them: no
`ANNOUNCE_URL` means it never attempts `refresh_announce`, no `TORRENT_FILE`
means it never attempts `replace_torrent`. Over-declaring turns a skipped step
into a failed one that burns a recovery attempt. If capabilities depend on which
credentials are configured, make `capabilities` a property — see `IPTorrents`.

**Always use `self.get()` / `self.request()`.** They apply `rate_limit_seconds`.
Private trackers ban accounts for hammering, and this tool polls on a timer.

**`announce_url` returns None when nothing needs changing.** Returning a URL
means "this is a repair"; returning the URL the torrent already has makes
stallarr report a fix it didn't make. Prefer rewriting `current` over building
from scratch — sites rotate announce domains, and the domain already on the
torrent is the known-good one.

**`torrent_info(...).exists = False` is a terminal verdict.** It marks the
torrent unregistered and stops all retries. Only return it when the site
genuinely said "no such torrent" — never on a timeout, a 500, or a logged-out
session. Raise `TrackerError` for those instead; the scan handles it and moves on.

**Override `classify_message` for site-specific error wording.** The shared
marker lists in `base.py` cover common phrasing; add yours if it says something
unusual for an unregistered torrent or a bad passkey.

**Never put a passkey in a log, an exception message or a webhook.** Announce
URLs are redacted by `recovery.actions._redact` on the way out; don't route
around it.

## Then

1. Add tests to `tests/test_trackers.py` (registration, host matching, and any
   parsing you wrote). `.venv/bin/python -m pytest -q`
2. Restart the container. The tracker appears on the Trackers tab with fields
   for URL, API token, passkey and cookie.
3. Fill in credentials, save, and check the status badge before trusting it.

## Seeding requirements

If the site publishes a page listing torrents you still owe seeding on, that is
the single most valuable thing a plugin can expose — it replaces inference with
the site's own verdict. Declare `Capability.SEEDING_REQUIREMENTS` and implement:

```python
async def seeding_requirements(self) -> list[SeedingRequirement]:
    resp = await self.get("/seeding_required.php")
    return _parse(resp.text)
```

Rules:

- **One call per scan, never per torrent.** The service calls this once per
  tracker before it walks the library. A per-torrent lookup here would be a ban
  risk, not a slowdown.
- **Return `[]` for "nothing owed".** Raise only when you genuinely could not
  tell — an exception is logged and the scan continues without it, which
  silently disables the feature if you raise on the empty case.
- **`name` must be the release name**, matched against the client by
  `utils.normalise_release` (alphanumerics only). Do not pre-normalise it.
- **Set `gap` and `seeded_hours` when the page shows them.** They go straight
  into the alert, and "536 MiB still owed" is far more actionable than
  "seeding required".

- **`torrent_id` must be the site's own id**, because it is the only handle on a
  torrent the client does not have. See below.

Scraped pages lie in specific ways — check for a hover tooltip or a header row
that a naive cell-index parse will read as data. `tests/test_seeding_requirements.py`
has a real cut-down page with both traps in it; copy that shape for a new site.

## Re-downloading a torrent that is missing locally

A requirement matching nothing in the client cannot be repaired by any recovery
step — there is no torrent to act on. If your site can serve a `.torrent` from
the id you put in `SeedingRequirement.torrent_id`, declare
`Capability.TORRENT_FILE_BY_ID` and implement:

```python
async def fetch_torrent_file_by_id(self, torrent_id: str) -> bytes | None:
    resp = await self.get(f"/download.php/{torrent_id}/x.torrent")
    if not looks_like_torrent(resp.content):
        raise TrackerError(f"{self.id}: not a .torrent (session expired?)")
    return resp.content
```

This is separate from `fetch_torrent_file` on purpose: that one takes an
infohash and searches the site for it, and an unmatched requirement has no
infohash — that absence is what makes it unmatched. Where both exist, implement
this one first and have `fetch_torrent_file` call it after the lookup; it is the
same request minus the search.

Raise `TrackerError` rather than returning junk. The caller reports the failure
and burns an attempt, which is what stops a torrent the site lists but will not
serve from being fetched on every scan forever.
