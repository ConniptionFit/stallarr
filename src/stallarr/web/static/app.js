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

const label = (text) => String(text).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

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
    renderStats(status);
    renderIssues(status.issues || []);
    $('#dry-run-pill').hidden = !status.recovery.dry_run;
    renderSeedingMetrics(status.seeding || {});
    renderSeedingUnmatched(status.seeding_unmatched || []);

    if (state.activeTab === 'torrents') await loadTorrents();
    if (state.activeTab === 'history') await loadHistory();
    if (state.activeTab === 'logs') await loadLogs();
    if (state.activeTab === 'trackers') { renderPlugins(status); await loadTrackerStats(); }
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

function renderStats(status) {
  const scan = status.last_scan || {};
  const critical = (status.issues || []).filter((i) => i.severity >= 3).length;
  const trackers = Object.values(status.trackers || {});
  const trackersUp = trackers.filter((t) => t.ok).length;
  const seeding = status.seeding || {};

  const cards = [
    { label: 'Torrents', value: scan.total ?? 0, sub: `${scan.skipped ?? 0} skipped` },
    { label: 'Healthy', value: scan.healthy ?? 0, cls: 'ok', sub: 'passing all checks' },
    { label: 'Problems', value: scan.sick ?? 0, cls: scan.sick ? 'warn' : 'ok', sub: `${critical} critical` },
    { label: 'Repairs', value: scan.actions ?? 0, sub: 'last scan' },
    {
      label: 'Seeding owed',
      value: seeding.outstanding ?? 0,
      cls: seeding.outstanding ? 'warn' : 'ok',
      sub: seeding.outstanding
        ? `${seeding.working ?? 0} being repaired`
        : `${seeding.seen ?? 0} seen in ${seeding.window_days ?? 7}d`,
    },
    {
      label: 'H&R averted',  // escaped on render — do not pre-encode
      value: seeding.averted ?? 0,
      cls: seeding.averted ? 'ok' : '',
      sub: `cleared after a repair · last ${seeding.window_days ?? 7}d`,
    },
    {
      label: 'Client',
      value: status.client.connected ? 'Up' : 'Down',
      cls: status.client.connected ? 'ok' : 'crit',
      sub: esc(status.client.detail || ''),
    },
    {
      label: 'Trackers',
      value: `${trackersUp}/${trackers.length}`,
      cls: trackers.length && trackersUp === trackers.length ? 'ok' : trackers.length ? 'warn' : '',
      sub: 'plugins reachable',
    },
    {
      label: 'Last scan',
      value: status.scanning ? '…' : ago(scan.finished_at),
      sub: `${scan.duration ?? 0}s · every ${status.scan_interval_minutes}m`,
    },
  ];

  $('#stat-grid').innerHTML = cards.map((c) => `
    <div class="stat ${c.cls || ''}">
      <div class="value">${esc(c.value)}</div>
      <div class="label">${esc(c.label)}</div>
      <div class="sub">${c.sub}</div>
    </div>`).join('');
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
      <td class="name">${esc(item.name)}</td>
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

async function loadTorrents() {
  const onlyIssues = $('#only-issues').checked;
  const { torrents } = await api(`/torrents?only_issues=${onlyIssues}`);
  const body = $('#torrents-table tbody');

  if (!torrents.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No torrents to show.</td></tr>';
    return;
  }

  body.innerHTML = torrents.map((t) => {
    const notes = t.skipped
      ? `<span class="muted">skipped: ${esc(t.skipped)}</span>`
      : t.findings.map((f) => `<span class="pill">${esc(label(f.ailment))}</span>`).join(' ') || '<span class="pill ok">healthy</span>';
    return `
      <tr>
        <td><span class="dot ${SEV[t.severity] || 'ok'}"></span></td>
        <td class="name">${esc(t.name)}<div class="muted">${bytes(t.size)}</div></td>
        <td><span class="pill">${esc(t.raw_state)}</span></td>
        <td><div class="bar"><i style="width:${(t.progress * 100).toFixed(1)}%"></i></div>
            <span class="muted">${(t.progress * 100).toFixed(1)}%</span></td>
        <td>${t.ratio.toFixed(2)}</td>
        <td>${t.tracker ? esc(t.tracker) : `<span class="muted">${esc(t.tracker_hosts[0] || '—')}</span>`}</td>
        <td>${notes}</td>
        <td class="actions"><button class="tiny" data-repair="${esc(t.infohash)}">Repair</button></td>
      </tr>`;
  }).join('');
  wireRowActions(body);
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
    const cls = unseen ? 'warn' : s.hit_and_runs ? 'crit' : 'ok';
    const seeds = s.seeding != null
      ? `${s.seeding} counted / ${s.local_seeding} local`
      : `${s.local_seeding} seeding locally`;
    const parts = [seeds];
    if (s.uploaded != null) parts.push(`↑ ${bytes(s.uploaded)}`);
    if (s.hit_and_runs != null) parts.push(`H&amp;R ${s.hit_and_runs}`);
    if (unseen) parts.push(`<strong>${unseen} not announcing</strong>`);
    return `
      <div class="stat ${cls}">
        <div class="value">${s.ratio != null ? s.ratio.toFixed(2) : '—'}</div>
        <div class="label">${esc(s.name || id)}</div>
        <div class="sub">${parts.join(' · ')}</div>
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
