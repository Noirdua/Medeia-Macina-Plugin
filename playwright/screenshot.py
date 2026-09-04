"""Screen-shot cmdlet for capturing screenshots of url in a pipeline.

This cmdlet processes files through the pipeline and creates screenshots using
Playwright, marking them as temporary artifacts for cleanup.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import time
from datetime import datetime
import httpx
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, quote, urljoin, unquote

from SYS.logger import debug_panel, log, is_debug_enabled, status_panel
from SYS.item_accessors import extract_item_tags, get_result_title
from API.HTTP import HTTPClient
from SYS.pipeline_progress import PipelineProgress
from SYS.utils import ensure_directory, sha256_file, unique_path, unique_preserve_order
from cmdlet import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
create_pipe_object_result = sh.create_pipe_object_result
normalize_result_input = sh.normalize_result_input
should_show_help = sh.should_show_help
get_field = sh.get_field
parse_cmdlet_args = sh.parse_cmdlet_args
from SYS import pipeline as pipeline_context

# ============================================================================
# CMDLET Metadata Declaration
# ============================================================================

# ============================================================================
# Playwright & Screenshot Dependencies
# ============================================================================

from plugins.playwright.runtime import PlaywrightTimeoutError, PlaywrightTool, USER_AGENT

try:
    from SYS.config import resolve_output_dir
except ImportError:
    resolve_output_dir = None

# ============================================================================
# Screenshot Constants & Configuration
# ============================================================================

DEFAULT_VIEWPORT: dict[str,
                       int] = {
                           "width": 1920,
                           "height": 1080
                       }
ARCHIVE_TIMEOUT = 30.0

ADBLOCK_HOST_PATTERNS: tuple[str, ...] = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "adservice.google.",
    "adsystem.com",
    "adnxs.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "casalemedia.com",
    "rubiconproject.com",
    "pubmatic.com",
    "scorecardresearch.com",
    "quantserve.com",
    "zedo.com",
    "moatads.com",
    "amazon-adsystem.com",
    "media.net",
)

ADBLOCK_URL_PATTERNS: tuple[str, ...] = (
    "/ads/",
    "?ads=",
    "&ads=",
    "advertisement",
    "googlesyndication",
    "doubleclick",
    "adservice",
    "adserver",
    "prebid",
    "taboola",
    "outbrain",
    "amazon-adsystem",
)

ADBLOCK_CSS_SELECTORS: tuple[str, ...] = (
    "[id*='ad-']",
    "[id^='ad-']",
    "[id*='ads-']",
    "[class*=' ad-']",
    "[class^='ad-']",
    "[class*='ads-']",
    "[class*='advert']",
    "[id*='sponsor']",
    "[class*='sponsor']",
    "iframe[src*='doubleclick.net']",
    "iframe[src*='googlesyndication.com']",
    "iframe[src*='taboola.com']",
    "iframe[src*='outbrain.com']",
)

# WebP has a hard maximum dimension per side.
# Pillow typically fails with: "encoding error 5: Image size exceeds WebP limit of 16383 pixels"
WEBP_MAX_DIM = 16_383

# Configurable selectors for specific websites
SITE_SELECTORS: Dict[str,
                     List[str]] = {
                         "twitter.com": [
                             "article[role='article']",
                             "div[data-testid='tweet']",
                             "div[data-testid='cellInnerDiv'] article",
                         ],
                         "x.com": [
                             "article[role='article']",
                             "div[data-testid='tweet']",
                             "div[data-testid='cellInnerDiv'] article",
                         ],
                         "instagram.com": [
                             "article[role='presentation']",
                             "article[role='article']",
                             "div[role='dialog'] article",
                             "section main article",
                         ],
                         "reddit.com": [
                             "shreddit-post",
                             "div[data-testid='post-container']",
                             "div[data-click-id='background']",
                             "article",
                         ],
                         "rumble.com": [
                             "rumble-player, iframe.rumble",
                             "div.video-item--main",
                             "main article",
                         ],
                     }


class ScreenshotError(RuntimeError):
    """Raised when screenshot capture or upload fails."""


@dataclass(slots=True)
class ScreenshotOptions:
    """Options controlling screenshot capture and post-processing."""

    output_dir: Path
    url: str = ""
    output_path: Optional[Path] = None
    full_page: bool = True
    headless: bool = True
    wait_after_load: float = 6.0
    wait_for_article: bool = False
    replace_video_posters: bool = True
    tag: Sequence[str] = ()
    archive: bool = False
    archive_timeout: float = ARCHIVE_TIMEOUT
    output_format: Optional[str] = None
    prefer_platform_target: bool = False
    target_selectors: Optional[Sequence[str]] = None
    selector_timeout_ms: int = 10_000
    interactive_pick: bool = False
    interactive_pick_timeout_s: float = 120.0
    quality: int = 8
    adblock: bool = True
    playwright_tool: Optional[Any] = None


@dataclass(slots=True)
class ScreenshotResult:
    """Details about the captured screenshot."""

    path: Path
    tag_applied: List[str]
    archive_url: List[str]
    url: List[str]
    capture_mode: str = ""
    capture_target: str = ""
    warnings: List[str] = field(default_factory=list)


# ============================================================================
# Helper Functions
# ============================================================================


def _slugify_url(url: str) -> str:
    """Convert URL to filesystem-safe slug."""
    parsed = urlsplit(url)
    candidate = f"{parsed.netloc}{parsed.path}"
    if parsed.query:
        candidate += f"?{parsed.query}"
    slug = "".join(char if char.isalnum() else "-" for char in candidate.lower())
    slug = slug.strip("-") or "screenshot"
    return slug[:100]


def _tags_from_url(url: str) -> List[str]:
    """Derive simple tags from a URL.

    - site:<domain> (strips leading www.)
    - title:<slug> derived from the last path segment, with extension removed
      and separators (-, _, %) normalized to spaces.
    """

    u = str(url or "").strip()
    if not u:
        return []

    parsed = None
    try:
        parsed = urlsplit(u)
        host = (
            str(
                getattr(parsed,
                        "hostname",
                        None) or getattr(parsed,
                                         "netloc",
                                         "") or ""
            ).strip().lower()
        )
    except Exception:
        parsed = None
        host = ""

    if host:
        # Drop credentials and port if present.
        if "@" in host:
            host = host.rsplit("@", 1)[-1]
        if ":" in host:
            host = host.split(":", 1)[0]
        if host.startswith("www."):
            host = host[len("www."):]

    path = ""
    if parsed is not None:
        try:
            path = str(getattr(parsed, "path", "") or "")
        except Exception:
            path = ""

    last = ""
    if path:
        try:
            last = path.rsplit("/", 1)[-1]
        except Exception:
            last = ""

    try:
        last = unquote(last or "")
    except Exception:
        last = last or ""

    if last and "." in last:
        # Drop a single trailing extension (e.g. .html, .php).
        last = last.rsplit(".", 1)[0]

    for sep in ("_", "-", "%"):
        if last and sep in last:
            last = last.replace(sep, " ")

    title = " ".join(str(last or "").split()).strip().lower()

    tags: List[str] = []
    if host:
        tags.append(f"site:{host}")
    if title:
        tags.append(f"title:{title}")
    return tags


def _title_from_url(url: str) -> str:
    """Return the normalized title derived from a URL's last path segment."""
    for t in _tags_from_url(url):
        if str(t).lower().startswith("title:"):
            return str(t)[len("title:"):].strip()
    return ""


def _normalize_format(fmt: Optional[str]) -> str:
    """Normalize output format to valid values."""
    if not fmt:
        return "webp"
    value = fmt.strip().lower()
    if value in {"mht", "mhtml"}:
        return "mhtml"
    if value in {"jpg",
                 "jpeg"}:
        return "jpeg"
    if value in {"png",
                 "pdf",
                 "mhtml",
                 "webp"}:
        return value
    return "webp"


def _format_suffix(fmt: str) -> str:
    """Get file suffix for format."""
    if fmt == "jpeg":
        return ".jpg"
    return f".{fmt}"


def _normalize_capture_mode(value: Optional[str]) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"full", "page", "fullscreen"}:
        return "full"
    if mode in {"pick", "picker", "interactive", "element", "select"}:
        return "interactive"
    return ""


def _format_supports_target_selection(fmt: Optional[str]) -> bool:
    return _normalize_format(fmt) not in {"pdf", "mhtml"}


def _normalize_quality(value: Any) -> int:
    try:
        quality = int(str(value).strip())
    except Exception:
        quality = 8
    return max(1, min(10, quality))


def _normalize_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _url_matches_adblock(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    if not lowered:
        return False
    try:
        host = str(urlsplit(lowered).hostname or "").strip().lower()
    except Exception:
        host = ""
    if host and any(pattern in host for pattern in ADBLOCK_HOST_PATTERNS):
        return True
    return any(pattern in lowered for pattern in ADBLOCK_URL_PATTERNS)


def _install_adblock(page: Any) -> Optional[Dict[str, int]]:
    try:
        state: Dict[str, int] = {"blocked": 0}

        def _route(route: Any) -> None:
            try:
                request = route.request
                url = str(getattr(request, "url", "") or "")
                resource_type = str(getattr(request, "resource_type", "") or "").strip().lower()
                if resource_type != "document" and _url_matches_adblock(url):
                    state["blocked"] = int(state.get("blocked", 0)) + 1
                    route.abort("blockedbyclient")
                    return
            except Exception:
                pass
            route.continue_()

        page.route("**/*", _route)
        return state
    except Exception:
        return None


def _remove_ad_elements(page: Any) -> int:
    try:
        selectors_json = repr(list(ADBLOCK_CSS_SELECTORS))
        removed = page.evaluate(
            f"""
            () => {{
                const selectors = {selectors_json};
                const seen = new Set();
                let removed = 0;
                for (const selector of selectors) {{
                    let nodes = [];
                    try {{
                        nodes = Array.from(document.querySelectorAll(selector));
                    }} catch (e) {{
                        continue;
                    }}
                    for (const node of nodes) {{
                        if (!(node instanceof Element)) continue;
                        if (seen.has(node)) continue;
                        seen.add(node);
                        try {{
                            node.remove();
                            removed += 1;
                        }} catch (e) {{}}
                    }}
                }}
                return removed;
            }}
            """
        )
        return int(removed or 0)
    except Exception:
        return 0


def _jpeg_quality_from_level(level: int) -> int:
    normalized = _normalize_quality(level)
    if normalized >= 10:
        return 100
    return 45 + ((normalized - 1) * 6)


def _webp_quality_settings(level: int) -> Dict[str, Any]:
    normalized = _normalize_quality(level)
    if normalized >= 10:
        return {
            "quality": 100,
            "method": 6,
            "lossless": True,
        }
    return {
        "quality": 45 + ((normalized - 1) * 6),
        "method": 6,
        "lossless": False,
    }


def _stdin_interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def _debug_rows(rows: Sequence[tuple[str, Any]]) -> List[tuple[str, Any]]:
    normalized: List[tuple[str, Any]] = []
    for key, value in rows:
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(item) for item in value) if value else "<none>"
        elif isinstance(value, Path):
            value = str(value)
        elif value in (None, ""):
            value = "<none>"
        normalized.append((str(key), value))
    return normalized


def _show_debug_panel(
    title: str,
    rows: Sequence[tuple[str, Any]],
    *,
    border_style: str = "cyan",
) -> None:
    try:
        debug_panel(title, _debug_rows(rows), border_style=border_style)
    except Exception:
        pass


def _install_element_picker(page: Any) -> None:
        page.evaluate(
                """
                () => {
                    try {
                        if (typeof window.__medeiaPickerCleanup === 'function') {
                            window.__medeiaPickerCleanup();
                        }

                        window.__medeiaPickerResult = null;

                        const cssEscape = (value) => {
                            try {
                                if (window.CSS && typeof window.CSS.escape === 'function') {
                                    return window.CSS.escape(String(value || ''));
                                }
                            } catch (e) {}
                            return String(value || '').replace(/[^a-zA-Z0-9_-]/g, '\\$&');
                        };

                        const buildSelector = (element) => {
                            if (!(element instanceof Element)) return '';
                            if (element.id) return '#' + cssEscape(element.id);
                            const parts = [];
                            let node = element;
                            while (node && node.nodeType === 1 && parts.length < 8) {
                                let part = String(node.localName || node.tagName || '').toLowerCase();
                                if (!part) break;
                                const classes = Array.from(node.classList || []).filter(Boolean).slice(0, 2);
                                if (classes.length) {
                                    part += classes.map((name) => '.' + cssEscape(name)).join('');
                                }
                                const parent = node.parentElement;
                                if (parent) {
                                    const siblings = Array.from(parent.children).filter((child) => child.localName === node.localName);
                                    if (siblings.length > 1) {
                                        part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
                                    }
                                }
                                parts.unshift(part);
                                const selector = parts.join(' > ');
                                try {
                                    if (document.querySelectorAll(selector).length === 1) {
                                        return selector;
                                    }
                                } catch (e) {}
                                node = parent;
                            }
                            return parts.join(' > ');
                        };

                        const box = document.createElement('div');
                        box.setAttribute('data-medeia-picker', 'box');
                        box.style.position = 'fixed';
                        box.style.pointerEvents = 'none';
                        box.style.zIndex = '2147483646';
                        box.style.border = '2px solid #ffb000';
                        box.style.background = 'rgba(255, 176, 0, 0.12)';
                        box.style.boxShadow = '0 0 0 99999px rgba(0, 0, 0, 0.12)';
                        box.style.display = 'none';

                        const banner = document.createElement('div');
                        banner.setAttribute('data-medeia-picker', 'banner');
                        banner.style.position = 'fixed';
                        banner.style.top = '12px';
                        banner.style.left = '50%';
                        banner.style.transform = 'translateX(-50%)';
                        banner.style.zIndex = '2147483647';
                        banner.style.padding = '10px 14px';
                        banner.style.background = 'rgba(18, 18, 18, 0.92)';
                        banner.style.color = '#ffffff';
                        banner.style.font = '13px/1.4 sans-serif';
                        banner.style.borderRadius = '10px';
                        banner.style.boxShadow = '0 8px 24px rgba(0, 0, 0, 0.35)';
                        banner.style.maxWidth = 'min(90vw, 920px)';
                        banner.style.pointerEvents = 'none';
                        banner.textContent = 'Medeia screenshot picker: hover an element, click to capture it, or press Escape to cancel.';

                        const updateBox = (element) => {
                            if (!(element instanceof Element)) {
                                box.style.display = 'none';
                                return;
                            }
                            const rect = element.getBoundingClientRect();
                            box.style.display = 'block';
                            box.style.left = rect.left + 'px';
                            box.style.top = rect.top + 'px';
                            box.style.width = rect.width + 'px';
                            box.style.height = rect.height + 'px';
                        };

                        const finish = (payload) => {
                            if (window.__medeiaPickerResult) {
                                return;
                            }
                            window.__medeiaPickerResult = payload;
                        };

                        const onMove = (event) => {
                            const target = event.target instanceof Element ? event.target : null;
                            if (!target || target.closest('[data-medeia-picker]')) {
                                return;
                            }
                            updateBox(target);
                        };

                        const onPointerDown = (event) => {
                            const target = event.target instanceof Element ? event.target : null;
                            if (!target || target.closest('[data-medeia-picker]')) {
                                return;
                            }
                            event.preventDefault();
                            event.stopPropagation();
                            event.stopImmediatePropagation();
                            const rect = target.getBoundingClientRect();
                            finish({
                                cancelled: false,
                                selector: buildSelector(target),
                                tag: String(target.localName || target.tagName || '').toLowerCase(),
                                text: String((target.textContent || '').trim()).slice(0, 200),
                                width: Math.round(rect.width || 0),
                                height: Math.round(rect.height || 0),
                            });
                        };

                        const onKeyDown = (event) => {
                            if (event.key !== 'Escape') {
                                return;
                            }
                            event.preventDefault();
                            event.stopPropagation();
                            event.stopImmediatePropagation();
                            finish({ cancelled: true });
                        };

                        window.__medeiaPickerCleanup = () => {
                            window.removeEventListener('mousemove', onMove, true);
                            window.removeEventListener('pointerdown', onPointerDown, true);
                            window.removeEventListener('keydown', onKeyDown, true);
                            try { box.remove(); } catch (e) {}
                            try { banner.remove(); } catch (e) {}
                            try { delete window.__medeiaPickerCleanup; } catch (e) {}
                        };

                        window.addEventListener('mousemove', onMove, true);
                        window.addEventListener('pointerdown', onPointerDown, true);
                        window.addEventListener('keydown', onKeyDown, true);
                        document.documentElement.appendChild(box);
                        document.documentElement.appendChild(banner);

                        try {
                            window.focus();
                        } catch (e) {}
                        try {
                            document.documentElement.setAttribute('tabindex', '-1');
                            document.documentElement.focus({ preventScroll: true });
                        } catch (e) {}
                    } catch (e) {
                        window.__medeiaPickerResult = {
                            cancelled: true,
                            error: String(e || ''),
                        };
                    }
                }
                """
        )


def _clear_element_picker(page: Any) -> None:
        try:
                page.evaluate(
                        """
                        () => {
                            try {
                                if (typeof window.__medeiaPickerCleanup === 'function') {
                                    window.__medeiaPickerCleanup();
                                }
                            } catch (e) {}
                        }
                        """
                )
        except Exception:
                pass


def _interactive_pick_selector(page: Any, *, timeout_s: float) -> Dict[str, Any]:
    picked: Dict[str, Any] = {}

    _install_element_picker(page)
    deadline = time.time() + max(5.0, float(timeout_s or 0.0))
    try:
        while time.time() < deadline:
            try:
                if page.is_closed():
                    picked["cancelled"] = True
                    break
            except Exception:
                break

            try:
                payload = page.evaluate("() => window.__medeiaPickerResult || null")
            except Exception:
                payload = None

            if isinstance(payload, dict) and payload:
                picked.update(payload)
                break

            time.sleep(0.05)
    finally:
        _clear_element_picker(page)

    if not picked:
        raise ScreenshotError("Timed out waiting for element selection")
    if picked.get("cancelled"):
        error_text = str(picked.get("error") or "").strip()
        if error_text:
            raise ScreenshotError(f"Element selection cancelled: {error_text}")
        raise ScreenshotError("Element selection cancelled")

    selector = str(picked.get("selector") or "").strip()
    if not selector:
        raise ScreenshotError("Element picker did not return a valid selector")
    return picked


def _prepare_capture_page(
    tool: Any,
    page: Any,
    options: ScreenshotOptions,
    warnings: List[str],
    progress: PipelineProgress,
) -> str:
    navigation_status = "loaded"
    adblock_state: Optional[Dict[str, int]] = None
    if options.adblock:
        adblock_state = _install_adblock(page)
    progress.step("loading navigating")
    try:
        tool.goto(page, options.url)
        progress.step("loading page loaded")
    except PlaywrightTimeoutError:
        navigation_status = "timeout"
        warnings.append("navigation timeout; capturing current page state")
        progress.step("loading navigation timeout")

    if options.wait_for_article:
        try:
            page.wait_for_selector("article", timeout=10_000)
        except PlaywrightTimeoutError:
            warnings.append("<article> selector not found; capturing fallback")

    if options.wait_after_load > 0:
        time.sleep(min(10.0, max(0.0, options.wait_after_load)))

    progress.step("loading stabilized")
    progress.step("capturing preparing")
    if options.replace_video_posters:
        page.evaluate(
            """
                document.querySelectorAll('video').forEach(v => {
                    if (v.poster) {
                        const img = document.createElement('img');
                        img.src = v.poster;
                        img.style.maxWidth = '100%';
                        img.style.borderRadius = '12px';
                        v.replaceWith(img);
                    }
                });
            """
        )
    removed_ads = 0
    if options.adblock:
        removed_ads = _remove_ad_elements(page)
        blocked_count = int((adblock_state or {}).get("blocked", 0))
        if blocked_count or removed_ads:
            warnings.append(
                f"adblock filtered {blocked_count} request(s) and removed {removed_ads} page element(s)"
            )
    return navigation_status


def _capture_selector_screenshot(
    page: Any,
    selector: str,
    destination: Path,
    format_name: str,
    selector_timeout_ms: int,
    quality_level: int,
) -> None:
    selector_text = str(selector or "").strip()
    if not selector_text:
        raise ScreenshotError("No selector was provided for element capture")

    timeout_ms = max(10_000, int(selector_timeout_ms or 0))
    locator = page.locator(selector_text).first
    locator.wait_for(state="visible", timeout=timeout_ms)

    try:
        page.add_style_tag(
            content=(
                "*,*::before,*::after{animation:none !important;transition:none !important;"
                "scroll-behavior:auto !important;}"
            )
        )
    except Exception:
        pass

    try:
        locator.scroll_into_view_if_needed(timeout=min(timeout_ms, 2_500))
    except Exception:
        pass

    try:
        locator.evaluate(
            """
            async (element) => {
              const media = Array.from(
                element.querySelectorAll('img,video,iframe')
              );
              const pending = media.map((node) => {
                if (node instanceof HTMLImageElement) {
                  if (node.complete) {
                    return Promise.resolve();
                  }
                  return new Promise((resolve) => {
                    const done = () => resolve();
                    node.addEventListener('load', done, { once: true });
                    node.addEventListener('error', done, { once: true });
                    setTimeout(done, 1500);
                  });
                }
                return Promise.resolve();
              });
              if (pending.length) {
                await Promise.allSettled(pending);
              }
              try {
                if (document.fonts && document.fonts.ready) {
                  await Promise.race([
                    document.fonts.ready,
                    new Promise((resolve) => setTimeout(resolve, 1500)),
                  ]);
                }
              } catch (e) {}
            }
            """
        )
    except Exception:
        pass

    def _read_clip() -> Optional[Dict[str, float]]:
        try:
            clip_value = locator.bounding_box()
        except Exception:
            clip_value = None
        if not isinstance(clip_value, dict):
            return None
        try:
            return {
                "x": max(0.0, float(clip_value.get("x") or 0.0)),
                "y": max(0.0, float(clip_value.get("y") or 0.0)),
                "width": max(1.0, float(clip_value.get("width") or 0.0)),
                "height": max(1.0, float(clip_value.get("height") or 0.0)),
            }
        except Exception:
            return None

    def _read_page_rect() -> Optional[Dict[str, float]]:
        try:
            rect_value = locator.evaluate(
                """
                (element) => {
                  const rect = element.getBoundingClientRect();
                  return {
                    x: Math.max(0, rect.left + window.scrollX),
                    y: Math.max(0, rect.top + window.scrollY),
                    width: Math.max(1, rect.width),
                    height: Math.max(1, rect.height),
                  };
                }
                """
            )
        except Exception:
            rect_value = None
        if not isinstance(rect_value, dict):
            return None
        try:
            return {
                "x": max(0.0, float(rect_value.get("x") or 0.0)),
                "y": max(0.0, float(rect_value.get("y") or 0.0)),
                "width": max(1.0, float(rect_value.get("width") or 0.0)),
                "height": max(1.0, float(rect_value.get("height") or 0.0)),
            }
        except Exception:
            return None

    def _read_viewport_rect() -> Optional[Dict[str, float]]:
        try:
            rect_value = locator.evaluate(
                """
                (element) => {
                  const rect = element.getBoundingClientRect();
                  return {
                    left: rect.left,
                    top: rect.top,
                    right: rect.right,
                    bottom: rect.bottom,
                    width: rect.width,
                    height: rect.height,
                  };
                }
                """
            )
        except Exception:
            rect_value = None
        if not isinstance(rect_value, dict):
            return None
        try:
            return {
                "left": float(rect_value.get("left") or 0.0),
                "top": float(rect_value.get("top") or 0.0),
                "right": float(rect_value.get("right") or 0.0),
                "bottom": float(rect_value.get("bottom") or 0.0),
                "width": max(1.0, float(rect_value.get("width") or 0.0)),
                "height": max(1.0, float(rect_value.get("height") or 0.0)),
            }
        except Exception:
            return None

    def _read_scroll_metrics() -> Dict[str, float]:
        try:
            metrics_value = page.evaluate(
                """
                () => {
                  const root = document.documentElement || document.body;
                  const body = document.body;
                  const scrollHeight = Math.max(
                    root ? root.scrollHeight || 0 : 0,
                    body ? body.scrollHeight || 0 : 0,
                  );
                  const innerWidth = window.innerWidth || 0;
                  const innerHeight = window.innerHeight || 0;
                  return {
                    scrollX: window.scrollX || window.pageXOffset || 0,
                    scrollY: window.scrollY || window.pageYOffset || 0,
                    innerWidth,
                    innerHeight,
                    maxScrollY: Math.max(0, scrollHeight - innerHeight),
                  };
                }
                """
            )
        except Exception:
            metrics_value = None
        if not isinstance(metrics_value, dict):
            return {
                "scrollX": 0.0,
                "scrollY": 0.0,
                "innerWidth": max(1.0, current_viewport_width),
                "innerHeight": max(1.0, current_viewport_height),
                "maxScrollY": 0.0,
            }
        try:
            return {
                "scrollX": max(0.0, float(metrics_value.get("scrollX") or 0.0)),
                "scrollY": max(0.0, float(metrics_value.get("scrollY") or 0.0)),
                "innerWidth": max(1.0, float(metrics_value.get("innerWidth") or current_viewport_width or 1.0)),
                "innerHeight": max(1.0, float(metrics_value.get("innerHeight") or current_viewport_height or 1.0)),
                "maxScrollY": max(0.0, float(metrics_value.get("maxScrollY") or 0.0)),
            }
        except Exception:
            return {
                "scrollX": 0.0,
                "scrollY": 0.0,
                "innerWidth": max(1.0, current_viewport_width),
                "innerHeight": max(1.0, current_viewport_height),
                "maxScrollY": 0.0,
            }

    stable_clip: Optional[Dict[str, float]] = None
    stable_reads = 0
    previous_clip: Optional[Dict[str, float]] = None
    for _ in range(12):
        current_clip = _read_clip()
        if current_clip is None:
            time.sleep(0.15)
            continue
        if previous_clip is not None:
            dx = abs(current_clip["x"] - previous_clip["x"])
            dy = abs(current_clip["y"] - previous_clip["y"])
            dw = abs(current_clip["width"] - previous_clip["width"])
            dh = abs(current_clip["height"] - previous_clip["height"])
            if max(dx, dy, dw, dh) <= 1.0:
                stable_reads += 1
            else:
                stable_reads = 0
        previous_clip = current_clip
        stable_clip = current_clip
        if stable_reads >= 2:
            break
        time.sleep(0.15)

    clip = stable_clip
    if clip is None:
        raise ScreenshotError(f"Could not measure selector '{selector_text}'")
    x = clip["x"]
    y = clip["y"]
    width = clip["width"]
    height = clip["height"]
    page_rect = _read_page_rect()
    if page_rect is None:
        raise ScreenshotError(f"Could not read page coordinates for selector '{selector_text}'")

    viewport_size = None
    try:
        viewport_size = page.viewport_size
    except Exception:
        viewport_size = None

    try:
        current_viewport_width = max(1.0, float((viewport_size or {}).get("width") or 0.0))
        current_viewport_height = max(1.0, float((viewport_size or {}).get("height") or 0.0))
    except Exception:
        current_viewport_width = 0.0
        current_viewport_height = 0.0

    required_width = max(1.0, x + width + 8.0)
    if required_width > current_viewport_width:
        try:
            page.set_viewport_size(
                {
                    "width": int(max(current_viewport_width, required_width)),
                    "height": int(max(current_viewport_height, 1.0)),
                }
            )
            try:
                locator.scroll_into_view_if_needed(timeout=min(timeout_ms, 2_500))
            except Exception:
                pass
            time.sleep(0.25)
            clip = _read_clip()
            if clip is None:
                raise ScreenshotError(f"Could not re-measure selector '{selector_text}' after viewport resize")
            x = clip["x"]
            y = clip["y"]
            width = clip["width"]
            height = clip["height"]
            page_rect = _read_page_rect()
            if page_rect is None:
                raise ScreenshotError(f"Could not re-read page coordinates for selector '{selector_text}'")
            current_viewport_width = max(current_viewport_width, required_width)
        except Exception as exc:
            raise ScreenshotError(f"Could not resize viewport for selector '{selector_text}': {exc}") from exc

    if height > max(1.0, current_viewport_height - 8.0):
        try:
            from PIL import Image
        except Exception as exc:
            raise ScreenshotError(
                f"Pillow is required for tall element capture: {exc}"
            ) from exc

        padding = 2.0
        output_left = max(0.0, page_rect["x"] - padding)
        output_top = max(0.0, page_rect["y"] - padding)
        output_width = max(1, int(page_rect["width"] + (padding * 2.0) + 0.9999))
        output_height = max(1, int(page_rect["height"] + (padding * 2.0) + 0.9999))
        canvas_mode = "RGB" if format_name == "jpeg" else "RGBA"
        canvas_bg = (255, 255, 255) if canvas_mode == "RGB" else (255, 255, 255, 0)
        stitched = Image.new(canvas_mode, (output_width, output_height), canvas_bg)
        stitched_bottom = 0
        overlap_px = 24
        step_cursor = 0
        max_iterations = max(10, int((output_height / max(1.0, current_viewport_height)) * 6.0) + 12)

        try:
            for _ in range(max_iterations):
                metrics = _read_scroll_metrics()
                desired_scroll_y = min(
                    metrics["maxScrollY"],
                    max(0.0, output_top + float(step_cursor)),
                )
                page.evaluate("(y) => window.scrollTo(0, y)", desired_scroll_y)
                page.wait_for_timeout(125)
                try:
                    locator.evaluate(
                        """
                        async () => {
                          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                        }
                        """
                    )
                except Exception:
                    pass

                metrics = _read_scroll_metrics()
                viewport_rect = _read_viewport_rect()
                if viewport_rect is None:
                    continue

                visible_left = max(0.0, viewport_rect["left"] - padding)
                visible_top = max(0.0, viewport_rect["top"] - padding)
                visible_right = min(metrics["innerWidth"], viewport_rect["right"] + padding)
                visible_bottom = min(metrics["innerHeight"], viewport_rect["bottom"] + padding)
                if visible_right <= visible_left or visible_bottom <= visible_top:
                    if metrics["scrollY"] >= metrics["maxScrollY"]:
                        break
                    step_cursor += max(1, int(metrics["innerHeight"] * 0.6))
                    continue

                clip_box = {
                    "x": float(int(visible_left)),
                    "y": float(int(visible_top)),
                    "width": float(int((visible_right - visible_left) + 0.9999)),
                    "height": float(int((visible_bottom - visible_top) + 0.9999)),
                }
                piece_bytes = page.screenshot(
                    timeout=timeout_ms,
                    type="png",
                    clip=clip_box,
                )

                capture_page_x = metrics["scrollX"] + visible_left
                capture_page_y = metrics["scrollY"] + visible_top
                paste_x = int(round(capture_page_x - output_left))
                paste_y = int(round(capture_page_y - output_top))

                with Image.open(io.BytesIO(piece_bytes)) as piece_image:
                    if canvas_mode == "RGB":
                        piece = piece_image.convert("RGB")
                    else:
                        piece = piece_image.convert("RGBA")

                    crop_left = max(0, -paste_x)
                    crop_top = max(0, -paste_y)
                    crop_right = min(piece.width, output_width - paste_x)
                    crop_bottom = min(piece.height, output_height - paste_y)
                    if crop_right <= crop_left or crop_bottom <= crop_top:
                        continue
                    if crop_left or crop_top or crop_right != piece.width or crop_bottom != piece.height:
                        piece = piece.crop((crop_left, crop_top, crop_right, crop_bottom))
                    dest_x = max(0, paste_x + crop_left)
                    dest_y = max(0, paste_y + crop_top)
                    stitched.paste(piece, (dest_x, dest_y))
                    piece_bottom = dest_y + piece.height

                if piece_bottom <= stitched_bottom + 1:
                    if metrics["scrollY"] >= metrics["maxScrollY"]:
                        break
                    step_cursor += max(1, int(metrics["innerHeight"] * 0.6))
                    continue

                stitched_bottom = max(stitched_bottom, piece_bottom)
                if stitched_bottom >= output_height:
                    break
                step_cursor = max(0, stitched_bottom - overlap_px)

            if stitched_bottom <= 0:
                raise ScreenshotError(
                    f"Could not capture stitched slices for selector '{selector_text}'"
                )

            save_kwargs: Dict[str, Any] = {}
            if format_name == "jpeg":
                save_kwargs.update({"format": "JPEG", "quality": _jpeg_quality_from_level(quality_level)})
            else:
                save_kwargs.update({"format": "PNG"})
            stitched.save(destination, **save_kwargs)
            return
        except ScreenshotError:
            raise
        except Exception as exc:
            raise ScreenshotError(
                f"Could not stitch tall selector capture for '{selector_text}': {exc}"
            ) from exc

    padding = 2.0
    x = max(0.0, x - padding)
    y = max(0.0, y - padding)
    width = max(1.0, width + (padding * 2.0))
    height = max(1.0, height + (padding * 2.0))

    clip_box: Dict[str, float] = {
        "x": float(int(x)),
        "y": float(int(y)),
        "width": float(int(width + 0.9999)),
        "height": float(int(height + 0.9999)),
    }

    screenshot_kwargs: Dict[str, Any] = {
        "path": str(destination),
        "timeout": timeout_ms,
        "clip": clip_box,
    }
    if format_name == "jpeg":
        screenshot_kwargs["type"] = "jpeg"
        screenshot_kwargs["quality"] = _jpeg_quality_from_level(quality_level)

    page.screenshot(**screenshot_kwargs)


def _capture_mhtml(page: Any, destination: Path) -> None:
    session = None
    try:
        context = getattr(page, "context", None)
        if context is None or not hasattr(context, "new_cdp_session"):
            raise ScreenshotError("MHTML output requires Chromium CDP session support")

        session = context.new_cdp_session(page)
        session.send("Page.enable")
        snapshot = session.send("Page.captureSnapshot", {"format": "mhtml"})
        data = snapshot.get("data") if isinstance(snapshot, dict) else None
        if not data:
            raise ScreenshotError("Chromium did not return any MHTML snapshot data")
        destination.write_text(str(data), encoding="utf-8", newline="")
    except ScreenshotError:
        raise
    except Exception as exc:
        raise ScreenshotError(f"Could not capture MHTML snapshot: {exc}") from exc
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def _convert_to_webp(
    src_png: Path,
    dst_webp: Path,
    *,
    quality: int = 90,
    method: int = 6,
    lossless: bool = False,
    max_dim: int = WEBP_MAX_DIM,
    downscale_if_oversize: bool = True,
) -> bool:
    """Convert a PNG screenshot to WebP via Pillow.

    Playwright does not currently support emitting WebP directly.
    """
    if not src_png or not Path(src_png).is_file():
        raise ScreenshotError(f"Source image not found: {src_png}")

    dst_webp = Path(dst_webp)
    try:
        dst_webp.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        from PIL import Image
    except Exception as exc:
        raise ScreenshotError(f"Pillow is required for webp conversion: {exc}") from exc

    # Write atomically to avoid partial files if conversion is interrupted.
    tmp_path = unique_path(dst_webp.with_suffix(".tmp.webp"))
    try:
        with Image.open(src_png) as im:
            did_downscale = False
            save_kwargs: Dict[str,
                              Any] = {
                                  "format": "WEBP",
                                  "quality": int(quality),
                                  "method": int(method),
                                  "lossless": bool(lossless),
                              }

            # Preserve alpha when present; Pillow handles it for WEBP.
            # Normalize palette images to RGBA to avoid odd palette artifacts.
            if im.mode == "P":
                im = im.convert("RGBA")

            # WebP enforces a hard max dimension per side (16383px).
            # When full-page captures are very tall, downscale proportionally to fit.
            try:
                w, h = im.size
            except Exception:
                w, h = 0, 0

            if (downscale_if_oversize and isinstance(max_dim,
                                                     int) and max_dim > 0
                    and (w > max_dim or h > max_dim)):
                scale = 1.0
                try:
                    scale = min(float(max_dim) / float(w), float(max_dim) / float(h))
                except Exception:
                    scale = 1.0

                if scale > 0.0 and scale < 1.0:
                    new_w = max(1, int(w * scale))
                    new_h = max(1, int(h * scale))
                    try:
                        resample = getattr(
                            getattr(Image,
                                    "Resampling",
                                    Image),
                            "LANCZOS",
                            None
                        )
                        if resample is None:
                            resample = getattr(Image, "LANCZOS", 1)
                        im = im.resize((new_w, new_h), resample=resample)
                        did_downscale = True
                    except Exception:
                        pass

            im.save(tmp_path, **save_kwargs)

        tmp_path.replace(dst_webp)
        return bool(did_downscale)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _matched_site_selectors(url: str) -> List[str]:
    """Return SITE_SELECTORS for a matched domain; empty if no match.

    Unlike `_selectors_for_url()`, this does not return a generic fallback.
    """
    u = str(url or "").lower()
    sels: List[str] = []
    for domain, selectors in SITE_SELECTORS.items():
        if domain in u:
            sels.extend(selectors)
    return sels


def _selectors_for_url(url: str) -> List[str]:
    """Return selectors to try for a URL.

    For now, prefer a minimal behavior: only return known SITE_SELECTORS.
    (The cmdlet already falls back to full-page capture when no selectors match.)
    """

    return _matched_site_selectors(url)


def _platform_preprocess(
    url: str,
    page: Any,
    warnings: List[str],
    timeout_ms: int = 10_000
) -> None:
    """Best-effort page tweaks for popular platforms before capture."""
    try:
        u = str(url or "").lower()

        def _try_click_buttons(
            names: List[str],
            passes: int = 2,
            per_timeout: int = 700
        ) -> int:
            clicks = 0
            for _ in range(max(1, int(passes))):
                for name in names:
                    try:
                        locator = page.get_by_role("button", name=name)
                        locator.first.click(timeout=int(per_timeout))
                        clicks += 1
                    except Exception:
                        pass
            return clicks

        # Dismiss common cookie / consent prompts.
        _try_click_buttons(
            [
                "Accept all",
                "Accept",
                "I agree",
                "Agree",
                "Allow all",
                "OK",
            ]
        )

        # Some sites need small nudges (best-effort).
        if "reddit.com" in u:
            _try_click_buttons(["Accept all", "Accept"])
        if ("twitter.com" in u) or ("x.com" in u):
            _try_click_buttons(["Accept all", "Accept"])
        if "instagram.com" in u:
            _try_click_buttons(["Allow all", "Accept all", "Accept"])
    except Exception:
        return


def _submit_wayback(url: str, timeout: float) -> Optional[str]:
    encoded = quote(url, safe="/:?=&")
    with HTTPClient(headers={
            "User-Agent": USER_AGENT
    }) as client:
        response = client.get(f"https://web.archive.org/save/{encoded}")
        content_location = response.headers.get("Content-Location")
        if content_location:
            return urljoin("https://web.archive.org", content_location)
        return str(response.url)


def _submit_archive_today(url: str, timeout: float) -> Optional[str]:
    """Submit URL to Archive.today."""
    encoded = quote(url, safe=":/?#[]@!$&'()*+,;=")
    with HTTPClient(headers={
            "User-Agent": USER_AGENT
    }) as client:
        response = client.get(f"https://archive.today/submit/?url={encoded}")
        response.raise_for_status()
        final = str(response.url)
        if final and ("archive.today" in final or "archive.ph" in final):
            return final
        return None


def _submit_archive_ph(url: str, timeout: float) -> Optional[str]:
    """Submit URL to Archive.ph."""
    encoded = quote(url, safe=":/?#[]@!$&'()*+,;=")
    with HTTPClient(headers={
            "User-Agent": USER_AGENT
    }) as client:
        response = client.get(f"https://archive.ph/submit/?url={encoded}")
        response.raise_for_status()
        final = str(response.url)
        if final and "archive.ph" in final:
            return final
        return None


def _archive_url(url: str, timeout: float) -> Tuple[List[str], List[str]]:
    """Submit URL to all available archive services."""
    archives: List[str] = []
    warnings: List[str] = []
    archive_status: List[tuple[str, Any]] = []
    for submitter, label in (
        (_submit_wayback, "wayback"),
        (_submit_archive_today, "archive.today"),
        (_submit_archive_ph, "archive.ph"),
    ):
        try:
            archived = submitter(url, timeout)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                warnings.append(f"archive {label} rate limited (HTTP 429)")
                archive_status.append((label, "rate limited (HTTP 429)"))
            else:
                warnings.append(
                    f"archive {label} failed: HTTP {exc.response.status_code}"
                )
                archive_status.append((label, f"HTTP {exc.response.status_code}"))
        except httpx.RequestError as exc:
            warnings.append(f"archive {label} failed: {exc}")
            archive_status.append((label, f"connection error: {exc}"))
        except Exception as exc:
            warnings.append(f"archive {label} failed: {exc}")
            archive_status.append((label, exc))
        else:
            if archived:
                archives.append(archived)
                archive_status.append((label, archived))
            else:
                archive_status.append((label, "no archive link returned"))

    if is_debug_enabled() and archive_status:
        _show_debug_panel(
            "Screenshot Archive",
            [("url", url), *archive_status],
        )
    return archives, warnings


def _prepare_output_path(options: ScreenshotOptions) -> Path:
    """Prepare and validate output path for screenshot."""
    ensure_directory(options.output_dir)
    explicit_format = _normalize_format(
        options.output_format
    ) if options.output_format else None
    inferred_format: Optional[str] = None
    if options.output_path is not None:
        path = options.output_path
        if not path.is_absolute():
            path = options.output_dir / path
        suffix = path.suffix.lower()
        if suffix:
            inferred_format = _normalize_format(suffix[1:])
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{_slugify_url(options.url)}_{stamp}"
        path = options.output_dir / filename
    final_format = explicit_format or inferred_format or "png"
    if not path.suffix:
        path = path.with_suffix(_format_suffix(final_format))
    else:
        current_suffix = path.suffix.lower()
        expected = _format_suffix(final_format)
        if current_suffix != expected:
            path = path.with_suffix(expected)
    options.output_format = final_format
    return unique_path(path)


def _capture(
    options: ScreenshotOptions,
    destination: Path,
    warnings: List[str],
    progress: PipelineProgress
) -> tuple[str, str]:
    """Capture screenshot using Playwright."""
    capture_mode = "full-page"
    capture_target = ""
    try:
        progress.step("loading launching browser")
        tool = options.playwright_tool or PlaywrightTool({})

        # Ensure Chromium engine is used for the screen-shot cmdlet (force for consistency)
        try:
            current_browser = (
                getattr(tool.defaults,
                        "browser",
                        "").lower() if getattr(tool,
                                               "defaults",
                                               None) is not None else ""
            )
            if current_browser != "chromium":
                base_cfg = {}
                try:
                    base_cfg = dict(getattr(tool,
                                            "_config",
                                            {}) or {})
                except Exception:
                    base_cfg = {}
                plugin_block = dict(base_cfg.get("plugin") or {}
                                  ) if isinstance(base_cfg,
                                                  dict) else {}
                pw_block = (
                    dict(plugin_block.get("playwright") or {})
                    if isinstance(plugin_block,
                                  dict) else {}
                )
                pw_block["browser"] = "chromium"
                plugin_block["playwright"] = pw_block
                if isinstance(base_cfg, dict):
                    base_cfg["plugin"] = plugin_block
                tool = PlaywrightTool(base_cfg)
        except Exception:
            tool = PlaywrightTool({
                "plugin": {
                    "playwright": {
                        "browser": "chromium"
                    }
                }
            })

        format_name = _normalize_format(options.output_format)
        capture_headless = bool(options.headless)
        picker_headless = capture_headless
        if options.interactive_pick and _format_supports_target_selection(format_name):
            picker_headless = False
            capture_headless = True
        elif format_name == "pdf":
            picker_headless = True
            capture_headless = True

        if is_debug_enabled():
            defaults = getattr(tool, "defaults", None)
            _show_debug_panel(
                "Screenshot Config",
                [
                    ("url", options.url),
                    ("format", _normalize_format(options.output_format)),
                    ("quality", options.quality),
                    ("browser", getattr(defaults, "browser", "unknown") if defaults else "unknown"),
                    ("headless", getattr(defaults, "headless", "unknown") if defaults else "unknown"),
                    (
                        "viewport",
                        (
                            f"{getattr(defaults, 'viewport_width', '?')}x{getattr(defaults, 'viewport_height', '?')}"
                            if defaults else "<none>"
                        ),
                    ),
                    ("timeout", f"{getattr(defaults, 'navigation_timeout_ms', '?')}ms" if defaults else "<none>"),
                    ("full_page", options.full_page),
                    ("interactive_pick", options.interactive_pick),
                    ("picker_headless", picker_headless),
                    ("capture_headless", capture_headless),
                    ("target_selectors", list(options.target_selectors or [])),
                    ("destination", destination),
                ],
                border_style="magenta",
            )

        navigation_status = "loaded"
        
        if format_name == "pdf" and not options.headless:
            warnings.append(
                "pdf output requires headless Chromium; overriding headless mode"
            )
        if not _format_supports_target_selection(format_name):
            if options.interactive_pick:
                warnings.append(
                    f"{format_name} output captures the full page; interactive element picking is ignored"
                )
            if options.prefer_platform_target:
                warnings.append(
                    f"{format_name} output captures the full page; selector targeting is ignored"
                )

        try:
            element_captured = False
            if options.interactive_pick and _format_supports_target_selection(format_name):
                selected_selector = ""
                with tool.open_page(
                    headless=picker_headless,
                    emulate_viewport=picker_headless,
                    start_maximized=not picker_headless,
                ) as page:
                    navigation_status = _prepare_capture_page(
                        tool,
                        page,
                        options,
                        warnings,
                        progress,
                    )
                    progress.step("capturing locating target")
                    picked = _interactive_pick_selector(
                        page,
                        timeout_s=options.interactive_pick_timeout_s,
                    )
                    selected_selector = str(picked.get("selector") or "").strip()
                if not selected_selector:
                    raise ScreenshotError("Element picker did not return a valid selector")

                capture_mode = "interactive"
                capture_target = selected_selector

                progress.step("loading launching browser")
                with tool.open_page(headless=capture_headless) as page:
                    navigation_status = _prepare_capture_page(
                        tool,
                        page,
                        options,
                        warnings,
                        progress,
                    )
                    progress.step("capturing output")
                    _capture_selector_screenshot(
                        page,
                        selected_selector,
                        destination,
                        format_name,
                        options.selector_timeout_ms,
                        options.quality,
                    )
                    element_captured = True
            else:
                with tool.open_page(headless=capture_headless) as page:
                    navigation_status = _prepare_capture_page(
                        tool,
                        page,
                        options,
                        warnings,
                        progress,
                    )
                    # Attempt platform-specific target capture if requested (and not PDF)
                    if options.prefer_platform_target and _format_supports_target_selection(format_name):
                        progress.step("capturing locating target")
                        try:
                            _platform_preprocess(options.url, page, warnings)
                        except Exception:
                            pass
                        selectors = list(options.target_selectors or [])
                        if not selectors:
                            selectors = _selectors_for_url(options.url)

                        for sel in selectors:
                            try:
                                _capture_selector_screenshot(
                                    page,
                                    sel,
                                    destination,
                                    format_name,
                                    options.selector_timeout_ms,
                                    options.quality,
                                )
                                element_captured = True
                                capture_mode = "selector"
                                capture_target = sel
                                break
                            except PlaywrightTimeoutError:
                                continue
                            except Exception as exc:
                                warnings.append(
                                    f"element capture failed for '{sel}': {exc}"
                                )

                    # Fallback to default capture paths
                    if not element_captured:
                        if format_name == "pdf":
                            capture_mode = "pdf"
                            page.emulate_media(media="print")
                            progress.step("capturing output")
                            page.pdf(path=str(destination), print_background=True)
                        elif format_name == "mhtml":
                            capture_mode = "mhtml"
                            progress.step("capturing output")
                            _capture_mhtml(page, destination)
                        else:
                            screenshot_kwargs: Dict[str, Any] = {
                                "path": str(destination)
                            }
                            if format_name == "jpeg":
                                screenshot_kwargs["type"] = "jpeg"
                                screenshot_kwargs["quality"] = _jpeg_quality_from_level(options.quality)
                            if options.full_page:
                                progress.step("capturing output")
                                page.screenshot(full_page=True, **screenshot_kwargs)
                                capture_mode = "full-page"
                            else:
                                article = page.query_selector("article")
                                if article is not None:
                                    article_kwargs = dict(screenshot_kwargs)
                                    article_kwargs.pop("full_page", None)
                                    progress.step("capturing output")
                                    article.screenshot(**article_kwargs)
                                    capture_mode = "article"
                                    capture_target = "article"
                                else:
                                    progress.step("capturing output")
                                    page.screenshot(**screenshot_kwargs)
                                    capture_mode = "page"

            if element_captured or capture_mode:
                progress.step("capturing saved")

            if is_debug_enabled():
                _show_debug_panel(
                    "Screenshot Capture",
                    [
                        ("url", options.url),
                        ("navigation", navigation_status),
                        ("mode", capture_mode),
                        ("target", capture_target),
                        ("wait_after_load_s", options.wait_after_load),
                        ("warnings", len(warnings)),
                        ("saved_to", destination),
                    ],
                )
        except Exception as exc:
            if is_debug_enabled():
                _show_debug_panel(
                    "Screenshot Error",
                    [
                        ("url", options.url),
                        ("destination", destination),
                        ("error", exc),
                    ],
                    border_style="red",
                )
            msg = str(exc).lower()
            if any(k in msg for k in ["executable", "not found", "no such file",
                                      "cannot find", "install"]):
                raise ScreenshotError(
                    "Chromium Playwright browser binaries not found. Install them: python ./scripts/bootstrap.py --playwright-only --browsers chromium"
                ) from exc
            raise
    except ScreenshotError:
        # Re-raise ScreenshotError raised intentionally (do not wrap)
        raise
    except Exception as exc:
        raise ScreenshotError(f"Failed to capture screenshot: {exc}") from exc
    return capture_mode, capture_target


def _capture_screenshot(
    options: ScreenshotOptions,
    progress: PipelineProgress
) -> ScreenshotResult:
    """Capture a screenshot for the given options."""
    requested_format = _normalize_format(options.output_format)
    destination = _prepare_output_path(options)
    warnings: List[str] = []
    capture_mode = ""
    capture_target = ""

    will_target = bool(options.prefer_platform_target or options.interactive_pick) and _format_supports_target_selection(requested_format)
    will_convert = requested_format == "webp"
    will_archive = bool(options.archive and options.url)
    interactive_extra_steps = 5 if (options.interactive_pick and _format_supports_target_selection(requested_format)) else 0
    total_steps = (
        9 + (1 if will_target else 0) + interactive_extra_steps +
        (1 if will_convert else 0) + (1 if will_archive else 0)
    )
    progress.begin_steps(total_steps)
    progress.step("loading starting")

    # Playwright screenshots do not natively support WebP output.
    # Capture as PNG, then convert via Pillow.
    capture_path = destination
    if requested_format == "webp":
        capture_path = unique_path(destination.with_suffix(".png"))
        options.output_format = "png"
    capture_mode, capture_target = _capture(options, capture_path, warnings, progress)

    if requested_format == "webp":
        progress.step("capturing converting to webp")
        try:
            webp_settings = _webp_quality_settings(options.quality)
            did_downscale = _convert_to_webp(
                capture_path,
                destination,
                quality=int(webp_settings["quality"]),
                method=int(webp_settings["method"]),
                lossless=bool(webp_settings["lossless"]),
            )
            if did_downscale:
                try:
                    destination.unlink(missing_ok=True)
                except Exception:
                    pass
                destination = capture_path
                warnings.append(
                    f"webp conversion required downscaling to fit {WEBP_MAX_DIM}px limit; using original png instead: {capture_path.name}"
                )
            else:
                try:
                    capture_path.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            warnings.append(f"webp conversion failed; keeping png: {exc}")
            destination = capture_path

    # Build URL list from captured url and any archives
    url: List[str] = [options.url] if options.url else []
    archive_url: List[str] = []
    if options.archive and options.url:
        progress.step("capturing archiving")
        archives, archive_warnings = _archive_url(options.url, options.archive_timeout)
        archive_url.extend(archives)
        warnings.extend(archive_warnings)
        if archives:
            url = unique_preserve_order([*url, *archives])

    progress.step("capturing finalized")

    applied_tag = unique_preserve_order(list(tag for tag in options.tag if tag.strip()))

    if is_debug_enabled():
        _show_debug_panel(
            "Screenshot Output",
            [
                ("url", options.url),
                ("requested_format", requested_format),
                ("path", destination),
                ("capture_mode", capture_mode),
                ("capture_target", capture_target),
                ("archives", archive_url),
                ("warnings", warnings),
            ],
        )

    return ScreenshotResult(
        path=destination,
        tag_applied=applied_tag,
        archive_url=archive_url,
        url=url,
        capture_mode=capture_mode,
        capture_target=capture_target,
        warnings=warnings,
    )


# ============================================================================
# Main Cmdlet Function
# ============================================================================


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Take screenshots of URL inputs from args or pipeline items."""
    if should_show_help(args):
        log(f"Cmdlet: {CMDLET.name}\nSummary: {CMDLET.summary}\nUsage: {CMDLET.usage}")
        return 0

    progress = PipelineProgress(pipeline_context)

    parsed = parse_cmdlet_args(args, CMDLET)

    format_value = parsed.get("format")
    capture_mode_value = _normalize_capture_mode(parsed.get("capture_mode"))
    raw_quality_value = parsed.get("quality")
    adblock_value = parsed.get("adblock")
    quality_value: Optional[int] = None
    if not format_value:
        try:
            plugin_cfg = config.get("plugin", {}) if isinstance(config, dict) else {}
            pw_cfg = plugin_cfg.get("playwright") if isinstance(plugin_cfg, dict) else None
            if isinstance(pw_cfg, dict):
                format_value = pw_cfg.get("format")
        except Exception:
            pass
    if not format_value:
        format_value = "webp"

    if raw_quality_value not in (None, ""):
        quality_value = _normalize_quality(raw_quality_value)
    else:
        try:
            plugin_cfg = config.get("plugin", {}) if isinstance(config, dict) else {}
            pw_cfg = plugin_cfg.get("playwright") if isinstance(plugin_cfg, dict) else None
            if isinstance(pw_cfg, dict) and pw_cfg.get("screenshot_quality") not in (None, ""):
                quality_value = _normalize_quality(pw_cfg.get("screenshot_quality"))
        except Exception:
            quality_value = None
    if quality_value is None:
        quality_value = _normalize_quality(None)
    adblock_enabled = _normalize_bool(adblock_value, default=True)

    storage_value = parsed.get("storage")
    selector_arg = parsed.get("selector")
    selectors = [selector_arg] if selector_arg else []
    archive_enabled = parsed.get("archive", False)

    url_arg = parsed.get("url")
    positional_url = [str(url_arg)] if url_arg else []

    url_to_process: List[Tuple[str, Any]] = []
    if positional_url:
        url_to_process = [(u, None) for u in positional_url]
    else:
        piped_results = normalize_result_input(result)
        if piped_results:
            for item in piped_results:
                url = get_field(item, "path") or get_field(item, "url") or get_field(item, "target")
                if url:
                    url_to_process.append((str(url), item))

    if not url_to_process:
        log("No url to process for screen-shot cmdlet", file=sys.stderr)
        return 1

    screenshot_dir: Optional[Path] = None
    screenshot_dir_source = "default temp"
    if storage_value:
        try:
            screenshot_dir = SharedArgs.resolve_storage(storage_value)
            screenshot_dir_source = f"--storage {storage_value}"
        except ValueError as exc:
            log(str(exc), file=sys.stderr)
            return 1
    if screenshot_dir is None and resolve_output_dir is not None:
        try:
            screenshot_dir = resolve_output_dir(config)
            screenshot_dir_source = "config resolver"
        except Exception:
            pass
    if screenshot_dir is None and config and config.get("outfile"):
        try:
            screenshot_dir = Path(config["outfile"]).expanduser()
            screenshot_dir_source = "config outfile"
        except Exception:
            pass
    if screenshot_dir is None:
        screenshot_dir = Path(tempfile.gettempdir())

    ensure_directory(screenshot_dir)

    format_name = _normalize_format(format_value)
    filtered_selectors = [str(s).strip() for s in selectors if str(s).strip()]
    manual_target_selectors = filtered_selectors if filtered_selectors else None
    interactive_default = bool(len(url_to_process) == 1 and _stdin_interactive())

    if is_debug_enabled():
        _show_debug_panel(
            "screen-shot",
            [
                ("args", list(args)),
                ("url_count", len(url_to_process)),
                ("urls", [u for u, _ in url_to_process]),
                ("archive", archive_enabled),
                ("format", format_name),
                ("quality", quality_value),
                ("adblock", adblock_enabled),
                ("capture_mode", capture_mode_value or ("interactive" if interactive_default and _format_supports_target_selection(format_name) else "auto")),
                ("output_dir", screenshot_dir),
                ("output_dir_source", screenshot_dir_source),
            ],
        )

    try:
        progress.ensure_local_ui(
            label="screen-shot",
            total_items=len(url_to_process),
            items_preview=[u for u, _ in url_to_process],
        )
    except Exception:
        pass

    shared_playwright_tool: Optional[Any] = None
    try:
        if isinstance(config, dict):
            plugin_block = dict(config.get("plugin") or {})
            pw_block = dict(plugin_block.get("playwright") or {})
            pw_block["browser"] = "chromium"
            pw_block["user_agent"] = "native"
            pw_block["viewport_width"] = int(DEFAULT_VIEWPORT.get("width", 1920))
            pw_block["viewport_height"] = int(DEFAULT_VIEWPORT.get("height", 1080))
            plugin_block["playwright"] = pw_block
            pw_local_cfg = dict(config)
            pw_local_cfg["plugin"] = plugin_block
        else:
            pw_local_cfg = {
                "plugin": {
                    "playwright": {
                        "browser": "chromium",
                        "user_agent": "native",
                        "viewport_width": int(DEFAULT_VIEWPORT.get("width", 1920)),
                        "viewport_height": int(DEFAULT_VIEWPORT.get("height", 1080)),
                    }
                }
            }
        shared_playwright_tool = PlaywrightTool(pw_local_cfg)
    except Exception:
        shared_playwright_tool = None

    all_emitted = []
    exit_code = 0

    def _extract_item_tags(item: Any) -> List[str]:
        return extract_item_tags(item)

    def _extract_item_title(item: Any) -> str:
        return get_result_title(item, "title", "name", "filename") or ""

    def _clean_title(text: str) -> str:
        value = (text or "").strip()
        if value.lower().startswith("screenshot:"):
            value = value.split(":", 1)[1].strip()
        return value

    for url, origin_item in url_to_process:
        if not url.lower().startswith(("http://", "https://", "file://")):
            log(f"[screen_shot] Skipping non-URL input: {url}", file=sys.stderr)
            continue

        try:
            options = ScreenshotOptions(
                url=url,
                output_dir=screenshot_dir,
                output_format=format_name,
                archive=archive_enabled,
                target_selectors=None,
                prefer_platform_target=False,
                wait_for_article=False,
                full_page=True,
                interactive_pick=False,
                quality=quality_value,
                adblock=adblock_enabled,
                playwright_tool=shared_playwright_tool,
            )

            auto_selectors = _matched_site_selectors(url)
            if manual_target_selectors:
                options.prefer_platform_target = True
                options.target_selectors = manual_target_selectors
            elif capture_mode_value == "full":
                options.prefer_platform_target = False
                options.target_selectors = None
            elif capture_mode_value == "interactive":
                options.interactive_pick = True
            elif interactive_default and _format_supports_target_selection(format_name):
                options.interactive_pick = True
            elif auto_selectors:
                options.prefer_platform_target = True
                options.target_selectors = auto_selectors

            screenshot_result = _capture_screenshot(options, progress)

            screenshot_hash = None
            try:
                screenshot_hash = sha256_file(screenshot_result.path)
            except Exception:
                pass

            try:
                capture_date = datetime.fromtimestamp(screenshot_result.path.stat().st_mtime).date().isoformat()
            except Exception:
                capture_date = datetime.now().date().isoformat()

            upstream_title = _clean_title(_extract_item_title(origin_item))
            url_title = _title_from_url(url)
            display_title = upstream_title or url_title or url

            upstream_tags = _extract_item_tags(origin_item)
            filtered_upstream_tags = [
                tag for tag in upstream_tags
                if not str(tag).strip().lower().startswith(("type:", "date:"))
            ]
            url_tags = _tags_from_url(url)
            merged_tags = unique_preserve_order(
                ["type:screenshot", f"date:{capture_date}"] + filtered_upstream_tags + url_tags
            )

            pipe_obj = create_pipe_object_result(
                source="screenshot",
                store="PATH",
                identifier=Path(screenshot_result.path).stem,
                file_path=str(screenshot_result.path),
                cmdlet_name="screen-shot",
                title=display_title,
                hash_value=screenshot_hash,
                is_temp=True,
                parent_hash=hashlib.sha256(url.encode()).hexdigest(),
                tag=merged_tags,
                url=url,
                source_url=url,
                extra={
                    "source_url": url,
                    "archive_url": screenshot_result.archive_url,
                    "url": screenshot_result.url,
                    "target": str(screenshot_result.path),
                },
            )

            pipeline_context.emit(pipe_obj)
            all_emitted.append(pipe_obj)

            if is_debug_enabled():
                _show_debug_panel(
                    "screen-shot output",
                    [
                        ("path", screenshot_result.path),
                        ("hash", screenshot_hash),
                        ("title", display_title),
                        ("capture_mode", screenshot_result.capture_mode),
                        ("capture_target", screenshot_result.capture_target),
                        ("tags", merged_tags),
                        ("archives", screenshot_result.archive_url),
                        ("warnings", screenshot_result.warnings),
                    ],
                )

            progress.on_emit(pipe_obj)

        except ScreenshotError as exc:
            log(f"Error taking screenshot of {url}: {exc}", file=sys.stderr)
            exit_code = 1
        except Exception as exc:
            log(f"Unexpected error taking screenshot of {url}: {exc}", file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            exit_code = 1

    progress.close_local_ui(force_complete=True)

    if not all_emitted:
        log("No screenshots were successfully captured", file=sys.stderr)
        return 1

    try:
        subject = all_emitted[0] if len(all_emitted) == 1 else list(all_emitted)
        display_type = "item" if len(all_emitted) <= 9 else "custom"
        sh.display_and_persist_items(
            list(all_emitted),
            title="Screenshot",
            subject=subject,
            display_type=display_type,
        )
    except Exception:
        status_panel(
            "Screenshot",
            [("captured", f"{len(all_emitted)} screenshot(s)")],
        )
    return exit_code


CMDLET = Cmdlet(
    name="screen-shot",
    summary="Capture a website screenshot",
    usage="screen-shot <url> [options] [-query \"format:webp quality:10 mode:full\"]",
    alias=["screenshot",
           "ss"],
    arg=[
        SharedArgs.URL,
        sh.QueryArg(
            "format",
            key="format",
            type="string",
            choices=["webp", "png", "jpeg", "jpg", "pdf", "mhtml", "mht"],
            query_only=True,
            description="Output format via -query, e.g. format:webp, format:pdf, or format:mhtml"
        ),
        sh.QueryArg(
            "capture_mode",
            key="mode",
            aliases=["capture", "mode"],
            choices=["full", "interactive"],
            query_only=True,
            description="Capture mode via -query, e.g. mode:full or mode:interactive"
        ),
        sh.QueryArg(
            "quality",
            key="quality",
            choices=["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"],
            query_only=True,
            description="Screenshot quality via -query, 1-10. 10 uses highest quality and lossless webp."
        ),
        sh.QueryArg(
            "adblock",
            key="adblock",
            aliases=["ads", "blockads"],
            choices=["true", "false", "on", "off", "yes", "no", "1", "0"],
            handler=lambda value: _normalize_bool(value, default=True),
            query_only=True,
            description="Ad and tracker blocking via -query. Defaults to true; use adblock:false to disable."
        ),
        CmdletArg(
            name="selector",
            type="string",
            description="CSS selector for element capture"
        ),
        SharedArgs.PATH,
        SharedArgs.QUERY,
    ],
    detail=[
        "Uses Playwright Chromium engine only. Install Chromium with: python ./scripts/bootstrap.py --playwright-only --browsers chromium",
        "PDF output requires headless Chromium (the cmdlet will enforce headless mode for PDF).",
        "MHTML output uses Chromium page snapshots to save the full page as a single archival file.",
        "Basic ad and tracker blocking is enabled by default during capture so MHTML archives are less likely to embed ad content.",
        "Screenshots are temporary artifacts stored in the configured `temp` directory.",
        "Interactive single-URL runs open a headful browser picker by default so you can hover and click the element to capture.",
        "Use -query \"mode:full\" to bypass the picker and capture the full page directly.",
        "Use -query \"format:webp\", \"format:pdf\", or \"format:mhtml\" to choose the output format.",
        "Use -query \"adblock:false\" if a site breaks and you need the raw unfiltered page.",
        "Use -query \"quality:1\" through \"quality:10\" to control jpeg/webp compression. quality:10 uses lossless webp.",
    ],
)

CMDLET.exec = _run
