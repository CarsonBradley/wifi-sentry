# Product

<!-- impeccable:product-schema 1 -->

> Written for the dashboard redesign without a live interview: every fact below comes from the
> repository (README, docs/, the engine's API) or from the written redesign brief. Facts that are
> inferences rather than statements are marked *(inferred)*.

## Platform

web

## Users

One person, the owner of the Mac that runs wifi-sentry. They open the dashboard on their phone,
often away from home over Tailscale, sometimes on the Mac itself. A visit answers three questions,
in this order:

1. Is anything moving at home right now?
2. What happened while I was away?
3. Is the system actually watching?

Occasional visits are for setup and tuning: the first-run guide, a walk test, sensitivity, phone
alerts, the arming schedule. Since the project is going public, a second audience reads the README
screenshots before installing *(from the brief)*.

## Product Purpose

wifi-sentry turns a Mac's own Wi-Fi radio into a motion sensor: it reads how the router's signal
fluctuates at the Mac's antennas, learns the still-room baseline and opens an event when the
activity score stays above a trigger level. The dashboard shows that live, keeps the event log, and
is where arming, alerts and calibration are controlled. Success is an owner who trusts the answer at
a glance, including when the answer is "I can't tell right now".

## Positioning

No camera, no microphone, no extra hardware: one number per antenna from a radio the user already
owns. It is a coarse, honest instrument. It cannot say where, who or how many, and the product says
so rather than overselling.

## Operating Context

- Glanced at on a phone (390 px class) in daylight or at night, often on a slow or flaky link; also
  used at desktop width on the Mac.
- Plain HTTP over a tailnet or loopback; iOS Safari; no service worker.
- Token login (required on loopback too; cookies last 30 days).
- Phone alerts go out through ntfy, a webhook, or email; the page itself can also chime while open.

## Capabilities and Constraints

- States the dashboard must represent: connecting, locked, disconnected / reconnecting / updates
  stopped, learning, clear, motion, sensor offline, can't sense (sensor online but unusable),
  reduced and degraded sensing, ESP32 sensors including "channel keeps changing".
- Arming with an exit-delay countdown and an optional weekly schedule.
- Live activity chart with 15 m / 1 h / 6 h / 24 h ranges, event bands and "not watching" gaps.
- Events list grouped by day with real / false-alarm labels, notes and pagination, plus the
  "since you last checked" summary.
- Sensitivity (low / medium / high), walk test, baseline relearn, first-run setup guide.
- Plain HTML/CSS/JS, no build step, no external requests (system fonts or fonts vendored under a
  redistributable licence), light and dark. The engine serves only files directly inside `web/`.
- The engine and API are frozen for this release.

## Brand Commitments

- Name: wifi-sentry, written in lower case.
- Voice: plain, specific, calm and honest about limits ("Not watching", never a stale "All clear").
- No commitments on logo, colour or type *(inferred: the existing shield mark and palette were never
  stated as binding, and the owner asked for the look to change)*.

## Evidence on Hand

- Measurements from one Mac in one studio apartment, documented in docs/how-it-works.md.
- No users, testimonials or benchmarks beyond that. Screenshots must use simulated data and say so.

## Product Principles

1. Never show a stale or unearned "all clear": connection, sensing and arming states are first-class.
2. The live trace against the trigger level is the product; everything else explains or acts on it.
3. Calm by default, unmistakable when something moves.
4. Say what it can't see.

## Accessibility & Inclusion

WCAG AA text contrast in both themes, visible focus, full keyboard operation, reduced motion
respected, touch targets of at least 44 px, labelled form fields, state changes announced through a
live region, and motion versus clear never signalled by colour alone.
