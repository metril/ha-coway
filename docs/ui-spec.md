# coway-bridge web UI — redesign spec

Single file `coway_bridge/app/coway_bridge/static/index.html`. Vanilla JS/CSS, no build, no external hosts/fonts/CDNs. Served in an HA ingress iframe: all URLs relative (`api/...`). Replace the current page wholesale.

## 1. Structure
App bar (sticky, 56px): left = inline fan glyph + "Coway Bridge" (16/500) + device switcher `<select>` (only if >1 device); right = live pill (`Live` green dot / `Reconnecting…` amber pulsing dot) + relay pill (`Relay: on|off|error`). Below: tab bar `Device · Diagnostics · Setup` (`role=tablist`, underline indicator 2px primary, 48px tall). Tab persisted in `location.hash` (`#device`, `#diagnostics`, `#setup`); default `#device`, but default to `#setup` when there are zero devices. Content max-width 1100px, centered, padding 16px (24px ≥900px).

## 2. Tokens (`:root`, dark overrides in `@media (prefers-color-scheme: dark)`)
| token | light | dark |
|---|---|---|
| --bg | #f4f6f8 | #111315 |
| --card | #ffffff | #1c1f22 |
| --card-2 (inset/tile) | #f1f3f5 | #25292d |
| --text | #1c1f22 | #e6e8ea |
| --muted | #5f6870 (5.6:1 on card) | #9aa3ab (6.9:1) |
| --line | #e2e6ea | #30353a |
| --primary | #0288d1 (text/fills on white, 4.6:1) | #18bcf2 |
| --on-primary | #ffffff | #00222e |
| --primary-soft | #e1f3fd | #0f3442 |
| --ok | #2e7d32 | #66bb6a |
| --warn | #b26a00 | #ffb74d |
| --crit | #c62828 | #ef5350 |
| --unk-bg (unknown reg row) | #fff7e0 | #33290f |
Soft variants for pills: `color-mix(in srgb, var(--ok) 14%, transparent)` (same for warn/crit).
Radius: --r-card 16px, --r-tile 12px, --r-ctl 10px, --r-pill 999px. Elevation: --e1 `0 1px 2px rgb(0 0 0/.06),0 1px 3px rgb(0 0 0/.08)`; dark --e1 `0 0 0 1px var(--line)`. Spacing scale 4/8/12/16/24/32. Font: `system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif`; mono `ui-monospace,"SF Mono",Menlo,Consolas,monospace`. Type: display 40/48 600 (PM value), title 20/28 500, h2 16/24 500, body 14/20 400, label 12/16 500 uppercase .04em letter-spacing (--muted), mono 12/18. `font-variant-numeric: tabular-nums` on all numbers.

## 3. Device tab
Desktop ≥900px (2-col grid, 16px gap, hero spans left col, tiles right col 2×2):
```
+---------------------------------------+  +-----------------+-----------------+
| Airmega 400S          (o Online)      |  | AIR QUALITY     | LIGHT           |
| last seen 3s ago · FW 0.0.0.10        |  |  12  µg/m³      |  893 lx         |
|                                       |  |  [● Good]       |  Bright         |
|        ( (  ⏻  ) )   <- 96px          |  |  PM10           |                 |
|           On                          |  +-----------------+-----------------+
| MODE  [ Smart | Bedtime | Manual ]    |  | PRE-FILTER      | HEPA (MAX2)     |
| FAN   (fan svg)  [ 1 | 2 | 3 ]        |  |   (ring 100%)   |   (ring 0%)     |
| LIGHT [toggle]                        |  |   OK            |   ⚠ Replace now |
| TIMER  (Off)(1h)(2h)(4h)(8h)          |  +-----------------+-----------------+
| SENSITIVITY (Low)(Medium)(High)       |
| ✓ Speed set to 2 · acked 16:03:53     |  <- result line (aria-live)
+---------------------------------------+
```
Mobile <900px: single column; order hero → AQ+Light (2-col tiles, still side by side ≥360px) → filters (2-col). Hero controls full width; chips wrap.

### Hero card
- Header: name `Airmega 400S` (title), status pill: Online (ok dot) / Offline (muted grey dot + text "Offline"). Subline (muted): `Last seen 3s ago · FW x` (relative time ticks every 1s; switch to `hh:mm` after 1h).
- Power button: 96px circle centered, `aria-pressed`, label below ("On"/"Off"). On: bg --primary, icon --on-primary, outer ring 6px --primary-soft. Off: bg --card-2, icon --muted. Hover: brightness 1.05; active: scale .96; focus: 3px ring --primary offset 3px.
- Rows (label column 96px desktop / label above control on mobile <480px):
  - Mode: segmented control (`role=radiogroup`, each `role=radio`), 40px tall, container --card-2 radius --r-ctl, selected segment --card with --e1 and --primary text. Values Smart/Bedtime/Manual.
  - Fan: 32px inline SVG fan (3 blades) + segmented `1 | 2 | 3`. Fan spins only when power on: duration 2.4s/1.4s/0.8s for speed 1/2/3; static when off or reduced-motion. In Smart/Bedtime the speed segments remain enabled (tapping one switches to Manual — say so in helper text under the row: "Choosing a speed switches to Manual.").
  - Light: switch (`role=switch`, 52×32 track, 24px thumb), label "Display light".
  - Timer: chips Off/1h/2h/4h/8h (radiogroup, 36px tall, min 44px hit area via padding). Selected chip: --primary-soft bg, --primary text, check glyph.
  - Sensitivity: chips Low/Medium/High (map insensitive/moderate/sensitive = reg 3/2/1; `title` shows the device term).
- When power is Off: all rows below power get `opacity:.5` but stay operable (setting speed powers on — backend already does this; other writes are allowed).
- Offline: entire control block disabled (`inert` + opacity .5) with an inline notice above it: "Purifier offline — controls unavailable. Last seen 12m ago." Never disable without this text.

### Optimistic commands (all hero controls)
1. On interaction: apply new value to UI immediately; mark control `aria-busy=true`, show 16px spinner in that row's label; other controls stay usable, but a second click on the *same* control cancels nothing — queue it (latest wins).
2. POST; on `ok:true`: clear pending; result line `✓ <Label> set to <value> · acked hh:mm:ss` (--ok).
3. On failure/timeout: revert to last confirmed value, shake row 1×(200ms, translateX ±4px; skipped with reduced motion), result line `✕ <Label> failed: <error>. Reverted.` (--crit) + "Retry" text button.
4. Incoming WS `reg` updates for a control with a pending command are ignored until it resolves; afterwards the latest reg value wins.
Result line: one line under hero, `role=status aria-live=polite`, fades after 6s (success only; errors persist until next action).

### Tiles (card, padding 16px, min-height 132px)
- Air quality: label "AIR QUALITY", value PM10 display size + `µg/m³` (14 muted), band pill (dot + text, never color alone), caption "PM10". Bands (US EPA 24h PM10): 0–54 Good (--ok), 55–154 Moderate (--warn), 155–254 Unhealthy for sensitive groups (--warn, bold), 255+ Unhealthy (--crit). Missing → "—" and pill "No data".
- Light: value `893 lx`; caption qualitative: <10 Dark, <200 Dim, <1000 Bright, else Very bright.
- Filter gauges (Pre-filter, HEPA (MAX2)): remaining = 100 − used, clamped 0–100. 72px SVG ring (stroke 8, track --line, arc color by state), % in center (h2). State text below ring always rendered: ≥20 "OK" (--ok), <20 "⚠ Replace soon" (--warn), <5 "⛔ Replace now" (--crit). Ring has `role=img aria-label="HEPA filter 0% remaining, replace now"`.

## 4. Diagnostics tab
Desktop ≥900px: grid `340px 1fr`; left column stacks Status card, Registers card, Tools card; right column = Frame log card, height `calc(100vh - 140px)` sticky top 120px. Mobile: Status → Tools → Registers (collapsed `<details>`) → Frame log (height 70vh).
```
+ Status ------------+  + Frame log ------------------------------------------+
| Connected  ● ok    |  | [P→A][A→P] [search /______] [Pause][Clear][⇩]  117/117|
| MAC   0008DC000001 |  |-----------------------------------------------------|
| Serial 00000TST... |  | 16:06:18.024 [P→A] A1020 pm10=0 lux=893 …         ▸ |
| FW 0.0.0.10 ...    |  | 16:06:18.410 [A→P] A1011 speed=2                  ▸ |
+ Registers ---------+  |   ▾ decoded fields (key/value grid) + raw hex/text  |
| [filter][unknown]  |  |                                                     |
| CONTROL            |  |                         [ 12 new frames ↓ ]         |
| 0001 power 0001 On |  +-----------------------------------------------------+
+ Tools -------------+
| reg[____] val[____] [Write]   [Request snapshot]
```
- Status card: dl grid (label 12 muted / value mono 13): State (pill), MAC (+copy icon button), Serial (+copy), Firmware, Last seen, Connected since, Upstream (relay state, retry_in, error in --crit).
- Registers: compact table 32px rows, columns Reg (mono) · Name · Raw (mono) · Decoded · Updated (relative). Group subheaders CONTROL / SENSORS (label style, sticky). Unknown regs: --unk-bg row + small `?` badge (`title="Unknown register"`, sr-only "unknown"). Filter input + "Unknown only" switch. Changed value flashes --primary-soft 600ms (none with reduced motion).
- Frame log: mono 12/18. Row = time (ms) · dir badge (P→A: --primary-soft/--primary; A→P: --card-2/--text, both with text) · cmd (bold) · summary (ellipsis) · chevron. Click/Enter toggles expansion showing `fields` as key/value grid + raw (wrapped, selectable) + "Copy raw". Toolbar: dir toggle buttons (`aria-pressed`), search (`/` focuses, debounced 150ms, matches cmd/summary/raw), Pause/Resume (paused shows "Paused · N held" badge), Clear (two-step: becomes "Confirm clear" for 3s), Download (`api/capture?mac=`), counter `shown / total`. DOM cap 500 rows (drop oldest), memory cap 2000. Auto-stick to bottom when scrolled within 40px of bottom; otherwise show floating "N new frames ↓" pill. Empty: "No frames yet — they appear live as the purifier talks." Filtered-empty: "No frames match." + "Reset filters".
- Tools card: Raw write (reg + val, 4-hex each, `pattern`, uppercase on input, inline error under field: "4 hex digits, e.g. 0003"), Write button primary; warning text muted: "Writes go straight to the purifier. Unknown registers can misbehave." Snapshot button secondary. Both report via a result line in the card (same format as hero).

## 5. Setup tab
Single card, max-width 720px, vertical stepper (numbered 28px circles on a 2px --line rail; completed steps not tracked — purely instructional):
1. **Put the purifier in Wi-Fi setup mode** — "See the purifier's manual or the IoCare app instructions." (No button sequence.)
2. **Join its setup network** from a laptop — "Connect to the open network the purifier broadcasts (usually named `Airmega…`)."
3. **Download the setup tool** (Python 3, no dependencies) — code block + Copy:
   `curl -O https://raw.githubusercontent.com/metril/ha-coway/main/coway_bridge/app/coway_bridge/setup_cli.py`
4. **Run it** — inputs: Serial (prefilled from selected device if known) and Wi-Fi SSID; code block updates live and is copyable:
   `python3 setup_cli.py --serial <serial> --ssid "<ssid>"`. Shell-quote SSID (escape `"`, `\`, `$`, `` ` ``). Placeholders shown as `<serial>`/`<ssid>` when empty. Note: "The tool asks for the Wi-Fi password itself — it is never entered here."
5. **Reconnect** your laptop to normal Wi-Fi. The purifier should appear on the Device tab within ~1 minute.
Below stepper: "Purifier not showing up?" `<details open>` with the pfSense checklist (see §6).
Code block: --card-2, radius 8, mono 13, horizontal scroll, Copy button top-right (text "Copy" → "Copied" 1.5s; fallback select-all if clipboard API denied).

## 6. Empty / offline / error states
- **No devices** (Device + Diagnostics tabs): centered card, fan illustration (64px, static), title "No purifier connected yet", body "The bridge is listening on port 9090. Point the purifier's cloud hostname at Home Assistant:", checklist (static `<ol>`): 1) pfSense → Firewall → NAT → Port Forward on the IoT interface: source = purifier IP, destination = any (not a fixed cloud IP), TCP 9090 → HA IP:9090. (DNS overrides do not work: the purifier also queries 8.8.8.8.) 2) Allow IoT → HA:9090 in the filter rules. 3) Make the purifier reconnect: kill its 9090 state in Diagnostics → States, or deauth it from the Wi-Fi AP. Never unplug it — it loses its Wi-Fi credentials. 4) Check the bridge log for the purifier connect / TLS handshake warnings. Primary button "Wi-Fi setup →" (switches to Setup tab). Text: "Not on Wi-Fi yet? Use Setup first."
- **Offline device**: hero notice (§3) + Status pill Offline; tiles show last values with caption "as of hh:mm" muted.
- **WS disconnected**: app-bar pill "Reconnecting…" (backoff 1→2→4…→30s), plus thin 2px --warn bar under app bar; values remain. On reconnect refetch `api/state` + `api/frames`.
- **API error on initial load**: full-card error "Can't reach the bridge" + "Retry".

## 7. Motion
Durations: 120ms (hover/press), 200ms (segment slide, toggle thumb, tab underline), 600ms (value flash). Easing `cubic-bezier(.2,0,0,1)`. Segment selection indicator slides between options. `@media (prefers-reduced-motion: reduce)`: all transitions/animations `none` (fan static, no shake, no flash, spinner replaced by "…").

## 8. Accessibility
WCAG 2.1 AA. Every interactive ≥44×44 hit area. Visible focus: `:focus-visible { outline: 3px solid var(--primary); outline-offset: 2px }`. Tabs: arrow keys move, Home/End. Radiogroups: roving tabindex + arrow keys. Switch toggles with Space. Status info never color-only (pills/gauges carry text/glyph). Icons have `aria-hidden` with adjacent text, or `aria-label` on icon-only buttons (copy, download). Frame log `role=log aria-live=off`; hero result line `aria-live=polite`; errors `role=alert`. `lang="en"`, logical heading order (h1 app name visually small, h2 cards). Respect 200% zoom at 400px without horizontal page scroll (only log/table/code scroll internally).

## 9. Favicon
In `<head>`: `<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%2303a9f4'/%3E%3Cpath d='M16 16c0-6 3-9 6-8s1 6-6 8zm0 0c6 0 9 3 8 6s-6 1-8-6zm0 0c0 6-3 9-6 8s-1-6 6-8zm0 0c-6 0-9-3-8-6s6-1 8 6z' fill='white'/%3E%3C/svg%3E">`. Reuse the same fan path for the app-bar glyph and fan SVG.

## 10. Backend additions (web.py)
Add command types to `WebApp._writes` (reject otherwise with existing 400):
- `{"type":"mode","value":"smart"|"bedtime"|"manual"}` → smart `{R_MODE:1}`, bedtime `{R_MODE:2}`, manual `{R_SPEED: current speed or 1}`; prepend `R_POWER:1` if off.
- `{"type":"timer","value":"off"|"1h"|"2h"|"4h"|"8h"}` → `{R_TIMER: inverse(R.TIMER)}`.
- `{"type":"sensitivity","value":"insensitive"|"moderate"|"sensitive"}` → `{R_SENS: inverse(R.SENSITIVITY)}`.
Move the preset/timer/sensitivity → writes mapping into a shared helper in `registers.py` (`writes_for(entity, value, control) -> dict|None`) used by both `mqtt_bridge.py` and `web.py`, so behaviour cannot drift. Add tests in `tests/test_web*.py` for each new type (valid + invalid value), xdist-safe.
UI reads state from `device.registers` by `group`+`name` (control: power/mode/speed/light/timer/sensitivity; sensor: pm10/lux/prefilter_used/hepa_used), parsing `raw` hex for numbers rather than `decoded` strings.

## 11. Acceptance
Screenshots via Playwright at 1280×800 and 400×860, light and dark, for: Device (online), Device (no devices), Diagnostics, Setup. No console errors, no 404s (favicon), no horizontal page scroll at 400px, Lighthouse a11y ≥ 95.
