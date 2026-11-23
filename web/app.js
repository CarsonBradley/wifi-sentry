/* wifi-sentry dashboard. Plain JS, no dependencies, no external requests. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const STALE_MS = 5000;        // no tick for this long: stop trusting the display
  const RECONNECT_MS = 10000;   // no tick for this long: tear down and reconnect
  const BACKOFF = [1, 2, 3, 5];   // seconds; capped low because a stale security page is worse than a few extra requests
  const PAGE = 50;
  const LEVEL_NAME = { low: 'Low', medium: 'Medium', high: 'High' };
  const NB = ' ';

  // ---------- storage (never throws) ----------
  const store = {
    get(k, d) {
      try { const v = localStorage.getItem('wifi-sentry.' + k); return v == null ? d : JSON.parse(v); }
      catch (e) { return d; }
    },
    set(k, v) { try { localStorage.setItem('wifi-sentry.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };

  // ---------- state ----------
  const S = {
    gen: 0,
    es: null,
    esOpen: false,
    conn: 'connecting',     // connecting | live | stale | down | locked
    locked: false,          // the engine wants the access token (401): show only the login
    loginMsg: null,
    failures: 0,
    retryAt: 0,
    retryTimer: null,
    everLive: false,
    statusFresh: false,
    status: null,
    statusAt: 0,            // client ms when status was received (for uptime)
    lastTickAt: 0,          // client performance.now() of last tick
    lastDataAt: 0,          // server time of last tick
    skew: null,             // server - client seconds
    events: new Map(),
    tailId: null,
    moreAvailable: false,
    eventsLoaded: false,
    range: [900, 3600, 21600, 86400].includes(store.get('range', 3600)) ? store.get('range', 3600) : 3600,
    walk: null,
    walkShowResult: false,
    chan: null,             // GET api/alerts (channels, options); null = not loaded / older engine
    arming: null,           // GET api/arming (exit delay, schedule)
    schedDraft: null,
    onboarding: null,       // GET api/onboarding
    summaryFrom: store.get('lastSeen', null),
    digestDismissed: false,
    digestGaps: [],
    hiddenAt: null,
    hiddenMotion: null,
    alertedIds: new Set(),
    lastAlertAt: 0,
    learnSamples: [],
    alerts: { sound: !!store.get('sound', false), vib: !!store.get('vib', false) },
  };

  // ---------- time + formatting ----------
  const serverNow = () => Date.now() / 1000 + (S.skew || 0);
  const fClock = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
  const fClockSec = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  const fHour = new Intl.DateTimeFormat(undefined, { hour: 'numeric' });
  const fWeekday = new Intl.DateTimeFormat(undefined, { weekday: 'short' });
  const fDay = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
  const clock = (t) => fClock.format(new Date(t * 1000));

  function dur(s, coarse) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + NB + 's';
    const m = Math.floor(s / 60);
    if (m < 60) return (m < 10 && !coarse && s % 60) ? m + NB + 'min ' + (s % 60) + NB + 's' : m + NB + 'min';
    const h = Math.floor(m / 60);
    if (h < 24) return (m % 60) ? h + NB + 'h ' + (m % 60) + NB + 'min' : h + NB + 'h';
    const d = Math.floor(h / 24);
    return (h % 24) ? d + NB + 'd ' + (h % 24) + NB + 'h' : d + NB + 'd';
  }
  const ago = (t) => { const d = serverNow() - t; return d < 5 ? 'just now' : dur(d, true) + ' ago'; };

  function dayKey(t) { const d = new Date(t * 1000); return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(); }
  function dayLabel(t) {
    const k = dayKey(t), n = serverNow();
    if (k === dayKey(n)) return 'Today';
    if (k === dayKey(n - 86400)) return 'Yesterday';
    return fDay.format(new Date(t * 1000));
  }
  function whenLabel(t) {
    const k = dayKey(t), n = serverNow();
    if (k === dayKey(n)) return 'at ' + clock(t);
    if (k === dayKey(n - 86400)) return 'yesterday at ' + clock(t);
    if (n - t < 6 * 86400) return fWeekday.format(new Date(t * 1000)) + ' at ' + clock(t);
    return fDay.format(new Date(t * 1000)) + ', ' + clock(t);
  }
  const num = (v, d) => (typeof v === 'number' && isFinite(v)) ? v.toFixed(d == null ? 2 : d) : '–';
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));

  // ---------- network ----------
  async function api(path, body, method, timeoutMs) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs || 8000);
    try {
      const opts = { cache: 'no-store', signal: ctl.signal };
      if (body !== undefined || method) {
        opts.method = method || 'POST';
        opts.headers = { 'Content-Type': 'application/json' };
        opts.body = JSON.stringify(body === undefined ? {} : body);
      }
      const r = await fetch('api/' + path, opts);
      let data = null;
      try { data = await r.json(); } catch (e) { /* non-JSON */ }
      if (!r.ok) {
        const err = new Error((data && (data.error || data.detail)) || ('HTTP ' + r.status));
        err.status = r.status;
        if (r.status === 401 && path !== 'login') lock();
        throw err;
      }
      return data;
    } finally { clearTimeout(timer); }
  }

  function parse(e) { try { return JSON.parse(e.data); } catch (err) { return null; } }

  function connect() {
    clearTimeout(S.retryTimer); S.retryTimer = null;
    if (S.es) { S.es.close(); S.es = null; }
    const gen = ++S.gen;
    S.esOpen = false;
    S.statusFresh = false;
    setConn('connecting');
    // Ask first whether a token is needed: an EventSource can't tell a 401 from a network error.
    fetch('api/auth', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then((a) => {
        if (gen !== S.gen) return;
        if (a && a.required && !a.authenticated) { lock(); return; }
        S.locked = false;
        openStream(gen);
      })
      .catch(() => { if (gen === S.gen) fail(gen); });
  }

  // Locked: forget everything shown so far, so nothing stale (an old "All clear") can be read as current.
  function lock(msg) {
    S.gen++;
    if (S.es) { S.es.close(); S.es = null; }
    clearTimeout(S.retryTimer); S.retryTimer = null;
    const was = S.locked;
    S.locked = true;
    S.esOpen = false; S.statusFresh = false;
    S.status = null;
    S.events.clear(); S.eventsLoaded = false; S.tailId = null;
    S.walk = null; S.digestGaps = [];
    S.chan = null; S.arming = null; S.schedDraft = null; S.onboarding = null;
    if (msg) S.loginMsg = msg;
    setConn('locked');
    render();
    if (!was) { const i = $('tokenInput'); if (i && !('ontouchstart' in window)) i.focus(); }
  }

  async function login(e) {
    e.preventDefault();
    const input = $('tokenInput'), btn = $('loginBtn');
    const tok = input.value.trim();
    if (!tok) return;
    btn.disabled = true;
    try {
      await api('login', { token: tok });
      input.value = '';
      S.loginMsg = null;
      S.locked = false;
      S.failures = 0;
      connect();
    } catch (err) {
      S.loginMsg = err.status === 401 ? 'That token isn’t right. Check it and try again.'
        : err.status === 429 ? 'Too many wrong tokens from this device. ' + err.message + '.'
          : 'Couldn’t reach wifi-sentry (' + err.message + ').';
      renderLogin();
    } finally { btn.disabled = false; }
  }

  function renderLogin() {
    const box = $('loginBox');
    box.hidden = !S.locked;
    if (!S.locked || !S.loginMsg) return;
    const m = $('loginMsg');
    if (m.textContent !== S.loginMsg) m.textContent = S.loginMsg;
    m.classList.add('err');
  }

  function openStream(gen) {
    let es;
    try { es = new EventSource('api/stream'); } catch (e) { fail(gen); return; }
    S.es = es;
    es.addEventListener('open', () => {
      if (gen !== S.gen) return;
      S.esOpen = true;
      S.lastTickAt = performance.now();
      resync(gen);
    });
    es.addEventListener('error', () => { if (gen === S.gen) fail(gen); });
    es.addEventListener('tick', (e) => { if (gen === S.gen) { const d = parse(e); if (d) onTick(d); } });
    es.addEventListener('status', (e) => { if (gen === S.gen) { const d = parse(e); if (d) { applyStatus(d); S.statusFresh = true; markLive(); render(); } } });
    es.addEventListener('motion', (e) => { if (gen === S.gen) { const d = parse(e); if (d) onMotion(d); } });
    // sent when a walk test finishes, including the server-side auto-stop
    es.addEventListener('walktest', (e) => { if (gen === S.gen) { const d = parse(e); if (d) applyWalk(d, false); } });
    // an alert was delivered or failed, or the channels changed (maybe in another tab)
    es.addEventListener('alerts', (e) => { if (gen === S.gen) { const d = parse(e); if (d) onAlertsEvent(d); } });
  }

  function fail(gen) {
    if (gen !== S.gen) return;
    if (S.es) { S.es.close(); S.es = null; }
    S.esOpen = false;
    S.statusFresh = false;
    S.failures++;
    const base = BACKOFF[Math.min(S.failures - 1, BACKOFF.length - 1)];
    const delay = base * 1000 * (0.8 + Math.random() * 0.4);
    S.retryAt = performance.now() + delay;
    clearTimeout(S.retryTimer);
    S.retryTimer = setTimeout(connect, delay);
    setConn('down');
    render();
  }

  async function resync(gen) {
    try {
      const st = await api('status');
      if (gen !== S.gen) return;
      applyStatus(st);
      S.statusFresh = true;
      markLive();
      render();
    } catch (e) {
      if (gen === S.gen) fail(gen);
      return;
    }
    api('walktest').then((w) => { if (gen === S.gen && w) applyWalk(w, false); }).catch(() => {});
    loadHistory();
    loadDay();
    loadEvents(false);
    loadDigestGaps();
    loadAlerts();
    loadArming();
    loadOnboarding();
  }

  function markLive() {
    if (!S.esOpen || !S.statusFresh) return;
    if (S.conn !== 'live') {
      S.failures = 0;
      S.everLive = true;
      setConn('live');
    }
  }

  function isLive() {
    return S.conn === 'live' && S.esOpen && S.statusFresh && S.status && performance.now() - S.lastTickAt < STALE_MS;
  }

  function setConn(c) {
    S.conn = c;
    const el = $('conn');
    el.dataset.conn = c;
    $('connText').textContent = { live: 'Live', connecting: S.everLive ? 'Reconnecting' : 'Connecting', stale: 'No updates', down: 'Disconnected', locked: 'Locked' }[c];
    document.body.classList.toggle('offline-ui', c !== 'live');
    document.body.classList.toggle('locked', c === 'locked');
  }

  // ---------- incoming data ----------
  function applyStatus(st) {
    const prev = S.status;
    S.status = Object.assign({ sources: [] }, st);
    if (!Array.isArray(S.status.sources)) S.status.sources = [];
    S.statusAt = Date.now();
    if (S.status.state === 'learning') {
      const p = Number(S.status.learning_progress) || 0;
      S.learnSamples.push([serverNow(), p]);
      if (S.learnSamples.length > 30) S.learnSamples.shift();
    } else S.learnSamples = [];
    if (prev && prev.state !== S.status.state) { announce(); historySoon(); }
    // a walk test started or stopped elsewhere (another tab, or the server's auto-stop)
    if (typeof S.status.walktest_running === 'boolean' && S.walk && S.status.walktest_running !== !!S.walk.running) {
      api('walktest').then((w) => applyWalk(w, false)).catch(() => {});
    }
  }

  // Coverage gaps (learning / no sensor / engine down) open and close on state changes:
  // refetch the chart so its "not watching" bands match what the engine recorded.
  let historyTimer = null;
  function historySoon() {
    clearTimeout(historyTimer);
    historyTimer = setTimeout(() => { loadHistory(); loadDay(); loadDigestGaps(); }, 1500);
  }

  function onTick(d) {
    S.lastTickAt = performance.now();
    if (typeof d.t === 'number') {
      const sk = d.t - Date.now() / 1000;
      S.skew = S.skew == null || Math.abs(sk - S.skew) > 5 ? sk : S.skew * 0.9 + sk * 0.1;
      S.lastDataAt = d.t;
    }
    const st = S.status;
    if (st) {
      if (d.state && d.state !== st.state) {
        const was = st.state;
        st.state = d.state;
        st.since = d.t;
        if (d.state === 'motion' && was !== 'motion' && st.armed) maybeAlert(null);
        announce();
      }
      if (typeof d.score === 'number') st.score = d.score;
      if (typeof d.threshold === 'number') st.threshold = d.threshold;
      if (d.sources && typeof d.sources === 'object') {
        for (const src of st.sources) if (src.id in d.sources) src.score = d.sources[src.id];
      }
    }
    if (S.conn === 'stale') setConn('live');
    markLive();
    // While no sensor is online the engine still ticks with score 0; that is "no data", not "quiet".
    const sc = typeof d.score === 'number' && d.state !== 'offline' ? d.score : null;
    chart.push(d.t, sc, d.threshold);
    day.push(d.t, sc, d.threshold);
    if (S.walk && S.walk.running && S.walk.live && typeof d.score === 'number') {
      S.walk.live.peak_score = Math.max(Number(S.walk.live.peak_score) || 0, d.score);
    }
    renderHero();
    renderSensorMeters(d.sources || {});
    renderWalkLive();
  }

  function onMotion(ev) {
    if (!ev || ev.id == null) return;
    const isNew = !S.events.has(ev.id);
    upsertEvent(ev);
    if (isNew && ev.end == null && ev.armed) maybeAlert(ev);
    if (document.hidden && ev.end == null) S.hiddenMotion = ev.start;
    renderEvents();
    renderDigest();
    renderTitle();
  }

  function upsertEvent(ev) {
    const prev = S.events.get(ev.id);
    S.events.set(ev.id, Object.assign({}, prev || {}, ev));
  }

  async function loadEvents(more) {
    const q = 'events?limit=' + PAGE + (more && S.tailId != null ? '&before=' + encodeURIComponent(S.tailId) : '');
    const btn = $('moreBtn');
    if (more) { btn.disabled = true; btn.textContent = 'Loading…'; }
    try {
      const r = await api(q);
      const evs = (r && Array.isArray(r.events)) ? r.events : [];
      evs.forEach(upsertEvent);
      if (more || !S.eventsLoaded) {
        if (evs.length) S.tailId = evs[evs.length - 1].id;
        S.moreAvailable = evs.length >= PAGE;
      }
      S.eventsLoaded = true;
      renderEvents();
      renderDigest();
    } catch (e) {
      if (more) toast('Couldn’t load older events. ' + e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Show older events';
    }
  }

  async function loadHistory() {
    const range = S.range;
    try {
      const h = await api('history?seconds=' + range + '&points=600');
      if (range !== S.range) return;
      chart.setRange(range);
      chart.setData(h);
      $('chartEmpty').hidden = !chart.isEmpty();
    } catch (e) { /* stays on what we have; reconnect will retry */ }
  }

  // The 24-hour chart has its own fixed range (2-minute buckets), independent of the strip chart's.
  let dayLoadedAt = 0;
  async function loadDay() {
    dayLoadedAt = Date.now();
    try {
      day.setData(await api('history?seconds=86400&points=720'));
      renderDaySummary();
    } catch (e) { /* keep what we have */ }
  }

  // Coverage gaps since the user last looked, for the "since you last checked" summary.
  async function loadDigestGaps() {
    const from = S.summaryFrom;
    if (from == null) { S.digestGaps = []; return; }
    try {
      const r = await api('gaps?since=' + encodeURIComponent(from));
      if (from !== S.summaryFrom) return;
      S.digestGaps = (r && Array.isArray(r.gaps)) ? r.gaps : [];
      renderDigest();
    } catch (e) { /* keep the previous answer */ }
  }

  const GAP_REASON = { engine_down: 'wifi-sentry wasn’t running', source_offline: 'no usable sensor signal', learning: 'learning the baseline' };

  // Merge gaps that touch (an engine restart is "down" then a few seconds of warm-up) and drop blips.
  function coverageGaps(gaps, from, minS) {
    const now = serverNow();
    const gs = (gaps || []).map((g) => ({ start: Math.max(g.start, from), end: g.ongoing ? now : g.end, ongoing: !!g.ongoing, reasons: [g.reason] }))
      .filter((g) => typeof g.start === 'number' && typeof g.end === 'number' && g.end > g.start)
      .sort((a, b) => a.start - b.start);
    const out = [];
    for (const g of gs) {
      const last = out[out.length - 1];
      if (last && g.start - last.end <= 15) {
        last.end = Math.max(last.end, g.end); last.ongoing = last.ongoing || g.ongoing;
        for (const r of g.reasons) if (!last.reasons.includes(r)) last.reasons.push(r);
      } else out.push(g);
    }
    return out.filter((g) => g.ongoing || g.reasons.includes('engine_down') || g.end - g.start >= minS);
  }

  // ---------- render: hero ----------
  function heroModel() {
    if (S.locked) return { k: 'locked', title: 'Enter access token', sub: 'This dashboard is protected. Motion status stays hidden until you unlock it.' };
    if (!isLive()) {
      if (!S.everLive && S.failures === 0) return { k: 'connecting', title: 'Connecting', sub: 'Reaching wifi-sentry…' };
      return { k: 'disconnected', title: S.conn === 'stale' ? 'Updates stopped' : 'Not connected', sub: 'Motion status unknown.' };
    }
    const st = S.status;
    const since = typeof st.since === 'number' ? st.since : null;
    const srcs = st.sources || [];
    const off = srcs.filter((s) => s.online === false);
    // online but unusable (e.g. per-antenna RSSI gone after a macOS update, or frozen)
    const blind = srcs.filter((s) => s.online !== false && s.health);
    switch (st.state) {
      case 'clear': {
        // The engine flips to clear end_timeout after the last movement; the event's end is the real "last motion".
        let quietFrom = since;
        const lastEv = sortedEvents()[0];
        if (quietFrom && lastEv && lastEv.end != null && lastEv.end < quietFrom && quietFrom - lastEv.end < 120) quietFrom = lastEv.end;
        const sub = quietFrom ? 'No motion for ' + dur(serverNow() - quietFrom, true) : 'No motion right now';
        // Partial coverage is set apart (ochre, in the sub line) so it is never read as full coverage.
        const warn = [];
        if (off.length) warn.push(off.length + ' of ' + srcs.length + ' sensors offline');
        if (blind.length) warn.push(plural(blind.length, 'sensor') + ' not usable');
        if (st.sensing === 'degraded' || st.sensing === 'reduced') warn.push(st.sensing + ' sensing');
        return { k: 'clear', title: 'All clear', sub, warn: warn.join(' · ') };
      }
      case 'motion': {
        let sub = since ? 'For ' + dur(serverNow() - since) : 'Happening now';
        if (!st.armed) sub += ' · disarmed, logged quietly';
        return { k: 'motion', title: 'Motion now', sub };
      }
      case 'learning': {
        let sub = 'Keep the room still. Motion isn’t detected until this finishes.';
        if (blind.length) sub += ' ' + sensorName(blind[0]).replace(/^t/, 'T') + ': ' + String(blind[0].health) + '.';
        return { k: 'learning', title: 'Learning your home', sub };
      }
      case 'offline': {
        if (!off.length && blind.length) {
          const b = blind[0];
          return { k: 'offline', title: 'Can’t sense', sub: sensorName(b).replace(/^t/, 'T') + ': ' + String(b.health) + '. Motion can’t be detected.' };
        }
        const who = off.length === 1 ? sensorName(off[0]) : 'any sensor';
        const last = off.length ? Math.max.apply(null, off.map((s) => s.last_seen || 0)) : 0;
        let sub = 'No data from ' + who + (last ? ' since ' + clock(last) : since ? ' for ' + dur(serverNow() - since, true) : '') + '. Motion can’t be detected.';
        return { k: 'offline', title: 'Sensor offline', sub };
      }
      default:
        return { k: 'unknown', title: 'Unknown state', sub: 'The engine reported “' + String(st.state) + '”.' };
    }
  }

  let lastAnnounced = '';
  function announce() {
    const m = heroModel();
    if (m.title !== lastAnnounced) { lastAnnounced = m.title; $('srLive').textContent = m.title; }
  }

  function renderHero() {
    const m = heroModel();
    const hero = $('hero');
    hero.dataset.state = m.k;
    $('heroTitle').textContent = m.title;
    const subHtml = esc(m.sub) + (m.warn ? ' <span class="sub-warn">' + esc(m.warn) + '</span>' : '');
    if ($('heroSub').innerHTML !== subHtml) $('heroSub').innerHTML = subHtml;
    const st = S.status;
    const live = m.k !== 'connecting' && m.k !== 'disconnected' && m.k !== 'locked';
    renderLogin();

    // learning progress
    const learning = live && st.state === 'learning';
    $('learnBox').hidden = !learning;
    if (learning) {
      const p = Math.max(0, Math.min(1, Number(st.learning_progress) || 0));
      $('learnBar').style.width = (p * 100).toFixed(1) + '%';
      let left = null;
      if (typeof st.learning_eta_s === 'number' && isFinite(st.learning_eta_s)) {
        left = st.learning_eta_s - (Date.now() - S.statusAt) / 1000;   // engine's own estimate, counted down
      } else {
        const ls = S.learnSamples;
        if (ls.length >= 2 && p > 0.02) {
          const [t0, p0] = ls[0], [t1, p1] = ls[ls.length - 1];
          if (p1 > p0 && t1 > t0) left = (1 - p) * (t1 - t0) / (p1 - p0);
        }
      }
      const eta = left == null ? '' : left > 5 ? ' · about ' + dur(left, true) + ' left' : ' · almost done';
      $('learnText').textContent = Math.round(p * 100) + '% done' + eta;
    }

    // the day chart's pen and the readout in its hub
    const watching = live && (st.state === 'clear' || st.state === 'motion');
    const thr = st ? Number(st.threshold) || 1 : 1, sc = st ? Number(st.score) || 0 : 0;
    day.setLive(watching ? { score: sc, thr } : null);
    let hv = '–', hc = '';
    if (watching) { hv = num(sc); hc = 'activity\ntrigger ' + num(thr, 1); }
    else if (live && st.state === 'learning') { hv = Math.round(Math.max(0, Math.min(1, Number(st.learning_progress) || 0)) * 100) + '%'; hc = 'learned'; }
    else if (live && st.state === 'offline') hc = 'no signal';
    else if (m.k === 'disconnected') hc = 'no data';
    if ($('hubVal').textContent !== hv) $('hubVal').textContent = hv;
    if ($('hubCap').textContent !== hc) $('hubCap').textContent = hc;
    $('dial').dataset.hot = String(watching && sc > thr);
    $('dial').classList.toggle('stale', !live && (S.everLive || S.failures > 0));

    // disconnected
    const disc = m.k === 'disconnected';
    $('discBox').hidden = !disc;
    if (disc) {
      const parts = [];
      parts.push(S.conn === 'stale' ? 'The connection is open but no readings are arriving.' : 'Can’t reach wifi-sentry.');
      if (S.lastDataAt) parts.push('Last reading ' + ago(S.lastDataAt) + ' (' + clock(S.lastDataAt) + ').');
      if (S.conn === 'down' && S.retryTimer) {
        const s = Math.max(0, Math.ceil((S.retryAt - performance.now()) / 1000));
        parts.push(s > 0 ? 'Retrying in ' + s + NB + 's.' : 'Retrying…');
      } else if (S.conn === 'connecting') parts.push('Retrying…');
      $('discText').textContent = parts.join(' ');
    }

    // arm switch (on = armed or counting down to armed)
    const arm = $('armSwitch');
    const am = armModel(st);
    arm.disabled = !live || arm.dataset.busy === '1';
    if (arm.dataset.busy !== '1') arm.setAttribute('aria-checked', String(!!(st && am.on)));
    arm.classList.toggle('pending', !!am.delay);
    $('armLabel').textContent = st ? am.label : 'Armed';
    $('armHint').textContent = !live ? (st ? 'Last known setting' : ' ') : (am.hint.startsWith('Motion shows here only') && !$('alertsPrompt').hidden ? 'Motion shows here only' : am.hint);
    renderTitle();
    announce();   // connection changes too: the live region must never keep saying "All clear"
  }

  // ---------- render: title ----------
  function renderTitle() {
    let t = 'wifi-sentry';
    const live = isLive();
    if (S.locked) t = 'Locked · wifi-sentry';
    else if (!live && (S.everLive || S.failures)) t = 'Disconnected · wifi-sentry';
    else if (live && S.status.state === 'motion') t = 'Motion now · wifi-sentry';
    else if (S.hiddenMotion) t = 'Motion at ' + clock(S.hiddenMotion) + ' · wifi-sentry';
    else if (live && S.status.state === 'offline') t = 'Sensor offline · wifi-sentry';
    else if (live && S.status.state === 'learning') t = 'Learning · wifi-sentry';
    if (document.title !== t) document.title = t;
  }

  // ---------- render: sensors ----------
  function sensorName(s) {
    if (s.kind === 'mac') return 'this Mac’s Wi-Fi';
    return srcName(s.id);
  }
  // engine ids: "mac", "esp32:<node>"
  const srcName = (id) => id === 'mac' ? 'Mac' : String(id).replace(/^esp32:/, '');
  function rssiBars(r) {
    if (typeof r !== 'number') return 0;
    return r >= -55 ? 4 : r >= -65 ? 3 : r >= -75 ? 2 : 1;
  }

  function renderSensors() {
    const ul = $('sensorList');
    const st = S.status;
    const live = isLive();
    const srcs = st ? st.sources.slice().sort((a, b) => (a.kind === 'mac' ? 0 : 1) - (b.kind === 'mac' ? 0 : 1) || String(a.id).localeCompare(String(b.id))) : [];
    ul.classList.toggle('stale', !live);
    if (!st) { ul.innerHTML = '<li class="fine">Waiting for the engine…</li>'; return; }
    if (!srcs.length) { ul.innerHTML = '<li class="fine">No sensors have reported yet.</li>'; return; }
    const html = srcs.map((s) => {
      const on = s.online !== false;
      const blind = on && !!s.health;
      // per-source learning only matters once the system as a whole is past it (e.g. a new ESP32 board)
      const learningHere = on && !blind && s.learning === true && st.state !== 'learning';
      const kind = s.kind === 'mac' ? 'This Mac’s Wi-Fi signal' : s.kind === 'esp32' ? 'ESP32 sensor' : (s.kind || 'Sensor');
      const name = s.kind === 'mac' ? 'This Mac' : srcName(s.id);
      const word = !on ? 'Offline' : blind ? 'No signal' : learningHere ? 'Learning ' + Math.round((Number(s.learning_progress) || 0) * 100) + '%' : 'Online';
      const state = !live ? 'Last known: ' + word.toLowerCase() : word;
      const seen = !on && s.last_seen ? '<span>Last seen ' + esc(ago(s.last_seen)) + '</span>' : '';
      const rate = on && typeof s.rate_hz === 'number' ? '<span>' + (s.rate_hz >= 10 ? Math.round(s.rate_hz) : s.rate_hz.toFixed(1)) + NB + 'Hz</span>' : '';
      // The Mac's motion signal is the two per-antenna readings; show both, not just their mean.
      const chains = typeof s.rssi_c0 === 'number' && typeof s.rssi_c1 === 'number';
      const rssiTxt = chains ? Math.round(s.rssi_c0) + ' / ' + Math.round(s.rssi_c1) + NB + 'dBm' : typeof s.rssi === 'number' ? Math.round(s.rssi) + NB + 'dBm' : '';
      const rssi = on && rssiTxt
        ? '<span' + (chains ? ' title="Signal at each of the two antennas"' : '') + '><span class="bars" data-n="' + rssiBars(s.rssi) + '" aria-hidden="true"><i></i><i></i><i></i><i></i></span>' +
          (chains ? '<span class="sr">Antennas </span>' : '') + rssiTxt + '</span>' : '';
      const lost = on && s.kind === 'esp32' && typeof s.lost_pct === 'number' ? '<span>' + (s.lost_pct < 0.1 ? '0' : s.lost_pct.toFixed(1)) + '% lost</span>' : '';
      const meter = on && !blind ? '<span title="Activity on this sensor"><span class="mini-meter" data-src="' + esc(s.id) + '"><b></b></span><span class="num" data-src-num="' + esc(s.id) + '"></span></span>' : '';
      const health = blind ? '<span class="sensor-health">' + esc(s.health) + '</span>' : '';
      return '<li class="sensor' + (on && !blind ? '' : ' off') + '"><span class="sensor-name"><span class="dot"></span>' + esc(name) + '</span>' +
        '<span class="sensor-state">' + esc(state) + '</span><span class="sensor-kind">' + esc(kind) + '</span>' +
        '<span class="sensor-stats">' + rate + rssi + lost + meter + seen + '</span>' + health + '</li>';
    }).join('');
    if (ul.innerHTML !== html) ul.innerHTML = html;
    const m = {};
    for (const s of srcs) m[s.id] = s.score;
    renderSensorMeters(m);

    let sys = '';
    if (!live) sys = 'Not connected';
    else {
      const parts = [];
      if (typeof st.uptime_s === 'number') parts.push('Up ' + dur(st.uptime_s + (Date.now() - S.statusAt) / 1000, true));
      if (st.version) parts.push('v' + String(st.version).replace(/^v/, ''));
      sys = parts.join(' · ');
    }
    $('sysLine').textContent = sys;
  }

  function renderSensorMeters(scores) {
    const thr = S.status ? Number(S.status.threshold) || 1 : 1;
    for (const id in scores) {
      const v = Number(scores[id]);
      const el = document.querySelector('.mini-meter[data-src="' + CSS.escape(id) + '"]');
      if (!el || !isFinite(v)) continue;
      el.firstChild.style.width = Math.min(100, (v / (thr * 2)) * 100) + '%';
      el.classList.toggle('hot', v > thr);
      const n = document.querySelector('[data-src-num="' + CSS.escape(id) + '"]');
      if (n) n.textContent = v.toFixed(2);
    }
  }

  // ---------- render: settings ----------
  function renderSettings() {
    const live = isLive();
    const lvl = S.status && S.status.sensitivity;
    for (const b of $('sensSeg').querySelectorAll('button')) {
      b.setAttribute('aria-checked', String(b.dataset.level === lvl));
      b.disabled = !live || $('sensSeg').dataset.busy === '1';
    }
    $('relearnBtn').disabled = !live || (S.status && S.status.state === 'learning');
    $('relearnGo').disabled = !live;
    $('walkStart').disabled = !live || (S.status && (S.status.state === 'learning' || S.status.state === 'offline'));
    $('walkStop').disabled = !live;
    for (const b of document.querySelectorAll('#walkResult button')) b.disabled = !live;
  }

  // ---------- render: events ----------
  const rows = new Map();
  let lastOrderSig = '';

  function levelOf(thr) {
    const th = S.status && S.status.thresholds;
    if (!th || typeof thr !== 'number') return null;
    for (const l of ['low', 'medium', 'high']) if (Math.abs(Number(th[l]) - thr) < 1e-6) return l;
    return null;
  }

  function strengthOf(ev) {
    const thr = Number(ev.threshold) || (S.status && Number(S.status.threshold)) || 0;
    const peak = Number(ev.peak_score);
    if (!thr || !isFinite(peak)) return { n: 0, word: '', ratio: null };
    const ratio = peak / thr;
    const n = ratio < 1.6 ? 1 : ratio < 3 ? 2 : 3;
    return { n, word: ['', 'Slight', 'Moderate', 'Strong'][n], ratio };
  }

  function evDuration(ev) {
    if (ev.end == null) return serverNow() - ev.start;
    if (typeof ev.duration_s === 'number') return ev.duration_s;
    return ev.end - ev.start;
  }

  function createRow(ev) {
    const d = document.createElement('details');
    d.className = 'ev';
    d.dataset.id = ev.id;
    d.innerHTML =
      '<summary><span class="ev-time"></span><span class="ev-main"><span class="ev-line"><span class="ev-dur"></span>' +
      '<span class="ev-str"></span></span>' +
      '<span class="ev-sub"></span></span><span class="ev-right"><span class="chip"></span>' +
      '<svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></span></summary>' +
      '<div class="ev-body"><p class="fine ev-detail"></p>' +
      '<div class="scale" role="group" aria-label="Was this real?"><button type="button" data-label="real">Real</button><button type="button" data-label="false">False alarm</button></div>' +
      '<div class="input-row"><input type="text" maxlength="200" placeholder="Add a note (optional)" aria-label="Note"><button class="btn" type="button" data-save>Save note</button></div></div>';
    d.querySelectorAll('[data-label]').forEach((b) => b.addEventListener('click', () => {
      const cur = S.events.get(ev.id);
      setLabel(ev.id, cur && cur.label === b.dataset.label ? null : b.dataset.label);
    }));
    const input = d.querySelector('input');
    d.querySelector('[data-save]').addEventListener('click', () => {
      const cur = S.events.get(ev.id);
      setLabel(ev.id, cur ? cur.label : null, input.value.trim());
    });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') d.querySelector('[data-save]').click(); });
    input.value = ev.note || '';
    input.dataset.synced = ev.note || '';
    rows.set(ev.id, d);
    return d;
  }

  function updateRow(d, ev) {
    const isNew = S.summaryFrom != null && !S.digestDismissed && ev.start > S.summaryFrom;
    const time = d.querySelector('.ev-time');
    const timeHtml = esc(clock(ev.start)) + (isNew ? '<span class="new">New</span>' : '');
    if (time.innerHTML !== timeHtml) time.innerHTML = timeHtml;
    const ongoing = ev.end == null;
    d.querySelector('.ev-dur').textContent = dur(evDuration(ev));
    const s = strengthOf(ev);
    d.querySelector('.ev-str').textContent = s.word.toLowerCase();
    // Only name the sensor when there is more than this Mac to choose from.
    const srcs = Array.isArray(ev.sources) && ev.sources.some((x) => x !== 'mac') ? ev.sources.map(srcName).join(', ') : '';
    const sub = [ev.walktest ? 'Walk test' : '', ev.armed ? 'Armed' : 'Disarmed', s.ratio ? s.ratio.toFixed(1) + '× trigger' : '', srcs]
      .filter(Boolean).join(' · ') + (ev.note ? ' · “' + ev.note + '”' : '');
    d.querySelector('.ev-sub').textContent = sub;

    const chip = d.querySelector('.chip');
    let cls = 'chip', txt = '';
    if (ongoing) { cls += ' live'; txt = 'Now'; }
    else if (ev.label === 'real') { cls += ' real'; txt = 'Real'; }
    else if (ev.label === 'false') { cls += ' false'; txt = 'False alarm'; }
    else if (ev.armed) { cls += ' todo'; txt = 'To review'; }
    chip.className = cls; chip.textContent = txt; chip.hidden = !txt;

    // ev.threshold is the trigger level in force when the event peaked, so the rating doesn't
    // change if sensitivity is changed later.
    const lvl = levelOf(ev.threshold);
    const detail = 'Peak ' + num(ev.peak_score) + (s.ratio ? ', ' + s.ratio.toFixed(1) + '× the trigger level' +
      (typeof ev.threshold === 'number' ? ' (' + num(ev.threshold, 1) + (lvl ? ', ' + LEVEL_NAME[lvl] : '') + ' at the time)' : '') : '') +
      '. Started ' + fClockSec.format(new Date(ev.start * 1000)) +
      (ongoing ? ', still going.' : ', ended ' + fClockSec.format(new Date(ev.end * 1000)) + '.');
    d.querySelector('.ev-detail').textContent = detail;
    d.querySelectorAll('[data-label]').forEach((b) => {
      b.setAttribute('aria-pressed', String(ev.label === b.dataset.label));
      b.disabled = !isLive() || d.dataset.busy === '1';
    });
    const input = d.querySelector('input');
    if (document.activeElement !== input && input.dataset.synced !== (ev.note || '')) {
      input.value = ev.note || ''; input.dataset.synced = ev.note || '';
    }
    d.querySelector('[data-save]').disabled = !isLive();
    d.setAttribute('aria-label', 'Motion ' + whenLabel(ev.start) + ', ' + dur(evDuration(ev)));
  }

  function sortedEvents() {
    return Array.from(S.events.values()).filter((e) => typeof e.start === 'number').sort((a, b) => b.start - a.start);
  }

  function renderEvents() {
    const list = sortedEvents();
    chart.setEvents(list);
    day.setEvents(list);
    renderDaySummary();
    const box = $('eventList');
    $('moreBtn').hidden = !S.moreAvailable;
    if (!S.eventsLoaded) return;
    $('eventsCount').textContent = list.length ? (S.moreAvailable ? 'Latest ' + list.length : plural(list.length, 'event')) : '';
    if (!list.length) {
      box.innerHTML = '<p class="empty">No motion recorded yet. When something moves, it shows up here with its time, length and strength, and you can mark whether it was real.</p>';
      lastOrderSig = '';
      return;
    }
    const sig = list.map((e) => e.id + '@' + dayKey(e.start)).join(',') + '|' + dayKey(serverNow());
    if (sig === lastOrderSig) {
      for (const ev of list) updateRow(rows.get(ev.id), ev);
      for (const h of box.querySelectorAll('.day-head [data-count]')) h.textContent = h.dataset.count;
      return;
    }
    lastOrderSig = sig;
    const focused = box.contains(document.activeElement) ? document.activeElement : null;
    const frag = document.createDocumentFragment();
    const counts = {};
    for (const ev of list) counts[dayKey(ev.start)] = (counts[dayKey(ev.start)] || 0) + 1;
    let curDay = null, group = null;
    for (const ev of list) {
      const k = dayKey(ev.start);
      if (k !== curDay) {
        curDay = k;
        group = document.createElement('div');
        group.className = 'day-group';
        group.setAttribute('role', 'group');
        const label = dayLabel(ev.start);
        group.setAttribute('aria-label', label);
        group.innerHTML = '<div class="day-head"><span>' + esc(label) + '</span><span data-count="' + plural(counts[k], 'event') + '">' + plural(counts[k], 'event') + '</span></div>';
        frag.appendChild(group);
      }
      const row = rows.get(ev.id) || createRow(ev);
      updateRow(row, ev);
      group.appendChild(row);
    }
    box.replaceChildren(frag);
    if (focused && document.contains(focused)) focused.focus({ preventScroll: true });
  }

  function tickOngoingRows() {
    for (const ev of S.events.values()) {
      if (ev.end == null && rows.has(ev.id)) rows.get(ev.id).querySelector('.ev-dur').textContent = dur(evDuration(ev));
    }
  }

  async function setLabel(id, label, note) {
    const ev = S.events.get(id);
    if (!ev) return;
    const row = rows.get(id);
    const before = { label: ev.label, note: ev.note };
    ev.label = label;
    if (note !== undefined) ev.note = note;
    if (row) { row.dataset.busy = '1'; updateRow(row, ev); }
    try {
      const body = { label };
      if (note !== undefined) body.note = note;
      const r = await api('events/' + encodeURIComponent(id) + '/label', body);
      if (r && r.id != null) upsertEvent(r);
      if (note !== undefined) toast('Note saved');
    } catch (e) {
      Object.assign(ev, before);
      toast('Couldn’t save that. ' + e.message);
    } finally {
      if (row) { delete row.dataset.busy; updateRow(row, S.events.get(id)); }
      renderDigest();
    }
  }

  // ---------- render: digest ----------
  function renderDigest() {
    const box = $('digest');
    const from = S.summaryFrom;
    if (from == null || S.digestDismissed || !S.eventsLoaded) { box.hidden = true; return; }
    const evs = sortedEvents().filter((e) => e.start > from);
    const away = serverNow() - from;
    const gaps = coverageGaps(S.digestGaps, from, 20);
    if (!evs.length && !gaps.length && away < 600) { box.hidden = true; return; }
    $('digestTitle').textContent = 'Since you last checked ' + whenLabel(from);
    let html;
    if (!evs.length) html = gaps.length ? 'No motion was recorded while it was watching.' : 'No motion was recorded.';
    else {
      const armed = evs.filter((e) => e.armed).length;
      const truncated = S.moreAvailable && evs.length >= S.events.size;
      const n = (truncated ? evs.length + '+' : evs.length);
      const latest = evs[0];
      const strong = strengthOf(latest).word;
      html = '<strong>' + n + ' motion event' + (evs.length === 1 ? '' : 's') + '</strong>' +
        (armed === evs.length ? (evs.length === 1 ? ', while armed' : ', all while armed') : armed ? ', ' + armed + ' while armed' : ', none while armed') + '. ';
      html += latest.end == null ? 'One is happening now.' :
        'Latest ' + esc(whenLabel(latest.start)) + ', ' + dur(evDuration(latest)) + (strong ? ', ' + strong.toLowerCase() : '') + '.';
      const review = evs.filter((e) => e.armed && !e.label && e.end != null).length;
      if (review) html += ' ' + review + (review === 1 ? ' needs' : ' need') + ' review.';
    }
    // "No motion" only means something for the time it was actually watching.
    if (gaps.length) {
      // One sentence however many gaps there were: the total, the reasons once, and the longest stretch.
      // The day chart shows where each one was.
      const span = (g) => {
        const from = whenLabel(g.start).replace(/^at /, '');
        if (g.ongoing) return 'since ' + from;
        if (g.end - g.start < 60) return 'for ' + dur(g.end - g.start) + ' at ' + from;
        return 'from ' + from + ' to ' + (dayKey(g.start) === dayKey(g.end) ? clock(g.end) : whenLabel(g.end).replace(/^at /, ''));
      };
      const reasons = [];
      for (const g of gaps) for (const r of g.reasons) if (!reasons.includes(GAP_REASON[r] || r)) reasons.push(GAP_REASON[r] || r);
      const why = ' (' + reasons.join('; ') + ')';
      const now = gaps.find((g) => g.ongoing);
      let txt;
      if (gaps.length === 1) txt = span(gaps[0]) + why;
      else {
        const total = gaps.reduce((a, g) => a + (g.end - g.start), 0);
        const longest = gaps.reduce((a, g) => (g.end - g.start > a.end - a.start ? g : a));
        txt = 'for ' + dur(total, true) + ' in ' + gaps.length + ' stretches' + why + (now ? ', ' + span(now) : '; the longest ' + span(longest));
      }
      html += '<span class="digest-gaps"><strong>Not watching</strong> ' + esc(txt) + '.</span>';
    }
    $('digestText').innerHTML = html;
    box.hidden = false;
  }

  function markSeen() {
    if (S.skew == null && !S.everLive) return;
    store.set('lastSeen', serverNow());
  }

  // ---------- walk test ----------
  function applyWalk(w, fromUser) {
    if (!w) return;
    const prevRunning = S.walk && S.walk.running;
    S.walk = w;
    if (fromUser || (prevRunning && !w.running)) S.walkShowResult = !!w.result;
    renderWalk();
  }

  function renderWalk() {
    const w = S.walk || { running: false };
    const running = !!w.running;
    const showResult = !running && S.walkShowResult && w.result;
    $('walkIdle').hidden = running || !!showResult;
    $('walkRun').hidden = !running;
    $('walkResult').hidden = !showResult;
    if (running) renderWalkLive();
    if (showResult) renderWalkResult(w.result);
    renderSettings();
  }

  function renderWalkLive() {
    const w = S.walk;
    if (!w || !w.running) return;
    const e = Math.max(0, serverNow() - (w.started || serverNow()));
    const m = Math.floor(e / 60), s = Math.floor(e % 60);
    $('walkClock').textContent = m + ':' + String(s).padStart(2, '0');
    $('walkBar').style.width = Math.min(100, (e / 60) * 100) + '%';
    $('walkBar').parentElement.classList.toggle('ok', e >= 30);
    $('walkHint').textContent = e < 5 ? 'Start walking.' : e < 30 ? 'Keep walking through the rooms you care about.' :
      e < 60 ? 'That’s enough to measure. Stop when you’re done.' : 'Plenty. Tap stop to see results.';
    const st = S.status;
    const peak = w.live && typeof w.live.peak_score === 'number' ? ' · peak ' + num(w.live.peak_score) : '';
    $('walkLive').textContent = st && isLive() ? 'Activity now ' + num(st.score) + peak + ' · trigger at ' + num(st.threshold) : '';
  }

  function renderWalkResult(r) {
    const rec = r.recommended;
    const cur = S.status && S.status.sensitivity;
    const thr = r.threshold_by_level || {}, det = r.detected_by_level || {};
    let html = '<h4>' + (rec && rec !== 'none' ? 'Recommended: ' + LEVEL_NAME[rec] : 'Your walk wasn’t detected') + '</h4>';
    if (r.message) html += '<p class="msg">' + esc(r.message) + '</p>';
    html += '<div class="levels">' + ['low', 'medium', 'high'].map((l) =>
      '<div class="' + (l === rec ? 'rec' : '') + '"><b>' + LEVEL_NAME[l] + (l === cur ? ' (current)' : '') + '</b>' +
      '<span class="' + (det[l] ? 'yes' : '') + '">' + (det[l] === undefined ? '–' : det[l] ? 'Detected' : 'Missed') + '</span>' +
      '<span class="num">trigger ' + num(thr[l], 1) + '</span></div>').join('') + '</div>';
    html += '<p class="walk-stats">Walk peak ' + num(r.peak_score) + ' · typical ' + num(r.median_score) + (typeof r.idle_median === 'number' ? ' · still room ' + num(r.idle_median) : '') +
      (typeof r.duration_s === 'number' ? ' · ' + dur(r.duration_s) : '') + '</p>';
    if (rec && rec !== 'none' && rec === cur) html += '<p class="msg">You’re already on ' + LEVEL_NAME[rec] + ', so there’s nothing to change.</p>';
    html += '<div class="btn-row">';
    if (rec && rec !== 'none' && rec !== cur) html += '<button class="btn primary" type="button" data-apply="' + rec + '">Use ' + LEVEL_NAME[rec] + '</button>';
    html += '<button class="btn ghost" type="button" data-again>Test again</button><button class="btn ghost" type="button" data-done>Done</button></div>';
    const box = $('walkResult');
    box.innerHTML = html;
    const apply = box.querySelector('[data-apply]');
    if (apply) apply.addEventListener('click', () => setSensitivity(apply.dataset.apply).then(() => renderWalk()));
    box.querySelector('[data-again]').addEventListener('click', walkStart);
    box.querySelector('[data-done]').addEventListener('click', () => { S.walkShowResult = false; renderWalk(); });
  }

  async function walkStart() {
    try {
      const w = await api('walktest', { action: 'start' });
      applyWalk(w, true);
      $('walkSec').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch (e) { toast('Couldn’t start the walk test. ' + e.message); }
  }
  async function walkStop() {
    $('walkStop').disabled = true;
    try { applyWalk(await api('walktest', { action: 'stop' }), true); }
    catch (e) { toast('Couldn’t stop the walk test. ' + e.message); }
    finally { $('walkStop').disabled = false; }
  }

  // ---------- actions ----------
  async function setSensitivity(level) {
    const seg = $('sensSeg');
    seg.dataset.busy = '1';
    renderSettings();
    try {
      const st = await api('sensitivity', { level });
      if (st && st.state) applyStatus(st);
      else if (S.status) S.status.sensitivity = level;
      toast('Sensitivity set to ' + LEVEL_NAME[level]);
    } catch (e) { toast('Couldn’t change sensitivity. ' + e.message); }
    finally { delete seg.dataset.busy; render(); }
  }

  async function setArmed(armed) {
    const sw = $('armSwitch');
    sw.dataset.busy = '1';
    sw.setAttribute('aria-checked', String(armed));
    renderHero();
    const body = { armed };
    // the dashboard's arm uses the exit delay; POST /api/arm without delay_s arms at once
    if (armed && S.status.arm && S.status.arm.delay_s) body.delay_s = S.status.arm.delay_s;
    try {
      const st = await api('arm', body);
      if (st && st.state) applyStatus(st);
      if (st && st.arm && st.arm.mode === 'delay') toast('Arming in ' + dur(st.arm.delay_s) + '. Motion until then is only logged.');
    } catch (e) {
      toast('Couldn’t ' + (armed ? 'arm' : 'disarm') + '. ' + e.message);
    } finally { delete sw.dataset.busy; render(); }
  }

  async function relearn() {
    $('relearnGo').disabled = true;
    try {
      const st = await api('relearn', {});
      if (st && st.state) applyStatus(st);
      $('relearnConfirm').hidden = true;
      toast('Learning started. Keep the room still.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (e) { toast('Couldn’t start relearning. ' + e.message); }
    finally { render(); }
  }

  // ---------- alerts ----------
  let audioCtx = null;
  const canVibrate = typeof navigator.vibrate === 'function';

  function audioReady() { return !!audioCtx && audioCtx.state === 'running'; }

  function unlockAudio() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return false;
      if (!audioCtx) {
        audioCtx = new AC();
        audioCtx.onstatechange = renderAlerts;
      }
      if (audioCtx.state !== 'running') audioCtx.resume().then(renderAlerts, renderAlerts);
      // iOS needs a buffer started inside the gesture to fully unlock output
      const b = audioCtx.createBuffer(1, 1, 22050);
      const src = audioCtx.createBufferSource();
      src.buffer = b; src.connect(audioCtx.destination); src.start(0);
      return true;
    } catch (e) { return false; }
  }

  function chime(urgent) {
    if (!audioReady()) return;
    const t0 = audioCtx.currentTime + 0.02;
    const notes = urgent ? [[880, 0], [660, 0.22], [880, 0.44], [660, 0.66]] : [[660, 0], [880, 0.16]];
    for (const [f, dt] of notes) {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = 'sine'; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t0 + dt);
      g.gain.exponentialRampToValueAtTime(urgent ? 0.5 : 0.3, t0 + dt + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.2);
      o.connect(g).connect(audioCtx.destination);
      o.start(t0 + dt); o.stop(t0 + dt + 0.22);
    }
  }

  function maybeAlert(ev) {
    const key = ev ? ev.id : 'tick';
    const nowMs = Date.now();
    if (ev && S.alertedIds.has(ev.id)) return;
    if (nowMs - S.lastAlertAt < 8000) { if (ev) S.alertedIds.add(ev.id); return; }
    if (ev) S.alertedIds.add(key);
    S.lastAlertAt = nowMs;
    if (S.alerts.sound) chime(true);
    if (S.alerts.vib && canVibrate) { try { navigator.vibrate([300, 150, 300, 150, 300]); } catch (e) { /* ignore */ } }
  }

  function renderAlerts() {
    const sw = $('soundSwitch'), vs = $('vibSwitch');
    sw.setAttribute('aria-checked', String(S.alerts.sound));
    vs.setAttribute('aria-checked', String(S.alerts.vib && canVibrate));
    vs.disabled = !canVibrate;
    $('vibNote').textContent = canVibrate ? '' : 'Not supported by this browser (including iPhone Safari)';
    $('soundNote').textContent = !S.alerts.sound ? 'iPhone’s silent switch mutes it' : audioReady() ? 'On · iPhone’s silent switch mutes it' : 'Tap anywhere on the page to finish turning it on';
    const on = S.alerts.sound || (S.alerts.vib && canVibrate);
    const btn = $('alertsBtn');
    btn.dataset.on = String(on);
    btn.querySelector('use').setAttribute('href', on ? '#i-bell-on' : '#i-bell');
    $('soundPaused').hidden = !(S.alerts.sound && !audioReady());
  }

  // ---------- toast ----------
  let toastTimer = null;
  function toast(msg) {
    const t = $('toast');
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3800);
  }

  // ---------- chart ----------
  const chart = new window.SentryChart($('chart'), $('chartTip'), {
    now: serverNow,
    fmtTick: (t, range) => {
      const d = new Date(t * 1000);
      if (range > 3600 && d.getHours() === 0 && d.getMinutes() === 0) return fWeekday.format(d);
      return range > 3600 ? fHour.format(d) : fClock.format(d);
    },
    fmtTip: (t, s, thr) => esc(S.range <= 3600 ? fClockSec.format(new Date(t * 1000)) : clock(t)) +
      '<br>Activity ' + num(s) + (isFinite(thr) ? ' · trigger ' + num(thr) : ''),
    fmtGap: (g) => {
      const end = g.ongoing || typeof g.end !== 'number' ? null : g.end;
      const f = S.range <= 3600 ? fClockSec : fClock;
      return '<b>Not watching</b>: ' + esc(GAP_REASON[g.reason] || g.reason) + '<br>' + esc(f.format(new Date(g.start * 1000))) +
        ' – ' + (end ? esc(f.format(new Date(end * 1000))) : 'now');
    },
  });

  const day = new window.DayChart($('dayChart'), $('dayTip'), {
    now: serverNow,
    fmtHour: (t) => {
      const d = new Date(t * 1000);
      return d.getHours() === 0 ? fWeekday.format(d) : fHour.format(d);
    },
    fmtTip: (t, s, thr) => esc(clock(t)) + '<br>Activity ' + num(s) + (isFinite(thr) ? ' · trigger ' + num(thr) : ''),
    fmtGap: (g) => {
      const end = g.ongoing || typeof g.end !== 'number' ? null : g.end;
      return '<b>Not watching</b>: ' + esc(GAP_REASON[g.reason] || g.reason) + '<br>' + esc(clock(g.start)) + ' – ' + (end ? esc(clock(end)) : 'now');
    },
    fmtEvent: (ev) => '<b>Motion ' + esc(clock(ev.start)) + '</b>, ' + esc(dur(evDuration(ev))) +
      (strengthOf(ev).word ? ', ' + strengthOf(ev).word.toLowerCase() : '') + '<br>' + (ev.armed ? 'Armed' : 'Disarmed') +
      (ev.label === 'real' ? ' · real' : ev.label === 'false' ? ' · false alarm' : ''),
  });

  // One line next to the chart and a full sentence for screen readers: what the last 24 hours hold.
  function renderDaySummary() {
    const now = serverNow(), from = now - 86400;
    const evs = sortedEvents().filter((e) => (e.end == null ? now : e.end) >= from);
    const blind = coverageGaps(day.gaps, from, 20);
    const blindS = blind.reduce((a, g) => a + (g.end - g.start), 0);
    const parts = [];
    if (S.eventsLoaded) parts.push(evs.length ? plural(evs.length, 'event') : 'No motion');
    if (blindS >= 60) parts.push(dur(blindS, true) + ' not watching');
    const note = parts.join(' · ');
    if ($('dayNote').textContent !== note) $('dayNote').textContent = note;
    let label = 'Activity over the last 24 hours, drawn around a dial with now at the top. ';
    label += evs.length ? plural(evs.length, 'motion event') + ', the latest ' + whenLabel(evs[0].start) + '. ' : 'No motion events. ';
    label += blind.length ? 'Not watching for ' + dur(blindS, true) + ' in total.' : 'Watching the whole time.';
    $('dayChart').setAttribute('aria-label', label);
  }

  // ---------- arming: hero switch text, exit delay, schedule ----------
  const DAY_LETTER = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
  const DAY_LONG = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

  function whenShort(t) {
    const k = dayKey(t), n = serverNow();
    if (k === dayKey(n)) return clock(t);
    if (k === dayKey(n + 86400)) return 'tomorrow ' + clock(t);
    return fWeekday.format(new Date(t * 1000)) + ' ' + clock(t);
  }

  // What the arm switch shows: on (armed, or counting down to it), label, and why.
  function armModel(st) {
    const armed = !!(st && st.armed);
    const a = st && st.arm;
    const chans = (st && st.alerts && st.alerts.channels) || [];
    if (!a) return { on: armed, label: armed ? 'Armed' : 'Disarmed', hint: armed ? 'Motion raises an alert' : 'Motion is logged without alerts' };
    if (a.mode === 'delay' && typeof a.arm_at === 'number') {
      const left = Math.max(0, Math.ceil(a.arm_at - serverNow()));
      return { on: true, delay: true, label: left ? 'Arming in ' + Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0') : 'Arming…',
        hint: 'Motion until then is only logged. Tap to cancel.' };
    }
    const nc = a.next_change;
    const next = nc ? (nc.armed ? 'arms ' : 'disarms ') + whenShort(nc.t) : '';
    let hint;
    if (a.mode === 'schedule') hint = 'By schedule' + (next ? ' · ' + next : '');
    else if (a.schedule_on) hint = (armed ? 'Armed' : 'Disarmed') + ' by you' + (next ? ' · schedule ' + next : '');
    else if (armed) hint = chans.length ? 'Motion sends an alert' : 'Motion shows here only: no phone alerts set up';
    else hint = 'Motion is logged without alerts';
    // Armed means nothing while it can't sense: say so instead of promising alerts.
    if (armed && st.state === 'offline') hint = 'Armed, but not watching until the sensor signal is back';
    else if (armed && st.state === 'learning') hint = 'Armed; nothing is detected until learning finishes';
    return { on: armed, label: armed ? 'Armed' : 'Disarmed', hint };
  }

  async function loadArming() {
    try { S.arming = await api('arming'); S.schedDraft = null; }
    catch (e) { if (e.status === 404) S.arming = null; }
    renderArming();
  }

  function copyWindows(ws) { return (ws || []).map((w) => ({ days: w.days.slice(), start: w.start, end: w.end === '24:00' ? '00:00' : w.end })); }

  function renderArming() {
    const v = S.arming;
    $('armSec').hidden = !v;
    if (!v) return;
    const live = isLive();
    const sel = $('delaySelect');
    const cur = Number(v.settings.delay_s) || 0;
    if (!Array.from(sel.options).some((o) => Number(o.value) === cur)) {
      const o = document.createElement('option');
      o.value = String(cur); o.textContent = dur(cur);
      sel.appendChild(o);
    }
    if (sel.dataset.busy !== '1') sel.value = String(cur);
    sel.disabled = !live || sel.dataset.busy === '1';
    if (!S.schedDraft) S.schedDraft = { enabled: v.settings.schedule.enabled, windows: copyWindows(v.settings.schedule.windows), dirty: false, sig: '' };
    const d = S.schedDraft;
    const sw = $('schedSwitch');
    sw.setAttribute('aria-checked', String(d.enabled));
    sw.disabled = !live;
    $('schedBox').hidden = !d.enabled;
    $('schedTz').textContent = 'Times are this Mac’s local time' + (v.timezone && v.timezone.includes('/') ? ' (' + v.timezone.replace(/_/g, ' ') + ')' : '') + '.';
    renderSchedRows();
    $('schedSave').disabled = !live || !d.dirty || !d.windows.length;
    $('schedAdd').disabled = d.windows.length >= 14;
  }

  function renderSchedRows() {
    const d = S.schedDraft, box = $('schedRows');
    const sig = d.windows.map((w) => w.days.join('') + w.start + w.end).join('|');
    if (sig === d.sig && box.childElementCount === d.windows.length) return;  // don't disturb typing
    d.sig = sig;
    box.innerHTML = d.windows.map((w, i) =>
      '<div class="win" data-i="' + i + '"><div class="days" role="group" aria-label="Days">' +
      DAY_LETTER.map((l, di) => '<button type="button" class="dow" data-day="' + di + '" aria-pressed="' + w.days.includes(di) + '" aria-label="' + DAY_LONG[di] + '">' + l + '</button>').join('') +
      '<button class="icon-btn small" type="button" data-rm aria-label="Remove this time"><svg class="ic"><use href="#i-x"/></svg></button>' +
      '</div><div class="win-times"><label class="time"><span class="sr">From</span><input type="time" data-k="start" value="' + esc(w.start) + '" required></label>' +
      '<span class="fine">to</span><label class="time"><span class="sr">Until</span><input type="time" data-k="end" value="' + esc(w.end) + '" required></label></div>' +
      '<p class="fine win-note">' + winNote(w) + '</p></div>').join('');
  }

  function winNote(w) {
    if (!w.days.length) return 'Pick at least one day.';
    if (w.end === w.start) return 'All day.';
    if (w.end > w.start) return '';
    const [h, mi] = w.end.split(':').map(Number);
    return 'Overnight, until ' + fClock.format(new Date(2026, 0, 1, h || 0, mi || 0)) + ' the next day.';
  }

  function schedChanged() {
    S.schedDraft.dirty = true;
    $('schedMsg').hidden = true;
    renderArming();
    for (const row of $('schedRows').querySelectorAll('.win')) row.querySelector('.win-note').textContent = winNote(S.schedDraft.windows[Number(row.dataset.i)]);
  }

  async function saveArming(body, msgEl) {
    try {
      S.arming = await api('arming', body);
      S.schedDraft = null;
      api('status').then((st) => { applyStatus(st); render(); }).catch(() => {});
      if (msgEl) {
        const a = S.arming.state;
        const nc = a.next_change;
        showMsg(msgEl, true, 'Saved. ' + (a.armed ? 'Armed' : a.mode === 'delay' ? 'Arming' : 'Disarmed') + ' now' +
          (nc ? '; ' + (nc.armed ? 'arms ' : 'disarms ') + whenShort(nc.t) + '.' : '.'));
      }
      return true;
    } catch (e) {
      if (msgEl) showMsg(msgEl, false, e.message); else toast('Couldn’t save. ' + e.message);
      return false;
    } finally { renderArming(); }
  }

  // ---------- alert channels ----------
  const COOLDOWNS = [[0, 'No limit'], [60, '1 min'], [120, '2 min'], [300, '5 min'], [600, '10 min'], [900, '15 min'], [1800, '30 min'], [3600, '1 hour']];
  const isLoopbackHost = (h) => /^(127\.|localhost$|\[::1\]$)/.test(h);

  async function loadAlerts() {
    try { S.chan = await api('alerts'); }
    catch (e) { if (e.status === 404) S.chan = null; return; }
    renderAlertsPanel(true);
    renderPrompt();
    if (S.onboarding && S.onboarding.show && S.onboarding.step === 'alerts') renderSetup(true);
  }

  let alertsReload = null;
  function onAlertsEvent(d) {
    if (S.status && d.summary) { S.status.alerts = d.summary; renderAlertFail(); renderHero(); }
    clearTimeout(alertsReload);
    alertsReload = setTimeout(() => { loadAlerts(); loadOnboarding(); }, 300);
  }

  const chanOf = (kind) => S.chan && S.chan.channels.find((c) => c.id === kind);

  function chanBadge(ch) {
    if (!ch.configured) return ['off', 'Not set up'];
    const l = ch.last;
    if (ch.enabled && l && !l.ok && l.kind !== 'test') return ['bad', 'Failing'];
    return ch.enabled ? ['on', 'On'] : ['off', 'Off'];
  }

  function lastLine(ch) {
    const l = ch.last;
    if (!l) return ch.configured ? 'Nothing sent yet.' : '';
    const what = { test: 'Test', close: '“Motion ended” alert', blind: '“Can’t sense” alert',
      watching: '“Watching again” alert', restart: '“Was not running” alert' }[l.kind] || 'Alert';
    let s = l.ok ? what + ' delivered ' + whenLabel(l.t) + '.' : what + ' failed ' + whenLabel(l.t) + ': ' + l.error;
    if (!l.ok && ch.last_ok) s += ' Last delivered ' + whenLabel(ch.last_ok) + '.';
    return s;
  }

  function showMsg(el, ok, text) {
    el.hidden = false;
    el.className = 'form-msg ' + (ok ? 'ok' : 'err');
    el.textContent = text;
  }

  function field(label, input, hint) {
    return '<label class="field"><span class="field-label">' + label + '</span>' + input + (hint ? '<span class="fine">' + hint + '</span>' : '') + '</label>';
  }
  const TXT = ' autocapitalize="off" autocorrect="off" spellcheck="false"';
  const SECRET = ' autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true"';

  function privacyNote(server, hasLink) {
    const pub = /^https:\/\/ntfy\.sh\/?$/i.test(server || 'https://ntfy.sh');
    if (!pub) return '<p class="note">Alerts go through your own server (' + esc(server.replace(/^https?:\/\//, '')) + '). On iPhone, instant delivery from a self-hosted server needs <code>upstream-base-url</code> set on it.</p>';
    return '<p class="note"><b>Privacy:</b> alerts pass through ntfy.sh, a free public server run by a third party. Anyone who learns the topic name can read them, so keep it to yourself like a password. The message only says that motion was detected, when, and how strong' +
      (hasLink ? ', plus your dashboard link' : ': no names, addresses or sensor details') + '.</p>';
  }

  function topicUrl(s) { return String(s.server || 'https://ntfy.sh').replace(/\/+$/, '') + '/' + s.topic; }

  function ntfySteps(ch) {
    const s = ch.settings;
    const pub = /^https:\/\/ntfy\.sh\/?$/i.test(s.server || '');
    const url = topicUrl(s);
    return '<ol class="steps">' +
      '<li><b>Install the ntfy app</b> on your phone. It’s free, from the App Store or Google Play; no account needed.</li>' +
      '<li><b>Subscribe to this topic</b> in the app (the + button' + (pub ? '' : ', with “Use another server”: <code>' + esc(s.server) + '</code>') + '):' +
      '<span class="codebox"><input readonly value="' + esc(s.topic) + '" aria-label="Topic name"' + TXT + '><button class="btn small" type="button" data-copy="' + esc(s.topic) + '"><svg class="ic"><use href="#i-copy"/></svg>Copy</button></span>' +
      '<span class="fine">Or open <a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(url.replace(/^https?:\/\//, '')) + '</a> on the phone.</span></li>' +
      '<li><b>Send a test</b> and check that it arrives.</li></ol>';
  }

  function enableRow(ch, text) {
    const can = ch.configured && ch.complete;
    return '<div class="row-toggle"><div><span class="label">' + text + '</span><span class="fine">' + (can ? (ch.enabled ? 'On: armed motion is sent here' : 'Off: nothing is sent here') : 'Fill in the settings below first') + '</span></div>' +
      '<button class="switch" type="button" role="switch" data-act="enable" aria-checked="' + ch.enabled + '" aria-label="' + text + '"' + (can ? '' : ' disabled') + '><span></span></button></div>';
  }

  function sourceNote(ch) {
    return ch.source === 'config_file' ? '<p class="note">Set in <code>config.local.toml</code>. Saving here takes this channel over; removing it here goes back to the file.</p>' : '';
  }

  function chanBody(kind) {
    const ch = chanOf(kind);
    const s = ch.settings;
    const test = '<button class="btn" type="button" data-act="test">Send test alert</button>';
    const remove = ch.source === 'dashboard' ? '<button class="btn ghost" type="button" data-act="remove">Remove</button>' : '';
    const msg = '<p class="form-msg" data-msg role="status" hidden></p>';
    if (kind === 'ntfy') {
      const adv = '<details class="adv"><summary>' + (ch.configured ? 'Server, access token, priority' : 'Use your own ntfy server') + '<svg class="ic chev"><use href="#i-chev"/></svg></summary><div class="adv-body">' +
        field('Server', '<input type="url" name="server" inputmode="url" value="' + esc(s.server || 'https://ntfy.sh') + '" placeholder="https://ntfy.sh"' + TXT + '>') +
        (ch.configured ? field('Topic', '<input type="text" name="topic" value="' + esc(s.topic) + '" maxlength="64"' + TXT + '>', s.topic && s.topic.length < 16 ? 'Short topics are easy to guess.' : '') : '') +
        field('Access token', '<input type="password" name="token" placeholder="' + (ch.secrets.token ? 'Saved; type to replace' : 'Only if your server needs one') + '"' + SECRET + '>',
          ch.secrets.token ? '<label class="inline"><input type="checkbox" name="clear_token"> Remove the saved token</label>' : '') +
        (ch.configured ? field('Priority', '<select name="priority">' + ['default', 'high', 'urgent'].map((p) => '<option value="' + p + '"' + (s.priority === p ? ' selected' : '') + '>' + { default: 'Normal', high: 'High (default)', urgent: 'Urgent' }[p] + '</option>').join('') + '</select>') : '') +
        (ch.configured ? '<div class="btn-row"><button class="btn" type="button" data-act="save">Save</button><button class="btn ghost" type="button" data-act="newtopic">New topic</button>' + remove + '</div>' : '') +
        '</div></details>';
      if (!s.topic) {
        return sourceNote(ch) + '<p class="fine">Free push notifications through the ntfy app. wifi-sentry makes up a long random topic name and your phone subscribes to it.</p>' +
          privacyNote(s.server, !!(S.chan.settings.base_url)) +
          '<div class="btn-row"><button class="btn primary" type="button" data-act="ntfy-setup"><svg class="ic"><use href="#i-phone"/></svg>Set up phone alerts</button></div>' + msg + adv;
      }
      return sourceNote(ch) + enableRow(ch, 'Send alerts to this topic') + ntfySteps(ch) +
        '<div class="btn-row">' + test + '</div>' + msg + privacyNote(s.server, !!(S.chan.settings.base_url)) + adv;
    }
    const saveBtn = '<button class="btn primary" type="button" data-act="save">' + (ch.configured ? 'Save' : 'Save and turn on') + '</button>';
    if (kind === 'webhook') {
      return sourceNote(ch) + (ch.configured ? enableRow(ch, 'Send alerts to this webhook') : '<p class="fine">For Home Assistant, Slack, Discord or anything that accepts a JSON POST.</p>') +
        field('Webhook URL', '<input type="url" name="url" inputmode="url" placeholder="' + (ch.secrets.url ? 'Saved; paste a new URL to replace' : 'https://…') + '"' + SECRET + '>',
          ch.secrets.url ? 'Saved: ' + esc(ch.url_hint || 'set') + ' (the full URL stays hidden, it often works as a password)' : '') +
        field('Format', '<select name="format">' + [['json', 'Generic JSON (Home Assistant, n8n, …)'], ['slack', 'Slack incoming webhook'], ['discord', 'Discord webhook']].map(([v, l]) => '<option value="' + v + '"' + (s.format === v ? ' selected' : '') + '>' + l + '</option>').join('') + '</select>') +
        '<details class="adv"><summary>What gets sent<svg class="ic chev"><use href="#i-chev"/></svg></summary><div class="adv-body"><p class="fine">A POST with <code>Content-Type: application/json</code>. Generic JSON:</p>' +
        '<pre class="code">{"source": "wifi-sentry",\n "kind": "open" | "close" | "test",\n "title": "Motion detected at home",\n "message": "Motion started at 14:03:12. …",\n "time": "2026-11-23T14:03:12-08:00",\n "url": dashboard link or null,\n "suppressed": 0,\n "event": {"id", "start", "end",\n   "duration_s", "peak_score",\n   "threshold", "armed", …},\n "status": {"state", "armed", "score",\n   "threshold", "sensitivity"}}</pre>' +
        '<p class="fine">Slack gets <code>{"text": …}</code>, Discord <code>{"content": …}</code>. Any 2xx answer counts as delivered.</p></div></details>' +
        '<div class="btn-row">' + saveBtn + (ch.configured ? test : '') + remove + '</div>' + msg;
    }
    return sourceNote(ch) + (ch.configured ? enableRow(ch, 'Send alerts by email') : '<p class="fine">Through your mail provider’s SMTP server. Most need an app password rather than your normal one.</p>') +
      '<div class="grid2">' + field('SMTP server', '<input type="text" name="host" value="' + esc(s.host) + '" placeholder="smtp.example.com" inputmode="url"' + TXT + '>') +
      field('Port', '<input type="text" name="port" value="' + esc(s.port) + '" inputmode="numeric" pattern="[0-9]*" maxlength="5"' + TXT + '>') + '</div>' +
      field('Security', '<select name="security">' + [['starttls', 'STARTTLS (usually port 587)'], ['ssl', 'SSL/TLS (usually port 465)'], ['none', 'None (local relay only)']].map(([v, l]) => '<option value="' + v + '"' + (s.security === v ? ' selected' : '') + '>' + l + '</option>').join('') + '</select>') +
      field('User name', '<input type="text" name="username" value="' + esc(s.username) + '" autocomplete="off"' + TXT + '>') +
      field('Password', '<input type="password" name="password" placeholder="' + (ch.secrets.password ? 'Saved; type to replace' : '') + '"' + SECRET + '>') +
      field('From', '<input type="email" name="from" value="' + esc(s.from) + '" autocomplete="off"' + TXT + '>') +
      field('To', '<input type="email" name="to" multiple value="' + esc((s.to || []).join(', ')) + '" autocomplete="off"' + TXT + '>', 'Separate several addresses with commas.') +
      '<div class="btn-row">' + saveBtn + (ch.configured ? test : '') + remove + '</div>' + msg;
  }

  function optsBody() {
    const st = S.chan.settings;
    const cur = Number(st.cooldown_s);
    const opts = COOLDOWNS.some(([v]) => v === cur) ? COOLDOWNS : COOLDOWNS.concat([[cur, dur(cur)]]);
    const sugg = [];
    if (!isLoopbackHost(location.hostname)) sugg.push(location.origin + '/');
    for (const u of S.chan.suggested_base_urls || []) if (!sugg.includes(u)) sugg.push(u);
    return field('Time between alerts', '<select name="cooldown_s">' + opts.map(([v, l]) => '<option value="' + v + '"' + (v === cur ? ' selected' : '') + '>' + l + (v === 300 ? ' (default)' : '') + '</option>').join('') + '</select>',
      'Every motion event is still logged. Events within this time after an alert are counted and mentioned in the next one.') +
      '<div class="row-toggle"><div><span class="label">Also tell me when motion ends</span><span class="fine">A second message for each alerted event</span></div>' +
      '<button class="switch" type="button" role="switch" name="notify_on_close" aria-checked="' + !!st.notify_on_close + '" aria-label="Also tell me when motion ends"><span></span></button></div>' +
      field('Dashboard link in alerts', '<input type="url" name="base_url" inputmode="url" value="' + esc(st.base_url) + '" placeholder="Not included"' + TXT + '>',
        'Lets you tap an alert to open this dashboard. Leave it empty to keep addresses out of the alert text.') +
      (sugg.length ? '<div class="btn-row">' + sugg.map((u) => '<button class="btn small ghost" type="button" data-use="' + esc(u) + '">Use ' + esc(u.replace(/^https?:\/\//, '').replace(/\/$/, '')) + '</button>').join('') + '</div>' : '') +
      '<div class="btn-row"><button class="btn primary" type="button" data-act="save-opts">Save</button></div><p class="form-msg" data-msg role="status" hidden></p>';
  }

  // The panel: three channels and the options, each a <details> whose body is only rebuilt when it
  // isn't being edited (so a status update never wipes what you're typing).
  function renderAlertsPanel(fresh) {
    const v = S.chan;
    $('alertsSec').hidden = !v;
    if (!v) return;
    const box = $('chanList');
    if (!box.dataset.built) {
      box.dataset.built = '1';
      box.innerHTML = ['ntfy', 'webhook', 'email'].map((k) =>
        '<details class="chan" data-kind="' + k + '"><summary>' +
        '<span class="chan-main"><span class="chan-name"></span><span class="chan-last"></span></span><span class="chip"></span><svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></summary>' +
        '<div class="chan-body" data-body></div></details>').join('') +
        '<details class="chan" data-kind="opts"><summary><span class="chan-main"><span class="chan-name">Alert options</span><span class="chan-last"></span></span><svg class="ic chev" aria-hidden="true"><use href="#i-chev"/></svg></summary><div class="chan-body" data-body></div></details>';
    }
    const live = isLive();
    let on = 0;
    for (const d of box.querySelectorAll('details.chan')) {
      const kind = d.dataset.kind;
      const body = d.querySelector('[data-body]');
      if (kind === 'opts') {
        const st = v.settings;
        d.querySelector('.chan-last').textContent = (Number(st.cooldown_s) ? 'At most one every ' + dur(st.cooldown_s) : 'Every event') + (st.notify_on_close ? ', plus “ended”' : '') + (st.base_url ? ', with link' : '');
      } else {
        const ch = chanOf(kind);
        if (ch.enabled) on++;
        d.querySelector('.chan-name').textContent = ch.label;
        const [cls, word] = chanBadge(ch);
        const chip = d.querySelector('.chip');
        chip.className = 'chip' + (cls ? ' ' + cls : '');
        chip.textContent = word;
        d.querySelector('.chan-last').textContent = ch.configured ? lastLine(ch) : { ntfy: 'Push to the free ntfy app', webhook: 'Home Assistant, Slack, Discord, …', email: 'Through your mail provider' }[kind];
        d.classList.toggle('bad', cls === 'bad');
      }
      const editing = body.contains(document.activeElement) || body.dataset.dirty === '1' || body.dataset.busy === '1';
      if (!body.dataset.built || (fresh && !editing)) {
        const keepMsg = body.querySelector('[data-msg]:not([hidden])');
        const openAdv = Array.from(body.querySelectorAll('details.adv')).map((x) => x.open);
        body.innerHTML = kind === 'opts' ? optsBody() : chanBody(kind);
        body.dataset.built = '1';
        delete body.dataset.dirty;
        if (keepMsg) { const m = body.querySelector('[data-msg]'); if (m) { m.hidden = false; m.className = keepMsg.className; m.textContent = keepMsg.textContent; } }
        body.querySelectorAll('details.adv').forEach((x, i) => { if (openAdv[i]) x.open = true; });
      }
    }
    $('alertsCount').textContent = on ? on + ' on' : 'Off';
    $('chanList').classList.toggle('stale', !live);
  }

  function formValues(body) {
    const out = {};
    for (const el of body.querySelectorAll('input[name], select[name]')) {
      if (el.type === 'checkbox') out[el.name] = el.checked;
      else out[el.name] = el.value.trim();
    }
    return out;
  }

  function channelPayload(kind, vals) {
    const p = {};
    if (kind === 'ntfy') {
      if (vals.server !== undefined) p.server = vals.server || 'https://ntfy.sh';
      if (vals.topic !== undefined) p.topic = vals.topic;
      if (vals.priority) p.priority = vals.priority;
      if (vals.token) p.token = vals.token;
      else if (vals.clear_token) p.token = '';
    } else if (kind === 'webhook') {
      if (vals.url) p.url = vals.url;
      p.format = vals.format;
    } else {
      p.host = vals.host; p.security = vals.security; p.username = vals.username;
      p.from = vals.from; p.to = vals.to;
      const port = Number(vals.port);
      p.port = Number.isInteger(port) && vals.port !== '' ? port : vals.port;
      if (vals.password) p.password = vals.password;
    }
    return p;
  }

  async function chanAction(kind, act, btn, body) {
    const msg = body.querySelector('[data-msg]');
    const ch = chanOf(kind);
    body.dataset.busy = '1';
    btn.disabled = true;
    try {
      if (act === 'ntfy-setup') {
        const t = await api('alerts/ntfy-topic', {});
        const vals = formValues(body);
        S.chan = await api('alerts/channels/ntfy', { topic: t.topic, server: vals.server || 'https://ntfy.sh', token: vals.token || undefined, enabled: true });
        delete body.dataset.busy;
        renderAlertsPanel(true);
        toast('Phone alerts set up. Subscribe in the ntfy app, then send a test.');
        loadOnboarding();
        return;
      }
      if (act === 'newtopic') {
        if (!confirm('Make a new random topic? Your phone has to subscribe to the new one; the old topic gets no more alerts.')) return;
        const t = await api('alerts/ntfy-topic', {});
        S.chan = await api('alerts/channels/ntfy', { topic: t.topic });
        delete body.dataset.busy;
        renderAlertsPanel(true);
        toast('New topic made. Subscribe to it in the ntfy app.');
        return;
      }
      if (act === 'remove') {
        if (!confirm('Remove the ' + ch.label + ' settings? Nothing more is sent there.')) return;
        S.chan = await api('alerts/channels/' + kind, undefined, 'DELETE');
        delete body.dataset.busy;
        renderAlertsPanel(true);
        loadOnboarding();
        return;
      }
      if (act === 'enable') {
        S.chan = await api('alerts/channels/' + kind, { enabled: !ch.enabled });
        delete body.dataset.busy;
        renderAlertsPanel(true);
        loadOnboarding();
        return;
      }
      if (act === 'save-opts') {
        const vals = formValues(body);
        const sw = body.querySelector('[name="notify_on_close"]');
        S.chan = await api('alerts/settings', { cooldown_s: Number(vals.cooldown_s), base_url: vals.base_url, notify_on_close: sw.getAttribute('aria-checked') === 'true' });
        delete body.dataset.dirty;
        delete body.dataset.busy;
        renderAlertsPanel(true);
        showMsg(body.querySelector('[data-msg]'), true, 'Saved.');
        return;
      }
      if (act === 'save' || (act === 'test' && body.dataset.dirty === '1')) {
        const p = channelPayload(kind, formValues(body));
        if (!ch.configured && kind !== 'ntfy') p.enabled = true;
        S.chan = await api('alerts/channels/' + kind, p);
        delete body.dataset.dirty;
        if (act === 'save') {
          delete body.dataset.busy;
          renderAlertsPanel(true);
          const m = body.querySelector('[data-msg]');
          const now = chanOf(kind);
          showMsg(m, true, now.enabled ? 'Saved. Send a test to check it works.' : 'Saved. It’s off: turn it on above to use it.');
          loadOnboarding();
          return;
        }
      }
      if (act === 'test') {
        showMsg(msg, true, 'Sending…');
        msg.className = 'form-msg';
        const r = await api('alerts/channels/' + kind + '/test', {}, 'POST', 40000);
        const target = kind === 'ntfy' ? 'Check your phone.' : kind === 'email' ? 'Check the inbox (and spam folder).' : '';
        showMsg(msg, r.ok, r.ok ? 'Delivered in ' + (r.ms / 1000).toFixed(1) + NB + 's. ' + target : 'Not delivered: ' + r.error);
        await loadAlerts();
      }
    } catch (e) {
      showMsg(msg || body.querySelector('[data-msg]'), false, e.message);
    } finally {
      delete body.dataset.busy;
      btn.disabled = false;
    }
  }

  async function copyText(text, btn) {
    let ok = false;
    try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; } } catch (e) { /* fall back */ }
    if (!ok) {
      const input = btn.parentElement.querySelector('input');
      try {
        input.focus(); input.select(); input.setSelectionRange(0, 9999);
        ok = document.execCommand('copy');
        input.blur();
      } catch (e) { ok = false; }
    }
    toast(ok ? 'Copied' : 'Couldn’t copy: select the text and copy it by hand');
  }

  // ---------- failing alerts, the small prompt ----------
  function renderAlertFail() {
    const st = S.status;
    const f = isLive() && st && st.alerts && st.alerts.failing && st.alerts.failing[0];
    $('alertFail').hidden = !f;
    if (!f) return;
    $('alertFailText').textContent = 'Alerts aren’t getting through. ' + f.label + ' failed ' + whenLabel(f.t) + ': ' + f.error;
  }

  function renderPrompt() {
    const ob = S.onboarding;
    $('alertsPrompt').hidden = !(ob && ob.alerts_prompt && !ob.show && isLive() && S.chan);
  }

  function openChannel(kind) {
    const d = document.querySelector('details.chan[data-kind="' + kind + '"]');
    if (!d) return;
    d.open = true;
    d.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  // ---------- first-run setup ----------
  const STEP_TITLE = { intro: 'Welcome to wifi-sentry', sensor: 'Sensor check', learning: 'Learning your home', walk: 'Walk test', alerts: 'Phone alerts', arm: 'Arm it when you leave' };
  let setupSig = '';

  async function loadOnboarding() {
    try { S.onboarding = await api('onboarding'); }
    catch (e) { if (e.status === 404) S.onboarding = null; }
    renderSetup(true);
    renderPrompt();
  }

  async function setOnboarding(body) {
    try { S.onboarding = await api('onboarding', body); }
    catch (e) { toast('Couldn’t save the setup step. ' + e.message); }
    renderSetup(true);
    renderPrompt();
  }

  function sensorCheck() {
    const st = S.status;
    if (!st || !isLive()) return '<p class="fine">Waiting for wifi-sentry…</p>';
    const mac = (st.sources || []).find((x) => x.kind === 'mac');
    const blind = (st.sources || []).find((x) => x.online !== false && x.health);
    const off = st.state === 'offline';
    let tone, head, text;
    if (blind || (off && st.sensing === 'none')) {
      tone = 'bad'; head = 'Can’t sense';
      text = (blind ? sensorName(blind).replace(/^t/, 'T') + ': ' + blind.health + '. ' : 'No data from this Mac’s Wi-Fi yet. ') + 'Nothing can be detected until this is fixed; <code>./wifi-sentry doctor</code> on the Mac says why.';
    } else if (st.sensing === 'degraded') {
      tone = 'warn'; head = 'Degraded sensing';
      text = 'This Mac doesn’t report the signal per antenna, only a smoothed overall strength. Detection will be poor. ESP32 sensor boards are the real fix.';
    } else if (st.sensing === 'reduced') {
      tone = 'warn'; head = 'Reduced sensing';
      text = 'Only one antenna reading is available. Motion is still detected, but radio glitches can’t be filtered out as well, so expect more false alarms.';
    } else if (off || !mac) {
      tone = 'warn'; head = 'Waiting for the sensor';
      text = 'No readings from this Mac’s Wi-Fi yet. If this stays, run <code>./wifi-sentry doctor</code> on the Mac.';
    } else {
      tone = 'ok'; head = 'Full sensing';
      const n = Array.isArray(mac.rssi_chains) ? mac.rssi_chains.length : 2;
      text = 'This Mac reports the Wi-Fi signal at each of its ' + n + ' antennas: the setup wifi-sentry was built and tuned on.';
    }
    return '<div class="check ' + tone + '"><b>' + head + '</b><p>' + text + '</p></div>';
  }

  function learningStep() {
    const st = S.status;
    if (!st || !isLive()) return '<p class="fine">Waiting for wifi-sentry…</p>';
    if (st.state === 'learning') {
      const p = Math.max(0, Math.min(1, Number(st.learning_progress) || 0));
      const left = typeof st.learning_eta_s === 'number' ? st.learning_eta_s - (Date.now() - S.statusAt) / 1000 : null;
      return '<p>It’s learning what your home looks like when nobody moves. Keep it still until this finishes: nobody walking around, fans and robot vacuums off. You can leave this page and come back.</p>' +
        '<div class="bar setup-bar"><span style="width:' + (p * 100).toFixed(1) + '%"></span></div><p class="fine num">' + Math.round(p * 100) + '% done' +
        (left != null ? (left > 5 ? ' · about ' + dur(left, true) + ' left' : ' · almost done') : '') + '</p>';
    }
    if (st.state === 'clear' || st.state === 'motion') return '<div class="check ok"><b>Done</b><p>It knows what your still home looks like. If you move this Mac or the router later, use “Relearn baseline” in Settings.</p></div>';
    return '<p class="fine">Learning starts as soon as the sensor delivers readings.</p>';
  }

  function walkStep() {
    const st = S.status, w = S.walk || {};
    const ready = st && isLive() && (st.state === 'clear' || st.state === 'motion');
    let h = '<p>Find out what this setup can actually see. Start, then walk normally for 30 to 60 seconds through the rooms you want covered, including past this Mac. Nothing is alerted during the test.</p>';
    if (w.running) {
      const e = Math.max(0, serverNow() - (w.started || serverNow()));
      h += '<div class="walk-timer"><span class="num">' + Math.floor(e / 60) + ':' + String(Math.floor(e % 60)).padStart(2, '0') + '</span><span class="fine">' +
        (e < 30 ? 'Keep walking.' : 'That’s enough. Stop when you’re done.') + '</span></div>' +
        '<div class="btn-row"><button class="btn primary" type="button" data-act="walk-stop">Stop and see results</button></div>';
    } else if (w.result && S.walkShowResult) {
      const r = w.result, rec = r.recommended, cur = st && st.sensitivity;
      h += '<div class="check ' + (rec !== 'none' ? 'ok' : 'warn') + '"><b>' + (rec !== 'none' ? 'Recommended: ' + LEVEL_NAME[rec] : 'Your walk wasn’t detected') + '</b><p>' + esc(r.message) + '</p></div>' +
        '<div class="btn-row">' + (rec !== 'none' && rec !== cur ? '<button class="btn primary" type="button" data-act="walk-apply" data-level="' + rec + '">Use ' + LEVEL_NAME[rec] + '</button>' : '') +
        '<button class="btn ghost" type="button" data-act="walk-start">Test again</button></div>' +
        (rec !== 'none' && rec === cur ? '<p class="fine">You’re already on ' + LEVEL_NAME[rec] + '.</p>' : '');
    } else {
      h += '<div class="btn-row"><button class="btn primary" type="button" data-act="walk-start"' + (ready ? '' : ' disabled') + '><svg class="ic"><use href="#i-route"/></svg>Start walk test</button></div>' +
        (ready ? '' : '<p class="fine">Available once learning has finished.</p>');
    }
    return h;
  }

  function alertsStep() {
    const ch = chanOf('ntfy');
    if (!S.chan || !ch) return '<p class="fine">Loading…</p>';
    const others = (S.chan.channels || []).filter((c) => c.id !== 'ntfy' && c.enabled).map((c) => c.label);
    let h;
    if (!ch.settings.topic) {
      h = '<p>Get a push notification on your phone when motion is detected while armed, even when this page is closed. It uses the free ntfy app; no account needed.</p>' +
        privacyNote(ch.settings.server, !!S.chan.settings.base_url) +
        '<div class="btn-row"><button class="btn primary" type="button" data-act="ntfy-setup"><svg class="ic"><use href="#i-phone"/></svg>Set up phone alerts</button></div>';
    } else {
      h = ntfySteps(ch) + '<div class="btn-row"><button class="btn primary" type="button" data-act="test">Send test alert</button></div>' +
        (ch.last && ch.last.kind === 'test' ? '<p class="form-msg ' + (ch.last.ok ? 'ok' : 'err') + '">' + (ch.last.ok ? 'Test delivered ' + whenLabel(ch.last.t) + '. Did it arrive on your phone?' : 'Not delivered: ' + esc(ch.last.error)) + '</p>' : '') +
        privacyNote(ch.settings.server, !!S.chan.settings.base_url);
    }
    h += '<p class="fine">' + (others.length ? 'Also on: ' + others.join(', ') + '. ' : '') + 'Webhook and email are in the Phone alerts panel.</p>';
    return h;
  }

  function armStep() {
    const st = S.status;
    if (!st || !isLive()) return '<p class="fine">Waiting for wifi-sentry…</p>';
    const am = armModel(st);
    const d = st.arm ? st.arm.delay_s : 0;
    return '<p>Alerts only go out while it’s armed. Arm it with the switch at the top when you leave' + (d ? ': you then have ' + dur(d) + ' to get out of the door before it starts alerting' : '') +
      '. Or let it arm itself on a schedule (Settings, Arming).</p>' +
      '<div class="check ' + (am.on && !am.delay ? 'ok' : '') + '"><b>' + esc(am.label) + '</b><p>' + esc(am.delay ? 'Motion until then is only logged. The switch at the top cancels it.' : am.hint) + '</p></div>' +
      (am.on ? '' : '<div class="btn-row"><button class="btn" type="button" data-act="arm-now">Arm now' + (d ? ' (' + dur(d) + ' delay)' : '') + '</button></div>');
  }

  function renderSetup(force) {
    const ob = S.onboarding;
    const show = !!(ob && ob.show) && !S.locked && (S.everLive || isLive());
    $('setup').hidden = !show;
    $('guideSec').hidden = !ob || !!ob.show;
    if (!show) { setupSig = ''; return; }
    const i = Math.max(0, ob.steps.indexOf(ob.step));
    const step = ob.steps[i];
    $('setupStepNo').textContent = 'Step ' + (i + 1) + ' of ' + ob.steps.length;
    $('setupDots').innerHTML = ob.steps.map((s, j) => '<li class="' + (j < i ? 'done' : j === i ? 'cur' : '') + '"></li>').join('');
    $('setupTitle').textContent = STEP_TITLE[step] || step;
    $('setupBack').hidden = i === 0;
    $('setupNext').textContent = i === 0 ? 'Start' : i === ob.steps.length - 1 ? 'Finish' : 'Next';
    const body = $('setupBody');
    if (body.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
    let html;
    if (step === 'intro') {
      html = '<p>wifi-sentry notices people moving in your home from how they disturb the Wi-Fi signal between this Mac and your router. No camera, no microphone.</p>' +
        '<p>It’s a coarse sensor: it reliably sees someone walking near the line between this Mac and the router, and can miss small or distant movement or tell a person from a pet.</p>' +
        '<p class="fine">Setup takes about 15 minutes, most of it waiting while it learns. You can skip any step and come back later from Settings.</p>';
    } else if (step === 'sensor') html = sensorCheck();
    else if (step === 'learning') html = learningStep();
    else if (step === 'walk') html = walkStep();
    else if (step === 'alerts') html = alertsStep();
    else html = armStep();
    const sig = step + '|' + html;
    if (sig === setupSig) return;  // unchanged: leave the DOM (and any focus) alone
    setupSig = sig;
    body.innerHTML = html;
  }

  async function setupAction(act, btn) {
    btn.disabled = true;
    try {
      if (act === 'walk-start') { applyWalk(await api('walktest', { action: 'start' }), true); }
      else if (act === 'walk-stop') { applyWalk(await api('walktest', { action: 'stop' }), true); }
      else if (act === 'walk-apply') { await setSensitivity(btn.dataset.level); }
      else if (act === 'arm-now') { await setArmed(true); }
      else if (act === 'ntfy-setup' || act === 'test') {
        const body = document.querySelector('details.chan[data-kind="ntfy"] [data-body]');
        if (act === 'ntfy-setup') {
          const t = await api('alerts/ntfy-topic', {});
          S.chan = await api('alerts/channels/ntfy', { topic: t.topic, enabled: true });
          renderAlertsPanel(true);
          if (body) body.dataset.built = '';
        } else {
          const r = await api('alerts/channels/ntfy/test', {}, 'POST', 40000);
          if (!r.ok) toast('Not delivered: ' + r.error);
          await loadAlerts();
        }
      }
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; setupSig = ''; renderSetup(true); }
  }

  function wireExtras() {
    // setup card
    $('setupNext').addEventListener('click', () => {
      const ob = S.onboarding; if (!ob) return;
      const i = ob.steps.indexOf(ob.step);
      if (i >= ob.steps.length - 1) { setOnboarding({ status: 'done' }); toast('All set. Settings has the setup guide if you want it again.'); return; }
      setOnboarding({ status: 'active', step: ob.steps[i + 1] });
      $('setup').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });
    $('setupBack').addEventListener('click', () => {
      const ob = S.onboarding; if (!ob) return;
      const i = ob.steps.indexOf(ob.step);
      if (i > 0) setOnboarding({ status: 'active', step: ob.steps[i - 1] });
    });
    $('setupSkip').addEventListener('click', () => { setOnboarding({ status: 'skipped' }); toast('Setup skipped. Settings has it if you want it later.'); });
    $('setupBody').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-act]');
      if (b) setupAction(b.dataset.act, b);
      const c = e.target.closest('button[data-copy]');
      if (c) copyText(c.dataset.copy, c);
    });
    $('guideBtn').addEventListener('click', () => { setOnboarding({ status: 'active', step: 'intro' }); window.scrollTo({ top: 0, behavior: 'smooth' }); });

    // prompts
    $('promptSetup').addEventListener('click', () => openChannel('ntfy'));
    $('promptDismiss').addEventListener('click', () => setOnboarding({ alerts_prompt_dismissed: true }));
    $('alertFailFix').addEventListener('click', () => { const f = S.status && S.status.alerts && S.status.alerts.failing[0]; if (f) openChannel(f.channel); });
    $('bellToPush').addEventListener('click', () => { $('alertsPanel').hidden = true; $('alertsBtn').setAttribute('aria-expanded', 'false'); openChannel('ntfy'); });

    // channels panel (delegated)
    const list = $('chanList');
    list.addEventListener('click', (e) => {
      const c = e.target.closest('button[data-copy]');
      if (c) { copyText(c.dataset.copy, c); return; }
      const u = e.target.closest('button[data-use]');
      if (u) { const inp = u.closest('[data-body]').querySelector('[name="base_url"]'); inp.value = u.dataset.use; u.closest('[data-body]').dataset.dirty = '1'; return; }
      const sw = e.target.closest('button.switch[name]');
      if (sw) { sw.setAttribute('aria-checked', String(sw.getAttribute('aria-checked') !== 'true')); sw.closest('[data-body]').dataset.dirty = '1'; return; }
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const d = b.closest('details.chan');
      chanAction(d.dataset.kind, b.dataset.act, b, d.querySelector('[data-body]'));
    });
    list.addEventListener('input', (e) => { const body = e.target.closest('[data-body]'); if (body) body.dataset.dirty = '1'; });
    list.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || e.target.tagName !== 'INPUT') return;
      e.preventDefault();
      const save = e.target.closest('[data-body]').querySelector('button[data-act="save"], button[data-act="save-opts"]');
      if (save) save.click();
    });

    // arming: exit delay + schedule
    $('delaySelect').addEventListener('change', async () => {
      const sel = $('delaySelect');
      const v = Number(sel.value);
      sel.dataset.busy = '1';
      renderArming();
      if (await saveArming({ delay_s: v })) toast(v ? 'Exit delay ' + dur(v) : 'No exit delay: arming takes effect at once');
      delete sel.dataset.busy;
      renderArming();
    });
    $('schedSwitch').addEventListener('click', async () => {
      const d = S.schedDraft, v = S.arming;
      if (!d || !v) return;
      if (d.enabled) {
        d.enabled = false;
        if (v.settings.schedule.enabled) { await saveArming({ schedule: { enabled: false } }); toast('Schedule off'); }
        else { S.schedDraft = null; renderArming(); }
        return;
      }
      d.enabled = true;
      if (!d.windows.length) d.windows.push({ days: [0, 1, 2, 3, 4], start: '09:00', end: '17:30' });
      d.dirty = true;
      renderArming();
      showMsg($('schedMsg'), true, 'Not active until you save.');
      $('schedMsg').className = 'form-msg';
    });
    $('schedAdd').addEventListener('click', () => {
      const d = S.schedDraft; if (!d) return;
      d.windows.push({ days: [0, 1, 2, 3, 4, 5, 6], start: '22:00', end: '07:00' });
      schedChanged();
    });
    $('schedRows').addEventListener('click', (e) => {
      const row = e.target.closest('.win'); if (!row) return;
      const w = S.schedDraft.windows[Number(row.dataset.i)];
      const day = e.target.closest('button.dow');
      if (day) {
        const di = Number(day.dataset.day);
        w.days = w.days.includes(di) ? w.days.filter((x) => x !== di) : w.days.concat([di]).sort();
        day.setAttribute('aria-pressed', String(w.days.includes(di)));
        S.schedDraft.sig = S.schedDraft.windows.map((x) => x.days.join('') + x.start + x.end).join('|');
        schedChanged();
        return;
      }
      if (e.target.closest('[data-rm]')) { S.schedDraft.windows.splice(Number(row.dataset.i), 1); schedChanged(); }
    });
    $('schedRows').addEventListener('change', (e) => {
      const row = e.target.closest('.win'); const k = e.target.dataset.k;
      if (!row || !k || !e.target.value) return;
      S.schedDraft.windows[Number(row.dataset.i)][k] = e.target.value.slice(0, 5);
      S.schedDraft.sig = S.schedDraft.windows.map((x) => x.days.join('') + x.start + x.end).join('|');
      schedChanged();
    });
    $('schedSave').addEventListener('click', async () => {
      const d = S.schedDraft; if (!d) return;
      if (d.windows.some((w) => !w.days.length)) { showMsg($('schedMsg'), false, 'Every time needs at least one day.'); return; }
      $('schedSave').disabled = true;
      await saveArming({ schedule: { enabled: true, windows: d.windows.map((w) => ({ days: w.days, start: w.start, end: w.end })) } }, $('schedMsg'));
    });
  }

  // ---------- render all ----------
  // A Mac that can't read per-antenna signal (or only one antenna) still scores, but worse: say so.
  function renderSensing() {
    const box = $('sensingNotice');
    const st = S.status;
    const src = isLive() && st ? (st.sources || []).find((x) => x.online !== false && !x.health && x.notice) : null;
    box.hidden = !src;
    if (!src) return;
    const lv = src.notice_level === 'degraded' ? 'degraded' : 'reduced';
    box.dataset.level = lv;
    const text = String(src.notice).replace(/^(degraded|reduced):\s*/, '');
    $('sensingText').textContent = (lv === 'degraded' ? 'Degraded sensing: ' : 'Reduced sensing: ') + text + '.';
  }

  function render() {
    renderHero();
    renderSetup();
    renderPrompt();
    renderAlertFail();
    renderSensing();
    renderSensors();
    renderSettings();
    renderWalk();
    renderEvents();
    renderDigest();
    renderAlerts();
    renderAlertsPanel();
    renderArming();
    $('chartWrap').classList.toggle('stale', !isLive() && (S.everLive || S.failures > 0));
  }

  // ---------- wiring ----------
  function wire() {
    $('armSwitch').addEventListener('click', () => { if (S.status) setArmed(!armModel(S.status).on); });
    $('loginBox').addEventListener('submit', login);
    $('retryBtn').addEventListener('click', () => { S.failures = Math.min(S.failures, 1); connect(); render(); });

    for (const b of $('rangeSeg').querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String(Number(b.dataset.range) === S.range));
      b.addEventListener('click', () => {
        S.range = Number(b.dataset.range);
        store.set('range', S.range);
        for (const o of $('rangeSeg').querySelectorAll('button')) o.setAttribute('aria-pressed', String(o === b));
        chart.setRange(S.range);
        loadHistory();
      });
    }
    chart.setRange(S.range);

    for (const b of $('sensSeg').querySelectorAll('button')) {
      b.addEventListener('click', () => { if (S.status && b.dataset.level !== S.status.sensitivity) setSensitivity(b.dataset.level); });
    }

    $('moreBtn').addEventListener('click', () => loadEvents(true));
    $('digestDismiss').addEventListener('click', () => {
      S.digestDismissed = true;
      markSeen();
      renderDigest();
      lastOrderSig = ''; renderEvents();
    });

    $('relearnBtn').addEventListener('click', () => { $('relearnConfirm').hidden = false; $('relearnBtn').hidden = true; });
    $('relearnCancel').addEventListener('click', () => { $('relearnConfirm').hidden = true; $('relearnBtn').hidden = false; });
    $('relearnGo').addEventListener('click', async () => { await relearn(); $('relearnBtn').hidden = false; });

    $('walkStart').addEventListener('click', walkStart);
    $('walkStop').addEventListener('click', walkStop);
    wireExtras();

    // alerts panel
    const panel = $('alertsPanel'), bell = $('alertsBtn');
    const setPanel = (open) => { panel.hidden = !open; bell.setAttribute('aria-expanded', String(open)); };
    bell.addEventListener('click', (e) => { e.stopPropagation(); setPanel(panel.hidden); });
    $('alertsClose').addEventListener('click', () => { setPanel(false); bell.focus(); });
    document.addEventListener('click', (e) => { if (!panel.hidden && !panel.contains(e.target) && e.target !== bell) setPanel(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !panel.hidden) { setPanel(false); bell.focus(); } });

    $('soundSwitch').addEventListener('click', () => {
      S.alerts.sound = !S.alerts.sound;
      store.set('sound', S.alerts.sound);
      if (S.alerts.sound) { unlockAudio(); setTimeout(() => chime(false), 60); }
      renderAlerts();
    });
    $('vibSwitch').addEventListener('click', () => {
      if (!canVibrate) return;
      S.alerts.vib = !S.alerts.vib;
      store.set('vib', S.alerts.vib);
      if (S.alerts.vib) { try { navigator.vibrate(120); } catch (e) { /* ignore */ } }
      renderAlerts();
    });
    $('testAlert').addEventListener('click', () => {
      unlockAudio();
      setTimeout(() => {
        if (S.alerts.sound || audioReady()) chime(true);
        if (canVibrate) { try { navigator.vibrate([300, 150, 300]); } catch (e) { /* ignore */ } }
      }, 60);
    });
    $('soundResume').addEventListener('click', () => { unlockAudio(); setTimeout(renderAlerts, 100); });
    // any tap re-arms audio if the user wants sound (iOS needs a gesture after each load)
    const gestureUnlock = () => { if (S.alerts.sound && !audioReady()) { unlockAudio(); setTimeout(renderAlerts, 100); } };
    document.addEventListener('touchend', gestureUnlock, { passive: true });
    document.addEventListener('click', gestureUnlock);

    // lifecycle
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        S.hiddenAt = Date.now();
        markSeen();
      } else {
        if (S.hiddenAt && Date.now() - S.hiddenAt > 5 * 60 * 1000) {
          S.summaryFrom = store.get('lastSeen', S.summaryFrom);
          S.digestDismissed = false;
          lastOrderSig = '';
          loadDigestGaps();
        }
        S.hiddenAt = null;
        S.hiddenMotion = null;
        // iOS suspends background pages: the stream is usually dead on return
        if (!isLive() || performance.now() - S.lastTickAt > 2500) { S.failures = Math.min(S.failures, 1); connect(); }
        render();
      }
    });
    window.addEventListener('pagehide', markSeen);
    window.addEventListener('pageshow', (e) => { if (e.persisted) connect(); });
    window.addEventListener('online', () => { if (!isLive()) connect(); });

    // Desktop: the instrument column stays in view while the log scrolls, but only when all of it
    // fits under the bar; otherwise sticking would hide its own bottom until the end of the page.
    const inst = document.querySelector('.col-inst');
    const fit = () => inst.classList.toggle('stick', inst.offsetHeight + 72 <= window.innerHeight);
    new ResizeObserver(fit).observe(inst);
    window.addEventListener('resize', fit);
    const topbar = document.querySelector('.bar');
    const onScroll = () => topbar.classList.toggle('scrolled', window.scrollY > 4);
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  // once a second: watchdog, clocks, durations
  let walkPollAt = 0;
  function everySecond() {
    if (S.conn === 'live' && S.esOpen) {
      const quiet = performance.now() - S.lastTickAt;
      if (quiet > RECONNECT_MS) { fail(S.gen); return; }
      if (quiet > STALE_MS) { setConn('stale'); render(); }
    }
    renderHero();
    if (S.onboarding && S.onboarding.show && ['walk', 'learning', 'arm'].includes(S.onboarding.step)) renderSetup();
    tickOngoingRows();
    renderWalkLive();
    chart.request();
    day.request();
    if (isLive() && Date.now() - dayLoadedAt > 5 * 60 * 1000) loadDay();
    if (S.walk && S.walk.running && isLive() && Date.now() - walkPollAt > 4000) {
      walkPollAt = Date.now();
      api('walktest').then((w) => applyWalk(w, false)).catch(() => {});
    }
    if (isLive() && Date.now() - S.statusAt > 30000) {
      S.statusAt = Date.now();
      api('status').then((st) => { applyStatus(st); renderSensors(); renderSettings(); }).catch(() => {});
    }
    if (isLive() && !document.hidden && Date.now() % 60000 < 1000) markSeen();
    $('chartWrap').classList.toggle('stale', !isLive() && (S.everLive || S.failures > 0));
    $('chartEmpty').hidden = !chart.isEmpty() || !S.everLive;
  }

  // Arriving from a ?token= link the server rejected (it redirects here with a reason in the hash).
  if (location.hash === '#token-rejected' || location.hash === '#too-many-attempts') {
    S.loginMsg = location.hash === '#token-rejected'
      ? 'The token in that link was not accepted (it may have been changed). Run ./wifi-sentry url on the Mac for the current link, or paste the token.'
      : 'Too many wrong tokens from this device. Wait a few minutes, then try again.';
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* ignore */ }
  }

  wire();
  render();
  connect();
  setInterval(everySecond, 1000);
})();
