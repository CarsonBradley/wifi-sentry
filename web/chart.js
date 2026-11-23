/* Hand-drawn canvas charts: activity score against the trigger level, with event bands and
   "not watching" gaps. SentryChart is the strip chart (selectable range); DayChart is the 24-hour
   circular chart, drawn like a chart recorder's disc: the pen sits at 12 o'clock and the paper turns
   once a day, so the day behind you runs counterclockwise from the top. */
(function () {
  'use strict';

  const BUCKETS = 600;
  const TAU = Math.PI * 2;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function niceStep(span, target) {
    const raw = span / Math.max(1, target);
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
    return 10 * p;
  }

  function lowerBound(arr, x) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < x) lo = mid + 1; else hi = mid; }
    return lo;
  }

  function readColors() {
    return {
      pen: cssVar('--pen'), motion: cssVar('--motion'), grid: cssVar('--grid'), gridFaint: cssVar('--grid-faint'),
      print: cssVar('--print'), ink: cssVar('--ink'), ink3: cssVar('--ink-3'), ground: cssVar('--ground'), watch: cssVar('--watch'),
      ev: cssVar('--motion-band'), evQuiet: cssVar('--quiet-band'),
      gap: cssVar('--watch-hatch'), gapBg: cssVar('--watch-hatch-bg'),
    };
  }

  // Shared plumbing: data series, live bucketing, gaps, hatch pattern, resize, colour-scheme changes.
  class Base {
    constructor(canvas, tip, opts) {
      this.canvas = canvas;
      this.tip = tip;
      this.ctx = canvas.getContext('2d');
      this.now = opts.now;
      this.opts = opts;
      this.t = []; this.s = []; this.th = [];
      this.lastBucket = -Infinity;
      this.events = [];
      this.gaps = [];
      this.hover = null;
      this.w = 0; this.h = 0;
      this.pending = false;
      this.colors = null;
      new ResizeObserver(() => this.resize()).observe(canvas);
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const onScheme = () => { this.colors = null; this._hatch = null; this.request(); };
      if (mq.addEventListener) mq.addEventListener('change', onScheme); else mq.addListener(onScheme);
      canvas.addEventListener('pointermove', (e) => this.onPointer(e));
      canvas.addEventListener('pointerdown', (e) => this.onPointer(e));
      canvas.addEventListener('pointerleave', () => this.clearHover());
      canvas.addEventListener('pointercancel', () => this.clearHover());
      canvas.addEventListener('pointerup', (e) => { if (e.pointerType !== 'mouse') setTimeout(() => this.clearHover(), 1800); });
    }

    get step() { return this.range / this.buckets; }

    setData(h) {
      const t = (h && h.t) || [], s = (h && h.score) || [], th = (h && h.threshold) || [];
      const n = Math.min(t.length, s.length);
      this.t = []; this.s = []; this.th = [];
      for (let i = 0; i < n; i++) {
        if (typeof t[i] !== 'number' || s[i] == null) continue;
        this.t.push(t[i]); this.s.push(Number(s[i])); this.th.push(th[i] != null ? Number(th[i]) : NaN);
      }
      this.lastBucket = this.t.length ? this.t[this.t.length - 1] : -Infinity;
      this.gaps = Array.isArray(h && h.gaps) ? h.gaps.filter((g) => typeof g.start === 'number') : [];
      this.request();
    }

    push(t, score, thr) {
      if (typeof t !== 'number' || score == null) return;
      const n = this.t.length;
      if (n && t <= this.t[n - 1] - 0.001 && t < this.lastBucket) return;
      if (n && t - this.lastBucket < this.step) {
        this.s[n - 1] = Math.max(this.s[n - 1], score);
        this.th[n - 1] = thr;
        this.t[n - 1] = t;
      } else {
        this.t.push(t); this.s.push(score); this.th.push(thr);
        this.lastBucket = t;
      }
      const cut = lowerBound(this.t, t - this.range - this.step * 2);
      if (cut > 0) { this.t.splice(0, cut); this.s.splice(0, cut); this.th.splice(0, cut); }
      this.request();
    }

    setEvents(evs) { this.events = evs; this.request(); }

    isEmpty() {
      const t0 = this.now() - this.range;
      return !this.t.length || this.t[this.t.length - 1] < t0;
    }

    gapEnd(g, t1) { return g.ongoing || typeof g.end !== 'number' ? t1 : g.end; }

    // The gap covering time t, if any. Learning gaps still have a (provisional) score worth seeing;
    // offline / engine-down stretches have none, so `blindOnly` skips the learning ones.
    gapAt(t, blindOnly) {
      const t1 = this.now();
      for (const g of this.gaps) {
        if (blindOnly && g.reason === 'learning') continue;
        if (t > g.start + this.step && t < this.gapEnd(g, t1)) return g;
      }
      return null;
    }

    // Runs of indices to draw as one line: broken on missing data and inside blind gaps.
    segments(i0) {
      const gap = Math.max(this.step * 3.5, 6);
      const segs = [];
      let cur = null;
      for (let i = i0; i < this.t.length; i++) {
        if (this.gaps.length && this.gapAt(this.t[i], true)) { if (cur) segs.push(cur); cur = null; continue; }
        if (cur && this.t[i] - this.t[cur[cur.length - 1]] > gap) { segs.push(cur); cur = null; }
        if (!cur) cur = [];
        cur.push(i);
      }
      if (cur) segs.push(cur);
      return segs;
    }

    lastThreshold() {
      for (let i = this.th.length - 1; i >= 0; i--) if (this.th[i] > 0) return this.th[i];
      return NaN;
    }

    hatch(color) {
      if (this._hatch && this._hatchColor === color) return this._hatch;
      const dpr = Math.min(3, window.devicePixelRatio || 1);
      const n = Math.round(7 * dpr);
      const c = document.createElement('canvas');
      c.width = c.height = n;
      const x = c.getContext('2d');
      x.strokeStyle = color; x.lineWidth = Math.max(1, dpr);
      x.beginPath(); x.moveTo(0, n); x.lineTo(n, 0); x.moveTo(-n / 2, n / 2); x.lineTo(n / 2, -n / 2); x.moveTo(n / 2, n * 1.5); x.lineTo(n * 1.5, n / 2); x.stroke();
      const pat = this.ctx.createPattern(c, 'repeat');
      if (pat && pat.setTransform && typeof DOMMatrix === 'function') pat.setTransform(new DOMMatrix().scale(1 / dpr));
      this._hatch = pat; this._hatchColor = color;
      return pat;
    }

    resize() {
      const r = this.canvas.getBoundingClientRect();
      const dpr = Math.min(3, window.devicePixelRatio || 1);
      this.w = r.width; this.h = r.height;
      this.canvas.width = Math.round(r.width * dpr);
      this.canvas.height = Math.round(r.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }

    request() {
      if (this.pending) return;
      this.pending = true;
      requestAnimationFrame(() => { this.pending = false; this.draw(); });
    }

    showTip(html, x, y) {
      this.tip.innerHTML = html;
      this.tip.hidden = false;
      const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
      this.tip.style.left = Math.max(0, Math.min(this.w - tw, x - tw / 2)) + 'px';
      this.tip.style.top = Math.max(0, Math.min(this.h - th, y)) + 'px';
    }

    onPointer(e) {
      const r = this.canvas.getBoundingClientRect();
      this.hover = { x: Math.max(0, Math.min(r.width, e.clientX - r.left)), y: Math.max(0, Math.min(r.height, e.clientY - r.top)) };
      this.request();
    }

    clearHover() { this.hover = null; this.tip.hidden = true; this.request(); }

    nearest(t) {
      let i = lowerBound(this.t, t);
      if (i >= this.t.length) i = this.t.length - 1;
      if (i > 0 && Math.abs(this.t[i - 1] - t) < Math.abs(this.t[i] - t)) i--;
      if (i < 0 || Math.abs(this.t[i] - t) > Math.max(this.step * 3, 5)) return -1;
      return i;
    }
  }

  // ---------- strip chart ----------
  class SentryChart extends Base {
    constructor(canvas, tip, opts) {
      super(canvas, tip, opts);
      this.range = 3600;
      this.buckets = BUCKETS;
    }

    setRange(seconds) { this.range = seconds; this.request(); }

    draw() {
      const ctx = this.ctx, W = this.w, H = this.h;
      if (!W || !H) return;
      const c = this.colors || (this.colors = readColors());
      const padL = 0, padR = 34, padT = 8, padB = 24;
      const t1 = this.now(), t0 = t1 - this.range;
      const pw = W - padL - padR, ph = H - padT - padB;
      ctx.clearRect(0, 0, W, H);

      const i0 = Math.max(0, lowerBound(this.t, t0) - 1);
      let maxS = 0, maxT = 0;
      for (let i = i0; i < this.t.length; i++) {
        if (this.s[i] > maxS) maxS = this.s[i];
        if (this.th[i] > maxT) maxT = this.th[i];
      }
      if (!maxT) maxT = this.lastThreshold() || 1;
      const yMax = Math.max(1, maxT * 1.6, Math.min(maxS * 1.1, maxT * 4));
      const X = (t) => padL + ((t - t0) / this.range) * pw;
      const Y = (v) => padT + ph - (Math.min(v, yMax) / yMax) * ph;
      this.geo = { padL, padT, pw, ph, t0, X, Y };

      const font = getComputedStyle(document.body).fontFamily;
      ctx.font = '11px ' + font;
      ctx.lineWidth = 1;

      // graticule: horizontal values, vertical time ticks
      ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
      const ys = niceStep(yMax, 3);
      for (let v = 0; v <= yMax + 1e-9; v += ys) {
        const y = Math.round(Y(v)) + 0.5;
        ctx.strokeStyle = v === 0 ? c.grid : c.gridFaint;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + pw, y); ctx.stroke();
        if (Math.abs(v - maxT) > ys * 0.3) {
          ctx.fillStyle = c.ink3;
          ctx.fillText(v % 1 ? v.toFixed(1) : String(v), padL + pw + 8, y);
        }
      }
      const narrow = pw < 480;
      const tickStep = { 900: 300, 3600: narrow ? 900 : 600, 21600: narrow ? 7200 : 3600, 86400: narrow ? 21600 : 10800 }[this.range]
        || niceStep(this.range, 5);
      const tzOff = -new Date(t1 * 1000).getTimezoneOffset() * 60;
      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
      for (let tt = Math.ceil((t0 + tzOff) / tickStep) * tickStep - tzOff; tt <= t1; tt += tickStep) {
        const x = Math.round(X(tt)) + 0.5;
        ctx.strokeStyle = c.gridFaint;
        ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + ph); ctx.stroke();
        ctx.strokeStyle = c.grid;
        ctx.beginPath(); ctx.moveTo(x, padT + ph); ctx.lineTo(x, padT + ph + 4); ctx.stroke();
        const label = this.opts.fmtTick(tt, this.range);
        const tw = ctx.measureText(label).width / 2;
        if (x - tw < 0 || x + tw > padL + pw + 4) continue;
        ctx.fillStyle = c.print;
        ctx.fillText(label, x, H - 6);
      }

      // event bands
      for (const ev of this.events) {
        const s = ev.start, e = ev.end == null ? t1 : ev.end;
        if (e < t0 || s > t1) continue;
        const x0 = X(Math.max(s, t0)), x1 = X(Math.min(e, t1));
        ctx.fillStyle = ev.armed ? c.ev : c.evQuiet;
        ctx.fillRect(x0 - 1, padT, Math.max(3, x1 - x0 + 2), ph);
      }

      // coverage gaps: "not watching" (hatched)
      for (const gp of this.gaps) {
        const s = gp.start, e = this.gapEnd(gp, t1);
        if (e < t0 || s > t1) continue;
        const x0 = X(Math.max(s, t0)), x1 = X(Math.min(e, t1));
        const w = Math.max(2, x1 - x0);
        ctx.fillStyle = c.gapBg; ctx.fillRect(x0, padT, w, ph);
        const pat = this.hatch(c.gap);
        if (pat) { ctx.fillStyle = pat; ctx.fillRect(x0, padT, w, ph); }
      }

      // trigger level, labelled on the value axis
      const thrPath = new Path2D();
      let started = false;
      for (let i = i0; i < this.t.length; i++) {
        const v = isNaN(this.th[i]) ? maxT : this.th[i];
        const x = X(this.t[i]), y = Y(v);
        if (!started) { thrPath.moveTo(Math.min(x, padL), y); started = true; }
        thrPath.lineTo(x, y);
      }
      const lastV = this.th.length && !isNaN(this.th[this.th.length - 1]) ? this.th[this.th.length - 1] : maxT;
      if (!started) thrPath.moveTo(padL, Y(lastV));
      thrPath.lineTo(padL + pw, Y(lastV));
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillStyle = c.motion; ctx.font = '600 11px ' + font;
      ctx.fillText(lastV % 1 ? lastV.toFixed(1) : String(lastV), padL + pw + 8, Y(lastV));
      ctx.font = '11px ' + font;

      if (this.t.length) {
        const segs = this.segments(i0);
        const line = new Path2D();
        for (const seg of segs) {
          seg.forEach((i, k) => { const x = X(this.t[i]), y = Y(this.s[i]); if (k === 0) line.moveTo(x, y); else line.lineTo(x, y); });
          if (seg.length === 1) line.lineTo(X(this.t[seg[0]]) + 1, Y(this.s[seg[0]]));
        }
        const above = new Path2D(thrPath); above.lineTo(padL + pw, 0); above.lineTo(padL - 10, 0); above.closePath();
        const below = new Path2D(thrPath); below.lineTo(padL + pw, H); below.lineTo(padL - 10, H); below.closePath();
        ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        ctx.lineWidth = 1.25;
        ctx.save(); ctx.clip(below); ctx.strokeStyle = c.pen; ctx.stroke(line); ctx.restore();
        ctx.save(); ctx.clip(above); ctx.strokeStyle = c.motion; ctx.lineWidth = 1.75; ctx.stroke(line); ctx.restore();
      }

      ctx.strokeStyle = c.motion; ctx.lineWidth = 1.25;
      ctx.stroke(thrPath);

      // pen position: latest point
      const li = this.t.length - 1;
      if (li >= 0 && t1 - this.t[li] < Math.max(this.step * 3.5, 6) && !this.gapAt(this.t[li], true)) {
        ctx.fillStyle = this.s[li] > (this.th[li] || maxT) ? c.motion : c.pen;
        ctx.beginPath(); ctx.arc(X(this.t[li]), Y(this.s[li]), 3, 0, TAU); ctx.fill();
      }

      if (this.hover) this.drawHover(c);
    }

    drawHover(c) {
      const ctx = this.ctx, g = this.geo;
      const hx = Math.max(g.padL, Math.min(g.padL + g.pw, this.hover.x));
      const t = g.t0 + ((hx - g.padL) / g.pw) * this.range;
      const inGap = this.gapAt(t, true) || this.gapAt(t, false);
      ctx.strokeStyle = c.ink3; ctx.lineWidth = 1;
      if (inGap && this.opts.fmtGap && (inGap.reason !== 'learning' || !this.t.length)) {
        ctx.beginPath(); ctx.moveTo(Math.round(hx) + 0.5, g.padT); ctx.lineTo(Math.round(hx) + 0.5, g.padT + g.ph); ctx.stroke();
        this.showTip(this.opts.fmtGap(inGap), hx, 0);
        return;
      }
      const i = this.nearest(t);
      if (i < 0) { this.tip.hidden = true; return; }
      const x = g.X(this.t[i]), y = g.Y(this.s[i]);
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, g.padT); ctx.lineTo(Math.round(x) + 0.5, g.padT + g.ph); ctx.stroke();
      ctx.fillStyle = c.ink; ctx.beginPath(); ctx.arc(x, y, 3.5, 0, TAU); ctx.fill();
      const learnGap = inGap && inGap.reason === 'learning';
      this.showTip(this.opts.fmtTip(this.t[i], this.s[i], this.th[i]) + (learnGap ? '<br>Learning: not detecting yet' : ''),
        x, y < g.padT + 44 ? g.padT + g.ph - 40 : 0);
    }
  }

  // ---------- 24-hour circular chart ----------
  class DayChart extends Base {
    constructor(canvas, tip, opts) {
      super(canvas, tip, opts);
      this.range = 86400;
      this.buckets = 720;          // 2-minute buckets
      this.live = null;            // {score, thr} at the pen, or null when not watching / not connected
    }

    setLive(v) {
      const same = (a, b) => a === b || (a && b && a.score === b.score && a.thr === b.thr);
      if (!same(v, this.live)) { this.live = v; this.request(); }
    }

    geometry() {
      const S = Math.min(this.w, this.h);
      const m = this.labelWidth() + 16;   // room for the hour labels at 3 and 9 o'clock
      const R = S / 2 - m, r0 = R * 0.36;
      const thr = (this.live && this.live.thr) || this.lastThreshold() || 1;
      const vMax = thr * 4;      // square-root scale: the trigger ring sits halfway across the band
      const t1 = this.now();
      const rOf = (v) => r0 + (R - r0) * Math.sqrt(Math.max(0, Math.min(v, vMax)) / vMax);
      const aOf = (t) => -Math.PI / 2 - ((t1 - t) / this.range) * TAU;
      return { cx: this.w / 2, cy: this.h / 2, R, r0, m, thr, t1, t0: t1 - this.range, rOf, aOf };
    }

    // Widest hour label in this locale ("12 PM", "Mon", "18"...), measured once.
    labelWidth() {
      if (this._lw) return this._lw;
      const ctx = this.ctx;
      ctx.font = '11px ' + getComputedStyle(document.body).fontFamily;
      const d = new Date(); d.setMinutes(0, 0, 0);
      let w = ctx.measureText('now').width;
      for (let h = 0; h < 24; h += 3) { d.setHours(h); w = Math.max(w, ctx.measureText(this.opts.fmtHour(d.getTime() / 1000)).width); }
      return (this._lw = Math.ceil(w));
    }

    sector(g, a0, a1) {
      const p = new Path2D();
      p.arc(g.cx, g.cy, g.R, a0, a1, false);
      p.arc(g.cx, g.cy, g.r0, a1, a0, true);
      p.closePath();
      return p;
    }

    draw() {
      const ctx = this.ctx, W = this.w, H = this.h;
      if (!W || !H) return;
      const c = this.colors || (this.colors = readColors());
      const g = this.geo = this.geometry();
      const { cx, cy, R, r0, t0, t1, rOf, aOf } = g;
      const P = (a, r) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
      ctx.clearRect(0, 0, W, H);
      const font = getComputedStyle(document.body).fontFamily;

      // event sectors (at least ~1.2 degrees wide, so a 10-second event is still visible)
      const minA = TAU / 300;
      for (const ev of this.events) {
        const s = ev.start, e = ev.end == null ? t1 : ev.end;
        if (e < t0 || s > t1) continue;
        let a0 = aOf(Math.max(s, t0)), a1 = aOf(Math.min(e, t1));
        if (a1 - a0 < minA) { const mid = (a0 + a1) / 2; a0 = mid - minA / 2; a1 = Math.min(mid + minA / 2, aOf(t1)); }
        ctx.fillStyle = ev.armed ? c.ev : c.evQuiet;
        ctx.fill(this.sector(g, a0, a1));
      }

      // not watching
      for (const gp of this.gaps) {
        const s = Math.max(gp.start, t0), e = Math.min(this.gapEnd(gp, t1), t1);
        if (e <= s) continue;
        let a0 = aOf(s), a1 = aOf(e);
        if (a1 - a0 < minA / 2) a1 = a0 + minA / 2;
        const p = this.sector(g, a0, a1);
        ctx.fillStyle = c.gapBg; ctx.fill(p);
        const pat = this.hatch(c.gap);
        if (pat) { ctx.fillStyle = pat; ctx.fill(p); }
      }

      // graticule: hour lines and rings, printed in chart green
      const tzOff = -new Date(t1 * 1000).getTimezoneOffset() * 60;
      const firstHour = Math.ceil((t0 + tzOff) / 3600) * 3600 - tzOff;
      ctx.lineWidth = 1;
      for (let tt = firstHour; tt <= t1; tt += 3600) {
        const a = aOf(tt);
        const local = Math.round((tt + tzOff) / 3600) % 24;
        const major = local % 3 === 0;
        ctx.strokeStyle = major ? c.grid : c.gridFaint;
        const [x0, y0] = P(a, r0), [x1, y1] = P(a, R);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
        // a tick for every hour outside the rim, longer every 3 hours
        ctx.strokeStyle = c.grid;
        const [x2, y2] = P(a, R + (major ? 7 : 4));
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      }
      for (const [f, col] of [[0, c.grid], [0.25, c.gridFaint], [0.75, c.gridFaint], [1, c.grid]]) {
        ctx.strokeStyle = col;
        ctx.beginPath(); ctx.arc(cx, cy, r0 + (R - r0) * f, 0, TAU); ctx.stroke();
      }

      // hour labels every 3 hours, around the outside; "now" at the pen
      // Anchored on the side facing the dial, so a label never runs off the canvas at 3 or 9 o'clock.
      ctx.font = '11px ' + font; ctx.textBaseline = 'middle';
      const lr = R + 12;
      for (let tt = firstHour; tt <= t1; tt += 3600) {
        const local = Math.round((tt + tzOff) / 3600) % 24;
        if (local % 3) continue;
        const a = aOf(tt);
        const fromTop = (t1 - tt) / this.range * TAU;
        if (fromTop < 0.25 || fromTop > TAU - 0.25) continue;   // keep clear of "now"
        const cos = Math.cos(a), sin = Math.sin(a);
        ctx.textAlign = cos > 0.35 ? 'left' : cos < -0.35 ? 'right' : 'center';
        const [x, y] = P(a, lr + (ctx.textAlign === 'center' ? 4 * Math.abs(sin) : 0));
        ctx.fillStyle = c.print;
        ctx.fillText(this.opts.fmtHour(tt), x, y);
      }
      ctx.textAlign = 'center';
      ctx.fillStyle = c.ink; ctx.font = '600 11px ' + font;
      ctx.fillText('now', cx, cy - lr - 2);

      // the trace
      const i0 = Math.max(0, lowerBound(this.t, t0) - 1);
      const tr = rOf(g.thr);
      if (this.t.length) {
        const segs = this.segments(i0);
        const line = new Path2D();
        for (const seg of segs) {
          seg.forEach((i, k) => { const [x, y] = P(aOf(this.t[i]), rOf(this.s[i])); if (k === 0) line.moveTo(x, y); else line.lineTo(x, y); });
        }
        const inside = new Path2D(); inside.arc(cx, cy, tr, 0, TAU);
        const outside = new Path2D(); outside.rect(0, 0, W, H); outside.arc(cx, cy, tr, 0, TAU, true);
        ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        ctx.save(); ctx.clip(inside); ctx.strokeStyle = c.pen; ctx.lineWidth = 0.9; ctx.stroke(line); ctx.restore();
        ctx.save(); ctx.clip(outside); ctx.strokeStyle = c.motion; ctx.lineWidth = 1.75; ctx.stroke(line); ctx.restore();
      }

      // trigger ring, with its value printed on it like the strip chart's axis
      ctx.strokeStyle = c.motion; ctx.lineWidth = 1.25;
      ctx.beginPath(); ctx.arc(cx, cy, tr, 0, TAU); ctx.stroke();
      {
        const a = -Math.PI / 2 + 0.42, [x, y] = P(a, tr);
        const label = g.thr % 1 ? g.thr.toFixed(1) : String(g.thr);
        ctx.font = '600 11px ' + font; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        const w = ctx.measureText(label).width + 8;
        ctx.fillStyle = c.ground; ctx.fillRect(x - w / 2, y - 8, w, 16);
        ctx.fillStyle = c.motion; ctx.fillText(label, x, y + 0.5);
      }

      // the day's record on the rim: vermilion where it saw motion, ochre where it wasn't watching
      ctx.lineWidth = 5; ctx.lineCap = 'butt';
      const rimR = R - 3.5;
      const minRim = TAU / 160;   // at least 9 minutes wide, so a short event or gap still shows on a phone
      for (const gp of this.gaps) {
        const s = Math.max(gp.start, t0), e = Math.min(this.gapEnd(gp, t1), t1);
        if (e <= s) continue;
        let a0 = aOf(s), a1 = aOf(e);
        if (a1 - a0 < minRim) { const mid = (a0 + a1) / 2; a0 = mid - minRim / 2; a1 = mid + minRim / 2; }
        // striped, not solid: "not watching" differs from motion by form as well as colour
        ctx.strokeStyle = c.watch; ctx.setLineDash([2, 2]);
        ctx.beginPath(); ctx.arc(cx, cy, rimR, a0, a1); ctx.stroke();
        ctx.setLineDash([]); ctx.lineWidth = 1;
        for (const r of [rimR - 2.5, rimR + 2.5]) { ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke(); }
        ctx.lineWidth = 5;
      }
      for (const ev of this.events) {
        const s = ev.start, e = ev.end == null ? t1 : ev.end;
        if (e < t0 || s > t1) continue;
        let a0 = aOf(Math.max(s, t0)), a1 = aOf(Math.min(e, t1));
        if (a1 - a0 < minRim) { const mid = (a0 + a1) / 2; a0 = mid - minRim / 2; a1 = Math.min(mid + minRim / 2, aOf(t1)); }
        ctx.strokeStyle = ev.armed ? c.motion : c.ink3;
        ctx.beginPath(); ctx.arc(cx, cy, rimR, a0, a1); ctx.stroke();
      }
      ctx.lineCap = 'round';

      // the pen: fixed at 12 o'clock, its tip at the live value
      const top = -Math.PI / 2;
      ctx.strokeStyle = c.ink3; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, cy - r0); ctx.lineTo(cx, cy - R - 6); ctx.stroke();
      if (this.live && typeof this.live.score === 'number') {
        const hot = this.live.score > g.thr;
        const [px, py] = P(top, rOf(this.live.score));
        ctx.strokeStyle = hot ? c.motion : c.ink; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(cx, cy - r0); ctx.lineTo(px, py); ctx.stroke();
        ctx.fillStyle = hot ? c.motion : c.ink;
        ctx.beginPath(); ctx.arc(px, py, 4, 0, TAU); ctx.fill();
      }

      if (this.hover) this.drawHover(c);
    }

    drawHover(c) {
      const ctx = this.ctx, g = this.geo;
      const dx = this.hover.x - g.cx, dy = this.hover.y - g.cy;
      const d = Math.hypot(dx, dy);
      if (d < g.r0 * 0.6 || d > g.R + g.m) { this.tip.hidden = true; return; }
      let back = (-Math.PI / 2 - Math.atan2(dy, dx)) % TAU;
      if (back < 0) back += TAU;
      const t = g.t1 - (back / TAU) * this.range;
      const a = g.aOf(t);
      ctx.strokeStyle = c.ink3; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(g.cx + g.r0 * Math.cos(a), g.cy + g.r0 * Math.sin(a)); ctx.lineTo(g.cx + g.R * Math.cos(a), g.cy + g.R * Math.sin(a)); ctx.stroke();
      const tipY = this.hover.y > g.cy ? this.hover.y - 70 : this.hover.y + 24;
      const win = this.step * 1.5;
      const ev = this.events.find((e) => t >= e.start - win && t <= (e.end == null ? g.t1 : e.end) + win);
      if (ev && this.opts.fmtEvent) { this.showTip(this.opts.fmtEvent(ev), this.hover.x, tipY); return; }
      const gap = this.gapAt(t, true);
      if (gap && this.opts.fmtGap) { this.showTip(this.opts.fmtGap(gap), this.hover.x, tipY); return; }
      const i = this.nearest(t);
      if (i < 0) { this.tip.hidden = true; return; }
      const r = g.rOf(this.s[i]);
      ctx.fillStyle = c.ink; ctx.beginPath(); ctx.arc(g.cx + r * Math.cos(a), g.cy + r * Math.sin(a), 3.5, 0, TAU); ctx.fill();
      this.showTip(this.opts.fmtTip(this.t[i], this.s[i], this.th[i]), this.hover.x, tipY);
    }
  }

  window.SentryChart = SentryChart;
  window.DayChart = DayChart;
})();
