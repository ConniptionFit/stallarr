# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## stallarr

Torrent health monitor. Reads qBittorrent, decides what's wrong with each
torrent, then runs the cheapest repair that could plausibly fix it. FastAPI +
vanilla-JS dashboard, one container.

**Adding a tracker is the most common task here — see `ADDING_A_TRACKER.md`.**
It's one file in `src/stallarr/trackers/`; do not edit the registry.

## Layout

```
src/stallarr/
  main.py             FastAPI app factory + lifespan; __main__.py runs uvicorn
  service.py          supervisor: owns client + plugins + scan loop, holds UI state
  config.py           JSON config in DATA_DIR, STALLARR_* env overrides
  models.py           domain types; AILMENT_ACTIONS is the recovery policy
  db.py               sqlite: attempt counts, history, notify dedupe, ignores
  clients/            qbittorrent.py implements clients/base.py's protocol
  trackers/           plugin registry; base.py -> unit3d.py -> per-site files
  diagnosis/rules.py  client state -> list of ailments
  recovery/           actions.py (the steps), engine.py (which step, when),
                      reap.py (guarded deletion), redownload.py (unmatched H&R debts)
  notify/__init__.py  Notifier: discord + telegram + ntfy, severity filter, dedupe
  integrations/       prowlarr.py: import tracker credentials
  discovery/          ratio builder: score.py (pure ranking), grab.py (guarded add)
  stats.py            dashboard library totals, pure functions of the scan's data
  utils.py            bencode/infohash, normalise_release, redact_secrets, formatting
  logging_setup.py    stdout + in-memory ring buffer the UI's log view reads
  web/routes.py       API; static/ (app.js, index.html, style.css, icon.svg) is the dashboard
  web/oidc.py         OIDC code flow;  web/metrics.py  hand-rolled Prometheus gauges
assets/               icon sources (stallarr.svg, stallarr-mark.svg), avatar and
                      social-preview PNGs, LICENSE-lucide
```

**Two remotes, one source of truth.** GitHub (`ConniptionFit/stallarr`) is where
PRs land. Forgejo (`git.pwrs.dev/powers/stallarr`, public) is a copy pushed from
GitHub `main` after merges; never commit there directly, it gets overwritten.
README, CLAUDE.md and ADDING_A_TRACKER.md are the only documentation; the
wikis on both hosts are left empty on purpose, so a docs change is a normal PR.

**The icon is derived from Lucide** (`shield` + `activity`, ISC License). Keep
`assets/LICENSE-lucide` and the attribution comments in the SVGs and in
`index.html` when editing it. `web/static/icon.svg` is a copy of
`assets/stallarr.svg` (the favicon); the header logo is the same mark inlined so
it can take the theme's `--accent` and `--ok` colours.

## Design decisions worth knowing before you change things

**Diagnosis is two-phase.** `diagnose_local` uses only client data and runs on
every torrent every scan. `enrich` calls the tracker API and runs *only* for
torrents that already look sick. Private trackers ban you for hammering, and
most torrents are fine. Don't move tracker calls into the hot path.

**Seeding requirements are a per-tracker call, not a per-torrent one.** A site
that publishes "you still owe seeding on these" (`Capability.SEEDING_REQUIREMENTS`)
is asked once per scan, and the rows are matched to local torrents by
`utils.normalise_release`. That is what makes it affordable where enrichment is
not — do not turn it into a per-torrent lookup.

A requirement is the strongest signal available and cuts both ways: it raises
`hit_and_run_risk`, and it *clears* an `unregistered` finding, because a site
cannot simultaneously demand seeding and have deleted the torrent. Since
`unregistered` escalates terminally, leaving it in place is how a live
obligation ends up stopped and abandoned.

**A requirement matching nothing locally is the one case no repair step fits.**
There is no torrent to resume, reannounce or recheck, so `recovery/redownload.py`
fetches the .torrent back by the *tracker's* id (`Capability.TORRENT_FILE_BY_ID`
— `fetch_torrent_file` cannot help, there being no infohash to search for) and
re-adds it. Three things keep that safe and belong together:

  * `skip_checking=False` always. The step's whole value is that surviving files
    on disk turn a re-download into an instant seed; skipping the hash check
    would download data the client already has.
  * A per-scan cap (`redownload_max_per_scan`). Name matching is fuzzy, so a
    tracker changing how it renders release names would make *every*
    requirement look unmatched at once. The cap is what stops that being a bulk
    download.
  * Attempts are capped and cooled down like a ladder step, keyed by
    `redownload:<tracker>:<id>` since there is no infohash. That column is full
    of real infohashes; the prefix is what keeps the two from colliding.

`ActionKind.REDOWNLOAD` is deliberately absent from `DEFAULT_LADDER` and
`HANDLERS`: it is not selectable by the engine and has its own switch, so
listing it would offer a second control for one behaviour. It *is* in
`/api/meta`'s `action_kinds` (all of the enum) rather than `actions` (the
ladder) — a history filter built from the ladder could never find one. What
remains unmatched after the attempt is reported in `seeding_unmatched`, never
dropped.

**The seeding digest is one alert per scan, deduped on identity.** Requirements
arrive in batches — a site publishes its whole hit-and-run list at once — so
`Notifier.seeding_digest` sends one grouped message rather than N alerts.
The dedupe key hashes *which* torrents are owed, not their state: a torrent
going from stopped to seeding while still owing data is progress, not news, and
re-alerting on it is how a channel gets muted. `Notifier.seeding_cleared`
ignores `min_severity` on purpose — it resolves an alert already delivered, and
suppressing that leaves someone believing a hit-and-run is still running.
`service._seeding_report` produces the flat data; grouping and sorting live in
the notifier, because that is a rendering decision.

**"Hit-and-runs averted" is an inference, and the ledger exists to keep it an
honest one.** No tracker publishes "you avoided a hit-and-run", so the only
observable fact is that a debt stopped being listed after stallarr acted on it.
`db.seeding_obligations` records every obligation (keyed by tracker + normalised
name, because there may be no local torrent to hold an infohash) with a count of
successful repairs landed while it was open; `averted` is the settled ones with
a non-zero count, and debts that cleared with no intervention are reported in
their own column rather than folded in. Three rules hold that number up:

  * `_seeding_requirements` returns *which trackers answered* alongside the
    rows, and only those settle anything. A tracker that times out returns no
    rows, and reading that as "nothing is owed" would settle every live debt in
    one go — and credit stallarr with averting each one.
  * `_note_intervention` ignores dry runs and failures. Both changed nothing.
  * An obligation that settles and is later listed again resets its counters
    (`_record_obligations_sync`), so last month's repair cannot be credited with
    this month's debt.

**Dashboard statistics keep four distinctions apart, and the labels promise them.**
`stats.py` computes library totals from the torrent list the scan already
fetched, so no stat costs a tracker call. Keep these separate when adding one:
*seeding* (complete, not stopped) vs *uploading* (sending data now); *client
ratio* (torrents in qBittorrent today) vs *account ratio* (the site's figure,
Trackers tab); a repair step *accepted* (`db.repair_stats`, dry runs excluded)
vs a torrent *recovered* (`ScanSummary.recovered`, summed from snapshots, since
no history row records a recovery); and *problems by type* counts each torrent
once under its primary finding so it adds up to the Problems tile. Every tile
states its time frame and carries a hover hint saying exactly what it counts.

**A client share limit stops a torrent and looks exactly like a pause.**
qBittorrent enforces per-torrent ratio and seeding-time caps by stopping the
torrent, with no distinct state: a capped torrent and a hand-paused one are both
`stoppedUP`. Resuming one is futile — it is re-stopped within seconds, so the
history row records a success that changed nothing. `recovery.actions.blocking_limits`
detects it; `lift_share_limits` is the only step that clears it, and it fires
only from the hit-and-run ladder because it overwrites a setting the user chose.

`/torrents/setShareLimits` on qBittorrent 5 rejects the whole call unless
*every* field is present, `shareLimitAction` included — so
`set_share_limits` reads the torrent back and resends the fields it is not
changing. Echo that action rather than defaulting it: it decides whether hitting
a limit stops the torrent or **deletes** it, and normalising it to `Default`
would be a silent data-loss policy change. qBittorrent 4 has no such field and
rejects the parameter, hence the empty-string check.

**Deletion is the one irreversible action, and `unregistered` is an inference.**
It comes from an empty search result or an announce message, and a tracker whose
lookup is having a bad minute produces exactly the same reading for every
torrent it holds. `recovery/reap.py` is therefore built around that failure, not
around the delete call. Four independent things must agree, and none of them is
redundant:

  * **A soak period** (`db.condemned`). A torrent must have been *continuously*
    unregistered for `delete_min_age_hours`; the row is dropped the instant the
    verdict lifts, so a flicker resets the clock. `condemn()` seeds `first_at`
    from the earliest matching history row, so a restart does not hand every
    known-dead torrent a fresh day of grace.
  * **A circuit breaker** on the share of one tracker's torrents that are
    unregistered — calibrated loose (0.5) on purpose. A broken lookup lands near
    1.0; a site that prunes hard lands at 0.05–0.3 and is telling the truth. A
    threshold inside that second range fires constantly on healthy libraries and
    never on the fault, which is how a rail gets switched off for good. It fails
    *closed* when a tracker has no torrent count to judge against.
  * **A per-scan cap**, so anything that defeats both costs a handful.
  * **`dry_run`**, which reports and touches nothing.

Only a *primary* `unregistered` finding qualifies: one dead tracker on a
multi-tracker torrent is not grounds to erase it. `ActionKind.DELETE` is absent
from `DEFAULT_LADDER`, `HANDLERS` and `AILMENT_ACTIONS` — every ladder step
tries to keep a torrent, and letting the engine select this one off a list would
make destruction just another rung.

**Adding a recovery step needs a ladder migration.** `recovery.ladder` is a
saved permission list, so a new step is absent from every existing config and
would be silently disabled forever. Add it to `DEFAULT_LADDER` *and* to
`config._LADDER_ADDITIONS` under a new version number. The version is what lets
the migration tell "did not exist yet" from "the user removed it on purpose".

**Enrichment has a budget, and it is small.** Each call is rate-limited (2s, 4s
for IPT), so they serialise: a library with a few hundred sick torrents spends
most of the scan interval inside `enrich`. Anything that increases how many
torrents get enriched per scan is a ban risk, not just a slowdown. Torrents
already escalated are skipped (`service._already_escalated`) — they will never
be repaired again, so the call buys nothing.

**Dashboard views that call a tracker are cached** (`service._display_call`,
`DISPLAY_CACHE_SECONDS`). The UI refreshes every ten seconds, so account stats
and freeleech lists read live would put a call on every site per refresh while
a tab sits open. Failures are cached too, for less time. Never route anything
the scan decides on through that cache.

**Except one question, which is still worth asking: does it still exist?**
Sites delete superseded torrents, and one deleted *after* stallarr gave up on it
keeps whatever stale diagnosis it had — permanently, since nothing re-examines
it. `service._recheck_candidates` re-enriches a few escalated torrents per scan
(`rules.recheck_escalated_days`, capped by `recheck_escalated_max_per_scan`),
least-recently-checked first, which reclassifies those as `unregistered` — the
only verdict that means "safe to delete". This is the *sole* reason to spend a
call on an escalated torrent; do not widen it into general re-diagnosis.

The gate is time-since-last-*answer*, held in `db.tracker_checks`, which is why
`enrich` returns a bool. A timeout or a plugin with no `TORRENT_INFO` must not
stamp the record: doing so would push the torrent back to the end of the queue
on a failure, and a long enough tracker outage would mean it is never really
checked again.

**Recovery order is per-ailment, not global** (`models.AILMENT_ACTIONS`). The
config `ladder` only says which steps are *permitted*. This matters: a global
cheapest-first order sends a reannounce at a torrent with a bad passkey, which
fails identically and burns an attempt. Auth failures rewrite the announce URL
first. If you add an ailment, add its ordered tuple there too.

**One repair step per torrent per scan.** Most fixes need an announce cycle to
take effect; firing the whole ladder at once would escalate a torrent that a
single reannounce would have fixed.

**A cooling-down step returns None, it does not fall through.** Skipping to the
next rung during a cooldown would defeat the point of the cooldown.

**Unregistered is terminal.** It escalates immediately and never retries. The
only authoritative sources are the tracker's own announce message or
`torrent_info(...).exists == False` — never a timeout.

**Idle seeds are healthy.** Seeding something nobody wants is normal. Upload is
only flagged when the tracker reports leechers *and* we have no peers, or when
no tracker is working. This distinction is the difference between a useful tool
and one that reannounces your whole library forever.

**Torrents the user paused are left alone** unless `rules.flag_user_paused` is
on. stallarr acts on pauses it caused itself, tracked via `stallarr:` tags.

**`replace_torrent` never deletes files** and refuses to run unless the fresh
`.torrent` has a matching infohash.

**Credentials are redacted at choke points, not at call sites.**
`utils.redact_secrets` is enforced in three places: `engine._log` (the single
path to both the history table and the log), `TrackerPlugin.request` (errors
quote the URL they failed on, and UNIT3D download links embed the rsskey), and
the notifier's error paths (Telegram and Discord put their token *in* the URL).
Redacting where data is written rather than where it is composed means a new
repair step cannot leak by forgetting.

It must cover every URL shape, because the ones it misses are invisible until
someone reads a log. IPTorrents puts the passkey before the verb
(`/<key>/announce`), UNIT3D after it (`/announce/<key>`), UNIT3D download links
use `<id>.<rsskey>`, and the notification services use token formats containing
`:` and `-`. It over-redacts on purpose. `tests/test_redaction.py` holds real
examples of each — add to it when you add a tracker with a new URL layout.

**Secrets are matched by substring in `_SECRET_FIELDS`.** A new secret config
field whose name matches none of those markers is served to the browser in the
clear. `ntfy_token` needed "token" adding. Check this whenever you add a field.
The list holds bare words (`secret`, not `session_secret`) on purpose — the
specific-name version is what let `ntfy_token` through once already.

**Auth is either/or/both: password, OIDC, or nothing.** `routes.auth_required`
is the single answer to "is this instance guarded", and every check goes through
it — an `if not password: return` anywhere else reopens the whole API the moment
someone uses SSO alone. The session cookie signs the subject *inside* the
digest, so it cannot be re-pointed at another identity, and `_session_key` mixes
in both the password and the OIDC client secret so rotating either revokes every
outstanding session.

stallarr runs the OIDC code flow itself rather than trusting forward-auth
headers, because a container is normally reachable on its published port and
over the docker network as well as through the proxy — headers protect only the
last of the three. Identity comes from the userinfo endpoint, not the ID token,
which is why there is no JWT library here; see the module docstring in
`web/oidc.py` for why that is sound. `oidc.configured()` requires issuer, client
id *and* secret: "enabled but half-filled" has to read as off, or a bad save
locks you out of the settings page that would fix it.

**The ratio builder browses on its own clock, never in the scan.**
`discovery/` calls `Capability.BROWSE` once per opted-in tracker per run
(`discovery.interval_minutes`, floored at `MIN_BROWSE_GAP` per tracker however
it is triggered). Browsing a catalogue is a different kind of traffic from
looking up torrents you hold, and some sites forbid it, which is why it is
opt-in per tracker id and not "every enabled tracker".

A grab puts data on disk and a seeding obligation on the account, so
`discovery/grab.py::preflight` runs every check *before* the download and each
fails closed: a disk budget of 0 (the default) refuses everything, unknown free
space refuses, the daily cap is per tracker. `db.discovered` is written only for
a real add, never a dry run, because it backs both "already grabbed" and the cap.
The grab endpoint takes ids only and looks the listing up in the server's own
candidate cache; nothing else the browser sends is trusted.

Protection from purging is three layers because there are three kinds of
purger: per-torrent unlimited share limits (qBittorrent's share-limit action
ignores tags and categories), a dedicated category (*arr apps only remove from
their own), and tags for qbit_manage-style tools. The tags are `stallarr-keep`,
*not* `stallarr:keep`: a `stallarr:` prefix makes `rules._check_paused` treat a
user's pause as stallarr's own and resume it. `TrackerListing.download_url`
embeds the rsskey, so `Scored.as_dict` leaves it out and grab history goes
through `redact_secrets`.

Unknown is never zero in `TrackerListing`: a missing `free` reads as "not
freeleech" (excluded under `freeleech_only`), a missing `leechers` as no demand
credit, a missing `size` as ineligible (the disk budget cannot be checked).

**Env vars beat the config file, so the UI must say so.** `config.load()`
reapplies `STALLARR_*` over anything saved, which means a pinned field accepts
an edit in the browser and then silently reverts. `config.env_pinned_paths()`
reports what is pinned; `/api/meta` ships it and the UI renders those fields
disabled. Both that function and `_apply_env` are generated from
`_env_assignments()` — keep it that way or the UI starts locking the wrong
fields.

**A tracker does not need a plugin file.** `TrackerConfig.plugin` names a base
from `trackers.base_plugins()` (`unit3d` or `generic`), and `build_plugin`
gives it identity from config. A plugin file always wins over the config base,
so writing `seedpool.py` later silently upgrades an imported entry. Most private
sites run stock UNIT3D, which is why the Prowlarr import can bring in trackers
nobody has written code for.

**Prowlarr supplies API keys and nothing else.** Cookies come back masked
(`********`) and announce passkeys are not stored there at all — Prowlarr
searches, it does not seed. `integrations/prowlarr.py::_usable` drops masked
values; never let one reach config, where it would look configured and fail
every call. An unrecognised site is never guessed at: it is only proposed as
UNIT3D after `_speaks_unit3d` confirms it, and otherwise comes in on the
`generic` base, which makes no site calls but routes its torrents by announce
host. A `generic` verdict never demotes a tracker already on `unit3d` (one failed
probe is not evidence), and `rsskey` is not mapped to `passkey` because they are
different secrets and the auto-sync overwrites.

With `prowlarr.auto_sync` on, `service._prowlarr_loop` re-syncs every
`sync_interval_minutes` (floored at `MIN_PROWLARR_SYNC_MINUTES`) on its own task,
not in the scan. A sync that changed config saves and calls
`reload(sync_prowlarr=False)`; reload otherwise syncs too, so passing True after
a sync repeats every probe.

## Working on it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio ruff
.venv/bin/python -m pytest -q                          # whole suite, no network, a few seconds
.venv/bin/python -m pytest -q tests/test_rules.py      # one file
.venv/bin/python -m pytest -q tests/test_reap.py -k breaker   # by name
.venv/bin/ruff check src tests                         # lint (config in pyproject.toml)
DATA_DIR=./data PYTHONPATH=src .venv/bin/python -m stallarr   # localhost:8383
docker compose up -d --build
```

Python 3.12+. pytest runs in `asyncio_mode = "auto"`, so async tests need no
marker, and `pythonpath = ["src"]` means no install step for tests. The tree is
not `ruff format`-clean, so don't run the formatter repo-wide: it produces a
diff across dozens of unrelated files. Format only what you touch.

Tests use a real sqlite DB in a temp `DATA_DIR` (set in `tests/conftest.py`
before any stallarr import — keep it that way, `config.DATA_DIR` binds at import
time). Shared fixtures such as `make_torrent` live in `conftest.py` and are
imported directly (`from conftest import make_torrent`). `FakeClient` in
`test_engine.py` records calls; use it rather than mocking
httpx.

The config file is the source of truth and the UI writes it. Secrets come back
from the UI as the sentinel `"__SET__"`, which `_merge` treats as "keep the
existing value" — a new secret field needs a matching entry in `_SECRET_FIELDS`
in `web/routes.py` or it will leak to the browser.
