# coway-bridge debug UI spec

Single `static/index.html` (inline CSS + JS, no CDN, no build). Served in an HA ingress iframe.
**All URLs relative** (`api/state`, never `/api/state`); WS URL = `new URL('api/ws', location.href)` with `http`→`ws`.

## Layout

Desktop (≥ 900px): 2-column grid `minmax(320px, 1fr) 2fr`, gap 16px, page padding 16px, max-width 1440px.
Left column = Status, Actions, Setup (collapsed). Right column = Registers, Frame log (log fills remaining height).
Mobile (< 900px): single column in order Status, Actions, Registers, Log (fixed 60vh), Setup.
Device picker appears in the header only when > 1 device; otherwise hidden.

```
+---------------------------------------------------------------------------+
| coway-bridge   [Device: 0008DC000001 v]           (*) WS live  Relay: off |
+--------------------------+------------------------------------------------+
| STATUS                   | REGISTERS            [filter____] [x] unknown  |
| (*) Connected            | Reg  Name        Value  Decoded     Updated    |
| MAC  0008DC000001        | 0001 power       0001   On          12:03:01   |
| FW   1.0.4               | 0003 fan_mode    0001   Manual      12:03:01   |
| Last seen 3s ago         | !000E reg_000E   0001   unknown     12:02:51   |
| Upstream: -- (relay off) |                                                |
+--------------------------+------------------------------------------------+
| ACTIONS                  | FRAME LOG  [All|P>A|A>P|C>P|P>C] [search__]    |
| Power [On][Off]          |   [Pause] [Clear] [Download]  412 / 2000       |
| Speed [1][2][3]          | 12:03:01.204 P>A  A1010RM ... 3B  power=On ... |
| Light [On][Off]          | 12:03:01.210 A>P  A1010SM ... 7C  ack          |
| Raw: reg[____] val[____] |  ...                                           |
|      [Write]             |                                                |
| [Request snapshot]       |                                                |
| status: "Sent, acked"    |                                                |
+--------------------------+                                                |
| > SETUP (Wi-Fi)          |                                                |
+--------------------------+------------------------------------------------+
```

## Components and states

**Header**: title, device picker (`<select>`), WS indicator (dot + text: "Live" / "Reconnecting…" / "Offline"), relay badge ("Relay: off" / "Relay: up" / "Relay: down").
- WS down: banner under header, `role="status"`: "Connection to bridge lost. Retrying in Ns." Retry backoff 1,2,4…30s. Data stays visible but dimmed (opacity .6) with "stale" label.

**Status card**: dot + label, MAC, FW, last seen (relative, ticks every 1s, absolute time in `title`), connected since, upstream.
- Connected: success dot + "Connected". Disconnected (no frame 60s): error dot + "Disconnected – last seen 4 min ago"; actions disabled with inline reason "Purifier offline; commands would be dropped."
- Relay down (relay on, upstream lost): warning badge + "Upstream down, local polling. Retry in Ns." Purifier status unaffected.
- No devices yet: Status, Registers, Log replaced by one empty-state card: "No purifier has connected yet." + 3 checks (NAT redirect to HAOS:9090, firewall IoT→HAOS:9090, `allowed_macs`) + "Open Setup" button that expands Setup.

**Registers table**: columns Reg (hex), Name, Raw (hex), Decoded, Updated. Two groups with `<tbody>` headers: Control, Sensors. Filter box (matches reg/name) + "Unknown only" checkbox.
- Unknown reg (`known:false`): row bg `--warn-bg`, leading "!" glyph, name `reg_XXXX`, Decoded "unknown". Text + glyph, not color only.
- Value change: Raw cell flashes `--accent-bg` for 1s (disabled under `prefers-reduced-motion`).
- Empty: "Waiting for first status report…".

**Frame log**: monospace table: time (`HH:MM:SS.mmm`), direction tag, raw (truncated with ellipsis, full on expand), decoded summary. Click/Enter on row toggles an expanded detail (full raw + decoded key/value list + "Copy raw").
- Direction tags (text label + color + left border): `P→A` purifier→app (blue), `A→P` app→purifier (green), `C→P` cloud→purifier (purple), `P→C` purifier→cloud (orange). Cloud chips are hidden from the filter when relay is off.
- Controls: direction segmented filter (multi-toggle, `aria-pressed`), text search (raw or decoded, debounced 150ms), Pause/Resume, Clear (clears view only; confirm inline: button becomes "Clear view? Yes / No" for 4s), Download (`GET api/capture`), counter "shown / buffered".
- Empty: "No frames yet. Frames appear here live." Filtered-empty: "No frames match the filter. [Reset filters]".
- Paused: button reads "Resume (N new)"; incoming frames buffered client-side (cap 2000, oldest dropped).
- Auto-scroll: newest at bottom; stick to bottom only if user is within 40px of it, else show "N new frames ↓" pill.
- High rate: see Performance.

**Actions card**: buttons Power On/Off, Speed 1/2/3, Light On/Off (current state rendered `aria-pressed="true"`), Raw write (reg: 4 hex chars, val: 4 hex chars, `pattern="[0-9A-Fa-f]{4}"`, uppercase on blur, inline error "Use 4 hex digits, e.g. 0001"), "Request snapshot".
- Pending: clicked button shows spinner + "Sending…", all action buttons `disabled` + `aria-busy`. Timeout 10s.
- Success: inline status line under card "Power On – acked 12:03:01" (success icon, 4s then fades to muted text, remains as last result).
- Failure: status line in error color, persistent: "Failed: purifier offline" / "No ack within 10s" / server `error` text. Never use toasts or modals.

**Setup card** (`<details>`, collapsed by default; auto-open when no devices): ordered steps:
1. Hold the purifier's Wi-Fi button until the Wi-Fi LED blinks (pairing mode).
2. Join the open Wi-Fi network `Airmega_400s` on a laptop.
3. Download the CLI: `curl -O https://raw.githubusercontent.com/metril/ha-coway/main/coway_bridge/app/coway_bridge/setup_cli.py`.
4. Enter your network below and run the generated command on that laptop.
5. Rejoin your normal Wi-Fi; the purifier should appear here within ~1 min.
Fields: Serial (label serial; prefilled from the connected unit), SSID. Live-rendered `<pre>`: `python3 setup_cli.py --serial 'SERIAL' --ssid 'My SSID'` (no password; the CLI prompts) + "Copy command". Nothing is sent to the bridge.

## Theming

Tokens on `:root`; dark overrides in `@media (prefers-color-scheme: dark)`. Match HA defaults.

| Token | Light | Dark |
|---|---|---|
| `--bg` | `#fafafa` | `#111111` |
| `--card` | `#ffffff` | `#1c1c1c` |
| `--text` | `#212121` | `#e1e1e1` |
| `--muted` | `#5f6368` (6.2:1) | `#9b9b9b` (6.6:1) |
| `--border` | `#e0e0e0` | `#2f2f2f` |
| `--accent` | `#03a9f4` bg / `#0277bd` text | `#4fc3f7` |
| `--ok` / `--warn` / `--err` | `#2e7d32` / `#b26a00` / `#c62828` | `#66bb6a` / `#ffb74d` / `#ef5350` |
| `--warn-bg` / `--accent-bg` | `#fff4e0` / `#e1f5fe` | `#3a2a10` / `#0d2a3a` |
| `--dir-pa` `--dir-ap` `--dir-cp` `--dir-pc` | `#1565c0` `#2e7d32` `#6a1b9a` `#b45309` | `#64b5f6` `#81c784` `#ce93d8` `#ffb74d` |

All text colors ≥ 4.5:1 on `--card`. Status always = icon/glyph + word, never dot alone.
Type: `font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`; card titles 16px/600 uppercase-off; mono `12.5px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`; numbers `font-variant-numeric: tabular-nums`.
Spacing 4px base: 4/8/12/16/24. Cards: radius 12px, padding 16px, 1px `--border`, no shadow. Buttons 36px high desktop, 44px min on `(pointer: coarse)`; radius 8px.

## Accessibility

- Landmarks: `<header>`, `<main>`, each card `<section aria-labelledby>`. Tables use real `<table>`, `<th scope>`.
- Live regions: connection/relay text `role="status" aria-live="polite"`; action result line `aria-live="polite"`, failures `role="alert"`. Frame log is NOT live (too noisy); the counter is plain text.
- Keyboard: all controls native buttons/inputs; log rows `tabindex="0"` with Enter/Space to expand, `aria-expanded`. Shortcuts (ignored while typing in inputs): `p` pause/resume, `/` focus log search.
- Focus: `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`. Never remove outlines. After pending action completes, focus stays on the triggering button.
- Disabled controls always have adjacent visible reason text. Respect `prefers-reduced-motion`.

## Performance

- WS messages appended to a queue; flush once per `requestAnimationFrame` (batch DOM via `DocumentFragment`).
- Client keeps up to 2000 frames in an array (matches server ring); DOM renders only the last 500 matching rows; older removed from the top. Filter changes re-render from the array.
- High rate (> 50 frames/s over 2s): show inline note "High frame rate – showing latest 500" and skip flash animations.
- Register table updates cells in place keyed by reg (no re-render). Relative times updated by a single 1s timer.
- Expanded log row decoded detail built lazily on expand.

## API (all paths relative to ingress root)

`GET api/state` → full snapshot, also sent as first WS message:
```json
{"bridge":{"version":"0.1.0","relay_enabled":true},
 "devices":[{"mac":"0008DC000001","fw":"1.0.4","online":true,"last_seen":"2026-11-15T12:03:01.204Z",
   "connected_since":"2026-11-15T11:00:00Z",
   "upstream":{"state":"up","since":"…","retry_in":null,"error":null},
   "registers":[{"reg":"0001","name":"power","group":"control","raw":"0001","decoded":"On","known":true,"updated":"…"}]}]}
```
`upstream.state`: `"disabled"|"connecting"|"up"|"down"`.

`GET api/frames?mac=&limit=2000` → `{"frames":[Frame…]}` (backfill on load). `GET api/capture?mac=` → `text/plain` attachment `coway-capture-<mac>-<ts>.txt`, `allframes.txt` format.

`POST api/devices/{mac}/command` body one of:
```json
{"type":"power","value":"on"}  {"type":"speed","value":2}  {"type":"light","value":"off"}
{"type":"raw","reg":"0001","val":"0001"}  {"type":"snapshot"}
```
→ `200 {"ok":true,"acked":true,"at":"…"}` | `409 {"ok":false,"error":"Purifier offline"}` | `504 {"ok":false,"error":"No ack within 10s"}` | `400` validation error.

`WS api/ws` server→client messages:
```json
{"t":"state","data":{…same as api/state…}}
{"t":"frame","data":{"id":4812,"mac":"0008DC000001","ts":"2026-11-15T12:03:01.204Z","dir":"P>A",
  "raw":"\u0002…\u0003","cmd":"A1010RM","summary":"power=On speed=1","fields":{"0001":"0001"}}}
{"t":"device","data":{…one device object…}}
{"t":"reg","data":{"mac":"…","reg":"0001","raw":"0001","decoded":"On","known":true,"updated":"…"}}
```
`dir` ∈ `P>A`, `A>P`, `C>P`, `P>C` (UI renders with arrows). Raw control chars shown as `␂`/`␃`. Client sends nothing except optional `{"t":"ping"}`; server pings every 20s.
