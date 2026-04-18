const API = '/api';
let currentSession = null;

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + id).classList.add('active');
}

function showDashboard() { showView('dashboard'); currentSession = null; loadSessions(); }
function showSession(id) { showView('session'); currentSession = id; loadSessionDetail(id); }

function showCreateForm() { document.getElementById('create-form').classList.remove('hidden'); }
function hideCreateForm() { document.getElementById('create-form').classList.add('hidden'); document.getElementById('form-new-session').reset(); }

// ── Load Sessions ─────────────────────────────────────────────────────────────
async function loadSessions() {
  const loading = document.getElementById('loading-sessions');
  const table = document.getElementById('sessions-table');
  const noSessions = document.getElementById('no-sessions');
  loading.classList.remove('hidden');
  table.classList.add('hidden');
  noSessions.classList.add('hidden');

  try {
    const res = await fetch(API + '/sessions');
    const sessions = await res.json();
    const tbody = document.getElementById('sessions-tbody');
    loading.classList.add('hidden');
    if (!sessions.length) { noSessions.classList.remove('hidden'); return; }
    table.classList.remove('hidden');
    tbody.innerHTML = sessions.map(s => `
      <tr onclick="showSession('${s.id}')" style="cursor:pointer">
        <td>${esc(s.id)}</td>
        <td>${esc(s.domain)}</td>
        <td>${s.round || 0}</td>
        <td>${s.cases || 0}</td>
        <td>${s.code_version || 0}</td>
        <td>${new Date().toLocaleDateString()}</td>
        <td>
          <button class="btn btn-danger" style="padding:0.3rem 0.6rem;font-size:12px" onclick="event.stopPropagation(); deleteSession('${s.id}')">Delete</button>
        </td>
      </tr>`).join('');
  } catch (e) {
    loading.textContent = 'Error loading sessions: ' + e.message;
  }
}

// ── Create Session ────────────────────────────────────────────────────────────
async function createSession(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    domain: fd.get('domain'),
    moral_principles: fd.get('moral_principles'),
    user_clarifications: fd.get('user_clarifications') || null,
  };
  const btn = e.submitter;
  btn.disabled = true; btn.textContent = 'Creating...';
  try {
    const res = await fetch(API + '/sessions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error('Failed: ' + (await res.text()));
    const session = await res.json();
    toast('Session created: ' + session.id);
    hideCreateForm();
    loadSessions();
    showSession(session.id);
  } catch (e) {
    toast('Error: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Create Session';
  }
}

// ── Delete Session ────────────────────────────────────────────────────────────
async function deleteSession(id) {
  if (!confirm('Delete session ' + id + '? This cannot be undone.')) return;
  try {
    const res = await fetch(API + '/sessions/' + id, { method: 'DELETE' });
    if (!res.ok) throw new Error(await res.text());
    toast('Session deleted');
    loadSessions();
    if (currentSession === id) showDashboard();
  } catch (e) { toast('Error: ' + e.message); }
}

async function deleteCurrentSession() {
  if (currentSession) await deleteSession(currentSession);
}

// ── Load Session Detail ───────────────────────────────────────────────────────
async function loadSessionDetail(id) {
  document.getElementById('session-title').textContent = id.substring(0, 12) + '...';
  document.getElementById('session-meta').textContent = 'Loading...';

  try {
    const [detailRes, costsRes] = await Promise.all([
      fetch(API + '/sessions/' + id),
      fetch(API + '/sessions/' + id + '/costs').catch(() => null),
    ]);
    if (!detailRes.ok) throw new Error(await detailRes.text());
    const session = await detailRes.json();
    const costs = costsRes ? await costsRes.json() : null;

    document.getElementById('session-title').textContent = session.domain || id;
    const round = session.current_round || 0;
    const cases = session.cases ? session.cases.length : 0;
    document.getElementById('session-meta').textContent =
      `Round ${round} · ${cases} cases · Code v${session.legal_code?.version || 0}` +
      (costs ? ` · $${costs.total_cost_usd?.toFixed(4) || 0} spent` : '');

    // Legal code
    const codeEl = document.getElementById('legal-code-text');
    const codeVerEl = document.getElementById('code-version');
    codeEl.textContent = session.legal_code?.text || '(no code yet)';
    codeVerEl.textContent = 'v' + (session.legal_code?.version || 0);

    // Cases
    const caseList = document.getElementById('case-list');
    const allCases = session.cases || [];
    if (!allCases.length) {
      caseList.innerHTML = '<p class="empty-state">No cases yet. Run a round to start.</p>';
    } else {
      caseList.innerHTML = allCases.map(c => `
        <div class="case-item ${c.status || 'escalated'}">
          <div class="case-header">
            <span class="case-id">#${c.id} · Round ${c.round || 0}</span>
            <span class="case-status ${c.status === 'auto_resolved' ? 'resolved' : 'escalated'}">${c.status || 'open'}</span>
          </div>
          <div class="case-scenario">${esc(c.scenario || '')}</div>
          ${c.resolution ? `<p style="margin-top:0.5rem;font-size:13px"><strong>Resolution:</strong> ${esc(c.resolution)}</p>` : ''}
          ${c.outside_votes?.length ? `<p style="margin-top:0.5rem;font-size:12px;color:var(--accent)">${c.outside_votes.length} outside votes</p>` : ''}
        </div>`).join('');
    }
  } catch (e) {
    document.getElementById('session-meta').textContent = 'Error: ' + e.message;
  }
}

// ── Run Session ────────────────────────────────────────────────────────────────
async function runSession() {
  if (!currentSession) return;
  const btn = document.getElementById('btn-run');
  const status = document.getElementById('run-status');
  btn.disabled = true;
  status.textContent = 'Running...';

  try {
    const res = await fetch(API + '/sessions/' + currentSession + '/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(await res.text());
    const result = await res.json();
    status.textContent = `Round ${result.current_round} done · ${result.cases_found} cases · ${result.auto_resolved} resolved`;
    toast('Round complete');
    await loadSessionDetail(currentSession);
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
    toast('Run error: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Init
loadSessions();
