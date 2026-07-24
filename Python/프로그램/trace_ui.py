from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QIODevice, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from analyzer import AnalysisResult
from runtime_model import RuntimeObject, build_runtime_objects
from trace_instrumentation import (
    TracePoint, apply_trace_point, generate_trace_runtime, load_trace_points,
    preview_add_trace_point, remove_trace_point,
)

try:
    from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
except ImportError:  # pragma: no cover - depends on the packaged Qt build
    QSerialPort = None
    QSerialPortInfo = None


@dataclass(slots=True)
class TimelineEvent:
    timestamp: int
    event: str
    context: str
    value: str
    raw: str


TRACE_EVENT_NAMES = {
    1: "TASK_IN",
    2: "TASK_OUT",
    3: "ISR_ENTER",
    4: "ISR_EXIT",
    5: "FUNC_ENTER",
    6: "FUNC_EXIT",
    7: "EVENT",
    8: "ERROR",
    9: "HARDFAULT",
}


def parse_trace_line(line: str) -> TimelineEvent | None:
    value = line.strip()
    if not value.startswith("CCH|"):
        return None
    parts = value.split("|")
    if len(parts) < 3:
        return None
    if parts[1].isdigit():
        timestamp = int(parts[1])
        if len(parts) >= 9 and parts[2] == "EVT":
            fields = {
                parts[index]: parts[index + 1]
                for index in range(2, len(parts) - 1, 2)
            }
            try:
                event = TRACE_EVENT_NAMES.get(int(fields.get("EVT", "0")), "EVENT")
            except ValueError:
                event = "EVENT"
            context = fields.get("CTX", "")
            detail = "|".join(
                f"{key}={fields[key]}" for key in ("VAL", "SEQ") if key in fields
            )
            return TimelineEvent(timestamp, event, context, detail, value)
        event = parts[2] if len(parts) > 2 else "EVENT"
        context = parts[3] if len(parts) > 3 else ""
        detail = "|".join(parts[4:]) if len(parts) > 4 else ""
        return TimelineEvent(timestamp, event, context, detail, value)
    if parts[1].startswith("FAULT"):
        return TimelineEvent(0, parts[1], "", "|".join(parts[2:]), value)
    return TimelineEvent(0, parts[1], parts[2] if len(parts) > 2 else "", "|".join(parts[3:]), value)


def parse_iar_log(text: str) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    for line in text.splitlines():
        parsed = parse_trace_line(line)
        if parsed:
            events.append(parsed)
            continue
        fields = next(csv.reader([line], delimiter="\t"))
        if len(fields) < 2 or not any(field.strip() for field in fields):
            continue
        timestamp_match = re.search(r"\d+", fields[0])
        timestamp = int(timestamp_match.group()) if timestamp_match else len(events)
        detail = next((field.strip() for field in reversed(fields[1:]) if field.strip()), "")
        events.append(TimelineEvent(timestamp, "IAR_EVENT", "", detail, line))
    return events


class TimelineCanvas(QWidget):
    COLORS = {
        "TASK_IN": QColor("#2E86C1"), "TASK_OUT": QColor("#85C1E9"),
        "FUNC_ENTER": QColor("#27AE60"), "FUNC_EXIT": QColor("#82E0AA"),
        "ISR_ENTER": QColor("#8E44AD"), "ISR_EXIT": QColor("#C39BD3"),
        "ERROR": QColor("#E74C3C"), "FAULT_BEGIN": QColor("#922B21"),
        "HARDFAULT": QColor("#922B21"), "IAR_EVENT": QColor("#F39C12"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.events: list[TimelineEvent] = []
        self.setMinimumHeight(210)

    def set_events(self, events: list[TimelineEvent]) -> None:
        self.events = list(events[-5000:])
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111418"))
        if not self.events:
            painter.setPen(QColor("#AAB2BD"))
            painter.drawText(self.rect(), Qt.AlignCenter, "수신된 Trace 이벤트가 없습니다.")
            return
        contexts = list(dict.fromkeys(item.context or item.event for item in self.events))[:16]
        lane = {name: index for index, name in enumerate(contexts)}
        left = 150
        top = 16
        row_height = max(18, (self.height() - top * 2) // max(1, len(contexts)))
        timestamps = [item.timestamp for item in self.events if item.timestamp > 0]
        minimum = min(timestamps, default=0)
        maximum = max(timestamps, default=minimum + 1)
        span = max(1, maximum - minimum)
        painter.setPen(QColor("#89939E"))
        for name, index in lane.items():
            y = top + index * row_height
            painter.drawText(6, y + row_height - 5, name[:22])
            painter.setPen(QPen(QColor("#29313A"), 1))
            painter.drawLine(left, y + row_height, self.width() - 8, y + row_height)
            painter.setPen(QColor("#89939E"))
        width = max(1, self.width() - left - 16)
        for item in self.events:
            key = item.context or item.event
            if key not in lane:
                continue
            x = left + int(((item.timestamp - minimum) / span) * width) if item.timestamp else left
            y = top + lane[key] * row_height + 3
            color = self.COLORS.get(item.event, QColor("#5DADE2"))
            painter.fillRect(x, y, max(4, min(18, width // 80 + 4)), row_height - 6, color)


class TraceCenterDialog(QDialog):
    functionActivated = Signal(str)
    sourcesChanged = Signal()

    KIND_LABELS = {
        "main": "Main Loop", "task": "FreeRTOS / RTOS Task",
        "isr": "Interrupt / ISR", "timer": "Software Timer",
        "callback": "Callback / Hook",
    }
    EVENT_LABELS = {
        "FUNC_ENTER": "함수 진입", "FUNC_EXIT": "함수 종료",
        "EVENT": "사용자 이벤트", "ERROR": "오류 이벤트",
        "IAR_EVENT": "IAR ITM Event", "HARDFAULT_DUMP": "HardFault 링버퍼 출력",
    }

    def __init__(self, result: AnalysisResult, parent=None) -> None:
        super().__init__(parent)
        self.result = result
        self.root = Path(result.root)
        self.runtime_objects: list[RuntimeObject] = []
        self.points: list[TracePoint] = []
        self.events: list[TimelineEvent] = []
        self._serial_buffer = ""
        self.serial = QSerialPort(self) if QSerialPort is not None else None
        self.setWindowTitle("실행 구조 및 Trace 센터")
        self.resize(1120, 760)
        self._build_ui()
        self.refresh(result)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_overview_tab()
        self._build_points_tab()
        self._build_timeline_tab()
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

    def _build_overview_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        guide = QLabel(
            "main, RTOS Task, ISR, Timer, Callback을 실행 객체로 분류합니다. "
            "객체를 더블클릭하면 기존 호출 트리와 CODE 미리보기로 이동합니다."
        )
        guide.setWordWrap(True)
        self.object_tree = QTreeWidget()
        self.object_tree.setHeaderLabels(["실행 객체", "위치", "판정", "근거 / 속성"])
        self.object_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.object_tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.object_tree.itemDoubleClicked.connect(self._object_activated)
        layout.addWidget(guide)
        layout.addWidget(self.object_tree, 1)
        self.tabs.addTab(page, "실행 객체")

    def _build_points_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        warning = QLabel(
            "계측은 사용자가 위치와 내용을 선택하고 아래 미리보기를 확인한 뒤에만 삽입됩니다. "
            "CCH 관리 마커가 변경된 경우 자동 제거하지 않습니다."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        form = QFormLayout()
        self.file_combo = QComboBox()
        self.file_combo.currentIndexChanged.connect(self._file_changed)
        self.function_combo = QComboBox()
        self.function_combo.currentIndexChanged.connect(self._function_changed)
        self.line_spin = QSpinBox()
        self.line_spin.setRange(1, 1)
        self.event_combo = QComboBox()
        for key, label in self.EVENT_LABELS.items():
            self.event_combo.addItem(label, key)
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("예: ParsingData 진입")
        self.value_edit = QLineEdit("0u")
        self.channel_spin = QSpinBox()
        self.channel_spin.setRange(1, 4)
        form.addRow("파일", self.file_combo)
        form.addRow("함수", self.function_combo)
        form.addRow("삽입 행", self.line_spin)
        form.addRow("이벤트", self.event_combo)
        form.addRow("표시 이름", self.label_edit)
        form.addRow("값/식", self.value_edit)
        form.addRow("IAR ITM 채널", self.channel_spin)
        layout.addLayout(form)
        actions = QHBoxLayout()
        preview_button = QPushButton("변경 미리보기")
        preview_button.clicked.connect(self._preview_point)
        apply_button = QPushButton("선택 위치에 삽입…")
        apply_button.clicked.connect(self._apply_point)
        remove_button = QPushButton("선택 계측 제거…")
        remove_button.clicked.connect(self._remove_point)
        generate_button = QPushButton("Trace 런타임 파일 생성/갱신")
        generate_button.clicked.connect(self._generate_runtime)
        actions.addWidget(preview_button)
        actions.addWidget(apply_button)
        actions.addWidget(remove_button)
        actions.addWidget(generate_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        splitter = QSplitter(Qt.Vertical)
        self.point_table = QTableWidget(0, 6)
        self.point_table.setHorizontalHeaderLabels(["이름", "이벤트", "파일", "행", "채널", "ID"])
        self.point_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.point_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setPlaceholderText("변경 미리보기가 여기에 표시됩니다.")
        splitter.addWidget(self.point_table)
        splitter.addWidget(self.preview_edit)
        splitter.setSizes([250, 250])
        layout.addWidget(splitter, 1)
        self.tabs.addTab(page, "테스트 포인트")

    def _build_timeline_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.port_combo = QComboBox()
        self.baud_combo = QComboBox()
        for baud in ("115200", "230400", "460800", "921600"):
            self.baud_combo.addItem(baud)
        refresh = QPushButton("포트 새로고침")
        refresh.clicked.connect(self._refresh_ports)
        self.connect_button = QPushButton("연결")
        self.connect_button.clicked.connect(self._toggle_serial)
        import_button = QPushButton("IAR Event Log / Trace 파일 가져오기…")
        import_button.clicked.connect(self._import_log)
        clear_button = QPushButton("지우기")
        clear_button.clicked.connect(self._clear_events)
        controls.addWidget(QLabel("COM"))
        controls.addWidget(self.port_combo)
        controls.addWidget(QLabel("Baud"))
        controls.addWidget(self.baud_combo)
        controls.addWidget(refresh)
        controls.addWidget(self.connect_button)
        controls.addWidget(import_button)
        controls.addWidget(clear_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.timeline_canvas = TimelineCanvas()
        self.event_table = QTableWidget(0, 5)
        self.event_table.setHorizontalHeaderLabels(["시간", "이벤트", "Context", "값", "원문"])
        self.event_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.timeline_canvas)
        splitter.addWidget(self.event_table)
        splitter.addWidget(self.terminal)
        splitter.setSizes([230, 260, 150])
        layout.addWidget(splitter, 1)
        self.tabs.addTab(page, "I/O Timeline")
        if self.serial is not None:
            self.serial.readyRead.connect(self._serial_ready)
        self._refresh_ports()

    def refresh(self, result: AnalysisResult) -> None:
        self.result = result
        self.root = Path(result.root)
        self.runtime_objects = build_runtime_objects(result)
        self.points = load_trace_points(self.root)
        self._populate_objects()
        self._populate_files()
        self._populate_points()

    def _populate_objects(self) -> None:
        self.object_tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for kind in self.KIND_LABELS:
            group = QTreeWidgetItem([self.KIND_LABELS[kind], "", "", ""])
            group.setExpanded(True)
            self.object_tree.addTopLevelItem(group)
            groups[kind] = group
        for item in self.runtime_objects:
            attributes = " · ".join(f"{key}: {value}" for key, value in item.attributes.items())
            detail = item.evidence + (f" · {attributes}" if attributes else "")
            child = QTreeWidgetItem([
                item.name,
                f"{Path(item.file).name}:{item.line}",
                item.confidence,
                detail,
            ])
            child.setData(0, Qt.UserRole, item.function_id or "")
            child.setToolTip(3, detail)
            groups.get(item.kind, groups["callback"]).addChild(child)
        for group in groups.values():
            group.setHidden(group.childCount() == 0)

    def _object_activated(self, item: QTreeWidgetItem) -> None:
        function_id = str(item.data(0, Qt.UserRole) or "")
        if function_id:
            self.functionActivated.emit(function_id)

    def _populate_files(self) -> None:
        current = self.file_combo.currentData()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        for parsed in self.result.files:
            if parsed.functions and Path(parsed.path).suffix.lower() in {".c", ".h"}:
                self.file_combo.addItem(parsed.relative_path, parsed.path)
        if current:
            index = self.file_combo.findData(current)
            if index >= 0:
                self.file_combo.setCurrentIndex(index)
        self.file_combo.blockSignals(False)
        self._file_changed()

    def _file_changed(self) -> None:
        path = str(self.file_combo.currentData() or "")
        parsed = next((item for item in self.result.files if item.path == path), None)
        self.function_combo.blockSignals(True)
        self.function_combo.clear()
        if parsed:
            for function in parsed.functions:
                self.function_combo.addItem(
                    f"{function.name}()  {function.start_line}-{function.end_line}",
                    function.id,
                )
            self.line_spin.setMaximum(max(1, len(parsed.text.splitlines()) + 1))
        self.function_combo.blockSignals(False)
        self._function_changed()

    def _function_changed(self) -> None:
        function = self.result.function(str(self.function_combo.currentData() or ""))
        if function:
            parsed = next((item for item in self.result.files if item.path == function.path), None)
            recommended = function.start_line + 1
            if parsed is not None:
                source_lines = parsed.text.splitlines()
                for line_index in range(
                    max(0, function.start_line - 1),
                    min(len(source_lines), function.end_line),
                ):
                    if "{" in source_lines[line_index]:
                        recommended = line_index + 2
                        break
            self.line_spin.setValue(min(self.line_spin.maximum(), recommended))
            self.label_edit.setText(f"{function.name} {self.event_combo.currentText()}")

    def _point_arguments(self) -> tuple[str, int, str, str, str, int]:
        path = str(self.file_combo.currentData() or "")
        if not path:
            raise ValueError("계측을 삽입할 파일을 선택하십시오.")
        return (
            path,
            self.line_spin.value(),
            str(self.event_combo.currentData()),
            self.label_edit.text(),
            self.value_edit.text(),
            self.channel_spin.value(),
        )

    def _preview_point(self) -> None:
        try:
            _, _, diff = preview_add_trace_point(self.root, *self._point_arguments())
            self.preview_edit.setPlainText(diff)
        except Exception as error:
            QMessageBox.warning(self, "계측 미리보기", str(error))

    def _apply_point(self) -> None:
        try:
            point, _, diff = preview_add_trace_point(self.root, *self._point_arguments())
        except Exception as error:
            QMessageBox.warning(self, "계측 삽입", str(error))
            return
        self.preview_edit.setPlainText(diff)
        answer = QMessageBox.question(
            self,
            "선택 위치에 계측 삽입",
            f"{point.file}:{point.line}에 계측 코드를 삽입합니다.\n"
            "미리보기 내용을 확인하셨습니까?",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            apply_trace_point(
                self.root, *self._point_arguments()
            )
            self.points = load_trace_points(self.root)
            self._populate_points()
            self.sourcesChanged.emit()
        except Exception as error:
            QMessageBox.critical(self, "계측 삽입 실패", str(error))

    def _remove_point(self) -> None:
        row = self.point_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "계측 제거", "제거할 계측 항목을 선택하십시오.")
            return
        point_id = self.point_table.item(row, 5).text()
        if QMessageBox.question(
            self, "계측 제거", "선택한 CCH 관리 계측 블록을 제거하시겠습니까?"
        ) != QMessageBox.Yes:
            return
        try:
            remove_trace_point(self.root, point_id)
            self.points = load_trace_points(self.root)
            self._populate_points()
            self.sourcesChanged.emit()
        except Exception as error:
            QMessageBox.critical(self, "계측 제거 실패", str(error))

    def _generate_runtime(self) -> None:
        try:
            header, source = generate_trace_runtime(self.root, self.points)
            QMessageBox.information(
                self,
                "Trace 런타임 생성",
                f"생성 완료:\n{header}\n{source}\n\n"
                "cch_trace.c를 펌웨어 빌드에 추가하고 CCH_Trace 폴더를 Include 경로에 포함하십시오.",
            )
            self.sourcesChanged.emit()
        except Exception as error:
            QMessageBox.critical(self, "Trace 런타임 생성 실패", str(error))

    def _populate_points(self) -> None:
        self.point_table.setRowCount(len(self.points))
        for row, point in enumerate(self.points):
            values = [
                point.label, self.EVENT_LABELS.get(point.event, point.event),
                point.file, str(point.line), str(point.channel), point.id,
            ]
            for column, value in enumerate(values):
                self.point_table.setItem(row, column, QTableWidgetItem(value))

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        if QSerialPortInfo is not None:
            for port in QSerialPortInfo.availablePorts():
                self.port_combo.addItem(port.portName())
        index = self.port_combo.findText(current)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)

    def _toggle_serial(self) -> None:
        if self.serial is None:
            QMessageBox.warning(self, "시리얼 포트", "현재 PySide6 패키지에 QtSerialPort가 없습니다.")
            return
        if self.serial.isOpen():
            self.serial.close()
            self.connect_button.setText("연결")
            return
        self.serial.setPortName(self.port_combo.currentText())
        self.serial.setBaudRate(int(self.baud_combo.currentText()))
        if not self.serial.open(QIODevice.ReadOnly):
            QMessageBox.warning(self, "시리얼 연결", self.serial.errorString())
            return
        self.connect_button.setText("연결 해제")

    def _serial_ready(self) -> None:
        if self.serial is None:
            return
        self._serial_buffer += bytes(self.serial.readAll()).decode("utf-8", errors="replace")
        while "\n" in self._serial_buffer:
            line, self._serial_buffer = self._serial_buffer.split("\n", 1)
            self._consume_line(line.rstrip("\r"))

    def _consume_line(self, line: str) -> None:
        self.terminal.appendPlainText(line)
        parsed = parse_trace_line(line)
        if parsed:
            self.events.append(parsed)
            self._refresh_events()

    def _refresh_events(self) -> None:
        visible = self.events[-5000:]
        self.event_table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            values = [str(item.timestamp), item.event, item.context, item.value, item.raw]
            for column, value in enumerate(values):
                self.event_table.setItem(row, column, QTableWidgetItem(value))
        self.timeline_canvas.set_events(visible)
        if visible:
            self.event_table.scrollToBottom()

    def _import_log(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "IAR Event Log 또는 Trace 파일", str(self.root), "Log files (*.log *.txt *.tsv *.csv);;모든 파일 (*.*)"
        )
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        imported_events = parse_iar_log(text)
        self.events.extend(imported_events)
        imported = len(imported_events)
        self.terminal.appendPlainText(f"[가져오기] {Path(path).name}: {imported}개 이벤트")
        self._refresh_events()

    def _clear_events(self) -> None:
        self.events.clear()
        self.terminal.clear()
        self._refresh_events()

    def done(self, result: int) -> None:
        if self.serial is not None and self.serial.isOpen():
            self.serial.close()
        super().done(result)
