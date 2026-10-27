// smtp-graph-relay dashboard. Renders /api/summary every 10 seconds.
// Everything shown comes from devices on the network, so it is only ever set as text
// (textContent), never as HTML.
'use strict';

const REFRESH_MS = 10000;
const DAY = 86400;

// What a device's last problem means, and the one-line fix.
const FIXES = {
  'tls-untrusted-certificate': 'Turn off certificate verification ("Verify Peer") in the device\'s email settings, '
    + 'or give the relay a trusted certificate (TLS_CERT_FILE / TLS_KEY_FILE).',
  'tls-old-version': 'The device only speaks an old TLS version. Set its encryption to None, or update its firmware.',
  'tls-implicit-tls': 'The device starts TLS immediately (SSL/TLS). Set its encryption to STARTTLS, or None.',
  'tls-no-shared-cipher': 'No cipher in common. Set the device\'s encryption to None, or update its firmware.',
  'tls-aborted': 'The device hung up while setting up encryption, usually because it rejected the certificate. '
    + 'Turn off certificate verification on the device.',
  'tls-handshake-failed': 'Try STARTTLS without certificate verification, or no encryption.',
  'login-failed': 'Correct the username or password on the device (SMTP_AUTH_USER / SMTP_AUTH_PASS).',
  'locked': 'Correct the SMTP password on the device. It unlocks by itself after 15 minutes.',
  'sender-not-allowed': 'Add the device\'s sender address to SMTP_ALLOWED_SENDERS, or change it on the device.',
  'invalid-sender': 'Set a valid sender address on the device.',
  'from-header-mismatch': 'The email\'s From line differs from the sender the device uses. Make them match.',
  'microsoft-refused': 'Microsoft 365 refused this mailbox. Check the app\'s Exchange scope (tools/Setup-RelayTenant.ps1).',
};
const REFUSALS = {
  'sender-not-allowed': 'sender not allowed',
  'invalid-sender': 'invalid sender',
  'from-header-mismatch': 'From line mismatch',
  'unparseable': 'unreadable message',
  'middleware-error': 'middleware error',
};

const state = { level: 'all', query: '', summary: null, logOpen: false, fromFilled: false };

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child !== null && child !== undefined) node.append(child);
  return node;
}

const now = () => Date.now() / 1000;
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });

function ago(ts) {
  const s = Math.max(0, now() - ts);
  if (s < 60) return `${Math.round(s)} sec ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < DAY) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / DAY)} d ago`;
}

function until(ts) {
  const s = ts - now();
  if (s <= 0) return 'now';
  if (s < 60) return `in ${Math.round(s)} sec`;
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  return `in ${Math.round(s / 3600)} h`;
}

function pill(kind, text, withDot = false) {
  return el('span', { class: `pill ${kind}` }, withDot ? el('span', { class: 'dot' }) : null, text);
}

function deviceName(d) {
  if (!d) return null;
  if (d.name === 'dashboard-test') return 'Dashboard test';
  // Many apps say hello as "[127.0.0.1]" (an address, not a name); their DNS name says more.
  if (!d.name || /^\[.*\]$/.test(d.name)) return d.hostname || d.name || null;
  return d.name;
}

function where(d) {
  const shown = deviceName(d);
  return [d.hostname !== shown ? d.hostname : null, d.ip].filter(Boolean).join(' · ');
}

// --- header and summary ---------------------------------------------------------------

function renderStatus(relay, reachable) {
  const pills = [reachable ? pill('ok', 'Running', true) : pill('bad', 'Unreachable', true)];
  const ms = relay ? relay.microsoft || {} : {};
  if (ms.ok === true) pills.push(pill('ok', 'Microsoft 365 sign-in OK'));
  else if (ms.ok === false) pills.push(pill('bad', 'Microsoft 365 sign-in failing'));
  else if (relay) pills.push(pill('muted-pill', 'Microsoft 365 not contacted yet'));
  if (relay) pills.push(pill('muted-pill', `${relay.starttls ? 'STARTTLS on' : 'STARTTLS off'} · port ${relay.smtp_port}`));
  document.getElementById('status-pills').replaceChildren(...pills);
}

function renderMetrics(s) {
  const items = [
    [s.sent, 'sent', ''],
    [s.queued, 'retrying', s.queued ? 'var(--warn)' : ''],
    [s.rejected, 'refused', s.rejected ? 'var(--bad)' : ''],
    [s.devices, s.devices === 1 ? 'device' : 'devices', ''],
  ];
  document.getElementById('metrics').replaceChildren(...items.map(([value, label, color]) => {
    const number = el('span', { class: 'metric-value', text: String(value) });
    if (color) number.style.color = color;
    return el('div', { class: 'metric' }, number, el('span', { class: 'metric-label', text: label }));
  }));
}

// --- needs attention ------------------------------------------------------------------

function renderAttention(summary) {
  const problems = [];
  const ms = summary.relay.microsoft || {};
  if (ms.ok === false) {
    problems.push({
      who: 'Microsoft 365', where: 'sign-in', title: 'Sign-in failing: mail is being queued, not sent',
      fix: `Check AUTHORITY, CLIENT_ID and CLIENT_SECRET in .env (the secret may have expired). ${ms.error || ''}`,
      when: ms.at ? ago(ms.at) : '',
    });
  }
  if (!summary.relay.queue_enabled) {
    problems.push({
      who: 'Retry queue', where: 'data volume', title: 'Queue is off: the data volume is not writable',
      fix: 'Mount a writable volume at /data so temporary failures are queued instead of refused.', when: '',
    });
  }
  for (const d of summary.devices) {
    if (!d.last_reason || now() - d.last_seen > DAY) continue;
    problems.push({
      who: deviceName(d) || 'Unknown device', where: where(d), title: d.last_result || 'Problem',
      fix: FIXES[d.last_reason] || '', when: ago(d.last_seen),
    });
  }
  const section = document.getElementById('attention');
  section.hidden = problems.length === 0;
  document.getElementById('attention-count').textContent =
    `${problems.length} ${problems.length === 1 ? 'item' : 'items'}`;
  document.getElementById('attention-list').replaceChildren(...problems.map((p) => el('div', { class: 'problem' },
    el('div', { class: 'problem-who' }, el('strong', { text: p.who }), el('span', { class: 'where', text: p.where })),
    el('div', { class: 'problem-what' }, el('span', { class: 'title', text: p.title }),
      p.fix ? el('span', { class: 'fix', text: p.fix }) : null),
    el('span', { class: 'problem-when', text: p.when }))));
}

// --- devices --------------------------------------------------------------------------

function tlsPill(d) {
  if (d.last_reason && d.last_reason.startsWith('tls-')) return pill('bad', 'TLS failed');
  if (d.tls === 'yes') return pill('ok', 'STARTTLS');
  if (d.tls === 'no') return pill('warn', 'No encryption');
  return pill('muted-pill', '—');
}

function renderDevices(devices) {
  const rows = devices.map((d) => el('div', { class: 'device' },
    el('span', { class: `dot ${d.last_reason ? 'dot-bad' : 'dot-ok'}` }),
    el('div', { class: 'device-name' },
      el('span', { class: 'name', text: deviceName(d) || 'Unknown device' }),
      el('span', { class: 'sub', text: where(d) })),
    tlsPill(d),
    el('span', { class: 'device-count', text: `${d.messages_24h} today` }),
    el('span', { class: 'device-seen', text: ago(d.last_seen) })));
  document.getElementById('device-list').replaceChildren(
    ...(rows.length ? rows : [el('div', { class: 'empty', text: 'No devices yet. They appear here as soon as they connect.' })]));
}

// --- activity -------------------------------------------------------------------------

function activityResult(m, queued) {
  if (m.result === 'queued') {
    const job = queued.get(m.queue_id);
    if (!job) return pill('warn', 'Queued');
    const cancel = el('button', { type: 'button', class: 'mini-btn', 'data-cancel': job.id, text: 'Cancel' });
    return el('span', {}, pill('warn', `Retrying · next try ${until(job.next_attempt)}`), cancel);
  }
  if (m.result === 'cancelled') return pill('muted-pill', 'Cancelled');
  if (m.result === 'sent') return pill('ok', m.attempts > 1 || m.queue_id ? 'Sent after retry' : 'Sent');
  if (m.result === 'rejected') {
    const why = REFUSALS[m.reason] || (m.graph ? `Microsoft 365 (${m.graph})` : 'refused');
    return pill('bad', `Refused · ${why}`);
  }
  if (m.result === 'failed') return pill('bad', 'Gave up after retries');
  if (m.result === 'deferred') return pill('warn', 'Device told to retry');
  if (m.result === 'skipped') return pill('muted-pill', 'Skipped by middleware');
  return pill('muted-pill', m.result || '?');
}

function renderActivity(summary) {
  const byIp = new Map(summary.devices.map((d) => [d.ip, d]));
  const queued = new Map(summary.queue.map((q) => [q.id, q]));
  const rows = summary.messages.map((m) => {
    const recipients = (m.rcpts || '').split(',').filter(Boolean);
    const to = recipients.length > 1 ? `${recipients[0]} +${recipients.length - 1}` : (recipients[0] || '');
    return el('tr', {},
      el('td', { class: 'faint', text: clock(m.ts) }),
      el('td', { text: deviceName({ ...byIp.get(m.ip), ...(m.name ? { name: m.name } : {}) }) || m.ip || '' }),
      el('td', { text: to ? `${m.sender || ''} → ${to}` : (m.sender || '') }),
      el('td', {}, activityResult(m, queued)),
      el('td', { class: 'num', text: String(m.attempts ?? 0) }));
  });
  document.getElementById('activity-rows').replaceChildren(
    ...(rows.length ? rows : [el('tr', {}, el('td', { class: 'empty-cell', colspan: '5', text: 'No messages yet.' }))]));
  const cancelAll = document.getElementById('cancel-all');
  cancelAll.hidden = summary.queue.length === 0;
  cancelAll.textContent = `Cancel all retries (${summary.queue.length})`;
}

async function cancelRetries(payload, question) {
  if (!window.confirm(question)) return;
  try {
    const response = await fetch('/api/queue/cancel', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (response.status === 401) { window.location.assign('/login'); return; }
    const result = await response.json();
    if (result.skipped && result.skipped.some((s) => s.status === 'sending')) {
      window.alert('One message was being sent at that moment and could not be cancelled.');
    }
  } catch {
    window.alert('Could not reach the dashboard. Check that the relay is still running.');
  }
  refresh();
}

// --- test send ------------------------------------------------------------------------

function renderSenders(relay) {
  const suggestions = relay.sender_suggestions || [];
  document.getElementById('sender-list').replaceChildren(...suggestions.map((s) =>
    el('option', { value: s.address, label: s.source })));
  const from = document.getElementById('test-from');
  const chips = suggestions.map((s) => {
    const chip = el('button', { type: 'button', class: 'chip' }, s.address, el('span', { class: 'source', text: ` · ${s.source}` }));
    chip.addEventListener('click', () => { from.value = s.address; from.focus(); });
    return chip;
  });
  const wildcards = relay.allowed_senders.filter((a) => a.startsWith('*@'));
  if (wildcards.length) {
    chips.push(el('span', { class: 'chips-label', text: `Also allowed: any address at ${wildcards.map((w) => w.slice(2)).join(', ')}` }));
  }
  document.getElementById('sender-chips').replaceChildren(
    ...(chips.length ? [el('span', { class: 'chips-label', text: 'Suggestions' }), ...chips] : []));
  if (!state.fromFilled && suggestions.length && !from.value) from.value = suggestions[0].address;
  state.fromFilled = true;
}

function showTestResult(kind, title, detail, hint) {
  const box = document.getElementById('test-result');
  box.className = `test-result ${kind}`;
  box.replaceChildren(el('strong', { text: title }),
    detail ? el('span', { class: 'detail', text: detail }) : null,
    hint ? el('span', { class: 'hint', text: hint }) : null);
  box.hidden = false;
}

const STAGES = { input: 'Check the addresses', connect: 'Could not reach the relay', login: 'Relay login failed',
  sender: 'Sender refused', recipient: 'Recipient refused', data: 'Message refused' };

async function sendTest(event) {
  event.preventDefault();
  const button = document.getElementById('test-button');
  const payload = { from: document.getElementById('test-from').value.trim(), to: document.getElementById('test-to').value.trim() };
  button.disabled = true;
  button.textContent = 'Sending…';
  showTestResult('neutral', 'Sending through the relay…', null, 'This can take up to 30 seconds if Microsoft 365 is slow.');
  try {
    const response = await fetch('/api/test-send', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (response.status === 401) { window.location.assign('/login'); return; }
    const result = await response.json();
    const detail = result.code ? `${result.code} ${result.reply}` : result.reply;
    if (result.ok && /Queued/.test(result.reply)) showTestResult('queued', 'Accepted and queued for retry', detail, result.hint);
    else if (result.ok) showTestResult('sent', `Sent as ${payload.from}`, detail, `Check ${payload.to}'s inbox. A copy is in the sender's Sent Items.`);
    else showTestResult('rejected', STAGES[result.stage] || 'Not sent', detail, result.hint);
  } catch {
    showTestResult('rejected', 'Could not reach the dashboard', null, 'Check that the relay is still running.');
  } finally {
    button.disabled = false;
    button.textContent = 'Send test';
    refresh();
  }
}

// --- raw log --------------------------------------------------------------------------

function renderLogs() {
  if (!state.summary || !state.logOpen) return;
  const query = state.query.toLowerCase();
  const lines = state.summary.logs.filter((l) => {
    if (state.level === 'warn' && l.level === 'INFO') return false;
    if (state.level === 'error' && !(l.level === 'ERROR' || l.level === 'CRITICAL')) return false;
    return !query || l.text.toLowerCase().includes(query);
  }).reverse();
  document.getElementById('log-lines').replaceChildren(...(lines.length ? lines.map((l) => el('div', { class: 'log-line' },
    el('span', { class: 'log-time', text: clock(l.ts) }),
    el('span', { class: `log-level lvl-${l.level}`, text: l.level }),
    el('span', { class: 'log-text', text: l.text }))) : [el('div', { class: 'log-empty', text: 'No matching log lines.' })]));
}

function toggleLog() {
  state.logOpen = !state.logOpen;
  document.getElementById('log-toggle').setAttribute('aria-expanded', String(state.logOpen));
  document.getElementById('log-toggle-label').textContent = state.logOpen ? 'Hide raw log' : 'Show raw log (last 300 lines)';
  document.getElementById('log-body').hidden = !state.logOpen;
  renderLogs();
}

// --- refresh --------------------------------------------------------------------------

async function refresh() {
  let response;
  try {
    response = await fetch('/api/summary', { credentials: 'same-origin', cache: 'no-store' });
  } catch {
    renderStatus(state.summary && state.summary.relay, false);
    document.body.classList.remove('loading');
    return;
  }
  if (response.status === 401) { window.location.assign('/login'); return; }
  if (!response.ok) {
    renderStatus(state.summary && state.summary.relay, false);
    document.body.classList.remove('loading');
    return;
  }
  const summary = await response.json();
  state.summary = summary;
  renderStatus(summary.relay, true);
  document.getElementById('who').textContent = summary.relay.user;
  document.getElementById('updated').textContent = `Updated ${clock(summary.generated)}`;
  renderMetrics(summary.stats);
  renderAttention(summary);
  renderDevices(summary.devices);
  renderActivity(summary);
  renderSenders(summary.relay);
  renderLogs();
  document.getElementById('footer').textContent =
    `smtp-graph-relay ${summary.relay.version} · history kept ${summary.relay.retention_days} days`
    + (summary.persistent ? '' : ' (in memory only: data volume not writable)');
  document.body.classList.remove('loading');  // first render done: show the cards in their final place
}

document.addEventListener('DOMContentLoaded', () => {
  for (const chip of document.querySelectorAll('.toolbar .chip')) {
    chip.addEventListener('click', () => {
      state.level = chip.dataset.level;
      for (const other of document.querySelectorAll('.toolbar .chip')) other.classList.toggle('chip-on', other === chip);
      renderLogs();
    });
  }
  document.getElementById('log-search').addEventListener('input', (e) => { state.query = e.target.value; renderLogs(); });
  document.getElementById('log-toggle').addEventListener('click', toggleLog);
  document.getElementById('test-form').addEventListener('submit', sendTest);
  document.getElementById('activity-rows').addEventListener('click', (e) => {
    const button = e.target.closest('button[data-cancel]');
    if (!button) return;
    button.disabled = true;
    cancelRetries({ id: button.dataset.cancel }, 'Stop retrying this message? It will not be sent.')
      .finally(() => { button.disabled = false; });
  });
  document.getElementById('cancel-all').addEventListener('click', () => {
    const count = state.summary ? state.summary.queue.length : 0;
    cancelRetries({ all: true }, `Stop retrying all ${count} queued messages? None of them will be sent.`);
  });
  refresh();
  setInterval(refresh, REFRESH_MS);
});
