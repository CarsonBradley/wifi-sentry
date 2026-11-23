#!/usr/bin/env python3
"""Screenshots of every dashboard state against a REAL test engine with simulated data.

Starts and stops its own private engine (never the live one), so it can also show the
disconnected and reconnecting states. Set it up once with demo_seed.py:

    D=/tmp/ws-demo
    engine/.venv/bin/python web/dev/demo_seed.py --write-config $D/test.toml --control-dir $D
    uv run --no-project --with playwright==1.63.0 python web/dev/shoot_engine.py --dir $D --seed

--seed (re)fills the data directory with a simulated day first (demo_seed.py). The engine learns a
baseline on its first start (30 s). Simulated motion comes from demo_sampler.py through the control
files in --dir; the ESP32 shot runs esp32/tools/fake_esp32.py on 127.0.0.1:5577, flapping between two
channels. Screenshots go to web/dev/screenshots/engine-*.png; --readme writes the README images to
docs/img/ instead (phone-clear, phone-motion, phone-events, phone-setup, desktop, demo.gif; needs ffmpeg).
Checks along the way print PASS / FAIL, including the token login.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)


class Engine:
    def __init__(self, a):
        self.a = a
        self.proc = None
        self.base = "http://127.0.0.1:%d" % a.port

    def start(self):
        a = self.a
        try:
            urllib.request.urlopen(self.base + "/api/auth", timeout=1).read()
            sys.exit("something already answers on port %d: stop it first" % a.port)
        except OSError:
            pass
        log = open(os.path.join(a.dir, "engine.log"), "a")
        self.proc = subprocess.Popen([a.python, "-m", "sentry.main", "--config", os.path.join(a.dir, "test.toml"),
                                      "--port", str(a.port), "--hosts", "loopback", "--data-dir", os.path.join(a.dir, "data"),
                                      "--learning-s", "30"], stdout=log, stderr=log, start_new_session=True)
        for _ in range(100):
            if self.proc.poll() is not None:
                sys.exit("the test engine exited at once; see %s" % os.path.join(a.dir, "engine.log"))
            try:
                urllib.request.urlopen(self.base + "/api/auth", timeout=1).read()
                break
            except OSError:
                time.sleep(0.2)
        self.token = open(os.path.join(a.dir, "data", "access-token")).read().strip()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(15)
        self.proc = None

    def api(self, path, body=None, method=None):
        req = urllib.request.Request(self.base + "/api/" + path, method=method or ("POST" if body is not None else "GET"),
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        return json.loads(urllib.request.urlopen(req, timeout=40).read())

    def wait_state(self, states, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.api("status")
            if st["state"] in states:
                return st
            time.sleep(0.5)
        raise TimeoutError("engine never reached %s" % (states,))


def control(a, name, value):
    p = os.path.join(a.dir, name)
    if value is None:
        if os.path.exists(p):
            os.remove(p)
    else:
        with open(p, "w") as f:
            f.write(value)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="the test instance's directory (test.toml, data/, control files)")
    ap.add_argument("--port", type=int, default=8466)
    ap.add_argument("--python", default=os.path.join(ROOT, "engine", ".venv", "bin", "python"), help="the engine's Python")
    ap.add_argument("--seed", action="store_true", help="refill the data directory with a simulated day first")
    ap.add_argument("--readme", action="store_true", help="write the README images to docs/img/")
    ap.add_argument("--only", help="comma list of groups: login,clear,motion,event,bell,learning,sensing,walk,disconnected,esp32")
    ap.add_argument("--schemes", default="light,dark")
    a = ap.parse_args()
    if a.port == 8450:
        sys.exit("refusing port 8450 (normally the live instance)")
    if not os.path.exists(a.python):
        a.python = sys.executable
    out = os.path.join(HERE, "screenshots")
    os.makedirs(out, exist_ok=True)
    only = set(a.only.split(",")) if a.only else None
    want = lambda g: only is None or g in only
    for f in ("moving", "mode"):
        control(a, f, None)

    eng = Engine(a)
    if a.seed:
        if not os.path.exists(os.path.join(a.dir, "data", "sentry.db")):   # first run: learn a baseline
            eng.start(); eng.wait_state(("clear",), 90); time.sleep(65); eng.stop()
        subprocess.run([a.python, os.path.join(HERE, "demo_seed.py"), "--data-dir", os.path.join(a.dir, "data")], check=True)
    eng.start()
    try:
        run(a, eng, out, want)
    finally:
        eng.stop()
        for f in ("moving", "mode"):
            control(a, f, None)
    bad = [n for n, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(bad), len(results)))
    sys.exit(1 if bad else 0)


def run(a, eng, out, want):
    eng.wait_state(("clear", "motion"), 90)
    eng.api("onboarding", {"status": "done"})
    eng.api("sensitivity", {"level": "medium"})
    eng.api("arm", {"armed": True})

    from playwright.sync_api import sync_playwright
    logs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()

        def ctx(width, scheme, cookie=True):
            mobile = width < 600
            c = browser.new_context(viewport={"width": width, "height": 844 if mobile else 900}, color_scheme=scheme,
                                    device_scale_factor=2, is_mobile=mobile, has_touch=mobile)
            c.add_init_script("try{localStorage.setItem('wifi-sentry.lastSeen', JSON.stringify(Date.now()/1000 - 5.2*3600))}catch(e){}")
            pg = c.new_page()
            pg.on("console", lambda m: logs.append(m.type + ": " + m.text) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: logs.append("pageerror: %s" % e))
            if cookie:
                pg.goto(eng.base + "/?token=" + eng.token)
                live(pg)
            return c, pg

        def live(pg, timeout=30):
            pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=timeout * 1000)
            pg.wait_for_timeout(1800)

        def title(pg, text, timeout=60):
            pg.wait_for_function("t => document.querySelector('#heroTitle').textContent.startsWith(t)", arg=text, timeout=timeout * 1000)

        def shot(pg, name, sel=None, full=False):
            if a.readme:
                return
            path = os.path.join(out, "engine-%s.png" % name)
            if sel:
                pg.locator(sel).screenshot(path=path)
            else:
                pg.screenshot(path=path, full_page=full)

        if a.readme:
            readme(a, eng, ctx, title, browser)
            browser.close()
            return

        for scheme in a.schemes.split(","):
            if want("login"):
                c, pg = ctx(390, scheme, cookie=False)
                pg.goto(eng.base + "/")
                title(pg, "Enter access token", 20)
                check(f"[{scheme}] locked without a cookie, nothing else shown",
                      pg.is_visible("#loginBox") and pg.is_hidden("#daySec") and pg.is_hidden("#eventsSec"))
                shot(pg, f"{scheme}-locked")
                pg.fill("#tokenInput", "not-the-token")
                pg.click("#loginBtn")
                pg.wait_for_function("document.querySelector('#loginMsg').classList.contains('err')", timeout=5000)
                check(f"[{scheme}] wrong token explained", "isn’t right" in pg.text_content("#loginMsg"), pg.text_content("#loginMsg"))
                shot(pg, f"{scheme}-locked-wrong")
                pg.fill("#tokenInput", eng.token)
                pg.click("#loginBtn")
                live(pg)
                check(f"[{scheme}] the right token unlocks", pg.is_visible("#daySec") and pg.is_hidden("#loginBox"))
                c.close()
                c, pg = ctx(390, scheme, cookie=False)
                pg.goto(eng.base + "/?token=wrong-token")
                title(pg, "Enter access token", 20)
                pg.wait_for_timeout(500)
                check(f"[{scheme}] a wrong link token is reported", "not accepted" in (pg.text_content("#loginMsg") or ""),
                      pg.text_content("#loginMsg"))
                check(f"[{scheme}] the rejected token is not left in the address bar", "token" not in pg.url, pg.url)
                c.close()

            if want("clear"):
                for w in (390, 1280):
                    c, pg = ctx(w, scheme)
                    title(pg, "All clear")
                    shot(pg, f"{scheme}-{w}-clear")
                    shot(pg, f"{scheme}-{w}-full", full=True)
                    c.close()

            c, pg = ctx(390, scheme)
            if want("motion"):
                control(a, "moving", "1")
                title(pg, "Motion now", 30)
                pg.wait_for_timeout(5000)
                check(f"[{scheme}] motion: title, live mark and tab title", pg.title().startswith("Motion now") and
                      pg.locator(".chip.live").count() == 1, pg.title())
                shot(pg, f"{scheme}-motion")
                d, dpg = ctx(1280, scheme)
                title(dpg, "Motion now", 30)
                shot(dpg, f"{scheme}-1280-motion")
                d.close()
                control(a, "moving", None)
                title(pg, "All clear", 90)
            if want("event"):
                pg.locator("details.ev").first.locator("summary").click()
                pg.wait_for_timeout(300)
                pg.locator("details.ev").first.scroll_into_view_if_needed()
                pg.evaluate("window.scrollBy(0, -120)")
                shot(pg, f"{scheme}-event-open")
                pg.locator("details.ev").first.locator("summary").click()
            if want("bell"):
                pg.evaluate("window.scrollTo(0,0)")
                pg.click("#alertsBtn")
                pg.wait_for_timeout(300)
                shot(pg, f"{scheme}-bell")
                pg.keyboard.press("Escape")
            if want("walk"):
                pg.locator("#walkStart").scroll_into_view_if_needed()
                pg.click("#walkStart")
                control(a, "moving", "1")
                pg.wait_for_timeout(6000)
                shot(pg, f"{scheme}-walk-running")
                pg.click("#walkStop")
                control(a, "moving", None)
                pg.wait_for_selector("#walkResult:not([hidden])", timeout=10000)
                pg.locator("#walkResult").scroll_into_view_if_needed()
                pg.wait_for_timeout(300)
                shot(pg, f"{scheme}-walk-result")
                pg.click("#walkResult [data-done]")
                eng.wait_state(("clear",), 90)
            if want("sensing"):
                for mode, expect, name in (("single", "Reduced", "reduced"), ("aggregate", "Degraded", "degraded"), ("off", "", "offline")):
                    control(a, "mode", mode)
                    t0 = time.time()
                    if mode == "off":
                        pg.wait_for_function("['Sensor offline','Can’t sense'].includes(document.querySelector('#heroTitle').textContent)", timeout=90000)
                        pg.wait_for_timeout(2500)
                        txt = pg.text_content("#heroTitle")
                        check(f"[{scheme}] Wi-Fi off: not watching, never 'All clear'", txt in ("Sensor offline", "Can’t sense", "Learning your home"), txt)
                        check(f"[{scheme}] armed hint doesn't promise alerts while blind", "not watching" in pg.text_content("#armHint")
                              or "Learning" in txt, pg.text_content("#armHint"))
                    else:
                        pg.wait_for_function("t => !document.querySelector('#sensingNotice').hidden && document.querySelector('#sensingText').textContent.startsWith(t)",
                                             arg=expect, timeout=90000)
                        check(f"[{scheme}] {name} sensing notice", expect in pg.text_content("#sensingText"), pg.text_content("#sensingText"))
                    pg.evaluate("window.scrollTo(0,0)")
                    pg.wait_for_timeout(800)
                    shot(pg, f"{scheme}-{name}")
                    print("  (%s after %.0fs)" % (name, time.time() - t0))
                control(a, "mode", None)
                eng.wait_state(("clear",), 120)
            if want("learning"):
                pg.locator("#relearnBtn").scroll_into_view_if_needed()
                pg.click("#relearnBtn")
                pg.wait_for_timeout(200)
                shot(pg, f"{scheme}-relearn-confirm")
                pg.click("#relearnGo")
                title(pg, "Learning", 10)
                pg.wait_for_timeout(9000)
                pg.evaluate("window.scrollTo(0,0)")
                pg.wait_for_timeout(600)
                check(f"[{scheme}] learning shows progress in the hub", pg.text_content("#hubVal").endswith("%"), pg.text_content("#hubVal"))
                shot(pg, f"{scheme}-learning")
                title(pg, "All clear", 90)
            if want("disconnected"):
                pg.evaluate("window.scrollTo(0,0)")
                eng.stop()
                pg.wait_for_function("['Not connected','Updates stopped'].includes(document.querySelector('#heroTitle').textContent)", timeout=20000)
                pg.wait_for_timeout(1200)
                check(f"[{scheme}] disconnected: no 'All clear', switch disabled", "All clear" not in pg.text_content("#hero") and pg.is_disabled("#armSwitch"))
                check(f"[{scheme}] disconnected: tab title", pg.title().startswith("Disconnected"), pg.title())
                shot(pg, f"{scheme}-disconnected")
                eng.start()
                t0 = time.time()
                live(pg, 40)
                check(f"[{scheme}] reconnects on its own", True, "%.1fs" % (time.time() - t0))
                shot(pg, f"{scheme}-reconnected")
            c.close()

            if want("esp32"):
                c, pg = ctx(390, scheme)
                fake = os.path.join(ROOT, "esp32", "tools", "fake_esp32.py")
                procs = [subprocess.Popen([sys.executable, fake, "--port", "5577", "--name", "hall", "--channel", ch, "--rate", "20",
                                           "--quiet", "--seed", seed], start_new_session=True) for ch, seed in (("6", "1"), ("11", "2"))]
                try:
                    pg.wait_for_function("document.querySelector('#sensorList').textContent.includes('hall')", timeout=30000)
                    # the dashboard refreshes the full status (sensor health) every 30 s
                    pg.wait_for_function("document.querySelector('#sensorList').textContent.includes('channel keeps changing')", timeout=60000)
                    pg.locator("#sensorsSec").scroll_into_view_if_needed()
                    pg.wait_for_timeout(500)
                    txt = pg.text_content("#sensorList")
                    check(f"[{scheme}] ESP32 'channel keeps changing' shown", "channel keeps changing" in txt, txt[-120:])
                    shot(pg, f"{scheme}-esp32", "#sensorsSec")
                finally:
                    for pr in procs:
                        pr.terminate()
                c.close()
        browser.close()
    # network errors logged by the browser are expected here (wrong tokens, the engine stopped on purpose)
    errs = [x for x in logs if "Failed to load resource" not in x]
    check("no console errors", not errs, errs[:6])


def readme(a, eng, ctx, title, browser):
    """The README images (docs/img/): phone all-clear, motion and events (light), the phone-alerts step of
    the setup guide and the desktop (dark), and demo.gif (all clear -> motion -> event logged).
    Alerts go to a local stub, never to a real ntfy server."""
    import shutil
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *x):
            pass

    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    img = os.path.join(ROOT, "docs", "img")

    for k in ("ntfy", "webhook", "email"):
        eng.api("alerts/channels/" + k, {}, "DELETE")
    eng.api("onboarding", {"status": "active", "step": "alerts"})
    c, pg = ctx(390, "dark")
    pg.wait_for_selector("#setup:not([hidden])")
    pg.evaluate("document.querySelector('#setup').scrollIntoView({block: 'start'}); window.scrollBy(0, -60)")
    pg.wait_for_timeout(500)
    pg.screenshot(path=os.path.join(img, "phone-setup.png"))
    c.close()
    eng.api("onboarding", {"status": "done", "alerts_prompt_dismissed": True})
    topic = eng.api("alerts/ntfy-topic", {})["topic"]
    eng.api("alerts/channels/ntfy", {"topic": topic, "server": "http://127.0.0.1:%d" % stub.server_address[1], "enabled": True})

    c, pg = ctx(1280, "dark")
    title(pg, "All clear")
    pg.screenshot(path=os.path.join(img, "desktop.png"))
    c.close()

    c, pg = ctx(390, "light")
    title(pg, "All clear")
    pg.screenshot(path=os.path.join(img, "phone-clear.png"))
    frames = tempfile.mkdtemp()
    shots = []

    def frame():
        path = os.path.join(frames, "%04d.png" % len(shots))
        pg.screenshot(path=path)
        shots.append((path, time.time()))

    t_end = time.time() + 3
    while time.time() < t_end:
        frame()
    control(a, "moving", "1")
    t_end = time.time() + 14
    while time.time() < t_end:
        frame()
    pg.screenshot(path=os.path.join(img, "phone-motion.png"))
    control(a, "moving", None)
    title(pg, "All clear", 90)
    t_end = time.time() + 2
    while time.time() < t_end:
        frame()
    y = pg.evaluate("document.querySelector('#eventsSec').getBoundingClientRect().top + scrollY - 56")
    for k in range(1, 13):
        pg.evaluate("y => window.scrollTo(0, y)", y * (1 - (1 - k / 12) ** 3))
        frame()
    pg.wait_for_timeout(400)
    pg.screenshot(path=os.path.join(img, "phone-events.png"))
    t_end = time.time() + 3
    while time.time() < t_end:
        frame()
    c.close()

    # GIF: real frame timings, 2x captures scaled to 520 px wide, one palette for the whole clip
    lst = os.path.join(frames, "list.txt")
    with open(lst, "w") as f:
        for (path, t), nxt in zip(shots, shots[1:] + [(None, shots[-1][1] + 2.5)]):
            f.write("file '%s'\nduration %.3f\n" % (path, max(0.05, nxt[1] - t)))
        f.write("file '%s'\n" % shots[-1][0])
    gif = os.path.join(img, "demo.gif")
    vf = "scale=520:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-vf", vf, "-loop", "0", gif], check=True)
    shutil.rmtree(frames)
    # 256-colour PNGs: a third of the size, no visible change for flat UI
    for name in ("phone-clear", "phone-motion", "phone-events", "phone-setup", "desktop"):
        src = os.path.join(img, name + ".png")
        tmp = src + ".tmp.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf",
                        "split[a][b];[a]palettegen=max_colors=256[p];[b][p]paletteuse=dither=none", "-pix_fmt", "pal8", tmp], check=True)
        os.replace(tmp, src)
    stub.shutdown()
    eng.api("alerts/channels/ntfy", {}, "DELETE")
    print("README images written to", img, "(%d GIF frames, %.1f MB)" % (len(shots), os.path.getsize(gif) / 1e6))


if __name__ == "__main__":
    main()
