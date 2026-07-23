from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer import AnalyzerSession, build_call_view, choose_main  # noqa: E402
from xlsx_exporter import export_xlsx  # noqa: E402


def deepest(result, root_id: str) -> int:
    best = 0
    visited: set[str] = set()
    stack = [(root_id, 1, frozenset())]
    while stack:
        function_id, depth, ancestors = stack.pop()
        best = max(best, depth)
        if function_id in ancestors:
            continue
        if function_id in visited:
            continue
        visited.add(function_id)
        function = result.function(function_id)
        if not function:
            continue
        path = ancestors | {function_id}
        for call in function.calls:
            if call.target_id:
                stack.append((call.target_id, depth + 1, path))
    return best


root = sys.argv[1]
started = time.perf_counter()
session = AnalyzerSession()
result = session.initial_scan(root, lambda phase, current, total, detail: print(phase, current, total) if current == total else None)
elapsed = time.perf_counter() - started
view = build_call_view(result)
main = choose_main(result)
app_tasks = result.by_name.get("App_Task", [])
print(
    "RESULT", len(result.files), len(result.functions), result.clang_files,
    len(view.rows), view.max_depth,
    sum(row.state == "safety_limit" for row in view.rows),
    round(elapsed, 2),
)
print("MAIN", main.path if main else "none", deepest(result, main.id) if main else 0)
for function in app_tasks[:5]:
    print("APP_TASK", function.path, len(function.calls), deepest(result, function.id))
if len(sys.argv) > 2:
    export_xlsx(
        sys.argv[2],
        result,
        view,
        lambda current, total, detail: print("XLSX", current, total, detail) if current == total else None,
    )
    print("XLSX_SAVED", sys.argv[2])
