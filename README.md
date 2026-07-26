# stallarr

🛟 **stallarr** watches your torrents for stalls, tracker errors and dead
passkeys, then repairs them — so your ratio keeps moving instead of quietly
dying at 3am.

It reads qBittorrent, works out what is actually wrong with each torrent, and
runs the cheapest repair that could plausibly fix it, escalating only when that
doesn't work. Tracker-specific knowledge lives in plugins, so teaching it a new
site is one small file.

Ships with **IPTorrents**, **OldToons World** and **DarkPeers**.

---

## What it detects

| Ailment | Meaning |
|---|---|
| `unregistered` | The site no longer has this torrent. Terminal — tagged for you, never retried. |
| `tracker_auth` | Bad or rotated passkey. |
| `tracker_down` | Every tracker on the torrent is failing. |
| `not_announcing` | Active for ages but no tracker was ever contacted. |
| `stalled_download` | No data for N minutes at partial progress. |
| `stalled_upload` | The tracker reports leechers, but we have no peers. |
| `missing_files` | Data has gone missing on disk. |
| `client_error` | qBittorrent has thrown the torrent into an error state. |
| `unexpectedly_paused` | Paused by a repair that then failed. |
| `hit_and_run_risk` | The tracker says you still owe seeding on this. |

**What it deliberately does not flag:** a seed with no leechers is healthy, not
stalled — that's just seeding. And a torrent you paused on purpose stays paused
unless you opt in with `flag_user_paused`.

## How it repairs

Each fault has its own escalation order, because a global "cheapest first" order
gets this wrong: reannouncing a torrent whose passkey is broken fails in exactly
the same way. So an auth failure rewrites the announce URL *first*.

```
hit_and_run_risk   lift share limits → resume → reannounce → refresh announce → escalate
tracker_auth       refresh announce → reannounce → escalate
stalled_download   reannounce → refresh announce → recheck → toggle → re-add → escalate
missing_files      recheck → escalate
unregistered       escalate immediately
```

One step per torrent per scan, each with its own cooldown and attempt limit —
most fixes need an announce cycle before you can tell whether they worked. When
a step runs out of attempts the next one takes over; when everything is
exhausted the torrent is tagged `stallarr:needs-help` and you get an alert.

Recovered torrents have their tags and attempt history cleared automatically.

### Torrents the site has deleted

Any torrent that looks sick is checked against its tracker, and a site that no
longer lists it is authoritative: that's `unregistered`, which escalates
immediately and is never retried, because no repair can fix a torrent that isn't
there any more.

Once a torrent is escalated stallarr stops spending tracker calls on it — but
sites delete torrents *later*, typically when a single episode is superseded by
a season pack. So a few given-up torrents are re-checked each scan
(least-recently-checked first) purely to ask whether they still exist. One that
has since been deleted is reclassified as `unregistered`, which is your cue that
it's safe to remove. Tune with **Re-check given-up torrents after (days)** —
set it to 0 to switch it off — and **Max re-checks per scan**, which stops a
restart from looking up every escalated torrent at once.

### Deleting them automatically

Off by default, under Settings → *Delete torrents the tracker has deleted*.
**Auto-delete unregistered torrents** removes the torrent; **…and their files**
erases the data too. They're separate switches because they're separate
decisions.

This is the only thing stallarr does that can't be undone, and `unregistered` is
an inference — a tracker whose search is briefly broken reports "not found" for
everything it has. So four things must agree before anything is deleted:

- **It's been unregistered for a while.** A torrent must hold that verdict
  *continuously* for **Only after unregistered for (hours)**, default 24. The
  clock resets the moment it looks registered again, so a one-off bad answer can
  never delete anything.
- **The tracker isn't obviously broken.** If more than **Abort if this fraction
  of a tracker is unregistered** (default 0.5) of a site's torrents come back
  missing, nothing on that site is touched. A broken lookup shows up near 100%;
  a site that just prunes hard sits around 5–30% and is telling the truth, which
  is why this is set loose rather than tight.
- **A per-scan cap** (default 5), so anything that gets past the first two costs
  a handful of torrents rather than a library.
- **Dry run** reports what it would delete and touches nothing.

Only the torrent's *main* problem counts. One dead tracker on a torrent that
also announces elsewhere isn't grounds to erase it.

None of those help against a tracker that's simply wrong for a day, so before
each deletion the `.torrent` file is written to **Keep them in** (default
`graveyard/` inside the data directory) with a small `.json` beside it naming
what it was. The data is still gone, but re-adding is a file away rather than a
re-download from a site that may not have it any more. If that export fails, the
deletion is **cancelled** rather than done without a record — a permanent
failure here shows up as "nothing is being deleted", which is the safe direction
to fail in. Turn it off with **Keep the .torrent file before deleting**.

### Seeing what's about to happen

The dashboard shows **Queued for deletion**: every torrent holding an
`unregistered` verdict, how long it's held it, and why it hasn't gone yet
(waiting out the soak, held by the circuit breaker, or eligible next scan).
**Keep** spares one permanently — it adds the torrent to the ignore list, so no
later scan can re-condemn it. Reprieving alone would only reset the clock and
delete it a day later, which isn't what anyone means by "keep".

When a circuit breaker trips, **Deletion held back** appears with the evidence:
how many of that tracker's torrents came back unregistered, out of how many, and
against what limit. A guard that only writes to the log looks exactly like having
nothing to delete.

The **Deleted** tab is the permanent record — what went, when, how big it was,
whether the data went with it, the tracker's own reason, and which kept file
corresponds. Unlike History, it is never trimmed by the retention setting.

The steps themselves: **resume**, **reannounce**, **refresh announce** (rewrite
a stale passkey, keeping the announce domain the torrent already uses),
**recheck**, **toggle** (stop/start to clear wedged session state), and
**replace torrent** (re-add from a fresh `.torrent`, keeping the data). That last
one never deletes files and refuses to run unless the downloaded infohash
matches.

## Quick start

```yaml
services:
  stallarr:
    image: stallarr:latest
    build: .
    container_name: stallarr
    restart: unless-stopped
    ports:
      - "8383:8383"
    volumes:
      - ./data:/config
```

```bash
docker compose up -d --build
```

Open <http://localhost:8383>, then:

1. **Settings** → point it at qBittorrent, save.
2. **Trackers** → enable your sites and paste credentials (below).
3. Turn on **dry run** for the first day. Watch the History tab, confirm you
   agree with what it *would* have done, then turn it off.

There's no password by default. Set one under Settings → Web if it's exposed
beyond your LAN.

## Tracker credentials

### Import them from Prowlarr

If you run Prowlarr, it already has the list of trackers you're on and the API
keys for them. **Trackers → Import from Prowlarr**, paste its URL and API key,
and hit *Preview* to see what would happen before committing.

It brings across API tokens, site URLs and announce domains, and it discovers
trackers you have no plugin file for: most private sites run stock UNIT3D, so
those are configured against a generic driver and work immediately. A site that
doesn't answer the UNIT3D API is listed as unsupported rather than enabled and
quietly broken — write a plugin for it.

Two things Prowlarr can't give you, because it searches rather than seeds:

- **Cookies** come back masked (`********`), so cookie-authenticated sites
  (IPTorrents) still need one pasted in by hand.
- **Announce passkeys** aren't stored there at all. Without one, the
  `refresh announce` repair step is unavailable for that tracker; everything
  else works.

Tick *keep credentials in sync* to re-import on every reload, so a key rotated
in Prowlarr lands here without a second visit.

### Or enter them by hand

**OldToons / DarkPeers (UNIT3D)** — API token from Settings → Security on the
site, plus your RSS key / passkey.

**IPTorrents** — no JSON API, so it works two ways:

- *Passkey* (the long string in your announce URL): enables announce repair.
  This is the reliable half and survives forever.
- *Cookie* (`uid=…; pass=…` from your browser's request headers): adds torrent
  lookups, seeder counts and `.torrent` downloads. Cookies expire; when one
  does, the plugin says so on the Trackers tab and falls back to passkey mode
  rather than failing scans.

Set both. Capabilities shown on the Trackers tab update to reflect what each
plugin can actually do with the credentials you gave it.

## Seeding obligations

Trackers that publish a "you still owe seeding on these" page are read once per
scan, and the rows matched against your client by release name. A match raises
`hit_and_run_risk` and starts recovery immediately; it also overrides any
`unregistered` finding, since a site cannot demand seeding for a torrent it has
deleted.

Two cases get special handling because the obvious fix does not work:

- **Torrents stopped by a share limit.** qBittorrent enforces per-torrent ratio
  and seeding-time caps by stopping the torrent, and `stoppedUP` looks identical
  to a hand-pause. Resuming one achieves nothing — the client stops it again
  within seconds. Under an active seeding obligation the limit is lifted back to
  *use global* first, then the torrent is resumed. This is the only step that
  changes a setting you chose, and it never runs outside that ladder.
- **Obligations with no matching torrent.** No repair step applies — there is no
  torrent to resume, reannounce or recheck. Turn on **re-download missing
  torrents** and stallarr fetches the .torrent back from the tracker and re-adds
  it; anything still unmatched afterwards stays on the dashboard.

Currently supplied by **IPTorrents** (needs the cookie, not just the passkey).

### What the dashboard tells you about it

Two stat cards and a per-tracker table, covering the last 7 days:

| | |
|---|---|
| **Owed now** | Obligations the tracker is currently listing. |
| **Being repaired** | How many of those stallarr has already acted on. |
| **Seen** | Distinct obligations in the window, settled or not. |
| **H&R averted** | Settled *after* a stallarr repair landed on them. |
| **Cleared on their own** | Settled with no intervention — the torrent was seeding fine. |

The last two columns are deliberately separate. No tracker announces that a
hit-and-run was avoided, so "averted" means exactly what it says: the site
stopped asking after stallarr did something about it. Debts that paid
themselves off are the normal case on a healthy library, and folding them in
would turn the number into a count of how many torrents you have.

Dry runs and failed repairs are never credited, and an obligation that clears
and is later listed again starts over — so a repair from last month can't be
counted against this month's debt.

### Re-downloading missing torrents

Off by default: it is the only thing stallarr does that puts data on your disk
rather than repairing data already there.

The add is always hash-checked, never skipped, because the good case is that the
files survived and only the client entry was lost — then qBittorrent verifies
them and starts seeding immediately, at no cost. Set **save path** to where
those files actually live (or leave it blank and switch on *place by category*)
so it can find them. If the data really is gone, the torrent downloads again,
which counts against your ratio a second time — worth it against a hit-and-run,
but the reason this is a switch and not a default.

Point **category** at something of your own, *not* at a `sonarr`/`radarr`/
`lidarr` category: those would make the *arr treat the re-add as a new grab and
import, rename or move the files out from under the seed.

Re-downloads are capped per scan, and each torrent is retried a limited number
of times with a cooldown, so a tracker listing something it will not serve
cannot be asked forever.

## Signing in

Three options, set under **Settings → Web**:

- **Nothing** (default) — the dashboard and API are open. Fine on a trusted LAN,
  not fine anywhere else.
- **A password** — one shared secret, HMAC-signed session cookie.
- **Single sign-on (OIDC)** — Authentik, Keycloak, Authelia, or any OIDC
  provider. Fill in the issuer URL, client ID and client secret, and register
  `<your base url>/api/auth/sso/callback` as the redirect URI at the provider.
  Restrict access with **allowed groups** if your provider sends a `groups`
  claim.

Password and SSO can both be on; either gets you in. stallarr runs the OIDC flow
itself rather than trusting proxy headers, so the login holds however the
container is reached — published port and docker network included, not just
through your reverse proxy.

> If you enable SSO with no password and the provider then breaks, set
> `STALLARR_WEB_PASSWORD` in your compose file to get back in — an env var beats
> whatever is in the config file.

## Adding another tracker

One file in `src/stallarr/trackers/`. For a UNIT3D site that's four lines:

```python
@register
class MySite(Unit3dPlugin):
    id = "mysite"
    name = "My Site"
    default_url = "https://mysite.org"
    hosts = ("mysite.org",)
```

It appears in the UI on restart, with no frontend or registry changes. See
[ADDING_A_TRACKER.md](ADDING_A_TRACKER.md) for non-UNIT3D sites and the rules
that keep a plugin from misbehaving.

## Configuration

Everything is editable in the UI and stored in `/config/config.json`. Secrets
can be pinned from compose instead, which wins over the file:

```yaml
environment:
  STALLARR_QBIT_URL: http://qbittorrent:8080
  STALLARR_QBIT_USERNAME: admin
  STALLARR_QBIT_PASSWORD: adminadmin
  STALLARR_DRY_RUN: "true"
  STALLARR_WEB_PASSWORD: changeme
  STALLARR_PROWLARR_URL: http://prowlarr:9696
  STALLARR_PROWLARR_API_KEY: ...
  STALLARR_TRACKER_DARKPEERS_API_KEY: ...
  STALLARR_TRACKER_IPTORRENTS_PASSKEY: ...
```

> **Pin only what you actually want fixed.** An env var wins on every load, so a
> pinned setting can't be changed from the WebUI — the save appears to work and
> then reverts. Settings shows those fields greyed out with the variable that
> owns them, but the simplest fix is to not pin anything you'd rather edit in
> the browser.

Thresholds worth tuning: `stalled_download_minutes` (45), `stalled_upload_minutes`
(240), `new_torrent_grace_minutes` (15), and `max_attempts_per_step` (3).

Exclude torrents with `rules.ignore_categories` / `only_categories`, the
`stallarr:ignore` tag, or the Ignore button in the UI.

## Notifications

Discord webhook, Telegram bot and/or **ntfy** — any combination, all enabled
independently under Settings. `min_severity` defaults to 3 (critical only); drop
it to 2 for warnings. Repeats for the same torrent and fault are suppressed for
`dedupe_hours`. Credentials are redacted before anything is sent.

### The seeding digest

On by default, and the one alert that maps directly onto losing an account: a
single message listing **every torrent every tracker still wants seeded**,
grouped by tracker and sorted by tracker then torrent name. Each line carries
what is still owed and what the torrent is doing about it:

```
🔴 Seeding required — 6 torrent(s) on 2 tracker(s)

IPTorrents (5)
• Godsmack-Awake-24BIT-96KHZ-WEB-FLAC-2000-OBZEN — 57.5 MiB still owed,
  seeded 0.2h · stopped at its seeding time limit
• Linkin Park-Heavy-REPACK-CDS-FLAC-2017-CHS — 36.4 MiB still owed · not in
  the client
…

Seedpool (1)
• Some.Other.Release — 1.2 GiB still owed · uploading
```

One digest, not one alert per torrent — these arrive in batches, and eight
notifications for one situation is how a channel gets muted. It re-alerts when
the *list* changes, not when a torrent's state does, so progress on a debt you
already know about stays quiet. You also get an all-clear when the last
obligation goes away, which ignores `min_severity` on purpose: an alert already
delivered has to be told when it resolves.

**ntfy** needs only a topic; the server defaults to `https://ntfy.sh`, so point
it at your own instance if you self-host. Protected topics take either an access
token or a username and password — the token wins if you set both. Severity maps
to ntfy priority (critical → high, warning → default, info → low); it never uses
priority 5, which would bypass Do Not Disturb. Tapping a notification opens the
dashboard, if you've set an external base URL.

Pick a hard-to-guess topic name on public ntfy.sh: anyone who knows the topic
can read it, and these messages name your torrents.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest -q
DATA_DIR=./data PYTHONPATH=src .venv/bin/python -m stallarr
```

The test suite is offline — no tracker or client needed.

## API

`/api/docs` for the full schema. Useful ones:

```
GET  /api/status                       dashboard state
GET  /api/torrents?only_issues=true    every torrent with its diagnosis
POST /api/scan                         scan now
POST /api/torrents/{hash}/repair       force one repair step
POST /api/torrents/{hash}/reset        clear the recovery ladder
GET  /api/condemned                    what is queued for deletion, and why
POST /api/condemned/{hash}/keep        spare one permanently
GET  /api/deletions                    the permanent record of what went
GET  /api/snapshots?days=7             per-scan history behind the trend charts
GET  /health                           container healthcheck
GET  /metrics                          Prometheus, when web.metrics_enabled
```

### Metrics

Off by default. `web.metrics_enabled` serves Prometheus gauges at `/metrics`
with **no authentication** — Prometheus has no session to present. It exports
counts only: no torrent names, no infohashes, nothing from config. It is opt-in
because the labels name the private trackers this host is on, which is more than
an open endpoint should volunteer.

```
stallarr_torrents_total / _healthy / _sick / _skipped
stallarr_actions_total, stallarr_scan_duration_seconds
stallarr_client_up, stallarr_tracker_up{tracker=…}
stallarr_seeding_outstanding, stallarr_seeding_averted
stallarr_condemned_total, stallarr_deletions_total, stallarr_deleted_bytes_total
stallarr_deletion_blocked{tracker=…}     present only while a breaker is tripped
stallarr_disk_free_bytes
```
