---
name: wifi-sentry
description: A chart recorder's day chart for a Wi-Fi motion sensor; printed chart paper, pen ink, one vermilion for motion.
colors:
  ground: "oklch(98.6% 0.005 170)"
  ground-2: "oklch(95.6% 0.01 170)"
  ink: "oklch(21% 0.018 200)"
  ink-2: "oklch(37% 0.018 200)"
  ink-3: "oklch(46% 0.018 200)"
  rule: "oklch(89% 0.018 170)"
  rule-strong: "oklch(57% 0.03 185)"
  print: "oklch(44% 0.085 172)"
  grid: "oklch(72% 0.075 168)"
  grid-faint: "oklch(88% 0.035 168)"
  pen: "oklch(31% 0.075 255)"
  act: "oklch(42% 0.085 182)"
  act-hover: "oklch(36% 0.085 182)"
  act-ink: "oklch(99% 0 0)"
  act-soft: "oklch(93% 0.03 180)"
  motion: "oklch(52% 0.19 33)"
  motion-bg: "oklch(95% 0.028 35)"
  motion-band: "oklch(60% 0.19 33 / 0.13)"
  quiet-band: "oklch(55% 0.02 200 / 0.11)"
  watch: "oklch(49% 0.1 70)"
  watch-bg: "oklch(95.5% 0.035 85)"
  watch-hatch: "oklch(66% 0.11 75 / 0.55)"
  watch-hatch-bg: "oklch(80% 0.09 80 / 0.12)"
  focus: "oklch(50% 0.14 245)"
  selection: "oklch(88% 0.06 175)"
  ground-dark: "oklch(17.5% 0.012 195)"
  ground-2-dark: "oklch(21.5% 0.014 195)"
  ink-dark: "oklch(94% 0.008 180)"
  ink-2-dark: "oklch(81% 0.012 180)"
  ink-3-dark: "oklch(71% 0.014 180)"
  rule-dark: "oklch(29% 0.016 185)"
  rule-strong-dark: "oklch(52% 0.025 185)"
  print-dark: "oklch(77% 0.085 170)"
  grid-dark: "oklch(47% 0.06 170)"
  grid-faint-dark: "oklch(29% 0.03 170)"
  pen-dark: "oklch(89% 0.035 230)"
  act-dark: "oklch(76% 0.09 175)"
  act-hover-dark: "oklch(82% 0.08 175)"
  act-ink-dark: "oklch(17% 0.02 195)"
  act-soft-dark: "oklch(28% 0.035 180)"
  motion-dark: "oklch(72% 0.16 35)"
  motion-bg-dark: "oklch(27% 0.055 30)"
  motion-band-dark: "oklch(70% 0.16 35 / 0.2)"
  quiet-band-dark: "oklch(75% 0.02 190 / 0.12)"
  watch-dark: "oklch(81% 0.12 80)"
  watch-bg-dark: "oklch(26% 0.04 80)"
  watch-hatch-dark: "oklch(75% 0.11 80 / 0.45)"
  watch-hatch-bg-dark: "oklch(75% 0.09 80 / 0.07)"
  focus-dark: "oklch(76% 0.12 240)"
  selection-dark: "oklch(38% 0.06 180)"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "40px"
    fontWeight: 700
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  readout:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "26px"
    fontWeight: 650
    lineHeight: 1.05
    letterSpacing: "-0.02em"
    fontFeature: "\"tnum\""
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "17px"
    fontWeight: 650
    letterSpacing: "-0.01em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "15px"
    fontWeight: 650
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.47
  fine:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "13px"
    fontWeight: 600
    fontFeature: "\"tnum\""
  chart:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", system-ui, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "11px"
    fontWeight: 400
  mono:
    fontFamily: "ui-monospace, \"SF Mono\", SFMono-Regular, Menlo, monospace"
    fontSize: "13px"
    lineHeight: 1.45
rounded:
  control: "3px"
  thumb: "2px"
  popover: "4px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  section: "24px"
  column-gap: "56px"
  gutter: "16px"
  hit: "44px"
components:
  button:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.title}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "44px"
  button-hover:
    backgroundColor: "{colors.ground-2}"
  button-primary:
    backgroundColor: "{colors.act}"
    textColor: "{colors.act-ink}"
    typography: "{typography.title}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.act-hover}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.act}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "44px"
  button-ghost-hover:
    backgroundColor: "{colors.act-soft}"
  scale-segment:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    height: "44px"
    padding: "0 8px"
  scale-segment-selected:
    backgroundColor: "{colors.act}"
    textColor: "{colors.act-ink}"
  switch:
    backgroundColor: "{colors.ground-2}"
    rounded: "{rounded.control}"
    width: "46px"
    height: "44px"
  switch-on:
    backgroundColor: "{colors.act}"
  tab:
    textColor: "{colors.ink-3}"
    typography: "{typography.label}"
    height: "44px"
    padding: "0 8px"
  tab-selected:
    textColor: "{colors.ink}"
  input:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "44px"
  notice:
    backgroundColor: "{colors.ground-2}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.control}"
    padding: "12px 12px 12px 14px"
  tooltip:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
    rounded: "{rounded.control}"
    padding: "8px 10px"
---

# Design System: wifi-sentry

## Overview

**Creative North Star: "The Chart Recorder's Day Chart"**

The dashboard is a sheet of printed chart paper, not a stack of cards. Hairline rules and graticule are printed in a teal chart green, the data is drawn in blue-black pen ink, the trigger level and motion are vermilion, and time the system was not watching is an ochre hatch. Status is a single sentence set large over the instrument; everything else is a log read top to bottom between hairlines.

Density is calm on a phone (390 px first) and two-column at desktop, where the instrument (status, arm switch, day chart) holds the left column and the log scrolls on the right. Colour is flat, with no texture, gradient or grain. Light and dark follow the system; at night the paper turns green-black and the pen turns pale.

**Key Characteristics:**
- Hairline rules divide content; nothing sits in a card.
- One saturated action colour, used only on things that can be pressed.
- Vermilion means motion or the trigger, and nothing else.
- State is drawn by form as well as colour: hollow vs filled marks, solid vs striped rims.
- Every number is tabular.

## Colors

A cool chart-paper ground with three working inks (print green, pen blue-black, vermilion) and one warning ochre. All values are OKLCH; every role has a dark-scheme counterpart (the `-dark` keys) applied under `prefers-color-scheme: dark`.

### Primary
- **Chart-Paper Green** (`print`): section headings, day-group heads, chart time labels, the live connection dot, healthy-sensor marks, "Online". The printed layer of the paper.
- **Pressable Teal** (`act`, `act-hover`, `act-soft`): fills on primary buttons, the on switch, the selected scale segment and weekday, the tab underline, links, "To review" and "New". Never used for decoration.

### Secondary
- **Vermilion** (`motion`, `motion-bg`, `motion-band`): the trigger line and ring, trace above the trigger, armed-motion bands and rims, the status word during motion, "Real" labels, errors.

### Tertiary
- **Ochre** (`watch`, `watch-bg`, `watch-hatch`, `watch-hatch-bg`): not watching, offline, disconnected, stale connection, reduced sensing, the coverage-gap note in the digest.

### Neutral
- **Paper** (`ground`) and **Paper Shade** (`ground-2`): page ground; hover fill, notices, notes, code blocks.
- **Ink scale** (`ink`, `ink-2`, `ink-3`): primary text; secondary text and sublines; captions, axis values, the idle pen arm, disarmed event rims.
- **Rules** (`rule`, `rule-strong`): hairline dividers; control borders and outlined buttons.
- **Graticule** (`grid`, `grid-faint`): major and minor chart lines, track backgrounds for meters and progress.
- **Pen Ink** (`pen`): the activity trace below the trigger and the per-sensor meter.
- **Quiet band** (`quiet-band`): event bands recorded while disarmed.
- **Focus** (`focus`) and **Selection** (`selection`).

### Named Rules
**The One Pressable Colour Rule.** Teal fills appear only on controls; a surface that can't be pressed never carries it.

**The Vermilion Means Motion Rule.** Vermilion marks the trigger level, motion and hard errors. Nothing decorative or merely "important" may use it.

**The Honest Gap Rule.** Time not watched is always drawn as ochre hatch, never left blank and never shown as clear.

## Typography

**Display Font:** system sans (`-apple-system`, `system-ui`, Segoe UI, Roboto, Helvetica Neue)
**Body Font:** the same system sans
**Label/Mono Font:** `ui-monospace` / SF Mono / Menlo, for tokens and commands only

**Character:** One native sans at a handful of weights (400, 600, 650, 700), with tight negative tracking on large sizes and tabular numerals wherever a number can change.

### Hierarchy
- **Display** (700, 40px, 1.02, -0.035em): the status word ("All clear", "Motion", "Not watching"). Also the walk-test timer (650). Colour follows state.
- **Readout** (650, 26px, 1.05): the live value in the day chart hub; vermilion when above trigger.
- **Headline** (650, 17px, print green): section heads. The setup guide's step title runs 22px in ink.
- **Title** (650, 15px): event times, digest lead, sub-heads; status subline is 16px ink-2.
- **Body** (400, 15px, 1.47): running text; long copy holds to 60–65ch.
- **Fine** (400, 13px, ink-3): captions, sensor kind, event sublines, field help.
- **Label** (600, 13px): state marks, tab labels, field labels, day-group heads.
- **Chart** (400 / 600, 11px): canvas axis and hour labels; 600 for the trigger value and "now".

### Named Rules
**The Tabular Rule.** Any number that updates or aligns in a column uses `tabular-nums`.

**The Sentence-Case Rule.** No uppercase labels, no tracked-out small caps. Headings are plain sentence case.

## Layout

Single column at phone width with a 16px gutter (respecting safe-area insets). From 960px the page becomes two columns inside a 1200px max width: the instrument column (max 440px) and the log column, 56px apart. The instrument column turns sticky (64px below the top bar) only while all of it fits in the window, and the day chart sizes to the window height (minimum 300px).

Spacing works on a 4px module shared with the chart graticule: 4 / 8 / 12 / 16 for internal gaps, about 22–24px vertical padding per section, 44px minimum for every row that can be pressed. Sections are separated by a 1px top rule, not by space alone. Event rows are a three-track grid (76px time, flexible body, right-aligned state mark) at least 58px tall; expanded detail indents 88px from 480px up.

The top bar is 52px, sticky, and gains a bottom hairline only once the page scrolls. The strip chart is 190px tall on phones, 230px from 960px.

## Elevation & Depth

Flat. Depth comes from hairlines and the paper shade, never from layered surfaces. One soft shadow exists, and only on things that float over the page: the alerts popover and the toast (`0 10px 28px -10px` plus `0 2px 6px`, ink-tinted; darker in dark mode). The switch uses inset 1px strokes as borders, not as depth.

### Named Rules
**The Paper Is Flat Rule.** Nothing in the page flow casts a shadow; a shadow means "this is floating above the page and will go away".

## Shapes

Square-cornered and drawn. Controls carry a 3px radius (`control`), the switch thumb 2px, the alerts popover 4px. Content regions have no radius because they have no boxes: they are bands between hairlines. Inline status marks are 9px squares, hollow or filled; only the live-motion mark and the connection dot are round. Icons are 20px line drawings (1.6 stroke, round caps) from one inline sprite.

### Named Rules
**The 3px Rule.** Radius belongs to controls. If it isn't pressable or input-able, its corners are square or it has no edge at all.

## Components

### Buttons
- **Shape:** 3px corners, 44px minimum height, 16px side padding, 600 15px label.
- **Default:** outlined in `rule-strong` on transparent; hover fills `ground-2` and darkens the border to `ink-3`; active nudges down 1px.
- **Primary:** filled `act` with `act-ink` text; hover `act-hover`. One per decision.
- **Ghost:** borderless teal text, 10px padding; hover tints `act-soft`.
- **Icon button:** 44px square, ink-2 icon, `ground-2` on hover; an "on" state adds a 6px teal dot.
- **Disabled:** 45% opacity, no movement.

### Switch
Square-cornered 46×26px track (54×30 for the arm switch) inside a 44px-tall hit area. Off: `ground-2` track, hollow thumb outlined in `ink-3`. On: teal track, `act-ink` thumb slid right (180ms). Pending (waiting on the engine): soft teal track with a blinking teal thumb.

### Tabs and Scales
- **Range tabs:** text-only, 600 13px tabular, ink-3; selected turns ink with a 2px teal underline 8px above the baseline of the hit area.
- **Joined scale:** equal segments sharing 1px `rule-strong` borders, outer corners 3px; the selected segment is filled teal. The "Real" choice on an event fills vermilion instead. Weekday toggles use the same fill language as separate 3px squares.

### State Marks
A 9px square before a 600 13px label. **Hollow = still to review / not set up / not watching; filled = decided.** Colour adds meaning on top: teal for "To review", vermilion for "Real" and errors, ink-3 filled for "False alarm", print green filled for "On". The live-motion mark is the one round, blinking mark.

### Inputs / Fields
44px tall, 3px corners, 1px `rule-strong` border on `ground`, 16px text (prevents iOS zoom). Focus draws a 2px `focus` outline inset 1px. Selects use a drawn chevron in ink-2. Field labels are 600 13px ink-2 above the control.

### Notices and Notes
Paper-shade blocks with a 1px `rule` border and 3px corners, used sparingly for things that need to be acted on or read in place. Warning notices tint to `motion-bg`; sensing problems tint to `watch-bg`, each with a 45% border of its ink and a matching icon. Form messages follow the same tint scheme (`act-soft` ok, `motion-bg` error).

### Navigation
A sticky top bar: line-mark and wordmark (lower case) left, connection line ("Live" with a print-green dot; ochre when stale or down; blinking while connecting) and the alerts bell right. No other navigation; the page is one scroll.

### Strip Chart
Canvas, horizontal time. Graticule in `grid-faint` with a `grid` baseline; value labels ink-3 on the right axis, time labels print green below. Trigger level is a 1.25px vermilion line with its value printed in 600 vermilion on the axis. The trace is 1.25px pen ink below the trigger and 1.75px vermilion where it crosses above. Event bands fill the full height (`motion-band` when armed, `quiet-band` when disarmed); gaps are `watch-hatch-bg` overlaid with a 7px diagonal ochre hatch. Ranges: 15 min, 1 h, 6 h, 24 h.

### Day Chart (signature)
A 24-hour disc drawn like a chart recorder's paper:
- **The pen sits at 12 o'clock** and the paper turns once a day, so the past runs counterclockwise from the top. "Now" is printed in 600 ink above the pen.
- **Radial scale is square-root**, from an inner hub (36% of the radius) out to four times the trigger, which puts the **trigger ring exactly halfway** across the band. The ring is 1.25px vermilion with its value knocked out on ground at about one o'clock.
- **Graticule:** an hour line per hour (major every 3 hours in `grid`, minor in `grid-faint`), rings at the hub, the rim and quarter points, outside ticks every hour (7px every third), hour labels every 3 hours in print green.
- **Trace:** 0.9px pen ink inside the ring, 1.75px vermilion outside it.
- **Sectors:** armed events fill `motion-band`, disarmed events `quiet-band`, gaps get the ochre hatch. Minimum sector width about 1.2°, so a 10-second event still shows.
- **Rim lane** (5px, at the outer edge, minimum 9 minutes wide): **solid vermilion = motion while armed; solid ink-3 = motion while disarmed; outlined, dash-striped ochre = not watching.** The not-watching mark differs from motion by form, not only colour.
- **The live pen:** a 2px arm from the hub to a 4px dot at the live value; ink when below trigger, vermilion above. The hub shows the live value (26px readout) and the trigger as a caption.
- **Touch to read:** pointer or finger over the disc draws a 1px ink-3 radius at that time and a dark tooltip (`ink` ground, `ground` text, 12px tabular) with the event, gap or value there.
- A legend below names Activity, Trigger level, Motion and Not watching with miniature swatches of the same marks.
- When data is stale the canvas drops to 40% opacity.

### Event Log
Day groups headed in 600 13px print green with a count right, then rows divided by hairlines: tabular time (with a teal "New"), length and strength, armed state and trigger multiple in fine text, a state mark and a chevron. Rows open in place (`details`) to label real / false alarm and add a note.

## Do's and Don'ts

### Do:
- **Do** separate content with 1px hairlines (`rule`) and section heads in print green, not with boxes.
- **Do** keep every pressable target at least 44px in both dimensions where it can be.
- **Do** draw state by form as well as colour: hollow vs filled squares, solid vs striped rims, ring vs trace weight.
- **Do** use `tabular-nums` on every changing number and keep chart labels at 11px.
- **Do** keep motion to state changes of 150–200ms (colour, switch thumb 180ms, chevron 200ms), plus the 1.2–1.4s blink on connecting, pending and live marks; turn all of it off under `prefers-reduced-motion`.
- **Do** use a visible 2px `focus` outline on every control (offset 2px; inset on text fields).
- **Do** read chart colours from the CSS tokens so both themes draw correctly.

### Don't:
- **Don't** put content in rounded cards, tinted hero tiles or pill toggles; the switch is square-cornered.
- **Don't** use teal on anything that can't be pressed, or vermilion for anything but motion, the trigger and errors.
- **Don't** signal motion vs clear, or watching vs not, by colour alone.
- **Don't** show a blank or "clear" chart region for time that wasn't watched; hatch it in ochre.
- **Don't** shadow anything in the page flow; shadows are for the popover and toast only.
- **Don't** add uppercase labels, kickers, external font requests, textures or gradients.
