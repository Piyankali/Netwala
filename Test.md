# netwala

**Playwright-powered MCP server for browser automation, recon, and authorized
web-application security testing.**

59 custom MCP tools on top of a persistent, human-like Firefox: real rendering,
real input events, real network capture, and a fingerprint that stays
self-consistent. Ships with a payload library, WAF detection, HAR capture, and
an optional human-behaviour layer.

> ⚠️ **Authorized use only.** netwala is built for bug-bounty engagements and
> pentests where you have written permission to test the target. It is not a
> CAPTCHA-bypass service, and it is not for scraping at scale.

---

## Table of contents

1. [What it is](#what-it-is)
2. [Architecture](#architecture)
3. [Features](#features)
4. [Supported operating systems](#supported-operating-systems)
5. [Dependencies](#dependencies)
6. [Installation](#installation)
7. [Quick start](#quick-start)
8. [Configuration](#configuration)
9. [Connecting an MCP client](#connecting-an-mcp-client)
10. [Security model](#security-model)
11. [Anti-bot design](#anti-bot-design)
12. [Troubleshooting](#troubleshooting)
13. [Changelog](#changelog)
14. [Roadmap / upcoming features](#roadmap--upcoming-features)
15. [FAQ](#faq)
16. [License](#license)

---

## What it is

netwala is an MCP (Model Context Protocol) server that gives an LLM agent a
real browser. Unlike a DOM scraper, it drives a genuine Firefox through
Playwright: pages render with JavaScript, CSS, fonts and WebGL, input arrives
through trusted OS-level events, and every request/response can be inspected.

It is designed for the middle of a security workflow:

```
subfinder / nmap / katana   ->   netwala (live verify, WAF, evidence)
                               ->   nuclei / ffuf (known vulns, dirs)
                               ->   netwala (request-level IDOR, API testing)
                               ->   findings report
```

It is **not** a scanner, not a fuzzer, and not a brute-forcer. It is the
"eyes and hands" layer.

### Why Firefox and not Chromium

Chromium was removed. On ARM64 + PRoot (the primary Termux deployment) Chromium
is unstable, and headless Chromium leaks enough signals to be worthless against
modern bot protection. Firefox runs natively on AArch64 and, when configured
correctly, presents a consistent, believable fingerprint.

---

## Architecture

```
┌────────────────────────────┐
│  LLM agent (MCP client)    │
└──────────────┬─────────────┘
               │ JSON-RPC over HTTP (streamable) or stdio
┌──────────────▼─────────────┐
│  netwala.py  (FastMCP)     │  59 tools, prompts, resources, input guards
│  ─ tool wrappers           │  structured errors, no traceback leaks
│  ─ cross-cutting wrappers  │  human-behaviour layer, safety checks
│  ─ payload_library         │  payloads_data.py
└──────────────┬─────────────┘
               │
┌──────────────▼─────────────┐
│  browser_manager.py        │  persistent browser, context pool,
│  ─ lifecycle / Xvfb        │  page registry, locale/tz, fingerprint
│  ─ stealth prefs, GPU      │
└──────────────┬─────────────┘
               │ Playwright async API
┌──────────────▼─────────────┐
│  Firefox (AArch64/x86_64)  │  headed under Xvfb by default
└────────────────────────────┘
```

**Files**

| File | Purpose |
|---|---|
| `netwala.py` | The MCP server: all 59 tools, device presets, human-behaviour layer, input guards, `--list` mode |
| `browser_manager.py` | Persistent `BrowserManager`: launch/lifecycle, context pool, page registry, locale/tz maps, stealth prefs, Xvfb management, fingerprint spoofing |
| `payloads_data.py` | Payload library: 10 vulnerability classes, ~50 payloads each, WAF-specific obfuscation variants |
| `backup/` | Versioned snapshots of `netwala.py` and `browser_manager.py` |

---

## Features

**59 custom tools** (+ 4 MCP meta-tools). Grouped:

| Category | Count | Tools |
|---|---|---|
| Navigation | 5 | `navigate`, `evaluate_javascript`, `get_page_snapshot`, `list_browser_targets`, `close_tab` |
| Vision | 6 | `capture_screenshot`, `observe`, `get_visual_navigation_map`, `visual_click_by_id`, `take_element_screenshot`, `scroll_and_capture` |
| Interaction | 9 | `click_by_selector`, `click_by_text`, `click_by_role`, `type_text`, `press_keys`, `hover`, `select_option`, `set_file_input_files`, `mouse_click_human_like` |
| Recon | 8 | `detect_tech`, `check_security_headers`, `check_robots`, `check_sitemap`, `extract_links`, `crawl_site`, `extract_forms`, `extract_table` |
| WAF | 8 | `detect_waf`, `payload_library`, `smart_replay`, `http_request_cffi`, `intercept_request`, `stop_intercept`, `replay_request`, `get_dom_diff` |
| Network | 5 | `capture_har`, `capture_network_traffic`, `get_response_body`, `capture_websocket`, `wait_for_response` |
| State | 5 | `get_cookies`, `set_cookies`, `save_storage_state`, `load_storage_state`, `clear_site_data` |
| Waits | 4 | `wait_for_selector`, `wait_for_navigation`, `wait_for_network_idle`, `wait_for_element_count` |
| Spoofing | 4 | `spawn_subagent_environment`, `destroy_subagent_environment`, `emulate_device`, `batch_execute_across_agents` |
| Misc | 5 | `server_boot`, `scroll_page`, `handle_next_dialog`, `switch_to_frame`, `run_wcag_accessibility_audit` |

### Highlights

- **Soft-challenge follow-through.** Some WAFs (e.g. DataDome's interstitial)
  answer the first document with `403`, run a device check, then fetch the real
  document with `200`. `navigate` follows that through and reports the real
  status plus `first_http_status` and `soft_challenge_resolved`.
- **Human-behaviour layer** (on by default): curved, eased mouse travel with
  speed variation and micro-jitter; jittered typing; smooth scrolling; hover and
  navigation dwell. Disable with `NETWALA_HUMAN=0`.
- **Consistent fingerprint.** UA, `oscpu`, `platform`, `navigator.languages`,
  locale and timezone all agree; viewport matches the window; injected
  properties read as `[native code]` under `Function.prototype.toString`.
- **Device presets.** `emulate_device("iPhone 13")` / `spawn_subagent_environment(emulate_device=...)`
  apply UA + viewport + DPR + touch + oscpu + locale + tz, durably across
  navigation. (Firefox cannot set `maxTouchPoints` natively, so it is applied via
  an init script.)
- **Encrypted session state.** `save_storage_state(encrypt=True)` uses Fernet with
  `NETWALA_KEY`; `load_storage_state(decrypt=True)` restores it.
- **Payload library.** 10 classes — `sqli`, `xss`, `lfi`, `rce`, `cmdi`, `ssti`,
  `xxe`, `nosqli`, `ssrf`, `open_redirect` — with WAF-targeted extras for
  Cloudflare, DataDome, Akamai, Imperva, ModSecurity, Wordfence, F5 and Sucuri.
- **Structured errors.** Tool failures return `{"error": {tool, code, message,
  suggestion}}`; raw tracebacks never reach the agent.
- **Input safety.** Container-side paths are realpath-allowlisted; URLs are
  restricted to `http/https`; loopback / link-local / cloud-metadata hosts are
  blocked by default (see [Security model](#security-model)).

---

## Supported operating systems

| OS | Support | Notes |
|---|---|---|
| **Android (Termux)** | ✅ Primary | ARM64. Runs inside a Debian **PRoot** container; `Xvfb` provides a virtual display. Chromium is not supported on this path. |
| **Debian 12 / 13 (x86_64, aarch64)** | ✅ Fully | Native install, no container needed. |
| **Ubuntu 22.04 / 24.04** | ✅ Fully | Same as Debian. |
| **Other Linux (glibc)** | ✅ Likely | Any distro with Python 3.11+, Firefox and `Xvfb`. |
| **macOS** | ⚠️ Partial | Firefox + Playwright work; `Xvfb` is unavailable so headed mode is not needed (a real display exists). Untested. |
| **Windows** | ❌ Not supported | Playwright + PRoot assumptions, paths and `Xvfb` are all POSIX. |

Requirements: **Python ≥ 3.11**, **Firefox** (AArch64 build on ARM), **Xvfb**
(Linux headless hosts), `curl`, `git`.

---

## Dependencies

### Python packages

| Package | Why |
|---|---|
| `mcp` | FastMCP server (MCP protocol) |
| `playwright` | Browser automation |
| `curl_cffi` | TLS/JA3-fingerprint-spoofed HTTP (`http_request_cffi`) |
| `cryptography` | Fernet encryption for storage state |
| `requests` | Used by some helper paths |

### System packages

| Package | Why |
|---|---|
| `firefox` (or `firefox-esr`) | The browser |
| `xvfb` | Virtual display for headed mode |
| `libgl1-mesa-dri` / `mesa-utils` | Software GL (`llvmpipe`) so WebGL returns a real context |
| `curl`, `ca-certificates` | Health checks, bootstrapping |
| `python3`, `pip` | Runtime |

### Browser binaries

Playwright manages its own Firefox build (`playwright install firefox`). On
Termux/PRoot the distro `firefox-esr` package is used instead.

---

## Installation

### Option A — Android / Termux (primary deployment)

netwala runs in a Debian PRoot container because Termux lacks a working
Firefox/Playwright stack.

```bash
# 1. Install PRoot distro + Firefox inside Debian
pkg install -y proot-distro termux-api
proot-distro install debian
proot-distro login debian -- bash -lc '
  apt update
  apt install -y python3 python3-pip python3-venv firefox-esr xvfb \
                 libgl1-mesa-dri mesa-utils curl ca-certificates git
'

# 2. Copy the netwala sources into the container
#    (host path: /data/data/com.termux/files/home/netwala-src -> /root/netwala)
proot-distro login debian -- bash -lc 'mkdir -p /root/netwala'
proot-distro login debian -- bash -lc '
  cp -r /data/data/com.termux/files/home/netwala-src/* /root/netwala/
'

# 3. Python dependencies
proot-distro login debian -- bash -lc '
  cd /root/netwala
  pip install --break-system-packages mcp playwright curl_cffi cryptography requests
  python3 -m playwright install firefox
'

# 4. Launch helper
#    start_netwala.sh handles Xvfb, the Fernet key, env passthrough and the
#    health check. Keep it next to the sources.
proot-distro login debian -- bash -lc 'cd /root/netwala && ./start_netwala.sh'
```

### Option B — Debian / Ubuntu (native)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv firefox-esr xvfb \
                    libgl1-mesa-dri mesa-utils curl ca-certificates git

git clone <repo-url> netwala && cd netwala
pip install --break-system-packages mcp playwright curl_cffi cryptography requests
python3 -m playwright install firefox

# Fernet key (used by save/load_storage_state with encrypt=True)
mkdir -p ~/.netwala
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > ~/.netwala/key
chmod 600 ~/.netwala/key
export NETWALA_KEY="$(cat ~/.netwala/key)"

./start_netwala.sh
```

### Option C — verify the install

```bash
curl -s http://127.0.0.1:8765/health
# {"status":"ok","server":"netwala-playwright","browser_booted":false,"endpoint":"http://127.0.0.1:8765/mcp"}

python3 netwala.py --list          # prints all 59 tools grouped by category
python3 netwala.py --transport stdio   # speak JSON-RPC over stdin/stdout
```

---

## Quick start

```bash
./start_netwala.sh
# ✅ netwala started (attempt 1, NETWALA_KEY from ~/.netwala/key)
```

Then ask your MCP client: *"How many netwala tools do you have?"* → **59**.

Minimal manual launch:

```bash
export NETWALA_KEY="$(cat ~/.netwala/key)"
export NETWALA_HEADED=1
export DISPLAY=:99
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
python3 netwala.py --transport streamable-http --host 127.0.0.1 --port 8765
```

---

## Configuration

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `NETWALA_KEY` | *(required for encrypt)* | Fernet key for `save_storage_state(encrypt=True)` |
| `NETWALA_HOST` | `127.0.0.1` | Bind address |
| `NETWALA_PORT` | `8765` | Bind port |
| `NETWALA_VERBOSE` | `0` | Verbose logging |
| `NETWALA_HEADED` | `1` | `1` = headed under Xvfb (WebGL works); `0` = headless |
| `NETWALA_DISPLAY` | `:99` | X display |
| `NETWALA_HUMAN` | `1` | Human-behaviour layer on/off |
| `NETWALA_LOCALE` | `en-US` | Locale applied to contexts |
| `NETWALA_TIMEZONE` | derived from locale | IANA timezone id |
| `NETWALA_FF_RFP` | `false` | Resist-Fingerprinting mode (stronger, more obviously spoofed) |
| `NETWALA_GPU_SPOOF` | `0` | Override WebGL vendor/renderer strings |
| `NETWALA_GPU_VENDOR` / `NETWALA_GPU_RENDERER` | — | Values for GPU spoofing |
| `NETWALA_ALLOW_LOOPBACK` | `1` (start script) | Allow loopback / link-local / metadata hosts |
| `NETWALA_ALLOW_FILE` | `0` | Allow `file://` URLs |
| `NETWALA_IO_DIRS` | — | Extra allowed I/O directories, colon-separated |

### Device presets

`iphone`, `iphone 13`, `ipad`, `pixel` — each sets UA, viewport, DPR, touch
points, `oscpu`, `platform`, locale and timezone. Unknown names are rejected with
the list of valid presets.

---

## Connecting an MCP client

**Streamable HTTP (default)**

```json
{
  "mcpServers": {
    "netwala": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

**stdio**

```json
{
  "mcpServers": {
    "netwala": {
      "command": "python3",
      "args": ["/root/netwala/netwala.py", "--transport", "stdio"]
    }
  }
}
```

**Raw JSON-RPC** (SSE-style response framing)

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2024-11-05","capabilities":{},
                 "clientInfo":{"name":"cli","version":"1"}}}'
# -> note the Mcp-Session-Id response header, then:
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Mcp-Session-Id: <id>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"navigate","arguments":{"url":"https://example.com"}}}'
```

---

## Security model

The agent supplies paths and URLs, and a prompt-injected page can steer the
agent — so nothing is trusted.

- **Path allowlist.** Container-side paths must resolve (realpath) inside
  `/tmp`, `/root/netwala`, or `/data/data/com.termux/files/home`. `..` traversal
  and anything under `/etc`, `/root`, etc. is refused. Extra dirs via
  `NETWALA_IO_DIRS`. Applies to `save_storage_state`, `load_storage_state`,
  `capture_screenshot`, `take_element_screenshot`, `set_file_input_files`,
  `capture_har` and `spawn_subagent_environment(storage_state_path=...)`.
- **URL scheme allowlist.** Only `http`/`https` (plus `about:blank` internally).
  `file://` is blocked unless `NETWALA_ALLOW_FILE=1`.
- **Host guard.** Loopback, link-local, unspecified and multicast addresses, and
  cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`) are
  blocked by default. `NETWALA_ALLOW_LOOPBACK=1` re-enables them for authorized
  internal / SSRF testing.
- **Arbitrary JavaScript.** `evaluate_javascript(js_code=...)` is arbitrary JS by
  design — that is the tool's purpose.
- **Cleanup.** Stale Playwright profile directories are swept at startup, and the
  browser is closed on `SIGINT`/`SIGTERM`/exit. A `SIGKILL` cannot be caught, so
  the startup sweep is the recovery path.
- **Error hygiene.** Structured error objects only; no tracebacks leak.

---

## Anti-bot design

Default-on measures, and the reason each one exists:

- **Headed mode (Xvfb).** Headless Firefox returns *no WebGL context at all* —
  one of the strongest bot tells there is. Headed + `LIBGL_ALWAYS_SOFTWARE=1`
  gives a real `llvmpipe` context.
- **Fingerprint consistency.** `screen` matches `innerWidth/innerHeight`;
  `navigator.languages` matches the locale; locale and timezone agree
  (`LOCALE_TZ`); `oscpu`/`platform` match the UA; injected properties report
  `[native code]`.
- **Human telemetry.** Curved Bézier mouse paths with smoothstep easing, speed
  variation and micro-jitter; jittered key delays; smooth scrolling; hover and
  navigation dwell. Turn off with `NETWALA_HUMAN=0`.
- **Stealth prefs.** Anti-tracking left at a believable setting rather than the
  default that advertises blocking.
- **Honest GPU.** GPU string spoofing is **off** by default: a half-spoof
  (NVIDIA renderer with `llvmpipe` parameters) is worse than the truth.

### Known limits (be honest about these)

- IP reputation is not solved by any of this. DataDome/Cloudflare will still
  challenge a datacenter or flagged IP; residential or mobile proxies are needed
  for volume.
- **Tor makes it worse.** Tor exits are flagged, so challenges and hard blocks
  increase.
- `navigator.plugins` / `navigator.mimeTypes` are empty in Firefox — expected,
  and matched to the UA.
- Low-to-medium volume only. This is a browser automation layer, not a scraping
  farm.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `{"status":"ok"}` but `browser_booted:false` | Normal until the first tool call. |
| Browser boots **headless** despite `NETWALA_HEADED=1` | Xvfb was not up at launch. Restart the server; the launcher starts Xvfb and waits. |
| `WebGL: NO_CONTEXT` | Headless mode. Set `NETWALA_HEADED=1` and ensure `Xvfb` runs on `NETWALA_DISPLAY`. |
| `NETWALA_KEY env var required` | Generate a Fernet key (see Installation) and export it. |
| `Unsupported engine: 'chromium'` | Expected — Chromium was removed. Use Firefox. |
| `payloads_data.py module not found` | The payload library is missing from the server directory. Restore `payloads_data.py`. |
| Path/URL refused | See [Security model](#security-model); use `NETWALA_IO_DIRS` / `NETWALA_ALLOW_LOOPBACK`. |
| Stale `/tmp/playwright_firefoxdev_profile-*` | Cleared automatically at startup. |
| `start_netwala.sh` exits with -15 | The launcher's `pkill -f netwala.py` matched the invoking shell. Run the launcher on its own line. |

---

## Changelog

**Current (2026-11-19)**

- Chromium removed — Firefox only; `engine='chromium'` returns a clear error.
- Input safety: path allowlist, `http/https` scheme guard, loopback/metadata
  guard, `NETWALA_IO_DIRS`, `NETWALA_ALLOW_FILE`, `NETWALA_ALLOW_LOOPBACK`.
- `navigate` soft-challenge follow-through (`first_http_status`,
  `soft_challenge_resolved`, `settled_title`, `settled_html_bytes`).
- Human-behaviour layer (curved eased mouse travel, jittered typing, smooth
  scroll, dwell) — default on.
- Fingerprint consistency: locale↔timezone map, `oscpu`/`platform`, languages,
  `[native code]` getters, viewport matched to the window.
- Headed-by-default (Xvfb) with a robustness check so a dead display can never
  silently produce a headless browser.
- Profile-leak hygiene: startup sweep + atexit close.
- `press_keys.keys` schema fixed to an explicit `anyOf`.
- `payload_library` rebuilt: 10 classes, ~50 payloads each, WAF variants.
- 59-tool sweep: 0 errors beyond the documented expected paths.

---

## Roadmap / upcoming features

Planned, in rough priority order:

1. **Proxy-aware session profiles** — attach a proxy per context and persist it
   in the encrypted storage state, so residential/mobile proxy use is one call.
2. **Integrated challenge queue (human-in-the-loop)** — when a Turnstile /
   DataDome widget is detected, pause, surface the widget, let the operator solve
   it, and resume. No automated solving.
3. **Stealth-score report** — a single call that reports the live fingerprint
   (WebGL renderer, screen/viewport, languages, tz, plugins, DPR, touch) and
   flags inconsistencies, before you touch a target.
4. **Crawl with scope pinning** — enforce an allowlist of in-scope origins during
   `crawl_site` / `extract_links` so a stray redirect can never take the agent
   off-scope.
5. **Cookie/localStorage diffing** — before/after state comparison for auth and
   IDOR tests.
6. **Request assertions in `replay_request`** — declarative checks (status,
   header, body regex) so a test can pass/fail without an LLM round-trip.
7. **Batch replay with rate control** — queued requests with jitter, for
   low-and-slow IDOR sweeps.
8. **HAR → findings stub** — turn a captured HAR into a draft report entry.
9. **Optional Chromium backend on x86_64** — where it is actually stable.
10. **Prompts/resources expansion** — per-vuln-type workflow prompts (SQ
