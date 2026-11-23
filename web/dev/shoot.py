#!/usr/bin/env python3
"""Drive the dashboard against the mock server in a headless browser and save screenshots.

Needs Playwright in a throwaway env, e.g.:
    python3 web/dev/mock_server.py --scenario demo --motion-every 0 &
    uv run --no-project --with playwright python web/dev/shoot.py --engine webkit

Uses the mock's /_mock/set hooks to force each state. Writes to web/dev/screenshots/.
Also renders the PNG app icons from icon.svg with --icons.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HERE)


def mock(base, **kw):
    req = urllib.request.Request(base + "/_mock/set", data=json.dumps(kw).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def wait_text(page, sel, text, timeout=30):
    page.wait_for_function(
        "([s, t]) => { const e = document.querySelector(s); return e && e.textContent.includes(t); }",
        arg=[sel, text], timeout=timeout * 1000)


def render_icons(p):
    b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_EXE") or None)
    svg = open(os.path.join(WEB, "icon.svg")).read().replace('rx="112"', 'rx="0"')
    for size in (180, 192, 512):
        pg = b.new_page(viewport={"width": size, "height": size})
        pg.set_content('<html><body style="margin:0">%s</body></html>' %
                       svg.replace("<svg ", '<svg width="%d" height="%d" ' % (size, size), 1))
        pg.screenshot(path=os.path.join(WEB, "icon-%d.png" % size), omit_background=False)
        pg.close()
    b.close()
    print("icons written")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8451")
    ap.add_argument("--engine", default="webkit", choices=["webkit", "chromium"])
    ap.add_argument("--chromium-exe", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "screenshots"))
    ap.add_argument("--icons", action="store_true")
    ap.add_argument("--only", default=None, help="comma list of shot groups")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    os.makedirs(args.out, exist_ok=True)
    base = args.base
    only = set(args.only.split(",")) if args.only else None

    with sync_playwright() as p:
        if args.icons:
            render_icons(p)
            return
        bt = p.webkit if args.engine == "webkit" else p.chromium
        browser = bt.launch(executable_path=args.chromium_exe) if args.chromium_exe else bt.launch()
        logs = []

        def ctx(width, height, scheme, mobile):
            c = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                    device_scale_factor=2, is_mobile=mobile and args.engine == "chromium",
                                    has_touch=mobile)
            last_seen = time.time() - 6.5 * 3600
            c.add_init_script("try{localStorage.setItem('wifi-sentry.lastSeen', '%f')}catch(e){}" % last_seen)
            return c

        def open_page(c):
            pg = c.new_page()
            pg.on("console", lambda m: logs.append("%s: %s" % (m.type, m.text)) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: logs.append("pageerror: %s" % e))
            pg.goto(base + "/")
            wait_text(pg, "#conn", "Live")
            pg.wait_for_timeout(1200)
            return pg

        def shot(pg, name, full=False):
            path = os.path.join(args.out, name + ".png")
            pg.screenshot(path=path, full_page=full)
            print("saved", name)

        def want(g):
            return only is None or g in only

        for scheme in ("light", "dark"):
            # ---------- phone ----------
            c = ctx(390, 844, scheme, True)
            pg = open_page(c)
            post(base, "/api/sensitivity", {"level": "medium"})
            post(base, "/api/arm", {"armed": True})
            if want("clear"):
                shot(pg, "phone-%s-clear" % scheme)
                shot(pg, "phone-%s-full" % scheme, full=True)
            if want("motion"):
                mock(base, burst=25, amp=5.5)
                wait_text(pg, "#heroTitle", "Motion now")
                pg.wait_for_timeout(4000)
                shot(pg, "phone-%s-motion" % scheme)
                wait_text(pg, "#heroTitle", "All clear", 60)
            if want("event"):
                pg.locator("details.ev").first.locator("summary").click()
                pg.wait_for_timeout(300)
                pg.locator("#eventsSec").scroll_into_view_if_needed()
                shot(pg, "phone-%s-event-open" % scheme)
                pg.locator("details.ev").first.locator("summary").click()
            if want("alerts"):
                pg.evaluate("window.scrollTo(0,0)")
                pg.click("#alertsBtn")
                pg.wait_for_timeout(300)
                shot(pg, "phone-%s-alerts" % scheme)
                pg.click("#alertsClose")
            if want("learning"):
                mock(base, learning=40)
                wait_text(pg, "#heroTitle", "Learning")
                pg.wait_for_timeout(7000)
                pg.evaluate("window.scrollTo(0,0)")
                shot(pg, "phone-%s-learning" % scheme)
                mock(base, learning=0.5)
                wait_text(pg, "#heroTitle", "All clear", 20)
            if want("offline"):
                mock(base, offline=True)
                wait_text(pg, "#heroTitle", "Sensor offline")
                pg.wait_for_timeout(500)
                shot(pg, "phone-%s-offline" % scheme)
                mock(base, offline=False)
                wait_text(pg, "#heroTitle", "All clear", 20)
            if want("esp32"):
                mock(base, esp32=True)
                pg.wait_for_timeout(1500)
                pg.locator("#sensorsSec").scroll_into_view_if_needed()
                shot(pg, "phone-%s-sensors-esp32" % scheme)
                mock(base, esp32=False)
            if want("walk"):
                pg.locator("#walkStart").scroll_into_view_if_needed()
                pg.click("#walkStart")
                pg.wait_for_timeout(6000)
                shot(pg, "phone-%s-walk-running" % scheme)
                pg.click("#walkStop")
                pg.wait_for_selector("#walkResult:not([hidden])")
                pg.wait_for_timeout(300)
                pg.locator("#walkResult").scroll_into_view_if_needed()
                shot(pg, "phone-%s-walk-result" % scheme)
                btn = pg.locator("#walkResult [data-done]")
                btn.click()
            if want("relearn"):
                pg.locator("#relearnBtn").scroll_into_view_if_needed()
                pg.click("#relearnBtn")
                pg.wait_for_timeout(200)
                shot(pg, "phone-%s-relearn-confirm" % scheme)
                pg.click("#relearnCancel")
            if want("disconnected"):
                pg.evaluate("window.scrollTo(0,0)")
                mock(base, die=14)
                wait_text(pg, "#heroTitle", "Not connected", 15)
                pg.wait_for_timeout(1500)
                shot(pg, "phone-%s-disconnected" % scheme)
                t0 = time.time()
                wait_text(pg, "#conn", "Live", 60)
                print("reconnected %.1fs after disconnect shot" % (time.time() - t0))
                pg.wait_for_timeout(1500)
                shot(pg, "phone-%s-reconnected" % scheme)
            c.close()

            # ---------- desktop ----------
            if want("desktop"):
                c = ctx(1280, 900, scheme, False)
                pg = open_page(c)
                pg.wait_for_timeout(800)
                shot(pg, "desktop-%s" % scheme)
                shot(pg, "desktop-%s-full" % scheme, full=True)
                pg.click('#rangeSeg [data-range="86400"]')
                pg.wait_for_timeout(1200)
                box = pg.locator("#chart").bounding_box()
                pg.mouse.move(box["x"] + box["width"] * 0.62, box["y"] + box["height"] * 0.5)
                pg.wait_for_timeout(300)
                pg.locator("#activity").screenshot(path=os.path.join(args.out, "desktop-%s-chart-24h.png" % scheme))
                print("saved desktop-%s-chart-24h" % scheme)
                pg.click('#rangeSeg [data-range="3600"]')
                c.close()

        browser.close()
    if logs:
        print("--- console errors/warnings ---")
        for l in logs:
            print(l)
    else:
        print("no console errors")


if __name__ == "__main__":
    main()
