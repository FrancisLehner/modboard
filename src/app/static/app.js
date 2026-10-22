/* Modboard's frontend: one render function over one payload.
 *
 * Everything on screen is a pure function of `S`, and `set()` is the only way
 * to change `S`. That is why there is no diffing and no component tree -- the
 * whole document is rebuilt on every change, which for five courses is cheaper
 * than the bookkeeping that would avoid it.
 *
 * The payload comes from GET /api/dashboard already shaped for these
 * functions; src/app/view_model.py does the reshaping. The one thing this file
 * must not do is derive a fact the backend did not send: a missing weight stays
 * missing, and an inferred link stays visibly inferred.
 */

const DAY = 86400000;
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const TYPES = {
  exam: { label: 'Exam', v: '--t-exam' }, project: { label: 'Project', v: '--t-project' },
  assignment: { label: 'Assignment', v: '--t-assignment' }, quiz: { label: 'Quiz', v: '--t-quiz' },
  presentation: { label: 'Presentation', v: '--t-presentation' },
  participation: { label: 'Participation', v: '--t-participation' },
  other: { label: 'Other', v: '--t-other' }
};

/* ---------------- state ---------------- */
const S = {
  screen: 'loading',   // loading | apikey | start | syncmode | settings | signedout | pulling | overview | course
  courseId: null,
  apiKey: '',
  keyError: '',
  data: null,          // GET /api/dashboard
  sync: null,          // GET /api/sync
  error: '',
  syncMode: 'changed', // changed | full -- what the sync-mode screen has selected
  syncFrom: 'overview', // the screen to go back to from the sync-mode screen
  settingsFrom: 'overview', // the screen to go back to from the settings menu
  pollTimer: null,
  openDesc: {},       // descKey -> true, for rows showing their description
  openBuckets: {}     // overview column title -> true, for columns showing every card
};
const set = (patch) => { Object.assign(S, patch); render(); };

/* Which rows have their description open. The key has to survive a re-render,
   so it is the activity's Moodle id, or name and date where an extracted item
   has no id. Encoded, because it travels through an onclick attribute --
   including the apostrophe, which encodeURIComponent leaves alone and which
   would otherwise close the quoted argument. */
const descKey = (it) =>
  encodeURIComponent(it.cmid || `${it.name}|${it.due || ''}`).replace(/'/g, '%27');
function toggleDesc(key) {
  if (S.openDesc[key]) delete S.openDesc[key]; else S.openDesc[key] = true;
  render();
}
function toggleBucket(title) {
  if (S.openBuckets[title]) delete S.openBuckets[title]; else S.openBuckets[title] = true;
  render();
}

/* Mark one activity as done or not done, for a hand-in Moodle never saw. The
   key is encoded the same way as descKey, for the same onclick reason. The
   answer is the whole dashboard, so the page redraws from one payload. */
const markKey = (it) => encodeURIComponent(it.key).replace(/'/g, '%27');
async function markDone(courseId, key, done) {
  try {
    const payload = await post(`/api/courses/${courseId}/status`, { key: decodeURIComponent(key), done });
    S.data = payload;
    set({ sync: payload.state.sync, error: '' });
  } catch (exc) {
    set({ error: exc.message });
  }
}

/* ---------------- api ---------------- */
async function api(path, options) {
  const response = await fetch(path, options);
  let body = {};
  try { body = await response.json(); } catch (e) { /* empty body is fine */ }
  if (!response.ok && body.error) throw new Error(body.error);
  if (!response.ok) throw new Error(`${path} failed (${response.status})`);
  return body;
}
const post = (path, payload) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {})
});

/* ---------------- helpers ---------------- */
const today = () => (S.data && S.data.today) || new Date().toISOString().slice(0, 10);
const ms = (iso) => { const p = String(iso).split('-').map(Number); return Date.UTC(p[0], p[1] - 1, p[2]); };
const daysTo = (iso) => iso ? Math.round((ms(iso) - ms(today())) / DAY) : null;
const fmt = (iso) => {
  if (!iso) return 'No date';
  const d = new Date(ms(iso));
  return WD[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' + MON[d.getUTCMonth()];
};
const urgency = (d) => d < 0 ? { bg: '#7A1B12', fg: '#fff' } : d <= 2 ? { bg: '#D92D20', fg: '#fff' }
  : d <= 6 ? { bg: '#FEC84B', fg: '#4E2D00' } : d <= 13 ? { bg: '#FDEAD7', fg: '#7A3E10' }
    : { bg: '#E4EEE2', fg: '#2E5A2C' };
const daysLabel = (d) => d < 0 ? 'Overdue by ' + (-d) + 'd' : d === 0 ? 'Today' : d === 1 ? 'Tomorrow' : 'In ' + d + ' days';
const tvar = (t) => 'var(' + (TYPES[t] || TYPES.other).v + ')';
/* Stands in for a big number that has no value. Muted, so an absent figure
   does not read as loudly as a real one. */
const blank = '<span style="color:var(--line-3)">–</span>';
const typeLabel = (t) => (TYPES[t] || TYPES.other).label;
const esc = (s) => String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const plural = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');

function ago(seconds) {
  if (!seconds) return 'never';
  const mins = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
  if (mins < 1) return 'just now';
  if (mins < 60) return plural(mins, 'min') + ' ago';
  const hours = Math.round(mins / 60);
  if (hours < 24) return plural(hours, 'hour') + ' ago';
  return plural(Math.round(hours / 24), 'day') + ' ago';
}

/* Whether an item counts toward the final grade. Three states, because the
   data has three: Moodle says so, a weighted component says so, or nothing
   does. The third is not "no". */
const gradedFlag = (it) => {
  const text = it.counts === true ? 'Graded' : it.counts === false ? 'Not graded' : 'Weight unknown';
  const colour = it.counts === true ? 'var(--ink)' : 'var(--muted)';
  return `<span style="display:inline-flex;align-items:center;gap:8px"><span class="divider"></span>`
    + `<span class="flag" style="color:${colour}">${text}</span></span>`;
};

/* Badge on anything a model read out of a document rather than Moodle
   returning it, and on anything it was unsure about. */
function provenance(entry) {
  if (!entry) return '';
  const title = entry.source ? `Read from ${entry.source}` : '';
  if (entry.confidence && entry.confidence == 'low') {
    return `<span class="tag warn" title="${esc(title)} (${esc(entry.confidence)} confidence)">Low Confidence Check this</span>`;
  }
  if (entry.origin === 'extracted') {
    return `<span class="tag info" title="${esc(title)}">From a document</span>`;
  }
  return '';
}

const courses = () => (S.data && S.data.courses) || [];
const course = (id) => courses().find((c) => c.id === id) || courses()[0] || null;

/* Everything a course page and the overview count over: its activities, the
   component each one sits under, and what is still outstanding. */
function stats(c) {
  const tag = { course: c.id, code: c.code };
  const items = c.grading.flatMap((g) => g.items.map((i) => ({ ...i, ...tag })))
    .concat(c.unmatched.map((i) => ({ ...i, ...tag })));
  const open = items.filter((i) => i.status === 'open');
  const dated = open.filter((i) => i.due).sort((a, b) => ms(a.due) - ms(b.due));
  const undated = open.filter((i) => !i.due);
  const upcoming = c.onsite.filter((o) => o.date && daysTo(o.date) >= 0);
  return {
    items, open, dated, undated, upcoming,
    nextDeadline: dated[0] || null,
    pastOnsite: c.onsite.length - upcoming.length
  };
}

/* ---------------- icons ---------------- */
const I = {
  logo: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
  home: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/></svg>',
  arrow: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h16M14 6l6 6-6 6"/></svg>',
  back: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12H4M10 6l-6 6 6 6"/></svg>',
  ext: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17L17 7M9 7h8v8"/></svg>',
  sync: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>',
  key: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3M14 9l2 2"/></svg>',
  lock: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  check: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"/></svg>',
  tick: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"/></svg>',
  cross: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  logout: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5M5 12h11"/></svg>',
  chevron: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
  gear: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>'
};
const brand = (size) => `<div class="brand"><div class="brand-mark">${I.logo}</div><div class="brand-name" style="${size ? 'font-size:' + size + 'px' : ''}">Modboard</div></div>`;

/* ================= ONBOARDING ================= */
const KEY_RE = /^sk-ant-[\w-]{10,}$/;

async function saveKey() {
  try {
    const state = await post('/api/key', { api_key: S.apiKey.trim() });
    // The server picks: a first run goes on to get started, a changed key
    // goes back to the dashboard.
    set({ keyError: '', apiKey: '', screen: state.screen });
  } catch (exc) {
    set({ keyError: exc.message });
  }
}

function screenApiKey() {
  const valid = KEY_RE.test(S.apiKey.trim());
  const typed = S.apiKey.length > 0;
  const message = S.keyError || (typed && !S.apiKey.startsWith('sk-ant-') ? 'Keys start with <span class="mono">sk-ant-</span>' : '');
  const back = S.data && S.data.courses.length
    ? `<button class="btn-ghost" onclick="set({screen:'settings',keyError:''})">${I.back} Back</button>` : '';
  return `<div class="onb"><div class="onb-card">
    <div style="display:flex;justify-content:space-between;align-items:center">${brand()}<span class="sub">Step 1 of 2</span></div>
    <div class="steps"><span class="on"></span><span></span></div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <h1 style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.025em;line-height:1.1">Add your Anthropic API key</h1>
      <p style="margin:0;font-size:16px;color:var(--ink-2);line-height:1.5">Modboard uses Claude to read your course documents and pull out data that could not be retrieved from relational data.</p>
    </div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <label for="key" style="font-size:14px;font-weight:500">API key</label>
      <div class="field">${I.key}<input id="key" type="password" autocomplete="off" spellcheck="false" placeholder="sk-ant-…" value="${esc(S.apiKey)}"
        oninput="S.apiKey=this.value;S.keyError='';document.getElementById('kbtn').disabled=!/^sk-ant-[\\w-]{10,}$/.test(this.value.trim());document.getElementById('kerr').style.visibility=this.value&&!/^sk-ant-/.test(this.value)?'visible':'hidden'"
        onkeydown="if(event.key==='Enter'&&/^sk-ant-[\\w-]{10,}$/.test(this.value.trim()))saveKey()"></div>
      <div id="kerr" style="font-size:13px;color:#B42318;visibility:${message ? 'visible' : 'hidden'}">${message || '&nbsp;'}</div>
      <div style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)">${I.lock} Stored in your macOS Keychain. Never leaves this machine except to call Anthropic.</div>
      <div style="font-size:13px;color:var(--muted)">Attention: you need to have credits for the API key.</div>
    </div>
    <div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:16px">
      <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener" style="font-size:14px;display:flex;align-items:center;gap:6px">Get a key in the Console ${I.ext}</a>
      <div style="display:flex;gap:10px;align-items:center">${back}
        <button id="kbtn" class="btn-primary" ${valid ? '' : 'disabled'} onclick="saveKey()">Save and continue</button></div>
    </div>
  </div></div>`;
}

function screenStart() {
  return `<div class="onb"><div class="onb-card">
    <div style="display:flex;justify-content:space-between;align-items:center">${brand()}<span class="sub">Step 2 of 2</span></div>
    <div class="steps"><span class="on"></span><span class="on"></span></div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <h1 style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.025em;line-height:1.1">Nothing here yet</h1>
      <p style="margin:0;font-size:16px;color:var(--ink-2);line-height:1.5">Modboard will pull your courses from Moodle and read through the documents. This runs in the background and takes a few minutes the first time.</p>
    </div>
    <div style="display:grid;grid-template-columns:auto 1fr;gap:12px 14px;font-size:15px;align-items:baseline">
      <span class="mono" style="color:var(--muted)">01</span><span>Courses, assignments, quizzes and grades</span>
      <span class="mono" style="color:var(--muted)">02</span><span>Syllabi, PDFs and announcements</span>
      <span class="mono" style="color:var(--muted)">03</span><span>Grading scheme and mandatory on-site sessions, read by Claude</span>
    </div>
    ${S.error ? `<div class="alert">${esc(S.error)}</div>` : ''}
    <p style="margin:0;font-size:13px;color:var(--muted)">The first run signs you in to Moodle in a browser window. Steps 02 and 03 call the Anthropic API and cost money.</p>
    <button class="btn-primary" style="width:100%;min-height:58px;font-size:18px" onclick="startSync()">Get started ${I.arrow}</button>
  </div></div>`;
}

/* ---- choosing how much to pull ---- */
function askSync() {
  set({ screen: 'syncmode', syncFrom: S.screen === 'course' ? 'course' : 'overview' });
}

function screenSyncMode() {
  const option = (mode, title, text) => `
    <button class="choice ${S.syncMode === mode ? 'on' : ''}" onclick="set({syncMode:'${mode}'})" aria-pressed="${S.syncMode === mode}">
      <span class="radio"></span>
      <span style="display:flex;flex-direction:column;gap:4px">
        <span style="font-size:16px;font-weight:500">${title}</span>
        <span style="font-size:14px;color:var(--ink-2);line-height:1.45">${text}</span>
      </span></button>`;
  return `<div class="onb"><div class="onb-card">
    <div style="display:flex;justify-content:space-between;align-items:center">${brand()}<span class="sub">Sync</span></div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <h1 style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.025em;line-height:1.1">What should this sync pull?</h1>
      <p style="margin:0;font-size:16px;color:var(--ink-2);line-height:1.5">Courses and assignments are always refreshed.</p>
    </div>
    <div style="display:flex;flex-direction:column;gap:10px">
      ${option('changed', 'Only new and changed',
        'You want to see what changed since the last sync.')}
      ${option('full', 'Full rebuild',
        'You want Modboard to fetch everything from zero and rebuild.')}
    </div>
    <div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:10px">
      <button class="btn-ghost" onclick="set({screen:S.syncFrom})">${I.back} Back</button>
      <button class="btn-primary" onclick="startSync()">${I.sync} Start sync</button>
    </div>
  </div></div>`;
}

/* ---- settings: what the gear opens ---- */
function openSettings() {
  set({ screen: 'settings', settingsFrom: S.screen === 'course' ? 'course' : 'overview', error: '' });
}

async function logout() {
  try {
    const state = await post('/api/logout');
    set({ error: '', screen: state.screen });
  } catch (exc) {
    set({ error: exc.message });
  }
}

function screenSettings() {
  const running = !!(S.sync && S.sync.running);
  const item = (icon, title, text, action, disabled) => `
    <button class="choice menu" onclick="${action}" ${disabled ? 'disabled' : ''}>
      <span class="menu-ic">${icon}</span>
      <span style="flex:1;display:flex;flex-direction:column;gap:4px">
        <span style="font-size:16px;font-weight:500">${title}</span>
        <span style="font-size:14px;color:var(--ink-2);line-height:1.45">${text}</span>
      </span>
      <span class="menu-go">${I.chevron}</span></button>`;
  return `<div class="onb"><div class="onb-card">
    <div style="display:flex;justify-content:space-between;align-items:center">${brand()}<span class="sub">Settings</span></div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <h1 style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.025em;line-height:1.1">Settings</h1>
    </div>
    <div style="display:flex;flex-direction:column;gap:10px">
      ${item(I.key, 'Change Anthropic API key',
        'Replace the key Modboard uses to read your course documents.',
        "set({screen:'apikey',apiKey:'',keyError:''})")}
      ${item(I.logout, 'Log out of Moodle',
        running ? 'Wait for the sync to finish first.'
          : 'Deletes your Moodle token from the Keychain. You sign in again on the next sync.',
        'logout()', running)}
    </div>
    ${S.error ? `<div class="alert">${esc(S.error)}</div>` : ''}
    <div style="display:flex;justify-content:flex-end">
      <button class="btn-ghost" onclick="set({screen:S.settingsFrom,error:''})">${I.back} Back</button>
    </div>
  </div></div>`;
}

/* After a logout, until the next sign-in. Signing in is a sync: its first
   stage opens the browser when no token is stored. */
function screenSignedOut() {
  return `<div class="onb"><div class="onb-card">
    <div style="display:flex;justify-content:space-between;align-items:center">${brand()}<span class="sub">Signed out</span></div>
    <div style="display:flex;flex-direction:column;gap:10px">
      <h1 style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.025em;line-height:1.1">You are logged out of Moodle</h1>
      <p style="margin:0;font-size:16px;color:var(--ink-2);line-height:1.5">Sign in again to see your courses. A browser window opens for the Moodle login, then Modboard syncs what changed.</p>
    </div>
    ${S.error ? `<div class="alert">${esc(S.error)}</div>` : ''}
    <button class="btn-primary" style="width:100%;min-height:58px;font-size:18px" onclick="set({syncMode:'changed'});startSync()">Sign in to Moodle ${I.arrow}</button>
  </div></div>`;
}

/* ---- the sync, as the backend reports it ---- */
async function startSync() {
  try {
    const body = await post('/api/sync', { full_rebuild: S.syncMode === 'full' });
    set({ sync: body.sync, error: '', screen: 'pulling' });
    poll();
  } catch (exc) {
    set({ error: exc.message, screen: S.screen === 'signedout' ? 'signedout' : courses().length ? 'overview' : 'start' });
  }
}

function poll() {
  clearTimeout(S.pollTimer);
  S.pollTimer = setTimeout(async () => {
    let sync;
    try { sync = await api('/api/sync'); } catch (exc) { set({ error: exc.message }); return; }
    if (!sync.finished) { set({ sync }); poll(); return; }
    // Finished: pick up whatever it wrote, successful or not.
    await loadDashboard();
    set({ sync, screen: sync.error ? 'pulling' : (courses().length ? 'overview' : 'start') });
  }, 1200);
}

function screenPulling() {
  const sync = S.sync || { percent: 0, stages: [] };
  const failed = !!sync.error;
  return `<div class="onb"><div class="onb-card">
    ${brand()}
    <div style="display:flex;flex-direction:column;gap:14px">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px">
        <h1 style="margin:0;font-size:30px;font-weight:500;letter-spacing:-0.02em">${failed ? 'Sync stopped' : 'Pulling your data'}</h1>
        <span style="font-size:40px;font-weight:500;letter-spacing:-0.03em">${sync.percent}%</span>
      </div>
      <div style="height:6px;border-radius:3px;background:var(--line);overflow:hidden"><div style="height:6px;width:${sync.percent}%;background:${failed ? '#B42318' : 'var(--ink-solid)'};transition:width .5s"></div></div>
    </div>
    <div>
      ${sync.stages.map((s) => {
    const ic = s.status === 'done' ? `<div class="job-ic" style="background:var(--ink-solid)">${I.check}</div>`
      : s.status === 'failed' ? `<div class="job-ic" style="background:#B42318">${I.cross}</div>`
        : s.status === 'running' ? `<div class="job-ic" style="border:2px solid var(--line)"><svg class="spin" width="26" height="26" viewBox="0 0 26 26" style="position:absolute"><circle cx="13" cy="13" r="12" fill="none" stroke="#161616" stroke-width="2" stroke-dasharray="20 80" stroke-linecap="round"/></svg></div>`
          : `<div class="job-ic" style="border:2px solid var(--line)"></div>`;
    const lit = s.status === 'done' || s.status === 'running' || s.status === 'failed';
    const detail = s.detail || (s.status === 'running' ? 'Running…' : s.status === 'skipped' ? 'Skipped' : '');
    return `<div class="job-row"><div style="position:relative">${ic}</div>
          <div style="flex:1;font-size:16px;color:${lit ? 'var(--ink)' : 'var(--muted)'}">${esc(s.label)}</div>
          <div style="font-size:13px;color:${s.status === 'failed' ? '#B42318' : 'var(--muted)'};text-align:right;max-width:55%">${esc(detail)}</div></div>`;
  }).join('')}
    </div>
    ${failed ? `<div class="alert">${esc(sync.error)}</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn-primary" onclick="startSync()">${I.sync} Try again</button>
        ${courses().length ? `<button class="btn-ghost" onclick="set({screen:'overview'})">Open the dashboard anyway</button>` : ''}
      </div>`
      : ''}
  </div></div>`;
}

/* ================= APP SHELL ================= */
function sidebar() {
  const sync = S.sync || {};
  const running = !!sync.running;
  const user = (S.data && S.data.user) || {};
  const term = (S.data && S.data.term) || {};
  return `<nav class="side" aria-label="Main">
    ${brand()}
    <div style="display:flex;flex-direction:column;gap:4px">
      <button class="nav-btn ${S.screen === 'overview' ? 'on' : ''}" style="font-size:17px" onclick="set({screen:'overview'})">${I.home}<span style="flex:1">Overview</span></button>
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <div style="font-size:14px;color:var(--muted);padding:0 14px 8px">Courses${term.label ? ' · ' + esc(term.label) : ''}</div>
      ${courses().map((c) => `<button class="nav-btn ${S.screen === 'course' && S.courseId === c.id ? 'on' : ''}" onclick="set({screen:'course',courseId:'${esc(c.id)}'})" title="${esc(c.name)}">
        <span class="chip">${esc(c.code)}</span><span class="nav-name" style="flex:1;font-size:15px;line-height:1.25">${esc(c.name)}</span><span class="badge">${stats(c).open.length}</span></button>`).join('')}
    </div>
    <div style="margin-top:auto;display:flex;flex-direction:column;gap:18px">
      ${S.error ? `<div class="alert" style="font-size:13px">${esc(S.error)}</div>` : ''}
      <button onclick="askSync()" ${running ? 'disabled' : ''} style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);border:0;background:transparent;padding:0;text-align:left;cursor:${running ? 'default' : 'pointer'}">
        <span style="width:8px;height:8px;border-radius:4px;background:${running ? '#B9873A' : '#3F8A3A'};flex:none"></span>
        <span style="flex:1">${running ? 'Syncing…' : 'Synced · ' + ago(S.data && S.data.synced_at)}</span>${I.sync}</button>
      <div style="display:flex;align-items:center;gap:12px">
        ${user.name ? `<div style="width:40px;height:40px;border-radius:20px;background:var(--wash);display:flex;align-items:center;justify-content:center;font-weight:600;flex:none">${esc(user.name[0])}</div>
          <div style="flex:1;min-width:0">
            <div class="one-line" style="font-size:16px" title="${esc(user.name)}">${esc(user.name)}</div>
            <div class="one-line" style="font-size:13px;color:var(--muted)" title="${esc(user.organisation || '')}">${esc(user.organisation || '')}</div>
          </div>`
      : `<div style="flex:1;font-size:13px;color:var(--muted)">${plural(courses().length, 'course')} in the database</div>`}
        <button aria-label="Settings" onclick="openSettings()" style="width:44px;height:44px;border:0;background:transparent;display:flex;align-items:center;justify-content:center">${I.gear}</button>
      </div>
    </div>
  </nav>`;
}

/* ---- grade composition (shared by the card and the course page) ---- */
function composition(c, big) {
  const h = big ? 64 : 34;
  const known = c.grading.filter((g) => g.weight !== null);
  const stated = known.reduce((s, g) => s + g.weight, 0);
  if (!known.length) {
    return `<div class="empty">No document states how much each part is worth.</div>`;
  }
  // Which labels a slice can actually show is decided by the container
  // queries in the stylesheet, from the slice's rendered width.
  const seg = (inner, track, flex, extra) =>
    `<div class="comp-seg ${big ? 'big' : 'sm'}" style="flex:${flex}" ${extra}>${inner}${track}</div>`;

  // A component worth 0% has no slice to draw. It still appears in the list
  // below the bar, where its 0% is the point rather than a gap.
  const parts = c.grading.filter((g) => g.weight !== 0).map((g) => {
    const unknown = g.weight === null;
    const block = `<div class="comp-block ${unknown ? 'unknown' : ''}" style="height:${h}px;${unknown ? '' : 'background:' + tvar(g.type) + ';'}font-size:${big ? 14 : 12}px;padding:${big ? '10px 11px' : '6px 7px'}">
        <span class="nm">${esc(g.name)}</span><span class="pct" style="font-size:${big ? 20 : 13}px">${unknown ? '?' : g.weight + '%'}</span>
      </div>`;
    const track = `<div class="comp-track"><div class="g" style="width:${g.graded_ratio * 100}%"></div><div class="s" style="width:${g.submitted_ratio * 100}%"></div></div>`;
    const title = `${g.name} · ${unknown ? 'weight not stated' : g.weight + '%'} · ${typeLabel(g.type)}`;
    return seg(block, track, unknown ? '0 0 46px' : `${g.weight} 1 0`, `title="${esc(title)}"`);
  });

  // The stated weights do not have to add up: a syllabus can name three of
  // four components. The remainder is drawn rather than hidden.
  if (stated < 100) {
    const block = `<div class="comp-block gap" style="height:${h}px;font-size:${big ? 14 : 12}px;padding:${big ? '10px 11px' : '6px 7px'}">
      <span class="nm">Not stated</span><span class="pct" style="font-size:${big ? 20 : 13}px">${100 - stated}%</span></div>`;
    parts.push(seg(block, '<div class="comp-track"></div>', `${100 - stated} 1 0`,
      'title="No document accounts for this share of the grade"'));
  }
  return `<div class="comp-bar">${parts.join('')}</div>`;
}

function typeLegend(c) {
  const seen = [...new Set(c.grading.map((g) => g.type))];
  return `<div style="display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--ink-2)">${seen.map((t) =>
    `<span style="display:flex;align-items:center;gap:6px"><span class="type-dot" style="background:${tvar(t)}"></span>${typeLabel(t)}</span>`).join('')}</div>`;
}

/* ================= OVERVIEW ================= */
function screenOverview() {
  const all = courses().map((c) => ({ c, st: stats(c) }));
  const open = all.flatMap(({ c, st }) => st.dated.map((i) => ({ ...i, courseName: c.name })))
    .sort((a, b) => ms(a.due) - ms(b.due));
  const dueWeek = open.filter((i) => daysTo(i.due) <= 7).length;
  const onsiteWeek = courses().flatMap((c) => c.onsite)
    .filter((o) => { const d = daysTo(o.date); return d !== null && d >= 0 && d <= 7; }).length;
  const measured = courses().filter((c) => c.handed_in_percent !== null);
  const progress = measured.length
    ? Math.round(measured.reduce((s, c) => s + c.handed_in_percent, 0) / measured.length) : null;
  const term = (S.data && S.data.term) || {};

  const buckets = [
    { title: 'Next 7 days', sub: 'Including overdue', test: (d) => d <= 7 },
    { title: 'In 2 weeks', sub: '8 – 14 days', test: (d) => d > 7 && d <= 14 },
    { title: 'This month', sub: '15 – 31 days', test: (d) => d > 14 && d <= 31 },
    { title: 'Later', sub: 'More than a month away', test: (d) => d > 31 }
  ];

  const card = (it) => {
    const d = daysTo(it.due), u = d === null ? { bg: 'var(--wash)', fg: 'var(--ink-2)' } : urgency(d);
    return `<article onclick="set({screen:'course',courseId:'${esc(it.course)}'})" style="cursor:pointer;border-radius:12px;padding:16px 18px;display:flex;flex-direction:column;gap:10px;border:1px solid var(--line-2);background:#fff">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <span class="chip">${esc(it.code)}</span>
        <span style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);min-width:0"><span class="type-dot" style="background:${tvar(it.type)}"></span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${typeLabel(it.type)}</span></span>
      </div>
      <div style="font-size:17px;font-weight:500;line-height:1.3">${esc(it.name)}</div>
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">
        <span style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink-2)">${fmt(it.due)} ${gradedFlag(it)}</span>
        ${d === null ? '' : `<span class="pill" style="background:${u.bg};color:${u.fg}">${daysLabel(d)}</span>`}
      </div>
    </article>`;
  };

  const column = (title, sub, list) => `<div style="display:flex;flex-direction:column;gap:14px">
    <div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;padding-bottom:10px;border-bottom:1px solid var(--line)">
      <div><div style="font-size:18px;font-weight:500">${title}</div><div class="sub">${sub}</div></div>
      <div style="font-size:28px;font-weight:500;letter-spacing:-0.02em">${list.length}</div>
    </div>
    ${list.length === 0 ? '<div class="empty">Nothing due</div>' : ''}
    ${list.slice(0, S.openBuckets[title] ? list.length : 3).map(card).join('')}
    ${list.length > 3 ? `<button class="more-btn" onclick="toggleBucket('${title}')" aria-expanded="${!!S.openBuckets[title]}">${S.openBuckets[title] ? 'Show less' : `+ ${list.length - 3} more`}</button>` : ''}
  </div>`;

  return `
  <section class="section" style="padding-top:40px;padding-bottom:36px;display:flex;flex-direction:column;gap:28px">
    <div style="display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px">
      <div>
        <div class="sub" style="font-size:14px">${fmt(today())}${term.week ? ' · Week ' + term.week + ' of ' + term.weeks : ''}</div>
        <h1 style="margin:6px 0 0;font-size:40px;font-weight:500;letter-spacing:-0.025em">${greeting()}</h1>
      </div>
      <button class="btn-ghost" onclick="askSync()">${I.sync} Sync</button>
    </div>
    <div class="stats">
      <div class="stat"><div class="num">${dueWeek}</div><div class="lbl">Due in the<br>next 7 days</div></div>
      <div class="stat"><div class="num">${onsiteWeek}</div><div class="lbl">On-site sessions<br>this week</div></div>
      <div class="stat"><div class="num">${progress === null ? blank : progress + '<small>%</small>'}</div><div class="lbl">Of all coursework<br>handed in</div></div>
    </div>
  </section>

  <section class="section" style="padding-top:0;display:flex;flex-direction:column;gap:22px">
    <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <h2 class="h2">Deadlines</h2>
      <div style="display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--ink-2)">
        ${[['> 2 weeks', 99], ['< 2 weeks', 13], ['< 1 week', 6], ['≤ 2 days', 0], ['Overdue', -1]].map(([l, d]) =>
    `<span style="display:flex;align-items:center;gap:6px"><span class="type-dot" style="background:${urgency(d).bg}"></span>${l}</span>`).join('')}
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px;align-items:start">
      ${buckets.map((b) => column(b.title, b.sub, open.filter((i) => b.test(daysTo(i.due))))).join('')}
    </div>
  </section>

  <section class="section" style="padding-top:8px;padding-bottom:110px;display:flex;flex-direction:column;gap:22px">
    <h2 class="h2">Courses</h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px">
      ${all.map(({ c }) => `<button class="card" onclick="set({screen:'course',courseId:'${esc(c.id)}'})" style="min-height:200px;padding:28px;display:flex;flex-direction:column;justify-content:space-between;gap:24px;text-align:left;font-size:inherit">
        <span class="chip" style="align-self:flex-start;font-size:13px;padding:6px 9px">${esc(c.id)}</span>
        <span style="width:100%;display:flex;align-items:flex-end;justify-content:space-between;gap:12px">
          <span style="font-size:28px;font-weight:500;letter-spacing:-0.02em;line-height:1.15">${esc(c.name)}</span>${I.arrow}
        </span>
      </button>`).join('')}
    </div>
  </section>`;
}

/* Greets by first name, which is what the sync stored. Before the first sync
   there is no name, and the greeting stops at the time of day. */
function greeting() {
  const hour = new Date().getHours();
  const user = (S.data && S.data.user) || {};
  const name = user.first_name || user.name || '';
  const part = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  return name ? `${part}, ${esc(name)}` : part;
}

/* ================= COURSE ================= */
function screenCourse() {
  const c = course(S.courseId);
  if (!c) return screenOverview();
  const st = stats(c);
  const next = st.nextDeadline;
  const lecturers = c.lecturers.length > 2
    ? `${esc(c.lecturers[0])} + ${c.lecturers.length - 1} more` : c.lecturers.map(esc).join(' · ');
  const meta = [lecturers, c.ects ? c.ects + ' ECTS' : null, c.term].filter(Boolean).join(' · ');

  const groups = [
    { label: 'Coming up', list: st.dated },
    { label: 'Handed in · awaiting grade', list: st.items.filter((i) => i.status === 'submitted').sort((a, b) => ms(b.due || '1970-01-01') - ms(a.due || '1970-01-01')) },
    { label: 'Marked as done', list: st.items.filter((i) => i.status === 'done').sort((a, b) => ms(b.due || '1970-01-01') - ms(a.due || '1970-01-01')), note: 'Moodle has no record of these.' },
    { label: 'Graded', list: st.items.filter((i) => i.status === 'graded').sort((a, b) => ms(b.due || '1970-01-01') - ms(a.due || '1970-01-01')) },
    { label: 'No date yet', list: st.undated, note: 'Moodle has no deadline for these.' }
  ];

  const row = (it, idx) => {
    const d = daysTo(it.due), isOpen = it.status === 'open';
    const u = d === null ? null : urgency(d);
    const when = fmt(it.due).split(' ');
    const key = descKey(it), shown = !!S.openDesc[key];
    const markable = isOpen || it.status === 'done';
    return `<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;padding:16px 20px;${idx ? 'border-top:1px solid var(--line);' : ''}background:${isOpen ? '#fff' : 'var(--wash-2)'}">
      ${markable ? `<button class="mark ${isOpen ? '' : 'on'}" onclick="markDone(${c.course_id}, '${markKey(it)}', ${isOpen})"
        title="${isOpen ? 'Mark as done' : 'Mark as not done'}" aria-label="${isOpen ? 'Mark as done' : 'Mark as not done'}" aria-pressed="${!isOpen}"><span>${I.tick}</span></button>` : ''}
      <div style="flex:0 0 86px">${it.due
        ? `<div class="sub">${when[0]}</div><div style="font-size:17px;font-weight:500">${when.slice(1).join(' ')}</div>`
        : `<div class="sub">—</div>`}</div>
      <div style="flex:1 1 200px;min-width:0;display:flex;flex-direction:column;gap:3px">
        <span style="font-size:16px;font-weight:500;color:${isOpen ? 'var(--ink)' : 'var(--ink-2)'}">${it.url
        ? `<a href="${esc(it.url)}" target="_blank" rel="noopener" style="text-decoration:none">${esc(it.name)}</a>` : esc(it.name)}</span>
        <span style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <span class="type-dot" style="background:${tvar(it.type)};width:8px;height:8px"></span>${typeLabel(it.type)} ${gradedFlag(it)} ${provenance(it)}</span>
        ${it.description
        ? `<button class="desc-btn" onclick="toggleDesc('${key}')">${shown ? 'Hide description' : 'Show description'}</button>` : ''}
      </div>
      ${isOpen ? (u ? `<span class="pill" style="background:${u.bg};color:${u.fg}">${daysLabel(d)}</span>` : '')
        : it.status === 'done' ? `<span class="pill" style="background:var(--line);color:#3A3A36;font-weight:500">Done</span>`
        : it.status === 'submitted' ? `<span class="pill" style="background:var(--line);color:#3A3A36;font-weight:500">Awaiting grade</span>`
          : it.points ? `<span style="display:flex;align-items:baseline;white-space:nowrap;line-height:1"><span style="font-size:34px;font-weight:500;letter-spacing:-0.03em">${it.points[0]}</span><span style="font-size:18px;color:var(--muted)">&thinsp;/&thinsp;${it.points[1]}</span></span>`
            : `<span class="pill" style="background:var(--line);color:#3A3A36;font-weight:500">Graded</span>`}
      ${it.description && shown ? `<div class="desc">${esc(it.description)}</div>` : ''}
    </div>`;
  };

  return `
  <section class="section" style="padding-top:28px;display:flex;flex-direction:column;gap:28px">
    <div style="display:flex;align-items:center;gap:8px;font-size:14px;color:var(--ink-2)">
      <button onclick="set({screen:'overview'})" style="border:0;background:transparent;padding:8px 0;font-size:14px;color:var(--ink-2);display:flex;align-items:center;gap:6px">${I.back} Overview</button>
      <span>/</span><span style="color:var(--ink)">${esc(c.id)}</span>
    </div>
    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:20px">
      <div style="display:flex;flex-direction:column;gap:8px">
        <h1 style="margin:0;font-size:40px;font-weight:500;letter-spacing:-0.025em">${esc(c.name)}</h1>
        <div style="font-size:16px;color:var(--ink-2)">${meta}</div>
      </div>
      ${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener" style="display:flex;align-items:center;gap:8px;min-height:44px;padding:0 16px;border-radius:10px;border:1px solid var(--line-3);text-decoration:none;font-size:15px">Open in Moodle ${I.ext}</a>` : ''}
    </div>
    <div class="stats">
      <div class="stat"><div class="num">${c.handed_in_percent === null ? blank : c.handed_in_percent + '<small>%</small>'}</div><div class="lbl">${c.handed_in_percent === null ? 'No weights stated<br>for this course' : 'Of the grade<br>handed in'}</div></div>
      <div class="stat"><div class="num">${next ? daysTo(next.due) + '<small> days</small>' : blank}</div><div class="lbl">${next ? 'Until ' + esc(next.name) : 'Nothing with<br>a deadline'}</div></div>
      <div class="stat"><div class="num">${st.upcoming.length}<small> of ${c.onsite.length}</small></div><div class="lbl">On-site sessions<br>still ahead</div></div>
    </div>
  </section>

  <section class="section" style="padding-top:8px;padding-bottom:110px;display:flex;flex-wrap:wrap;gap:36px;align-items:flex-start">
    <div style="flex:3 1 520px;min-width:0;display:flex;flex-direction:column;gap:36px">
      <!-- assignments -->
      <div style="display:flex;flex-direction:column;gap:18px">
        <h2 class="h2">Assignments &amp; exams</h2>
        ${groups.map((grp) => {
    if (!grp.list.length) {
      return grp.label === 'Coming up'
        ? `<div style="display:flex;flex-direction:column;gap:10px">
              <div style="display:flex;align-items:baseline;gap:8px;font-size:13px;font-weight:600;color:var(--ink-2)">Coming up<span style="font-weight:400;color:var(--muted)">0</span></div>
              <div class="empty">Nothing coming up</div></div>` : '';
    }
    return `<div style="display:flex;flex-direction:column;gap:10px">
            <div style="display:flex;align-items:baseline;gap:8px;font-size:13px;font-weight:600;color:var(--ink-2)">${grp.label}<span style="font-weight:400;color:var(--muted)">${grp.list.length}</span>
              ${grp.note ? `<span class="sub" style="font-weight:400">${grp.note}</span>` : ''}</div>
            <div class="card" style="overflow:hidden">${grp.list.map(row).join('')}</div>
          </div>`;
  }).join('')}
      </div>

      <!-- attendance -->
      <div style="display:flex;flex-direction:column;gap:18px">
        <h2 class="h2">Mandatory attendance</h2>
        ${c.onsite.length === 0
      ? `<div class="empty">No document names a session you have to attend in person.</div>`
      : `<div class="card" style="padding:22px;display:flex;flex-direction:column;gap:18px">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:12px">
            <div><div style="font-size:44px;font-weight:500;letter-spacing:-0.035em;line-height:1">${c.onsite.length}</div><div class="sub" style="margin-top:6px">sessions you have to be on site</div></div>
            <div class="att-dots" style="max-width:140px;justify-content:flex-end">${c.onsite.map((o) =>
        `<span class="att-dot ${daysTo(o.date) < 0 ? 'past' : o === st.upcoming[0] ? 'next' : ''}" title="${fmt(o.date)}"></span>`).join('')}</div>
          </div>
          <div>
            ${c.onsite.map((o) => {
          const d = daysTo(o.date), past = d !== null && d < 0, nxt = o === st.upcoming[0];
          const date = o.date ? new Date(ms(o.date)) : null;
          return `<div style="display:flex;gap:14px;padding:14px 0;border-top:1px solid var(--line);opacity:${past ? 0.5 : 1}">
                <div style="flex:0 0 52px;text-align:center;border-radius:10px;padding:6px 0;background:${nxt ? '#FEC84B' : past ? 'transparent' : 'var(--wash)'};${past ? 'border:1px solid var(--line-2)' : ''}">
                  <div class="flag" style="color:${nxt ? '#4E2D00' : 'var(--muted)'}">${date ? MON[date.getUTCMonth()] : '—'}</div>
                  <div style="font-size:22px;font-weight:500;line-height:1.1">${date ? date.getUTCDate() : '?'}</div>
                </div>
                <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:4px">
                  <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline;flex-wrap:wrap">
                    <span style="font-size:16px;font-weight:500">${esc(o.name)}</span>
                    <span style="display:flex;gap:6px;align-items:center">${provenance(o)}
                      ${nxt ? `<span class="pill" style="background:${urgency(d).bg};color:${urgency(d).fg};font-size:12px;padding:4px 8px">${daysLabel(d)}</span>` : past ? '<span class="sub">Past</span>' : ''}</span>
                  </div>
                  <span style="font-size:13px;color:var(--ink-2)">${date ? WD[date.getUTCDay()] : 'Date unknown'}${o.time ? ' · ' + esc(o.time) : ''}${o.description ? ' · ' + esc(o.description) : ''}</span>
                </div>
              </div>`;
        }).join('')}
          </div>
        </div>`}
      </div>
    </div>

    <aside style="flex:2 1 320px;min-width:0;display:flex;flex-direction:column;gap:36px">
      <!-- grade composition -->
      <div style="display:flex;flex-direction:column;gap:18px">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap">
          <h2 class="h2">How your final grade is built</h2>
        </div>
        <div class="card" style="padding:24px;display:flex;flex-direction:column;gap:22px">
          ${c.grading.length === 0 ? '<div class="empty">No grading scheme was found in this course\'s documents.</div>' : `
          ${composition(c, true)}
          <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px">
            ${typeLegend(c)}
            <div style="display:flex;gap:14px;font-size:12px;color:var(--ink-2)">
              <span style="display:flex;align-items:center;gap:6px"><span style="width:14px;height:4px;border-radius:2px;background:var(--ink-solid)"></span>Graded</span>
              <span style="display:flex;align-items:center;gap:6px"><span style="width:14px;height:4px;border-radius:2px;background:repeating-linear-gradient(-45deg,#161616 0 1.5px,#fff 1.5px 4px);outline:1px solid var(--line-3)"></span>Submitted</span>
              <span style="display:flex;align-items:center;gap:6px"><span style="width:14px;height:4px;border-radius:2px;background:var(--line)"></span>Open</span>
            </div>
          </div>
          <div>
            ${c.grading.map((g) => `<div style="display:grid;grid-template-columns:auto 1fr auto;gap:4px 16px;align-items:center;padding:16px 0;border-top:1px solid var(--line)">
                <span style="width:6px;min-height:40px;border-radius:3px;background:${tvar(g.type)};grid-row:span 2;align-self:stretch"></span>
                <span style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><span style="font-size:17px;font-weight:500">${esc(g.name)}</span>
                  <span class="flag" style="color:var(--muted)">${typeLabel(g.type)}</span>
                  ${provenance(g)}</span>
                <span style="font-size:26px;font-weight:500;letter-spacing:-0.02em;grid-row:span 2">${g.weight === null ? '<span style="font-size:18px;color:var(--muted)">n/a</span>' : g.weight + '%'}</span>
                <span style="font-size:14px;color:var(--ink-2)">${esc(g.rule)}${g.items_total ? ` · ${g.items_done} of ${g.items_total} done` : ' · no Moodle activity matched'}</span>
              </div>`).join('')}
          </div>
          ${c.pass_rule ? `<div style="padding:14px 16px;border-radius:10px;background:var(--wash);font-size:14px;line-height:1.5"><b>To pass:</b> ${esc(c.pass_rule)}</div>` : ''}
          ${c.unmatched.length ? `<div style="padding:14px 16px;border-radius:10px;background:var(--wash);font-size:14px;line-height:1.5">
            <b>${c.unmatched.length} Moodle ${c.unmatched.length === 1 ? 'activity' : 'activities'}</b> could not be tied to any component above, so none of them counts toward the bar.</div>` : ''}
        `}
        </div>
      </div>
    </aside>
  </section>`;
}

/* ================= render ================= */
function screenLoading() {
  return `<div class="onb"><div class="onb-card" style="align-items:center;gap:20px">
    ${brand()}
    <svg class="spin" width="28" height="28" viewBox="0 0 26 26"><circle cx="13" cy="13" r="12" fill="none" stroke="#161616" stroke-width="2" stroke-dasharray="20 80" stroke-linecap="round"/></svg>
    <div class="sub">${S.error ? esc(S.error) : 'Reading the database…'}</div>
  </div></div>`;
}

function render() {
  const sc = S.screen;
  let html;
  if (sc === 'loading') html = screenLoading();
  else if (sc === 'apikey') html = screenApiKey();
  else if (sc === 'start') html = screenStart();
  else if (sc === 'syncmode') html = screenSyncMode();
  else if (sc === 'settings') html = screenSettings();
  else if (sc === 'signedout') html = screenSignedOut();
  else if (sc === 'pulling') html = screenPulling();
  else html = `<div class="shell">${sidebar()}<main>${sc === 'course' ? screenCourse() : screenOverview()}</main></div>`;

  const focused = document.activeElement && document.activeElement.id;
  document.getElementById('app').innerHTML = html;
  if (focused === 'key') {
    const field = document.getElementById('key');
    if (field) { field.focus(); field.setSelectionRange(field.value.length, field.value.length); }
  }
}

async function loadDashboard() {
  const payload = await api('/api/dashboard');
  S.data = payload;
  S.sync = payload.state.sync;
  if (!S.courseId && payload.courses.length) S.courseId = payload.courses[0].id;
  return payload;
}

async function boot() {
  try {
    const payload = await loadDashboard();
    const screen = payload.state.sync.running ? 'pulling' : payload.state.screen;
    set({ screen });
    if (payload.state.sync.running) poll();
  } catch (exc) {
    set({ error: exc.message });
  }
}
boot();
