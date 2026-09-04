from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict

def _repo_root() -> Path:
    package_dir = Path(__file__).resolve().parent
    if package_dir.name.lower() == "mpv" and package_dir.parent.name.lower() == "plugins":
        return package_dir.parent.parent
    return package_dir.parent


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        payload: Dict[str, Any] = {
            "success": False,
            "stdout": "",
            "stderr": "",
            "error": "Missing url",
            "table": None,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 2

    url = str(args[0] or "").strip()
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()

    _root_str = str(_repo_root())
    _path_added = _root_str not in sys.path
    if _path_added:
        sys.path.insert(0, _root_str)
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            from plugins.mpv.pipeline_helper import _run_op

            payload = _run_op("ytdlp-formats", {"url": url})
    finally:
        if _path_added:
            sys.path.remove(_root_str)

    noisy_stdout = captured_stdout.getvalue().strip()
    noisy_stderr = captured_stderr.getvalue().strip()
    if noisy_stdout:
        payload["stdout"] = "\n".join(filter(None, [str(payload.get("stdout") or "").strip(), noisy_stdout]))
    if noisy_stderr:
        payload["stderr"] = "\n".join(filter(None, [str(payload.get("stderr") or "").strip(), noisy_stderr]))

    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())