#!/usr/bin/env python3
"""Functional checks of the dashboard against the mock (headless browser).

    python3 web/dev/mock_server.py --motion-every 0 --many-events 70 &
    uv run --no-project --with playwright python web/dev/check.py

Checks: live connect, labels round-trip, arm toggle, sensitivity, pagination with
`before`, stale-stream watchdog (no ticks while the socket stays open), server death
and recovery, and that "All clear" is never on screen while disconnected.
"""

import json
import os
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8451"


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=5).read())


def mock(**kw):
    req = urllib.request.Request(BASE + "/_mock/set", data=json.dumps(kw).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""), flush=True)


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = getattr(p, os.environ.get("PW_BROWSER", "webkit")).launch()  # PW_BROWSER=chromium if WebKit isn't installed
        pg = b.new_page(viewport={"width": 390, "height": 844})
        pg.goto(BASE + "/")
        pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=15000)
        check("connects and goes live", True)
        title = pg.text_content("#heroTitle")
        check("hero shows a live state", title in ("All clear", "Motion now", "Learning your home", "Sensor offline"), title)

        # label round trip
        first = pg.locator("details.ev").first
        eid = first.get_attribute("data-id")
        first.locator("summary").click()
        first.locator('[data-label="false"]').click()
        pg.wait_for_timeout(600)
        ev = [e for e in get("/api/events?limit=200")["events"] if str(e["id"]) == eid][0]
        check("mark false alarm reaches server", ev["label"] == "false", str(ev["label"]))
        first.locator("input").fill("cat on the desk")
        first.locator("[data-save]").click()
        pg.wait_for_timeout(600)
        ev = [e for e in get("/api/events?limit=200")["events"] if str(e["id"]) == eid][0]
        check("note saved", ev.get("note") == "cat on the desk" and ev["label"] == "false", repr(ev.get("note")))
        first.locator('[data-label="false"]').click()
        pg.wait_for_timeout(600)
        ev = [e for e in get("/api/events?limit=200")["events"] if str(e["id"]) == eid][0]
        check("tapping the active label clears it", ev["label"] is None, str(ev["label"]))

        # arm toggle
        before = get("/api/status")["armed"]
        pg.click("#armSwitch")
        pg.wait_for_timeout(600)
        after = get("/api/status")["armed"]
        check("arm toggle flips server state", after != before)
        pg.click("#armSwitch")
        pg.wait_for_timeout(600)
        check("arm toggle flips back", get("/api/status")["armed"] == before)

        # sensitivity
        pg.click('#sensSeg [data-level="high"]')
        pg.wait_for_timeout(600)
        check("sensitivity high", get("/api/status")["sensitivity"] == "high")
        pg.click('#sensSeg [data-level="medium"]')
        pg.wait_for_timeout(600)
        check("sensitivity medium", get("/api/status")["sensitivity"] == "medium")

        # pagination
        n0 = pg.locator("details.ev").count()
        more_visible = pg.is_visible("#moreBtn")
        if more_visible:
            pg.click("#moreBtn")
            pg.wait_for_timeout(1000)
            n1 = pg.locator("details.ev").count()
            ids = [int(x) for x in pg.eval_on_selector_all("details.ev", "els => els.map(e => e.dataset.id)")]
            check("show older loads the next page", n1 > n0, "%d -> %d" % (n0, n1))
            check("no duplicate rows after paging", len(ids) == len(set(ids)))
        else:
            check("pagination (needs --many-events 70)", False, "more button hidden with %d rows" % n0)

        # stale stream: socket open, ticks stop
        mock(freeze=16)
        t0 = time.time()
        pg.wait_for_function("document.querySelector('#heroTitle').textContent === 'Updates stopped'", timeout=12000)
        check("stale ticks detected", True, "%.1fs after freeze" % (time.time() - t0))
        check("no 'All clear' while stale", "All clear" not in pg.text_content("#hero"))
        pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=30000)
        check("recovers after ticks resume", True, "%.1fs after freeze" % (time.time() - t0))

        # server death
        mock(die=8)
        t0 = time.time()
        pg.wait_for_function("document.querySelector('#heroTitle').textContent === 'Not connected'", timeout=8000)
        check("server death noticed", True, "%.1fs" % (time.time() - t0))
        seen_clear = False
        while time.time() - t0 < 7:
            if "All clear" in pg.text_content("#hero"):
                seen_clear = True
            pg.wait_for_timeout(250)
        check("never shows 'All clear' while disconnected", not seen_clear)
        check("title says disconnected", pg.title().startswith("Disconnected"), pg.title())
        check("arm switch disabled while disconnected", pg.is_disabled("#armSwitch"))
        pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=40000)
        check("reconnects after server returns", True, "%.1fs after kill (8s down)" % (time.time() - t0))

        # walk test -> recommendation -> one-tap apply (run the mock with --walk-amp 5 for a "low" result)
        pg.locator("#walkStart").scroll_into_view_if_needed()
        pg.click("#walkStart")
        pg.wait_for_timeout(1500)
        check("walk test running", get("/api/walktest")["running"] and pg.is_visible("#walkRun"))
        pg.wait_for_timeout(5000)
        clock = pg.text_content("#walkClock")
        check("walk timer counts", clock not in ("0:00", "0:01"), clock)
        pg.click("#walkStop")
        pg.wait_for_selector("#walkResult:not([hidden])", timeout=5000)
        rec = get("/api/walktest")["result"]["recommended"]
        apply = pg.locator("#walkResult [data-apply]")
        if apply.count():
            apply.click()
            pg.wait_for_timeout(800)
            check("apply recommended sensitivity", get("/api/status")["sensitivity"] == rec, rec)
            pg.click('#sensSeg [data-level="medium"]')
            pg.wait_for_timeout(600)
        else:
            check("walk result shown (no apply needed: %s)" % rec, True)
        pg.click("#walkResult [data-done]")

        # learning -> clear
        mock(learning=4)
        pg.wait_for_function("document.querySelector('#heroTitle').textContent.startsWith('Learning')", timeout=5000)
        check("learning shows progress", pg.is_visible("#learnBox"))
        pg.wait_for_function("document.querySelector('#heroTitle').textContent === 'All clear'", timeout=15000)
        check("learning finishes to all clear", True)

        # motion -> title
        mock(burst=12, amp=5)
        pg.wait_for_function("document.title.startsWith('Motion now')", timeout=10000)
        check("title changes on motion", True)
        b.close()
    bad = [n for n, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(bad), len(results)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
