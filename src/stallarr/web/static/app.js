'use strict';

const SEV = ['ok', 'info', 'warn', 'crit'];
const SENTINEL = '__SET__';
const state = {
  config: null,
  plugins: [],
  meta: { env_pinned: {} },
  activeTab: 'dashboard',
  timer: null,
  // Unsaved tracker edits, keyed by tracker id. Switching trackers in the
  // dropdown must not throw away what you typed into the previous one.
  trackerDraft: {},
  selectedTracker: null,
  // Torrents are cached so filtering and paging are local: the ten-second
  // refresh replaces the data, not the view you are working in.
  torrents: [],
  issues: [],
  selected: new Set(),
  page: 0,
  // Same caching rationale as torrents: filtering/paging the Seeding tab
  // should not cost a round trip.
  seeding: [],
  seedingPage: 0,
  trendDays: 7,
};

// --- helpers ----------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 401) { showLogin(); throw new Error('authentication required'); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), 4200);
}

const bytes = (n) => {
  if (!n) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
};

const ago = (ts) => {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

const duration = (s) => {
  if (!s) return '—';
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  if (days) return `${days}d ${hours}h`;
  const minutes = Math.floor((s % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
};

/** Plain-language names for ailments and repair steps. The enum values are
 *  stable identifiers, not prose: "Stalled Upload" says less than "Seeding but
 *  not uploading", and "Toggle" says nothing at all. Anything not listed falls
 *  back to title-casing the identifier. */
const FRIENDLY = {
  unregistered: 'Deleted on tracker',
  tracker_down: 'Tracker unreachable',
  tracker_auth: 'Tracker rejected passkey',
  not_announcing: 'Not announcing',
  stalled_download: 'Download stalled',
  stalled_upload: 'Seeding, not uploading',
  missing_files: 'Files missing',
  client_error: 'Client error',
  unexpectedly_paused: 'Stopped unexpectedly',
  hit_and_run_risk: 'Hit-and-run risk',
  resume: 'Resume',
  lift_share_limits: 'Lift share limits',
  reannounce: 'Reannounce',
  refresh_announce: 'Refresh announce URL',
  recheck: 'Recheck files',
  toggle: 'Stop and restart',
  replace_torrent: 'Replace .torrent',
  redownload: 'Re-download',
  delete: 'Delete',
};

const label = (text) => FRIENDLY[text]
  || String(text).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

const pct = (fraction) => (fraction == null ? '—' : `${Math.round(fraction * 100)}%`);
const rate = (n) => `${bytes(n)}/s`;
const ratioText = (r) => (r == null ? '—' : r.toFixed(2));

// --- auth -------------------------------------------------------------------

// `status` is omitted when a mid-session 401 sends us here, so fall back to
// what boot() already learned rather than rendering a login page with no way in.
function showLogin(status = state.auth || {}) {
  const sso = status.sso || {};
  const failed = new URLSearchParams(location.search).get('sso_error');

  // Never auto-redirect after a failure: the IdP would send the browser
  // straight back into the same error, in a loop, with the reason flashing past
  // too fast to read.
  if (sso.auto_redirect && !failed) { location.href = '/api/auth/sso/start'; return; }

  $('#login-error').textContent = failed || '';
  $('#sso-button').classList.toggle('hidden', !sso.enabled);
  $('#sso-label').textContent = sso.name || 'Single sign-on';
  $('#login-form').classList.toggle('hidden', !status.password_login);
  $('#login-or').classList.toggle('hidden', !(sso.enabled && status.password_login));

  $('#login').classList.remove('hidden');
  $('#app').classList.add('hidden');
  clearInterval(state.timer);
}

async function boot() {
  const status = await fetch('/api/auth/status').then((r) => r.json());
  state.auth = status;
  if (status.auth_required && !status.authenticated) { showLogin(status); return; }
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  $('#logout').hidden = !status.auth_required;
  $('#whoami').textContent = status.user && status.user !== 'local' ? status.user : '';
  $('#whoami').hidden = !$('#whoami').textContent;
  // meta first: renderSettings needs env_pinned to know what to lock.
  state.meta = await api('/meta');
  renderHistoryFilter();
  await Promise.all([loadConfig(), loadPlugins()]);
  await refresh();
  state.timer = setInterval(refresh, 10000);
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await api('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password: $('#login-password').value }),
    });
    $('#login-error').textContent = '';
    location.reload();
  } catch (err) {
    $('#login-error').textContent = err.message;
  }
});

$('#logout').addEventListener('click', async () => {
  await api('/auth/logout', { method: 'POST' });
  location.reload();
});

// --- tabs -------------------------------------------------------------------

$$('.tab').forEach((tab) => tab.addEventListener('click', () => {
  $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
  state.activeTab = tab.dataset.tab;
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === `tab-${state.activeTab}`));
  refresh();
}));

// --- dashboard --------------------------------------------------------------

async function refresh() {
  try {
    const status = await api('/status');
    state.issues = status.issues || [];
    renderStats(status);
    renderProblems(status);
    renderRepairs(status.repairs || {});
    renderLibrary(status.library || {});
    renderIssues(state.issues);
    $('#dry-run-pill').hidden = !status.recovery.dry_run;
    renderSeedingMetrics(status.seeding || {});
    renderSeedingUnmatched(status.seeding_unmatched || []);
    renderBreakers(status.deletion_blocks || []);
    renderCondemned(status.condemned || []);

    if (state.activeTab === 'dashboard') await loadTrend();
    if (state.activeTab === 'torrents') await loadTorrents();
    if (state.activeTab === 'history') await loadHistory();
    if (state.activeTab === 'deletions') await loadDeletions();
    if (state.activeTab === 'logs') await loadLogs();
    if (state.activeTab === 'trackers') { renderPlugins(status); await loadTrackerStats(); }
    if (state.activeTab === 'seeding') await loadSeeding();
  } catch (err) {
    if (err.message !== 'authentication required') toast(err.message, 'bad');
  }
}

/** What each tracker is owed, and what stallarr did about it.
 *
 *  "Averted" is an inference and the wording says so: the tracker never
 *  announces that a hit-and-run was avoided, so the honest claim is that it
 *  stopped asking after a repair landed. Debts that cleared with no
 *  intervention get their own column rather than being folded in.
 */
function renderSeedingMetrics(metrics) {
  const rows = metrics.by_tracker || [];
  $('#seeding-metrics-card').hidden = !rows.length;
  if (!rows.length) return;

  $('#seeding-window').textContent = `counts cover the last ${metrics.window_days} days`;
  $('#seeding-metrics-body').innerHTML = rows.map((r) => `
    <tr>
      <td><span class="pill">${esc(r.tracker_name || r.tracker)}</span></td>
      <td>${r.outstanding ? `<strong>${r.outstanding}</strong>` : '0'}</td>
      <td class="muted">${r.working}</td>
      <td class="muted">${r.seen}</td>
      <td>${r.averted ? `<span class="pill ok">${r.averted}</span>` : '<span class="muted">0</span>'}</td>
      <td class="muted">${r.self_cleared}</td>
    </tr>`).join('');

  const recent = metrics.recent || [];
  $('#seeding-recent').innerHTML = recent.length
    ? 'Averted = the tracker stopped asking after stallarr repaired it. Most recent: '
      + recent.slice(0, 4).map((r) =>
        `<strong>${esc(r.name)}</strong> (${esc(label(r.last_action))}, ${ago(r.cleared_at)})`).join(' · ')
    : 'Averted = the tracker stopped asking after stallarr repaired it. Nothing has qualified yet.';
}

/** Trackers whose deletions are currently refused, and the evidence.
 *
 *  A circuit breaker that only writes to the log is indistinguishable from
 *  having nothing to delete, so it gets a card of its own rather than a line in
 *  a status pill.
 */
function renderBreakers(blocks) {
  $('#breaker-card').hidden = !blocks.length;
  if (!blocks.length) return;

  $('#breaker-body').innerHTML = blocks.map((b) => `
    <tr>
      <td><span class="pill">${esc(b.tracker)}</span></td>
      <td>${b.condemned}${b.total ? ` of ${b.total}` : ''}</td>
      <td><strong>${b.fraction === null ? '—' : `${(b.fraction * 100).toFixed(0)}%`}</strong></td>
      <td class="muted">${(b.limit * 100).toFixed(0)}%</td>
      <td class="muted">${esc(b.reason)}</td>
    </tr>`).join('');
}

/** What is queued for deletion, and why it has not happened yet. */
function renderCondemned(rows) {
  $('#condemned-card').hidden = !rows.length;
  if (!rows.length) return;

  const bytesAtRisk = rows.reduce((sum, r) => sum + (r.size || 0), 0);
  $('#condemned-meta').textContent =
    `${rows.length} torrent(s) · ${bytes(bytesAtRisk)}`;

  $('#condemned-body').innerHTML = rows.map((r) => `
    <tr>
      <td class="name">${esc(r.name)}</td>
      <td><span class="pill">${esc(r.tracker_name || r.tracker)}</span></td>
      <td>${bytes(r.size)}</td>
      <td class="muted">${ago(r.first_at)}</td>
      <td class="muted">${esc(r.status)}</td>
      <td class="actions">
        <button class="tiny" data-keep="${esc(r.infohash)}" data-name="${esc(r.name)}">Keep</button>
      </td>
    </tr>`).join('');

  $$('[data-keep]', $('#condemned-body')).forEach((btn) => btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const res = await api(`/condemned/${btn.dataset.keep}/keep`, {
        method: 'POST',
        body: JSON.stringify({ name: btn.dataset.name }),
      });
      toast(res.detail, 'good');
    } catch (err) { toast(err.message, 'bad'); }
    refresh();
  }));
}

/** Small inline sparklines. No chart library: these are polylines over a fixed
 *  viewBox, and pulling in a dependency for that would be the tail wagging.
 *
 *  Each series says what its headline number means. Counts show the latest
 *  value and the change across the window; "Uploaded" and "Recovered" are
 *  totals *earned within* the window, which is the figure someone watching
 *  their ratio actually wants. */
async function loadTrend() {
  const days = state.trendDays;
  let snapshots = [];
  try { ({ snapshots } = await api(`/snapshots?days=${days}`)); } catch { return; }

  $('#trend-card').hidden = snapshots.length < 2;
  if (snapshots.length < 2) return;

  const window_ = days === 1 ? '24h' : `${days}d`;
  $('#trend-meta').textContent = `${snapshots.length} scans · ${ago(snapshots[0].ts)} to now`;

  // Upload earned per scan interval. Library totals fall when a torrent is
  // removed, so only increases count; a row from before these columns existed
  // reads 0 and must not turn the whole library into "uploaded this week".
  const gains = snapshots.map((row, i) => {
    if (!i) return 0;
    const prev = snapshots[i - 1].uploaded || 0;
    return prev > 0 ? Math.max((row.uploaded || 0) - prev, 0) : 0;
  });
  let running = 0;
  const cumulative = gains.map((g) => (running += g));

  const series = [
    {
      label: 'Uploaded', cls: 'ok', values: cumulative,
      value: bytes(running), sub: `earned in the last ${window_}`,
    },
    { key: 'seeding', label: 'Seeding', cls: 'ok', sub: 'complete and not stopped' },
    { key: 'uploading', label: 'Uploading', cls: '', sub: 'sending data at scan time' },
    { key: 'sick', label: 'Problems', cls: 'warn', sub: 'torrents with a finding' },
    {
      label: 'Recovered', cls: 'ok',
      values: (() => { let n = 0; return snapshots.map((r) => (n += r.recovered || 0)); })(),
      sub: `came back healthy in the last ${window_}`,
    },
    { key: 'owed', label: 'Seeding owed', cls: '', sub: 'listed by a tracker as owed' },
    { key: 'condemned', label: 'Queued for deletion', cls: 'crit', sub: 'deleted on tracker, soaking' },
    { key: 'total', label: 'Library size', cls: '', sub: 'torrents in the client' },
  ];

  $('#trend-charts').innerHTML = series.map((s) => {
    const values = s.values || snapshots.map((row) => row[s.key] || 0);
    const last = values[values.length - 1];
    let headline = s.value ?? last;
    let sub = s.sub;
    if (s.key) {
      const delta = last - values[0];
      sub += ` · ${delta === 0 ? 'no change' : (delta > 0 ? `+${delta}` : `${delta}`)} over ${window_}`;
    }
    return `
      <div class="trend">
        <div class="trend-head">
          <span class="label">${esc(s.label)}</span>
          <span class="value ${s.cls}">${esc(headline)}</span>
          <span class="muted sub">${esc(sub)}</span>
        </div>
        ${sparkline(values)}
      </div>`;
  }).join('');
}

$$('#trend-window button').forEach((btn) => btn.addEventListener('click', () => {
  state.trendDays = Number(btn.dataset.days);
  $$('#trend-window button').forEach((b) => b.classList.toggle('active', b === btn));
  loadTrend();
}));

function sparkline(values) {
  const width = 240;
  const height = 40;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / span) * height).toFixed(1)}`)
    .join(' ');
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"
    role="img" aria-hidden="true"><polyline points="${points}"/></svg>`;
}

/** Seeding the tracker wants that no local torrent satisfies. */
function renderSeedingUnmatched(rows) {
  $('#seeding-unmatched-card').hidden = !rows.length;
  if (!rows.length) return;
  $('#seeding-unmatched-body').innerHTML = rows.map((r) => `
    <tr>
      <td><span class="pill">${esc(r.tracker)}</span></td>
      <td>${esc(r.name)}</td>
      <td><span class="muted">${esc(r.detail)}</span></td>
    </tr>`).join('');
}

/** The headline tiles, grouped by the question each group answers.
 *
 *  Every tile carries a `hint` (shown on hover) saying exactly what is
 *  counted. The old single grid mixed "right now", "last scan", "last 7 days"
 *  and "all time" figures side by side with nothing to tell them apart; each
 *  group and sub-line now states its time frame. */
function renderStats(status) {
  const scan = status.last_scan || {};
  const critical = (status.issues || []).filter((i) => i.severity >= 3).length;
  const trackers = Object.values(status.trackers || {});
  const trackersUp = trackers.filter((t) => t.ok).length;
  const seeding = status.seeding || {};
  const lib = status.library || {};
  const repairs = status.repairs || {};
  const days = seeding.window_days ?? repairs.window_days ?? 7;
  const condemned = (status.condemned || []).length;
  const blocks = (status.deletion_blocks || []).length;
  const deletions = status.deletions || {};
  const watched = (scan.total ?? 0) - (scan.skipped ?? 0);

  const groups = [
    {
      title: 'Upload & ratio', note: 'torrents in your client right now',
      cards: [
        {
          label: 'Uploaded', value: bytes(lib.uploaded || 0), cls: 'ok',
          sub: `↓ ${bytes(lib.downloaded || 0)} downloaded`,
          hint: 'Total uploaded by the torrents currently in qBittorrent. Removed torrents no longer count, so this is lower than your tracker account totals.',
        },
        {
          label: 'Client ratio', value: ratioText(lib.ratio),
          cls: lib.ratio == null ? '' : lib.ratio >= 1 ? 'ok' : 'warn',
          sub: 'uploaded ÷ downloaded',
          hint: 'Uploaded divided by downloaded across the torrents in your client. Not your account ratio on any site; see the Trackers tab for those.',
        },
        {
          label: 'Upload speed', value: rate(lib.upspeed || 0),
          sub: `↓ ${rate(lib.dlspeed || 0)} at last scan`,
          hint: 'Combined upload speed of every torrent, measured when the last scan ran.',
        },
        {
          label: 'Uploading now', value: `${lib.uploading ?? 0}`,
          cls: lib.uploading ? 'ok' : '',
          sub: `of ${lib.seeding ?? 0} seeding`,
          hint: 'Seeding torrents that were sending data at the last scan. Idle seeds are normal: nobody is downloading them right now.',
        },
      ],
    },
    {
      title: 'Health', note: 'last scan',
      cards: [
        {
          label: 'Checked', value: watched,
          sub: scan.skipped ? `${scan.skipped} skipped by your filters` : `of ${scan.total ?? 0} in client`,
          hint: 'Torrents stallarr diagnosed on the last scan. Skipped ones are ignored, in an excluded category or tag, queued, or busy checking/moving.',
        },
        {
          label: 'Healthy', value: scan.healthy ?? 0, cls: 'ok',
          sub: watched ? `${pct((scan.healthy ?? 0) / watched)} of checked` : 'passing every check',
          hint: 'Torrents with no findings at all.',
        },
        {
          label: 'Problems', value: scan.sick ?? 0, cls: scan.sick ? 'warn' : 'ok',
          sub: `${critical} critical`,
          hint: 'Torrents with at least one finding. Critical means deleted on tracker, files missing, passkey rejected or hit-and-run risk.',
        },
        {
          label: 'Recovered', value: repairs.recovered ?? 0, cls: repairs.recovered ? 'ok' : '',
          sub: `came back healthy · ${days}d`,
          hint: `Torrents that had a problem and then passed every check, over the last ${days} days. This is the outcome; repair counts below are only the attempts.`,
        },
      ],
    },
    {
      title: 'Repairs', note: `last ${days} days`,
      cards: [
        {
          label: 'Repairs tried', value: repairs.attempts ?? 0,
          sub: `${scan.actions ?? 0} on the last scan · ${repairs.torrents ?? 0} torrents`,
          hint: 'Recovery steps stallarr ran (resume, reannounce, recheck and so on). Dry runs are not counted.',
        },
        {
          label: 'Accepted', value: pct(repairs.success_rate),
          cls: repairs.success_rate == null ? '' : repairs.success_rate >= 0.8 ? 'ok' : 'warn',
          sub: `${repairs.succeeded ?? 0} of ${repairs.attempts ?? 0} carried out`,
          hint: 'Share of repair steps the client carried out without an error. A step can be accepted and still not fix the torrent.',
        },
        {
          label: 'Failed', value: repairs.failed ?? 0, cls: repairs.failed ? 'warn' : 'ok',
          sub: repairs.dry_run ? `${repairs.dry_run} dry-run step(s) not counted` : 'errored when run',
          hint: 'Repair steps that returned an error. The History tab has the messages.',
        },
      ],
    },
    {
      title: 'Hit-and-run protection', note: `last ${days} days`,
      cards: [
        {
          label: 'Seeding owed', value: seeding.outstanding ?? 0,
          cls: seeding.outstanding ? 'warn' : 'ok',
          sub: seeding.outstanding
            ? `${seeding.working ?? 0} being repaired`
            : `${seeding.seen ?? 0} seen in ${days}d`,
          hint: 'Torrents a tracker currently lists as still owing seed time or ratio. Only sites that publish this list are counted.',
        },
        {
          label: 'H&R averted', value: seeding.averted ?? 0, cls: seeding.averted ? 'ok' : '',
          sub: `${seeding.self_cleared ?? 0} cleared on their own`,
          hint: 'Obligations the tracker stopped listing after stallarr repaired the torrent. Ones that cleared with no repair are shown separately and not claimed.',
        },
      ],
    },
    {
      title: 'Housekeeping', note: 'deletion and disk',
      cards: [
        {
          label: 'Queued for deletion', value: condemned, cls: condemned ? 'warn' : 'ok',
          sub: blocks ? `${blocks} tracker(s) held back` : 'awaiting the soak period',
          hint: 'Torrents the tracker has deleted, waiting out the soak period before removal.',
        },
        {
          label: 'Reclaimed', value: bytes(deletions.reclaimed || 0),
          sub: `${deletions.count || 0} deleted · all time`,
          hint: 'Disk space freed by deleting torrents together with their files, since stallarr was installed.',
        },
        {
          label: 'Free space',
          value: status.disk && status.disk.free != null ? bytes(status.disk.free) : '—',
          sub: 'where the client writes',
          hint: 'Free space on the client\'s default save path, as qBittorrent reports it.',
        },
      ],
    },
    {
      title: 'System', note: '',
      cards: [
        {
          label: 'Client',
          value: status.client.connected ? 'Up' : 'Down',
          cls: status.client.connected ? 'ok' : 'crit',
          sub: status.client.detail || '',
          hint: 'Whether qBittorrent answered on the last scan.',
        },
        {
          label: 'Trackers',
          value: `${trackersUp}/${trackers.length}`,
          cls: trackers.length && trackersUp === trackers.length ? 'ok' : trackers.length ? 'warn' : '',
          sub: 'site APIs reachable',
          hint: 'Configured trackers whose API answered the last connection check.',
        },
        {
          label: 'Last scan',
          value: status.scanning ? '…' : ago(scan.finished_at),
          sub: `took ${scan.duration ?? 0}s · every ${status.scan_interval_minutes}m`,
          hint: 'When the last full scan finished, how long it took, and how often scans run.',
        },
      ],
    },
  ];

  $('#stat-sections').innerHTML = groups.map((g) => `
    <section class="stat-section">
      <h3>${esc(g.title)}${g.note ? `<span class="muted">${esc(g.note)}</span>` : ''}</h3>
      <div class="stat-grid">${g.cards.map((c) => `
        <div class="stat ${c.cls || ''}"${c.hint ? ` title="${esc(c.hint)}"` : ''}>
          <div class="value">${esc(c.value)}</div>
          <div class="label">${esc(c.label)}</div>
          <div class="sub">${esc(c.sub)}</div>
        </div>`).join('')}
      </div>
    </section>`).join('');
}

/** One bar per problem type, worst-severity colour, primary finding only. */
function renderProblems(status) {
  const rows = status.problems_by_ailment || [];
  const total = rows.reduce((n, r) => n + r.count, 0);
  $('#problems-meta').textContent = total ? `${total} torrent(s)` : '';
  if (!rows.length) {
    $('#problems-bars').innerHTML = '<p class="muted">Nothing wrong on the last scan.</p>';
    return;
  }
  const max = Math.max(...rows.map((r) => r.count));
  $('#problems-bars').innerHTML = rows.map((r) => `
    <div class="bar-row" title="${esc(r.ailment)}">
      <span>${esc(label(r.ailment))}</span>
      <span class="track"><span class="fill ${SEV[r.severity] || ''}" style="width:${(r.count / max) * 100}%"></span></span>
      <span class="count">${r.count}</span>
    </div>`).join('');
}

/** Which repair steps ran, and how often the client carried them out. */
function renderRepairs(repairs) {
  const rows = repairs.by_action || [];
  $('#repairs-meta').textContent = `last ${repairs.window_days ?? 7} days`;
  $('#repairs-body').innerHTML = rows.length
    ? rows.map((r) => `
      <tr>
        <td><span class="pill">${esc(label(r.action))}</span></td>
        <td>${r.attempts}</td>
        <td>${r.succeeded} <span class="muted">(${pct(r.success_rate)})</span></td>
        <td>${r.failed ? `<strong>${r.failed}</strong>` : '<span class="muted">0</span>'}</td>
        <td class="muted">${r.torrents}</td>
      </tr>`).join('')
    : '<tr><td colspan="5" class="muted">No repairs in this window.</td></tr>';
}

/** Where upload is coming from, per tracker, from the client's own numbers. */
function renderLibrary(lib) {
  const rows = lib.by_tracker || [];
  $('#library-card').hidden = !rows.length;
  if (!rows.length) return;
  const totalUp = lib.uploaded || 0;
  $('#library-body').innerHTML = rows.map((r) => `
    <tr>
      <td><span class="pill">${esc(r.tracker_name || r.tracker)}</span></td>
      <td>${r.torrents}</td>
      <td>${r.seeding}${r.stopped ? ` <span class="muted">· ${r.stopped} stopped</span>` : ''}</td>
      <td>${r.uploading ? `<strong>${r.uploading}</strong>` : '<span class="muted">0</span>'}</td>
      <td>${bytes(r.uploaded)}</td>
      <td class="muted">${bytes(r.downloaded)}</td>
      <td>${ratioText(r.ratio)}</td>
      <td class="muted">${rate(r.upspeed)}</td>
      <td>${totalUp ? pct(r.uploaded / totalUp) : '—'}</td>
    </tr>`).join('');
}

function renderIssues(issues) {
  const body = $('#issues-table tbody');
  $('#issues-meta').textContent = issues.length ? `${issues.length} torrent(s) need attention` : '';

  if (!issues.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">Nothing wrong — everything is seeding happily.</td></tr>';
    return;
  }
  body.innerHTML = issues.map(rowForIssue).join('');
  wireRowActions(body);
}

function rowForIssue(item) {
  const findings = item.findings.map((f) =>
    `<div><span class="pill">${esc(label(f.ailment))}</span> <span class="muted">${esc(f.detail)}</span></div>`).join('');
  return `
    <tr>
      <td><span class="dot ${SEV[item.severity] || 'info'}" title="${esc(item.severity_name)}"></span></td>
      <td class="name"><a href="#" data-detail="${esc(item.infohash)}">${esc(item.name)}</a></td>
      <td>${findings}</td>
      <td>${item.tracker ? `<span class="pill">${esc(item.tracker)}</span>` : '<span class="muted">unmatched</span>'}</td>
      <td class="actions">
        <button class="tiny" data-repair="${esc(item.infohash)}">Repair</button>
        <button class="tiny" data-reset="${esc(item.infohash)}">Reset</button>
        <button class="tiny" data-ignore="${esc(item.infohash)}">Ignore</button>
      </td>
    </tr>`;
}

function wireRowActions(root) {
  $$('[data-repair]', root).forEach((btn) => btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const res = await api(`/torrents/${btn.dataset.repair}/repair`, { method: 'POST' });
      toast(`${res.action ? label(res.action) + ': ' : ''}${res.message}`, res.ok ? 'good' : 'bad');
    } catch (err) { toast(err.message, 'bad'); }
    btn.disabled = false;
    refresh();
  }));

  $$('[data-reset]', root).forEach((btn) => btn.addEventListener('click', async () => {
    await api(`/torrents/${btn.dataset.reset}/reset`, { method: 'POST' });
    toast('Recovery ladder reset', 'good');
    refresh();
  }));

  $$('[data-ignore]', root).forEach((btn) => btn.addEventListener('click', async () => {
    await api(`/torrents/${btn.dataset.ignore}/ignore`, { method: 'POST', body: JSON.stringify({}) });
    toast('Torrent ignored', 'good');
    refresh();
  }));
}

$('#scan-now').addEventListener('click', async () => {
  const res = await api('/scan', { method: 'POST' });
  toast(res.detail, 'good');
  setTimeout(refresh, 1500);
});

// --- torrents ---------------------------------------------------------------

$('#only-issues').addEventListener('change', loadTorrents);

const PAGE_SIZE = 100;

const SORTS = {
  severity: (a, b) => b.severity - a.severity || a.name.localeCompare(b.name),
  name: (a, b) => a.name.localeCompare(b.name),
  size: (a, b) => b.size - a.size,
  ratio: (a, b) => a.ratio - b.ratio,
  progress: (a, b) => a.progress - b.progress,
};

/** Fetch, then render from the cache. Split so that the ten-second refresh can
 *  update the data without a filter change costing a round trip. */
async function loadTorrents() {
  const onlyIssues = $('#only-issues').checked;
  const { torrents } = await api(`/torrents?only_issues=${onlyIssues}`);
  state.torrents = torrents;
  refreshTorrentFilterOptions(torrents);
  renderTorrents();
}

/** Populate the tracker and problem dropdowns from what is actually present.
 *  Rebuilt only when the set of values changes, because replacing a <select>'s
 *  options while it is open closes it. */
function refreshTorrentFilterOptions(torrents) {
  const trackers = [...new Set(torrents.map((t) => t.tracker || t.tracker_hosts[0] || ''))]
    .filter(Boolean).sort();
  const ailments = [...new Set(torrents.flatMap((t) => t.findings.map((f) => f.ailment)))].sort();

  syncOptions($('#torrent-tracker'), trackers, 'all trackers');
  syncOptions($('#torrent-ailment'), ailments, 'all problems');
}

function syncOptions(select, values, allLabel) {
  const wanted = JSON.stringify(values);
  if (select.dataset.values === wanted) return;
  const current = select.value;
  select.dataset.values = wanted;
  select.innerHTML = `<option value="">${esc(allLabel)}</option>`
    + values.map((v) => `<option value="${esc(v)}">${esc(label(v))}</option>`).join('');
  // Keep the user's choice if it still exists; otherwise fall back to "all"
  // rather than silently filtering by something no longer on screen.
  select.value = values.includes(current) ? current : '';
}

function visibleTorrents() {
  const needle = $('#torrent-search').value.trim().toLowerCase();
  const tracker = $('#torrent-tracker').value;
  const ailment = $('#torrent-ailment').value;

  return (state.torrents || []).filter((t) => {
    if (needle && !t.name.toLowerCase().includes(needle)) return false;
    if (tracker && (t.tracker || t.tracker_hosts[0] || '') !== tracker) return false;
    if (ailment && !t.findings.some((f) => f.ailment === ailment)) return false;
    return true;
  }).sort(SORTS[$('#torrent-sort').value] || SORTS.severity);
}

function renderTorrents() {
  const rows = visibleTorrents();
  const pages = Math.max(Math.ceil(rows.length / PAGE_SIZE), 1);
  // A filter that shrinks the list can strand you past the end.
  state.page = Math.min(state.page, pages - 1);
  const page = rows.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  const body = $('#torrents-table tbody');

  $('#torrents-count').textContent = rows.length === (state.torrents || []).length
    ? `${rows.length} torrent(s)`
    : `${rows.length} of ${(state.torrents || []).length} torrent(s)`;

  $('#torrents-pager').hidden = pages < 2;
  $('#page-label').textContent = `page ${state.page + 1} of ${pages}`;
  $('#page-prev').disabled = state.page === 0;
  $('#page-next').disabled = state.page >= pages - 1;

  if (!page.length) {
    body.innerHTML = '<tr><td colspan="9" class="empty">No torrents match.</td></tr>';
    renderBulkBar();
    return;
  }

  body.innerHTML = page.map(torrentRow).join('');
  wireRowActions(body);
  wireSelection(body);
  renderBulkBar();
}

function torrentRow(t) {
  const notes = t.skipped
    ? `<span class="muted">skipped: ${esc(t.skipped)}</span>`
    : t.findings.map((f) => `<span class="pill">${esc(label(f.ailment))}</span>`).join(' ')
      || '<span class="pill ok">healthy</span>';
  const checked = state.selected.has(t.infohash) ? ' checked' : '';
  return `
    <tr>
      <td class="tight"><input type="checkbox" data-select="${esc(t.infohash)}"${checked}></td>
      <td><span class="dot ${SEV[t.severity] || 'ok'}"></span></td>
      <td class="name"><a href="#" data-detail="${esc(t.infohash)}">${esc(t.name)}</a>
          <div class="muted">${bytes(t.size)}</div></td>
      <td><span class="pill">${esc(t.raw_state)}</span></td>
      <td><div class="bar"><i style="width:${(t.progress * 100).toFixed(1)}%"></i></div>
          <span class="muted">${(t.progress * 100).toFixed(1)}%</span></td>
      <td>${t.ratio.toFixed(2)}</td>
      <td>${t.tracker ? esc(t.tracker) : `<span class="muted">${esc(t.tracker_hosts[0] || '—')}</span>`}</td>
      <td>${notes}</td>
      <td class="actions"><button class="tiny" data-repair="${esc(t.infohash)}">Repair</button></td>
    </tr>`;
}

// --- selection and bulk actions ---------------------------------------------

function wireSelection(root) {
  $$('[data-select]', root).forEach((box) => box.addEventListener('change', () => {
    if (box.checked) state.selected.add(box.dataset.select);
    else state.selected.delete(box.dataset.select);
    renderBulkBar();
  }));
}

function renderBulkBar() {
  const count = state.selected.size;
  $('#bulk-bar').classList.toggle('hidden', !count);
  $('#bulk-count').textContent = `${count} selected`;
  // Reflects only what is on screen, so it cannot claim "all" while filtered.
  const shown = $$('[data-select]').map((b) => b.dataset.select);
  $('#select-page').checked = shown.length > 0 && shown.every((h) => state.selected.has(h));
}

$('#select-page').addEventListener('change', (event) => {
  $$('[data-select]').forEach((box) => {
    box.checked = event.target.checked;
    if (box.checked) state.selected.add(box.dataset.select);
    else state.selected.delete(box.dataset.select);
  });
  renderBulkBar();
});

$('#bulk-clear').addEventListener('click', () => {
  state.selected.clear();
  renderTorrents();
});

$$('[data-bulk]').forEach((btn) => btn.addEventListener('click', async () => {
  const kind = btn.dataset.bulk;
  const hashes = [...state.selected];
  if (!hashes.length) return;
  if (!confirm(`${label(kind)} ${hashes.length} torrent(s)?`)) return;

  btn.disabled = true;
  let ok = 0;
  let failed = 0;
  // Sequential on purpose: these each hit qBittorrent, and firing a hundred
  // at once is how you get rate-limited by your own client.
  for (const infohash of hashes) {
    try {
      if (kind === 'repair') await api(`/torrents/${infohash}/repair`, { method: 'POST' });
      if (kind === 'reset') await api(`/torrents/${infohash}/reset`, { method: 'POST' });
      if (kind === 'ignore') {
        await api(`/torrents/${infohash}/ignore`, { method: 'POST', body: JSON.stringify({}) });
      }
      ok += 1;
    } catch { failed += 1; }
  }
  btn.disabled = false;
  state.selected.clear();
  toast(`${label(kind)}: ${ok} done${failed ? `, ${failed} failed` : ''}`, failed ? 'bad' : 'good');
  await loadTorrents();
}));

['#torrent-search', '#torrent-tracker', '#torrent-ailment', '#torrent-sort'].forEach((sel) => {
  $(sel).addEventListener('input', () => { state.page = 0; renderTorrents(); });
});
$('#page-prev').addEventListener('click', () => { state.page -= 1; renderTorrents(); });
$('#page-next').addEventListener('click', () => { state.page += 1; renderTorrents(); });

// --- seeding ------------------------------------------------------------------

const SEEDING_SORTS = {
  tracker: (a, b) =>
    (a.tracker_name || a.tracker).localeCompare(b.tracker_name || b.tracker)
      || a.name.localeCompare(b.name),
  name: (a, b) => a.name.localeCompare(b.name),
  ratio: (a, b) => a.ratio - b.ratio,
  uploaded: (a, b) => b.uploaded - a.uploaded,
  seeding_time: (a, b) => b.seeding_time - a.seeding_time,
};

/** Fetch, then render from the cache — same split as loadTorrents, so the
 *  refresh timer doesn't cost a round trip while you're filtering. */
async function loadSeeding() {
  const data = await api('/seeding');
  state.seeding = Object.values(data.trackers || {}).flat();
  refreshSeedingFilterOptions(state.seeding);
  renderSeeding();
}

function refreshSeedingFilterOptions(rows) {
  const trackers = [...new Set(rows.map((r) => r.tracker_name || r.tracker))]
    .filter(Boolean).sort();
  syncOptions($('#seeding-tracker'), trackers, 'all trackers');
}

function visibleSeeding() {
  const needle = $('#seeding-search').value.trim().toLowerCase();
  const tracker = $('#seeding-tracker').value;
  return (state.seeding || []).filter((r) => {
    if (needle && !r.name.toLowerCase().includes(needle)) return false;
    if (tracker && (r.tracker_name || r.tracker) !== tracker) return false;
    return true;
  }).sort(SEEDING_SORTS[$('#seeding-sort').value] || SEEDING_SORTS.tracker);
}

function renderSeeding() {
  const rows = visibleSeeding();
  const pages = Math.max(Math.ceil(rows.length / PAGE_SIZE), 1);
  state.seedingPage = Math.min(state.seedingPage, pages - 1);
  const page = rows.slice(state.seedingPage * PAGE_SIZE, (state.seedingPage + 1) * PAGE_SIZE);
  const body = $('#seeding-table tbody');

  $('#seeding-count').textContent = rows.length === (state.seeding || []).length
    ? `${rows.length} torrent(s)`
    : `${rows.length} of ${(state.seeding || []).length} torrent(s)`;

  $('#seeding-pager').hidden = pages < 2;
  $('#seeding-page-label').textContent = `page ${state.seedingPage + 1} of ${pages}`;
  $('#seeding-page-prev').disabled = state.seedingPage === 0;
  $('#seeding-page-next').disabled = state.seedingPage >= pages - 1;

  if (!page.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">Nothing is actively seeding.</td></tr>';
    return;
  }

  // A sub-header row per tracker, inserted whenever it changes — only
  // meaningful under the default tracker sort, since any other sort
  // interleaves trackers and a group heading would misdescribe its rows.
  const grouped = ($('#seeding-sort').value || 'tracker') === 'tracker';
  const html = [];
  let lastTracker = null;
  for (const r of page) {
    if (grouped && r.tracker !== lastTracker) {
      lastTracker = r.tracker;
      html.push(`<tr class="group-row"><td colspan="7">${esc(r.tracker_name || r.tracker)}</td></tr>`);
    }
    html.push(seedingRow(r));
  }
  body.innerHTML = html.join('');
}

function seedingRow(r) {
  const tagPills = (r.tags || [])
    .filter((t) => t.startsWith('stallarr:'))
    .map((t) => `<span class="pill">${esc(label(t.replace('stallarr:', '')))}</span>`);
  const notes = [
    r.recently_recovered ? '<span class="pill ok">recovered</span>' : '',
    ...tagPills,
  ].filter(Boolean).join(' ') || '<span class="muted">—</span>';

  const freeleech = r.freeleech === true ? '<span class="pill ok">freeleech</span>'
    : r.freeleech === false ? '<span class="muted">no</span>'
    : '<span class="muted">—</span>';

  return `
    <tr>
      <td class="name">${esc(r.name)}</td>
      <td><span class="pill">${esc(r.tracker_name || r.tracker)}</span></td>
      <td>${r.ratio.toFixed(2)}</td>
      <td>${bytes(r.uploaded)}</td>
      <td>${duration(r.seeding_time)}</td>
      <td>${freeleech}</td>
      <td>${notes}</td>
    </tr>`;
}

['#seeding-search', '#seeding-tracker', '#seeding-sort'].forEach((sel) => {
  $(sel).addEventListener('input', () => { state.seedingPage = 0; renderSeeding(); });
});
$('#seeding-page-prev').addEventListener('click', () => { state.seedingPage -= 1; renderSeeding(); });
$('#seeding-page-next').addEventListener('click', () => { state.seedingPage += 1; renderSeeding(); });

// --- detail drawer ------------------------------------------------------------

function openDrawer(infohash) {
  const t = (state.torrents || []).find((x) => x.infohash === infohash)
    || (state.issues || []).find((x) => x.infohash === infohash);
  if (!t) return;

  $('#drawer-title').textContent = t.name;
  const rows = [
    ['Infohash', t.infohash],
    ['State', `${t.raw_state}${t.skipped ? ` (skipped: ${t.skipped})` : ''}`],
    ['Size', bytes(t.size)],
    ['Progress', `${((t.progress || 0) * 100).toFixed(1)}%`],
    ['Ratio', (t.ratio ?? 0).toFixed(2)],
    ['Tracker', t.tracker || (t.tracker_hosts || []).join(', ') || 'unmatched'],
    ['Announce hosts', (t.tracker_hosts || []).join(', ') || '—'],
    ['Category', t.category || '—'],
    ['Tags', (t.tags || []).join(', ') || '—'],
  ];

  const findings = (t.findings || []).map((f) => `
    <div class="finding">
      <span class="pill">${esc(label(f.ailment))}</span>
      <span class="muted">${esc(f.detail)}</span>
    </div>`).join('') || '<p class="muted">Nothing wrong with this one.</p>';

  $('#drawer-body').innerHTML = `
    <dl class="detail">${rows.map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    <h3>Findings</h3>${findings}
    <div class="card-foot">
      <button class="tiny" data-repair="${esc(t.infohash)}">Repair</button>
      <button class="tiny" data-reset="${esc(t.infohash)}">Reset ladder</button>
      <button class="tiny" data-ignore="${esc(t.infohash)}">Ignore</button>
    </div>`;
  wireRowActions($('#drawer-body'));
  $('#drawer').classList.remove('hidden');
}

const closeDrawer = () => $('#drawer').classList.add('hidden');
$('#drawer-close').addEventListener('click', closeDrawer);
$('#drawer').addEventListener('click', (e) => { if (e.target.id === 'drawer') closeDrawer(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-detail]');
  if (!link) return;
  event.preventDefault();
  openDrawer(link.dataset.detail);
});

// --- deletions ----------------------------------------------------------------

async function loadDeletions() {
  const { deletions, totals } = await api('/deletions');
  $('#deletions-meta').textContent = totals.count
    ? `${totals.count} deleted · ${bytes(totals.reclaimed)} reclaimed`
    : '';
  const body = $('#deletions-table tbody');
  if (!deletions.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">Nothing has been deleted.</td></tr>';
    return;
  }
  body.innerHTML = deletions.map((d) => `
    <tr>
      <td class="muted">${ago(d.deleted_at)}</td>
      <td class="name">${esc(d.name)}</td>
      <td><span class="pill">${esc(d.tracker)}</span></td>
      <td>${bytes(d.size)}</td>
      <td>${d.files_deleted
        ? '<span class="pill warn">erased</span>'
        : '<span class="pill">entry only</span>'}</td>
      <td class="muted">${esc(d.reason)}</td>
      <td class="muted">${d.export_path
        ? `<code>${esc(d.export_path.split('/').pop())}</code>`
        : '—'}</td>
    </tr>`).join('');
}

// --- history ----------------------------------------------------------------

/** Options come from the ActionKind enum, so a new action type appears in the
 *  filter with no frontend change — the same rule the tracker form follows. */
function renderHistoryFilter() {
  const select = $('#history-action');
  const kinds = state.meta.action_kinds || [];
  select.innerHTML = '<option value="">all actions</option>'
    + kinds.map((k) => `<option value="${esc(k)}">${esc(label(k))}</option>`).join('');
}

function historyQuery() {
  const params = new URLSearchParams({ limit: '200' });
  const action = $('#history-action').value;
  const outcome = $('#history-outcome').value;
  if (action) params.set('action', action);
  if (outcome) params.set('ok', outcome);
  return params.toString();
}

async function loadHistory() {
  const { history } = await api(`/history?${historyQuery()}`);
  const body = $('#history-table tbody');
  const filtered = $('#history-action').value || $('#history-outcome').value;
  $('#history-count').textContent = history.length
    ? `${history.length}${history.length === 200 ? '+' : ''} shown`
    : '';
  if (!history.length) {
    body.innerHTML = `<tr><td colspan="5" class="empty">${
      filtered ? 'Nothing matches those filters.' : 'No recovery actions recorded yet.'
    }</td></tr>`;
    return;
  }
  body.innerHTML = history.map((h) => `
    <tr>
      <td class="muted">${ago(h.ts)}</td>
      <td class="name">${esc(h.name)}</td>
      <td><span class="pill">${esc(label(h.ailment || '—'))}</span></td>
      <td><span class="pill">${esc(label(h.action || '—'))}</span>${h.dry_run ? ' <span class="pill warn">dry run</span>' : ''}</td>
      <td><span class="pill ${h.ok ? 'ok' : 'bad'}">${h.ok ? 'ok' : 'failed'}</span> <span class="muted">${esc(h.message)}</span></td>
    </tr>`).join('');
}

$('#history-action').addEventListener('change', loadHistory);
$('#history-outcome').addEventListener('change', loadHistory);

// --- trackers ---------------------------------------------------------------

// `always` fields apply to every tracker; the rest are rendered only when the
// plugin lists them in `credential_fields`, so a UNIT3D site shows an API token
// and nothing else.
const TRACKER_FIELDS = [
  { name: 'url', label: 'Site URL', always: true },
  { name: 'api_key', label: 'API token', secret: true,
    hint: 'On UNIT3D sites: Settings → Security → API Tokens.' },
  { name: 'passkey', label: 'Announce passkey', secret: true,
    hint: 'Never supplied by Prowlarr. Copy it out of any existing announce URL in qBittorrent — the segment between the host and /announce.' },
  { name: 'cookie_uid', label: 'Cookie · uid', secret: true,
    hint: 'Firefox: Shift+F9 → Cookies → the site → value of the "uid" row.' },
  { name: 'cookie_pass', label: 'Cookie · pass', secret: true,
    hint: 'Same panel, value of the "pass" row. Not visible via document.cookie — it is HttpOnly, so the storage inspector is the only place to read it.' },
  { name: 'cookie', label: 'Raw cookie header', secret: true, placeholder: 'uid=…; pass=…',
    hint: 'Optional. Paste a whole Cookie header here instead — it overrides the two fields above.' },
  { name: 'extra_hosts', label: 'Extra announce hosts', kind: 'list', always: true },
  { name: 'min_ratio', label: 'Warn below ratio', kind: 'number', always: true,
    hint: 'The site’s own requirement, not a global one. 0 disables the check.' },
  { name: 'max_hit_and_runs', label: 'Warn at hit-and-runs', kind: 'number', always: true,
    hint: 'Most sites tolerate a couple, so the useful alarm is "approaching", not "any". 0 disables.' },
];

/** Fields to render for one plugin, per its declared credential_fields. */
function fieldsFor(plugin) {
  const creds = plugin.credential_fields || ['api_key', 'passkey'];
  return TRACKER_FIELDS.filter((f) => f.always || creds.includes(f.name));
}

async function loadPlugins() {
  const { plugins } = await api('/plugins');
  state.plugins = plugins;
  if (!state.plugins.some((p) => p.id === state.selectedTracker)) {
    state.selectedTracker = state.plugins.length ? state.plugins[0].id : null;
  }
  renderPlugins();
}

/** Config for one tracker, with any unsaved edits layered on top. */
function trackerValues(id) {
  const saved = (state.config && state.config.trackers && state.config.trackers[id]) || {};
  return { ...saved, ...(state.trackerDraft[id] || {}) };
}

function renderPlugins(status) {
  const trackerStatus = (status && status.trackers) || {};
  const picker = $('#tracker-picker');

  picker.innerHTML = state.plugins.map((p) => {
    const cfg = trackerValues(p.id);
    const mark = cfg.enabled ? '●' : '○';
    return `<option value="${esc(p.id)}" ${p.id === state.selectedTracker ? 'selected' : ''}>${mark} ${esc(p.name)}</option>`;
  }).join('') || '<option>No trackers</option>';

  renderTrackerDetail(trackerStatus);
}

function renderTrackerDetail(trackerStatus = {}) {
  const plugin = state.plugins.find((p) => p.id === state.selectedTracker);
  const host = $('#plugin-detail');
  if (!plugin) {
    host.innerHTML = '<p class="muted">No tracker plugins are registered.</p>';
    return;
  }

  const cfg = trackerValues(plugin.id);
  const live = trackerStatus[plugin.id] || plugin.status;
  const badge = live
    ? `<span class="pill ${live.ok ? 'ok' : 'bad'}">${esc(live.detail || (live.ok ? 'ok' : 'error'))}</span>`
    : '<span class="pill">not connected</span>';
  const caps = (live && live.capabilities ? live.capabilities : plugin.capabilities)
    .map((c) => `<span class="pill">${esc(label(c))}</span>`).join(' ')
    || '<span class="muted">no capabilities — add credentials below</span>';
  const origin = plugin.builtin
    ? `<span class="muted">plugin file · ${esc(plugin.hosts.join(', ') || 'no hosts')}</span>`
    : `<span class="pill warn">config-defined</span> <span class="muted">generic ${esc(cfg.plugin || 'unit3d')} driver · ${esc((cfg.hosts || []).join(', '))}</span>`;

  const saved = (state.config.trackers && state.config.trackers[plugin.id]) || {};
  const draft = state.trackerDraft[plugin.id] || {};

  const fields = fieldsFor(plugin).map((field) => {
    const value = cfg[field.name];
    const isSet = saved[field.name] === SENTINEL;
    // A *stored* secret never round-trips through the DOM — the server sends a
    // sentinel and a blank box means "keep what is stored". But a secret the
    // user has typed and not yet saved has to survive a re-render, or it
    // vanishes the moment they click away and the save writes back an empty
    // string. That value is already on screen; echoing it leaks nothing.
    const pending = field.secret && typeof draft[field.name] === 'string'
      && draft[field.name] !== '' && draft[field.name] !== SENTINEL;
    const shown = field.secret
      ? (pending ? draft[field.name] : '')
      : field.kind === 'list' ? (value || []).join(', ') : (value || '');
    const placeholder = field.secret
      ? (isSet ? 'saved — leave blank to keep' : 'not set')
      : (field.placeholder || (field.name === 'url' ? plugin.default_url : '') || '');
    return `
      <div class="field">
        <label title="${esc(field.hint || '')}">${esc(field.label)}</label>
        <input data-field="${field.name}" data-kind="${field.kind || (field.secret ? 'secret' : 'text')}"
               type="${field.secret ? 'password' : 'text'}" title="${esc(field.hint || '')}"
               value="${esc(shown)}" placeholder="${esc(placeholder)}">
        ${field.hint ? `<span class="muted hint">${esc(field.hint)}</span>` : ''}
      </div>`;
  }).join('');

  host.innerHTML = `
    <div class="plugin" data-plugin="${esc(plugin.id)}">
      <div class="plugin-head">
        <div>
          <strong>${esc(plugin.name)}</strong>
          <div>${origin}</div>
        </div>
        <div class="caps">${caps} ${badge}</div>
        <label class="inline">
          <input type="checkbox" data-field="enabled" ${cfg.enabled ? 'checked' : ''}> enabled
        </label>
      </div>
      <div class="plugin-fields">${fields}</div>
    </div>`;

  host.querySelectorAll('[data-field]').forEach((el) => {
    el.addEventListener('input', captureTrackerDraft);
    el.addEventListener('change', () => {
      captureTrackerDraft();
      // Only the enabled toggle changes anything outside this form (the ●/○ in
      // the picker). Re-rendering on every blur rebuilds the inputs under the
      // user's cursor for no reason.
      if (el.dataset.field === 'enabled') renderPlugins();
    });
  });
}

/** Read the visible form back into the draft for the selected tracker. */
function captureTrackerDraft() {
  const node = $('#plugin-detail .plugin');
  if (!node) return;
  const id = node.dataset.plugin;
  const saved = (state.config.trackers && state.config.trackers[id]) || {};
  const draft = { enabled: $('[data-field="enabled"]', node).checked };

  TRACKER_FIELDS.forEach((field) => {
    const el = $(`[data-field="${field.name}"]`, node);
    if (!el) return;
    if (field.kind === 'list') {
      draft[field.name] = el.value.split(',').map((s) => s.trim()).filter(Boolean);
    } else if (field.secret) {
      // Blank means unchanged: send the sentinel back so the server keeps
      // the stored secret rather than clearing it.
      draft[field.name] = el.value === '' ? (saved[field.name] === SENTINEL ? SENTINEL : '') : el.value;
    } else {
      draft[field.name] = el.value.trim();
    }
  });

  state.trackerDraft[id] = draft;
}

$('#tracker-picker').addEventListener('change', (event) => {
  captureTrackerDraft();
  state.selectedTracker = event.target.value;
  renderPlugins();
});

$('#save-trackers').addEventListener('click', async () => {
  captureTrackerDraft();
  const trackers = {};
  Object.entries(state.trackerDraft).forEach(([id, draft]) => {
    const saved = (state.config.trackers && state.config.trackers[id]) || {};
    trackers[id] = { ...draft, options: saved.options || {} };
  });

  if (!Object.keys(trackers).length) {
    $('#trackers-saved').textContent = 'Nothing changed.';
    setTimeout(() => { $('#trackers-saved').textContent = ''; }, 3000);
    return;
  }

  try {
    await saveConfig({ trackers });
    state.trackerDraft = {};
    renderPlugins();
    $('#trackers-saved').textContent = 'Saved.';
    toast('Tracker settings saved', 'good');
  } catch (err) {
    toast(err.message, 'bad');
  }
  setTimeout(() => { $('#trackers-saved').textContent = ''; }, 3000);
});

/** Per-site account numbers, as labelled rows rather than one dotted line.
 *
 *  Two kinds of figure sit on each card and are kept visibly apart: what the
 *  *site* says about your account (above the rule) and what your *client* is
 *  doing with that site's torrents (below it). */
async function loadTrackerStats() {
  const stats = await api('/tracker-stats');
  const entries = Object.entries(stats);
  if (!entries.length) {
    $('#tracker-stats').innerHTML = '<span class="muted">No trackers are enabled yet.</span>';
    return;
  }
  $('#tracker-stats').innerHTML = `<div class="stat-grid">${entries.map(([id, s]) => {
    if (s.error) {
      return `<div class="stat crit"><div class="value">!</div>
        <div class="label">${esc(s.name || id)}</div>
        <div class="sub">${esc(s.error)}</div></div>`;
    }
    // Seeding locally but not counted by the tracker: those announces are not
    // landing, so the torrents earn nothing. Worth surfacing loudly.
    const unseen = s.unseen_seeds;
    const warnings = s.warnings || [];
    // An account heading for trouble outranks announces that are not landing:
    // one is a threshold you set, the other is an inference.
    const cls = warnings.length ? 'crit' : unseen ? 'warn' : s.hit_and_runs ? 'crit' : 'ok';
    const rows = [];
    const row = (k, v, title = '') => rows.push(
      `<dt${title ? ` title="${esc(title)}"` : ''}>${esc(k)}</dt><dd>${v}</dd>`);

    if (s.uploaded != null) row('Uploaded', esc(bytes(s.uploaded)));
    if (s.downloaded != null) row('Downloaded', esc(bytes(s.downloaded)));
    if (s.buffer != null) {
      row('Buffer', `${s.buffer < 0 ? '−' : ''}${esc(bytes(Math.abs(s.buffer)))}`,
        'Uploaded minus downloaded.');
    }
    if (s.headroom != null) {
      const floor = (s.ratio_floor ?? 1).toFixed(2);
      row(`Room to ${floor}`,
        s.headroom < 0
          ? `<strong>${esc(bytes(-s.headroom))} short</strong>`
          : esc(bytes(s.headroom)),
        s.ratio_floor_configured
          ? `How much more you can download before your ratio falls to your ${floor} floor.`
          : 'How much more you can download before your ratio falls to 1.00. Set a minimum ratio on this tracker to measure against your own floor.');
    }
    if (s.bonus != null) row('Bonus points', esc(Number(s.bonus).toLocaleString()));
    if (s.hit_and_runs != null) {
      row('Hit-and-runs', s.hit_and_runs ? `<strong>${s.hit_and_runs}</strong>` : '0',
        'Hit-and-runs the site currently counts against your account.');
    }
    if (s.seeding != null) {
      row('Site counts seeding', `${s.seeding}`,
        'How many torrents the tracker believes you are seeding.');
    }
    rows.push('<span class="rule"></span>');
    row('Seeding in client', `${s.local_seeding}`,
      'Complete, not-stopped torrents from this site in qBittorrent.');
    if (unseen) {
      row('Not announcing', `<strong>${unseen}</strong>`,
        'Seeding in your client but not counted by the site, so they earn nothing. Usually a passkey or announce problem.');
    }
    const local = s.local || null;
    if (local) {
      row('Uploading now', `${local.uploading}`);
      row('Upload speed', esc(rate(local.upspeed)));
    }

    const alarms = warnings.map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join('');
    return `
      <div class="stat ${cls}">
        <div class="value">${s.ratio != null ? s.ratio.toFixed(2) : '—'}</div>
        <div class="label">${esc(s.name || id)} · account ratio</div>
        <dl class="kv">${rows.join('')}</dl>
        ${alarms}
      </div>`;
  }).join('')}</div>`;
}

// --- prowlarr ---------------------------------------------------------------

function prowlarrPayload() {
  return {
    url: $('#prowlarr-url').value.trim(),
    api_key: $('#prowlarr-key').value || SENTINEL,
  };
}

function renderProposals(proposals) {
  const rows = proposals.map((p) => {
    const cls = p.supported ? (p.provides.length ? 'ok' : 'warn') : 'bad';
    const provides = p.provides.length
      ? p.provides.map((f) => `<span class="pill ok">${esc(label(f))}</span>`).join(' ')
      : '<span class="pill warn">no credentials available</span>';
    return `
      <tr>
        <td><span class="pill ${cls}">${esc(p.tracker_id)}</span></td>
        <td class="name">${esc(p.name)}<div class="muted">${esc(p.url)}</div></td>
        <td>${provides}</td>
        <td class="muted">${esc(p.detail)}</td>
      </tr>`;
  }).join('');

  $('#prowlarr-result').innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr><th>Tracker</th><th>Indexer</th><th>Provides</th><th>Notes</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="empty">No torrent indexers found.</td></tr>'}</tbody>
      </table>
    </div>`;
}

async function prowlarrCall(path, label_) {
  $('#prowlarr-status').textContent = `${label_}…`;
  try {
    const res = await api(path, { method: 'POST', body: JSON.stringify(prowlarrPayload()) });
    $('#prowlarr-status').textContent = '';
    return res;
  } catch (err) {
    $('#prowlarr-status').textContent = '';
    toast(err.message, 'bad');
    return null;
  }
}

$('#prowlarr-test').addEventListener('click', async () => {
  const res = await prowlarrCall('/prowlarr/test', 'Testing');
  if (res) toast(res.detail, res.ok ? 'good' : 'bad');
});

$('#prowlarr-preview').addEventListener('click', async () => {
  const res = await prowlarrCall('/prowlarr/preview', 'Checking trackers');
  if (res) renderProposals(res.proposals);
});

$('#prowlarr-import').addEventListener('click', async () => {
  const res = await prowlarrCall('/prowlarr/import', 'Importing');
  if (!res) return;
  renderProposals(res.proposals);
  const changed = res.imported.filter((i) => i.changed.length);
  toast(changed.length ? `Imported ${changed.length} tracker(s)` : 'Everything was already up to date', 'good');
  state.trackerDraft = {};
  await Promise.all([loadConfig(), loadPlugins()]);
  await saveProwlarrSettings();
  refresh();
});

async function saveProwlarrSettings() {
  const payload = prowlarrPayload();
  await saveConfig({
    prowlarr: {
      enabled: true,
      url: payload.url,
      api_key: payload.api_key,
      auto_sync: $('#prowlarr-autosync').checked,
    },
  });
}

$('#prowlarr-autosync').addEventListener('change', async () => {
  try {
    await saveProwlarrSettings();
    toast('Prowlarr settings saved', 'good');
  } catch (err) { toast(err.message, 'bad'); }
});

function renderProwlarrSettings() {
  const cfg = (state.config && state.config.prowlarr) || {};
  $('#prowlarr-url').value = cfg.url || '';
  $('#prowlarr-key').value = '';
  $('#prowlarr-key').placeholder = cfg.api_key === SENTINEL ? 'saved — leave blank to keep' : 'Settings → General';
  $('#prowlarr-autosync').checked = !!cfg.auto_sync;
}

// --- settings ---------------------------------------------------------------

const SETTINGS_SCHEMA = [
  { group: 'Torrent client' },
  { path: 'qbittorrent.url', label: 'qBittorrent URL' },
  { path: 'qbittorrent.username', label: 'Username' },
  { path: 'qbittorrent.password', label: 'Password', type: 'password' },
  { path: 'qbittorrent.verify_ssl', label: 'Verify SSL', type: 'checkbox' },

  { group: 'Scanning' },
  { path: 'scan_interval_minutes', label: 'Scan interval (minutes)', type: 'number' },
  { path: 'log_level', label: 'Log level', type: 'select', options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'] },
  { path: 'history_retention_days', label: 'History retention (days)', type: 'number' },

  { group: 'Detection thresholds' },
  { path: 'rules.stalled_download_minutes', label: 'Stalled download after (min)', type: 'number' },
  { path: 'rules.stalled_upload_minutes', label: 'Stalled upload after (min)', type: 'number' },
  { path: 'rules.new_torrent_grace_minutes', label: 'New torrent grace (min)', type: 'number' },
  { path: 'rules.announce_timeout_minutes', label: 'Announce timeout (min)', type: 'number' },
  { path: 'rules.flag_user_paused', label: 'Treat user-paused as a fault', type: 'checkbox' },
  { path: 'rules.recheck_escalated_days', label: 'Re-check given-up torrents after (days, 0 = never)', type: 'number' },
  { path: 'rules.recheck_escalated_max_per_scan', label: 'Max re-checks per scan', type: 'number' },
  { path: 'rules.ignore_categories', label: 'Ignore categories', type: 'list' },
  { path: 'rules.only_categories', label: 'Only these categories', type: 'list' },
  { path: 'rules.ignore_tags', label: 'Ignore tags', type: 'list' },

  { group: 'Recovery' },
  { path: 'recovery.enabled', label: 'Auto-recovery enabled', type: 'checkbox' },
  { path: 'recovery.dry_run', label: 'Dry run (log only)', type: 'checkbox' },
  { path: 'recovery.max_attempts_per_step', label: 'Attempts per step', type: 'number' },
  { path: 'recovery.toggle_pause_seconds', label: 'Pause/resume gap (s)', type: 'number' },
  { path: 'recovery.tagging_enabled', label: 'Tag torrents in client', type: 'checkbox' },

  { group: 'Re-download missing torrents' },
  { path: 'recovery.redownload_missing', label: 'Re-add torrents the tracker wants seeded', type: 'checkbox' },
  { path: 'recovery.redownload_max_per_scan', label: 'Max re-downloads per scan', type: 'number' },
  { path: 'recovery.redownload_category', label: 'Category for re-added torrents' },
  { path: 'recovery.redownload_use_auto_tmm', label: 'Place by category (finds existing files)', type: 'checkbox' },
  { path: 'recovery.redownload_save_path', label: 'Save path override (blank = by category)' },
  { path: 'recovery.redownload_tag', label: 'Tag for re-added torrents' },

  { group: 'Delete torrents the tracker has deleted' },
  { path: 'recovery.delete_unregistered', label: 'Auto-delete unregistered torrents', type: 'checkbox' },
  { path: 'recovery.delete_unregistered_files', label: '…and their files (irreversible)', type: 'checkbox' },
  { path: 'recovery.delete_min_age_hours', label: 'Only after unregistered for (hours)', type: 'number' },
  { path: 'recovery.delete_max_per_scan', label: 'Max deletions per scan', type: 'number' },
  { path: 'recovery.delete_max_tracker_fraction', label: 'Abort if this fraction of a tracker is unregistered', type: 'number' },
  { path: 'recovery.delete_export_torrents', label: 'Keep the .torrent file before deleting', type: 'checkbox' },
  { path: 'recovery.delete_export_dir', label: 'Keep them in' },

  { group: 'Metrics' },
  { path: 'web.metrics_enabled', label: 'Expose Prometheus /metrics (unauthenticated)', type: 'checkbox' },

  { group: 'Notifications' },
  { path: 'notify.discord_enabled', label: 'Discord enabled', type: 'checkbox' },
  { path: 'notify.discord_webhook_url', label: 'Discord webhook URL', type: 'password' },
  { path: 'notify.telegram_enabled', label: 'Telegram enabled', type: 'checkbox' },
  { path: 'notify.telegram_bot_token', label: 'Telegram bot token', type: 'password' },
  { path: 'notify.telegram_chat_id', label: 'Telegram chat ID' },
  { path: 'notify.ntfy_enabled', label: 'ntfy enabled', type: 'checkbox' },
  { path: 'notify.ntfy_url', label: 'ntfy server' },
  { path: 'notify.ntfy_topic', label: 'ntfy topic' },
  { path: 'notify.ntfy_token', label: 'ntfy access token (optional)', type: 'password' },
  { path: 'notify.ntfy_username', label: 'ntfy username (optional)' },
  { path: 'notify.ntfy_password', label: 'ntfy password (optional)', type: 'password' },
  { path: 'notify.seeding_digest_enabled', label: 'Digest of torrents needing seeding', type: 'checkbox' },
  { path: 'notify.min_severity', label: 'Minimum severity (1-3)', type: 'number' },
  { path: 'notify.dedupe_hours', label: 'Repeat alert after (hours)', type: 'number' },

  { group: 'Web' },
  { path: 'web.password', label: 'WebUI password (blank = open)', type: 'password' },
  { path: 'web.base_url', label: 'External base URL' },

  { group: 'Single sign-on (OIDC)' },
  { path: 'web.oidc_enabled', label: 'SSO enabled', type: 'checkbox' },
  { path: 'web.oidc_display_name', label: 'Sign-in button text' },
  { path: 'web.oidc_issuer', label: 'Issuer URL' },
  { path: 'web.oidc_client_id', label: 'Client ID' },
  { path: 'web.oidc_client_secret', label: 'Client secret', type: 'password' },
  { path: 'web.oidc_scopes', label: 'Scopes' },
  { path: 'web.oidc_redirect_url', label: 'Redirect URL override (blank = from base URL)' },
  { path: 'web.oidc_allowed_groups', label: 'Allowed groups (blank = any)', type: 'list' },
  { path: 'web.oidc_auto_redirect', label: 'Skip the sign-in page', type: 'checkbox' },
];

const dig = (obj, path) => path.split('.').reduce((acc, key) => (acc == null ? acc : acc[key]), obj);

function put(obj, path, value) {
  const keys = path.split('.');
  let cursor = obj;
  keys.slice(0, -1).forEach((key) => { cursor = cursor[key] = cursor[key] || {}; });
  cursor[keys.at(-1)] = value;
}

async function loadConfig() {
  state.config = await api('/config');
  renderSettings();
  renderProwlarrSettings();
}

function renderSettings() {
  const pinned = state.meta.env_pinned || {};

  $('#settings-form').innerHTML = SETTINGS_SCHEMA.map((item) => {
    if (item.group) return `<div class="settings-group"><h3>${esc(item.group)}</h3></div>`;
    const value = dig(state.config, item.path);
    // A field pinned by an env var cannot be changed here: config.load()
    // reapplies the env value over anything saved. Show it locked and name the
    // variable, rather than accepting an edit that silently reverts.
    const env = pinned[item.path];
    const lock = env
      ? `<span class="muted hint">locked by <code>${esc(env)}</code> — change it in your compose file</span>`
      : '';
    const dis = env ? 'disabled' : '';

    if (item.type === 'checkbox') {
      return `<div class="field ${env ? 'pinned' : ''}"><label>&nbsp;</label>
        <label class="inline"><input type="checkbox" data-path="${item.path}" ${value ? 'checked' : ''} ${dis}> ${esc(item.label)}</label>
        ${lock}
      </div>`;
    }
    if (item.type === 'select') {
      const opts = item.options.map((o) => `<option ${o === value ? 'selected' : ''}>${o}</option>`).join('');
      return `<div class="field ${env ? 'pinned' : ''}"><label>${esc(item.label)}</label>
        <select data-path="${item.path}" ${dis}>${opts}</select>${lock}</div>`;
    }
    const isSecret = item.type === 'password';
    const shown = isSecret ? (value === SENTINEL ? '' : value ?? '')
      : item.type === 'list' ? (value || []).join(', ') : value ?? '';
    const type = item.type === 'number' ? 'number' : isSecret ? 'password' : 'text';
    const placeholder = isSecret && value === SENTINEL ? 'saved — leave blank to keep' : '';
    return `<div class="field ${env ? 'pinned' : ''}"><label>${esc(item.label)}</label>
      <input type="${type}" data-path="${item.path}" data-kind="${item.type || 'text'}"
             value="${esc(shown)}" placeholder="${placeholder}" ${dis}>${lock}</div>`;
  }).join('');
}

async function saveConfig(patch) {
  const body = await api('/config', { method: 'PUT', body: JSON.stringify(patch) });
  state.config = body.config;
  await loadPlugins();
  return body;
}

$('#save-settings').addEventListener('click', async () => {
  const patch = {};
  $$('#settings-form [data-path]').forEach((el) => {
    // Env-pinned fields are rendered disabled. Sending them would bake the env
    // value into config.json, so it would linger after the var was removed.
    if (el.disabled) return;
    const path = el.dataset.path;
    if (el.type === 'checkbox') return put(patch, path, el.checked);
    if (el.tagName === 'SELECT') return put(patch, path, el.value);
    if (el.dataset.kind === 'number') return put(patch, path, Number(el.value));
    if (el.dataset.kind === 'list') {
      return put(patch, path, el.value.split(',').map((s) => s.trim()).filter(Boolean));
    }
    if (el.dataset.kind === 'password' && el.value === '') {
      // Blank means "leave it alone", not "clear it".
      return dig(state.config, path) === SENTINEL ? put(patch, path, SENTINEL) : undefined;
    }
    return put(patch, path, el.value);
  });

  try {
    const res = await saveConfig(patch);
    renderSettings();
    if (res.ignored && res.ignored.length) {
      const names = res.ignored.map((i) => i.env).join(', ');
      toast(`Saved, but ${names} still overrides that setting`, 'bad');
    } else {
      toast('Settings saved', 'good');
    }
    $('#settings-saved').textContent = 'Saved — service reloaded.';
  } catch (err) {
    toast(err.message, 'bad');
  }
  setTimeout(() => { $('#settings-saved').textContent = ''; }, 4000);
});

$('#test-notify').addEventListener('click', async () => {
  try {
    const res = await api('/notify/test', { method: 'POST' });
    toast(res.error ? res.error : `Sent via ${Object.keys(res).join(', ')}`, res.error ? 'bad' : 'good');
  } catch (err) {
    toast(err.message, 'bad');
  }
});

// --- logs -------------------------------------------------------------------

$('#log-level').addEventListener('change', loadLogs);

async function loadLogs() {
  const level = $('#log-level').value;
  const { logs } = await api(`/logs?limit=300${level ? `&level=${level}` : ''}`);
  $('#log-console').innerHTML = logs.map((l) => {
    const time = new Date(l.ts * 1000).toLocaleTimeString();
    return `<span class="log-${esc(l.level)}">${time}  ${esc(l.level.padEnd(7))} ${esc(l.logger)}: ${esc(l.message)}</span>`;
  }).join('\n') || '<span class="muted">No log entries yet.</span>';
}

boot().catch((err) => {
  console.error(err);
  showLogin();
});
