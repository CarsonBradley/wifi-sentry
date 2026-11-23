#!/usr/bin/env python3
"""Drive the new dashboard flows against a REAL engine instance (not the mock) in a headless browser:
first-run setup, phone alerts (ntfy / webhook / email), alert options, exit delay, schedule, prompts.

Start a private test instance first (never the live one: this script arms it and edits its alerts). Its
config must allow alert targets on 127.0.0.1 (the stubs below), which a normal install refuses:
    printf '[alerts]\nallow_loopback_targets = true\n' > /tmp/ws-test.toml
    ./run.sh --config /tmp/ws-test.toml --port 8452 --hosts loopback --data-dir /tmp/ws-test --learning-s 60 --fake-mac
(or use the config demo_seed.py --write-config makes, which also allows them)
    uv run --no-project --with playwright==1.63.0 python web/dev/flows.py --base http://127.0.0.1:8452 \
        --token "$(cat /tmp/ws-test/access-token)"

No real network: every "Send test alert" goes to a local HTTP stub (ntfy, webhook) or SMTP stub on
127.0.0.1 started by this script. The ntfy topic is created with the public server only to show the
setup screen; the server is switched to the stub before anything is sent.
Screenshots go to web/dev/screenshots/flow-*.png (--no-shots to skip).
"""

import argparse
import json
import os
import socketserver
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GOT = []        # (path, headers, body) received by the HTTP stub
MAILS = []      # messages received by the SMTP stub


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        GOT.append((self.path, dict(self.headers), self.rfile.read(n)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"id":"stub"}')

    def log_message(self, *a):
        pass


class Smtp(socketserver.StreamRequestHandler):
    def handle(self):
        w = self.wfile.write
        w(b"220 stub\r\n")
        data, buf = False, []
        for line in self.rfile:
            if data:
                if line in (b".\r\n", b".\n"):
                    data = False
                    MAILS.append(b"".join(buf).decode())
                    w(b"250 queued\r\n")
                else:
                    buf.append(line)
                continue
            c = line[:4].upper()
            if c == b"EHLO":
                w(b"250-stub\r\n250 8BITMIME\r\n")
            elif c == b"DATA":
                data = True
                w(b"354 go\r\n")
            elif c == b"QUIT":
                w(b"221 bye\r\n")
                return
            else:
                w(b"250 ok\r\n")


def stubs():
    h = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    s = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Smtp)
    for srv in (h, s):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return h.server_address[1], s.server_address[1]


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8452")
    ap.add_argument("--engine", default="chromium", choices=["chromium", "webkit"])
    ap.add_argument("--schemes", default="light,dark")
    ap.add_argument("--no-shots", action="store_true")
    ap.add_argument("--force", action="store_true", help="allow port 8450 (the default live port)")
    ap.add_argument("--token", required=True, help="the test instance's access token (<data-dir>/access-token)")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    if base.endswith(":8450") and not a.force:
        sys.exit("refusing to drive port 8450 (normally the live instance); start a test instance, see --help")
    http_port, smtp_port = stubs()
    stub = f"http://127.0.0.1:{http_port}"

    def api(path, body=None, method=None):
        req = urllib.request.Request(base + "/api/" + path, method=method or ("POST" if body is not None else "GET"),
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + a.token})
        return json.loads(urllib.request.urlopen(req, timeout=40).read())

    def reset():
        for k in ("ntfy", "webhook", "email"):
            api("alerts/channels/" + k, {}, "DELETE")
        api("alerts/settings", {"cooldown_s": 300, "notify_on_close": False, "base_url": ""})
        api("arming", {"delay_s": 60, "schedule": {"enabled": False, "windows": []}})
        api("arm", {"armed": False})
        api("onboarding", {"status": "active", "step": "intro", "alerts_prompt_dismissed": False})

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = getattr(p, a.engine).launch()
        for scheme in a.schemes.split(","):
            reset()
            GOT.clear()
            MAILS.clear()
            logs = []
            c = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme, device_scale_factor=2,
                                    is_mobile=a.engine == "chromium", has_touch=True)
            pg = c.new_page()
            pg.on("console", lambda m: logs.append(m.type + ": " + m.text) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: logs.append("pageerror: %s" % e))
            pg.on("dialog", lambda d: d.accept())

            def shot(name, sel=None, full=False):
                if a.no_shots:
                    return
                path = os.path.join(HERE, "screenshots", f"flow-{scheme}-{name}.png")
                if sel:
                    pg.locator(sel).screenshot(path=path)
                else:
                    pg.screenshot(path=path, full_page=full)

            def text(sel):
                return pg.text_content(sel) or ""

            def nxt():
                pg.click("#setupNext")
                pg.wait_for_timeout(500)

            pg.goto(base + "/?token=" + a.token)
            pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=20000)
            pg.wait_for_selector("#setup:not([hidden])", timeout=5000)
            pg.wait_for_timeout(600)
            check(f"[{scheme}] setup shown on a fresh install", "Welcome" in text("#setupTitle"))
            check(f"[{scheme}] no small prompt while the setup runs", pg.is_hidden("#alertsPrompt"))
            shot("setup-1-intro")

            nxt()
            check(f"[{scheme}] step 2: sensor check", text("#setupTitle") == "Sensor check" and "Full sensing" in text("#setupBody"),
                  text("#setupBody")[:80])
            shot("setup-2-sensor", "#setup")
            nxt()
            check(f"[{scheme}] step 3: learning done", "Done" in text("#setupBody"))
            # resumable: reload the page, same step
            pg.reload()
            pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=20000)
            pg.wait_for_selector("#setup:not([hidden])")
            pg.wait_for_timeout(500)
            check(f"[{scheme}] setup resumes at the same step after a reload", text("#setupStepNo") == "Step 3 of 6", text("#setupStepNo"))
            nxt()
            pg.click('#setupBody [data-act="walk-start"]')
            pg.wait_for_timeout(5500)
            check(f"[{scheme}] walk test runs from the setup", api("walktest")["running"] and "Stop" in text("#setupBody"))
            shot("setup-4-walk-running", "#setup")
            pg.click('#setupBody [data-act="walk-stop"]')
            pg.wait_for_timeout(1200)
            check(f"[{scheme}] walk result shown", "Recommended" in text("#setupBody") or "wasn’t detected" in text("#setupBody"))
            shot("setup-4-walk-result", "#setup")
            nxt()
            check(f"[{scheme}] step 5: phone alerts with the privacy note", "Privacy:" in text("#setupBody") and "ntfy.sh" in text("#setupBody"))
            shot("setup-5-alerts", "#setup")
            pg.click('#setupBody [data-act="ntfy-setup"]')
            pg.wait_for_selector("#setupBody .codebox", timeout=5000)
            topic = pg.input_value("#setupBody .codebox input")
            ch = next(x for x in api("alerts")["channels"] if x["id"] == "ntfy")
            check(f"[{scheme}] ntfy topic generated, saved and on", ch["enabled"] and ch["settings"]["topic"] == topic and len(topic) >= 24, topic)
            shot("setup-5-alerts-subscribe", "#setup")
            # point it at the local stub before anything is sent (no real network)
            api("alerts/channels/ntfy", {"server": stub})
            pg.evaluate("window.scrollTo(0,0)")
            pg.wait_for_timeout(400)
            pg.click('#setupBody [data-act="test"]')
            pg.wait_for_selector("#setupBody .form-msg", timeout=15000)
            got = [g for g in GOT if g[0] == "/" + topic]
            check(f"[{scheme}] test alert from the setup reached the (stub) ntfy server",
                  got and got[-1][1].get("Title") == "wifi-sentry test alert" and "delivered" in text("#setupBody"), text("#setupBody .form-msg"))
            shot("setup-5-alerts-tested", "#setup")
            nxt()
            check(f"[{scheme}] step 6: arm", text("#setupTitle").startswith("Arm"))
            pg.click('#setupBody [data-act="arm-now"]')
            pg.wait_for_timeout(1500)
            st = api("status")
            check(f"[{scheme}] arm from the setup starts the exit delay", st["arm"]["mode"] == "delay" and not st["armed"], st["arm"])
            check(f"[{scheme}] hero shows the countdown", text("#armLabel").startswith("Arming in"), text("#armLabel"))
            shot("setup-6-arm", "#setup")
            pg.evaluate("window.scrollTo(0,0)")
            shot("hero-arming")
            pg.click("#armSwitch")       # cancel during the delay
            pg.wait_for_timeout(800)
            st = api("status")
            check(f"[{scheme}] tapping the switch cancels the delay", st["arm"]["target"] is False and not st["armed"])
            pg.click("#setupNext")        # Finish
            pg.wait_for_timeout(800)
            check(f"[{scheme}] finish hides the setup (server-side)", pg.is_hidden("#setup") and api("onboarding")["status"] == "done")
            check(f"[{scheme}] 'Run setup again' offered", pg.is_visible("#guideBtn"))

            # ---- alerts panel: webhook
            pg.locator('details.chan[data-kind="ntfy"] > summary').scroll_into_view_if_needed()
            pg.click('details.chan[data-kind="ntfy"] > summary')
            pg.wait_for_timeout(300)
            shot("panel-ntfy", "#alertsSec")
            pg.click('details.chan[data-kind="ntfy"] > summary')
            pg.click('details.chan[data-kind="webhook"] > summary')
            body = 'details.chan[data-kind="webhook"] [data-body]'
            pg.fill(body + ' input[name="url"]', stub + "/hook/secret-part")
            pg.select_option(body + ' select[name="format"]', "slack")
            pg.click(body + ' [data-act="save"]')
            pg.wait_for_timeout(800)
            v = api("alerts")
            w = next(x for x in v["channels"] if x["id"] == "webhook")
            check(f"[{scheme}] webhook saved and on, URL never returned", w["enabled"] and w["secrets"]["url"] and "secret-part" not in json.dumps(v), w.get("url_hint"))
            pg.click(body + ' [data-act="test"]')
            pg.wait_for_function("document.querySelector('details.chan[data-kind=\"webhook\"] [data-msg]').textContent.startsWith('Delivered')", timeout=15000)
            hook = [g for g in GOT if g[0] == "/hook/secret-part"]
            check(f"[{scheme}] webhook test delivered in Slack format", hook and json.loads(hook[-1][2]).get("text", "").startswith("wifi-sentry test alert"))
            pg.fill(body + ' input[name="url"]', "http://127.0.0.1:9/nothing")
            pg.click(body + ' [data-act="test"]')      # unsaved change: saves first, then tests
            pg.wait_for_function("document.querySelector('details.chan[data-kind=\"webhook\"] [data-msg]').textContent.startsWith('Not delivered')", timeout=15000)
            check(f"[{scheme}] failed test shows the real error", "couldn't connect to 127.0.0.1:9" in text(body + " [data-msg]"), text(body + " [data-msg]"))
            shot("panel-webhook-error", 'details.chan[data-kind="webhook"]')
            pg.click('details.chan[data-kind="webhook"] > summary')

            # ---- email
            pg.click('details.chan[data-kind="email"] > summary')
            body = 'details.chan[data-kind="email"] [data-body]'
            pg.fill(body + ' input[name="host"]', "127.0.0.1")
            pg.fill(body + ' input[name="port"]', str(smtp_port))
            pg.select_option(body + ' select[name="security"]', "none")
            pg.fill(body + ' input[name="password"]', "app-password-xyz")
            pg.fill(body + ' input[name="from"]', "sentry@example.com")
            pg.fill(body + ' input[name="to"]', "me@example.com")
            pg.click(body + ' [data-act="save"]')
            pg.wait_for_timeout(800)
            v = api("alerts")
            e = next(x for x in v["channels"] if x["id"] == "email")
            check(f"[{scheme}] email saved, password write-only", e["enabled"] and e["secrets"]["password"] and "app-password-xyz" not in json.dumps(v))
            pg.click(body + ' [data-act="test"]')
            pg.wait_for_function("document.querySelector('details.chan[data-kind=\"email\"] [data-msg]').textContent.startsWith('Delivered')", timeout=20000)
            check(f"[{scheme}] email test delivered to the SMTP stub", MAILS and "Subject: wifi-sentry test alert" in MAILS[-1])
            pg.select_option(body + ' select[name="security"]', "starttls")
            pg.click(body + ' [data-act="test"]')
            pg.wait_for_function("document.querySelector('details.chan[data-kind=\"email\"] [data-msg]').textContent.startsWith('Not delivered')", timeout=20000)
            check(f"[{scheme}] STARTTLS failure explained", "STARTTLS extension not supported" in text(body + " [data-msg]"), text(body + " [data-msg]"))
            shot("panel-email-error", 'details.chan[data-kind="email"]')
            pg.click('details.chan[data-kind="email"] > summary')

            # ---- options
            pg.click('details.chan[data-kind="opts"] > summary')
            body = 'details.chan[data-kind="opts"] [data-body]'
            pg.select_option(body + ' select[name="cooldown_s"]', "600")
            pg.click(body + ' button[name="notify_on_close"]')
            pg.fill(body + ' input[name="base_url"]', "http://192.0.2.5:8450/")
            pg.click(body + ' [data-act="save-opts"]')
            pg.wait_for_timeout(800)
            s = api("alerts")["settings"]
            check(f"[{scheme}] alert options saved", s == {"cooldown_s": 600.0, "notify_on_close": True, "base_url": "http://192.0.2.5:8450/"}, s)
            shot("panel-options", "#alertsSec")
            pg.click('details.chan[data-kind="opts"] > summary')

            # ---- exit delay + schedule
            pg.locator("#armSec").scroll_into_view_if_needed()
            pg.select_option("#delaySelect", "120")
            pg.wait_for_timeout(800)
            check(f"[{scheme}] exit delay changed", api("arming")["settings"]["delay_s"] == 120)
            pg.click("#schedSwitch")
            pg.wait_for_timeout(300)
            check(f"[{scheme}] schedule editor opens with a default window", pg.locator("#schedRows .win").count() == 1)
            pg.click("#schedAdd")
            pg.locator("#schedRows .win").nth(1).locator('button.dow[data-day="5"]').click()   # drop Saturday
            pg.locator("#schedRows .win").nth(1).locator('input[data-k="start"]').fill("23:00")
            pg.locator("#schedRows .win").nth(1).locator('input[data-k="start"]').dispatch_event("change")
            pg.click("#schedSave")
            pg.wait_for_timeout(1000)
            sch = api("arming")["settings"]["schedule"]
            check(f"[{scheme}] schedule saved", sch["enabled"] and len(sch["windows"]) == 2 and sch["windows"][1]["days"] == [0, 1, 2, 3, 4, 6]
                  and sch["windows"][1]["start"] == "23:00", sch)
            check(f"[{scheme}] saving didn't flip the state", api("status")["armed"] is False and "Saved" in text("#schedMsg"), text("#schedMsg"))
            shot("settings-schedule", "#armSec")
            pg.evaluate("window.scrollTo(0,0)")
            pg.wait_for_timeout(300)
            check(f"[{scheme}] hero says what the schedule does next", "schedule" in text("#armHint"), text("#armHint"))
            shot("hero-schedule")

            # ---- small prompt for an install that skipped setup and has no channels
            for k in ("ntfy", "webhook", "email"):
                api("alerts/channels/" + k, {}, "DELETE")
            api("onboarding", {"status": "skipped"})
            pg.reload()
            pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=20000)
            pg.wait_for_timeout(1200)
            check(f"[{scheme}] small 'set up phone alerts' prompt", pg.is_visible("#alertsPrompt") and pg.is_hidden("#setup"))
            shot("prompt")
            pg.click("#promptDismiss")
            pg.wait_for_timeout(600)
            check(f"[{scheme}] prompt dismissal stored server-side", pg.is_hidden("#alertsPrompt") and api("onboarding")["alerts_prompt"] is False)
            check(f"[{scheme}] no console errors", not logs, logs[:5])
            c.close()

            # ---- desktop overview
            c = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme, device_scale_factor=2)
            pg = c.new_page()
            api("onboarding", {"status": "active", "step": "alerts"})
            pg.goto(base + "/?token=" + a.token)
            pg.wait_for_function("document.querySelector('#conn').textContent.includes('Live')", timeout=20000)
            pg.wait_for_timeout(1500)
            shot("desktop")
            c.close()
        reset()
        api("onboarding", {"status": "done"})
        browser.close()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
