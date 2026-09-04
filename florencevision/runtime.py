from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from SYS.config import get_nested_config_value as _get_nested
from SYS.logger import debug


def _truncate_debug_text(text: str, max_chars: int = 12000) -> str:
    s = str(text or "")
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n... (truncated; {len(s)} chars total)"


def _debug_repr(value: Any, max_chars: int = 12000) -> str:
    """Pretty-ish repr for debug without risking huge output."""
    try:
        import pprint

        s = pprint.pformat(value, width=120, compact=True)
    except Exception:
        try:
            s = repr(value)
        except Exception:
            s = f"<{type(value).__name__}>"
    return _truncate_debug_text(s, max_chars=max_chars)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _clean_tag_value(text: str) -> str:
    # Keep tags conservative: strip confidence suffixes, lowercase, trim,
    # replace whitespace runs with underscore.
    s0 = str(text or "").strip()
    if not s0:
        return ""

    # Strip common confidence suffix patterns, e.g.:
    #   "car (0.98)", "car:0.98", "car 0.98", "car (98%)"
    # Keep this conservative to avoid mangling labels like "iphone 14".
    import re

    s0 = re.sub(r"\s*[\(\[]\s*\d+(?:\.\d+)?\s*%?\s*[\)\]]\s*$", "", s0)
    s0 = re.sub(r"\s*[:=]\s*\d+(?:\.\d+)?\s*%?\s*$", "", s0)
    s0 = re.sub(r"\s+\d+\.\d+\s*%?\s*$", "", s0)

    s = s0.strip().lower()
    if not s:
        return ""
    # remove leading/trailing punctuation
    s = s.strip("\"'`.,;:!?()[]{}<>|\\/")
    # Common list markers / bullets that show up in OCR/tag outputs.
    s = s.strip("-–—•·")
    s = "_".join([p for p in s.replace("\t", " ").replace("\n", " ").split() if p])
    # avoid empty or purely underscores
    s = s.strip("_")
    if not s:
        return ""
    # Drop values that have no alphanumerics (e.g. "-" / "___").
    if not any(ch.isalnum() for ch in s):
        return ""
    return s


def _normalize_task_prompt(task: str) -> str:
    """Normalize human-friendly task names to Florence prompt tokens.

        Accepts either Florence tokens (e.g. "<OD>" / "<CAPTION>" or "<|...|>")
        or friendly aliases. Default and most aliases map to Florence's supported
        detailed-caption + grounding combo to yield both labels and a caption:
            - "tag" / "tags" -> "<|detailed_caption|><|grounding|>"
            - "detection" / "detect" / "od" / "grounding" -> "<|detailed_caption|><|grounding|>"
            - "caption" -> "<|detailed_caption|>"
            - "ocr" -> "<|ocr|>"
    """
    raw = str(task or "").strip()
    if not raw:
        return "<|detailed_caption|><|grounding|>"

    # If user already provided a Florence token, keep it unless it's a legacy OD token
    # (then expand to the detailed_caption+grounding combo).
    if raw.startswith("<") and raw.endswith(">"):
        od_like = raw.strip().lower()
        if od_like in {"<od>", "<|od|>", "<|object_detection|>", "<|object-detection|>"}:
            return "<|detailed_caption|><|grounding|>"
        return raw if raw.startswith("<|") else raw.upper()

    key = raw.strip().lower().replace("_", "-")
    key = " ".join(key.split())

    if key in {"tag", "tags"}:
        return "<|detailed_caption|><|grounding|>"
    if key in {
        "detection",
        "detect",
        "object-detection",
        "object detection",
        "object_detection",
        "od",
    }:
        return "<|detailed_caption|><|grounding|>"
    if key in {"grounding", "bbox", "boxes", "box"}:
        return "<|detailed_caption|><|grounding|>"
    if key in {"caption", "cap", "describe", "description"}:
        return "<|detailed_caption|>"
    if key in {"more-detailed-caption", "detailed-caption", "more detailed caption", "detailed_caption"}:
        return "<|detailed_caption|>"
    if key in {"ocr", "text", "read", "extract-text", "extract text"}:
        return "<|ocr|>"

    # Unknown strings: pass through (remote-code models sometimes accept custom prompts).
    return raw


def _is_caption_task(prompt: str) -> bool:
    p = str(prompt or "").upper()
    return "CAPTION" in p


def _is_tag_task(prompt: str) -> bool:
    p = str(prompt or "").strip().lower()
    return "tag" in p or "<|tag|>" in p


def _is_ocr_task(prompt: str) -> bool:
    p = str(prompt or "").strip().lower()
    return "ocr" in p or "<|ocr|>" in p


def _strip_florence_tokens(text: str) -> str:
    """Remove Florence prompt/special tokens from generated text."""
    import re

    s = str(text or "")
    if not s:
        return ""

    # Remove <|...|> style tokens and legacy <OD>/<CAPTION>/<OCR> style tokens.
    s = re.sub(r"<\|[^>]+?\|>", " ", s)
    s = re.sub(r"<[^>]+?>", " ", s)

    # Remove common leftover special tokens.
    s = s.replace("</s>", " ").replace("<s>", " ")
    return " ".join(s.split())


def _split_text_to_labels(text: str) -> List[str]:
    """Split a generated text blob into candidate labels."""
    raw = _strip_florence_tokens(text)
    if not raw:
        return []

    out: List[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in line.replace(";", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _clean_caption_text(text: str) -> str:
    """Strip Florence tokens and collapse whitespace for caption text."""
    cleaned = _strip_florence_tokens(text)
    return " ".join(str(cleaned or "").split())


def _collect_candidate_strings(value: Any) -> List[str]:
    """Best-effort extraction of tag-like strings from nested Florence outputs."""
    out: List[str] = []

    if value is None:
        return out
    if isinstance(value, str):
        s = value.strip()
        if s:
            out.append(s)
        return out

    if isinstance(value, dict):
        # Prefer common semantic keys first.
        for key in (
            "labels",
            "label",
            "text",
            "texts",
            "words",
            "word",
            "caption",
            "captions",
            "phrase",
            "phrases",
            "name",
            "names",
        ):
            if key in value:
                out.extend(_collect_candidate_strings(value.get(key)))
        # Then fall back to scanning other values (numbers/bboxes are ignored).
        for v in value.values():
            out.extend(_collect_candidate_strings(v))
        return out

    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_collect_candidate_strings(item))
        return out

    return out


def _collect_captions(value: Any, key_hint: str = "") -> List[str]:
    """Extract caption-like strings from nested structures by key name."""
    out: List[str] = []

    def _norm(val: Any) -> Optional[str]:
        if val is None:
            return None
        try:
            s = str(val).strip()
        except Exception:
            return None
        return s if s else None

    try:
        hint_has_caption = "caption" in str(key_hint or "").lower()
    except Exception:
        hint_has_caption = False

    if isinstance(value, str):
        if hint_has_caption:
            s = _norm(value)
            if s:
                cleaned = _clean_caption_text(s)
                if cleaned:
                    out.append(cleaned)
                else:
                    out.append(s)
        return out

    if isinstance(value, dict):
        for k, v in value.items():
            out.extend(_collect_captions(v, key_hint=str(k)))
        return out

    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_collect_captions(item, key_hint=key_hint))
        return out

    return out


@dataclass(slots=True)
class FlorenceVisionDefaults:
    enabled: bool = False
    strict: bool = False
    model: str = "microsoft/Florence-2-large"
    device: str = "cpu"  # "cpu" | "cuda" | "mps"
    dtype: Optional[str] = None  # e.g. "float16" | "bfloat16" | None
    max_tags: int = 12
    namespace: str = "florence"
    task: str = "tag"  # Friendly aliases: tag/detection/caption/ocr (or raw Florence prompt tokens)


class FlorenceVisionTool:
    """Microsoft Florence vision model wrapper.

    Designed to be dependency-light at import time; heavy deps are imported lazily.

        Config:
            [plugin=florencevision]
      enabled=true
      strict=false
      model="microsoft/Florence-2-large"
      device="cpu"
      dtype="float16"  # optional
      max_tags=12
    task="<|tag|>"  # or <|od|>, <|caption|>, <|ocr|>

    Notes:
      Florence-2 typically requires `trust_remote_code=True` when loading via Transformers.
    """

    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config: Dict[str, Any] = dict(config or {})
        self.defaults = self._load_defaults()
        self._model = None
        self._processor = None
        self._last_caption: Optional[str] = None

    def _load_defaults(self) -> FlorenceVisionDefaults:
        cfg = self._config
        plugin_block = _get_nested(cfg, "plugin", "florencevision")
        if not isinstance(plugin_block, dict):
            plugin_block = {}

        base = FlorenceVisionDefaults()

        defaults = FlorenceVisionDefaults(
            enabled=_as_bool(plugin_block.get("enabled"), False),
            strict=_as_bool(plugin_block.get("strict"), False),
            model=str(plugin_block.get("model") or base.model),
            device=str(plugin_block.get("device") or base.device),
            dtype=(str(plugin_block.get("dtype")).strip() if plugin_block.get("dtype") else None),
            max_tags=_as_int(plugin_block.get("max_tags"), base.max_tags),
            namespace=str(plugin_block.get("namespace") or base.namespace),
            task=str(plugin_block.get("task") or base.task),
        )
        return defaults

    def enabled(self) -> bool:
        return bool(self.defaults.enabled)

    def applicable_path(self, media_path: Path) -> bool:
        try:
            return media_path.suffix.lower() in self.IMAGE_EXTS
        except Exception:
            return False

    def _ensure_loaded(self) -> None:
        if self._model is not None and self._processor is not None:
            return

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor
        except Exception as exc:
            raise RuntimeError(
                "FlorenceVision requires optional dependencies. Install at least: torch, transformers, pillow. "
                "(Florence-2 typically also needs trust_remote_code=True)."
            ) from exc

        model_id = self.defaults.model
        device = self.defaults.device

        debug(f"[florencevision] Loading processor/model: model={model_id} device={device} dtype={self.defaults.dtype}")

        dtype = None
        if self.defaults.dtype:
            dt = self.defaults.dtype.strip().lower()
            dtype = {
                "float16": torch.float16,
                "fp16": torch.float16,
                "bfloat16": torch.bfloat16,
                "bf16": torch.bfloat16,
                "float32": torch.float32,
                "fp32": torch.float32,
            }.get(dt)

        # FlorenceVision often runs on CPU for many users; float16/bfloat16 on CPU
        # is fragile (and can produce dtype mismatches like float vs Half bias).
        # If the configured device is CPU, force float32 unless explicitly set to float32.
        if str(device).strip().lower() == "cpu" and dtype in {torch.float16, torch.bfloat16}:
            debug(
                f"[florencevision] Overriding dtype to float32 on CPU (was {self.defaults.dtype})"
            )
            dtype = torch.float32

        # Florence-2 usually requires trust_remote_code.
        self._processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        base_kwargs: Dict[str, Any] = {
            "trust_remote_code": True,
            "torch_dtype": dtype,
        }

        # Transformers attention backends have been a moving target. Some Florence-2
        # remote-code builds trigger AttributeError on SDPA capability checks.
        # Prefer eager attention when supported, otherwise fall back.
        try:
            self._model = AutoModelForCausalLM.from_pretrained(
                model_id,
                attn_implementation="eager",
                **base_kwargs,
            )
        except TypeError:
            # Older Transformers: no attn_implementation kwarg.
            self._model = AutoModelForCausalLM.from_pretrained(model_id, **base_kwargs)
        except AttributeError as exc:
            if "_supports_sdpa" in str(exc):
                try:
                    self._model = AutoModelForCausalLM.from_pretrained(
                        model_id,
                        attn_implementation="eager",
                        **base_kwargs,
                    )
                except TypeError:
                    self._model = AutoModelForCausalLM.from_pretrained(model_id, **base_kwargs)
            else:
                raise

        # Defensive compatibility patch: some Florence-2 implementations do not
        # declare SDPA support flags but newer Transformers paths may probe them.
        try:
            if self._model is not None and not hasattr(self._model, "_supports_sdpa"):
                setattr(self._model, "_supports_sdpa", False)
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to set model compatibility flag _supports_sdpa")

        try:
            self._model.to(device)  # type: ignore[union-attr]
        except Exception:
            # Fallback to cpu
            self._model.to("cpu")  # type: ignore[union-attr]

        try:
            self._model.eval()  # type: ignore[union-attr]
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to set Florence model to eval mode")

        try:
            md = getattr(self._model, "device", None)
            dt = None
            try:
                dt = next(self._model.parameters()).dtype  # type: ignore[union-attr]
            except Exception:
                dt = None
            debug(f"[florencevision] Model loaded: device={md} param_dtype={dt}")
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to inspect Florence model device/dtype")

    def tags_for_image(self, media_path: Path) -> List[str]:
        """Return Florence-derived tags for an image.

        Uses the configured Florence task (default: tag) and turns model output into tags.
        """
        self._ensure_loaded()
        self._last_caption = None

        try:
            from PIL import Image
        except Exception as exc:
            raise RuntimeError("FlorenceVision requires pillow (PIL).") from exc

        if self._processor is None or self._model is None:
            return []

        prompt = _normalize_task_prompt(str(self.defaults.task or "tag"))
        try:
            debug(f"[florencevision] Task prompt: {prompt}")
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to emit debug Task prompt for FlorenceVision")

        max_tags = max(0, int(self.defaults.max_tags or 0))

        try:
            debug(
                f"[florencevision] Opening image: path={media_path} exists={media_path.exists()} size_bytes={(media_path.stat().st_size if media_path.exists() else 'n/a')}"
            )
        except Exception:
            debug(f"[florencevision] Opening image: path={media_path}")

        image = Image.open(str(media_path)).convert("RGB")
        try:
            debug(f"[florencevision] Image loaded: mode={image.mode} size={image.width}x{image.height}")
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to emit debug for image load")

        processor = self._processor
        model = self._model

        # Inspect forward signature once; reused across cascaded runs.
        forward_params: set[str] = set()
        try:
            import inspect

            forward_params = set(inspect.signature(getattr(model, "forward")).parameters.keys())
        except Exception:
            forward_params = set()

        def _run_prompt(task_prompt: str) -> Tuple[str, Any, Any]:
            """Run a single Florence prompt and return (generated_text, parsed, seq)."""
            try:
                debug(f"[florencevision] running prompt: {task_prompt}")
            except Exception:
                pass

            try:
                # Florence expects special task tokens like <|tag|>, <|od|>, <|caption|>, <|ocr|>.
                inputs = processor(text=task_prompt, images=image, return_tensors="pt")
                try:
                    import torch

                    keys = []
                    try:
                        keys = list(dict(inputs).keys())
                    except Exception:
                        try:
                            keys = list(getattr(inputs, "keys")())
                        except Exception:
                            keys = []

                    debug(f"[florencevision] Processor output keys: {keys}")

                    for k in keys:
                        try:
                            v = dict(inputs).get(k)
                        except Exception:
                            try:
                                v = inputs.get(k)  # type: ignore[union-attr]
                            except Exception:
                                v = None

                        if v is None:
                            debug(f"[florencevision]   {k}: None")
                            continue
                        if hasattr(v, "shape"):
                            try:
                                debug(
                                    f"[florencevision]   {k}: tensor shape={tuple(v.shape)} dtype={getattr(v, 'dtype', None)}"
                                )
                                continue
                            except Exception:
                                from SYS.logger import logger
                                logger.exception("Failed to debug tensor shape for processor key '%s'", k)
                        if isinstance(v, (list, tuple)):
                            has_none = any(x is None for x in v)
                            debug(f"[florencevision]   {k}: {type(v).__name__} len={len(v)} has_none={has_none}")
                            continue
                        debug(f"[florencevision]   {k}: type={type(v).__name__}")
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed while inspecting processor output keys")

                try:
                    inputs = inputs.to(model.device)  # type: ignore[attr-defined]
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to move processor inputs to device %s", getattr(model, 'device', None))

                # Align floating-point input tensors with the model's parameter dtype.
                try:
                    import torch

                    try:
                        model_dtype = next(model.parameters()).dtype  # type: ignore[union-attr]
                    except Exception:
                        model_dtype = None

                    if model_dtype is not None:
                        for k, v in list(inputs.items()):
                            try:
                                if hasattr(v, "dtype") and torch.is_floating_point(v):
                                    inputs[k] = v.to(dtype=model_dtype)
                            except Exception:
                                continue
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to inspect/align model dtype for Florence inputs")

                try:
                    gen_inputs_all = {k: v for k, v in dict(inputs).items() if v is not None}
                except Exception:
                    gen_inputs_all = inputs  # type: ignore[assignment]

                gen_inputs: Dict[str, Any] = {}
                if isinstance(gen_inputs_all, dict):
                    input_ids = gen_inputs_all.get("input_ids")
                    pixel_values = gen_inputs_all.get("pixel_values")
                    attention_mask = gen_inputs_all.get("attention_mask")

                    if input_ids is not None:
                        gen_inputs["input_ids"] = input_ids
                    if pixel_values is not None:
                        gen_inputs["pixel_values"] = pixel_values

                    try:
                        if (
                            attention_mask is not None
                            and hasattr(attention_mask, "shape")
                            and hasattr(input_ids, "shape")
                            and tuple(attention_mask.shape) == tuple(input_ids.shape)
                        ):
                            gen_inputs["attention_mask"] = attention_mask
                    except Exception:
                        from SYS.logger import logger
                        logger.exception("Failed to reconcile attention mask shape with input_ids for Florence processor")

                try:
                    debug(
                        "[florencevision] model forward supports: "
                        f"pixel_mask={'pixel_mask' in forward_params} "
                        f"image_attention_mask={'image_attention_mask' in forward_params} "
                        f"pixel_attention_mask={'pixel_attention_mask' in forward_params}"
                    )
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to debug model forward supports")

                try:
                    gen_inputs.setdefault("use_cache", False)
                    gen_inputs.setdefault("num_beams", 1)
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to set default gen_inputs values")

                try:
                    debug(f"[florencevision] generate kwargs: {sorted(list(gen_inputs.keys()))}")
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to debug generate kwargs")

                pv = gen_inputs.get("pixel_values")
                if pv is None:
                    raise RuntimeError(
                        "FlorenceVision processor did not produce 'pixel_values'. "
                        "This usually indicates an image preprocessing issue."
                    )

                try:
                    import torch

                    cm = torch.inference_mode
                except Exception:
                    cm = None

                def _do_generate(kwargs: Dict[str, Any]) -> Any:
                    if cm is not None:
                        with cm():
                            return model.generate(**kwargs, max_new_tokens=1024)
                    return model.generate(**kwargs, max_new_tokens=1024)

                try:
                    generated_ids = _do_generate(gen_inputs)
                except AttributeError as exc:
                    msg = str(exc)
                    if "_supports_sdpa" in msg:
                        try:
                            if not hasattr(model, "_supports_sdpa"):
                                setattr(model, "_supports_sdpa", False)
                        except Exception:
                            from SYS.logger import logger
                            logger.exception("Failed to patch model _supports_sdpa flag in retry handler")
                        generated_ids = _do_generate(gen_inputs)
                    elif "NoneType" in msg and "shape" in msg:
                        retry_inputs = dict(gen_inputs)

                        try:
                            if (
                                "attention_mask" not in retry_inputs
                                and isinstance(gen_inputs_all, dict)
                                and gen_inputs_all.get("attention_mask") is not None
                            ):
                                am = gen_inputs_all.get("attention_mask")
                                ii = retry_inputs.get("input_ids")
                                if (
                                    am is not None
                                    and ii is not None
                                    and hasattr(am, "shape")
                                    and hasattr(ii, "shape")
                                    and tuple(am.shape) == tuple(ii.shape)
                                ):
                                    retry_inputs["attention_mask"] = am
                        except Exception:
                            from SYS.logger import logger
                            logger.exception("Failed while filling retry_inputs attention_mask in AttributeError handler")

                        try:
                            import torch

                            pv2 = retry_inputs.get("pixel_values")
                            if pv2 is not None and hasattr(pv2, "shape") and len(pv2.shape) == 4:
                                b, _c, h, w = tuple(pv2.shape)
                                mask = torch.ones((b, h, w), dtype=torch.long, device=pv2.device)
                                if "pixel_mask" in forward_params and "pixel_mask" not in retry_inputs:
                                    retry_inputs["pixel_mask"] = mask
                                elif "image_attention_mask" in forward_params and "image_attention_mask" not in retry_inputs:
                                    retry_inputs["image_attention_mask"] = mask
                                elif "pixel_attention_mask" in forward_params and "pixel_attention_mask" not in retry_inputs:
                                    retry_inputs["pixel_attention_mask"] = mask
                        except Exception:
                            from SYS.logger import logger
                            logger.exception("Failed to build mask or adjust retry_inputs in AttributeError handler")

                        try:
                            debug(
                                f"[florencevision] generate retry kwargs: {sorted(list(retry_inputs.keys()))}"
                            )
                        except Exception:
                            from SYS.logger import logger
                            logger.exception("Failed to debug generate retry kwargs")

                        generated_ids = _do_generate(retry_inputs)
                    else:
                        raise

                try:
                    debug(f"[florencevision] generated_ids type={type(generated_ids).__name__}")
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to debug generated_ids type")

                seq = getattr(generated_ids, "sequences", generated_ids)
                generated_text = processor.batch_decode(seq, skip_special_tokens=False)[0]
            except Exception as exc:
                try:
                    import traceback

                    debug(f"[florencevision] prompt run failed: {type(exc).__name__}: {exc}")
                    debug("[florencevision] traceback:\n" + traceback.format_exc())
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to emit debug for prompt run failure: %s", exc)
                raise

            parsed = None
            try:
                parsed = processor.post_process_generation(
                    generated_text,
                    task=task_prompt,
                    image_size=(image.width, image.height),
                )
            except Exception:
                parsed = None

            try:
                generated_text_no_special = None
                try:
                    generated_text_no_special = processor.batch_decode(seq, skip_special_tokens=True)[0]
                except Exception:
                    generated_text_no_special = None

                debug("[florencevision] ===== RAW GENERATED (skip_special_tokens=False) =====")
                debug(_truncate_debug_text(str(generated_text or "")))
                if generated_text_no_special is not None:
                    debug("[florencevision] ===== RAW GENERATED (skip_special_tokens=True) =====")
                    debug(_truncate_debug_text(str(generated_text_no_special or "")))

                if parsed is None:
                    debug("[florencevision] post_process_generation: None")
                elif isinstance(parsed, dict):
                    try:
                        keys = list(parsed.keys())
                    except Exception:
                        keys = []
                    debug(f"[florencevision] post_process_generation: dict keys={keys}")
                    try:
                        if task_prompt in parsed:
                            debug(f"[florencevision] post_process[{task_prompt!r}] type={type(parsed.get(task_prompt)).__name__}")
                            debug("[florencevision] post_process[prompt] repr:\n" + _debug_repr(parsed.get(task_prompt)))
                        elif len(parsed) == 1:
                            only_key = next(iter(parsed.keys()))
                            debug(f"[florencevision] post_process single key {only_key!r} type={type(parsed.get(only_key)).__name__}")
                            debug("[florencevision] post_process[single] repr:\n" + _debug_repr(parsed.get(only_key)))
                        else:
                            for k in list(parsed.keys())[:5]:
                                debug(f"[florencevision] post_process[{k!r}] type={type(parsed.get(k)).__name__}")
                                debug("[florencevision] post_process[key] repr:\n" + _debug_repr(parsed.get(k)))
                    except Exception:
                        from SYS.logger import logger
                        logger.exception("Failed while debugging parsed post_process output for prompt %s", task_prompt)
                else:
                    debug(f"[florencevision] post_process_generation: type={type(parsed).__name__}")
                    debug("[florencevision] post_process repr:\n" + _debug_repr(parsed))
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to post-process generated output for prompt %s", task_prompt)

            return generated_text, parsed, seq

        def _extract_labels_and_captions(task_prompt: str, generated_text: str, parsed: Any) -> Tuple[List[str], List[str], List[str], List[Tuple[str, str, str]]]:
            labels: List[str] = []
            caption_candidates: List[str] = []

            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    key_lower = str(k).lower()
                    if "caption" in key_lower:
                        caption_candidates.extend(_collect_captions(v, key_hint=str(k)))
                        continue
                    labels.extend(_collect_candidate_strings(v))
            elif parsed is not None:
                if isinstance(parsed, str) and parsed.strip() and _is_caption_task(task_prompt):
                    caption_candidates.append(parsed.strip())
                else:
                    labels.extend(_collect_candidate_strings(parsed))

            if not labels:
                raw = str(generated_text or "").strip()
                if raw:
                    labels.extend(_split_text_to_labels(raw))

            try:
                debug(f"[florencevision] candidate label strings ({len(labels)}): {labels!r}")
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to emit candidate label strings debug")

            out: List[str] = []
            seen: set[str] = set()
            dropped: List[Tuple[str, str, str]] = []
            for lab in labels:
                v = _clean_tag_value(lab)
                if not v:
                    dropped.append((str(lab), "", "cleaned_empty"))
                    continue

                if v in {
                    "od",
                    "caption",
                    "more_detailed_caption",
                    "more-detailed-caption",
                    "ocr",
                    "tag",
                    "grounding",
                    "object_detection",
                    "detailed_caption",
                    "caption_to_phrase_grounding",
                }:
                    dropped.append((str(lab), v, "filtered_task_token"))
                    continue

                if v.startswith("florence:"):
                    v = v.split(":", 1)[1].strip("_")
                    if not v:
                        dropped.append((str(lab), "", "stripped_namespace_empty"))
                        continue

                key = v.lower()
                if key in seen:
                    dropped.append((str(lab), v, "duplicate"))
                    continue
                seen.add(key)
                out.append(v)
                if max_tags and len(out) >= max_tags:
                    break

            try:
                debug(f"[florencevision] cleaned tags ({len(out)}): {out!r}")
                if dropped:
                    debug(f"[florencevision] dropped ({len(dropped)}):")
                    for raw_lab, cleaned, reason in dropped:
                        debug(f"[florencevision]   drop reason={reason} raw={raw_lab!r} cleaned={cleaned!r}")
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to emit cleaned/dropped tags debug info")

            return labels, caption_candidates, out, dropped

        def _best_caption(candidates: Sequence[str]) -> Optional[str]:
            cleaned: List[str] = []
            raw: List[str] = []
            for c in candidates:
                try:
                    s = str(c).strip()
                except Exception:
                    continue
                if not s:
                    continue
                raw.append(s)
                cc = _clean_caption_text(s)
                if cc:
                    cleaned.append(cc)

            if cleaned:
                try:
                    return max(cleaned, key=lambda s: len(str(s)), default=None)
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to choose best caption from cleaned candidates")
            try:
                return max(raw, key=lambda s: len(str(s)), default=None)
            except Exception:
                return None
            try:
                return max(raw, key=lambda s: len(str(s)), default=None)
            except Exception:
                return None

        def _grounding_candidates_from_caption(caption_text: Optional[str], fallback_tags: Sequence[str]) -> List[str]:
            import re

            words: List[str] = []
            if caption_text:
                cap_clean = _clean_caption_text(caption_text)
                if cap_clean:
                    words.extend(re.split(r"[^A-Za-z0-9_\-]+", cap_clean))

            # Add any fallback tags (e.g., cleaned caption labels) to seed grounding.
            for tag in fallback_tags or []:
                cleaned_tag = _clean_tag_value(tag)
                if cleaned_tag:
                    words.append(cleaned_tag)

            seen: set[str] = set()
            out: List[str] = []
            for w in words:
                if not w:
                    continue
                w_clean = re.sub(r"[^A-Za-z0-9_\-]+", "", w).strip("._-")
                if len(w_clean) < 3:
                    continue
                if not any(ch.isalpha() for ch in w_clean):
                    continue
                if re.match(r"loc[_-]?\d", w_clean, re.IGNORECASE):
                    continue
                if w_clean.lower() in {"detailed", "caption", "grounding", "poly", "task"}:
                    continue
                key = w_clean.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(w_clean)
                if len(out) >= max(max_tags * 2, 10):
                    break
            return out

        is_combo_prompt = "<|detailed_caption|>" in prompt and "<|grounding|>" in prompt

        final_tags: List[str] = []
        caption_text: Optional[str] = None

        if is_combo_prompt:
            # Cascaded flow: caption first, then grounding seeded by caption terms.
            cap_text, cap_parsed, _cap_seq = _run_prompt("<|detailed_caption|>")
            cap_labels, cap_captions, cap_cleaned, _cap_dropped = _extract_labels_and_captions("<|detailed_caption|>", cap_text, cap_parsed)

            best_cap = _best_caption(cap_captions) or _best_caption([_strip_florence_tokens(cap_text)])
            if best_cap:
                cap_cleaned_text = _clean_caption_text(best_cap)
                if cap_cleaned_text:
                    caption_text = cap_cleaned_text

            candidates = _grounding_candidates_from_caption(caption_text, cap_cleaned or cap_labels)
            grounding_prompt = "<|grounding|>" if not candidates else "<|grounding|> Find and label: " + ", ".join(candidates)
            try:
                debug(f"[florencevision] grounding prompt: {grounding_prompt}")
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to emit grounding prompt debug")

            grd_text, grd_parsed, _grd_seq = _run_prompt(grounding_prompt)
            _grd_labels, grd_captions, grd_cleaned, _grd_dropped = _extract_labels_and_captions(grounding_prompt, grd_text, grd_parsed)

            final_tags = grd_cleaned or cap_cleaned
            if not caption_text:
                caption_text = _best_caption(grd_captions)

            # If grounding still produced nothing useful, fall back to raw split of grounding text.
            if not final_tags:
                fallback_labels = _split_text_to_labels(grd_text)
                final_tags = [_clean_tag_value(v) for v in fallback_labels if _clean_tag_value(v)]
                if max_tags:
                    final_tags = final_tags[:max_tags]
        else:
            gen_text, parsed, _seq = _run_prompt(prompt)
            _labels, captions, cleaned_tags, _dropped = _extract_labels_and_captions(prompt, gen_text, parsed)
            final_tags = cleaned_tags
            caption_text = _best_caption(captions)

            # Fallback: if combo-like prompt yields only task tokens, retry with caption-only once.
            try:
                is_combo = "<|detailed_caption|>" in prompt and "<|grounding|>" in prompt
                only_task_tokens = not final_tags or all(t in {"object_detection", "grounding", "tag"} for t in final_tags)
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to compute is_combo/only_task_tokens for prompt '%s'", prompt)
                is_combo = False
                only_task_tokens = False

            if is_combo and only_task_tokens and not getattr(self, "_od_tag_retrying", False):
                try:
                    self._od_tag_retrying = True
                    debug("[florencevision] caption+grounding produced no labels; retrying with <|detailed_caption|> only")
                    original_task = self.defaults.task
                    try:
                        self.defaults.task = "<|detailed_caption|>"
                    except Exception:
                        from SYS.logger import logger
                        logger.exception("Failed to set self.defaults.task to '<|detailed_caption|>' during od retry")
                    final_tags = self.tags_for_image(media_path)
                finally:
                    try:
                        self.defaults.task = original_task
                    except Exception:
                        from SYS.logger import logger
                        logger.exception("Failed to restore self.defaults.task after od retry")
                    self._od_tag_retrying = False

        self._last_caption = caption_text if caption_text else None
        return final_tags

    @property
    def last_caption(self) -> Optional[str]:
        return self._last_caption

    def tags_for_file(self, media_path: Path) -> List[str]:
        if not self.enabled():
            return []
        if not self.applicable_path(media_path):
            return []
        return self.tags_for_image(media_path)


def config_schema() -> List[Dict[str, Any]]:
    defaults = FlorenceVisionDefaults()
    return [
        {
            "key": "enabled",
            "label": "Enable FlorenceVision",
            "type": "boolean",
            "default": str(defaults.enabled).lower(),
            "choices": ["true", "false"],
        },
        {
            "key": "strict",
            "label": "Strict mode",
            "type": "boolean",
            "default": str(defaults.strict).lower(),
            "choices": ["true", "false"],
        },
        {
            "key": "model",
            "label": "Model",
            "default": defaults.model,
        },
        {
            "key": "device",
            "label": "Device",
            "default": defaults.device,
            "choices": ["cpu", "cuda", "mps"],
        },
        {
            "key": "dtype",
            "label": "DType",
            "default": defaults.dtype or "",
            "choices": ["", "float16", "bfloat16", "float32"],
        },
        {
            "key": "max_tags",
            "label": "Max tags",
            "type": "integer",
            "default": defaults.max_tags,
        },
        {
            "key": "namespace",
            "label": "Namespace",
            "default": defaults.namespace,
        },
        {
            "key": "task",
            "label": "Task",
            "default": defaults.task,
            "choices": ["tag", "detection", "caption", "ocr"],
        },
    ]


__all__ = ["FlorenceVisionTool", "FlorenceVisionDefaults", "config_schema"]
