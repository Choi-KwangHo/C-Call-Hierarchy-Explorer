from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from iar_map_analyzer import MapAnalysis, choose_map_file, discover_map_files, parse_map_file


class _Signals(QObject):
    result = Signal(object)
    error = Signal(object)


class _Worker(QRunnable):
    def __init__(self, task) -> None:
        super().__init__()
        self.task = task
        self.signals = _Signals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.result.emit(self.task())
        except Exception as error:  # noqa: BLE001
            self.signals.error.emit(error)


class _Metric(QFrame):
    def __init__(self, title: str, accent: str) -> None:
        super().__init__()
        self.setObjectName("mapMetric")
        self.setStyleSheet(
            f"QFrame#mapMetric {{ background:#202A33; border:1px solid #344550; "
            f"border-top:3px solid {accent}; border-radius:8px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.title = QLabel(title)
        self.title.setStyleSheet("color:#9EAFBA;font-size:11px;")
        self.value = QLabel("-")
        self.value.setStyleSheet("color:#F4F8FA;font-size:19px;font-weight:700;")
        self.detail = QLabel("")
        self.detail.setStyleSheet("color:#AFC0CA;font-size:10px;")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def set_value(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.detail.setText(detail)


class IarMapAnalyzerWidget(QWidget):
    """Polished, in-process IAR MAP dashboard."""

    statusChanged = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.root = ""
        self.analysis: MapAnalysis | None = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.worker: _Worker | None = None
        self._last_root = ""
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._watch_map)

    def _build_ui(self) -> None:
        self.setStyleSheet(
            "QWidget#mapRoot { background:#121A20; } QLabel#mapTitle { color:#F5FAFC; font-size:22px; font-weight:700; } "
            "QLabel#mapSub { color:#94A7B3; font-size:11px; } QLineEdit, QComboBox { min-height:28px; } "
            "QTableWidget { border:1px solid #33434E; } QGroupBox { border:1px solid #33434E; margin-top:8px; padding-top:12px; }"
        )
        self.setObjectName("mapRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("IAR MAP Analyzer")
        title.setObjectName("mapTitle")
        self.subtitle = QLabel("분석폴더의 MAP 파일을 자동 감지하고 Flash·SRAM·Stack·Heap을 한 화면에서 점검합니다.")
        self.subtitle.setObjectName("mapSub")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box, 1)
        self.refresh_button = QPushButton("↻ 다시 분석")
        self.refresh_button.clicked.connect(lambda: self.refresh(True))
        self.refresh_button.setObjectName("primary")
        header.addWidget(self.refresh_button)
        outer.addLayout(header)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("MAP 파일"))
        self.map_combo = QComboBox()
        self.map_combo.setMinimumWidth(360)
        self.map_combo.currentIndexChanged.connect(self._map_changed)
        controls.addWidget(self.map_combo, 1)
        choose = QPushButton("파일 선택")
        choose.clicked.connect(self._choose_map)
        controls.addWidget(choose)
        self.path_label = QLabel("분석폴더를 열면 MAP을 자동 검색합니다.")
        self.path_label.setStyleSheet("color:#8FA3AE;font-size:11px;")
        controls.addWidget(self.path_label, 2)
        outer.addLayout(controls)

        metrics = QGridLayout()
        metrics.setSpacing(8)
        self.map_metric = _Metric("MAP 파일", "#1683C5")
        self.flash_metric = _Metric("Flash 사용률", "#2E9CE6")
        self.sram_metric = _Metric("SRAM 사용률", "#A776F0")
        self.stack_metric = _Metric("CSTACK 대비 Stack", "#43C982")
        self.heap_metric = _Metric("HEAP", "#F4A73B")
        for column, widget in enumerate((self.map_metric, self.flash_metric, self.sram_metric, self.stack_metric, self.heap_metric)):
            metrics.addWidget(widget, 0, column)
        outer.addLayout(metrics)

        self.tabs = QTabWidget()
        self.memory_table = QTableWidget(0, 5)
        self.memory_table.setHorizontalHeaderLabels(["영역", "크기", "시작", "끝", "상태"])
        self.memory_table.horizontalHeader().setStretchLastSection(True)
        self.memory_table.setAlternatingRowColors(True)
        self.memory_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._summary_text = QPlainTextEdit()
        self._summary_text.setReadOnly(True)
        self._summary_text.setFont(QFont("Cascadia Mono", 10))
        self._raw_text = QPlainTextEdit()
        self._raw_text.setReadOnly(True)
        self._raw_text.setFont(QFont("Cascadia Mono", 9))
        self._stack_table = QTableWidget(0, 3)
        self._stack_table.setHorizontalHeaderLabels(["Call Graph Root", "Max Use", "Total Use"])
        self._stack_table.horizontalHeader().setStretchLastSection(True)
        self._stack_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tabs.addTab(self.memory_table, "메모리 배치")
        self.tabs.addTab(self._stack_table, "Stack Usage")
        self.tabs.addTab(self._summary_text, "분석 결과")
        self.tabs.addTab(self._raw_text, "MAP 원본 라인")
        outer.addWidget(self.tabs, 1)
        self.status = QLabel("MAP 파일을 기다리는 중입니다.")
        self.status.setStyleSheet("color:#9FB0BA;font-size:11px;")
        outer.addWidget(self.status)

    def set_root(self, root: str) -> None:
        normalized = str(Path(root).resolve()) if root else ""
        if normalized.casefold() == self._last_root.casefold():
            return
        self._last_root = normalized
        self.root = normalized
        self.analysis = None
        self._reload_candidates()
        if self.root:
            self.timer.start()
            self.refresh(False)
        else:
            self.timer.stop()
            self._clear()

    def _reload_candidates(self) -> None:
        self.map_combo.blockSignals(True)
        self.map_combo.clear()
        candidates = discover_map_files(self.root) if self.root else []
        for path in candidates:
            self.map_combo.addItem(path.name, str(path))
        self.map_combo.blockSignals(False)
        if candidates:
            self.path_label.setText(str(candidates[0]))
        else:
            self.path_label.setText("MAP 후보를 찾지 못했습니다. 파일을 직접 선택할 수 있습니다.")

    def _choose_map(self) -> None:
        start = self.root or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "IAR MAP 파일 선택", start, "IAR MAP (*.map);;모든 파일 (*.*)")
        if not path:
            return
        index = self.map_combo.findData(path)
        if index < 0:
            self.map_combo.addItem(Path(path).name, path)
            index = self.map_combo.count() - 1
        self.map_combo.setCurrentIndex(index)
        self.refresh(True)

    def _map_changed(self, _index: int) -> None:
        if self.map_combo.currentData():
            self.path_label.setText(str(self.map_combo.currentData()))
            self.refresh(False)

    def _current_path(self) -> str:
        value = self.map_combo.currentData()
        if value and Path(str(value)).is_file():
            return str(value)
        candidate = choose_map_file(self.root)
        return str(candidate) if candidate else ""

    def refresh(self, force: bool = False) -> None:
        path = self._current_path()
        if not path:
            self._clear()
            self.status.setText("MAP 파일이 없습니다. 분석폴더의 IAR 출력 경로를 확인하십시오.")
            return
        if self.worker is not None:
            return
        if not force and self.analysis and self.analysis.path.casefold() == path.casefold():
            try:
                if self.analysis.signature == f"{Path(path).resolve()}|{Path(path).stat().st_size}|{Path(path).stat().st_mtime_ns}":
                    return
            except OSError:
                pass
        self.status.setText(f"MAP 분석 중: {Path(path).name}")
        worker = _Worker(lambda selected=path: parse_map_file(selected))
        self.worker = worker
        worker.signals.result.connect(self._ready)
        worker.signals.error.connect(self._error)
        worker.signals.result.connect(lambda _value: self._clear_worker(worker))
        worker.signals.error.connect(lambda _value: self._clear_worker(worker))
        self.pool.start(worker)

    def _clear_worker(self, worker: _Worker) -> None:
        if self.worker is worker:
            self.worker = None

    def _watch_map(self) -> None:
        if self.isVisible():
            self._reload_candidates()
            self.refresh(False)

    def _ready(self, analysis: MapAnalysis) -> None:
        self.analysis = analysis
        self.path_label.setText(analysis.path)
        self._display(analysis)
        self.status.setText(f"분석 완료 · {analysis.file_name} · 경고 {len(analysis.warnings)}건")
        self.statusChanged.emit(self.status.text())

    def _error(self, error: Exception) -> None:
        self.status.setText(f"MAP 분석 실패: {error}")
        self.statusChanged.emit(self.status.text())

    @staticmethod
    def _fmt(value: int) -> str:
        if abs(value) >= 1024 * 1024:
            return f"{value / 1024 / 1024:.2f} MB ({value:,} B)"
        return f"{value / 1024:.2f} KB ({value:,} B)"

    @staticmethod
    def _percent(used: int, total: int) -> str:
        return f"{(used * 100 / total):.1f}%" if total else "-"

    def _display(self, a: MapAnalysis) -> None:
        flash_total = a.flash.size
        sram_total = a.sram.size
        self.map_metric.set_value(a.file_name, f"MCU {a.mcu_hint or '자동 확인 필요'}")
        self.flash_metric.set_value(self._percent(a.flash_used, flash_total), f"{self._fmt(a.flash_used)} / {self._fmt(flash_total)}")
        self.sram_metric.set_value(self._percent(a.readwrite_data, sram_total), f"{self._fmt(a.readwrite_data)} / {self._fmt(sram_total)}")
        self.stack_metric.set_value(self._percent(a.max_stack, a.cstack.size), f"{self._fmt(a.max_stack)} / {self._fmt(a.cstack.size)}")
        heap_state = "미확인"
        if a.heap.size:
            heap_state = "사용 API 감지" if (a.malloc_present or a.calloc_present) else "예약량 확인"
        self.heap_metric.set_value(heap_state, f"{self._fmt(a.heap.size)} · NoFree={'예' if a.no_free else '아니오'}")

        rows = [a.flash, a.sram, a.stack_bottom, a.cstack, a.heap]
        self.memory_table.setRowCount(0)
        for block in rows:
            if not block.size and not block.found:
                continue
            row = self.memory_table.rowCount()
            self.memory_table.insertRow(row)
            values = [block.name, self._fmt(block.size), f"0x{block.start:08X}", f"0x{block.end:08X}", "확인" if block.found else "기본값/추정"]
            for col, value in enumerate(values):
                self.memory_table.setItem(row, col, QTableWidgetItem(value))
        self._stack_table.setRowCount(0)
        for item in a.stack_usage:
            row = self._stack_table.rowCount()
            self._stack_table.insertRow(row)
            for col, value in enumerate((item.category, self._fmt(item.max_use), self._fmt(item.total_use))):
                self._stack_table.setItem(row, col, QTableWidgetItem(value))
        summary = [
            f"MAP: {a.file_name}", f"ICF: {a.icf_file or '확인되지 않음'}", f"MCU: {a.mcu_hint or '확인되지 않음'}",
            f"readonly code: {self._fmt(a.readonly_code)}", f"readonly data: {self._fmt(a.readonly_data)}",
            f"readwrite data: {self._fmt(a.readwrite_data)}", f"Static RW 추정: {self._fmt(a.static_rw)}",
            f"STACK_BOTTOM_B: {self._fmt(a.stack_bottom.size)}", f"CSTACK: {self._fmt(a.cstack.size)}",
            f"HEAP: {self._fmt(a.heap.size)}", f"malloc/calloc/free: {'/'.join(('Y' if x else 'N') for x in (a.malloc_present, a.calloc_present, a.free_present))}",
            "", "경고:", *(f"- {warning}" for warning in a.warnings),
        ]
        self._summary_text.setPlainText("\n".join(summary))
        self._raw_text.setPlainText("\n".join(a.raw_lines))

    def _clear(self) -> None:
        self.analysis = None
        for metric in (self.map_metric, self.flash_metric, self.sram_metric, self.stack_metric, self.heap_metric):
            metric.set_value("-", "")
        self.memory_table.setRowCount(0)
        self._stack_table.setRowCount(0)
        self._summary_text.clear()
        self._raw_text.clear()

