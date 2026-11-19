"""
Persistent browser manager for netwala.
Handles browser lifecycle, context pool, page registry, mouse state.
Firefox (Juggler) only — Chromium was removed (ARM64 + PRoot incompatibility).
"""
import asyncio
import json as _json
import os as _os
import subprocess as _subprocess
import time as _time
import uuid
from typing import Optional, Dict, Tuple, Literal
from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Playwright
)

EngineName = Literal["firefox"]  # Chromium removed (ARM64+PRoot incompatibility)

_RFP_ENABLED = _os.environ.get("NETWALA_FF_RFP", "false").lower() in ("1", "true", "yes")

# Headed mode (Xvfb) is the DEFAULT: headless Firefox has NO WebGL at all, which
# is one of the strongest bot signals there is. NETWALA_HEADED=0 forces headless.
_HEADLESS = _os.environ.get("NETWALA_HEADED", "1").lower() in ("0", "false", "no")
_DEFAULT_DISPLAY = _os.environ.get("NETWALA_DISPLAY", ":99")

# locale -> a timezone that matches it, so tz and locale never contradict.
LOCALE_TZ = {
    "en": "America/New_York", "en-US": "America/New_York", "en-GB": "Europe/London",
    "en-CA": "America/Toronto", "en-AU": "Australia/Sydney", "en-IN": "Asia/Kolkata",
    "en-NZ": "Pacific/Auckland", "en-ZA": "Africa/Johannesburg",
    "fr": "Europe/Paris", "fr-FR": "Europe/Paris", "de": "Europe/Berlin", "de-DE": "Europe/Berlin",
    "es": "Europe/Madrid", "es-ES": "Europe/Madrid", "es-MX": "America/Mexico_City",
    "it": "Europe/Rome", "it-IT": "Europe/Rome", "nl": "Europe/Amsterdam", "nl-NL": "Europe/Amsterdam",
    "pt": "Europe/Lisbon", "pt-BR": "America/Sao_Paulo", "pt-PT": "Europe/Lisbon",
    "ja": "Asia/Tokyo", "ja-JP": "Asia/Tokyo", "ko": "Asia/Seoul", "ko-KR": "Asia/Seoul",
    "zh": "Asia/Shanghai", "zh-CN": "Asia/Shanghai", "zh-TW": "Asia/Taipei",
    "ru": "Europe/Moscow", "ru-RU": "Europe/Moscow", "pl": "Europe/Warsaw", "pl-PL": "Europe/Warsaw",
    "tr": "Europe/Istanbul", "tr-TR": "Europe/Istanbul",
    "sv": "Europe/Stockholm", "sv-SE": "Europe/Stockholm",
}
DEFAULT_LOCALE = _os.environ.get("NETWALA_LOCALE", "en-US")
DEFAULT_TZ = _os.environ.get("NETWALA_TIMEZONE") or LOCALE_TZ.get(DEFAULT_LOCALE, "America/New_York")
DEFAULT_VIEWPORT = {"width": 1366, "height": 768}   # matches the Xvfb screen

# Half-spoofing the GPU is worse than none: the renderer string would say
# "NVIDIA" while every WebGL parameter still comes from llvmpipe. Off by
# default; enable only if you accept that inconsistency.
GPU_SPOOF = _os.environ.get("NETWALA_GPU_SPOOF", "0").lower() in ("1", "true", "yes")
GPU_VENDOR = _os.environ.get("NETWALA_GPU_VENDOR", "NVIDIA Corporation")
GPU_RENDERER = _os.environ.get(
    "NETWALA_GPU_RENDERER",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
)


def _locale_languages(locale: str) -> list:
    """['en-US'] -> ['en-US','en'];  ['fr-FR'] -> ['fr-FR','fr','en']."""
    base = locale.split("-")[0]
    langs = [locale]
    if base != locale:
        langs.append(base)
    if base != "en":
        langs.append("en")
    seen, out = set(), []
    for l in langs:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def _spoof_script(languages=None, oscpu=None, platform=None, dpr=None,
                  touch_points=None, has_touch=False, gpu=None) -> str:
    """Init script whose injected getters LOOK native.

    Anti-bot scripts read Function.prototype.toString / property descriptors to
    spot Object.defineProperty hooks, so every getter we install also carries a
    native-looking toString.
    """
    out = [
        "(() => {",
        "const _nw_def = (obj, prop, val, name) => { try {",
        " const g = () => val;",
        " Object.defineProperty(g, 'toString', {value: () => 'function ' + name + '() { [native code] }', configurable: true});",
        " Object.defineProperty(obj, prop, {get: g, configurable: true});",
        "} catch (e) {} };",
    ]
    if languages:
        out.append("let _nw_l = " + _json.dumps(list(languages)) + "; _nw_l = _nw_l.slice(); Object.freeze(_nw_l);")
        out.append("_nw_def(navigator, 'languages', _nw_l, 'languages');")
        out.append("_nw_def(navigator, 'language', " + _json.dumps(languages[0]) + ", 'language');")
    if oscpu:
        out.append("_nw_def(navigator, 'oscpu', " + _json.dumps(oscpu) + ", 'oscpu');")
    if platform:
        out.append("_nw_def(navigator, 'platform', " + _json.dumps(platform) + ", 'platform');")
    if dpr:
        out.append("_nw_def(window, 'devicePixelRatio', " + repr(dpr) + ", 'devicePixelRatio');")
    if touch_points is not None:
        out.append("_nw_def(navigator, 'maxTouchPoints', " + repr(touch_points) + ", 'maxTouchPoints');")
    if has_touch:
        # Firefox does NOT expose ontouchstart/ontouchmove even with
        # Playwright's native has_touch=True (verified: 'ontouchstart' in
        # window stays False). A real touch device has the property PRESENT
        # with a null handler, so define it — that is what bot checks read.
        out.append(
            "try { Object.defineProperty(window, 'ontouchstart', {value: null, configurable: true});"
            " Object.defineProperty(window, 'ontouchmove', {value: null, configurable: true}); } catch (e) {}")
    if gpu:
        vend, rend = gpu
        out.append(
            "try { if (typeof WebGLRenderingContext !== 'undefined') {"
            " const _nw_o = WebGLRenderingContext.prototype.getParameter;"
            " const _nw_f = function (p) {"
            "  if (p === 37445) return " + _json.dumps(vend) + ";"
            "  if (p === 37446) return " + _json.dumps(rend) + ";"
            "  return _nw_o.call(this, p);"
            " };"
            " Object.defineProperty(_nw_f, 'toString', {value: () => 'function getParameter() { [native code] }', configurable: true});"
            " WebGLRenderingContext.prototype.getParameter = _nw_f;"
            "} } catch (e) {}"
        )
    out.append("})();")
    return "".join(out)

FIREFOX_STEALTH_PREFS = {
    "dom.webdriver.enabled": False,
    "privacy.resistFingerprinting": _RFP_ENABLED,   # env-controlled (default OFF = timezone spoof works)
    # Tracking Protection blocks the WAF's OWN script domains, which makes the
    # interstitial say "disable any ad blocker". Keep it off for a pentest browser.
    "privacy.trackingprotection.enabled": False,
    "pdfjs.disabled": True,
    # Software GL: headed + these prefs gives a real llvmpipe WebGL context.
    "webgl.force-enabled": True,
    "webgl.disabled": False,
    "webgl.enable-webgl2": True,
    "webgl.software-render": True,
}


def _display_ready(display: str) -> bool:
    """True only if something is actually LISTENING on `display`.

    A stale /tmp/.X11-unix/X<n> socket left behind by a killed Xvfb used to make
    this return True, so `ensure_xvfb` never restarted the server and the
    browser silently booted headless (no WebGL at all). The socket is now
    PROBED, not just stat()ed.
    """
    import socket as _socket
    num = display.lstrip(":").split(".")[0]
    _sock = f"/tmp/.X11-unix/X{num}"
    if _os.path.exists(_sock):
        try:
            _s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            _s.settimeout(1.0)
            _s.connect(_sock)
            _s.close()
            return True
        except Exception:
            return False          # stale socket: nobody is listening
    # No socket at all. A live Xvfb on some display is still a reason to skip
    # starting a second one — match the process NAME, never the command line
    # (`pgrep -f "Xvfb :99"` matches this launcher's own shell).
    try:
        out = _subprocess.run(["pgrep", "-x", "Xvfb"],
                              capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return True
    except Exception:
        pass
    return False


_XVFB_PROC = None          # module-level handle; shutdown() terminates it


def ensure_xvfb(display: str = _DEFAULT_DISPLAY) -> bool:
    """Make sure an X server is running on `display`. Best effort.

    Safe to call repeatedly; clears a stale lock left by a killed Xvfb.
    """
    if _display_ready(display):
        return True
    num = display.lstrip(":").split(".")[0]
    for stale in (f"/tmp/.X{num}-lock", f"/tmp/.X11-unix/X{num}"):
        try:
            if _os.path.exists(stale):
                _os.remove(stale)
        except Exception:
            pass
    try:
        for _attempt in range(3):
            global _XVFB_PROC
            _XVFB_PROC = _subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1366x768x24", "-nolisten", "tcp"],
                stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL)
            for _ in range(12):                     # up to ~3.6s
                _time.sleep(0.3)
                if _display_ready(display):
                    return True
            # still not up: clear any lock it left and retry
            for stale in (f"/tmp/.X{num}-lock", f"/tmp/.X11-unix/X{num}"):
                try:
                    if _os.path.exists(stale):
                        _os.remove(stale)
                except Exception:
                    pass
        return False
    except Exception:
        return False

STEALTH_JS = """
(() => {
    try { delete Navigator.prototype.webdriver; } catch(e) {}
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}

    // Deterministic per-session canvas noise
    const SESSION_SEED = window.__netwala_canvas_seed ?? Math.floor(Math.random() * 1000);
    const origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) {
        const ctx = origGetContext.call(this, type, ...args);
        if (type === '2d' && ctx && !ctx.__patched) {
            const origFillText = ctx.fillText.bind(ctx);
            ctx.fillText = function(...a) {
                ctx.save();
                ctx.globalAlpha = 0.0001;
                ctx.fillRect(SESSION_SEED % 10, (SESSION_SEED * 7) % 10, 1, 1);
                ctx.restore();
                return origFillText(...a);
            };
            ctx.__patched = true;
        }
        return ctx;
    };

    // Permissions consistency
    if (navigator.permissions && navigator.permissions.query) {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (p) => {
            if (p.name === 'notifications')
                return Promise.resolve({ state: Notification.permission });
            return origQuery(p);
        };
    }
})();
"""

class BrowserManager:
    def __init__(self):
        self._pw: Optional[Playwright] = None
        self._browsers: Dict[str, Browser] = {}
        self._contexts: Dict[str, BrowserContext] = {}
        self._pages: Dict[str, Page] = {}
        self._page_contexts: Dict[str, str] = {}
        self._last_mouse: Tuple[float, float] = (400, 300)
        self._start_lock = asyncio.Lock()
        self._stealth_enabled = True
        self._default_engine: EngineName = "firefox"
        self._headless: bool = _HEADLESS

    @property
    def last_mouse(self) -> Tuple[float, float]:
        return self._last_mouse

    @last_mouse.setter
    def last_mouse(self, pos: Tuple[float, float]):
        self._last_mouse = pos

    async def start(self, engine: EngineName = "firefox", headless: Optional[bool] = None):
        self._pw = await async_playwright().start()
        self._default_engine = engine
        self._headless = self._headless if headless is None else headless
        await self._launch(engine, self._headless)

    async def _launch(self, engine: EngineName, headless: bool) -> Browser:
        # Chromium removed — only Firefox supported (ARM64+PRoot environment)
        if engine != "firefox":
            raise ValueError(
                f"Unsupported engine: '{engine}'. "
                f"Netwala only supports Firefox (Chromium incompatible with ARM64+PRoot). "
                f"Use engine='firefox' or omit the parameter."
            )
        launch_env = dict(_os.environ)
        if not headless:
            # Always verify/start the X server: a stale DISPLAY env (":99" is
            # exported by start_netwala.sh) with no live Xvfb kills Firefox.
            disp = launch_env.get("DISPLAY") or _DEFAULT_DISPLAY
            if ensure_xvfb(disp):
                launch_env["DISPLAY"] = disp
                launch_env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
                launch_env.setdefault("MOZ_X11_EGL", "1")
            else:
                headless = True
        try:
            _prefs = dict(FIREFOX_STEALTH_PREFS) if self._stealth_enabled else {}
            # Playwright's service_workers="block" is a no-op on Firefox
            # (verified: an SW still registered). This opt-in pref actually
            # disables SW — off by default because `navigator.serviceWorker`
            # becoming undefined is itself a detectable tell.
            if _os.environ.get("NETWALA_DISABLE_SERVICE_WORKERS", "0").strip().lower() in ("1", "true", "yes"):
                _prefs["dom.serviceWorkers.enabled"] = False
            browser = await self._pw.firefox.launch(
                headless=headless,
                firefox_user_prefs=_prefs,
                env=launch_env,
            )
        except Exception:
            if headless:
                raise
            # last resort: a headless browser still beats a dead one
            headless = True
            browser = await self._pw.firefox.launch(
                headless=True,
                firefox_user_prefs=_prefs,
                env=dict(_os.environ),
            )
        self._headless = headless
        self._browsers[engine] = browser
        return browser

    async def ensure_started(
        self,
        engine: Optional[EngineName] = None,
        headless: Optional[bool] = None,
    ) -> "BrowserManager":
        """Lazy boot — thread-safe with lock (fixes #4 race)."""
        async with self._start_lock:
            if self._pw is None:
                await self.start(engine or self._default_engine, headless)
        return self

    async def get_browser(self, engine: Optional[EngineName] = None) -> Browser:
        engine = engine or self._default_engine
        b = self._browsers.get(engine)
        if b is None or not b.is_connected():
            b = await self._launch(engine, self._headless)
        return b

    async def new_context(
        self,
        engine: Optional[EngineName] = None,
        proxy: Optional[dict] = None,
        har_path: Optional[str] = None,
        viewport: Optional[dict] = None,
        user_agent: Optional[str] = None,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        geolocation: Optional[dict] = None,
        storage_state: Optional[str] = None,
        cookies: Optional[list] = None,
        extra_http_headers: Optional[dict] = None,
        bypass_csp: bool = False,
        device_scale_factor: Optional[float] = None,
        has_touch: bool = False,
        touch_points: Optional[int] = None,
        oscpu: Optional[str] = None,
        platform: Optional[str] = None,
        languages: Optional[list] = None,
        webgl_gpu: bool = GPU_SPOOF,
    ) -> Tuple[str, BrowserContext]:
        await self.ensure_started(engine)
        browser = await self.get_browser(engine)
        kwargs = {}
        if proxy:
            kwargs["proxy"] = proxy
        if har_path:
            kwargs["record_har_path"] = har_path
            kwargs["record_har_mode"] = "full"
            kwargs["record_har_content"] = "embed"
        # A service worker can answer its own fetch() from cache, which
        # page.route() can never see. Block SWs so the interceptor and the
        # network tools see every request.
        kwargs.setdefault("service_workers", "block")
        if viewport:
            kwargs["viewport"] = viewport
        elif "viewport" not in kwargs:
            # screen is 1366x768 (Xvfb); a different default viewport makes
            # screen != innerWidth, a classic bot tell.
            kwargs["viewport"] = dict(DEFAULT_VIEWPORT)
        if user_agent:
            kwargs["user_agent"] = user_agent
        locale = locale or DEFAULT_LOCALE
        if not timezone_id:
            # never let locale and timezone contradict (en-US + UTC is a classic tell)
            timezone_id = LOCALE_TZ.get(locale) or DEFAULT_TZ
        kwargs["locale"] = locale
        kwargs["timezone_id"] = timezone_id
        if geolocation:
            kwargs["geolocation"] = geolocation
            kwargs["permissions"] = kwargs.get("permissions", []) + ["geolocation"]
        if storage_state:
            kwargs["storage_state"] = storage_state
        if extra_http_headers:
            kwargs["extra_http_headers"] = extra_http_headers
        kwargs["bypass_csp"] = bypass_csp
        if device_scale_factor:
            kwargs["device_scale_factor"] = device_scale_factor
        if has_touch:
            kwargs["has_touch"] = True
        ctx = await browser.new_context(**kwargs)
        ctx_id = f"ctx_{uuid.uuid4().hex[:8]}"
        # Auto-register popups (target=_blank tabs, window.open) with the SAME
        # ctx_id. Passing None left them unmapped, so destroy_context() never
        # purged them and list_browser_targets() reported context_id: None.
        def _on_new_page(new_page):
            try:
                # Register async without blocking
                import asyncio as _a
                _a.create_task(self.register_page(new_page, ctx_id))
            except Exception:
                pass
        try:
            ctx.on("page", _on_new_page)
        except Exception:
            pass
        if cookies:
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                pass
        if self._stealth_enabled:
            # Generate seed ONCE per context (not per navigation)
            import random as _rnd
            _canvas_seed = _rnd.randint(0, 999)
            await ctx.add_init_script(f"window.__netwala_canvas_seed = {_canvas_seed};")
            await ctx.add_init_script(STEALTH_JS)
        # Device emulation: Firefox resets device_scale_factor after the first
        # navigation and cannot set maxTouchPoints natively, so both are
        # re-applied on every document via an init script. UA / platform /
        # oscpu / languages must all agree — a mismatch is an instant tell.
        _gpu = (GPU_VENDOR, GPU_RENDERER) if webgl_gpu else None
        await ctx.add_init_script(_spoof_script(
            languages=languages or _locale_languages(locale),
            oscpu=oscpu, platform=platform,
            dpr=device_scale_factor, touch_points=touch_points,
            has_touch=has_touch, gpu=_gpu))
        self._contexts[ctx_id] = ctx
        return ctx_id, ctx

    async def register_page(self, page: Page, context_id: Optional[str] = None) -> str:
        tid = f"tab_{uuid.uuid4().hex[:8]}"
        self._pages[tid] = page
        if context_id:
            self._page_contexts[tid] = context_id
        # Auto-purge when page is closed (by JS, popup close, crash, etc.)
        def _on_close(_=None, _tid=tid):
            self._pages.pop(_tid, None)
            self._page_contexts.pop(_tid, None)
        try:
            page.on("close", _on_close)
        except Exception:
            pass
        return tid

    def get_page_by_id(self, target_id: str) -> Optional[Page]:
        pg = self._pages.get(target_id)
        if pg and not pg.is_closed():
            return pg
        return None

    async def get_page(
        self,
        target_id: Optional[str] = None,
        engine: Optional[EngineName] = None,
    ) -> Tuple[Page, str]:
        await self.ensure_started(engine)
        if target_id:
            pg = self.get_page_by_id(target_id)
            if pg:
                return pg, target_id
            raise ValueError(f"Target {target_id} not found or closed")
        # find any open page
        for pid, pg in list(self._pages.items()):
            if not pg.is_closed():
                return pg, pid
        # create new context + page
        ctx_id, ctx = await self.new_context(engine)
        pg = await ctx.new_page()
        tid = await self.register_page(pg, ctx_id)
        return pg, tid

    async def _drop_intercepts(self, tid: str, pg) -> int:
        """Remove this tab's armed intercept handlers and registry entry."""
        reg = getattr(self, "_intercepts", None)
        if not reg:
            return 0
        entries = list(reg.get(tid, []))
        removed = 0
        for entry in entries:
            try:
                _pat, _h = entry
            except Exception:
                _pat, _h = None, entry
            try:
                if _pat is not None:
                    await pg.unroute(_pat, handler=_h)
                else:
                    await pg.unroute(handler=_h)
                removed += 1
            except Exception:
                pass
        reg.pop(tid, None)
        return removed

    async def destroy_context(self, ctx_id: str) -> bool:
        ctx = self._contexts.get(ctx_id)
        if not ctx:
            return False
        # Drop intercept handlers for every tab in this context BEFORE closing,
        # so the registry cannot keep stale entries (and stale routes) behind.
        for tid in [t for t, c in list(self._page_contexts.items()) if c == ctx_id]:
            pg = self._pages.get(tid)
            if pg is not None:
                try:
                    await self._drop_intercepts(tid, pg)
                except Exception:
                    pass
        try:
            await asyncio.wait_for(ctx.close(), timeout=15)
        except Exception:
            return False  # Keep in registry so caller can retry
        self._contexts.pop(ctx_id, None)
        for tid in [t for t, c in list(self._page_contexts.items()) if c == ctx_id]:
            self._pages.pop(tid, None)
            self._page_contexts.pop(tid, None)
        return True

    async def close_page(self, target_id: str) -> bool:
        pg = self._pages.get(target_id)
        if not pg:
            return False
        try:
            await self._drop_intercepts(target_id, pg)
        except Exception:
            pass
        try:
            await asyncio.wait_for(pg.close(), timeout=10)
        except Exception:
            return False
        self._pages.pop(target_id, None)
        self._page_contexts.pop(target_id, None)
        return True

    def list_contexts(self) -> Dict[str, dict]:
        out = {}
        for cid, ctx in self._contexts.items():
            pages = [t for t, c in self._page_contexts.items() if c == cid]
            out[cid] = {"pages": pages}
        return out

    def list_pages(self) -> Dict[str, dict]:
        out = {}
        for tid, pg in self._pages.items():
            if pg.is_closed():
                continue
            try:
                out[tid] = {
                    "url": pg.url,
                    "context_id": self._page_contexts.get(tid),
                }
            except Exception:
                pass
        return out

    async def shutdown(self):
        # Each step gets its own timeout. A bare `await ctx.close()` that hangs
        # would be cancelled mid-flight by the caller's timeout and leave
        # Firefox (and the Playwright driver) orphaned.
        # Close pages explicitly first: a context close already does it, but an
        # explicit pass means a single stuck page cannot hold up the rest.
        for _p in list(self._pages.values()):
            try:
                await asyncio.wait_for(_p.close(), timeout=4)
            except Exception:
                pass
        self._pages.clear()
        for ctx in list(self._contexts.values()):
            for _t in (6, 3):
                try:
                    await asyncio.wait_for(ctx.close(), timeout=_t)
                    break
                except Exception:
                    continue
        self._contexts.clear()
        self._pages.clear()
        self._page_contexts.clear()
        for b in list(self._browsers.values()):
            for _t in (6, 3):
                try:
                    await asyncio.wait_for(b.close(), timeout=_t)
                    break
                except Exception:
                    continue
        self._browsers.clear()
        if self._pw:
            try:
                await asyncio.wait_for(self._pw.stop(), timeout=6)
            except Exception:
                pass
            self._pw = None
        self._kill_xvfb()

    def _kill_xvfb(self):
        global _XVFB_PROC
        _p = _XVFB_PROC
        _XVFB_PROC = None
        if _p is None:
            return
        try:
            _p.terminate()
            try:
                _p.wait(timeout=4)
            except Exception:
                _p.kill()
        except Exception:
            pass

mgr = BrowserManager()
