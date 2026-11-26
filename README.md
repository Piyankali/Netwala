# 🕵️ NETWALA MCP — Autonomous AI Pentesting Framework

<p align="center">
<b>AI-native browser automation and web application security testing over MCP</b>
</p>

<p align="center">
<a href="https://github.com/Piyankali/Netwala"><img src="https://img.shields.io/badge/version-3.0-blue.svg" alt="Version 3.0"></a>
<img src="https://img.shields.io/badge/engine-Playwright%20Firefox-green.svg" alt="Playwright Firefox (Chromium removed)">
<img src="https://img.shields.io/badge/tools-59%20custom-orange.svg" alt="59 custom MCP tools">
<img src="https://img.shields.io/badge/meta-4-lightgrey.svg" alt="4 MCP meta-tools">
<img src="https://img.shields.io/badge/license-MIT-red.svg" alt="MIT License">
<img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
</p>

<img width="1672" height="941" alt="netwala" src="https://github.com/user-attachments/assets/e1a580a9-f5de-4274-bf88-f2a3dc2914f9" />

**Playwright-powered MCP server for browser automation, recon, and authorized
web-application security testing.**

Netwala MCP is a Model Context Protocol (MCP) server that gives an LLM agent a
real browser. It exposes **59 custom tools** plus the 4 standard MCP meta-tools
(**63 total**) to compatible AI clients such as Hermes and Claude.

It combines Playwright-powered **Firefox** automation with page inspection,
crawling, request analysis, network capture, isolated browser contexts, WAF
detection, a built-in payload library, and evidence collection — all while
keeping the fingerprint self-consistent and the input surface validated.

```
subfinder / nmap / katana   ->   netwala (live verify, WAF, evidence)
                               ->   nuclei / ffuf (known vulns, dirs)
                               ->   netwala (request-level IDOR, API testing)
                               ->   findings report
```

> ⚠️ **Authorized use only.** Netwala is built for bug-bounty engagements and
> pentests where you have written permission to test the target. It is **not** a
> CAPTCHA-bypass service and **not** a scraping farm. It does not guarantee that
> a target's bot protection, WAF, fingerprinting or security controls can be
> bypassed.

---

## 📑 Contents

- [Highlights](#-highlights)
- [Tool inventory](#-tool-inventory--63-tools)
- [Architecture](#-architecture)
- [Supported platforms](#-supported-platforms)
- [Dependencies](#-dependencies)
- [Installation](#-installation)
- [Run the MCP server](#%EF%B8%8F-run-the-mcp-server)
- [Connect an MCP client](#connect-an-mcp-client)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Known security boundaries & caveats](#known-security-boundaries--caveats)
- [Anti-bot design](#anti-bot-design)
- [Security considerations](#-security-considerations)
- [Testing and validation](#-testing-and-validation)
- [Troubleshooting](#troubleshooting)
- [Limitations](#%EF%B8%8F-limitations)
- [Changelog](#changelog)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)
- [FAQ](#faq)
- [Support](#-support)
- [License](#license)
- [Legal notice](#legal-notice)

---

## 🚀 Highlights

- **MCP interface** — browser and security-testing capability exposed to any
  MCP-compatible agent.
- **59 custom tools** — navigation, interaction, visual inspection,
  reconnaissance, WAF/testing, network analysis, state, and isolated contexts.
- **Firefox, headed by default** — headless Firefox returns *no WebGL context at
  all*, one of the strongest bot signals there is. Netwala runs headed under
  `Xvfb` with software GL so WebGL returns a real `llvmpipe` context.
- **Human-behaviour layer** (default on) — curved, eased mouse travel with speed
  variation and micro-jitter, jittered typing, smooth scrolling, hover and
  navigation dwell.
- **Consistent fingerprint** — UA, `oscpu`, `platform`, `navigator.languages`,
  locale and timezone all agree; viewport matches the window; injected
  properties read as `[native code]`.
- **Soft-challenge follow-through** — some WAFs (e.g. DataDome's interstitial)
  answer the first document with `403`, run a device check, then fetch the real
  document with `200`. `navigate` follows that through and reports the real
  status plus `first_http_status` and `soft_challenge_resolved`.
- **Payload library** — 10 classes with WAF-targeted obfuscation variants.
- **Encrypted session state** — Fernet-encrypted storage state via `NETWALA_KEY`.
- **Input safety** — agent-supplied paths and URLs are validated before use.
- **Structured errors** — no raw tracebacks ever reach the agent.
- **Mobile-oriented workflow** — documented Termux + Debian PRoot setup path.

---

## 🛠️ Tool inventory — 63 tools

59 custom tools plus 4 standard MCP meta-tools.

### 1. Navigation — 5 tools

| # | Tool | Description |
|---|---|---|
| 1 | `navigate` | Navigate to a URL; reports the real status, follows soft-challenge reloads, optional redirect chain and screenshot |
| 2 | `evaluate_javascript` | Execute JavaScript in the page context (optionally inside a frame) |
| 3 | `get_page_snapshot` | Extract title, links, forms, HTML comments and visible text |
| 4 | `list_browser_targets` | List open tabs and contexts |
| 5 | `close_tab` | Close a selected tab |

### 2. Vision — 6 tools

| # | Tool | Description |
|---|---|---|
| 6 | `capture_screenshot` | Full-page, viewport or element screenshot (PNG/JPEG, PDF fallback) |
| 7 | `observe` | Screenshot + accessibility information + console errors in one call |
| 8 | `get_visual_navigation_map` | Set-of-Marks-style map with element IDs, selectors and XPath |
| 9 | `visual_click_by_id` | Click a visual-map element by ID |
| 10 | `take_element_screenshot` | Screenshot of a specific element |
| 11 | `scroll_and_capture` | Scroll a long/lazy page and capture screenshots |

### 3. Interaction — 9 tools

| # | Tool | Description |
|---|---|---|
| 12 | `click_by_selector` | Click by CSS selector (human-like travel) |
| 13 | `click_by_text` | Click by visible text |
| 14 | `click_by_role` | Click by accessible role |
| 15 | `type_text` | Type text with jittered, human-like key delays |
| 16 | `press_keys` | Press keys or key sequences (string or list) |
| 17 | `hover` | Hover to reveal menus, tooltips or hover states |
| 18 | `select_option` | Select a dropdown option by value, label or index |
| 19 | `set_file_input_files` | Set files on a file input for authorized upload testing |
| 20 | `mouse_click_human_like` | Bézier curved mouse movement + click |

### 4. Recon — 8 tools

| # | Tool | Description |
|---|---|---|
| 21 | `detect_tech` | Identify servers, frameworks, JS libraries and WAF products |
| 22 | `check_security_headers` | Review CSP, HSTS, X-Frame-Options and friends |
| 23 | `check_robots` | Fetch and parse robots.txt without navigating |
| 24 | `check_sitemap` | Fetch and parse sitemap.xml without navigating |
| 25 | `extract_links` | Recursive link extraction with depth, deadline and domain pinning |
| 26 | `crawl_site` | Crawl for robots, sitemap, links and forms |
| 27 | `extract_forms` | Extract forms, inputs and CSRF-token information |
| 28 | `extract_table` | Convert HTML tables to structured JSON |

### 5. WAF & testing — 8 tools

| # | Tool | Description |
|---|---|---|
| 29 | `detect_waf` | Fingerprint the WAF and test benign vs malicious traffic |
| 30 | `payload_library` | Ready-to-use payloads for 10 vuln classes, WAF-targeted variants |
| 31 | `smart_replay` | Replay a request in-page (auto-attaches cookies/CSRF) |
| 32 | `http_request_cffi` | TLS/JA3-spoofed HTTP via curl-cffi (Firefox/Chrome/Safari/Edge impersonation) |
| 33 | `intercept_request` | Inspect and modify, drop or delay requests and responses |
| 34 | `stop_intercept` | Stop all active interceptions |
| 35 | `replay_request` | Replay with modified headers or body |
| 36 | `get_dom_diff` | Compare DOM state before/after a JS action |

### 6. Network — 5 tools

| # | Tool | Description |
|---|---|---|
| 37 | `capture_har` | Record HTTP Archive data, optionally extract JS-file references |
| 38 | `capture_network_traffic` | Capture network activity, optionally flag exposed secrets |
| 39 | `get_response_body` | Wait for a matching API response and return its body |
| 40 | `capture_websocket` | Capture WebSocket frames |
| 41 | `wait_for_response` | Wait for a response matching given criteria |

### 7. State — 5 tools

| # | Tool | Description |
|---|---|---|
| 42 | `get_cookies` | Retrieve cookies with filters |
| 43 | `set_cookies` | Set cookies in the context |
| 44 | `save_storage_state` | Save cookies + localStorage, optionally Fernet-encrypted |
| 45 | `load_storage_state` | Spawn a context pre-loaded with saved state |
| 46 | `clear_site_data` | Clear cookies, cache, localStorage and session storage |

### 8. Waits — 4 tools

| # | Tool | Description |
|---|---|---|
| 47 | `wait_for_selector` | Wait for a selector, including any-of and not-present conditions |
| 48 | `wait_for_navigation` | Wait for a navigation event (arm before triggering) |
| 49 | `wait_for_network_idle` | Wait for configured network-idle conditions |
| 50 | `wait_for_element_count` | Wait until a selector matches a requested count |

### 9. Spoofing & orchestration — 4 tools

| # | Tool | Description |
|---|---|---|
| 51 | `spawn_subagent_environment` | Isolated context with proxy, UA, viewport, locale, tz, device profile |
| 52 | `destroy_subagent_environment` | Tear down an isolated context |
| 53 | `emulate_device` | Apply a device profile (iPhone, iPhone 13, iPad, Pixel, Samsung, Desktop) |
| 54 | `batch_execute_across_agents` | Run JS tasks across multiple contexts with concurrency control |

### 10. Misc — 5 tools

| # | Tool | Description |
|---|---|---|
| 55 | `server_boot` | Initialize and warm up the browser engine |
| 56 | `scroll_page` | Scroll to top, bottom or a selected element |
| 57 | `handle_next_dialog` | Accept or dismiss the next dialog and capture its text |
| 58 | `switch_to_frame` | Select an iframe for inspection or execution |
| 59 | `run_wcag_accessibility_audit` | axe-core audit with a built-in fallback on CSP-blocked hosts |

### Standard MCP meta-tools — 4 tools

Supplied by the MCP framework, not implemented as custom Netwala tools.

| # | Tool | Description |
|---|---|---|
| 60 | `list_prompts` | List available server prompts |
| 61 | `get_prompt` | Retrieve a named prompt |
| 62 | `list_resources` | List available MCP resources |
| 63 | `read_resource` | Read an MCP resource |

**Count check:** 5 + 6 + 9 + 8 + 8 + 5 + 5 + 4 + 4 + 5 = **59 custom**;
59 + 4 = **63 total**.

> Tool availability and argument schemas are defined by the running server.
> Use MCP tool discovery against your installed version as the source of truth.

---

## 🏗️ Architecture

```
+----------------------------- ANDROID DEVICE ------------------------------+
|                                                                            |
|  +---------------------------+       +---------------------------------+   |
|  | TERMUX HOST               |       | DEBIAN PROOT                    |   |
|  |                           |       |                                 |   |
|  | +-----------------------+ | HTTP  | +-----------------------------+ |   |
|  | | Hermes / MCP Client   | |------>| | netwala.py / FastMCP        | |   |
|  | +-----------------------+ |       | | 59 custom tools             | |   |
|  |                           |       | +--------------+--------------+ |   |
|  |                           |       |                |                |   |
|  |                           |       | +--------------v--------------+ |   |
|  |                           |       | | browser_manager.py          | |   |
|  |                           |       | | Playwright async API        | |   |
|  |                           |       | +--------------+--------------+ |   |
|  |                           |       |                |                |   |
|  |                           |       |       +--------v--------+       |   |
|  |                           |       |       |  Xvfb :99        |       |   |
|  |                           |       |  +----v------+          |       |   |
|  |                           |       |  | Firefox   |          |       |   |
|  |                           |       |  +-----------+          |       |   |
|  +---------------------------+       +---------------------------------+   |
|                                                                            |
+----------------------------------------------------------------------------+
```

### Main components

1. **Transport layer** — MCP over Streamable HTTP. Default bind `127.0.0.1:8765`,
   endpoint `/mcp`. A `stdio` transport is also supported.
2. **`netwala.py`** — the FastMCP server: registers the 59 tools, prompts and
   resources; validates inputs through declared schemas plus the input-safety
   guards; dispatches to the browser manager asynchronously.
3. **`browser_manager.py`** — wraps the Playwright async API: browser lifecycle,
   context pool, page registry, locale↔timezone maps, stealth prefs, Xvfb
   management, fingerprint spoofing.
4. **`payloads_data.py`** — the payload library (pure data, no dependencies).
5. **Browser contexts** — isolate cookies, localStorage and session data between
   tasks. Contexts isolate *browser state*, not the host OS.

### Request lifecycle

1. The AI client interprets the user's request.
2. The client sends an MCP tool call to the configured endpoint.
3. The server validates the tool name and arguments.
4. The tool invokes the browser manager (or another subsystem).
5. Playwright performs the operation.
6. Netwala returns structured output.
7. The agent evaluates the result and decides the next authorized step.

### Files

| File | Purpose |
|---|---|
| `netwala.py` | MCP server: all 59 tools, device presets, human-behaviour layer, input guards, `--list` mode |
| `browser_manager.py` | Persistent `BrowserManager`: lifecycle, context pool, page registry, locale/tz, stealth prefs, Xvfb, fingerprint |
| `payloads_data.py` | Payload library: 10 classes, ~50 payloads each, WAF variants |
| `start_netwala.sh` | Launcher: Xvfb, Fernet key, env passthrough, health check |
| `backup/` | Versioned snapshots (never delete these) |

---

## 💻 Supported platforms

| Platform | Status | Notes |
|---|---|---|
| **Android / Termux + Debian PRoot** | ✅ Primary target | ARM64. Browser runs in PRoot; `Xvfb` provides the display. Chromium is **not** supported on this path. |
| **Debian 12 / 13** | ✅ Supported | x86_64 and aarch64. |
| **Ubuntu 22.04 / 24.04** | ✅ Supported | Same path as Debian. |
| **Kali Linux** | ✅ Supported | Suitable for authorized security research. |
| **Other Linux (glibc)** | ✅ Likely | Any distro with Python 3.11+, Firefox and Xvfb. |
| **macOS** | ⚠️ Partial | Firefox + Playwright work; no Xvfb needed (real display). Untested. |
| **Windows + WSL2** | ⚠️ Partial | Use a supported Linux distro inside WSL2. Native Windows is not supported. |
| **Docker / headless** | 🗺️ Planned | Container deployment is on the roadmap. |

Requirements: **Python ≥ 3.11**, **Firefox**, **Xvfb** on headless Linux hosts,
`curl`, `git`.

> Platform status reflects project documentation. Verify on your specific OS,
> CPU architecture and browser combination before relying on it.

---

## 📦 Dependencies

### Python packages

| Package | Why |
|---|---|
| `mcp` | FastMCP server (MCP protocol) |
| `playwright` | Browser automation |
| `curl_cffi` | TLS/JA3-spoofed HTTP (`http_request_cffi`) |
| `cryptography` | Fernet encryption for storage state |
| `requests` | Helper paths |

### System packages

| Package | Why |
|---|---|
| `firefox` / `firefox-esr` | The browser |
| `xvfb` | Virtual display for headed mode |
| `libgl1-mesa-dri`, `mesa-utils` | Software GL (`llvmpipe`) so WebGL returns a real context |
| `curl`, `ca-certificates` | Health checks, bootstrapping |
| `python3`, `pip` | Runtime |

### Browser binaries

Playwright manages its own Firefox build (`playwright install firefox`). On
Termux/PRoot the distro `firefox-esr` package is used instead. On supported
Debian-like systems you can install browser system dependencies with:

```bash
python3 -m mcp==1.30.0 playwright install-deps firefox
```

> **Android/PRoot caveat:** `install-deps` and prebuilt Playwright browser
> binaries may not work in every Android PRoot environment. Follow the
> platform-specific procedure below if the standard installation fails.

---

## ⚙️ Installation

### Option A — Android / Termux with Debian PRoot (primary)

On the Termux host:

```bash
pkg update && pkg upgrade -y
pkg install -y python proot-distro termux-api git
termux-wake-lock
proot-distro install debian
proot-distro login debian
```

Inside Debian PRoot:

```bash
apt update
apt install -y python3 python3-pip python3-venv firefox-esr xvfb \
               libgl1-mesa-dri mesa-utils curl ca-certificates git

# Clone the repository
git clone https://github.com/Piyankali/Netwala.git
cd Netwala

# Python dependencies
python3 -m pip install --break-system-packages \
    mcp==1.30.0 playwright curl_cffi cryptography requests

# Browser binaries (may not work in every PRoot environment — see caveat)
python3 -m playwright install firefox
```

### Option B — Linux / macOS / WSL2

```bash
git clone https://github.com/Piyankali/Netwala.git
cd Netwala

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install mcp==1.30.0 playwright curl_cffi cryptography requests
python3 -m playwright install firefox

# On Debian/Ubuntu, if required:
python -m playwright install-deps firefox
```

### Option C — Fernet key (for encrypted storage state)

```bash
mkdir -p ~/.netwala
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > ~/.netwala/key
chmod 600 ~/.netwala/key
export NETWALA_KEY="$(cat ~/.netwala/key)"
```

`start_netwala.sh` generates this key automatically on first run.

### Option D — Verify the install

```bash
curl -s http://127.0.0.1:8765/health
# {"status":"ok","server":"netwala-playwright","browser_booted":false,
#  "endpoint":"http://127.0.0.1:8765/mcp"}

python3 netwala.py --list              # all 59 tools grouped by category
python3 netwala.py --transport stdio   # JSON-RPC over stdin/stdout
python3 netwala.py --help              # exact options for this version
```

---

## ▶️ Run the MCP server

```bash
python3 netwala.py \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8765
```

Expected endpoint: **http://127.0.0.1:8765/mcp**

Or use the launcher (Termux/PRoot), which handles Xvfb, the Fernet key, env
passthrough and the health check:

```bash
./start_netwala.sh
# ✅ netwala started (attempt 1, NETWALA_KEY from ~/.netwala/key)
```

Minimal manual launch:

```bash
export NETWALA_KEY="$(cat ~/.netwala/key)"
export NETWALA_HEADED=1
export DISPLAY=:99
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
python3 netwala.py --transport streamable-http --host 127.0.0.1 --port 8765
```

---

## Connect an MCP client

### Hermes

```yaml
mcp_servers:
  netwala:
    url: "http://127.0.0.1:8765/mcp"
    enabled: true
    timeout: 300
    skip_preflight: true
```

### Generic MCP client (Streamable HTTP)

```json
{
  "mcpServers": {
    "netwala": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

### stdio

```json
{
  "mcpServers": {
    "netwala": {
      "command": "python3",
      "args": ["/path/to/netwala.py", "--transport", "stdio"]
    }
  }
}
```

### Raw JSON-RPC

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2024-11-05","capabilities":{},
                 "clientInfo":{"name":"cli","version":"1"}}}'
# note the Mcp-Session-Id response header, then:
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Mcp-Session-Id: <id>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"navigate","arguments":{"url":"https://example.com"}}}'
```

### Verify the connection

After starting the server:

- [ ] The process stays running.
- [ ] The MCP endpoint is reachable from the client environment.
- [ ] Tool discovery returns **59 custom + 4 meta** tools.
- [ ] A harmless smoke test passes (`navigate` to a local page or an explicitly
      authorized target).
- [ ] Screenshots, logs and captured artifacts land in the expected location.

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

`iphone`, `iphone 13`, `ipad`, `pixel`, `samsung`, `desktop` — each sets UA, viewport, DPR, touch
points, `oscpu`, `platform`, locale and timezone, applied durably across
navigation. Unknown names are rejected with the list of valid presets.

### Isolation and proxy configuration

- Use separate contexts for separate test identities.
- Do not share authenticated storage between unrelated targets.
- Treat saved storage state, cookies and HAR files as sensitive.
- Configure proxies only for authorized testing, and know the effective route.
- A browser context is **not** an OS-level sandbox.

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
  blocked unless `NETWALA_ALLOW_LOOPBACK=1` (the launcher default, for
  authorized internal / SSRF testing).
- **Arbitrary JavaScript.** `evaluate_javascript(js_code=...)` is arbitrary JS by
  design — that is the tool's purpose.
- **TLS verification.** `http_request_cffi` **defaults to `verify_ssl=False`** so
  self-signed pentest targets work. That means certificate validation is NOT
  checked unless you pass `verify_ssl=True` — a missing-validation finding will
  be invisible under the default. See
  [Known security boundaries & caveats](#known-security-boundaries--caveats).
- **Cleanup.** Stale Playwright profile directories are swept at startup, and the
  browser is closed on `SIGINT`/`SIGTERM`/exit. A `SIGKILL` cannot be caught, so
  the startup sweep is the recovery path.
- **Error hygiene.** Structured error objects only; no tracebacks leak.

> **What "verified" means here.** Behavioural claims (tool counts, statuses,
> WAF results, cleanup) come from **runtime** runs against a live server, and the
> numbers quoted were observed on that run. Statements about what the code does
> (guards, defaults, parameter plumbing) come from **static** reading of the
> source — `ast.parse` / argument-flow checks — and are labelled as such. The two
> are not interchangeable: a static claim is not evidence that the running server
> behaves that way, and a runtime observation can be environment-specific.

---

## Known security boundaries & caveats

Read this before trusting the guards.

- **`NETWALA_ALLOW_LOOPBACK=1` disables the SSRF/loopback guard for the whole
  session.** `start_netwala.sh` sets it by default so authorized internal and
  SSRF testing works. While it is on, `navigate`, `replay_request`,
  `smart_replay`, `http_request_cffi`, `capture_har` and `load_storage_state`
  will reach `127.0.0.1`, `::1`, RFC1918 ranges, link-local and cloud-metadata
  addresses. Set `NETWALA_ALLOW_LOOPBACK=0` if you do not need internal targets.
- **Tools that write files.** `run_wcag_accessibility_audit(save_to_file=...)`,
  `capture_screenshot` / `take_element_screenshot(save_path=...)`,
  `capture_har(har_path=...)` and `save_storage_state(path=...)` all write to
  disk. Every one of them is now constrained by `safe_path()` to `/tmp`,
  `/root/netwala` and `/data/data/com.termux/files/home` (plus
  `NETWALA_IO_DIRS`). `set_file_input_files` writes temporary upload files to
  `/tmp` under a `basename()`-sanitised name and removes them afterwards.
- **`evaluate_javascript` is arbitrary code.** It runs in the page's origin with
  the page's cookies. That is the intended capability, not a bug — but a
  prompt-injected page can steer the agent into running it.
- **`http_request_cffi` skips certificate verification by default**
  (`verify_ssl=False`) so self-signed pentest targets work. Set
  `verify_ssl=True` when you actually want to test certificate validation — the
  default will happily hide a missing-validation bug.
- **`intercept_request` rewrites traffic in the browser context.** It is a test
  instrument, not a secure proxy.
- **No automated CAPTCHA solving.** There is no solver, and none is planned.
- **Browser contexts are not OS sandboxes.** They isolate browser state only.

---

## Anti-bot design

Default-on measures, and the reason each one exists:

- **Headed mode (Xvfb).** Headless Firefox returns *no WebGL context at all* —
  one of the strongest bot signals there is. Headed + `LIBGL_ALWAYS_SOFTWARE=1`
  gives a real `llvmpipe` context.
- **Fingerprint consistency.** `screen` matches `innerWidth/innerHeight`;
  `navigator.languages` matches the locale; locale and timezone agree
  (`LOCALE_TZ`); `oscpu`/`platform` match the UA; injected properties report
  `[native code]`.
- **Human telemetry.** Curved Bézier mouse paths with smoothstep easing, speed
  variation and micro-jitter; jittered key delays; smooth scrolling; hover and
  navigation dwell. Disable with `NETWALA_HUMAN=0`.
- **Stealth prefs.** Anti-tracking left at a believable setting rather than the
  default that advertises blocking.
- **Honest GPU.** GPU string spoofing is **off** by default: a half-spoof
  (NVIDIA renderer with `llvmpipe` parameters) is worse than the truth.

### Be honest about what this does not solve

Using Firefox alone does **not** guarantee a normal-user TLS fingerprint, and it
does not prevent bot detection. TLS, browser, device and behavioral fingerprints
depend on the actual runtime and network stack. Framework-compatible input
events and curved mouse movement do **not** guarantee that an application's
automation defenses will be bypassed.

- **IP reputation** is not solved by any of this. DataDome/Cloudflare will still
  challenge a datacenter or flagged IP; residential or mobile proxies are needed
  for volume.
- **Tor makes it worse** — Tor exits are flagged, so challenges and hard blocks
  increase. Measured, not assumed.
- `navigator.plugins` / `navigator.mimeTypes` are empty in Firefox — expected.
- Low-to-medium volume only.

---

## 🔐 Security considerations

Netwala can interact with authenticated applications and capture sensitive
browser and network data. Deploy it accordingly.

- **Authorization** — test only systems you own or have explicit permission to
  assess.
- **Network exposure** — keep the server bound to loopback unless remote access
  is deliberately configured.
- **Authentication** — never expose an unauthenticated MCP endpoint to an
  untrusted network.
- **Secrets** — treat cookies, localStorage, authorization headers, JWTs, API
  keys, HAR files and screenshots as confidential.
- **Storage** — protect saved browser state and delete it when no longer needed.
- **Isolation** — use separate contexts and test identities; contexts are not OS
  sandboxes.
- **Destructive actions** — avoid destructive tests on production systems unless
  explicitly authorized and coordinated.
- **Evidence** — redact credentials and personal data before sharing reports or
  committing artifacts.

---

## 🧪 Testing and validation

The project has been exercised against intentionally vulnerable training
environments, including:

- **TryHackMe — Pickle Rick**
- **TryHackMe — OWASP Juice Shop**
- **TryHackMe — Mutillidae II**

Reported activities include reconnaissance, form extraction, request inspection,
authentication-flow analysis, file-upload validation, browser automation, and
debugging of Netwala's own tools.

These are project-reported results, not an independent security audit or proof
that every tool works on every supported platform.

### Recommended release verification

Before publishing a release, verify:

- [ ] Server starts with the documented command.
- [ ] MCP client connects over Streamable HTTP.
- [ ] Discovery returns the expected custom and meta-tool counts.
- [ ] Tool schemas match the implementation.
- [ ] Firefox smoke test passes on each claimed supported platform.
- [ ] Context creation and destruction work.
- [ ] Navigation, interaction and wait tools work against a local test app.
- [ ] Network capture and response retrieval work.
- [ ] Screenshots and HAR artifacts are generated correctly.
- [ ] Sensitive artifacts are not accidentally exposed.
- [ ] Regression tests pass after fixes.

Keep test output, environment details, browser versions and reproducible steps
in the repository's test documentation.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `{"status":"ok"}` but `browser_booted:false` | Normal until the first tool call. |
| Browser boots **headless** despite `NETWALA_HEADED=1` | Xvfb was not up at launch. Restart the server; the launcher starts Xvfb and waits. |
| `WebGL: NO_CONTEXT` | Headless mode. Set `NETWALA_HEADED=1` and ensure Xvfb runs on `NETWALA_DISPLAY`. |
| `NETWALA_KEY env var required` | Generate a Fernet key (see Installation) and export it. |
| `Unsupported engine: 'chromium'` | Expected — Chromium was removed. Use Firefox. |
| `payloads_data.py module not found` | The payload library is missing from the server directory. Restore `payloads_data.py`. |
| Path/URL refused | See [Security model](#security-model); use `NETWALA_IO_DIRS` / `NETWALA_ALLOW_LOOPBACK`. |
| Stale `/tmp/playwright_firefoxdev_profile-*` | Cleared automatically at startup. |
| `start_netwala.sh` exits with -15 | The launcher's `pkill -f netwala.py` matched the invoking shell. Run the launcher on its own line. |

---

## ⚠️ Limitations

- Browser binary availability differs by OS and CPU architecture.
- Android PRoot may require platform-specific workarounds for browser
  installation and execution.
- Browser contexts isolate browser state, not the host operating system.
- Heavy parallel workloads can exhaust memory, especially on mobile devices.
  The project notes an estimated **~150 MB per Firefox context** — approximate
  and environment-dependent, not a guarantee.
- Dynamic applications may require custom waits or workflow-specific handling.
- WAF and bot-protection behavior varies by target; no evasion or bypass is
  guaranteed.
- Tool descriptions in this README do not replace the live MCP schemas returned
  by the server.

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

**v3.0** — `inject_cookies` merged into `set_cookies`.

---

## 🚧 Roadmap

- **Web dashboard** — monitor active agents, browser contexts and network
  activity.
- **Burp Suite bridge** — export selected requests into Burp Suite workflows.
- **Proxy rotation support** — configurable proxy-provider integrations, with
  per-context proxy persisted in the encrypted storage state.
- **Docker image** — reproducible container deployment.
- **Multi-agent orchestration** — coordination and resource control across
  browser contexts.
- **Evidence engine** — standardized, reproducible evidence and finding records
  (including HAR → findings stubs).
- **Scope-pinned crawling** — enforce an in-scope origin allowlist during
  `crawl_site` / `extract_links`.
- **Stealth scorecard** — one call that reports the live fingerprint and flags
  inconsistencies before you touch a target.
- **Human-in-the-loop challenge queue** — surface a Turnstile/DataDome widget to
  the operator and resume. No automated solving.
- **Request assertions and batch replay** — declarative pass/fail checks and
  rate-controlled queues for IDOR sweeps.
- **Regression verification** — re-run checks after tool fixes and releases.
- **Camoufox evaluation** — *evaluated and rejected on ARM64*; revisit only if
  the platform constraint changes.

---

## 🤝 Contributing

Contributions, bug reports and documentation improvements are welcome.

When reporting a bug, include:

- Netwala version or commit.
- Operating system and CPU architecture.
- Python and Playwright versions.
- Browser engine and version.
- Exact reproduction steps.
- Relevant sanitized logs or screenshots.
- Expected and actual behavior.

**Do not** include live credentials, session cookies, private tokens or
sensitive target data in public issues.

---

## FAQ

**Is this a CAPTCHA bypass tool?** No. It is a browser automation layer with a
consistent fingerprint and human-like input. Solving challenges is left to a
human operator; automated CAPTCHA solving is deliberately not implemented
because it violates the terms of the protected services.

**Will this beat Cloudflare/DataDome?** Not by itself. It removes the obvious
automation tells, which gets you through low-volume browsing on many sites. IP
reputation, volume and challenge-solving are separate gates.

**Why does `navigate` sometimes report 403 and then the page looks fine?** The
first document was the interstitial. `navigate` now follows the real document
and reports both statuses — read `soft_challenge_resolved`.

**Can I use it for scraping?** Technically yes at low volume, but that is not
what it is for, and it will lose to rate limiting. Use a proper crawler.

**Does it work on my laptop?** On Debian/Ubuntu/Kali, yes. On macOS, probably.
On Windows, only inside WSL2.

**Why was Chromium removed?** On ARM64 + PRoot (the primary Termux deployment)
Chromium is unstable, and headless Chromium leaks enough signals to be
worthless against modern bot protection. Firefox runs natively on AArch64.

---

## 💖 Support

Netwala is open-source software. If it is useful in your authorized security
research, you can support ongoing development.

- **GitHub Sponsors:** https://github.com/sponsors/Piyankali
- **Issues:** https://github.com/Piyankali/Netwala/issues

---

## License

Released under the **MIT License**.

Provided for authorized security research. Use only against systems you own or
have explicit written permission to test. The authors accept no liability for
misuse.

---

## Legal notice

Netwala is a dual-use security tool. Accessing a system without authorization is
illegal in most jurisdictions, regardless of the tool used. You are responsible
for complying with all applicable laws, with the terms of service of the
systems you test, and with the rules of any bug-bounty program you participate
in. The maintainers do not endorse or encourage unauthorized access.

By using Netwala you acknowledge that you will only test systems you own or have
explicit written permission to assess.
