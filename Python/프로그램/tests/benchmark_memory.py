from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from analyzer import AnalyzerSession, build_call_view  # noqa: E402
from virtual_tree import CallTreeWidget  # noqa: E402


class ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def memory_mb() -> tuple[float, float]:
    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCountersEx),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    scale = 1024 * 1024
    return counters.WorkingSetSize / scale, counters.PrivateUsage / scale


def main() -> None:
    root = sys.argv[1]
    app = QApplication.instance() or QApplication([])
    baseline = memory_mb()

    started = time.perf_counter()
    result = AnalyzerSession().initial_scan(root)
    scan_seconds = time.perf_counter() - started
    after_scan = memory_mb()

    started = time.perf_counter()
    view = build_call_view(result, include_other_roots=True)
    view_seconds = time.perf_counter() - started
    after_view = memory_mb()

    widget = CallTreeWidget()
    widget.resize(1400, 850)
    started = time.perf_counter()
    widget.set_view(view, False)
    app.processEvents()
    ui_seconds = time.perf_counter() - started
    after_ui = memory_mb()

    depth = max(widget.body._source_child_keys_by_depth, key=lambda value: len(widget.body._source_child_keys_by_depth[value]))
    started = time.perf_counter()
    widget.body.toggle_depth(depth)
    app.processEvents()
    collapse_seconds = time.perf_counter() - started

    sections = [row.state for row in view.rows if row.kind == "section"]
    print("MEMORY_MB", "baseline", *[round(value, 1) for value in baseline])
    print("MEMORY_MB", "scan", *[round(value, 1) for value in after_scan])
    print("MEMORY_MB", "view", *[round(value, 1) for value in after_view])
    print("MEMORY_MB", "ui", *[round(value, 1) for value in after_ui])
    print("COUNTS", len(result.files), len(result.functions), len(view.rows), view.max_depth, view.interrupt_roots)
    print("SECTIONS", {name: sections.count(name) for name in sorted(set(sections))})
    print("SECONDS", round(scan_seconds, 3), round(view_seconds, 3), round(ui_seconds, 3), round(collapse_seconds, 3), "depth", depth)
    widget.close()


if __name__ == "__main__":
    main()
