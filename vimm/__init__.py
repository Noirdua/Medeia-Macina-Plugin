"""Minimal Vimm provider: table-row parsing for display.

This minimal implementation focuses on fetching a Vimm search result page,
turning the vault table rows into SearchResults, and letting the CLI
auto-insert the download-file stage directly from the first table so that
Playwright-driven downloads happen without showing a nested detail table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, parse_qs, urljoin, urlparse, urlunparse, urlencode
from lxml import html as lxml_html
import base64
import re
from pathlib import Path

from API.HTTP import HTTPClient
from PluginCore.base import Plugin, SearchResult, parse_inline_query_arguments
from PluginCore.inline_utils import resolve_filter
from SYS.logger import debug, debug_panel
from SYS.plugin_helpers import TablePluginMixin
from plugins.playwright import PlaywrightTool


class Vimm(TablePluginMixin, Plugin):
    """Minimal provider for vimm.net vault listings using TableProvider mixin.

    NOTES / HOW-TO (selection & auto-download):
      - This provider exposes file rows on a detail page. Each file row includes
        a `path` which is an absolute download URL (or a form action + mediaId).

      - To make `@N` expansion robust (so users can do `@1 | add-file -instance <x>`)
        we ensure three things:
          1) The ResultTable produced by the `selector()` sets `source_command` to
             "download-file" (the canonical cmdlet for downloading files).
          2) Each row carries explicit selection args: `['-url', '<full-url>']`.
             Using an explicit `-url` flag avoids ambiguity during argument
             parsing (some cmdlets accept positional URLs, others accept flags).
             3) The CLI's expansion logic places selection args *before* plugin
                 source args (e.g., `-plugin vimm`) so the first positional token is
                 the intended URL (not an unknown flag like `-plugin`).

      - Why this approach? Argument parsing treats the *first* unrecognized token
        as a positional value (commonly interpreted as a URL). If a plugin
        injects hints like `-plugin vimm` *before* a bare URL, the parser can
        misinterpret `-plugin` as the URL, causing confusing attempts to
        download `-plugin`. By using `-url` and ensuring the URL appears first
        we avoid that class of bugs and make `@N` -> `download-file`/`add-file`
        flows reliable.

    The code below implements these choices (and contains inline comments
    explaining specific decisions)."""

    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})
    URL = ("https://vimm.net/vault/",)
    URL_DOMAINS = ("vimm.net",)

    def get_source_command(self, args_list: List[str]) -> Tuple[str, List[str]]:
        return "search-file", ["-plugin", self.name]

    REGION_CHOICES = [
        {"value": "1", "text": "Argentina"},
        {"value": "2", "text": "Asia"},
        {"value": "3", "text": "Australia"},
        {"value": "35", "text": "Austria"},
        {"value": "31", "text": "Belgium"},
        {"value": "4", "text": "Brazil"},
        {"value": "5", "text": "Canada"},
        {"value": "6", "text": "China"},
        {"value": "38", "text": "Croatia"},
        {"value": "7", "text": "Denmark"},
        {"value": "8", "text": "Europe"},
        {"value": "9", "text": "Finland"},
        {"value": "10", "text": "France"},
        {"value": "11", "text": "Germany"},
        {"value": "12", "text": "Greece"},
        {"value": "13", "text": "Hong Kong"},
        {"value": "27", "text": "India"},
        {"value": "33", "text": "Ireland"},
        {"value": "34", "text": "Israel"},
        {"value": "14", "text": "Italy"},
        {"value": "15", "text": "Japan"},
        {"value": "16", "text": "Korea"},
        {"value": "30", "text": "Latin America"},
        {"value": "17", "text": "Mexico"},
        {"value": "18", "text": "Netherlands"},
        {"value": "40", "text": "New Zealand"},
        {"value": "19", "text": "Norway"},
        {"value": "28", "text": "Poland"},
        {"value": "29", "text": "Portugal"},
        {"value": "20", "text": "Russia"},
        {"value": "32", "text": "Scandinavia"},
        {"value": "37", "text": "South Africa"},
        {"value": "21", "text": "Spain"},
        {"value": "22", "text": "Sweden"},
        {"value": "36", "text": "Switzerland"},
        {"value": "23", "text": "Taiwan"},
        {"value": "39", "text": "Turkey"},
        {"value": "41", "text": "United Arab Emirates"},
        {"value": "24", "text": "United Kingdom"},
        {"value": "25", "text": "USA"},
        {"value": "26", "text": "World"},
    ]

    QUERY_ARG_CHOICES = {
        "system": [
            "Atari2600",
            "Atari5200",
            "Atari7800",
            "CDi",
            "Dreamcast",
            "GB",
            "GBA",
            "GBC",
            "GG",
            "GameCube",
            "Genesis",
            "Jaguar",
            "JaguarCD",
            "Lynx",
            "SMS",
            "NES",
            "3DS",
            "N64",
            "DS",
            "PS1",
            "PS2",
            "PS3",
            "PSP",
            "Saturn",
            "32X",
            "SegaCD",
            "SNES",
            "TG16",
            "TGCD",
            "VB",
            "Wii",
            "WiiWare",
            "Xbox",
            "Xbox360",
            "X360-D",
        ],
        "region": REGION_CHOICES,
    }
    # PluginCore still looks for INLINE_QUERY_FIELD_CHOICES, so expose this
    # mapping once and keep QUERY_ARG_CHOICES as the readable name we prefer.
    INLINE_QUERY_FIELD_CHOICES = QUERY_ARG_CHOICES

    # Table metadata/constants grouped near the table helpers below.
    TABLE_AUTO_STAGES = {"vimm": ["download-file"]}
    AUTO_STAGE_USE_SELECTION_ARGS = True
    TABLE_SYSTEM_COLUMN = {"label": "Platform", "metadata_key": "system"}

    def validate(self) -> bool:
        return True

    def search(self, query: str, limit: int = 50, filters: Optional[Dict[str, Any]] = None, **kwargs: Any) -> List[SearchResult]:
        q = (query or "").strip()
        if not q:
            return []

        base = "https://vimm.net/vault/"
        normalized_filters: Dict[str, Any] = {}
        for key, value in (filters or {}).items():
            if key is None:
                continue
            normalized_filters[str(key).lower()] = value

        system_value = normalized_filters.get("system") or normalized_filters.get("platform")
        system_param = str(system_value or "").strip()

        region_value = normalized_filters.get("region")
        region_param = str(region_value or "").strip()

        params = [("p", "list"), ("q", q)]
        if system_param:
            params.append(("system", system_param))
        if region_param:
            params.append(("region", region_param))
        url = f"{base}?{urlencode(params)}"
        debug_panel(
            "vimm search",
            [
                ("query", q),
                ("url", url),
                ("system", system_param or "<any>"),
                ("region", region_param or "<any>"),
                ("filters", normalized_filters or "<none>"),
            ],
            border_style="cyan",
        )

        try:
            with HTTPClient(timeout=9.0) as client:
                resp = client.get(url)
                content = resp.content
        except Exception as exc:
            debug(f"[vimm] HTTP fetch failed: {exc}")
            return []

        try:
            doc = lxml_html.fromstring(content)
        except Exception as exc:
            debug(f"[vimm] HTML parse failed: {exc}")
            return []

        xpaths = [
            "//table//tbody/tr",
            "//table//tr[td]",
            "//div[contains(@class,'list-item')]",
            "//div[contains(@class,'result')]",
            "//li[contains(@class,'item')]",
        ]

        rows = doc.xpath("//table//tr[td]")
        results = self._build_results_from_rows(rows, url, system_param, limit)
        if not results:
            results = self.search_table_from_url(url, limit=limit, xpaths=xpaths)
            self._ensure_system_column(results, system_param)

        results = [self._apply_selection_defaults(r, referer=url, detail_url=getattr(r, "path", "")) for r in (results or [])]

        debug_panel(
            "vimm search results",
            [
                ("query", q),
                ("results", len(results)),
                ("url", url),
            ],
            border_style="cyan",
        )
        return results[: int(limit)]

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        normalized, inline_args = parse_inline_query_arguments(query)
        inline_args_norm: Dict[str, Any] = {}
        for k, v in (inline_args or {}).items():
            if k is None:
                continue
            key_norm = str(k).strip().lower()
            if key_norm == "platform":
                key_norm = "system"
            inline_args_norm[key_norm] = v

        filters = resolve_filter(self, inline_args_norm)
        return normalized, filters

    def _build_results_from_rows(
        self,
        rows: List[Any],
        base_url: str,
        system_value: Optional[str],
        limit: int,
    ) -> List[SearchResult]:
        out: List[SearchResult] = []
        seen: set[str] = set()
        system_column = getattr(self, "TABLE_SYSTEM_COLUMN", {}) or {}
        key = str(system_column.get("metadata_key") or "system").strip()
        if not key:
            key = "system"

        for tr in rows:
            if len(out) >= limit:
                break
            rec = self._parse_table_row(tr, base_url, system_value)
            if not rec:
                continue
            path = rec.get("path")
            if not path or path in seen:
                continue
            seen.add(path)
            columns = self._build_columns_from_record(rec)
            if not columns:
                continue
            metadata: Dict[str, Any] = {"raw_record": rec, "detail_url": path, "referer": base_url}
            if path:
                metadata["_selection_args"] = ["-url", path]
            platform_value = rec.get("platform")
            if platform_value:
                metadata[key] = platform_value
            sr = SearchResult(
                table="vimm",
                title=rec.get("title") or "",
                path=path,
                detail="",
                annotations=[],
                media_kind="file",
                size_bytes=None,
                tag={"vimm"},
                columns=columns,
                full_metadata=metadata,
            )
            out.append(self._apply_selection_defaults(sr, referer=base_url, detail_url=path))
        return out

    def _parse_table_row(self, tr: Any, base_url: str, system_value: Optional[str]) -> Dict[str, str]:
        tds = tr.xpath("./td")
        if not tds:
            return {}

        rec: Dict[str, str] = {}
        title_anchor = tds[0].xpath('.//a[contains(@href,"/vault/")]') or []
        if title_anchor:
            el = title_anchor[0]
            rec["title"] = (el.text_content() or "").strip()
            href = el.get("href") or ""
            rec["path"] = urljoin(base_url, href) if href else ""
            if system_value:
                rec["platform"] = str(system_value).strip().upper()
            rec["region"] = self._flag_text_at(tds, 1)
            rec["version"] = self._text_at(tds, 2)
            rec["languages"] = self._text_at(tds, 3)
        else:
            raw_platform = (tds[0].text_content() or "").strip()
            if raw_platform:
                rec["platform"] = raw_platform.upper()
            anchors = tds[1].xpath('.//a[contains(@href,"/vault/")]') or tds[1].xpath('.//a')
            if not anchors:
                return {}
            el = anchors[0]
            rec["title"] = (el.text_content() or "").strip()
            href = el.get("href") or ""
            rec["path"] = urljoin(base_url, href) if href else ""
            rec["region"] = self._flag_text_at(tds, 2)
            rec["version"] = self._text_at(tds, 3)
            rec["languages"] = self._text_at(tds, 4)

        return {k: v for k, v in rec.items() if v}

    def _text_at(self, tds: List[Any], idx: int) -> str:
        if idx < 0 or idx >= len(tds):
            return ""
        return (tds[idx].text_content() or "").strip()

    def _flag_text_at(self, tds: List[Any], idx: int) -> str:
        if idx < 0 or idx >= len(tds):
            return ""
        td = tds[idx]
        imgs = td.xpath('.//img[contains(@class,"flag")]/@title')
        if imgs:
            return str(imgs[0]).strip()
        return (td.text_content() or "").strip()

    def _build_columns_from_record(self, rec: Dict[str, str]) -> List[Tuple[str, str]]:
        title = rec.get("title") or ""
        if not title:
            return []
        columns: List[Tuple[str, str]] = [("Title", title)]
        system_column = getattr(self, "TABLE_SYSTEM_COLUMN", {}) or {}
        label = str(system_column.get("label") or "Platform")
        platform_value = rec.get("platform")
        if platform_value:
            columns.append((label, platform_value))
        for key, friendly in (("region", "Region"), ("version", "Version"), ("languages", "Languages")):
            value = rec.get(key)
            if value:
                columns.append((friendly, value))
        return columns

    def _apply_selection_defaults(self, sr: SearchResult, *, referer: Optional[str], detail_url: Optional[str]) -> SearchResult:
        """Attach selection metadata so @N expansion passes a usable URL first."""

        try:
            md = dict(getattr(sr, "full_metadata", {}) or {})
        except Exception:
            md = {}

        path_val = str(getattr(sr, "path", "") or "")
        if not path_val:
            path_val = str(detail_url or "")

        if path_val:
            md.setdefault("_selection_args", ["-url", path_val])
            md.setdefault("detail_url", detail_url or path_val)
        if referer:
            md.setdefault("referer", referer)

        sr.full_metadata = md
        return sr

    def _ensure_system_column(self, results: List[SearchResult], system_value: Optional[str]) -> None:
        if not results or not system_value:
            return
        label_value = str(system_value).strip()
        if not label_value:
            return
        label_value = label_value.upper()
        system_column = getattr(self, "TABLE_SYSTEM_COLUMN", {}) or {}
        label_name = str(system_column.get("label") or "Platform").strip()
        if not label_name:
            label_name = "Platform"
        normalized_label = label_name.strip().lower()
        metadata_key = str(system_column.get("metadata_key") or "system").strip()
        if not metadata_key:
            metadata_key = "system"
        for result in results:
            try:
                cols = getattr(result, "columns", None)
                if isinstance(cols, list):
                    lowered = {str(name or "").strip().lower() for name, _ in cols}
                    if normalized_label not in lowered:
                        insert_pos = 1 if cols else 0
                        cols.insert(insert_pos, (label_name, label_value))
                metadata = getattr(result, "full_metadata", None)
                if isinstance(metadata, dict):
                    metadata.setdefault(metadata_key, label_value)
            except Exception:
                continue

    def _parse_detail_doc(self, doc, base_url: str) -> List[Any]:
        """Parse a Vimm detail page (non-standard table layout) and return a list
        of SearchResult or dict payloads suitable for `ResultTable.add_result()`.

        The function extracts simple key/value rows and file download entries (anchors
        or download forms) and returns property dicts first followed by file SearchResults.
        """
        def _build_download_url(action_url: str, params: Dict[str, str]) -> str:
            if not action_url:
                return ""
            if not params:
                return action_url
            cleaned = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
            if not cleaned:
                return action_url
            parsed = urlparse(action_url)
            existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
            existing.update(cleaned)
            query = urlencode(existing, doseq=True)
            return urlunparse(parsed._replace(query=query))

        try:
            # Prefer the compact 'rounded' detail table when present
            tables = doc.xpath('//table[contains(@class,"rounded") and contains(@class,"cellpadding1")]') or doc.xpath('//table[contains(@class,"rounded")]')
            if not tables:
                return []

            tbl = tables[0]
            trs = tbl.xpath('.//tr') or []

            # Aggregate page properties into a mapping and create file rows with Title, Region, CRC, Version
            props: Dict[str, Any] = {}
            anchors_by_label: Dict[str, List[Dict[str, str]]] = {}

            for tr in trs:
                try:
                    if tr.xpath('.//hr'):
                        continue
                    tds = tr.xpath('./td')
                    if not tds:
                        continue

                    # Canvas-based title row (base64 encoded in data-v)
                    canvas = tr.xpath('.//canvas[@data-v]')
                    if canvas:
                        data_v = canvas[0].get('data-v') or ''
                        try:
                            raw = base64.b64decode(data_v)
                            txt = raw.decode('utf-8', errors='ignore').strip()
                        except Exception:
                            txt = (canvas[0].text_content() or '').strip()
                        if txt:
                            props['Title'] = txt
                        continue

                    label = (tds[0].text_content() or '').strip()
                    if not label:
                        continue
                    val_td = tds[-1]

                    # collect anchors under this label for later detection
                    anchors = val_td.xpath('.//a')
                    if anchors:
                        entries = []
                        for a in anchors:
                            entries.append({'text': (a.text_content() or '').strip(), 'href': a.get('href') or ''})
                        # try to capture any explicit span value (e.g., CRC) even if an anchor exists
                        span_data = val_td.xpath('.//span[@id]/text()')
                        if span_data:
                            props[label] = str(span_data[0]).strip()
                        else:
                            # fallback to direct text nodes excluding anchor text
                            txts = [t.strip() for t in val_td.xpath('.//text()') if t.strip()]
                            anchor_texts = [a.text_content().strip() for a in anchors if a.text_content()]
                            filtered = [t for t in txts if t not in anchor_texts]
                            if filtered:
                                props[label] = filtered[0]
                        anchors_by_label[label] = entries
                        continue

                    img_title = val_td.xpath('.//img/@title')
                    if img_title:
                        val = str(img_title[0]).strip()
                    else:
                        span_data = val_td.xpath('.//span[@id]/text()')
                        if span_data:
                            val = str(span_data[0]).strip()
                        else:
                            opt = val_td.xpath('.//select/option[@selected]/text()')
                            if opt:
                                val = str(opt[0]).strip()
                            else:
                                vt = val_td.xpath('.//div[@id="version_text"]/text()')
                                if vt:
                                    val = vt[0].strip()
                                else:
                                    val = (val_td.text_content() or '').strip()

                    props[label] = val
                except Exception:
                    continue

            # Download form handling: find action, mediaId, and dl_size
            form = doc.xpath('//form[@id="dl_form"]')
            action = ''
            media_id = None
            dl_size = None
            form_inputs: Dict[str, str] = {}
            download_url = ''
            if form:
                f = form[0]
                action = f.get('action') or ''
                if action.startswith('//'):
                    action = 'https:' + action
                elif action.startswith('/'):
                    action = urljoin(base_url, action)
                media_ids = f.xpath('.//input[@name="mediaId"]/@value')
                media_id = media_ids[0] if media_ids else None
                size_vals = doc.xpath('//td[@id="dl_size"]/text()')
                dl_size = size_vals[0].strip() if size_vals else None
                inputs = f.xpath('.//input[@name]')
                for inp in inputs:
                    name = (inp.get('name') or '').strip()
                    if not name:
                        continue
                    form_inputs[name] = inp.get('value') or ''
                download_url = _build_download_url(action, form_inputs)

            file_results: List[SearchResult] = []

            # Create file rows from anchors that look like downloads
            for lbl, alist in anchors_by_label.items():
                for a in alist:
                    href = a.get('href') or ''
                    txt = a.get('text') or ''
                    is_download_link = False
                    if href:
                        low = href.lower()
                        if 'p=download' in low or '/download' in low or '/dl' in low:
                            is_download_link = True
                        for ext in ('.zip', '.nes', '.gba', '.bin', '.7z', '.iso'):
                            if low.endswith(ext):
                                is_download_link = True
                                break
                    if txt and re.search(r"\.[a-z0-9]{1,5}$", txt, re.I):
                        is_download_link = True
                    if not is_download_link:
                        continue

                    title = txt or props.get('Title') or ''
                    path = urljoin(base_url, href) if href else ''
                    cols = [("Title", title), ("Region", props.get('Region', '')), ("CRC", props.get('CRC', '')), ("Version", props.get('Version', ''))]
                    if dl_size:
                        cols.append(("Size", dl_size))
                    metadata: Dict[str, Any] = {"raw_record": {"label": lbl}}
                    if base_url:
                        metadata["referer"] = base_url
                        metadata.setdefault("detail_url", base_url)
                    sr = SearchResult(table="vimm", title=title, path=path, detail="", annotations=[], media_kind="file", size_bytes=None, tag={"vimm"}, columns=cols, full_metadata=metadata)
                    file_results.append(self._apply_selection_defaults(sr, referer=base_url, detail_url=base_url))

            # If no explicit file anchors, but we have a form, create a single file entry using page properties
            if not file_results and (media_id or action):
                # Ensure CRC is captured even if earlier parsing missed it
                if not props.get('CRC'):
                    try:
                        crc_vals = doc.xpath('//span[@id="data-crc"]/text()')
                        if crc_vals:
                            props['CRC'] = str(crc_vals[0]).strip()
                    except Exception:
                        pass

                title = props.get('Title') or ''
                cols = [("Title", title), ("Region", props.get('Region', '')), ("CRC", props.get('CRC', '')), ("Version", props.get('Version', ''))]
                if dl_size:
                    cols.append(("Size", dl_size))
                target_path = download_url or action or base_url
                sr = SearchResult(
                    table="vimm",
                    title=title,
                    path=target_path,
                    detail="",
                    annotations=[],
                    media_kind="file",
                    size_bytes=None,
                    tag={"vimm"},
                    columns=cols,
                    full_metadata={
                        "mediaId": media_id,
                        "dl_action": action,
                        "download_url": download_url,
                        "form_params": dict(form_inputs),
                        "referer": base_url,
                        "raw_props": props,
                    },
                )
                file_results.append(self._apply_selection_defaults(sr, referer=base_url, detail_url=base_url))

            # Attach mediaId/dl_action to file rows
            if file_results and (media_id or action):
                for fi in file_results:
                    try:
                        fi.full_metadata = dict(getattr(fi, 'full_metadata', {}) or {})
                        if media_id:
                            fi.full_metadata['mediaId'] = media_id
                        if action:
                            fi.full_metadata['dl_action'] = action
                        if form_inputs:
                            fi.full_metadata.setdefault('form_params', dict(form_inputs))
                        if download_url:
                            fi.full_metadata['download_url'] = download_url
                        if dl_size and not any((k.lower() == 'size') for k, _ in getattr(fi, 'columns', [])):
                            fi.columns.append(("Size", dl_size))
                    except Exception:
                        continue

            # Return only file rows (properties are attached as columns)
            return file_results
        except Exception:
            return []

    def _fetch_detail_rows(self, detail_url: str) -> List[SearchResult]:
        """Fetch the detail page for a selected row and return the parsed file rows."""

        detail_url = str(detail_url or "").strip()
        if not detail_url:
            return []
        try:
            with HTTPClient(timeout=9.0) as client:
                resp = client.get(detail_url)
                doc = lxml_html.fromstring(resp.content)
        except Exception as exc:
            debug(f"[vimm] detail fetch failed: {exc}")
            return []
        return self._parse_detail_doc(doc, base_url=detail_url)

    def _download_from_payload(self, payload: Dict[str, Any], output_dir: Path) -> Optional[Path]:
        """Download using the metadata/form data stored in a SearchResult payload."""

        try:
            d = payload or {}
            fm = d.get("full_metadata") or {}
            media_id = fm.get("mediaId") or fm.get("media_id")
            base_action = fm.get("dl_action") or d.get("path") or ""
            download_url = fm.get("download_url")
            params = dict(fm.get("form_params") or {})
            if media_id:
                params.setdefault("mediaId", media_id)
            target = download_url or base_action
            if not target:
                return None
            if target.startswith("//"):
                target = "https:" + target

            # Avoid downloading HTML detail pages directly; let detail parsing handle them.
            low_target = target.lower()
            if ("vimm.net/vault" in low_target or "?p=list" in low_target) and not download_url and not media_id and not params:
                return None

            referer = fm.get("referer") or d.get("referer") or d.get("detail_url")
            headers: Dict[str, str] = {}

            if not referer:
                try:
                    from SYS.pipeline import get_last_result_items

                    items = get_last_result_items() or []
                    try:
                        parsed_target = urlparse(target)
                        target_qs = parse_qs(parsed_target.query)
                        target_media = None
                        if isinstance(target_qs, dict):
                            target_media = (target_qs.get("mediaId") or target_qs.get("mediaid") or [None])[0]
                            if target_media is not None:
                                target_media = str(target_media)
                    except Exception:
                        target_media = None

                    found = None
                    for it in items:
                        try:
                            it_d = it if isinstance(it, dict) else (it.to_dict() if hasattr(it, "to_dict") else {})
                            fm2 = (it_d.get("full_metadata") or {}) if isinstance(it_d, dict) else {}
                            dl_cand = (fm2.get("download_url") or fm2.get("dl_action") or it_d.get("path"))
                            if target_media:
                                m2 = None
                                if isinstance(fm2, dict):
                                    m2 = str(fm2.get("mediaId") or fm2.get("media_id") or "")
                                if m2 and m2 == target_media:
                                    found = it_d
                                    break
                            if dl_cand and str(dl_cand).strip() and (str(dl_cand).strip() == str(target).strip() or str(dl_cand) in str(target) or str(target) in str(dl_cand)):
                                found = it_d
                                break
                        except Exception:
                            continue

                    if found:
                        referer = (found.get("full_metadata") or {}).get("referer") or found.get("detail_url") or found.get("path")
                except Exception:
                    referer = referer

            if referer:
                headers["Referer"] = str(referer)
            headers_arg = headers or None

            out_dir = Path(output_dir or Path("."))
            out_dir.mkdir(parents=True, exist_ok=True)
            filename_hint = str(d.get("title") or f"vimm_{media_id or 'download'}")

            with HTTPClient(timeout=60.0) as client:
                try:
                    if download_url:
                        resp = client.get(target, headers=headers_arg)
                    elif params:
                        resp = client.get(target, params=params, headers=headers_arg)
                    else:
                        resp = client.get(target, headers=headers_arg)
                except Exception as exc_get:
                    try:
                        detail_url = referer or target
                        p = self._playwright_fetch(detail_url, out_dir, selector="form#dl_form button[type=submit]", timeout_sec=60)
                        if p:
                            debug(f"[vimm] downloaded via Playwright after get() error: {p}")
                            return p
                    except Exception as e:
                        debug(f"[vimm] Playwright download failed after get() error: {e}")

                    debug(f"[vimm] HTTP GET failed (network): {exc_get}")
                    return None

                try:
                    resp.raise_for_status()
                except Exception as exc:
                    try:
                        detail_url = referer or target
                        p = self._playwright_fetch(detail_url, out_dir, selector="form#dl_form button[type=submit]", timeout_sec=60)
                        if p:
                            debug(f"[vimm] downloaded via Playwright after HTTP error: {p}")
                            return p
                    except Exception as e:
                        debug(f"[vimm] Playwright download failed after HTTP error: {e}")

                    debug(f"[vimm] HTTP GET failed: {exc}")
                    return None

                content = getattr(resp, "content", b"") or b""
                cd = getattr(resp, "headers", {}).get("content-disposition", "") if hasattr(resp, "headers") else ""
                m = re.search(r'filename\*?=(?:"([^"]*)"|([^;\s]*))', cd)
                if m:
                    fname = m.group(1) or m.group(2)
                else:
                    fname = filename_hint

                out_path = out_dir / str(fname)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(content)
                return out_path
        except Exception as exc:
            debug(f"[vimm] download failed: {exc}")
            return None

    def _playwright_fetch(self, detail_url: str, out_dir: Path, selector: str = "form#dl_form button[type=submit]", timeout_sec: int = 90) -> Optional[Path]:
        """Attempt a browser-driven download using the shared Playwright tool.

        Playwright is a required runtime dependency for this operation; import
        failures will surface at module import time rather than being silently
        swallowed by per-call guards.
        """

        # Prefer headful-first attempts for Vimm to mirror real browser behaviour
        cfg = {}
        try:
            from SYS.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}

        tool = PlaywrightTool(cfg)
        result = tool.download_file(
            detail_url,
            selector=selector,
            out_dir=out_dir,
            timeout_sec=timeout_sec,
            headless_first=False,
            debug_mode=False,
        )
        if result.ok and result.path:
            return result.path
        debug(f"[vimm] playwright helper failed: {result.error}")
        return None

    def download(self, result: Any, output_dir: Path, progress_callback: Optional[Any] = None) -> Optional[Path]:
        """Download an item identified on a Vimm detail page."""

        payload = result.to_dict() if hasattr(result, "to_dict") else (result if isinstance(result, dict) else {})
        downloaded = self._download_from_payload(payload, output_dir)
        if downloaded:
            return downloaded

        detail_url = str(payload.get("path") or "").strip()
        if not detail_url:
            return None

        for row in self._fetch_detail_rows(detail_url):
            detail_payload = row.to_dict() if hasattr(row, "to_dict") else (row if isinstance(row, dict) else {})
            downloaded = self._download_from_payload(detail_payload, output_dir)
            if downloaded:
                return downloaded

        return None
