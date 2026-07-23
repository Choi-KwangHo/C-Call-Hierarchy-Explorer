from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer import AnalyzerSession, build_call_view  # noqa: E402
from project_cache import ProjectCacheStore  # noqa: E402


def main() -> None:
    root = str(Path(sys.argv[1]).resolve())
    session = AnalyzerSession()
    started = time.perf_counter()
    result = session.initial_scan(root)
    full_scan = time.perf_counter() - started
    view = build_call_view(result)

    with tempfile.TemporaryDirectory() as temporary:
        store = ProjectCacheStore(temporary)
        started = time.perf_counter()
        path = store.save(root, session.cache, result, {"call_tree": {}})
        save_seconds = time.perf_counter() - started
        started = time.perf_counter()
        payload = store.load(root)
        load_seconds = time.perf_counter() - started
        restored = AnalyzerSession()
        restored.restore(root, payload["session_cache"], payload["result"])
        started = time.perf_counter()
        restored_view = build_call_view(payload["result"])
        view_seconds = time.perf_counter() - started
        print(
            "CACHE_BENCHMARK",
            "files", len(result.files),
            "functions", len(result.functions),
            "rows", len(view.rows),
            "cache_mb", round(path.stat().st_size / 1024 / 1024, 1),
            "full_scan_s", round(full_scan, 3),
            "save_s", round(save_seconds, 3),
            "load_s", round(load_seconds, 3),
            "view_s", round(view_seconds, 3),
            "restored_rows", len(restored_view.rows),
        )


if __name__ == "__main__":
    main()
