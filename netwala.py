"""
netwala — Playwright-based MCP server.
Firefox (Juggler) only. Chromium was removed: it is unstable on ARM64 + PRoot
(the primary Termux deployment) and headless Chromium leaks enough signals to be
useless against modern bot protection. Debian 13 compatible.
"""
import asyncio
import json
import os
import random
import signal
import sys
import time
import uuid
from typing import Optional, Dict, Any, List, Union

try:
    from mcp.server.fastmcp import FastMCP as MCPServer
except ImportError:
    from mcp.server.mcpserver import MCPServer

from browser_manager import mgr

MCP_HOST = os.environ.get("NETWALA_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("NETWALA_PORT", "8765"))
MCP_PATH = "/mcp"


# ═══════════════════════════════════════════════════════════════════
# DEVICE PRESETS — single source of truth shared by spawn_subagent_environment
# and emulate_device (keeps the two handlers from drifting apart).
#   ua  = User-Agent (network + JS)
#   vp  = viewport
#   dpr = devicePixelRatio  (real via device_scale_factor + durable init script)
#   tp  = maxTouchPoints    (Firefox cannot set it natively -> init-script override)
# ═══════════════════════════════════════════════════════════════════
DEVICE_PRESETS = {
    "iphone":    {"ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15", "vp": {"width": 390, "height": 844},  "dpr": 3,     "tp": 5, "oscpu": "iPhone",                    "platform": "iPhone",          "locale": "en-US", "tz": "America/New_York"},
    "iphone 13": {"ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15", "vp": {"width": 390, "height": 844},  "dpr": 3,     "tp": 5, "oscpu": "iPhone",                    "platform": "iPhone",          "locale": "en-US", "tz": "America/New_York"},
    "ipad":      {"ua": "Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) AppleWebKit/605.1.15",          "vp": {"width": 820, "height": 1180}, "dpr": 2,     "tp": 5, "oscpu": "iPad",                      "platform": "iPad",            "locale": "en-US", "tz": "America/New_York"},
    "pixel":     {"ua": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36",                 "vp": {"width": 412, "height": 915},  "dpr": 2.625, "tp": 5, "oscpu": "Linux armv81",              "platform": "Linux armv8l",    "locale": "en-US", "tz": "America/New_York"},
    "samsung":   {"ua": "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36",               "vp": {"width": 360, "height": 800},  "dpr": 2.75,  "tp": 5, "oscpu": "Linux armv81",              "platform": "Linux armv8l",    "locale": "en-US", "tz": "America/New_York"},
    "desktop":   {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",                                   "vp": {"width": 1920, "height": 1080}, "dpr": 1,    "tp": 0, "oscpu": "Windows NT 10.0; Win64; x64", "platform": "Win32",           "locale": "en-US", "tz": "America/New_York"},
}



# ═══════════════════════════════════════════════════════════════════
# CROSS-CUTTING INFRASTRUCTURE (Phase D)
# ═══════════════════════════════════════════════════════════════════
import logging
from datetime import datetime as _dt
from pathlib import Path as _Path

_LOG_DIR = _Path.home() / ".hermes" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / f"netwala_{_dt.now().strftime('%Y-%m-%d')}.log"

_netlog = logging.getLogger("netwala.tools")
if not _netlog.handlers:
    _netlog.setLevel(logging.INFO)
    _fh = logging.FileHandler(_LOG_FILE, mode='a', encoding='utf-8')
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
    _netlog.addHandler(_fh)

def _verbose():
    return os.environ.get("NETWALA_VERBOSE", "0").lower() in ("1", "true", "yes")


def _log_tool(tool, status, duration_ms=0, args_summary="", error=None):
    if not (_verbose() or error):
        return
    try:
        msg = f"{tool} status={status} dur={duration_ms}ms args={str(args_summary)[:200]}"
        if error:
            msg += f" err={str(error)[:300]}"
        _netlog.info(msg)
        for _h in _netlog.handlers:
            try: _h.flush()
            except Exception: pass
    except Exception:
        pass


def _struct_err(tool, exc, **extra):
    """Structured error helper for LLM recovery."""
    ename = type(exc).__name__
    msg = str(exc)[:400]
    err = {"tool": tool, "code": ename, "message": msg}
    if "Timeout" in ename:
        err["suggestion"] = "Increase timeout_ms or wait_for_selector before this call"
        err["retry_after_ms"] = 1000
    elif "Selector" in msg or "not found" in msg.lower():
        err["suggestion"] = "Verify selector via get_page_snapshot or get_visual_navigation_map"
    elif "TargetClosed" in ename or "closed" in msg.lower():
        err["suggestion"] = "Tab may be closed — call list_browser_targets and spawn new context"
    elif "connect" in msg.lower() or "net::" in msg:
        err["suggestion"] = "Network unreachable — verify URL or check proxy"
    err.update(extra)
    return json.dumps({"error": err})


async def _retry(tool, fn, max_attempts=2, delay=0.5):
    """Retry fn on transient failures (TimeoutError, TargetClosedError)."""
    import asyncio as _a
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as e:
            last = e
            ename = type(e).__name__
            transient = ("Timeout" in ename or "TargetClosed" in ename
                         or "Connection" in ename or "net::" in str(e))
            if transient and attempt < max_attempts:
                _log_tool(tool, "retry", 0, f"attempt={attempt}", error=str(e)[:100])
                await _a.sleep(delay * attempt)
                continue
            raise
    raise last


def _wrap_tool(orig_fn, tool_name):
    """Wrap any tool with: logging, timing, structured errors."""
    import functools
    import time as _t

    @functools.wraps(orig_fn)
    async def wrapper(*args, **kwargs):
        start = _t.time()
        try:
            result = await orig_fn(*args, **kwargs)
            dur = int((_t.time() - start) * 1000)
            # Detect tool-level {"error": ...} response (not exception) for logging
            status = "ok"
            if isinstance(result, str):
                _s = result.lstrip()
                try:
                    _d = json.loads(_s)
                except Exception:
                    _d = None
                if isinstance(_d, dict):
                    if ("error" in _d or _d.get("ok") is False
                            or _d.get("status") in ("failed", "error")):
                        status = "tool_error"
                elif _s.startswith('{"error"'):
                    status = "tool_error"
            # Never log args/kwargs: they can carry cookies, auth headers and
            # other secrets. A count is enough for diagnostics.
            _log_tool(tool_name, status, dur,
                      f"args={len(args)} kwargs={len(kwargs)}")
            return result
        except Exception as e:
            dur = int((_t.time() - start) * 1000)
            _log_tool(tool_name, "error", dur, "", error=e)
            return _struct_err(tool_name, e)

    return wrapper



def _norm_url(u: str) -> str:
    """Loose URL compare: drop fragment, trailing slash, case of the host."""
    try:
        u = (u or "").strip()
        if "#" in u:
            u = u.split("#", 1)[0]
        if u.endswith("/") and u.count("/") > 2:
            u = u[:-1]
        return u.lower()
    except Exception:
        return (u or "").lower()


# ------------------------ INPUT SAFETY ------------------------
# The agent supplies paths and URLs. Nothing here is trusted: a prompt-injected
# page can steer the agent, so every container-side path and every outbound URL
# is checked before it is used. Loopback / link-local / cloud-metadata hosts are
# blocked by default (set NETWALA_ALLOW_LOOPBACK=1 for authorized internal testing).
ALLOWED_IO_DIRS = [
    "/tmp",
    "/root/netwala",
    "/data/data/com.termux/files/home",      # Termux home (bind-mounted)
]
# extra allowlisted dirs, colon-separated: NETWALA_IO_DIRS=/srv:/mnt/data
for _extra in os.environ.get("NETWALA_IO_DIRS", "").split(":"):
    _extra = _extra.strip()
    if _extra:
        ALLOWED_IO_DIRS.append(_extra)
ALLOWED_SCHEMES = ("http", "https")
BLOCKED_HOSTS = {
    "127.0.0.1", "::1", "0.0.0.0", "localhost",
    "169.254.169.254", "metadata.google.internal", "[::1]",
}
_ALLOW_LOOPBACK = os.environ.get("NETWALA_ALLOW_LOOPBACK", "0").strip().lower() in ("1", "true", "yes", "on")
_ALLOW_FILE = os.environ.get("NETWALA_ALLOW_FILE", "0").strip().lower() in ("1", "true", "yes", "on")


def _realpath(p: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(p))
    except Exception:
        return os.path.abspath(p)


def _under(path: str, roots) -> bool:
    rp = _realpath(path)
    for root in roots:
        r = _realpath(root)
        if rp == r or rp.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def safe_path(path, what: str = "file", must_exist: bool = False) -> str:
    """Allow only paths inside ALLOWED_IO_DIRS (realpath-resolved, no traversal)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{what}: path must be a non-empty string")
    rp = _realpath(path)
    if not _under(rp, ALLOWED_IO_DIRS):
        raise ValueError(
            f"{what}: path '{path}' is outside the allowed directories "
            f"({', '.join(ALLOWED_IO_DIRS)}). Refusing to touch it."
        )
    if must_exist and not os.path.exists(rp):
        raise ValueError(f"{what}: file not found: {rp}")
    return rp


def check_url(url, what: str = "url") -> str:
    """Only http/https; loopback / link-local / cloud-metadata hosts are blocked."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{what}: url must be a non-empty string")
    if url.strip().lower() in ("about:blank", "about:srcdoc"):
        return url.strip()
    from urllib.parse import urlparse
    try:
        u = urlparse(url.strip())
    except Exception:
        raise ValueError(f"{what}: cannot parse url '{url}'")
    scheme = (u.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        if scheme == "file" and _ALLOW_FILE:
            return url.strip()          # explicitly opted in
        raise ValueError(
            f"{what}: scheme '{u.scheme}' not allowed (only http/https"
            + (" plus file, NETWALA_ALLOW_FILE=1" if _ALLOW_FILE else "")
            + "). Other schemes are blocked."
        )
    host = (u.hostname or "").lower()
    if not host:
        raise ValueError(f"{what}: no host in '{url}'")
    blocked = host in BLOCKED_HOSTS
    if not blocked:
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
            blocked = ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
        except ValueError:
            blocked = False
    if blocked and not _ALLOW_LOOPBACK:
        raise ValueError(
            f"{what}: host '{host}' is loopback / link-local / cloud-metadata and is "
            "blocked by default. Set NETWALA_ALLOW_LOOPBACK=1 to allow it for "
            "authorized internal testing."
        )
    return url.strip()


def _dumps_capped(obj, limit: int = 50000) -> str:
    """Serialize to JSON safely — trims list fields before serialization."""
    try:
        s = json.dumps(obj, default=str)
        if len(s) <= limit:
            return s
        # Trim heavy list fields
        for key in ("elements", "events", "results", "links", "secrets_found", "payloads"):
            if isinstance(obj.get(key), list):
                arr = obj[key]
                while arr and len(json.dumps(obj, default=str)) > limit:
                    arr.pop()
                obj["truncated"] = True
                obj["_original_size"] = len(s)
                return json.dumps(obj, default=str)
        # Fallback: trim the whole object
        return json.dumps({"truncated": True, "original_size": len(s),
                           "partial": s[:limit]}, default=str)
    except Exception as e:
        return json.dumps({"error": f"serialization failed: {e}"})


mcp = MCPServer(
    "netwala-playwright",
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path=MCP_PATH,
)



# ── Suppress benign SSE disconnect noise (anyio.ClosedResourceError) ──
import logging

class _IgnoreSSEDisconnect(logging.Filter):
    """Filters out harmless ClosedResourceError from MCP SSE writer on client disconnect."""
    _KEYS = (
        "ClosedResourceError",
        "standalone_sse_writer",
        "Error in standalone SSE writer",
        "Exception in ASGI application",
        "Application shutdown failed",
    )

    def filter(self, record):
        # Message-based match
        msg = record.getMessage() or ""
        if any(k in msg for k in self._KEYS):
            return False
        # Exc-info based match (covers logger.exception calls)
        if record.exc_info:
            exc = record.exc_info[1]
            if exc and type(exc).__name__ == "ClosedResourceError":
                return False
            # Nested cause
            cause = getattr(exc, "__cause__", None)
            if cause and type(cause).__name__ == "ClosedResourceError":
                return False
        return True

for _name in ("mcp", "mcp.server", "mcp.server.streamable_http",
              "uvicorn", "uvicorn.error", "uvicorn.access", "asyncio", "anyio"):
    logging.getLogger(_name).addFilter(_IgnoreSSEDisconnect())

# ------------------------ HEALTH ROUTE ------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    return JSONResponse({
        "status": "ok",
        "server": "netwala-playwright",
        "browser_booted": len(mgr._browsers) > 0,
        "endpoint": f"http://127.0.0.1:{MCP_PORT}{MCP_PATH}",
    })


# ------------------------ HUMAN-LIKE INPUT ------------------------

async def human_move(page, tx: float, ty: float, steps: int = 25) -> None:
    """Bézier travel with ease-in/out and per-step speed variation.

    A real hand accelerates out, cruises, then decelerates into the target —
    it never moves at a constant rate like a synthetic straight line.
    """
    sx, sy = mgr.last_mouse
    dist = ((tx - sx) ** 2 + (ty - sy) ** 2) ** 0.5
    if dist < 2:
        return
    cx = (sx + tx) / 2 + random.uniform(-0.12, 0.12) * dist
    cy = (sy + ty) / 2 + random.uniform(-0.12, 0.12) * dist
    n = max(8, min(steps, int(dist / 12) + 8))
    for i in range(1, n + 1):
        u = i / n
        e = u * u * (3 - 2 * u)                    # smoothstep
        x = (1-e)**2*sx + 2*(1-e)*e*cx + e**2*tx + random.uniform(-0.5, 0.5)
        y = (1-e)**2*sy + 2*(1-e)*e*cy + e**2*ty + random.uniform(-0.5, 0.5)
        await page.mouse.move(x, y)
        base = 0.016 * (0.35 + 1.3 * (1.0 - abs(2 * u - 1)))   # slow at the ends
        await asyncio.sleep(random.uniform(base * 0.6, base * 1.5))
    mgr.last_mouse = (tx, ty)


async def human_click(page, x: float, y: float) -> None:
    # Clamp to viewport to avoid silent no-op clicks
    try:
        vp = await page.evaluate("({w: innerWidth, h: innerHeight})")
        w, h = vp["w"], vp["h"]
        if x < 0 or y < 0 or x >= w or y >= h:
            raise ValueError(
                f"Click coords ({x},{y}) outside viewport ({w}x{h}). "
                f"Scroll element into view before clicking."
            )
    except ValueError:
        raise
    except Exception:
        pass  # non-fatal — proceed if evaluate failed

    await human_move(page, x, y)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.03, 0.08))
    await page.mouse.up()


# ------------------------ HUMAN BEHAVIOUR LAYER ------------------------
# Default ON (NETWALA_HUMAN=0 to disable). Makes the browser look like a person:
# curved mouse travel, settling pauses, jittered typing, smooth chunked scrolling.

_HUMAN_ENABLED = os.environ.get("NETWALA_HUMAN", "1").lower() in ("1", "true", "yes")


def h_on() -> bool:
    return _HUMAN_ENABLED


async def h_dwell(lo: float = 0.4, hi: float = 1.2) -> None:
    """Pause like a person reading / thinking between actions."""
    if _HUMAN_ENABLED:
        await asyncio.sleep(random.uniform(lo, hi))


async def h_wander(page) -> None:
    """Small idle mouse drift, as if the person is not touching the mouse."""
    if not _HUMAN_ENABLED:
        return
    try:
        vp = await page.evaluate("({w: innerWidth, h: innerHeight})")
        await human_move(page, random.uniform(0.1, 0.9) * vp["w"],
                         random.uniform(0.1, 0.9) * vp["h"],
                         steps=random.randint(8, 18))
    except Exception:
        pass


async def h_scroll(page, dy: float) -> None:
    """Smooth, chunked scroll with variable speed and occasional pauses."""
    if not _HUMAN_ENABLED:
        await page.evaluate(f"window.scrollBy(0, {int(dy)})")
        return
    remaining = float(dy)
    sign = 1.0 if dy >= 0 else -1.0
    guard = 0
    while abs(remaining) > 2 and guard < 200:
        guard += 1
        step = sign * random.uniform(50, 170)
        if abs(step) > abs(remaining):
            step = remaining
        await page.mouse.wheel(0, step)
        remaining -= step
        await asyncio.sleep(random.uniform(0.04, 0.16))
        if random.random() < 0.12:              # pause mid-scroll
            await asyncio.sleep(random.uniform(0.25, 0.9))
    if abs(remaining) > 0.5:
        await page.mouse.wheel(0, remaining)


async def h_click(page, x: float, y: float, button: str = "left", click_count: int = 1) -> None:
    """Curved travel, settle, then click with a human press duration."""
    await human_click(page, x, y)
    for _ in range(click_count - 1):
        await asyncio.sleep(random.uniform(0.08, 0.18))
        await page.mouse.click(x, y, button=button)


async def h_type(page, text: str, base_delay_ms: int = 30) -> None:
    """Per-key typing with jitter and occasional thinking pauses."""
    if not _HUMAN_ENABLED:
        await page.keyboard.type(text, delay=base_delay_ms)
        return
    d = max(base_delay_ms / 1000.0, 0.01)
    for ch in text:
        await page.keyboard.type(ch, delay=0)
        await asyncio.sleep(random.uniform(d * 0.5, d * 1.8))
        if random.random() < 0.05:
            await asyncio.sleep(random.uniform(0.15, 0.5))
        if ch in ".!? " and random.random() < 0.25:
            await asyncio.sleep(random.uniform(0.1, 0.35))


# ------------------------ NUCLEAR PRIMITIVES ------------------------

@mcp.tool()
async def server_boot(
    engine: str = "firefox",
    headless: Optional[bool] = None,
    warmup: bool = False,
) -> str:
    """
    Initialize Playwright server. Call once or let first tool auto-boot.
    warmup: pre-open blank page to avoid first-call delay
    headless: None = env default (NETWALA_HEADED, headed/Xvfb by default)
    """
    try:
        # Normalise so "Firefox" / "FIREFOX" / " firefox " all work; anything
        # else is rejected explicitly instead of reaching _launch's ValueError.
        engine = (engine or "firefox").strip().lower()
        if engine != "firefox":
            return json.dumps({
                "status": "failed",
                "error": (f"Unsupported engine: '{engine}'. Netwala only supports Firefox "
                          "(Chromium incompatible with ARM64+PRoot). "
                          "Use engine='firefox' or omit the parameter."),
            })
        if mgr._pw is None:
            await mgr.start(engine=engine, headless=headless)
            status = "booted"
        else:
            status = "already_booted"
            # The browser cannot be restarted here, but keep the recorded
            # default engine consistent with what was asked for.
            try:
                mgr._default_engine = engine
            except Exception:
                pass
        warmed = False
        if warmup:
            try:
                # Use a throwaway context — don't touch agent's active tabs
                warm_ctx_id, warm_ctx = await mgr.new_context()
                warm_pg = await warm_ctx.new_page()
                await warm_pg.goto("about:blank", timeout=5000)
                await mgr.destroy_context(warm_ctx_id)
                warmed = True
            except Exception:
                pass
        return json.dumps({
            "status": status, "engine": engine, "headless": mgr._headless,
            "warmed_up": warmed, "contexts": len(mgr.list_contexts()),
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:400]})


@mcp.tool()
async def navigate(
    url: str,
    target_id: Optional[str] = None,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 60000,
    return_redirect_chain: bool = False,
    auth_probe: bool = False,
    auth_keywords: Optional[List[str]] = None,
    screenshot_on_load: bool = False,
    scroll_to_bottom: bool = False,
) -> str:
    """
    Navigate with optional redirect chain, auth detection, screenshot, scroll.
    auth_probe: detect if redirected to login (default keywords: login|signin|auth|session)
    """
    try:
        check_url(url, "navigate")
        page, tid = await mgr.get_page(target_id)
        redirect_chain = []
        _on_response = None
        if return_redirect_chain:
            def _on_response(resp):
                try:
                    if resp.request.is_navigation_request():
                        redirect_chain.append({"status": resp.status, "url": resp.url})
                except Exception:
                    pass
            page.on("response", _on_response)
        # Track every document response from BEFORE the navigation: a WAF
        # interstitial can replace the document within milliseconds of goto
        # returning, so a listener attached afterwards misses it.
        _doc = {"r": None, "t": 0.0}
        def _on_doc(resp2):
            try:
                if resp2.request.is_navigation_request():
                    _doc["r"] = resp2
                    _doc["t"] = time.time()
            except Exception:
                pass
        page.on("response", _on_doc)

        resp = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        # a person reads the page for a moment before doing anything
        await h_dwell(0.12, 0.4)
        # --- soft-challenge follow-through -------------------------------
        # Some WAFs (DataDome's interstitial) answer the FIRST document with
        # 403/429/503, bounce through captcha-delivery, then come back to the
        # real page. goto() returns that first response, so wait for the
        # document that actually belongs to the requested URL and report that.
        first_status = resp.status if resp else None
        settled = None
        if resp is not None and first_status in (403, 429, 503):
            _target = _norm_url(url)
            _t0 = time.time()
            while time.time() - _t0 < 20.0:
                await asyncio.sleep(0.2)
                # (a) the real document arrived as a response
                r = _doc["r"]
                try:
                    if r is not None and r is not resp and _norm_url(r.url) == _target:
                        resp = r
                        break
                except Exception:
                    pass
                # (b) or the interstitial finished and painted real content
                try:
                    s = await page.evaluate(
                        "({l: document.documentElement.outerHTML.length,"
                        " b: /access is temporarily restricted|you have been blocked/i.test(document.body.innerText||'')})")
                except Exception:
                    s = None
                if s:
                    if s.get("b"):
                        break                       # hard block: nothing will change
                    if s.get("l", 0) > 50000:
                        settled = s                 # real content is on screen
                        break
            # let the title catch up with the settled document
            await h_dwell(0.3, 0.7)
            if resp.status == first_status and settled is None:
                try:
                    settled = await page.evaluate(
                        "({t: document.title, l: document.documentElement.outerHTML.length})")
                except Exception:
                    settled = None
        try:
            page.remove_listener("response", _on_doc)
        except Exception:
            pass
        if return_redirect_chain and _on_response:
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass
        result = {
            "status": "navigated",
            "target_id": tid,
            "url": page.url,
            "title": await page.title(),
            "http_status": resp.status if resp else None,
            "response_url": resp.url if resp else None,
        }
        if first_status is not None and resp is not None and resp.status != first_status:
            result["soft_challenge_resolved"] = True
            result["first_http_status"] = first_status
            result["note"] = ("first document came back "
                              f"{first_status} (WAF interstitial); the page "
                              "reloaded and the real document is reported")
        elif (first_status in (403, 429, 503) and settled
              and settled.get("l", 0) > 50000):
            result["soft_challenge_resolved"] = True
            result["first_http_status"] = first_status
            result["settled_title"] = settled.get("t")
            result["settled_html_bytes"] = settled.get("l")
            result["note"] = ("first document was a WAF interstitial "
                              f"({first_status}); the follow-up document was "
                              "not captured, but the page settled to real content")
        if return_redirect_chain:
            result["redirect_chain"] = redirect_chain
            result["redirect_count"] = len(redirect_chain)
        if auth_probe:
            kws = auth_keywords or ["login", "signin", "sign-in", "auth", "session"]
            final_url = page.url.lower()
            final_title = (result.get("title") or "").lower()
            detected = any(k in final_url or k in final_title for k in kws)
            result["auth_required"] = detected
            result["auth_keywords_checked"] = kws
        if scroll_to_bottom:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)
            result["scrolled_to_bottom"] = True
        if screenshot_on_load:
            path = f"/tmp/nav_{tid}_{uuid.uuid4().hex[:6]}.png"
            await page.screenshot(path=path, full_page=True)
            result["screenshot"] = path
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def evaluate_javascript(
    js_code: str,
    target_id: Optional[str] = None,
    args: Optional[List[Any]] = None,
    timeout_ms: int = 30000,
    frame_url_pattern: Optional[str] = None,
) -> str:
    """
    Run JS. Now captures exact Error Stack Traces for advanced debugging.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        target_context = page
        executed_in_frame = False
        
        if frame_url_pattern:
            import re as _re
            matched_frame = None
            for frame in page.frames:
                if _re.search(frame_url_pattern, frame.url):
                    matched_frame = frame
                    break
            if not matched_frame:
                return json.dumps({
                    "ok": False,
                    "error": "FRAME_NOT_FOUND",
                    "message": f"No frame matching '{frame_url_pattern}' found on page",
                    "available_frames": [f.url for f in page.frames],
                    "hint": "Check frame_url_pattern or wait for iframe to load"
                })
            target_context = matched_frame
            executed_in_frame = True

        if args is not None:
            if isinstance(args, list) and len(args) > 0:
                arg_refs = ", ".join([f"__args[{i}]" for i in range(len(args))])
                wrapped = f"(__args) => (({js_code})({arg_refs}))"
                result = await asyncio.wait_for(target_context.evaluate(wrapped, args), timeout=timeout_ms / 1000.0)
            else:
                result = await asyncio.wait_for(target_context.evaluate(js_code, args), timeout=timeout_ms / 1000.0)
        else:
            result = await asyncio.wait_for(target_context.evaluate(js_code), timeout=timeout_ms / 1000.0)
            
        return json.dumps({"ok": True, "result": result, "executed_in_frame": bool(frame_url_pattern)}, default=str)
    except asyncio.TimeoutError:
        return json.dumps({"ok": False, "error": "Timeout"})
    except Exception as e:
        # Extract stack trace if available
        stack = getattr(e, "stack", str(e))
        return json.dumps({"ok": False, "error": type(e).__name__, "message": str(e)[:500], "stack_trace": stack[:1000]})


@mcp.tool()
async def get_page_snapshot(
    target_id: Optional[str] = None,
    include_form_states: bool = True,
    include_hidden: bool = False,
    include_meta: bool = False,
    include_comments: bool = True,
    include_text_elements: bool = True,
    max_elements: int = 200,
) -> str:
    """
    Extracts page info including interactive elements, HTML comments, and text headings.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        
        js_code = """
        ([includeHidden, includeMeta, includeComments, includeText, maxEl, includeFormStates]) => {
            const result = { title: document.title, url: location.href };
            const els = [];
            
            // Build selector dynamically based on include_text_elements
            let sel = 'input,button,a,form,select,textarea,[role=button]';
            if (includeText) {
                sel += ',h1,h2,h3,h4,pre,table,th,td';
            }
            
            for (const el of document.querySelectorAll(sel)) {
                if (els.length >= maxEl) break;
                const visible = el.offsetWidth > 0 && el.offsetHeight > 0;
                if (!visible && !includeHidden) continue;
                
                els.push({
                    tag: el.tagName.toLowerCase(), 
                    id: el.id || null, 
                    name: el.getAttribute('name') || null,
                    text: (el.innerText || el.value || '').trim().slice(0, 100) // text cap at 100 chars
                });
            }
            result.elements = els;
            
            if (includeComments) {
                const comments = [];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_COMMENT, null, false);
                while (walker.nextNode()) {
                    const val = walker.currentNode.nodeValue.trim();
                    if (val.length > 2) comments.push(val);
                }
                result.html_comments = comments;
            }
            
            if (includeFormStates) {
                result.form_states = Array.from(
                    document.querySelectorAll('input,select,textarea')).slice(0, maxEl).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type'),
                    name: el.getAttribute('name') || null,
                    value: (el.value == null ? null : String(el.value).slice(0, 200)),
                    checked: !!el.checked,
                    disabled: !!el.disabled,
                    required: !!el.required
                }));
            }
            if (includeMeta) {
                result.meta = Array.from(document.querySelectorAll('meta')).map(m => ({
                    name: m.getAttribute('name') || m.getAttribute('property'),
                    content: (m.getAttribute('content') || '').slice(0, 200)
                })).filter(m => m.name);
            }
            return result;
        }
        """
        result = await page.evaluate(js_code, [include_hidden, include_meta, include_comments, include_text_elements, max_elements, include_form_states])
        result["target_id"] = tid
        return _dumps_capped(result, 50000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def capture_screenshot(
    target_id: Optional[str] = None,
    full_page: bool = True,
    save_path: Optional[str] = None,
    format: str = "png",
) -> str:
    """Capture screenshot. format: 'png', 'jpeg', or 'pdf' (auto-fallbacks to png on Firefox)."""
    try:
        if save_path:
            save_path = safe_path(save_path, "capture_screenshot")
        page, tid = await mgr.get_page(target_id)
        ext = format.lower()
        
        if ext == "pdf":
            try:
                path = save_path or f"/tmp/shot_{uuid.uuid4().hex[:8]}.pdf"
                await page.pdf(path=path, format="A4", print_background=True)
                return json.dumps({"status": "saved", "path": path, "format": "pdf", "bytes": os.path.getsize(path)})
            except Exception as e:
                if "Chromium" in str(e) or "PDF" in str(e):
                    ext = "png" # Fallback for Firefox
                else:
                    raise e

        path = save_path or f"/tmp/shot_{uuid.uuid4().hex[:8]}.{ext}"
        await page.screenshot(path=path, full_page=full_page, type=ext)
        return json.dumps({"status": "saved", "path": path, "format": ext, "bytes": os.path.getsize(path), "note": "Auto-fallback applied if PDF was requested."})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


# ------------------ REAL FILE UPLOAD (was stub) ------------------

@mcp.tool()
async def set_file_input_files(
    selector: str,
    file_paths: Optional[List[str]] = None,
    base64_files: Optional[List[Dict[str, str]]] = None,
    bypass_accept: bool = False,
    target_id: Optional[str] = None,
) -> str:
    """
    Attach files. 
    bypass_accept=True bypasses the <input accept="..."> restriction (e.g., upload PHP to an image field).
    """
    try:
        if file_paths:
            file_paths = [safe_path(p, "set_file_input_files", must_exist=True) for p in file_paths]
        page, tid = await mgr.get_page(target_id)
        loc = page.locator(selector).first
        if await loc.count() == 0: return json.dumps({"error": f"Selector not found: {selector}"})
        
        if bypass_accept:
            # Remove the accept attribute using JS, in the SAME frame as the
            # element (page.evaluate only sees the main document).
            _accept_js = ("(sel) => { const el = document.querySelector(sel);"
                          " if (el) el.removeAttribute('accept');"
                          " return el ? 'removed' : 'not_found'; }")
            _frame = page.main_frame
            try:
                for _f in page.frames:
                    try:
                        if await _f.evaluate(
                                "!!document.querySelector(" + json.dumps(selector) + ")"):
                            _frame = _f
                            break
                    except Exception:
                        continue
            except Exception:
                _frame = page.main_frame
            try:
                await _frame.evaluate(_accept_js, selector)
            except Exception:
                pass

        if base64_files:
            import base64 as _b64
            import binascii as _bin
            tmp_files = []
            try:
                for item in base64_files:
                    # basename only: a name of "../../etc/cron.d/x" must not be
                    # able to escape /tmp via the f-string.
                    name = os.path.basename(item.get("name", "upload.bin")) or "upload.bin"
                    data = item.get("base64", "")
                    # Decode BEFORE opening the file: a bad payload must not
                    # leave a 0-byte orphan behind in /tmp.
                    try:
                        raw = _b64.b64decode(data, validate=True)
                    except (_bin.Error, ValueError, TypeError) as _e:
                        return json.dumps({
                            "error": f"Invalid base64 for '{name}': {type(_e).__name__}: {str(_e)[:120]}",
                            "hint": "base64 must be standard-alphabet, correctly padded.",
                        })
                    path = safe_path(f"/tmp/upload_{uuid.uuid4().hex[:8]}_{name}",
                                     "set_file_input_files")
                    with open(path, "wb") as f:
                        f.write(raw)
                    tmp_files.append(path)
                await loc.set_input_files(tmp_files, timeout=10000)
            finally:
                for _p in tmp_files:
                    try:
                        os.remove(_p)
                    except Exception:
                        pass
            return json.dumps({"status": "files_set", "mode": "base64", "count": len(tmp_files), "bypass_accept": bypass_accept})
            
        if not file_paths: return json.dumps({"error": "provide file_paths or base64_files"})
        missing = [p for p in file_paths if not os.path.exists(p)]
        if missing: return json.dumps({"error": f"Files not found: {missing}"})
        
        await loc.set_input_files(file_paths, timeout=10000)
        return json.dumps({"status": "files_set", "mode": "path", "count": len(file_paths), "bypass_accept": bypass_accept})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


# ---------------- REAL HAR CAPTURE (was stub) ----------------

@mcp.tool()
async def capture_har(
    url: str,
    har_path: Optional[str] = None,
    wait_ms: int = 3000,
    engine: Optional[str] = None,  # Only "firefox" supported (Chromium removed)
    include_bodies: bool = False,
    filter_pattern: Optional[str] = None,
    max_size_mb: int = 20,
    return_parsed: bool = False,
    extract_js_files: bool = False,
) -> str:
    """
    Record HAR.

    extract_js_files=True saves every JS source to /tmp/js_extracts/ (perfect for
    finding API keys, secrets and hidden endpoints in minified JS).
    max_size_mb caps how much of the HAR is analysed: when the file is larger,
    the most recent entries that fit the budget are kept and `size_capped` is set.
    return_parsed=True adds a `parsed` block (methods / statuses / hosts /
    mime_types, top 10 each).
    filter_pattern: regex applied to each entry's request URL; non-matching
    entries are dropped before analysis.
    include_bodies=True attaches up to 2000 chars of each matching response body
    under `bodies` (HAR content is recorded with content="embed").
    """
    har_path = har_path or f"/tmp/har_{uuid.uuid4().hex[:8]}.har"
    try:
        check_url(url, "capture_har")
        if har_path:
            har_path = safe_path(har_path, "capture_har")
        ctx_id, ctx = await mgr.new_context(engine=engine, har_path=har_path)
        try:
            page = await ctx.new_page()
            await page.goto(url, wait_until="load", timeout=60000)
            await asyncio.sleep(wait_ms / 1000.0)
        finally:
            await mgr.destroy_context(ctx_id)  # Proper cleanup + flushes HAR
        
        import os as _os
        import re as _re2
        with open(har_path, encoding="utf-8") as f:
            har = json.load(f)
        entries = har.get("log", {}).get("entries", [])
        total_entries = len(entries)

        _frx = None
        if filter_pattern:
            try:
                _frx = _re2.compile(filter_pattern)
            except _re2.error as _fe:
                return json.dumps({
                    "error": f"Invalid filter_pattern regex: {_fe}",
                    "hint": "Use a valid Python regex matched against the request URL.",
                })
        if _frx is not None:
            entries = [e for e in entries
                       if _frx.search(e.get("request", {}).get("url", ""))]

        try:
            _fsize_mb = _os.path.getsize(har_path) / (1024 * 1024)
        except Exception:
            _fsize_mb = 0.0
        size_capped = False
        if max_size_mb and max_size_mb > 0 and _fsize_mb > max_size_mb:
            # Keep the most recent entries that fit inside the budget.
            _budget = max_size_mb * 1024 * 1024
            _kept = []
            for _e in reversed(entries):
                _need = len(json.dumps(_e))
                if _budget - _need < 0:
                    break
                _budget -= _need
                _kept.append(_e)
            entries = list(reversed(_kept))
            size_capped = True

        result = {
            "status": "har_saved",
            "path": har_path,
            "total_entries": total_entries,
            "entries_analyzed": len(entries),
            "har_size_mb": round(_fsize_mb, 2),
            "max_size_mb": max_size_mb,
            "size_capped": size_capped,
        }

        if include_bodies:
            _bodies = []
            for _e in entries[:50]:
                _ct = _e.get("response", {}).get("content", {})
                _txt = _ct.get("text", "")
                if _txt:
                    _bodies.append({
                        "url": _e.get("request", {}).get("url", "")[:150],
                        "mime_type": _ct.get("mimeType", ""),
                        "body": _txt[:2000],
                    })
            result["bodies_included"] = True
            result["bodies_count"] = len(_bodies)
            result["bodies"] = _bodies

        if return_parsed:
            from collections import Counter
            from urllib.parse import urlparse as _up
            _urls = [e.get("request", {}).get("url", "") for e in entries]
            result["parsed"] = {
                "methods": dict(Counter(e.get("request", {}).get("method") for e in entries).most_common(10)),
                "statuses": dict(Counter(e.get("response", {}).get("status") for e in entries).most_common(10)),
                "hosts": dict(Counter(_up(u).netloc for u in _urls).most_common(10)),
                "mime_types": dict(Counter(
                    (e.get("response", {}).get("content", {}).get("mimeType") or "").split(";")[0]
                    for e in entries).most_common(10)),
            }
        
        if extract_js_files:
            import os
            js_dir = "/tmp/js_extracts"
            os.makedirs(js_dir, exist_ok=True)
            js_count = 0
            extracted_files = []
            for e in entries:
                mime = e.get("response", {}).get("content", {}).get("mimeType", "")
                if "javascript" in mime or "ecmascript" in mime:
                    body = e.get("response", {}).get("content", {}).get("text", "")
                    if body:
                        _u = e["request"]["url"].split("?")[0].rstrip("/")
                        name = os.path.basename(_u.split("/")[-1]) or f"script_{js_count}.js"
                        fpath = safe_path(f"{js_dir}/{js_count}_{name[:30]}.js", "capture_har")
                        with open(fpath, "w", encoding="utf-8") as jf:
                            jf.write(body)
                        extracted_files.append(fpath)
                        js_count += 1
            result["js_extracted_dir"] = js_dir
            result["js_files_count"] = js_count
            result["js_files_sample"] = extracted_files[:5]
            
        return json.dumps(result, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:500]})


# ----------------- VISUAL MAP + CLICK -----------------

@mcp.tool()
async def get_visual_navigation_map(
    target_id: Optional[str] = None,
    include_iframes: bool = True,
    include_shadow_dom: bool = True,
    include_disabled: bool = False,
    include_input_values: bool = False,
    include_form_context: bool = False,
    max_elements: int = 500,
) -> str:
    """
    SoM Map with CSS Selector and XPath extraction for precise agent targeting.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        all_elements = []
        element_id_counter = [1]

        scan_js = """
        ([includeShadow, includeDisabled, includeValues, includeForm]) => {
            const sel = 'a,button,input,select,textarea,[role=button],[onclick],[tabindex]:not([tabindex="-1"])';
            const out = [];
            const seen = new Set();
            
            function getXPath(el) {
                if (el.id) return '//*[@id="' + el.id + '"]';
                if (el === document.body) return '/html/body';
                let ix = 0, siblings = el.parentNode.childNodes;
                for (let i = 0; i < siblings.length; i++) {
                    let sibling = siblings[i];
                    if (sibling === el) return getXPath(el.parentNode) + '/' + el.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                    if (sibling.nodeType === 1 && sibling.tagName === el.tagName) ix++;
                }
            }

            function scanRoot(root, frameMarker) {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) continue;
                    const s = getComputedStyle(el);
                    if (s.visibility === 'hidden' || s.display === 'none') continue;
                    if (!includeDisabled && (el.disabled || el.getAttribute('aria-disabled') === 'true')) continue;
                    
                    const key = frameMarker + ':' + el.tagName + ':' + Math.round(r.x) + ':' + Math.round(r.y);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    
                    // Generate a decent CSS selector
                    let cssSel = el.tagName.toLowerCase();
                    if (el.id) cssSel += '#' + el.id;
                    if (el.name) cssSel += '[name="' + el.name + '"]';
                    // SVGElement.className is an SVGAnimatedString, not a
                    // string — .split() on it throws a TypeError.
                    const cn = (typeof el.className === 'string')
                        ? el.className
                        : ((el.className && el.className.baseVal) || '');
                    if (!el.id && !el.name && cn) {
                        const cls = cn.split(' ').filter(c => c && !c.includes(':')).slice(0,2).join('.');
                        if (cls) cssSel += '.' + cls;
                    }

                    const item = {
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 80),
                        css_selector: cssSel,
                        xpath: getXPath(el),
                        x: Math.round(r.left + r.width / 2),
                        y: Math.round(r.top + r.height / 2),
                        in_viewport: r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth
                    };
                    out.push(item);
                    if (includeShadow && el.shadowRoot) scanRoot(el.shadowRoot, frameMarker + ':shadow');
                }
            }
            scanRoot(document, 'main');
            return out;
        }
        """

        _scan_args = [include_shadow_dom, include_disabled, include_input_values, include_form_context]
        # include_iframes used to be accepted and silently ignored — the scan
        # only ever ran against the top document.
        _frames = [page.main_frame]
        if include_iframes:
            _frames += [f for f in page.frames if f != page.main_frame]
        for _fi, _fr in enumerate(_frames):
            try:
                _els = await _fr.evaluate(scan_js, _scan_args)
            except Exception:
                continue            # frame detached / navigated mid-scan
            for el in _els:
                el["id"] = element_id_counter[0]
                el["frame"] = "main" if _fi == 0 else (_fr.url or "")[:120]
                el["frame_index"] = _fi   # stable identity; URLs collide for
                                          # about:blank / srcdoc iframes
                element_id_counter[0] += 1
                all_elements.append(el)

        out = {
            "status": "map_generated",
            "total": len(all_elements),
            "returned": min(max_elements, len(all_elements)),
            "elements": all_elements[:max_elements],
        }
        # NOTE: never slice the serialized string (json.dumps(...)[:N]) — that
        # yields invalid JSON and breaks visual_click_by_id's json.loads.
        return _dumps_capped(out, 500000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def visual_click_by_id(
    element_id: int,
    target_id: Optional[str] = None,
    button: str = "left",
    click_count: int = 1,
    modifiers: Optional[List[str]] = None,
) -> str:
    """Click visual map element by ID with button/modifiers support. Auto-scrolls."""
    try:
        page, tid = await mgr.get_page(target_id)
        raw = await get_visual_navigation_map(tid, max_elements=1000)
        try:
            data = json.loads(raw)
        except Exception as e:
            return json.dumps({"error": f"visual map parse failed: {type(e).__name__}: {str(e)[:150]}"})
        els = data.get("elements") or []
        el = next((e for e in els if e.get("id") == element_id), None)
        if not el:
            # The map was generated with max_elements=1000; a page with more
            # interactive elements truncates it, so an id beyond the cap looked
            # like "not in map". Regenerate with room for everything and retry.
            _total = int(data.get("total") or 0)
            if _total > len(els):
                try:
                    _raw2 = await get_visual_navigation_map(tid, max_elements=max(_total, 1000))
                    _data2 = json.loads(_raw2)
                    els = _data2.get("elements") or []
                    el = next((e for e in els if e.get("id") == element_id), None)
                    data = _data2
                except Exception:
                    pass
        if not el:
            return json.dumps({"error": f"Element ID {element_id} not in current map "
                                       f"(map returned {len(els)} of {data.get('total')} elements)"})
        mods = modifiers or []
        if not el.get("in_viewport", True):
            # The map id is a position in the SCAN's element list, which can
            # include shadow-DOM and iframe elements and dedupes by geometry.
            # Re-deriving it with a querySelectorAll counter over the top
            # document targets the WRONG node as soon as those differ. Locate
            # by the map's own xpath instead (in the right frame), and only
            # fall back to the counter if that fails.
            _xp = el.get("xpath") or ""
            # Frame indices are only valid for the moment of the scan: frames
            # can be added, removed or reordered between scan and click. So the
            # recorded index is just a HINT — the frame is confirmed by actually
            # finding the element there, and every other frame is tried before
            # falling back. This makes a stale index unable to click the wrong
            # frame.
            _fi = el.get("frame_index")
            _cands = []
            if _fi is not None:
                _fl = list(page.frames)
                if 0 <= _fi < len(_fl):
                    _cands.append(_fl[_fi])
            for _f in page.frames:
                if _f not in _cands:
                    _cands.append(_f)
            _host = page
            _moved = False
            if _xp:
                for _f in _cands:
                    try:
                        if await _f.locator(f"xpath={_xp}").count() == 0:
                            continue
                        _loc = _f.locator(f"xpath={_xp}").first
                        await _loc.scroll_into_view_if_needed(timeout=5000)
                        await asyncio.sleep(0.15)
                        _bb = await _loc.bounding_box()
                        if _bb:
                            el["x"] = _bb["x"] + _bb["width"] / 2
                            el["y"] = _bb["y"] + _bb["height"] / 2
                            el["in_viewport"] = True
                            el["frame"] = "main" if _f == page.main_frame else (_f.url or "")[:120]
                            el["frame_index"] = list(page.frames).index(_f) if _f in page.frames else None
                            _host = _f
                            _moved = True
                            break
                    except Exception:
                        continue
            if not _moved:
                _host = page
            if not _moved:
                # Last resort only (no usable xpath). The filter and dedupe MUST
                # match get_visual_navigation_map's scan, or the counter does not
                # correspond to the map's element ids.
                js_scroll = "(id) => { const s='a,button,input,select,textarea,[role=button],[onclick],[tabindex]:not([tabindex=\"-1\"])'; const a=document.querySelectorAll(s); const seen=new Set(); let i=1; for (const n of a) { const r=n.getBoundingClientRect(); if (r.width<2||r.height<2) continue; const st=getComputedStyle(n); if (st.visibility==='hidden'||st.display==='none') continue; if (n.disabled || n.getAttribute('aria-disabled')==='true') continue; const k=n.tagName+':'+Math.round(r.x)+':'+Math.round(r.y); if (seen.has(k)) continue; seen.add(k); if (i===id) { n.scrollIntoView({block:'center',inline:'center',behavior:'instant'}); return true; } i++; } return false; }"
                await _host.evaluate(js_scroll, element_id)
                await asyncio.sleep(0.15)
                js_bbox = "(id) => { const s='a,button,input,select,textarea,[role=button],[onclick],[tabindex]:not([tabindex=\"-1\"])'; const a=document.querySelectorAll(s); const seen=new Set(); let i=1; for (const n of a) { const r=n.getBoundingClientRect(); if (r.width<2||r.height<2) continue; const st=getComputedStyle(n); if (st.visibility==='hidden'||st.display==='none') continue; if (n.disabled || n.getAttribute('aria-disabled')==='true') continue; const k=n.tagName+':'+Math.round(r.x)+':'+Math.round(r.y); if (seen.has(k)) continue; seen.add(k); if (i===id) return {x:r.left+r.width/2, y:r.top+r.height/2}; i++; } return null; }"
                fresh = await _host.evaluate(js_bbox, element_id)
                if fresh:
                    el["x"], el["y"] = fresh["x"], fresh["y"]
        x, y = el["x"], el["y"]
        for _m in mods:
            await page.keyboard.down(_m)
        try:
            await h_click(page, x, y, button=button, click_count=click_count)
        finally:
            for _m in mods:
                await page.keyboard.up(_m)
        return json.dumps({"status": "clicked", "element": el, "click_coords": {"x": round(x, 2), "y": round(y, 2)}, "button": button, "modifiers": mods})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def mouse_click_human_like(
    x: float,
    y: float,
    target_id: Optional[str] = None,
    button: str = "left",
    click_count: int = 1,
    modifiers: Optional[List[str]] = None,
) -> str:
    """Bézier mouse movement + click with button/modifiers support."""
    try:
        page, tid = await mgr.get_page(target_id)
        mods = modifiers or []
        for m in mods:
            await page.keyboard.down(m)
        await human_click(page, x, y)
        if click_count > 1:
            for _ in range(click_count - 1):
                await asyncio.sleep(0.05)
                await page.mouse.click(x, y, button=button)
        for m in mods:
            await page.keyboard.up(m)
        return json.dumps({"status": "clicked", "x": x, "y": y, "button": button, "click_count": click_count, "modifiers": mods, "target_id": tid})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def click_by_selector(
    selector: str,
    target_id: Optional[str] = None,
    button: str = "left",
    click_count: int = 1,
    modifiers: Optional[List[str]] = None,
    offset_x: int = 0,
    offset_y: int = 0,
    timeout_ms: int = 10000,
) -> str:
    """
    Click by CSS. 
    offset_x/offset_y allow clicking slightly off-center to bypass invisible honeypot overlays.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return json.dumps({"error": f"Selector not found: {selector}"})
        await loc.scroll_into_view_if_needed(timeout=timeout_ms)
        try:
            await loc.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            pass
        box = await loc.bounding_box()
        if not box:
            return json.dumps({"error": f"Not visible: {selector}"})
            
        # Calculate center + offset
        x = box["x"] + (box["width"] / 2) + offset_x
        y = box["y"] + (box["height"] / 2) + offset_y
        
        mods = modifiers or []
        for m in mods: await page.keyboard.down(m)
        await h_click(page, x, y, button=button, click_count=click_count)
        for m in mods: await page.keyboard.up(m)
        
        return json.dumps({"status": "clicked", "coords": {"x": round(x, 2), "y": round(y, 2)}, "offset_applied": bool(offset_x or offset_y)})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


# ----------------- SWARM: CONTEXT + TAB -----------------

@mcp.tool()
async def spawn_subagent_environment(
    url: str = "about:blank",
    engine: Optional[str] = None,  # Only "firefox" supported (Chromium removed)
    proxy_server: Optional[str] = None,
    user_agent: Optional[str] = None,
    viewport: Optional[Dict[str, int]] = None,
    locale: Optional[str] = None,
    timezone_id: Optional[str] = None,
    geolocation: Optional[Dict[str, float]] = None,
    storage_state_path: Optional[str] = None,
    cookies: Optional[List[Dict[str, Any]]] = None,
    extra_http_headers: Optional[Dict[str, str]] = None,
    bypass_csp: bool = False,
    emulate_device: Optional[str] = None,
    webgl_gpu: bool = False,
    device_scale_factor: Optional[float] = None,
    has_touch: Optional[bool] = None,
    touch_points: Optional[int] = None,
    oscpu: Optional[str] = None,
    platform: Optional[str] = None,
    languages: Optional[List[str]] = None,
) -> str:
    """
    Create an isolated browser context.

    emulate_device applies a DEVICE_PRESETS profile (iphone, iphone 13, ipad,
    pixel, samsung, desktop): UA + viewport + DPR + touch + oscpu + platform +
    locale + tz. Explicit arguments below override the preset.

    webgl_gpu: apply a WebGL vendor/renderer override (off by default).
    device_scale_factor / has_touch / touch_points / oscpu / platform /
    languages: raw fingerprint overrides.
    """
    try:
        if url and url.strip().lower() not in ("about:blank", "about:srcdoc"):
            check_url(url, "spawn_subagent_environment")
        if storage_state_path:
            storage_state_path = safe_path(storage_state_path, "spawn_subagent_environment", must_exist=True)
        # Handle emulate_device mapping (single source of truth: DEVICE_PRESETS)
        _dpr = None
        _tp = None
        _touch = False
        _oscpu = None
        _platform = None
        if emulate_device:
            dev = DEVICE_PRESETS.get(emulate_device.lower())
            if not dev:
                return json.dumps({
                    "status": "failed",
                    "error": f"Device '{emulate_device}' not supported. Use: {', '.join(sorted(DEVICE_PRESETS))}"
                })
            user_agent = user_agent or dev["ua"]
            viewport = viewport or dev["vp"]
            _dpr = dev.get("dpr")
            _tp = dev.get("tp")
            _touch = (dev.get("tp") or 0) > 0
            _oscpu = dev.get("oscpu")
            _platform = dev.get("platform")
            locale = locale or dev.get("locale")
            timezone_id = timezone_id or dev.get("tz")

        # has_touch must agree with navigator.maxTouchPoints. Playwright's
        # native has_touch installs ontouchstart/ontouchmove, but Firefox cannot
        # set maxTouchPoints itself, so `_spoof_script` does it — and it only
        # emits the getter when touch_points is not None. Passing
        # has_touch=True with touch_points=None therefore produced a window with
        # ontouchstart AND navigator.maxTouchPoints === 0, a classic automation
        # tell. Default the effective touch points to 1 in that case.
        _eff_touch = bool(_touch or has_touch)
        _eff_tp = _tp if _tp is not None else touch_points
        if _eff_touch and (_eff_tp is None or _eff_tp < 1):
            _eff_tp = 1

        proxy = {"server": proxy_server} if proxy_server else None
        ctx_id, ctx = await mgr.new_context(
            engine=engine, proxy=proxy, user_agent=user_agent, viewport=viewport,
            locale=locale, timezone_id=timezone_id, geolocation=geolocation,
            storage_state=storage_state_path, cookies=cookies,
            extra_http_headers=extra_http_headers, bypass_csp=bypass_csp,
            device_scale_factor=(_dpr if _dpr is not None else device_scale_factor),
            has_touch=_eff_touch,
            touch_points=_eff_tp,
            oscpu=(_oscpu if _oscpu is not None else oscpu),
            platform=(_platform if _platform is not None else platform),
            languages=languages,
            webgl_gpu=webgl_gpu,
        )
        
        # bypass_csp is now handled by Playwright's native option
        # (passed to mgr.new_context below via bypass_csp=True)
        # NO network interception, NO double requests, NO WAF trigger.
    except Exception as e:
        return json.dumps({"status": "failed", "error": f"Context creation failed: {str(e)[:300]}"})

    try:
        page = await ctx.new_page()
        tid = await mgr.register_page(page, ctx_id)
    except Exception as e:
        await mgr.destroy_context(ctx_id)
        return json.dumps({"status": "failed", "error": f"Page creation failed: {str(e)[:300]}"})

    # Report what the context ACTUALLY has, not what was requested: an unset
    # viewport becomes the manager default (1366x768), an unset oscpu/platform
    # is derived from the locale, and DPR/touch come from the init script.
    _eff = {}
    try:
        _eff = json.loads(await page.evaluate(
            "JSON.stringify({dpr: window.devicePixelRatio,"
            " tp: navigator.maxTouchPoints,"
            " langs: navigator.languages,"
            " ua: navigator.userAgent,"
            " oscpu: navigator.oscpu, platform: navigator.platform,"
            " touch: ('ontouchstart' in window)})"))
    except Exception:
        _eff = {}
    _vp = page.viewport_size or viewport or {"width": 1366, "height": 768}

    result = {
        "status": "spawned", "context_id": ctx_id, "target_id": tid, "url": url,
        "emulated_device": emulate_device,
        "viewport": _vp,
        "viewport_requested": viewport,
        "device_pixel_ratio": _eff.get("dpr", (_dpr if _dpr is not None else device_scale_factor)),
        # EFFECTIVE values, read back from the live page (the `_eff` dict);
        # fall back to the values we asked the manager for.
        "max_touch_points": _eff.get("tp", _eff_tp),
        "has_touch": bool(_eff.get("touch", _eff_touch)),
        "touch_points_effective": _eff_tp,
        "has_touch_effective": _eff_touch,
        "oscpu": _eff.get("oscpu") or (_oscpu if _oscpu is not None else oscpu),
        "platform": _eff.get("platform") or (_platform if _platform is not None else platform),
        "languages": _eff.get("langs") or languages,
        "user_agent": _eff.get("ua", user_agent),
        "webgl_gpu": webgl_gpu,
        "device_scale_factor_requested": (_dpr if _dpr is not None else device_scale_factor),
        # exactly what the CALLER passed (None counts as "not specified");
        # emulate_device-derived values are reported separately.
        "has_touch_requested": has_touch,
        "touch_points_requested": touch_points,
        "emulate_device_touch_points": _tp,
        "proxy": proxy_server,
    }
    if url and url != "about:blank":
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            result["url"] = page.url
        except Exception as e:
            result["nav_warning"] = str(e)[:200]
    return json.dumps(result)


@mcp.tool()
async def destroy_subagent_environment(
    context_id: str,
    force: bool = False,
) -> str:
    """
    Close context (all its tabs). force: cleanup even if state is weird.
    """
    try:
        existed = context_id in mgr._contexts
        ok = await mgr.destroy_context(context_id)
        return json.dumps({
            "status": "destroyed" if ok else "not_found",
            "context_id": context_id, "success": ok,
            "existed_before": existed,
        })
    except Exception as e:
        if force:
            try:
                mgr._contexts.pop(context_id, None)
            except Exception:
                pass
            return json.dumps({"status": "forced_destroy", "context_id": context_id, "success": False,
                               "error": str(e)[:200]})
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def list_browser_targets(
    include_thumbnails: bool = False,
    include_last_activity: bool = False,
) -> str:
    """
    List all open tabs.
    include_thumbnails: small screenshot per tab (~50KB each, slower)
    include_last_activity: last URL/title change time (from internal state, best-effort)
    """
    try:
        pages = mgr.list_pages()
        contexts = mgr.list_contexts()
        result_pages = {}
        for tid, info in pages.items():
            entry = dict(info)
            if include_last_activity:
                entry["last_activity"] = "unknown (playwright does not track)"
            if include_thumbnails:
                try:
                    pg = mgr._pages.get(tid)
                    if pg and not pg.is_closed():
                        path = f"/tmp/thumb_{tid}.png"
                        await pg.screenshot(path=path, full_page=False)
                        entry["thumbnail"] = path
                except Exception as e:
                    entry["thumbnail_error"] = str(e)[:100]
            result_pages[tid] = entry
        return json.dumps({"pages": result_pages, "page_count": len(result_pages), "contexts": contexts}, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def close_tab(
    target_id: str,
    force: bool = False,
) -> str:
    """
    Close tab. force: ignore errors (already-closed tabs).
    """
    try:
        existed = target_id in mgr._pages
        ok = await mgr.close_page(target_id)
        return json.dumps({
            "status": "closed" if ok else "not_found",
            "target_id": target_id, "success": ok,
            "existed_before": existed,
        })
    except Exception as e:
        if force:
            return json.dumps({"status": "forced_close", "target_id": target_id, "success": False,
                               "error": str(e)[:200]})
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


# ----------------- NETWORK CAPTURE -----------------

@mcp.tool()
async def capture_network_traffic(
    url: Optional[str] = None,
    duration_seconds: float = 5.0,
    target_id: Optional[str] = None,
    include_bodies: bool = False,
    url_filter: Optional[str] = None,
    resource_types: Optional[List[str]] = None,
    status_filter: Optional[List[int]] = None,
    capture_request_headers: bool = False,
    capture_response_headers: bool = False,
    adaptive_duration: bool = False,
    stabilize_ms: int = 500,
    extract_secrets: bool = False,
) -> str:
    """
    Capture network.
    extract_secrets=True automatically finds JWT tokens, API keys, and Auth headers!
    resource_types: keep only these request types (e.g. ["xhr","fetch","document"]).
    status_filter: keep only these response statuses (e.g. [200, 302, 403]).
    include_bodies: attach up to 2000 chars of each response body.
    adaptive_duration: stop early after `stabilize_ms` with no new events
      (still capped at `duration_seconds`).
    """
    import re as _re
    try:
        page, tid = await mgr.get_page(target_id)
        events = []
        secrets_found = []

        _rt = {str(t).lower() for t in resource_types} if resource_types else None
        _st = set(status_filter) if status_filter else None

        def _type_ok(rt):
            return (not _rt) or (str(rt).lower() in _rt)
        
        secret_patterns = {
            "jwt": _re.compile(r'eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*'),
            "api_key": _re.compile(r'(?i)(api[_-]?key|apikey)[^a-zA-Z0-9]{1,5}([a-zA-Z0-9]{16,})'),
            "bearer": _re.compile(r'(?i)bearer\s+([a-zA-Z0-9\-._~+/]+=*)')
        }

        def scan_secrets(text, url):
            if not text: return
            for name, pattern in secret_patterns.items():
                matches = pattern.findall(text)
                for m in matches:
                    val = m if isinstance(m, str) else m[-1]
                    secrets_found.append({"type": name, "url": url[:100], "value": val[:100]})

        _body_tasks = []
        def on_request(req):
            if url_filter and not _re.search(url_filter, req.url): return
            if not _type_ok(req.resource_type): return
            ev = {"type": "request", "method": req.method, "url": req.url}
            if capture_request_headers:
                ev["headers"] = dict(req.headers)
                if extract_secrets:
                    scan_secrets(str(req.headers), req.url)
                    scan_secrets(req.post_data, req.url)
            events.append(ev)

        def on_response(resp):
            if url_filter and not _re.search(url_filter, resp.url): return
            if _st and resp.status not in _st: return
            if not _type_ok(resp.request.resource_type): return
            ev = {"type": "response", "status": resp.status, "url": resp.url}
            if capture_response_headers:
                ev["headers"] = dict(resp.headers)
                if extract_secrets:
                    scan_secrets(str(resp.headers), resp.url)
            events.append(ev)
            if include_bodies:
                async def _grab(r=resp, e=ev):
                    try:
                        e["body"] = (await r.text())[:2000]
                    except Exception:
                        e["body_error"] = True
                _body_tasks.append(asyncio.create_task(_grab()))

        page.on("request", on_request)
        page.on("response", on_response)
        try:
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            if adaptive_duration:
                # Keep watching until `duration_seconds` elapses, but stop early
                # after `stabilize_ms` with no new events.
                _deadline = time.time() + duration_seconds
                _last = len(events)
                _quiet_since = time.time()
                while True:
                    _remain = _deadline - time.time()
                    if _remain <= 0:
                        break
                    await asyncio.sleep(min(stabilize_ms / 1000.0, _remain))
                    if len(events) != _last:
                        _last = len(events)
                        _quiet_since = time.time()
                    elif (time.time() - _quiet_since) >= stabilize_ms / 1000.0:
                        break
            else:
                await asyncio.sleep(duration_seconds)
        finally:
            # Bodies must be gathered BEFORE the listeners come off, or the
            # last responses are missed; listeners must come off even on error.
            if _body_tasks:
                await asyncio.gather(*_body_tasks, return_exceptions=True)
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)

        return _dumps_capped({
            "count": len(events),
            "secrets_found": secrets_found if extract_secrets else None,
            "events": events[:100]
        }, 200000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:500]})


# ----------------- WCAG AUDIT -----------------

@mcp.tool()
async def run_wcag_accessibility_audit(
    target_id: Optional[str] = None,
    wcag_level: str = "AA",
    include_passes: bool = False,
    include_incomplete: bool = False,
    remediation_hints: bool = True,
    save_to_file: Optional[str] = None,
) -> str:
    """
    WCAG audit via axe-core.
    wcag_level: "A" | "AA" | "AAA"
    include_passes: also report passed rules
    include_incomplete: items axe couldn't determine
    remediation_hints: include fix guidance per violation
    save_to_file: write markdown report to path
    """
    try:
        page, tid = await mgr.get_page(target_id)
        level = wcag_level.upper() if wcag_level.upper() in ("A", "AA", "AAA") else "AA"

        # Prefer the vendored copy: injecting a third-party CDN script into the
        # target's origin leaks the target URL/Referer to that CDN.
        engine = "axe-core"
        note = None
        loaded = False
        try:
            _axe_local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "vendor", "axe.min.js")
            if os.path.exists(_axe_local):
                await page.add_script_tag(path=_axe_local)
                loaded = bool(await page.evaluate("typeof axe !== 'undefined'"))
        except Exception:
            loaded = False
        if not loaded:
            loaded = await page.evaluate("""
            () => new Promise((resolve) => {
                if (typeof axe !== 'undefined') return resolve(true);
                const s = document.createElement('script');
                s.src = 'https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.2/axe.min.js';
                s.onload = () => resolve(true);
                s.onerror = () => resolve(false);
                document.head.appendChild(s);
                setTimeout(() => resolve(typeof axe !== 'undefined'), 8000);
            })
            """)
        if not loaded:
            # axe-core CDN blocked (CSP/network) — fall back to built-in checks so
            # the tool still returns actionable data on CSP-hardened targets.
            engine = "netwala-basic-fallback"
            note = ("axe-core CDN could not be loaded (CSP or network); built-in "
                    "basic WCAG checks were used instead of full axe-core.")
            result = await page.evaluate("""
            () => {
                const V = [];
                const add = (id, impact, help, nodes) => V.push({
                    id, impact, help,
                    helpUrl: 'https://www.w3.org/WAI/WCAG21/Understanding/' + id + '.html',
                    nodes: nodes.length,
                    sample: (nodes[0] && nodes[0].outerHTML) ? String(nodes[0].outerHTML).slice(0, 100) : null,
                });
                const q = (sel) => Array.from(document.querySelectorAll(sel));
                if (!document.documentElement.lang) add('html-has-lang', 'serious', '<html> must have a lang attribute', [document.documentElement]);
                if (!document.title) add('document-title', 'serious', 'Page must have a title', [document.documentElement]);
                const imgs = q('img').filter(i => !i.hasAttribute('alt'));
                if (imgs.length) add('image-alt', 'critical', 'Images must have alternate text', imgs);
                const fields = q('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]), select, textarea');
                const unlabelled = fields.filter(el => {
                    const byId = el.id && document.querySelector('label[for="' + el.id + '"]');
                    const wrapped = el.closest('label');
                    return !byId && !wrapped && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby') && !el.getAttribute('title');
                });
                if (unlabelled.length) add('label', 'critical', 'Form elements must have labels', unlabelled);
                const anon = q('button, a').filter(el => !(el.textContent || '').trim() && !el.getAttribute('aria-label') && !el.getAttribute('title'));
                if (anon.length) add('button-name', 'critical', 'Buttons and links must have discernible text', anon);
                const ifr = q('iframe').filter(f => !f.getAttribute('title'));
                if (ifr.length) add('frame-title', 'serious', 'iframes must have a title', ifr);
                if (!document.querySelector('h1')) add('page-has-heading-one', 'moderate', 'Page should contain a level-one heading', [document.body]);
                const dup = [];
                const seen = {};
                q('[id]').forEach(el => { if (seen[el.id]) dup.push(el); seen[el.id] = 1; });
                if (dup.length) add('duplicate-id', 'moderate', 'id attribute values must be unique', dup);
                const badlang = q('[lang]').filter(el => !String(el.lang || '').trim());
                if (badlang.length) add('valid-lang', 'serious', 'lang attribute must have a valid value', badlang);
                return { violations: V, passes: [], incomplete: [] };
            }
            """)
        else:
            tags = ["wcag2a", "wcag21a"]
            if level in ("AA", "AAA"):
                tags += ["wcag2aa", "wcag21aa"]
            if level == "AAA":
                tags += ["wcag2aaa"]
            tags_json = json.dumps(tags)

            result = await page.evaluate(f"""
            async () => {{
                const r = await axe.run({{ runOnly: {{ type: 'tag', values: {tags_json} }} }});
                return {{
                    violations: r.violations.map(v => ({{
                        id: v.id, impact: v.impact, help: v.help,
                        helpUrl: v.helpUrl, nodes: v.nodes.length,
                        sample: v.nodes[0]?.html?.slice(0, 100) || null,
                    }})),
                    passes: r.passes.map(p => ({{ id: p.id, nodes: p.nodes.length }})),
                    incomplete: r.incomplete.map(i => ({{ id: i.id, impact: i.impact, nodes: i.nodes.length }})),
                }};
            }}
            """)

        out = {
            "wcag_level": level,
            "engine": engine,
            "target_id": tid,
            "url": page.url,
            "violations": result.get("violations", []),
            "violation_count": len(result.get("violations", [])),
        }
        if note:
            out["note"] = note
        if include_passes:
            out["passes"] = result.get("passes", [])
        if include_incomplete:
            out["incomplete"] = result.get("incomplete", [])

        if not remediation_hints:
            for v in out.get("violations", []):
                for _k in ("help", "helpUrl", "sample", "remediation"):
                    v.pop(_k, None)
            out["remediation_hints"] = False

        if save_to_file and out["violations"]:
            try:
                save_to_file = safe_path(save_to_file, "run_wcag_accessibility_audit")
                lines = [f"# WCAG {level} Audit — {page.url}", ""]
                lines.append(f"**Engine:** {engine}")
                if note:
                    lines.append(f"**Note:** {note}")
                lines.append(f"**Violations:** {out['violation_count']}")
                lines.append("")
                for v in out["violations"]:
                    lines.append(f"## [{(v.get('impact') or 'unknown').upper()}] {v.get('id')} — {v.get('help')}")
                    lines.append(f"- **Affected nodes:** {v.get('nodes')}")
                    lines.append(f"- **Help:** {v.get('helpUrl')}")
                    if v.get("sample"):
                        lines.append(f"- **Sample:** `{v['sample']}`")
                    lines.append("")
                with open(save_to_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                out["report_path"] = save_to_file
            except Exception as e:
                out["report_error"] = str(e)[:200]

        return _dumps_capped(out, 50000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


# ----------------- BATCH -----------------

@mcp.tool()
async def batch_execute_across_agents(
    tasks: List[Dict[str, Any]],
    concurrency_limit: int = 5,
    retry_failed: int = 1,
    timeout_per_task_ms: int = 60000,
) -> str:
    """
    Parallel JS execution with concurrency limit + retries.
    Each task: {target_id, js_code}. Failed tasks retried N times.
    """
    import time as _t
    sem = asyncio.Semaphore(max(1, concurrency_limit))

    async def run_one(task, idx):
        t_id = task.get("target_id")
        js = task.get("js_code", "")
        async with sem:
            last_err = None
            start = _t.time()
            for attempt in range(retry_failed + 1):
                try:
                    page, _ = await mgr.get_page(t_id)
                    res = await asyncio.wait_for(
                        page.evaluate(js),
                        timeout=timeout_per_task_ms / 1000.0,
                    )
                    return {"idx": idx, "target_id": t_id, "ok": True,
                            "result": res,
                            "duration_ms": int((_t.time() - start) * 1000),
                            "attempts": attempt + 1}
                except Exception as e:
                    last_err = str(e)[:200]
                    if attempt < retry_failed:
                        await asyncio.sleep(0.5)
            return {"idx": idx, "target_id": t_id, "ok": False,
                    "error": last_err,
                    "duration_ms": int((_t.time() - start) * 1000),
                    "attempts": retry_failed + 1}

    results = await asyncio.gather(
        *[run_one(t, i) for i, t in enumerate(tasks)],
        return_exceptions=True,
    )
    cleaned = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            cleaned.append({"idx": i, "ok": False, "error": str(r)[:200]})
        else:
            cleaned.append(r)
    return json.dumps({
        "count": len(cleaned),
        "succeeded": sum(1 for r in cleaned if r.get("ok")),
        "failed": sum(1 for r in cleaned if not r.get("ok")),
        "concurrency_limit": concurrency_limit,
        "results": cleaned,
    }, indent=2)


@mcp.tool()
async def wait_for_selector(
    selector: Optional[str] = None,
    timeout_ms: int = 30000,
    state: str = "attached",
    not_present: bool = False,
    any_of: Optional[List[str]] = None,
    return_element_info: bool = False,
    target_id: Optional[str] = None,
) -> str:
    """
    Wait for selector(s).
    any_of: list, returns on first match. not_present: wait to disappear.
    return_element_info: include bbox/text/tag/attributes.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        if any_of:
            import time as _t
            start = _t.time()
            while (_t.time() - start) * 1000 < timeout_ms:
                for sel in any_of:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            info = {"matched": sel, "status": "found"}
                            if return_element_info:
                                try:
                                    info["bbox"] = await loc.bounding_box()
                                    info["text"] = (await loc.inner_text())[:100]
                                    info["tag"] = await loc.evaluate("el => el.tagName.toLowerCase()")
                                except Exception:
                                    pass
                            return json.dumps(info)
                    except Exception:
                        continue
                await asyncio.sleep(0.2)
            return json.dumps({"error": "any_of timeout", "tried": any_of})
        if not selector:
            return json.dumps({"error": "either selector or any_of required"})
        if not_present:
            # First check if element ever existed
            count = await page.locator(selector).count()
            if count == 0:
                return json.dumps({
                    "status": "never_existed",
                    "selector": selector,
                    "warning": "Element was never attached — cannot confirm removal. "
                               "If you expected it to exist, the selector may be wrong."
                })
            await page.wait_for_selector(selector, timeout=timeout_ms, state="detached")
            return json.dumps({"status": "gone", "selector": selector})
        await page.wait_for_selector(selector, timeout=timeout_ms, state=state)
        result = {"status": "found", "selector": selector, "state": state}
        if return_element_info:
            loc = page.locator(selector).first
            try:
                result["bbox"] = await loc.bounding_box()
                result["text"] = (await loc.inner_text())[:100]
                result["tag"] = await loc.evaluate("el => el.tagName.toLowerCase()")
            except Exception:
                pass
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def wait_for_navigation(
    timeout_ms: int = 30000,
    wait_until: str = "domcontentloaded",
    target_id: Optional[str] = None,
) -> str:
    """
    Wait for URL to change. Call BEFORE clicking link/submit.
    wait_until: load | domcontentloaded | networkidle | commit
    """
    try:
        page, tid = await mgr.get_page(target_id)
        old_url = page.url
        try:
            await page.wait_for_url(
                lambda url: url != old_url,
                timeout=timeout_ms,
                wait_until=wait_until,
            )
            return json.dumps({"status": "navigated", "old_url": old_url, "new_url": page.url, "target_id": tid})
        except Exception:
            return json.dumps({"status": "no_navigation", "current_url": page.url, "old_url": old_url, "target_id": tid})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def wait_for_network_idle(
    timeout_ms: int = 30000,
    wait_until: str = "networkidle",
    target_id: Optional[str] = None,
) -> str:
    """
    Wait for page load state.
    wait_until: networkidle (default, quiet) | load | domcontentloaded
    """
    try:
        page, tid = await mgr.get_page(target_id)
        await page.wait_for_load_state(wait_until, timeout=timeout_ms)
        return json.dumps({"status": wait_until, "url": page.url, "target_id": tid})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300],
                           "note": "timeout or invalid wait_until"})


@mcp.tool()
async def press_keys(
    keys: Union[str, List[str]],
    delay_between_ms: int = 0,
    target_id: Optional[str] = None,
) -> str:
    """
    Press keys. keys: str ("Enter", "Control+a") OR list (["Control","a"]).
    delay_between_ms: delay between multi-key sequence.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        if isinstance(keys, list):
            for k in keys:
                await page.keyboard.press(str(k))
                if delay_between_ms > 0:
                    await asyncio.sleep(delay_between_ms / 1000.0)
            return json.dumps({"status": "pressed_sequence", "keys": keys, "count": len(keys)})
        await page.keyboard.press(str(keys))
        return json.dumps({"status": "pressed", "keys": keys})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def type_text(
    selector: str,
    text: str,
    delay_ms: int = 30,
    humanize: bool = False,
    paste_mode: bool = False,
    clear_first: bool = True,
    type_and_enter: bool = False,
    framework_bypass: bool = False,
    target_id: Optional[str] = None,
) -> str:
    """
    Type text. framework_bypass=True forces React/Angular to register the input
    by dispatching native 'input' and 'change' events (solves the "typed but didn't register" bug).
    """
    try:
        page, tid = await mgr.get_page(target_id)
        loc = page.locator(selector).first
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.focus()
        
        if clear_first:
            try:
                await loc.fill("")
            except Exception:
                pass
                
        if framework_bypass:
            js = """
            ([sel, txt]) => {
                const el = document.querySelector(sel);
                if(!el) return false;
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                const nativeTextAreaValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
                if (el.tagName === 'INPUT') nativeInputValueSetter.call(el, txt);
                else if (el.tagName === 'TEXTAREA') nativeTextAreaValueSetter.call(el, txt);
                else el.value = txt;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
            """
            await page.evaluate(js, [selector, text])
        elif paste_mode:
            await loc.fill(text)
        elif humanize:
            import random as _rand
            for ch in text:
                await page.keyboard.type(ch, delay=0)
                await asyncio.sleep(_rand.uniform(0.05, 0.15))
                if _rand.random() < 0.03:
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(_rand.uniform(0.08, 0.2))
                    await page.keyboard.type(ch, delay=0)
        else:
            await h_type(page, text, delay_ms)
            
        if type_and_enter:
            await asyncio.sleep(0.1)
            await page.keyboard.press("Enter")
            
        return json.dumps({"status": "typed", "selector": selector, "length": len(text), "framework_bypass": framework_bypass})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def select_option(
    selector: str,
    value: Optional[str] = None,
    label: Optional[str] = None,
    index: Optional[int] = None,
    values: Optional[List[str]] = None,
    target_id: Optional[str] = None,
) -> str:
    """
    Select <select> option. Use ONE of: value, label, index, or values (multi-select).
    """
    try:
        page, tid = await mgr.get_page(target_id)
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return json.dumps({"error": f"Selector not found: {selector}"})
        if values:
            selected = await loc.select_option(values)
            return json.dumps({"status": "selected", "selector": selector,
                               "mode": "multi", "values": values, "selected": selected})
        if label is not None:
            selected = await loc.select_option(label=label)
            return json.dumps({"status": "selected", "selector": selector,
                               "mode": "label", "label": label, "selected": selected})
        if index is not None:
            selected = await loc.select_option(index=index)
            return json.dumps({"status": "selected", "selector": selector,
                               "mode": "index", "index": index, "selected": selected})
        if value is not None:
            selected = await loc.select_option(value=value)
            return json.dumps({"status": "selected", "selector": selector,
                               "mode": "value", "value": value, "selected": selected})
        return json.dumps({"error": "one of value, label, index, values required"})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def click_by_text(
    text: str,
    exact: bool = False,
    target_id: Optional[str] = None,
    button: str = "left",
    click_count: int = 1,
    modifiers: Optional[List[str]] = None,
    force: bool = False,
) -> str:
    """Click by visible text with button/modifiers support."""
    try:
        page, tid = await mgr.get_page(target_id)
        loc = page.get_by_text(text, exact=exact).first
        mods = modifiers or []
        if force:
            await loc.click(button=button, click_count=click_count, modifiers=mods, force=True)
            return json.dumps({"status": "clicked", "text": text, "forced": True})
        await loc.scroll_into_view_if_needed(timeout=5000)
        box = await loc.bounding_box()
        if not box:
            return json.dumps({"error": f"Not visible: {text}"})
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        for _m in mods:
            await page.keyboard.down(_m)
        try:
            await h_click(page, x, y, button=button, click_count=click_count)
        finally:
            for _m in mods:
                await page.keyboard.up(_m)
        return json.dumps({"status": "clicked", "text": text, "coords": {"x": round(x, 2), "y": round(y, 2)}, "button": button, "modifiers": mods, "target_id": tid})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def scroll_page(
    direction: str = "down",
    pixels: int = 500,
    to: Optional[str] = None,
    to_element: Optional[str] = None,
    wait_after_ms: int = 200,
    target_id: Optional[str] = None,
) -> str:
    """
    Scroll window.
    direction: "down" | "up" (pixels used)
    to: "top" | "bottom" (overrides direction, jumps)
    to_element: CSS selector to scroll into view (overrides other params)
    wait_after_ms: wait after scroll for lazy-load
    """
    try:
        page, tid = await mgr.get_page(target_id)
        if to_element:
            loc = page.locator(to_element).first
            if await loc.count() == 0:
                return json.dumps({"error": f"Not found: {to_element}"})
            await loc.scroll_into_view_if_needed(timeout=5000)
            if wait_after_ms > 0:
                await page.wait_for_timeout(wait_after_ms)
            return json.dumps({"status": "scrolled_to_element", "selector": to_element})
        if to == "top":
            await page.evaluate("window.scrollTo(0, 0)")
            if wait_after_ms > 0:
                await page.wait_for_timeout(wait_after_ms)
            return json.dumps({"status": "scrolled", "to": "top"})
        if to == "bottom":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            if wait_after_ms > 0:
                await page.wait_for_timeout(wait_after_ms)
            return json.dumps({"status": "scrolled", "to": "bottom"})
        dy = pixels if direction.lower() == "down" else -pixels
        await h_scroll(page, dy)
        if wait_after_ms > 0:
            await page.wait_for_timeout(wait_after_ms)
        pos = await page.evaluate("({x: window.scrollX, y: window.scrollY})")
        return json.dumps({"status": "scrolled", "direction": direction, "pixels": pixels, "position": pos})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def get_cookies(
    target_id: Optional[str] = None,
    domain_filter: Optional[str] = None,
    http_only_only: bool = False,
    secure_only: bool = False,
) -> str:
    """Get cookies with optional filters. domain_filter: regex on domain."""
    try:
        page, tid = await mgr.get_page(target_id)
        cookies = await page.context.cookies()
        if domain_filter:
            import re as _re
            cookies = [c for c in cookies if _re.search(domain_filter, c.get("domain", ""))]
        if http_only_only:
            cookies = [c for c in cookies if c.get("httpOnly")]
        if secure_only:
            cookies = [c for c in cookies if c.get("secure")]
        return json.dumps({"count": len(cookies), "target_id": tid, "cookies": cookies})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def set_cookies(
    cookies: List[Dict[str, Any]],
    target_id: Optional[str] = None,
    clear_first: bool = False,
) -> str:
    """Set cookies. Each: {name, value, url or domain+path}. clear_first: wipe existing."""
    try:
        page, tid = await mgr.get_page(target_id)
        if clear_first:
            await page.context.clear_cookies()
        await page.context.add_cookies(cookies)
        return json.dumps({"status": "set", "count": len(cookies), "cleared_first": clear_first})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def save_storage_state(
    path: Optional[str] = None,
    target_id: Optional[str] = None,
    encrypt: bool = False,
) -> str:
    """
    Save full session (cookies + localStorage). Path optional (auto-generated).
    encrypt: Fernet-encrypt the state with NETWALA_KEY (requires `cryptography`).
    """
    try:
        path = safe_path(path, "save_storage_state") if path else path
        page, tid = await mgr.get_page(target_id)
        path = path or f"/tmp/storage_{uuid.uuid4().hex[:8]}.json"
        await page.context.storage_state(path=path)
        if encrypt:
            import os as _os
            from pathlib import Path as _P
            _key = _os.environ.get("NETWALA_KEY", "")
            if not _key:
                return json.dumps({
                    "error": "NETWALA_KEY env var required for encrypt=True",
                    "hint": "Run: export NETWALA_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
                })
            try:
                from cryptography.fernet import Fernet
            except ImportError:
                return json.dumps({
                    "error": "cryptography not installed",
                    "install": "pip install cryptography --break-system-packages"
                })
            f_key = Fernet(_key.encode() if isinstance(_key, str) else _key)
            with open(path, "rb") as f:
                plaintext = f.read()
            encrypted = f_key.encrypt(plaintext)
            with open(path, "wb") as f:
                f.write(encrypted)
            import stat as _stat
            _P(path).chmod(0o600)
            return json.dumps({"status": "saved", "path": path, "encrypted": True, "method": "fernet", "bytes": len(encrypted)})
        return json.dumps({"status": "saved", "path": path, "encrypted": False, "bytes": os.path.getsize(path)})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def load_storage_state(
    path: str,
    url: str = "about:blank",
    decrypt: bool = False,
    engine: Optional[str] = None,  # Only "firefox" supported (Chromium removed)
    proxy_server: Optional[str] = None,
) -> str:
    """Spawn new context pre-loaded with saved session. decrypt: reverse of save's encrypt."""
    try:
        path = safe_path(path, "load_storage_state", must_exist=True)
        if url and url.strip().lower() not in ("about:blank", "about:srcdoc"):
            check_url(url, "load_storage_state")
        if not os.path.exists(path):
            return json.dumps({"error": f"File not found: {path}"})
        state_path = path
        if decrypt:
            import os as _os
            from pathlib import Path as _P
            _key = _os.environ.get("NETWALA_KEY", "")
            if not _key:
                return json.dumps({"error": "NETWALA_KEY env var required for decrypt=True"})
            try:
                from cryptography.fernet import Fernet
            except ImportError:
                return json.dumps({"error": "cryptography not installed",
                                   "install": "pip install cryptography --break-system-packages"})
            f_key = Fernet(_key.encode() if isinstance(_key, str) else _key)
            with open(path, "rb") as f:
                ciphertext = f.read()
            try:
                plaintext = f_key.decrypt(ciphertext)
            except Exception as e:
                return json.dumps({"error": f"Decryption failed (wrong key?): {str(e)[:200]}"})
            state_path = f"/tmp/storage_dec_{uuid.uuid4().hex[:8]}.json"
            with open(state_path, "wb") as f:
                f.write(plaintext)
            _P(state_path).chmod(0o600)
        proxy = {"server": proxy_server} if proxy_server else None
        ctx_id, ctx = await mgr.new_context(
            engine=engine,
            proxy=proxy,
            storage_state=state_path,
        )
        page = await ctx.new_page()
        if url and url != "about:blank":
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
        tid = await mgr.register_page(page, ctx_id)
        return json.dumps({
            "status": "loaded", "path": path,
            "context_id": ctx_id, "target_id": tid,
            "url": page.url, "decrypted": decrypt,
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def handle_next_dialog(
    action: str = "accept",
    prompt_text: str = "",
    capture_content: bool = True,
    target_id: Optional[str] = None,
) -> str:
    """Handle next dialog and RETURN its text."""
    try:
        page, tid = await mgr.get_page(target_id)
        captured_msg = []
        async def on_dialog(d):
            if capture_content:
                captured_msg.append({"type": d.type, "message": d.message})
            try:
                if action.lower() == "accept": await d.accept(prompt_text or None)
                else: await d.dismiss()
            except Exception: pass
        page.once("dialog", on_dialog)
        await asyncio.sleep(0.5)
        return json.dumps({"status": "handled", "action": action, "captured_dialogs": captured_msg})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})

@mcp.tool()
async def observe(
    include_screenshot: bool = True,
    include_console_logs: bool = False,
    include_network_errors: bool = False,
    include_perf_metrics: bool = False,
    max_elements: int = 100,
    target_id: Optional[str] = None,
) -> str:
    """
    One-call observation: screenshot + ARIA + visual map + optional diagnostics.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        console_logs = []
        net_errors = []
        _on_console = None
        _on_response = None
        if include_console_logs:
            def _on_console(msg):
                try:
                    console_logs.append({"type": msg.type, "text": msg.text[:200]})
                except Exception:
                    pass
            page.on("console", _on_console)
        if include_network_errors:
            def _on_response(resp):
                try:
                    if resp.status >= 400:
                        net_errors.append({"status": resp.status, "url": resp.url[:200]})
                except Exception:
                    pass
            page.on("response", _on_response)

        result = {
            "target_id": tid,
            "url": page.url,
            "title": await page.title(),
        }
        if include_screenshot:
            path = f"/tmp/observe_{uuid.uuid4().hex[:8]}.png"
            await page.screenshot(path=path, full_page=True)
            result["screenshot"] = path
        result["aria_snapshot"] = await page.locator("body").aria_snapshot()
        elements = await page.evaluate("""
        (() => {
            const sel = 'a,button,input,select,textarea,[role=button],[onclick],[tabindex]:not([tabindex="-1"])';
            const out = []; const seen = new Set(); let idx = 1;
            for (const el of document.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                const s = getComputedStyle(el);
                if (s.visibility === 'hidden' || s.display === 'none') continue;
                const key = el.tagName + ':' + r.x + ':' + r.y + ':' + r.width + ':' + r.height;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({
                    id: idx++,
                    tag: el.tagName.toLowerCase(),
                    text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80),
                    x: Math.round(r.left + r.width / 2),
                    y: Math.round(r.top + r.height / 2),
                    in_viewport: r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth,
                });
            }
            return out;
        })()
        """)
        if len(elements) > max_elements:
            result["elements"] = elements[:max_elements]
            result["element_truncated"] = True
        else:
            result["elements"] = elements
        result["element_count"] = len(elements)
        if include_console_logs or include_network_errors:
            await page.wait_for_timeout(300)
            try:
                if _on_console: page.remove_listener("console", _on_console)
                if _on_response: page.remove_listener("response", _on_response)
            except Exception:
                pass
        if include_console_logs:
            result["console_logs"] = console_logs[-20:]
        if include_network_errors:
            result["network_errors"] = net_errors[-20:]
        if include_perf_metrics:
            perf = await page.evaluate("""
            (() => {
                const t = performance.timing;
                const entries = performance.getEntriesByType('resource');
                return {
                    dom_content_loaded_ms: t.domContentLoadedEventEnd - t.navigationStart,
                    load_event_ms: t.loadEventEnd - t.navigationStart,
                    resource_count: entries.length,
                };
            })()
            """)
            result["perf"] = perf
        return _dumps_capped(result, 12000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})




# ═══════════════════════════════════════════════════════════════════
# AUTO-WRAP: applies logging + structured errors to all registered tools
# ═══════════════════════════════════════════════════════════════════
def _apply_cross_cutting_wrappers():
    """Wrap all mcp tools with logging/retry/struct-errors. Idempotent."""
    try:
        tools_registry = getattr(mcp, "_tool_manager", None)
        if not tools_registry or not hasattr(tools_registry, "_tools"):
            return
        for name, tool_obj in list(tools_registry._tools.items()):
            fn = getattr(tool_obj, "fn", None)
            if fn is None:
                continue
            if getattr(fn, "_crosswrapped", False):
                continue
            wrapped = _wrap_tool(fn, name)
            wrapped._crosswrapped = True
            tool_obj.fn = wrapped
            # Also update module-level global so direct `from netwala import X` gets wrapped
            if name in globals() and not getattr(globals()[name], "_crosswrapped", False):
                globals()[name] = wrapped
    except Exception:
        pass


@mcp.tool()
async def intercept_request(
    url_pattern: str,
    method: Optional[str] = None,
    headers_modify: Optional[Dict[str, str]] = None,
    body_override: Optional[str] = None,
    drop_request: bool = False,
    delay_ms: int = 0,
    response_modify_body: Optional[str] = None,
    response_replace_regex: Optional[str] = None,
    target_id: Optional[str] = None,
) -> str:
    """
    Advanced Request/Response Interceptor (Burp Suite Pro style).
    - response_modify_body: Fully replace the server's response (great for XSS testing).
    - delay_ms: Delay the response to test Race Conditions.
    - response_replace_regex: REGEX substitution on the response body. Format
      "pattern==>replacement" (or legacy "pattern|replacement", split on the
      first separator); the pattern is compiled and applied with re.sub, so it
      is NOT a literal replace.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        import re as _re

        # By default, treat url_pattern as literal (escape regex chars)
        # This prevents URLs with ? / ( ) from breaking
        _pattern = url_pattern if url_pattern.startswith("^") or url_pattern.startswith(".*") else _re.escape(url_pattern)
        try:
            _rx = _re.compile(_pattern)
        except _re.error as _e:
            return json.dumps({
                "status": "error",
                "error": f"Invalid url_pattern regex: {_e}",
                "hint": ("A literal URL is escaped automatically; prefix a regex "
                         "with ^ or .* if you really want one."),
            })

        # Validate the replacement spec at ARM time. Raising inside the route
        # handler makes Playwright fail the request (observed: a 60s goto
        # timeout), which is far worse than refusing to arm.
        _sep = None
        if response_replace_regex:
            _sep = "==>" if "==>" in response_replace_regex else (
                "|" if "|" in response_replace_regex else None)
            if _sep is None:
                return json.dumps({
                    "status": "error",
                    "error": ("response_replace_regex must be 'pattern==>replacement' "
                              "(or legacy 'pattern|replacement')"),
                })
            try:
                _re.compile(response_replace_regex.split(_sep, 1)[0])
            except _re.error as _pe:
                return json.dumps({
                    "status": "error",
                    "error": f"Invalid response_replace_regex pattern: {_pe}",
                })

        async def handler(route):
            req = route.request
            if method and req.method.upper() != method.upper():
                await route.continue_()
                return
            if not _re.search(_pattern, req.url):
                await route.continue_()
                return
                
            if drop_request:
                await route.abort()
                return

            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)

            # Fetch the real response if we need to modify it.
            # route.fetch() can fail (upstream timeout / connection reset); an
            # unhandled throw here fails the request, so fall back to serving
            # the modified body directly.
            if response_modify_body is not None or response_replace_regex:
                resp = None
                body = ""
                try:
                    resp = await route.fetch()
                    body = await resp.text()
                except Exception:
                    resp = None
                    body = ""
                
                if response_replace_regex:
                    # Format: "pattern==>replacement" (preferred) or legacy
                    # "pattern|replacement". Split on the FIRST separator so a
                    # pattern containing '|' survives; the left side is a REGEX
                    # (re.sub), matching the parameter name.
                    if "==>" in response_replace_regex:
                        parts = response_replace_regex.split("==>", 1)
                    else:
                        parts = response_replace_regex.split("|", 1)
                    if len(parts) != 2:
                        raise ValueError(
                            "response_replace_regex must be 'pattern==>replacement'")
                    try:
                        body = _re.sub(parts[0], parts[1], body)
                    except _re.error as _re_err:
                        raise ValueError(f"Invalid response_replace_regex: {_re_err}")
                
                if response_modify_body is not None:
                    body = response_modify_body

                if resp is not None:
                    await route.fulfill(response=resp, body=body)
                else:
                    # upstream unreachable: preserve the intent, drop the headers
                    await route.fulfill(status=200, body=body,
                                        content_type="text/html; charset=utf-8")
                return

            # Otherwise just modify request and continue
            new_headers = dict(req.headers)
            if headers_modify:
                new_headers.update(headers_modify)
            # `is not None`, not truthiness: body_override="" must clear the
            # body rather than silently reuse the original post data.
            new_body = body_override if body_override is not None else req.post_data

            await route.continue_(headers=new_headers, post_data=new_body)

        # Track our own handlers so stop_intercept removes only these and the
        # route table does not accumulate duplicates across repeated arming.
        # Kept on the manager (keyed by tab id) rather than on the Playwright
        # Page object, which does not reliably carry arbitrary attributes.
        _reg = getattr(mgr, "_intercepts", None)
        if _reg is None:
            _reg = {}
            setattr(mgr, "_intercepts", _reg)
        # unroute() requires the url pattern too, so store it with the handler.
        _reg.setdefault(tid, []).append((_rx, handler))
        await page.route(_rx, handler)
        return json.dumps({
            "status": "interception_armed",
            "url_pattern": url_pattern,
            "armed_handlers": len(_reg.get(tid, [])),
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def detect_tech(target_id: Optional[str] = None) -> str:
    """
    Fingerprint the target's tech stack (Server, Framework, CMS, JS Libraries, CDN).
    """
    try:
        page, tid = await mgr.get_page(target_id)
        
        # 1. Get DOM-based tech
        dom_tech = await page.evaluate("""
        (() => {
            const tech = [];
            if (window.React || document.querySelector('[data-reactroot]')) tech.push('React');
            if (window.Vue || document.querySelector('[data-v-]')) tech.push('Vue.js');
            if (window.angular || document.querySelector('[ng-version]')) tech.push('Angular');
            if (window.jQuery) {
                const _jq = (window.jQuery.fn && window.jQuery.fn.jquery)
                    || window.jQuery.jquery || '';
                tech.push(_jq ? ('jQuery v' + _jq) : 'jQuery (version unknown)');
            }
            if (window.Backbone) tech.push('Backbone.js');
            if (window.Ember) tech.push('Ember.js');
            if (window.__NEXT_DATA__) tech.push('Next.js');
            if (window.__NUXT__) tech.push('Nuxt.js');
            if (window.Shopify) tech.push('Shopify');
            if (window.wp || document.querySelector('link[href*="wp-content"]')) tech.push('WordPress');
            const generator = document.querySelector('meta[name="generator"]');
            if (generator) tech.push(generator.content);
            return tech;
        })()
        """)

        # 2. Get Header-based tech (Server, CDN, WAF)
        headers_info = {}
        try:
            resp = await page.request.get(page.url, timeout=10000)
            if resp:
                h = resp.headers
                headers_info = {
                    "server": h.get("server"),
                    "x-powered-by": h.get("x-powered-by"),
                    "via": h.get("via"),
                    "cf-ray": h.get("cf-ray") is not None and "Cloudflare" or None,
                    "x-aws-cf-id": h.get("x-amz-cf-id") is not None and "AWS CloudFront" or None,
                }
        except Exception:
            pass

        return json.dumps({
            "status": "success",
            "url": page.url,
            "detected_technologies": list(set(dom_tech)),
            "server_headers": {k: v for k, v in headers_info.items() if v}
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def extract_links(
    target_id: Optional[str] = None,
    pattern: Optional[str] = None,
    include_external: bool = False,
    crawl_depth: int = 1,
    max_duration_seconds: int = 120,
    max_pages: int = 100,
) -> str:
    """
    Extract links with deadline + domain pinning. Deque BFS.

    crawl_depth = number of LEVELS visited, counting the start page as level 1:
      1 = start page only, 2 = start page + the pages its links point to, ...
      (clamped to 1-5). NOTE this differs from crawl_site's `max_depth`, which
      counts HOPS (0 = start page only). Both mean "how far to walk", but the
      numbers are off by one — do not copy a value between them.
    """
    from urllib.parse import urlparse
    from collections import deque
    import time as _time
    import re as _re

    try:
        crawl_depth = int(crawl_depth)
        if crawl_depth < 1 or crawl_depth > 5:
            return json.dumps({
                "error": f"crawl_depth must be 1-5 (got {crawl_depth})",
                "note": ("crawl_depth counts LEVELS (1 = start page only). "
                         "crawl_site's max_depth counts HOPS (0 = start page only)."),
            })

        page, tid = await mgr.get_page(target_id)
        base_url = page.url
        base_parsed = urlparse(base_url)
        if not base_parsed.scheme or not base_parsed.netloc:
            return json.dumps({"error": f"Cannot determine origin: {base_url}"})
        base_origin = base_parsed.scheme + "://" + base_parsed.netloc

        all_found = set()
        to_visit = deque([(base_url, 0)])
        visited = set()
        start_time = _time.time()
        timed_out = False
        capped = False

        crawler_page = page
        if crawl_depth > 1:
            crawler_page = await page.context.new_page()

        try:
            while to_visit:
                if _time.time() - start_time > max_duration_seconds:
                    timed_out = True
                    break
                if len(visited) >= max_pages:
                    capped = True
                    break
                current_url, depth = to_visit.popleft()
                if current_url in visited or depth >= crawl_depth:
                    continue
                visited.add(current_url)
                if current_url != base_url or crawler_page != page:
                    try:
                        await crawler_page.goto(current_url, wait_until="domcontentloaded", timeout=8000)
                    except Exception:
                        continue
                try:
                    links = await crawler_page.evaluate("""(inc) => {
                        const o = window.location.origin;
                        const u = new Set();
                        document.querySelectorAll('a[href]').forEach(a => {
                            let h = a.href;
                            if (!h || h.startsWith('javascript:') || h.startsWith('#') || h.startsWith('mailto:') || h.startsWith('tel:')) return;
                            if (inc || h.startsWith(o)) u.add(h);
                        });
                        return Array.from(u);
                    }""", include_external)
                except Exception:
                    continue
                for link in links:
                    if pattern and not _re.search(pattern, link):
                        continue
                    if not include_external:
                        lp = urlparse(link)
                        lo = lp.scheme + "://" + lp.netloc
                        if lo != base_origin:
                            continue
                    if link not in all_found:
                        all_found.add(link)
                        if depth + 1 < crawl_depth:
                            to_visit.append((link, depth + 1))
        finally:
            if crawler_page != page:
                try:
                    await crawler_page.close()
                except Exception:
                    pass

        return json.dumps({
            "status": "success",
            "base_origin": base_origin,
            "depth_reached": crawl_depth,
            "total_found": len(all_found),
            "visited_pages": len(visited),
            "timed_out": timed_out,
            "capped": capped,
            "max_duration_seconds": max_duration_seconds,
            "max_pages": max_pages,
            "links": list(all_found)[:200],
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})

@mcp.tool()
async def check_security_headers(target_id: Optional[str] = None) -> str:
    """
    Quickly audit security headers (CSP, HSTS, X-Frame-Options, etc.).
    """
    try:
        page, tid = await mgr.get_page(target_id)
        resp = await page.request.get(page.url, timeout=10000)
        if not resp:
            return json.dumps({"error": "No response from server"})
        
        headers = resp.headers
        audit = {
            "missing": [],
            "present": {},
            "info_leakage": []
        }
        
        # Check important headers
        sec_headers = [
            "strict-transport-security", "content-security-policy",
            "x-frame-options", "x-content-type-options",
            "referrer-policy", "permissions-policy"
        ]
        
        for h in sec_headers:
            if h in headers:
                audit["present"][h] = headers[h]
            else:
                audit["missing"].append(h)
                
        # Check for server info leak
        for leak in ["server", "x-powered-by", "x-aspnet-version"]:
            if leak in headers:
                audit["info_leakage"].append(f"{leak}: {headers[leak]}")

        return json.dumps({"status": "success", "url": page.url, "audit": audit}, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def wait_for_response(
    url_pattern: str,
    timeout_ms: int = 15000,
    target_id: Optional[str] = None
) -> str:
    """Wait for a specific network request to complete."""
    try:
        page, tid = await mgr.get_page(target_id)
        import re as _re
        
        # Playwright's correct way to wait for a specific event
        response = await page.wait_for_event(
            "response", 
            predicate=lambda r: bool(_re.search(url_pattern, r.url)), 
            timeout=timeout_ms
        )
        
        return json.dumps({
            "status": "response_received",
            "url": response.url,
            "status_code": response.status,
            "method": response.request.method
        })
    except Exception as e:
        return json.dumps({"error": "Timeout or failed", "details": str(e)[:200],
                           "note": "expected when no request matching url_pattern fires within timeout_ms — not a tool fault"})


@mcp.tool()
async def extract_table(
    table_selector: str = "table",
    target_id: Optional[str] = None
) -> str:
    """
    Extract an HTML table into clean JSON data.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        
        data = await page.evaluate("""
        (selector) => {
            const table = document.querySelector(selector);
            if (!table) return null;
            
            const rows = Array.from(table.querySelectorAll('tr'));
            return rows.map(row => {
                const cells = Array.from(row.querySelectorAll('th, td'));
                return cells.map(cell => cell.innerText.trim());
            });
        }
        """, table_selector)
        
        if data is None:
            return json.dumps({"error": f"Table not found: {table_selector}"})
            
        return json.dumps({"status": "success", "rows": len(data), "data": data[:100]}, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def click_by_role(
    role: str,
    name: Optional[str] = None,
    exact: bool = False,
    target_id: Optional[str] = None
) -> str:
    """
    Click an element using accessibility roles (bypasses CSS changes).
    role: "button", "link", "checkbox", "menuitem", etc.
    name: The text label of the element (e.g., "Submit")
    """
    try:
        page, tid = await mgr.get_page(target_id)
        loc = page.get_by_role(role, name=name, exact=exact).first
        
        if await loc.count() == 0:
            return json.dumps({"error": f"No element found with role='{role}' and name='{name}'"})
            
        await loc.scroll_into_view_if_needed()
        await loc.click()
        
        return json.dumps({"status": "clicked", "role": role, "name": name})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def capture_websocket(
    duration_seconds: int = 10,
    target_id: Optional[str] = None
) -> str:
    """
    Capture live WebSocket frames for a specific duration.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        frames = []
        
        def on_ws(ws):
            def on_frame_sent(payload):
                frames.append({"type": "sent", "data": str(payload)[:500]})
            def on_frame_recv(payload):
                frames.append({"type": "received", "data": str(payload)[:500]})
            ws.on("framesent", on_frame_sent)
            ws.on("framereceived", on_frame_recv)
            
        page.on("websocket", on_ws)
        try:
            # Wait for traffic
            await asyncio.sleep(duration_seconds)
        finally:
            try:
                page.remove_listener("websocket", on_ws)
            except Exception:
                pass

        return json.dumps({
            "status": "captured",
            "duration": duration_seconds,
            "total_frames": len(frames),
            "frames": frames[-50:] # Last 50 frames
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def check_robots(
    base_url: Optional[str] = None,
    target_id: Optional[str] = None
) -> str:
    """
    Fetch and parse robots.txt WITHOUT navigating the current tab.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        url = base_url or page.url
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return json.dumps({"error": f"Cannot derive origin from: {url}"})
        origin = f"{parsed.scheme}://{parsed.netloc}"
        robots_url = origin + "/robots.txt"
        
        # Use page.request to fetch data without changing the browser's current page
        resp = await page.request.get(robots_url, timeout=10000)
        if resp.status != 200:
            return json.dumps({"error": "robots.txt not found or unreachable", "status": resp.status,
                               "note": "host has no reachable robots.txt — target-appropriate, not a tool fault"})
            
        content = await resp.text()
        
        import re
        disallowed = re.findall(r'Disallow:\s*(.*)', content)
        allowed = re.findall(r'Allow:\s*(.*)', content)
        
        return json.dumps({
            "status": "success",
            "robots_url": robots_url,
            "disallowed_paths": [d.strip() for d in disallowed if d.strip() and d.strip() != '/'],
            "allowed_paths": [a.strip() for a in allowed if a.strip()],
            "raw_length": len(content)
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def check_sitemap(
    base_url: Optional[str] = None,
    target_id: Optional[str] = None
) -> str:
    """
    Fetch sitemap.xml WITHOUT navigating the current tab.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        url = base_url or page.url
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return json.dumps({"error": f"Cannot derive origin from: {url}"})
        origin = f"{parsed.scheme}://{parsed.netloc}"
        sitemap_url = origin + "/sitemap.xml"
        
        resp = await page.request.get(sitemap_url, timeout=10000)
        if resp.status != 200:
            return json.dumps({"error": "sitemap.xml not found", "status": resp.status,
                               "note": "host has no sitemap.xml — target-appropriate, not a tool fault"})
            
        content = await resp.text()
        import re
        urls = re.findall(r'<loc>(.*?)</loc>', content)
        
        return json.dumps({
            "status": "success",
            "sitemap_url": sitemap_url,
            "total_urls": len(urls),
            "urls": urls[:100] # cap at 100
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def get_dom_diff(
    action_js: str,
    target_id: Optional[str] = None
) -> str:
    """
    Capture HTML, run a JS action, capture HTML again, and compare.
    action_js: JS code to execute between snapshots (e.g., trigger XSS payload).
    """
    try:
        page, tid = await mgr.get_page(target_id)
        
        # Snapshot 1
        before = await page.evaluate("document.body.innerText")
        before_html = await page.evaluate("document.body.innerHTML")
        
        # Action. action_js may be an expression, a function, or a statement
        # block. Decide which by COMPILING it in the page (new Function never
        # runs the code), then execute exactly ONCE. The previous version ran the
        # raw code first and re-ran it wrapped on failure, so a statement that
        # mutated state and then threw ran TWICE.
        # `new Function` here only COMPILES action_js to decide expression-vs-
        # statement; it never runs it. action_js is agent-supplied code that this
        # tool exists to execute (same trust level as evaluate_javascript), and it
        # is executed exactly once below.
        try:
            _mode = await page.evaluate("""(code) => {
                try { new Function("return (" + code + ")"); return "expr"; }
                catch (e) { return "stmt"; }
            }""", action_js)
        except Exception as _e0:
            return json.dumps({
                "status": "action_failed",
                "error": f"action_js could not be compiled: {type(_e0).__name__}: {str(_e0)[:200]}",
                "hint": ("Pass an expression (e.g. 'document.body.innerHTML=1') "
                         "or a statement block; a returned function is called for you."),
            })
        try:
            if _mode == "expr":
                # a bare function expression is called, matching page.evaluate semantics
                await page.evaluate(
                    "() => { const r = (" + action_js + ");"
                    " return (typeof r === 'function') ? r() : r; }")
            else:
                await page.evaluate("() => { " + action_js + " }")
        except Exception as _e1:
            return json.dumps({
                "status": "action_failed",
                "error": f"action_js could not be executed: {type(_e1).__name__}: {str(_e1)[:200]}",
                "mode": _mode,
                "hint": ("Pass an expression (e.g. 'document.body.innerHTML=1') "
                         "or a statement block; a returned function is called for you."),
            })
        await asyncio.sleep(1.5) # Wait for DOM to update
        
        # Snapshot 2
        after = await page.evaluate("document.body.innerText")
        after_html = await page.evaluate("document.body.innerHTML")
        
        # Simple diff (just length and whether it changed)
        changed = before != after
        html_changed = before_html != after_html
        diff_size = len(after) - len(before)
        
        # Extract newly added lines (very basic)
        import difflib
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        diff = list(difflib.unified_diff(before_lines, after_lines, lineterm=''))
        
        # Show the ACTUAL changed lines, not the diff's file/hunk headers:
        # a plain diff[:20] is almost entirely '---', '+++' and '@@' lines.
        _added = [l for l in diff if l.startswith('+') and not l.startswith('+++')]
        _removed = [l for l in diff if l.startswith('-') and not l.startswith('---')]
        _hunks = [l for l in diff if l.startswith('@@')]
        preview = (_removed[:20] + _added[:20]) if (_removed or _added) else diff[:20]
        return _dumps_capped({
            "status": "success",
            "dom_changed": changed,
            "html_changed": html_changed,
            "size_delta": diff_size,
            "html_size_delta": len(after_html) - len(before_html),
            "added_lines": len(_added),
            "removed_lines": len(_removed),
            "hunks": len(_hunks),
            "diff_preview": preview[:40],
        }, 200000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def scroll_and_capture(
    scroll_count: int = 5,
    delay_ms: int = 1000,
    target_id: Optional[str] = None
) -> str:
    """
    Scroll down the page to load lazy content, then take a screenshot.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        
        for i in range(scroll_count):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(delay_ms / 1000.0)
            
        # Scroll back to top for a proper full-page screenshot
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
        
        path = f"/tmp/fullpage_{uuid.uuid4().hex[:6]}.png"
        await page.screenshot(path=path, full_page=True)
        
        return json.dumps({
            "status": "success",
            "scrolls_performed": scroll_count,
            "screenshot": path
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def wait_for_element_count(
    selector: str,
    count: int = 1,
    timeout_ms: int = 15000,
    target_id: Optional[str] = None
) -> str:
    """
    Wait until a specific number of elements matching a selector appear.
    Useful for infinite scroll or dynamic lists.
    """
    try:
        if int(count) < 1:
            return json.dumps({
                "error": f"count must be >= 1 (got {count})",
                "details": "nth=0 would be invalid; pass 1 for the first match.",
            })
        page, tid = await mgr.get_page(target_id)

        # Playwright has a built-in wait for selector count
        await page.wait_for_selector(f"{selector} >> nth={count-1}", timeout=timeout_ms)
        
        actual = await page.locator(selector).count()
        return json.dumps({
            "status": "success",
            "selector": selector,
            "expected": count,
            "actual": actual
        })
    except Exception as e:
        return json.dumps({"error": "Timeout waiting for elements", "details": str(e)[:200]})


@mcp.prompt()
def quick_recon(target: str) -> str:
    """Generates a professional Recon prompt for a target URL."""
    return f"Perform a full reconnaissance on {target}. Use detect_tech, check_security_headers, check_robots, and check_sitemap. Then use extract_links with crawl_depth=2 to map the application. Finally, use capture_har(extract_js_files=True) to find any exposed API keys. Report all findings."

@mcp.prompt()
def sqli_testing(target: str) -> str:
    """Generates a SQL Injection testing prompt."""
    return f"Test {target} for SQL Injection. Navigate to all pages containing forms or URL parameters. Use type_text with payloads like ' OR 1=1 --, ' OR '1'='1, and admin' --. Use get_dom_diff to check if the payload executed or caused a database error. Report all confirmed SQL injections with the exact payload and evidence."

@mcp.prompt()
def xss_testing(target: str) -> str:
    """Generates an XSS testing prompt."""
    return f"Test {target} for Cross-Site Scripting (XSS). Use bypass_csp=True when spawning the context. Inject payloads like <script>alert(1)</script> and <img src=x onerror=alert(1)> into all input fields. Use get_dom_diff(action_js='alert(1)') to confirm execution. Report reflected, stored, and DOM-based XSS."

@mcp.resource("config://netwala")
def get_netwala_config() -> str:
    """Returns Netwala configuration details."""
    return json.dumps({"version": "v3.0", "custom_tools": len(mcp._tool_manager._tools), "engine": "Firefox", "status": "Ready for HackerOne"})

@mcp.tool()
async def clear_site_data(target_id: Optional[str] = None) -> str:
    """
    Clears context cookies and permissions, then localStorage, sessionStorage,
    IndexedDB and CacheStorage for the current origin, and reloads.

    NOTE: HSTS lives in the Firefox profile (SiteSecurityServiceState.txt) and
    cannot be cleared from JS/Playwright — restart with a fresh profile for
    that. The HTTP cache is not cleared either (use a fresh context).
    """
    try:
        page, tid = await mgr.get_page(target_id)
        ctx = page.context
        await ctx.clear_cookies()
        # Clear each store independently and report per store. A long-running
        # browser can start refusing localStorage access (observed:
        # "Page.evaluate: {}" after extended uptime); failing the whole tool on
        # that hides the real state, so report it instead.
        stores = {}
        for _store in ("localStorage", "sessionStorage"):
            try:
                stores[_store] = await page.evaluate(
                    f"(()=>{{try{{window.{_store}.clear();return 'ok'}}"
                    f"catch(e){{return e.name+': '+e.message}}}})()")
            except Exception as e:
                stores[_store] = f"{type(e).__name__}: {str(e)[:120]}"
        # IndexedDB and CacheStorage are NOT covered by the storage API above.
        try:
            stores["extra"] = await page.evaluate(
                "(async () => { const out = {indexeddb: 'n/a', cachestorage: 'n/a'};"
                " try { if (window.indexedDB && indexedDB.databases) {"
                "   const dbs = await indexedDB.databases();"
                "   for (const d of dbs) { try { indexedDB.deleteDatabase(d.name); } catch (e) {} }"
                "   out.indexeddb = 'cleared:' + dbs.length;"
                " } } catch (e) { out.indexeddb = 'error:' + e.name; }"
                " try { if (window.caches) {"
                "   for (const k of await caches.keys()) { try { await caches.delete(k); } catch (e) {} }"
                "   out.cachestorage = 'cleared';"
                " } } catch (e) { out.cachestorage = 'error:' + e.name; }"
                " return out; })()")
        except Exception as _se:
            stores["extra"] = f"{type(_se).__name__}: {str(_se)[:120]}"
        await ctx.clear_permissions()
        # Bypass cache by reloading
        await page.reload(wait_until="domcontentloaded")
        _all_ok = all(v == "ok" for k, v in stores.items() if k != "extra")
        return json.dumps({
            "status": "success" if _all_ok else "partial",
            "message": "Site data cleared and page reloaded.",
            "stores": stores,
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def get_response_body(
    url_pattern: str,
    timeout_ms: int = 15000,
    target_id: Optional[str] = None
) -> str:
    """
    Wait for a specific network request and return its response body (JSON/HTML).
    """
    try:
        page, tid = await mgr.get_page(target_id)
        import re as _re
        
        async with page.expect_response(lambda r: bool(_re.search(url_pattern, r.url)), timeout=timeout_ms) as response_info:
            pass
            
        response = await response_info.value
        body = await response.text()
        
        return json.dumps({
            "status": "success",
            "url": response.url,
            "status_code": response.status,
            "body_preview": body[:2000] # Cap to 2000 chars to save token space
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": "Timeout or failed", "details": str(e)[:200],
                           "note": "expected when no request matching url_pattern fires within timeout_ms — not a tool fault"})


@mcp.tool()
async def replay_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    target_id: Optional[str] = None
) -> str:
    """
    Replay a request directly from the browser context (Burp Repeater style).
    """
    try:
        check_url(url, "replay_request")
        page, tid = await mgr.get_page(target_id)
        
        kwargs = {
            "method": method.upper(),
            "headers": headers or {},
            "fail_on_status_code": False,
            "timeout": 15000
        }
        if body:
            kwargs["data"] = body

        # Use page.request to send request via the browser's network stack (bypasses CORS)
        resp = await page.request.fetch(url, **kwargs)
        resp_body = await resp.text()
        
        return json.dumps({
            "status": "success",
            "url": resp.url,
            "status_code": resp.status,
            "response_headers": dict(resp.headers),
            "body_preview": resp_body[:1500]
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def extract_forms(target_id: Optional[str] = None) -> str:
    """
    Extract all HTML forms with their action, method, inputs, and hidden CSRF tokens.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        
        forms = await page.evaluate("""
        () => {
            const forms = [];
            document.querySelectorAll('form').forEach(f => {
                const inputs = [];
                f.querySelectorAll('input, textarea, select').forEach(i => {
                    inputs.push({ name: i.name, type: i.type, value: i.value, id: i.id });
                });
                forms.push({ action: f.action, method: (f.method || 'GET').toUpperCase(), inputs: inputs });
            });
            return forms;
        }
        """)
        
        return json.dumps({"status": "success", "form_count": len(forms), "forms": forms}, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def crawl_site(
    url: Optional[str] = None,
    max_depth: int = 2,
    target_id: Optional[str] = None
) -> str:
    """
    Full site crawler: robots, sitemap, links, forms in one shot.

    max_depth = the deepest hop that gets VISITED; pages at depth 0..max_depth
    are loaded (clamped to 0-5). 0 = start page only, 1 = start page and its
    direct links, 2 = one more hop, and so on.

    Hard caps: at most 200 pages and 60 seconds per call. The result reports
    `page_cap_hit` / `time_cap_hit` when a cap stopped the crawl.

    URLs are compared after dropping the fragment and normalising the trailing
    slash. Query strings are kept on purpose: `?id=1` and `?id=2` are different
    pages, and they are exactly what IDOR work needs.

    Only same-origin links are followed, and the tab is returned to the start
    URL when the crawl finishes.
    """
    try:
        from urllib.parse import urlparse, urlunparse
        import re as _re
        import time as _time

        max_depth = int(max_depth)
        if max_depth < 0 or max_depth > 5:
            return json.dumps({
                "error": f"max_depth must be 0-5 (got {max_depth})",
                "note": ("max_depth = deepest hop VISITED. 0 = start page only, "
                         "1 = start page + its direct links."),
            })

        page, tid = await mgr.get_page(target_id)
        base_url = url or page.url
        check_url(base_url, "crawl_site")
        _bp = urlparse(base_url)
        if not _bp.scheme or not _bp.netloc:
            return json.dumps({"error": f"Cannot determine origin: {base_url}"})
        base_origin = f"{_bp.scheme}://{_bp.netloc}"
        base_domain = _bp.netloc

        def _norm(u):
            """Identity for dedupe: no fragment, no trailing slash; query kept."""
            try:
                p = urlparse(u)
                path = p.path or "/"
                if path != "/" and path.endswith("/"):
                    path = path.rstrip("/")
                return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))
            except Exception:
                return u

        async def _get(u):
            try:
                r = await page.request.get(u, timeout=15000)
                return (await r.text()) if r.status == 200 else ""
            except Exception:
                return ""

        robots_txt = await _get(f"{base_origin}/robots.txt")
        sitemap_txt = await _get(f"{base_origin}/sitemap.xml")
        sitemap_urls = _re.findall(r"<loc>(.*?)</loc>", sitemap_txt)

        seen = set()          # normalised URLs already visited
        listed = set()        # normalised URLs already reported
        to_visit = [(base_url, 0)]
        internal_links = []
        forms = []
        page_cap_hit = False
        time_cap_hit = False
        _t0 = _time.time()
        try:
            while to_visit:
                if len(seen) >= 200:
                    page_cap_hit = True
                    break
                if (_time.time() - _t0) >= 60:
                    time_cap_hit = True
                    break
                cur, depth = to_visit.pop(0)
                _key = _norm(cur)
                if _key in seen:
                    continue
                seen.add(_key)
                if depth > 0:
                    try:
                        await page.goto(cur, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        continue
                try:
                    links = await page.evaluate(
                        "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
                except Exception:
                    continue
                for h in links:
                    if not h.startswith(("http://", "https://")):
                        continue
                    try:
                        o = urlparse(h)
                    except Exception:
                        continue
                    if f"{o.scheme}://{o.netloc}" != base_origin:
                        continue                      # same-origin only
                    k = _norm(h)
                    if k not in listed:
                        listed.add(k)
                        internal_links.append(h)
                    if depth < max_depth and k not in seen:
                        to_visit.append((h, depth + 1))

            try:
                forms = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('form')).map(f => ({action: f.action, method: f.method}))")
            except Exception:
                forms = []
        finally:
            # Leave the tab where the caller put it, not on the last crawled URL.
            try:
                await page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass

        return json.dumps({
            "status": "success",
            "domain": base_domain,
            "origin": base_origin,
            "start_url": base_url,
            "max_depth": max_depth,
            "pages_crawled": len(seen),
            "page_cap": 200,
            "time_cap_seconds": 60,
            "page_cap_hit": page_cap_hit,
            "time_cap_hit": time_cap_hit,
            "elapsed_seconds": round(_time.time() - _t0, 2),
            "robots_found": len(robots_txt) > 0,
            "sitemap_urls": len(sitemap_urls),
            "links_found": len(internal_links),
            "forms_found": len(forms),
            "links": internal_links,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def take_element_screenshot(
    selector: str,
    save_path: Optional[str] = None,
    target_id: Optional[str] = None
) -> str:
    """Take a screenshot of a SPECIFIC element (button, form, div) instead of the whole page."""
    try:
        if save_path:
            save_path = safe_path(save_path, "take_element_screenshot")
        page, tid = await mgr.get_page(target_id)
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return json.dumps({"error": f"Selector not found: {selector}"})
            
        path = save_path or f"/tmp/element_{uuid.uuid4().hex[:8]}.png"
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.screenshot(path=path)
        return json.dumps({"status": "saved", "path": path, "bytes": os.path.getsize(path)})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def hover(selector: str, target_id: Optional[str] = None) -> str:
    """Hover over an element to trigger dropdowns/tooltips."""
    try:
        page, tid = await mgr.get_page(target_id)
        loc = page.locator(selector).first
        if h_on():
            try:
                await loc.scroll_into_view_if_needed(timeout=5000)
                box = await loc.bounding_box()
                if box:
                    await human_move(page, box["x"] + box["width"] / 2,
                                     box["y"] + box["height"] / 2)
                    await asyncio.sleep(random.uniform(0.1, 0.35))
            except Exception:
                pass
        await loc.hover()
        return json.dumps({"status": "hovered", "selector": selector})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def emulate_device(device_name: str, target_id: Optional[str] = None) -> str:
    """Instantly switch the current tab to a device layout (iPhone/Pixel/Desktop).

    Viewport is applied for real. Firefox cannot change the User-Agent or the
    real devicePixelRatio on a LIVE context, so UA / DPR / maxTouchPoints are
    applied at JS level (immediate + init script so they survive navigation).
    The HTTP-header UA is unchanged — use spawn_subagent_environment(
    emulate_device=...) for a real network-level UA.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        dev = DEVICE_PRESETS.get(device_name.lower())
        if not dev:
            return json.dumps({"error": f"Device {device_name} not supported. Use: {', '.join(sorted(DEVICE_PRESETS))}"})

        vp = dev["vp"]
        await page.set_viewport_size(vp)

        # Firefox defines navigator.userAgent / platform / oscpu on
        # Navigator.prototype. An OWN property on the navigator instance is
        # detectable via hasOwnProperty / getOwnPropertyNames, so install on the
        # prototype (falling back to the instance if the proto is Object.prototype).
        parts = ["const _nw_def=(o,p,v,n)=>{try{const g=()=>v;"
                 "Object.defineProperty(g,'toString',{value:()=>'function '+n+'() { [native code] }',configurable:true});"
                 "Object.defineProperty(o,p,{get:g,configurable:true})}catch(e){}};",
                 "const _navproto=(()=>{const p=Object.getPrototypeOf(navigator);"
                 "return (p && p!==Object.prototype)?p:navigator;})();"]
        if dev.get("dpr"):
            parts.append("_nw_def(window,'devicePixelRatio'," + repr(dev["dpr"]) + ",'devicePixelRatio');")
        if dev.get("tp") is not None:
            parts.append("_nw_def(_navproto,'maxTouchPoints'," + repr(dev["tp"]) + ",'maxTouchPoints');")
        if dev.get("oscpu"):
            parts.append("_nw_def(_navproto,'oscpu'," + json.dumps(dev["oscpu"]) + ",'oscpu');")
        if dev.get("platform"):
            parts.append("_nw_def(_navproto,'platform'," + json.dumps(dev["platform"]) + ",'platform');")
        parts.append("_nw_def(_navproto,'userAgent'," + json.dumps(dev["ua"]) + ",'userAgent');")
        script = "(() => { " + "".join(parts) + " })()"

        # immediate effect on the current document
        try:
            await page.evaluate(script)
        except Exception:
            pass
        # durable across later navigations / new documents in this context
        try:
            await page.context.add_init_script(script)
        except Exception:
            pass

        return json.dumps({
            "status": "emulated", "device": device_name, "viewport": vp,
            "device_pixel_ratio": dev.get("dpr"),
            "max_touch_points": dev.get("tp"),
            "oscpu": dev.get("oscpu"), "platform": dev.get("platform"),
            "ua_js_level": dev["ua"],
            "note": ("viewport real; UA/DPR/maxTouchPoints are JS-level on a live "
                     "context (HTTP-header UA unchanged). Use "
                     "spawn_subagent_environment(emulate_device=...) for a real network UA."),
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def switch_to_frame(frame_url_pattern: str, js_code: str, target_id: Optional[str] = None) -> str:
    """Execute JS inside a specific iframe (payment forms, embedded widgets)."""
    try:
        page, tid = await mgr.get_page(target_id)
        import re as _re
        
        for frame in page.frames:
            if _re.search(frame_url_pattern, frame.url):
                result = await frame.evaluate(js_code)
                return json.dumps({"status": "success", "frame_url": frame.url, "result": result}, default=str)
                
        return json.dumps({"error": f"No frame matching {frame_url_pattern} found",
                           "available_frames": [f.url for f in page.frames],
                           "note": "expected when no iframe matches frame_url_pattern — not a tool fault"})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})



@mcp.tool()
async def stop_intercept(target_id: Optional[str] = None) -> str:
    """Stops all active route interceptions (clears intercept_request)."""
    try:
        page, tid = await mgr.get_page(target_id)
        # Remove ONLY the handlers this tool armed; unroute_all() would also
        # tear down anything else sharing the page.
        _reg = getattr(mgr, "_intercepts", None) or {}
        _handlers = list(_reg.get(tid, []))
        cleared = 0
        for _entry in _handlers:
            try:
                _pat, _h = _entry
            except Exception:
                _pat, _h = None, _entry
            try:
                if _pat is not None:
                    await page.unroute(_pat, handler=_h)
                else:
                    await page.unroute(handler=_h)
                cleared += 1
            except Exception:
                pass
        _reg[tid] = []
        return json.dumps({
            "status": "success",
            "message": "All intercepts cleared.",
            "cleared": cleared,
            "remaining": len(_reg.get(tid, [])),
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def detect_waf(
    target_id: Optional[str] = None,
    active_test: bool = False,
) -> str:
    """
    Detects a WAF from response headers/cookies, then compares a benign request
    with an attack-shaped one.

    PASSIVE by default. `active_test=True` additionally fires the attack-shaped
    probe. Only enable it on a target you are authorized to test: it sends a
    real <script> payload and a WAF may ban the session cookie / IP for it.
    Both probes go to a fixed NON-EXISTENT path under the origin, never to
    location.href, so they cannot re-trigger a state-changing GET.
    """
    try:
        page, tid = await mgr.get_page(target_id)
        
        # 1. Get headers. Use the context request API (no extra in-page fetch
        # against the target); fall back to an in-page HEAD only if that fails.
        try:
            _hr = await page.context.request.head(page.url)
            info = {"headers": {k.lower(): v for k, v in _hr.headers.items()},
                    "cookies": await page.evaluate("document.cookie")}
        except Exception:
            info = await page.evaluate("""
            async () => {
                const resp = await fetch(location.href, {method: 'HEAD', credentials: 'include'});
                const headers = {};
                resp.headers.forEach((v, k) => headers[k.toLowerCase()] = v);
                return {headers, cookies: document.cookie};
            }
            """)
        
        headers = info.get("headers", {})
        headers_str = " ".join([f"{k}:{v}" for k, v in headers.items()]).lower() + " " + info.get("cookies", "").lower()
        
        # 2. Signatures (Order matters! Specific WAFs first, CDNs last)
        signatures = {
            "datadome": ["datadome", "x-datadome", "dd_cookie", "x-dd-b", "dd_attr"],
            "imperva_incapsula": ["x-iinfo", "visid_incap", "incap_ses", "incapsula"],
            "akamai": ["akamai-grn", "x-akamai-transformed", "akamai-origin-hop"],
            "sucuri": ["x-sucuri-id", "x-sucuri-cache", "sucuri"],
            "f5_bigip": ["bigipserver", "x-wa-info"],
            "cloudflare": ["cf-ray", "__cf_bm", "cf_clearance", "cf-cache-status"],
            "aws_waf": ["x-amzn-waf-action", "x-amzn-requestid-mismatch", "awswaf"], # Removed x-amz-cf-id (that's CloudFront)
        }
        
        detected = []
        for waf, sigs in signatures.items():
            for sig in sigs:
                if sig in headers_str:
                    detected.append({"waf": waf, "matched": sig})
                    break
        
        primary_waf = detected[0]["waf"] if detected else "unknown"
        
        # 3. Benign vs Malicious Test
        tests = await page.evaluate("""
        async (active) => {
            try {
                // Benign test — fixed non-existent path, NOT location.href
                const _probe = location.origin + '/_netwala_probe_' + Math.random().toString(36).slice(2);
                const r_benign = await fetch(_probe, {credentials: 'include'});
                const benign_status = r_benign.status;
                const benign_text = await r_benign.text();
                const bl = benign_text.toLowerCase();
                const benign_blocked = (benign_status === 403 || benign_status === 429 || benign_status === 503) ||
                    bl.includes('request was blocked') || bl.includes('access denied') ||
                    bl.includes('you have been blocked') || bl.includes('captcha-delivery.com') ||
                    bl.includes('dd_captcha') || (bl.length < 1500 && bl.includes('blocked'));
                
                // Attack test — only when the caller explicitly opts in
                let attack = {skipped: true, reason: "active_test is false"};
                if (active) {
                    const r_attack = await fetch(_probe + '?test=' + encodeURIComponent('<script>alert(1)</script>'), {credentials: 'include'});
                    const attack_status = r_attack.status;
                    const attack_text = await r_attack.text();
                    const al = attack_text.toLowerCase();
                    attack = {status: attack_status, blocked:
                        (attack_status === 403 || attack_status === 429 || attack_status === 503) ||
                        al.includes('request was blocked') || al.includes('access denied') ||
                        al.includes('you have been blocked') || al.includes('captcha-delivery.com') ||
                        al.includes('dd_captcha')};
                }

                return {benign: {status: benign_status, blocked: benign_blocked}, attack: attack};
            } catch (e) { return {error: "CORS_OR_NETWORK_ERROR: " + e.toString(), hint: "Ensure the URL host matches the current tab origin."}; }
        }
        """, active_test)
        
        # 4. Evasion map
        evasion_map = {
            "datadome": ["Headful browser (Xvfb)", "Residential proxy", "Slow behavioral timing"],
            "imperva_incapsula": ["Case alternation (UnIoN)", "Inline comments (UN/**/ION)", "Hex encoding"],
            "akamai": ["Double URL-encoding", "Unicode bypass", "CHAR() functions"],
            "cloudflare": ["URL-encode payloads", "Use HTML entities", "Chunked transfer encoding"],
            "aws_waf": ["Chunked encoding", "Mismatched Content-Type", "GraphQL query bypass"],
            "unknown": ["Payload obfuscation", "TLS Spoofing (http_request_cffi)"]
        }

        return json.dumps({
            "waf_detected": [d["waf"] for d in detected] or ["none_identified"],
            "primary_waf": primary_waf,
            "benign_test": tests.get("benign"),
            "attack_test": tests.get("attack"),
            "evasion_strategies": evasion_map.get(primary_waf, evasion_map["unknown"]),
            "recommendation": "If benign is 200 and attack is 403, WAF is active. Use payload_library or http_request_cffi."
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})

@mcp.tool()
async def payload_library(
    vuln_type: str,
    waf: Optional[str] = None,
    count: int = 0,
) -> str:
    """
    Returns ready-to-use payloads (48-55 per vuln type), with REAL obfuscation for the detected WAF.
    count=0 (default) returns ALL payloads. count>0 truncates to that many.
    vuln_types: sqli, xss, lfi, rce, cmdi, ssti, xxe, nosqli, ssrf, open_redirect
    """
    try:
        from payloads_data import get_payloads
        result, error = get_payloads(vuln_type, waf, count)
        if error:
            return json.dumps({"error": error})
        return json.dumps(result, indent=2)
    except ImportError:
        return json.dumps({"error": "payloads_data.py module not found in /root/netwala"})
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})

@mcp.tool()
async def smart_replay(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    use_browser_context: bool = True,
    auto_validate: bool = True,
    target_id: Optional[str] = None,
) -> str:
    """
    Enhanced replay: sends request via IN-PAGE fetch (auto-attaches cookies/CSRF).
    Returns detailed feedback so agent knows if anything was missing.
    """
    try:
        check_url(url, "smart_replay")
        page, tid = await mgr.get_page(target_id)
        
        # 1. Harvest available tokens
        tokens = await page.evaluate("""
        (() => {
            const tokens = {};
            document.querySelectorAll('meta[name*="csrf" i], meta[name*="token" i]').forEach(m => {
                if (m.content) tokens[m.name] = m.content;
            });
            document.querySelectorAll('input[type=hidden]').forEach(i => {
                if (i.name && i.value) tokens[i.name] = i.value;
            });
            return {tokens, cookies_present: document.cookie.length > 0};
        })()
        """)
        
        # 2. Fire request in-page (auto-attaches everything)
        result = await page.evaluate("""
        async ([url, method, extraHeaders, body]) => {
            const opts = {method, credentials: 'include', headers: extraHeaders || {}};
            if (body) opts.body = body;
            try {
                const resp = await fetch(url, opts);
                const text = await resp.text();
                const rHeaders = {};
                resp.headers.forEach((v, k) => rHeaders[k.toLowerCase()] = v);
                return {
                    status: resp.status,
                    status_text: resp.statusText,
                    headers: rHeaders,
                    body_preview: text.slice(0, 2000),
                    final_url: resp.url
                };
            } catch (e) {
                return {error: "CORS_OR_NETWORK_ERROR: " + e.toString(), hint: "Ensure the URL host matches the current tab origin."};
            }
        }
        """, [url, method.upper(), headers or {}, body])
        
        # 3. Validation — tell the agent what might be missing
        validation = {"request_ok": True, "issues": []}
        status = result.get("status", 0)
        body_preview = str(result.get("body_preview", "")).lower()
        
        if result.get("error"):
            validation["request_ok"] = False
            validation["issues"].append({"code": "FETCH_FAILED", "detail": result["error"]})
        elif status == 403:
            validation["request_ok"] = False
            validation["issues"].append({
                "code": "WAF_OR_AUTH_BLOCKED",
                "suggestion": "Run detect_waf() to identify WAF. If Imperva/Cloudflare, try payload_library() with obfuscation or http_request_cffi()."
            })
        elif status == 401:
            validation["request_ok"] = False
            validation["issues"].append({
                "code": "AUTH_MISSING",
                "available_tokens": list(tokens.get("tokens", {}).keys()),
                "suggestion": "Add Authorization/Bearer header manually or re-login."
            })
        elif status == 419 or "csrf" in body_preview and "invalid" in body_preview:
            validation["request_ok"] = False
            validation["issues"].append({
                "code": "CSRF_MISMATCH",
                "available_tokens": tokens.get("tokens", {}),
                "suggestion": "Add X-CSRF-Token header from available_tokens."
            })
        elif status == 400:
            validation["issues"].append({
                "code": "BAD_REQUEST",
                "suggestion": "Check body format — may need Content-Type header."
            })
        elif status == 404:
            validation["issues"].append({
                "code": "ENDPOINT_NOT_FOUND",
                "suggestion": "URL may be wrong or requires different method."
            })
        elif status >= 200 and status < 300:
            validation["request_ok"] = True
            validation["note"] = f"Success ({status}). Response received."
        
        return _dumps_capped({
            "request": {"url": url, "method": method.upper(), "headers": headers, "body": body[:200] if body else None},
            "response": result,
            "auto_attached_tokens": list(tokens.get("tokens", {}).keys()) if use_browser_context else [],
            "validation": validation if auto_validate else "disabled",
            "sent_via": "in-page fetch (auto cookies + CSRF)" if use_browser_context else "direct"
        }, 200000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


@mcp.tool()
async def http_request_cffi(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    impersonate: str = "firefox133",  # chrome120 gets 403 on Cloudflare; firefox133 works
    cookies: Optional[Dict[str, str]] = None,
    verify_ssl: bool = False,
) -> str:
    """
    HTTP request with curl-cffi — spoofs TLS/JA3 fingerprint of real browser.
    Bypasses Cloudflare/DataDome at NETWORK level when Playwright is blocked.
    impersonate options: chrome120, chrome110, firefox133, safari17_0, edge101
    """
    try:
        check_url(url, "http_request_cffi")
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return json.dumps({"error": "curl-cffi not installed. Run: pip install curl-cffi --break-system-packages"})
    
    try:
        resp = cffi_requests.request(
            method=method.upper(),
            url=url,
            headers=headers or {},
            data=body,
            impersonate=impersonate,
            cookies=cookies,
            timeout=30,
            allow_redirects=True,
            verify=verify_ssl,   # False by default for self-signed pentest targets;
                            # set verify_ssl=True to actually test certificate validation
        )
        
        # Detect if WAF blocked
        blocked = resp.status_code in (403, 429) or "blocked" in resp.text.lower()[:500]
        
        return _dumps_capped({
            "status": resp.status_code,
            "status_text": resp.reason,
            "headers": dict(resp.headers),
            "body_preview": resp.text[:3000],
            "final_url": resp.url,
            "tls_impersonated_as": impersonate,
            "likely_waf_blocked": blocked,
            "note": "If blocked, try impersonate='firefox133' or combine with smart_replay() in-page fetch."
        }, 200000)
    except Exception as e:
        return json.dumps({"error": type(e).__name__ + ": " + str(e)[:300]})


# ----------------- SHUTDOWN -----------------

async def _safe_shutdown():
    try:
        if getattr(mgr, "_pw", None) is not None:
            # Room for shutdown()'s own per-step timeouts (pages + contexts +
            # browsers + playwright driver), otherwise the outer wait_for
            # pre-empts them and we are back to a cancelled teardown.
            await asyncio.wait_for(mgr.shutdown(), timeout=40)
    except Exception:
        pass
    os._exit(0)


def _shutdown_signal(signum, frame):
    """Shut the browser down on the loop it was created on.

    Playwright's async objects are bound to the loop they were started on, so
    building a NEW event loop in a worker thread and calling ctx.close() /
    browser.close() / pw.stop() from it risks a loop-affinity error or a hang
    (the old code needed a 10s hard-exit watchdog for exactly that reason).
    The signal handler runs in the main thread, so schedule the coroutine back
    onto the running loop instead.
    """
    import threading
    import time as _time

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(_safe_shutdown(), loop)
        except Exception:
            pass
    else:
        # No running loop: use this thread's own loop rather than creating a
        # second one (Playwright objects are bound to their loop).
        try:
            asyncio.run(_safe_shutdown())
        except Exception:
            pass

    # Force exit if the graceful path hangs
    threading.Thread(target=lambda: (_time.sleep(12), os._exit(1)),
                     daemon=True).start()


_apply_cross_cutting_wrappers()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="netwala MCP server")
    parser.add_argument(
        "--transport", "-t",
        default="streamable-http",
        choices=["stdio", "streamable-http", "sse"],
        help="MCP transport (default: streamable-http for Termux↔PRoot)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all registered tools and exit."
    )
    parser.add_argument(
        "--host", default=MCP_HOST,
        help=(f"Bind host (default: {MCP_HOST} = loopback only, SAFE). "
              f"Use --host 0.0.0.0 to expose on WiFi/LAN. "
              f"WARNING: 0.0.0.0 has NO auth — anyone on your WiFi can control the browser."))
    parser.add_argument(
        "--port", type=int, default=MCP_PORT,
        help=f"MCP server port (default: {MCP_PORT}). "
             f"Only change if running multiple instances.")
    parser.add_argument(
        "--headed", action="store_true",
        help=("Run headful. DEFAULT is headed (NETWALA_HEADED=1) under Xvfb: "
              "headless Firefox has NO WebGL, which is a strong bot signal. "
              "Use --headless to force headless."))
    parser.add_argument("--headless", action="store_true", default=None)
    args = parser.parse_args()
    # --headed / --headless override the env default. With NEITHER flag the
    # browser_manager default applies (NETWALA_HEADED, headed/Xvfb by default).
    # Previously the flag was computed and never applied, and a missing flag
    # forced headless.
    if args.headed:
        mgr._headless = False
    elif args.headless:
        mgr._headless = True
    # --list handler: print all tools and exit
    if args.list:
        try:
            tools = mcp._tool_manager._tools if hasattr(mcp, "_tool_manager") else {}
            if not tools:
                print("No tools registered.")
                import sys as _sys
                _sys.exit(0)
            print(f"Netwala MCP - {len(tools)} Custom Tools")
            print("=" * 70)
            categories = {
                "Navigation": ["navigate", "evaluate_javascript", "get_page_snapshot", "list_browser_targets", "close_tab"],
                "Vision": ["capture_screenshot", "observe", "get_visual_navigation_map", "visual_click_by_id", "take_element_screenshot", "scroll_and_capture"],
                "Interaction": ["click_by_selector", "click_by_text", "click_by_role", "type_text", "press_keys", "hover", "select_option", "set_file_input_files", "mouse_click_human_like"],
                "Recon": ["detect_tech", "check_security_headers", "check_robots", "check_sitemap", "extract_links", "crawl_site", "extract_forms", "extract_table"],
                "WAF": ["detect_waf", "payload_library", "smart_replay", "http_request_cffi", "intercept_request", "stop_intercept", "replay_request", "get_dom_diff"],
                "Network": ["capture_har", "capture_network_traffic", "get_response_body", "capture_websocket", "wait_for_response"],
                "State": ["get_cookies", "set_cookies", "save_storage_state", "load_storage_state", "clear_site_data"],
                "Waits": ["wait_for_selector", "wait_for_navigation", "wait_for_network_idle", "wait_for_element_count"],
                "Spoofing": ["spawn_subagent_environment", "destroy_subagent_environment", "emulate_device", "batch_execute_across_agents"],
                "Misc": ["server_boot", "scroll_page", "handle_next_dialog", "switch_to_frame", "run_wcag_accessibility_audit"],
            }
            categorized = set()
            for cat_name, tool_names in categories.items():
                matched = [n for n in tool_names if n in tools]
                if not matched:
                    continue
                print()
                print(f"[{cat_name}]")
                for name in matched:
                    tool_obj = tools[name]
                    desc = (getattr(tool_obj, "description", "") or "").strip()
                    desc = desc.replace(chr(10), " ")[:70]
                    print(f"  {name:<32s} {desc}")
                    categorized.add(name)
            uncategorized = [n for n in tools if n not in categorized]
            if uncategorized:
                print()
                print("[Other]")
                for name in sorted(uncategorized):
                    tool_obj = tools[name]
                    desc = (getattr(tool_obj, "description", "") or "").strip()
                    desc = desc.replace(chr(10), " ")[:70]
                    print(f"  {name:<32s} {desc}")
            print()
            print(f"Total: {len(tools)} custom (+ 4 MCP meta = {len(tools) + 4})")
            import sys as _sys
            _sys.exit(0)
        except Exception as e:
            print(f"Error: {e}")
            import sys as _sys
            _sys.exit(1)

    # ---- startup hygiene: drop profile dirs leaked by abrupt kills ----
    try:
        import glob as _glob, shutil as _shutil
        _n = 0
        for _d in _glob.glob("/tmp/playwright_firefoxdev_profile-*"):
            try:
                _shutil.rmtree(_d, ignore_errors=True)
                _n += 1
            except Exception:
                pass
        if _n:
            print(f"[cleanup] removed {_n} stale Playwright profile dir(s) from /tmp")
    except Exception:
        pass

    def _atexit_cleanup():
        try:
            if getattr(mgr, "_pw", None) is not None:
                asyncio.run(asyncio.wait_for(mgr.shutdown(), timeout=6))
        except Exception:
            pass
    try:
        import atexit as _atexit
        _atexit.register(_atexit_cleanup)
    except Exception:
        pass

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    signal.signal(signal.SIGINT, _shutdown_signal)
    signal.signal(signal.SIGTERM, _shutdown_signal)

    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            # Lazy boot — first tool call pe Firefox launch hoga
            print(
                f"[netwala] MCP listening on http://{args.host}:{args.port}{MCP_PATH} "
                f"(transport={args.transport}, engine=firefox)",
                file=sys.stderr,
                flush=True,
            )
            mcp.run(transport=args.transport)
    except Exception as e:
        sys.stderr.write(f"\n[!] Server crashed: {e}\n")
        os._exit(1)
