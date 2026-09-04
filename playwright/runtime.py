from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from SYS.config import get_nested_config_value as _get_nested
from SYS.logger import debug

try:
    from playwright.sync_api import TimeoutError as _SyncPlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - handled at runtime
    sync_playwright = None

    class PlaywrightTimeoutError(RuntimeError):
        """Fallback timeout type when Playwright is unavailable."""

else:
    PlaywrightTimeoutError = _SyncPlaywrightTimeoutError

# Re-export for consumers (e.g. cmdlets catching navigation timeouts)
__all__ = [
    "PlaywrightTimeoutError",
    "PlaywrightTool",
    "PlaywrightDefaults",
    "PlaywrightDownloadResult",
]


def _resolve_out_dir(arg_outdir: Optional[Union[str, Path]]) -> Path:
    """Resolve an output directory using config when possible."""
    if arg_outdir:
        p = Path(arg_outdir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    try:
        from SYS.config import load_config, resolve_output_dir

        cfg = load_config()
        p = resolve_output_dir(cfg)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to create resolved output dir %s", p)
        return p
    except Exception:
        return Path(tempfile.mkdtemp(prefix="pwdl_"))


def _find_filename_from_cd(cd: str) -> Optional[str]:
    if not cd:
        return None
    m = re.search(r"filename\*?=(?:UTF-8''\s*)?\"?([^\";]+)\"?", cd)
    if m:
        return m.group(1)
    return None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class PlaywrightDefaults:
    browser: str = "chromium"  # chromium|firefox|webkit
    headless: bool = True
    user_agent: str = USER_AGENT
    viewport_width: int = 1920
    viewport_height: int = 1080
    navigation_timeout_ms: int = 90_000
    ignore_https_errors: bool = True
    screenshot_quality: int = 8
    ffmpeg_path: Optional[str] = None  # Path to ffmpeg executable; auto-detected if None


@dataclass(slots=True)
class PlaywrightDownloadResult:
    ok: bool
    path: Optional[Path] = None
    url: Optional[str] = None
    mode: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "path": str(self.path) if self.path else None,
            "url": self.url,
            "mode": self.mode,
            "error": self.error,
        }


class PlaywrightTool:
    """Small wrapper to standardize Playwright defaults and lifecycle.

    This is meant to keep cmdlets/providers from duplicating:
    - sync_playwright start/stop
    - browser launch/context creation
    - user-agent/viewport defaults
    - ffmpeg path resolution (for video recording)

        Config overrides (plugin.playwright keys):
            - plugin.playwright.browser="chromium"
            - plugin.playwright.headless=true
            - plugin.playwright.user_agent="..."
            - plugin.playwright.viewport_width=1280
            - plugin.playwright.viewport_height=1200
            - plugin.playwright.navigation_timeout_ms=90000
            - plugin.playwright.ignore_https_errors=true
            - plugin.playwright.screenshot_quality=8
            - plugin.playwright.ffmpeg_path="/path/to/ffmpeg"  (auto-detected if not set)
    
    FFmpeg resolution (in order):
      1. Config key: playwright.ffmpeg_path
      2. Environment variable: FFMPEG_PATH (global shared env)
      3. Environment variable: PLAYWRIGHT_FFMPEG_PATH (legacy)
      4. Project bundled: MPV/ffmpeg/bin/ffmpeg[.exe]
      5. System PATH: which ffmpeg
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config: Dict[str,
                           Any] = dict(config or {})
        self.defaults = self._load_defaults()

    def _load_defaults(self) -> PlaywrightDefaults:
        cfg = self._config
        defaults = PlaywrightDefaults()
        pw_block = _get_nested(cfg, "plugin", "playwright")
        if not isinstance(pw_block, dict):
            pw_block = {}

        def _get(name: str, fallback: Any) -> Any:
            val = pw_block.get(name)
            return fallback if val is None else val

        browser = str(_get("browser", defaults.browser)).strip().lower() or "chromium"
        if browser not in {"chromium",
                           "firefox",
                           "webkit"}:
            browser = "chromium"

        headless_raw = _get("headless", defaults.headless)
        headless = bool(headless_raw)

        # Resolve user_agent configuration. Support a 'custom' token which reads
        # the actual UA from `user_agent_custom` so the UI can offer a simple
        # dropdown without scattering long UA strings to users by default.
        ua_raw = _get("user_agent", None)
        if ua_raw is None:
            ua = defaults.user_agent
        else:
            ua_s = str(ua_raw).strip()
            if ua_s.lower() == "custom":
                ua_custom = _get("user_agent_custom", "")
                ua = str(ua_custom).strip() or defaults.user_agent
            else:
                ua = ua_s

        def _int(name: str, fallback: int) -> int:
            raw = _get(name, fallback)
            try:
                return int(raw)
            except Exception:
                return fallback

        vw = _int("viewport_width", defaults.viewport_width)
        vh = _int("viewport_height", defaults.viewport_height)
        nav_timeout = _int("navigation_timeout_ms", defaults.navigation_timeout_ms)
        screenshot_quality = max(1, min(10, _int("screenshot_quality", defaults.screenshot_quality)))

        ignore_https = bool(_get("ignore_https_errors", defaults.ignore_https_errors))

        # Try to find ffmpeg: config override, global env FFMPEG_PATH, legacy
        # PLAYWRIGHT_FFMPEG_PATH, bundled, then system. This allows Playwright to
        # use the shared ffmpeg path used by other tools (FFMPEG_PATH env).
        ffmpeg_path: Optional[str] = None
        config_ffmpeg = _get("ffmpeg_path", None)

        if config_ffmpeg:
            # User explicitly configured ffmpeg path
            candidate = str(config_ffmpeg).strip()
            if candidate and Path(candidate).exists():
                ffmpeg_path = candidate
            else:
                ffmpeg_path = None

        if not ffmpeg_path:
            # Prefer a global FFMPEG_PATH env var (shared by tools) before Playwright-specific one
            env_ffmpeg = os.environ.get("FFMPEG_PATH")
            if env_ffmpeg and Path(env_ffmpeg).exists():
                ffmpeg_path = env_ffmpeg

        if not ffmpeg_path:
            # Backward-compatible Playwright-specific env var
            env_ffmpeg2 = os.environ.get("PLAYWRIGHT_FFMPEG_PATH")
            if env_ffmpeg2 and Path(env_ffmpeg2).exists():
                ffmpeg_path = env_ffmpeg2
        
        if not ffmpeg_path:
            # Try to find bundled ffmpeg in the project (Windows-only, in MPV/ffmpeg/bin)
            try:
                repo_root = Path(__file__).resolve().parents[2]
                bundled_ffmpeg = repo_root / "MPV" / "ffmpeg" / "bin"
                if bundled_ffmpeg.exists():
                    ffmpeg_exe = bundled_ffmpeg / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                    if ffmpeg_exe.exists():
                        ffmpeg_path = str(ffmpeg_exe)
            except Exception:
                pass
        
        if not ffmpeg_path:
            # Try system ffmpeg if bundled not found
            system_ffmpeg = shutil.which("ffmpeg")
            if system_ffmpeg:
                ffmpeg_path = system_ffmpeg

        return PlaywrightDefaults(
            browser=browser,
            headless=headless,
            user_agent=ua,
            viewport_width=vw,
            viewport_height=vh,
            navigation_timeout_ms=nav_timeout,
            ignore_https_errors=ignore_https,
            screenshot_quality=screenshot_quality,
            ffmpeg_path=ffmpeg_path,
        )


    def require(self) -> None:
        """Ensure Playwright is present; raise a helpful RuntimeError if not."""
        try:
            assert sync_playwright is not None
        except Exception:
            raise RuntimeError(
                "playwright is required; install with: pip install playwright; then: playwright install"
            )

    def ffmpeg_available(self) -> bool:
        """Check if ffmpeg is available on the system."""
        return bool(self.defaults.ffmpeg_path)
    
    def require_ffmpeg(self) -> None:
        """Require ffmpeg to be available; raise a helpful error if not.
        
        This should be called before operations that need ffmpeg (e.g., video recording).
        """
        if not self.ffmpeg_available():
            raise RuntimeError(
                "ffmpeg is required but not found on your system.\n"
                "Install it using:\n"
                "  Windows: choco install ffmpeg (if using Chocolatey) or use the bundled version in MPV/ffmpeg\n"
                "  macOS: brew install ffmpeg\n"
                "  Linux: apt install ffmpeg (Ubuntu/Debian) or equivalent for your distribution\n"
                "\n"
                "Or set the PLAYWRIGHT_FFMPEG_PATH environment variable to point to your ffmpeg executable."
            )

    @contextlib.contextmanager
    def open_page(
        self,
        *,
        headless: Optional[bool] = None,
        user_agent: Optional[str] = None,
        viewport_width: Optional[int] = None,
        viewport_height: Optional[int] = None,
        ignore_https_errors: Optional[bool] = None,
        accept_downloads: bool = False,
        emulate_viewport: bool = True,
        start_maximized: bool = False,
    ) -> Iterator[Any]:
        """Context manager yielding a Playwright page with sane defaults."""
        self.require()

        h = self.defaults.headless if headless is None else bool(headless)
        ua = self.defaults.user_agent if user_agent is None else str(user_agent)
        vw = self.defaults.viewport_width if viewport_width is None else int(
            viewport_width
        )
        vh = self.defaults.viewport_height if viewport_height is None else int(
            viewport_height
        )
        ihe = (
            self.defaults.ignore_https_errors
            if ignore_https_errors is None else bool(ignore_https_errors)
        )

        # Support Playwright-native headers/user-agent.
        # If user_agent is unset/empty or explicitly set to one of these tokens,
        # we omit the user_agent override so Playwright uses its bundled Chromium UA.
        ua_value: Optional[str]
        ua_text = str(ua or "").strip()
        if not ua_text or ua_text.lower() in {"native",
                                              "playwright",
                                              "default"}:
            ua_value = None
        else:
            ua_value = ua_text

        pw = None
        browser = None
        context = None
        try:
            assert sync_playwright is not None
            pw = sync_playwright().start()

            browser_type = getattr(pw, self.defaults.browser, None)
            if browser_type is None:
                browser_type = pw.chromium

            launch_args = ["--disable-blink-features=AutomationControlled"]
            if bool(start_maximized) and not h:
                launch_args.append("--start-maximized")

            browser = browser_type.launch(
                headless=h,
                args=launch_args,
            )
            context_kwargs: Dict[str,
                                 Any] = {
                                     "ignore_https_errors": ihe,
                                     "accept_downloads": bool(accept_downloads),
                                 }
            if bool(emulate_viewport):
                context_kwargs["viewport"] = {
                    "width": vw,
                    "height": vh,
                }
            else:
                context_kwargs["no_viewport"] = True
            if ua_value is not None:
                context_kwargs["user_agent"] = ua_value

            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            yield page
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to close Playwright context")
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to close Playwright browser")
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to stop Playwright engine")

    def goto(self, page: Any, url: str) -> None:
        """Navigate with configured timeout."""
        try:
            page.goto(
                url,
                timeout=int(self.defaults.navigation_timeout_ms),
                wait_until="domcontentloaded"
            )
        except Exception:
            raise

    def download_file(
        self,
        url: str,
        *,
        selector: str = "form#dl_form button[type=submit]",
        out_dir: Optional[Union[str, Path]] = None,
        timeout_sec: int = 60,
        headless_first: bool = False,
        debug_mode: bool = False,
    ) -> PlaywrightDownloadResult:
        """Download a file by clicking a selector and capturing the response.

        The helper mirrors the standalone `scripts/playwright_fetch.py` logic
        and tries multiple click strategies (expect_download, tooltip continue,
        submitDL, JS/mouse click) to coax stubborn sites.
        """
        try:
            self.require()
        except Exception as exc:
            return PlaywrightDownloadResult(ok=False, error=str(exc))

        out_path_base = _resolve_out_dir(out_dir)
        timeout_ms = max(10_000, int(timeout_sec) * 1000 if timeout_sec is not None else int(self.defaults.navigation_timeout_ms))
        nav_timeout_ms = max(timeout_ms, int(self.defaults.navigation_timeout_ms))
        selector_timeout_ms = 10_000

        # Preserve legacy behaviour: headless_first=False tries headful then headless; True reverses the order.
        order = [True, False] if headless_first else [False, True]
        seen = set()
        modes = []
        for m in order:
            if m in seen:
                continue
            seen.add(m)
            modes.append(m)

        last_error: Optional[str] = None

        for mode in modes:
            try:
                if debug_mode:
                    debug(f"[playwright] download url={url} selector={selector} headless={mode} out_dir={out_path_base}")

                with self.open_page(headless=mode, accept_downloads=True) as page:
                    page.goto(url, wait_until="networkidle", timeout=nav_timeout_ms)
                    page.wait_for_selector(selector, timeout=selector_timeout_ms)
                    self._wait_for_block_clear(page, timeout_ms=6000)

                    el = page.query_selector(selector)

                    # 1) Direct click with expect_download
                    try:
                        with page.expect_download(timeout=timeout_ms) as dl_info:
                            if el:
                                el.click()
                            else:
                                page.click(selector)
                        dl = dl_info.value
                        filename = dl.suggested_filename or Path(dl.url).name or "download"
                        out_path = out_path_base / filename
                        dl.save_as(str(out_path))
                        return PlaywrightDownloadResult(ok=True, path=out_path, url=dl.url, mode="download")
                    except PlaywrightTimeoutError:
                        last_error = "download timeout"
                    except Exception as click_exc:
                        last_error = str(click_exc) or last_error

                    # 2) Tooltip continue flow
                    try:
                        btn = page.query_selector("#tooltip4 input[type=button]")
                        if btn:
                            btn.click()
                            with page.expect_download(timeout=timeout_ms) as dl_info:
                                if el:
                                    el.click()
                                else:
                                    page.click(selector)
                            dl = dl_info.value
                            filename = dl.suggested_filename or Path(dl.url).name or "download"
                            out_path = out_path_base / filename
                            dl.save_as(str(out_path))
                            return PlaywrightDownloadResult(ok=True, path=out_path, url=dl.url, mode="tooltip-download")
                    except Exception as tooltip_exc:
                        last_error = str(tooltip_exc) or last_error

                    # 3) Submit handler that respects tooltip flow
                    try:
                        page.evaluate("() => { try { submitDL(document.forms['dl_form'], 'tooltip4'); } catch (e) {} }")
                        resp = page.wait_for_response(
                            lambda r: r.status == 200 and any(k.lower() == 'content-disposition' for k in r.headers.keys()),
                            timeout=timeout_ms,
                        )
                        if resp:
                            out_path = self._save_response(resp, out_path_base)
                            if out_path:
                                return PlaywrightDownloadResult(ok=True, path=out_path, url=getattr(resp, "url", None), mode="response")
                    except Exception as resp_exc:
                        last_error = str(resp_exc) or last_error

                    # 4) JS/mouse click and capture response
                    try:
                        if el:
                            try:
                                page.evaluate("el => el.click()", el)
                            except Exception:
                                page.evaluate(f"() => document.querySelector('{selector}').click()")
                        else:
                            page.evaluate(f"() => document.querySelector('{selector}').click()")

                        if el:
                            try:
                                box = el.bounding_box()
                                if box:
                                    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                                    page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                            except Exception:
                                from SYS.logger import logger
                                logger.exception("Failed to perform mouse click for selector '%s'", selector)

                        resp = page.wait_for_response(
                            lambda r: r.status == 200 and any(k.lower() == 'content-disposition' for k in r.headers.keys()),
                            timeout=timeout_ms,
                        )
                        if resp:
                            out_path = self._save_response(resp, out_path_base)
                            if out_path:
                                return PlaywrightDownloadResult(ok=True, path=out_path, url=getattr(resp, "url", None), mode="response-fallback")
                    except Exception as final_exc:
                        last_error = str(final_exc) or last_error

            except Exception as exc:
                last_error = str(exc)
                if debug_mode:
                    try:
                        debug(f"[playwright] attempt failed (headless={mode}): {traceback.format_exc()}")
                    except Exception:
                        from SYS.logger import logger
                        logger.exception("Failed to emit debug info for Playwright attempt failure")
                continue

        return PlaywrightDownloadResult(ok=False, error=last_error or "no download captured")

    def debug_dump(self) -> None:
        try:
            debug(
                f"[playwright] browser={self.defaults.browser} headless={self.defaults.headless} "
                f"viewport={self.defaults.viewport_width}x{self.defaults.viewport_height} "
                f"nav_timeout_ms={self.defaults.navigation_timeout_ms}"
            )
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to debug_dump Playwright defaults")

    def _wait_for_block_clear(self, page: Any, timeout_ms: int = 8000) -> bool:
        try:
            page.wait_for_function(
                "() => { for (const k in window) { if (Object.prototype.hasOwnProperty.call(window, k) && k.startsWith('blocked_')) { try { return window[k] === false; } catch(e) {} return false; } } return true; }",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    def _save_response(self, response: Any, out_dir: Path) -> Optional[Path]:
        try:
            cd = ""
            try:
                headers = getattr(response, "headers", {}) or {}
                cd = "".join([v for k, v in headers.items() if str(k).lower() == "content-disposition"])
            except Exception:
                cd = ""

            filename = _find_filename_from_cd(cd) or Path(str(getattr(response, "url", "") or "")).name or "download"
            body = response.body()
            out_path = out_dir / filename
            out_path.write_bytes(body)
            return out_path
        except Exception as exc:
            try:
                debug(f"[playwright] failed to save response: {exc}")
            except Exception:
                pass
            return None


def config_schema() -> List[Dict[str, Any]]:
    """Return a schema describing editable Playwright tool defaults for the config UI.

    Notes:
      - `user_agent` is a dropdown with a `custom` option; put the real UA in
        `user_agent_custom` when choosing `custom`.
      - Viewport dimensions are offered as convenient choices.
      - `ffmpeg_path` intentionally defaults to empty; Playwright will consult
        a global `FFMPEG_PATH` environment variable (or fallback to bundled/system).
    """
    _defaults = PlaywrightDefaults()

    browser_choices = ["chromium", "firefox", "webkit"]
    viewport_width_choices = [1920, 1366, 1280, 1024, 800]
    viewport_height_choices = [1080, 900, 768, 720, 600]

    return [
        {
            "key": "browser",
            "label": "Playwright browser",
            "group": "Browser",
            "default": _defaults.browser,
            "choices": browser_choices,
        },
        {
            "key": "headless",
            "label": "Headless",
            "group": "Browser",
            "type": "boolean",
            "default": str(_defaults.headless),
            "choices": ["true", "false"],
        },
        {
            "key": "user_agent",
            "label": "User Agent",
            "group": "Browser",
            "default": "default",
            "choices": ["default", "native", "custom"],
        },
        {
            "key": "user_agent_custom",
            "label": "Custom User Agent (used when User Agent = custom)",
            "group": "Browser",
            "default": "",
            "placeholder": "Mozilla/5.0 ...",
        },
        {
            "key": "viewport_width",
            "label": "Viewport width",
            "group": "Capture",
            "type": "integer",
            "default": _defaults.viewport_width,
            "choices": viewport_width_choices,
        },
        {
            "key": "viewport_height",
            "label": "Viewport height",
            "group": "Capture",
            "type": "integer",
            "default": _defaults.viewport_height,
            "choices": viewport_height_choices,
        },
        {
            "key": "navigation_timeout_ms",
            "label": "Navigation timeout (ms)",
            "group": "Navigation",
            "type": "integer",
            "default": _defaults.navigation_timeout_ms,
        },
        {
            "key": "screenshot_quality",
            "label": "Default screenshot quality",
            "group": "Capture",
            "type": "integer",
            "default": _defaults.screenshot_quality,
            "choices": list(range(1, 11)),
        },
        {
            "key": "ignore_https_errors",
            "label": "Ignore HTTPS errors",
            "group": "Navigation",
            "type": "boolean",
            "default": str(_defaults.ignore_https_errors),
            "choices": ["true", "false"],
        },
        {
            "key": "ffmpeg_path",
            "label": "FFmpeg path (leave empty to use global/bundled)",
            "group": "Environment",
            "type": "path",
            "default": "",
            "placeholder": "C:/path/to/ffmpeg.exe",
        },
    ]
