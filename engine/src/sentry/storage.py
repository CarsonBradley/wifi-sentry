"""SQLite storage (events, key/value state, baselines, 1 Hz score history) and raw-sample recording."""
from __future__ import annotations

import gzip
import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("sentry.storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start REAL NOT NULL,
  end REAL,
  peak_score REAL NOT NULL,
  sources TEXT NOT NULL,
  armed INTEGER NOT NULL,
  label TEXT,
  note TEXT NOT NULL DEFAULT '',
  walktest INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS history (t REAL PRIMARY KEY, score REAL NOT NULL, threshold REAL NOT NULL, state TEXT);
CREATE TABLE IF NOT EXISTS history_src (t REAL NOT NULL, src TEXT NOT NULL, score REAL, PRIMARY KEY (t, src));
CREATE TABLE IF NOT EXISTS gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL NOT NULL, end REAL, reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS gaps_end ON gaps(end);
CREATE TABLE IF NOT EXISTS walktests (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, result TEXT, raw_path TEXT);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(events)")}
        if "threshold" not in cols:  # migration for databases created before the column existed
            self.db.execute("ALTER TABLE events ADD COLUMN threshold REAL")
        self._hist: list = []
        self._hist_src: list = []
        self._last_flush = time.monotonic()

    def close(self) -> None:
        self.flush_history()
        self.db.close()

    # ---------------------------------------------------------------- events
    @staticmethod
    def _row(r: sqlite3.Row, now: float | None = None) -> dict:
        end = r["end"]
        ref = end if end is not None else (now if now is not None else time.time())
        return {
            "id": r["id"],
            "start": r["start"],
            "end": end,
            "duration_s": round(max(0.0, ref - r["start"]), 1),
            "peak_score": r["peak_score"],
            "threshold": r["threshold"],
            "sources": json.loads(r["sources"]),
            "armed": bool(r["armed"]),
            "label": r["label"],
            "note": r["note"] or "",
            "walktest": bool(r["walktest"]),
        }

    def create_event(self, ev: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO events(start, end, peak_score, threshold, sources, armed, label, note, walktest) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ev["start"], ev.get("end"), ev["peak_score"], ev.get("threshold"), json.dumps(ev["sources"]),
             int(ev["armed"]), ev.get("label"), ev.get("note") or "", int(bool(ev.get("walktest")))))
        return int(cur.lastrowid)

    def update_event(self, ev: dict) -> None:
        # label/note are owned by the user once set via the API; the detector only appends notes
        self.db.execute(
            "UPDATE events SET start=?, end=?, peak_score=?, threshold=?, sources=?, walktest=?, "
            "note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
            (ev["start"], ev.get("end"), ev["peak_score"], ev.get("threshold"), json.dumps(ev["sources"]),
             int(bool(ev.get("walktest"))),
             ev.get("note") or "", ev.get("note") or "", ev["id"]))

    def get_event(self, eid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return self._row(r) if r else None

    def list_events(self, limit: int = 50, before: int | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        if before is not None:
            rows = self.db.execute("SELECT * FROM events WHERE id<? ORDER BY id DESC LIMIT ?", (before, limit))
        else:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        now = time.time()
        return [self._row(r, now) for r in rows.fetchall()]

    def label_event(self, eid: int, label, note: str | None) -> dict | None:
        if note is None:
            self.db.execute("UPDATE events SET label=? WHERE id=?", (label, eid))
        else:
            self.db.execute("UPDATE events SET label=?, note=? WHERE id=?", (label, note, eid))
        return self.get_event(eid)

    def last_event(self) -> dict | None:
        r = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def close_dangling(self, end: float | None) -> int:
        """Events left open by a crash/kill end at the last heartbeat (or their start), noted."""
        cur = self.db.execute(
            "UPDATE events SET end=MAX(start, COALESCE(?, start)), note=CASE WHEN note='' THEN "
            "'engine stopped while open' ELSE note || '; engine stopped while open' END WHERE end IS NULL", (end,))
        return cur.rowcount

    # ---------------------------------------------------------------- kv
    def kv_get(self, k: str, default=None):
        r = self.db.execute("SELECT v, updated FROM kv WHERE k=?", (k,)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["v"])
        except json.JSONDecodeError:
            return default

    def kv_updated(self, k: str) -> float | None:
        r = self.db.execute("SELECT updated FROM kv WHERE k=?", (k,)).fetchone()
        return r["updated"] if r else None

    def kv_set(self, k: str, v) -> None:
        self.db.execute("INSERT INTO kv(k, v, updated) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                        "updated=excluded.updated", (k, json.dumps(v), time.time()))

    # ---------------------------------------------------------------- history
    def add_history(self, t: float, score: float, threshold: float, state: str, per: dict) -> None:
        self._hist.append((round(t, 3), round(score, 4), threshold, state))
        for sid, s in per.items():
            self._hist_src.append((round(t, 3), sid, None if s is None else round(s, 4)))
        if time.monotonic() - self._last_flush >= 5.0:
            self.flush_history()

    def flush_history(self) -> None:
        if self._hist:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR REPLACE INTO history(t, score, threshold, state) VALUES (?,?,?,?)", self._hist)
            self.db.executemany("INSERT OR REPLACE INTO history_src(t, src, score) VALUES (?,?,?)", self._hist_src)
            self.db.execute("COMMIT")
        self._hist, self._hist_src = [], []
        self._last_flush = time.monotonic()

    def history(self, seconds: float, points: int, now: float | None = None) -> dict:
        self.flush_history()
        now = now if now is not None else time.time()
        seconds = max(10.0, min(float(seconds), 30 * 86400.0))
        points = max(10, min(int(points), 5000))
        t0 = now - seconds
        bw = seconds / points
        rows = self.db.execute(
            "SELECT CAST((t - ?) / ? AS INTEGER) AS b, MAX(t) AS t, MAX(score) AS s, MAX(threshold) AS th "
            "FROM history WHERE t >= ? AND t <= ? GROUP BY b ORDER BY b", (t0, bw, t0, now)).fetchall()
        out_t = [round(t0 + (r["b"] + 1) * bw, 3) for r in rows]
        idx = {r["b"]: i for i, r in enumerate(rows)}
        per: dict[str, list] = {}
        for r in self.db.execute(
                "SELECT CAST((t - ?) / ? AS INTEGER) AS b, src, MAX(score) AS s FROM history_src "
                "WHERE t >= ? AND t <= ? GROUP BY b, src", (t0, bw, t0, now)).fetchall():
            i = idx.get(r["b"])
            if i is None:
                continue
            per.setdefault(r["src"], [None] * len(rows))[i] = r["s"]
        return {"t": out_t, "score": [r["s"] for r in rows], "threshold": [r["th"] for r in rows],
                "per_source": per, "gaps": self.gaps(t0, now)}

    def prune(self, keep_days: float, now: float | None = None) -> None:
        self.flush_history()
        cut = (now or time.time()) - keep_days * 86400
        self.db.execute("DELETE FROM history WHERE t < ?", (cut,))
        self.db.execute("DELETE FROM history_src WHERE t < ?", (cut,))
        self.db.execute("DELETE FROM gaps WHERE end IS NOT NULL AND end < ?", (cut,))

    # ---------------------------------------------------------------- coverage gaps
    def open_gap(self, start: float, reason: str) -> int:
        return int(self.db.execute("INSERT INTO gaps(start, end, reason) VALUES (?,?,?)",
                                   (round(start, 3), None, reason)).lastrowid)

    def close_gap(self, gid: int, end: float, min_len: float = 0.0) -> None:
        r = self.db.execute("SELECT start FROM gaps WHERE id=?", (gid,)).fetchone()
        if r is None:
            return
        if end - r["start"] < min_len:
            self.db.execute("DELETE FROM gaps WHERE id=?", (gid,))
        else:
            self.db.execute("UPDATE gaps SET end=? WHERE id=?", (round(end, 3), gid))

    def add_gap(self, start: float, end: float, reason: str) -> None:
        self.db.execute("INSERT INTO gaps(start, end, reason) VALUES (?,?,?)", (round(start, 3), round(end, 3), reason))

    def close_open_gaps(self, end: float) -> int:
        """Gaps left open by an unclean stop end at the last heartbeat."""
        return self.db.execute("UPDATE gaps SET end=MAX(start, ?) WHERE end IS NULL", (round(end, 3),)).rowcount

    def gaps(self, t0: float, t1: float) -> list[dict]:
        rows = self.db.execute("SELECT start, end, reason FROM gaps WHERE start <= ? AND (end IS NULL OR end >= ?) "
                               "ORDER BY start", (t1, t0)).fetchall()
        out = []
        for r in rows:
            g = {"start": r["start"], "end": r["end"] if r["end"] is not None else round(t1, 3),
                 "reason": r["reason"]}
            if r["end"] is None:
                g["ongoing"] = True
            out.append(g)
        return out

    def last_heartbeat(self) -> float | None:
        hb = self.kv_get("heartbeat")
        r = self.db.execute("SELECT MAX(t) AS t FROM history").fetchone()
        cands = [x for x in (hb, r["t"] if r else None) if isinstance(x, (int, float))]
        return max(cands) if cands else None

    # ---------------------------------------------------------------- walk tests
    def add_walktest(self, started: float, result: dict, raw_path: str) -> None:
        self.db.execute("INSERT INTO walktests(started, result, raw_path) VALUES (?,?,?)",
                        (started, json.dumps(result), raw_path))

    def last_walktest(self) -> dict | None:
        r = self.db.execute("SELECT result FROM walktests ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(r["result"]) if r else None


class RawRecorder:
    """Hourly JSONL files under data/raw/, gzipped when closed, pruned by age and total size.

    max_bytes holds on every write, not only when the hour rotates: a sample flood (junk on the ESP32
    port, say) must not fill the disk within the hour. A write that would exceed it first deletes the
    oldest closed files (the newest data is the useful part); if the current hour alone fills the
    budget, samples are dropped and counted until the next hour. The one excess allowed is transient:
    the .gz of the file being compressed exists next to its source until it replaces it."""

    def __init__(self, directory: str | Path, keep_hours: float, max_bytes: int):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_s = keep_hours * 3600
        self.max_bytes = int(max_bytes)
        self.fh = None
        self.cur_name = None
        self.dropped = 0
        self._full_logged = False
        self._lock = threading.Lock()   # background compression; held for seconds, never taken by write()
        self._acct = threading.Lock()   # the size accounting below; held only briefly
        self._closed: list[list] = []   # [path, bytes] of closed files, oldest first
        self._closed_total = 0
        self._cur_bytes = 0             # bytes written to the current file, including buffered ones
        self._evicted: set[Path] = set()  # closed .jsonl files write() deleted, maybe while being compressed

    def write(self, line: str, t: float | None = None) -> None:
        name = time.strftime("raw-%Y%m%d-%H.jsonl", time.localtime(t or time.time()))
        if name != self.cur_name:
            self._rotate(name)
        nl = not line.endswith("\n")
        n = (len(line) if line.isascii() else len(line.encode("utf-8", "replace"))) + nl
        if self._closed_total + self._cur_bytes + n > self.max_bytes and not self._make_room(n):
            self.dropped += 1
            if not self._full_logged:
                self._full_logged = True
                log.warning("raw recording reached [record] max_bytes (%d) within one hour; dropping samples "
                            "until the next hour", self.max_bytes)
            return
        self.fh.write(line)
        if nl:
            self.fh.write("\n")
        self._cur_bytes += n

    def _make_room(self, n: int) -> bool:
        with self._acct:
            while self._closed and self._closed_total + self._cur_bytes + n > self.max_bytes:
                p, size = self._closed.pop(0)
                self._closed_total -= size
                p.unlink(missing_ok=True)
                if p.suffix == ".jsonl":
                    self._evicted.add(p)
            return self._closed_total + self._cur_bytes + n <= self.max_bytes

    def _rotate(self, name: str) -> None:
        old = None
        if self.fh is not None:
            self.fh.close()
            old = self.dir / self.cur_name
        self.cur_name = name
        path = self.dir / name
        self.fh = open(path, "a", buffering=1 << 16)
        self._full_logged = False
        with self._acct:
            if old is not None:
                self._closed.append([old, self._cur_bytes])
            self._closed = [e for e in self._closed if e[0] != path]  # reopened (restart, clock step)
            self._closed_total = sum(e[1] for e in self._closed)
            self._cur_bytes = path.stat().st_size
        if old is None:
            self.prune()  # first file of this run: account for what is already on disk
        th = threading.Thread(target=self._compress_and_prune, daemon=True)
        self._threads = [x for x in getattr(self, "_threads", []) if x.is_alive()] + [th]
        th.start()

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Wait for background compression to finish (tests; never called on the hot path)."""
        for th in list(getattr(self, "_threads", [])):
            th.join(timeout)

    def _compress_and_prune(self) -> None:
        with self._lock:
            try:
                for p in sorted(self.dir.glob("raw-*.jsonl")):
                    if p.name == self.cur_name:
                        continue
                    gz = p.with_suffix(".jsonl.gz")
                    # append: a second file for the same hour name (DST fall-back, clock step) adds a
                    # gzip member instead of overwriting the first one
                    try:
                        with open(p, "rb") as fi, gzip.open(gz, "ab", compresslevel=6) as fo:
                            shutil.copyfileobj(fi, fo)
                    except FileNotFoundError:  # deleted by write() to make room
                        continue
                    with self._acct:
                        p.unlink(missing_ok=True)
                        if p in self._evicted:  # write() deleted it to make room while it was being compressed
                            gz.unlink(missing_ok=True)
                self.prune()  # accounts for the new .gz files
            except Exception:  # noqa: BLE001
                log.exception("raw recorder maintenance failed")

    def prune(self) -> None:
        """Resync the accounting with the directory, then apply keep_hours and max_bytes."""
        with self._acct:
            now = time.time()
            files = []
            for p in self.dir.glob("raw-*.jsonl*"):
                if p.name == self.cur_name:
                    continue
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                if now - st.st_mtime > self.keep_s:
                    p.unlink(missing_ok=True)
                else:
                    files.append([p, st.st_size, st.st_mtime])
            files.sort(key=lambda e: e[2])
            total = sum(e[1] for e in files)
            while files and total + self._cur_bytes > self.max_bytes:
                p, size, _ = files.pop(0)
                total -= size
                p.unlink(missing_ok=True)
            self._closed = [[p, size] for p, size, _ in files]
            self._closed_total = total
            self._evicted.clear()

    def flush(self) -> None:
        if self.fh is not None:
            self.fh.flush()

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None
