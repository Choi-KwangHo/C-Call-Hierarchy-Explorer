from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QStandardPaths, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from eeprom_map import (
    AT24C128_CAPACITY, AT24C128_PAGE_SIZE, EepromMapResult,
    EepromSourceConfig, StructInfo, analyze_eeprom_source, load_source_configs,
    parse_github_location, save_source_configs, source_catalog_path, source_revision,
)


class _WorkerSignals(QObject):
    progress = Signal(int, int, str)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class _Worker(QRunnable):
    def __init__(self, function) -> None:
        super().__init__()
        self.function = function
        self.signals = _WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(self.signals.progress.emit)
            try:
                self.signals.result.emit(result)
            except RuntimeError:
                pass
        except Exception as error:  # noqa: BLE001 - convert background failures to UI errors
            try:
                self.signals.error.emit(str(error))
            except RuntimeError:
                pass
        finally:
            try:
                self.signals.finished.emit()
            except RuntimeError:
                pass


class SummaryCard(QFrame):
    def __init__(self, title: str, value: str = "-") -> None:
        super().__init__()
        self.setObjectName("summaryCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(3)
        caption = QLabel(title)
        caption.setObjectName("cardCaption")
        self.value = QLabel(value)
        self.value.setObjectName("cardValue")
        layout.addWidget(caption)
        layout.addWidget(self.value)


class MemoryMapCanvas(QWidget):
    pageSelected = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.result: EepromMapResult | None = None
        self.selected_page = -1
        self.setMinimumHeight(245)
        self.setMouseTracking(True)

    def set_result(self, result: EepromMapResult | None) -> None:
        self.result = result
        self.selected_page = -1
        self.update()

    def _geometry(self) -> tuple[int, int, int, int, int]:
        columns = 16
        rows = 16
        left, top = 62, 26
        cell_width = max(24, (self.width() - left - 16) // columns)
        cell_height = max(11, (self.height() - top - 18) // rows)
        return left, top, cell_width, cell_height, columns

    def _page_at(self, position) -> int:
        left, top, width, height, columns = self._geometry()
        column = int((position.x() - left) // width)
        row = int((position.y() - top) // height)
        if 0 <= column < columns and 0 <= row < 16:
            return row * columns + column
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        page = self._page_at(event.position())
        if page < 0 or not self.result:
            self.setToolTip("")
            return
        start = page * self.result.config.page_size
        matches = [region for region in self.result.regions if region.address < start + self.result.config.page_size and region.address + region.size > start]
        detail = "\n".join(f"• {item.name}: {item.payload_size}/{item.size} bytes ({item.status})" for item in matches)
        self.setToolTip(
            f"Page {page} · 0x{start:04X}~0x{start + self.result.config.page_size - 1:04X}"
            + (f"\n{detail}" if detail else "\n미사용")
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            page = self._page_at(event.position())
            if page >= 0:
                self.selected_page = page
                self.pageSelected.emit(page)
                self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#12171D"))
        painter.setRenderHint(QPainter.Antialiasing, False)
        if not self.result:
            painter.setPen(QColor("#9AA7B2"))
            painter.drawText(self.rect(), Qt.AlignCenter, "EEPROM 맵을 분석하면 256개 물리 페이지가 여기에 표시됩니다.")
            return
        left, top, width, height, columns = self._geometry()
        page_count = max(1, self.result.config.capacity // self.result.config.page_size)
        states = ["free"] * page_count
        for region in self.result.regions:
            if not region.allocated:
                continue
            first = max(0, region.address // self.result.config.page_size)
            last = min(page_count - 1, max(region.address, region.address + region.size - 1) // self.result.config.page_size)
            for page in range(first, last + 1):
                if region.status == "중복 영역" or states[page] == "used":
                    states[page] = "overlap"
                else:
                    states[page] = "used"
        painter.setFont(QFont("Segoe UI", 8))
        for page in range(min(256, page_count)):
            row, column = divmod(page, columns)
            x, y = left + column * width, top + row * height
            color = QColor("#26313A")
            if states[page] == "used":
                color = QColor("#2583C5")
            elif states[page] == "overlap":
                color = QColor("#D94A4A")
            if page == self.selected_page:
                color = QColor("#F0A33B")
            painter.fillRect(x + 1, y + 1, width - 2, height - 2, color)
            painter.setPen(QColor("#3A4752"))
            painter.drawRect(x, y, width, height)
        painter.setPen(QColor("#AAB6C0"))
        for row in range(16):
            painter.drawText(6, top + row * height, 50, height, Qt.AlignVCenter | Qt.AlignRight, f"{row * 16:03d}")
        for column in range(16):
            painter.drawText(left + column * width, 3, width, 20, Qt.AlignCenter, f"{column:X}")


class CDeclarationHighlighter(QSyntaxHighlighter):
    def __init__(self, document) -> None:
        super().__init__(document)
        self.rules: list[tuple[object, QTextCharFormat]] = []
        import re
        keyword = QTextCharFormat()
        keyword.setForeground(QColor("#569CD6"))
        keyword.setFontWeight(QFont.Bold)
        for word in ("typedef", "struct", "union", "enum", "const", "volatile", "unsigned", "signed", "char", "short", "int", "long", "float", "double", "void"):
            self.rules.append((re.compile(rf"\b{word}\b"), keyword))
        number = QTextCharFormat()
        number.setForeground(QColor("#B5CEA8"))
        self.rules.append((re.compile(r"\b(?:0x[0-9A-Fa-f]+|\d+)\b"), number))
        comment = QTextCharFormat()
        comment.setForeground(QColor("#6A9955"))
        self.rules.append((re.compile(r"//.*$"), comment))

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        for pattern, text_format in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), text_format)


class EepromSourceSettingsDialog(QDialog):
    HEADERS = ["아이템 표시명", "GitHub 저장소/브랜치 주소", "브랜치", "분석 하위 폴더", "용량", "페이지", "자동", "주기(분)"]

    def __init__(self, configs: list[EepromSourceConfig], current_root: str, parent=None) -> None:
        super().__init__(parent)
        self.current_root = current_root
        self.setWindowTitle("EEPROM 소스 및 자동 동기화 설정")
        self.resize(1280, 560)
        self.setMinimumSize(900, 430)
        self.setStyleSheet("""
            QDialog { background:#181C20; color:#E6EDF3; }
            QTableWidget { background:#11161B; alternate-background-color:#171D23; color:#DCE5EC; gridline-color:#33404A; }
            QHeaderView::section { background:#27313A; color:#F2F6F8; padding:7px; border:0; border-right:1px solid #3C4A54; }
            QLineEdit, QSpinBox { background:#10151A; color:#E6EDF3; border:1px solid #46545E; padding:5px; }
            QPushButton { background:#2C3740; color:#F3F6F8; border:1px solid #495864; padding:7px 12px; }
            QPushButton:hover { background:#3B4A55; }
            QPushButton#primary { background:#1479B8; border-color:#1479B8; }
            QLabel#help { color:#AAB7C1; }
            QCheckBox { color:#E6EDF3; }
        """)
        layout = QVBoxLayout(self)
        title = QLabel("AT24C128 프로젝트 소스")
        title.setStyleSheet("font-size:22px; font-weight:600; color:#FFFFFF;")
        help_label = QLabel(
            "표시명과 GitHub 저장소 또는 /tree/ 브랜치 주소를 등록합니다. 자동 동기화는 선택한 주기로 원격 커밋만 확인하고, 변경된 경우에만 다시 분석합니다."
        )
        help_label.setObjectName("help")
        help_label.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(help_label)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        for config in configs:
            self._append(config)
        layout.addWidget(self.table, 1)
        controls = QHBoxLayout()
        add = QPushButton("＋ 항목 추가")
        add.clicked.connect(lambda: self._append(EepromSourceConfig.create("새 EEPROM 항목")))
        remove = QPushButton("선택 항목 삭제")
        remove.clicked.connect(self._remove_selected)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addStretch(1)
        self.deploy_default = QCheckBox("현재 목록을 다음 배포 기본값에도 반영")
        self.deploy_default.setChecked(source_catalog_path() is not None)
        self.deploy_default.setEnabled(source_catalog_path() is not None)
        self.deploy_default.setToolTip("개발 소스에서 실행할 때 eeprom_sources.json에 기록되어 다음 release.bat 배포에 포함됩니다.")
        controls.addWidget(self.deploy_default)
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Ok).setText("적용")
        buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _append(self, config: EepromSourceConfig) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            config.display_name, config.repository_url, config.branch,
            config.subdirectory, str(config.capacity), str(config.page_size),
            "", str(config.refresh_minutes),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.UserRole, config.id)
            if column == 6:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if config.auto_refresh else Qt.Unchecked)
            self.table.setItem(row, column, item)

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def configs(self) -> list[EepromSourceConfig]:
        values: list[EepromSourceConfig] = []
        for row in range(self.table.rowCount()):
            text = lambda column: (self.table.item(row, column).text().strip() if self.table.item(row, column) else "")
            values.append(EepromSourceConfig(
                id=str(self.table.item(row, 0).data(Qt.UserRole) or ""),
                display_name=text(0), repository_url=text(1), branch=text(2) or "main",
                subdirectory=text(3), capacity=int(text(4)), page_size=int(text(5)),
                auto_refresh=self.table.item(row, 6).checkState() == Qt.Checked,
                refresh_minutes=int(text(7)),
            ))
        return values

    def _validate_accept(self) -> None:
        try:
            configs = self.configs()
            names: set[str] = set()
            for config in configs:
                if not config.display_name:
                    raise ValueError("아이템 표시명을 입력하십시오.")
                if config.display_name.casefold() in names:
                    raise ValueError(f"중복된 아이템 표시명입니다: {config.display_name}")
                names.add(config.display_name.casefold())
                if not 1 <= config.refresh_minutes <= 10:
                    raise ValueError("자동 동기화 주기는 1분에서 10분 사이여야 합니다.")
                if config.capacity <= 0 or config.page_size <= 0 or config.capacity % config.page_size:
                    raise ValueError(f"{config.display_name}: 용량은 페이지 크기의 배수여야 합니다.")
                parse_github_location(config.repository_url, config.branch)
        except (ValueError, TypeError) as error:
            QMessageBox.warning(self, "EEPROM 설정 확인", str(error))
            return
        self.accept()


class EepromMapDialog(QDialog):
    configsChanged = Signal()

    def __init__(self, settings: QSettings, current_root: str, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.current_root = current_root
        self.configs = load_source_configs(settings, current_root)
        self.results: dict[str, EepromMapResult] = {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.worker: _Worker | None = None
        self._checking_only = False
        self.cache_root = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "eeprom-repositories"
        self.setWindowTitle("AT24C128 EEPROM 메모리 맵")
        self.resize(1360, 860)
        self.setMinimumSize(980, 650)
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._automatic_refresh)
        self._reload_combo()
        self._reset_timer()
        QTimer.singleShot(0, lambda: self.refresh(True))

    def _build_ui(self) -> None:
        self.setStyleSheet("""
            QDialog { background:#151A1F; color:#E5EDF3; }
            QLabel { color:#DDE6EC; }
            QLabel#title { font-size:22px; font-weight:600; color:#FFFFFF; }
            QLabel#subtitle { color:#9FADB8; }
            QFrame#summaryCard { background:#202830; border:1px solid #34414B; border-radius:6px; }
            QLabel#cardCaption { color:#9EACB7; font-size:11px; }
            QLabel#cardValue { color:#F5F8FA; font-size:17px; font-weight:600; }
            QComboBox, QLineEdit { background:#10161B; color:#E8EEF2; border:1px solid #43515C; padding:6px; }
            QPushButton { background:#2A3540; color:#F1F5F7; border:1px solid #46545F; padding:7px 12px; }
            QPushButton:hover { background:#3A4853; }
            QPushButton#primary { background:#147CB8; border-color:#147CB8; }
            QTabWidget::pane { border:1px solid #33404A; background:#11171C; }
            QTabBar::tab { background:#222B33; color:#BFCBD4; padding:8px 16px; }
            QTabBar::tab:selected { background:#147CB8; color:#FFFFFF; }
            QTableWidget { background:#11171C; alternate-background-color:#171E24; color:#DDE6EC; gridline-color:#303C45; selection-background-color:rgba(20,124,184,120); selection-color:#FFFFFF; }
            QHeaderView::section { background:#253039; color:#E7EDF1; border:0; border-right:1px solid #3A4650; padding:7px; }
            QPlainTextEdit { background:#0E1418; color:#DCE5EB; border:1px solid #33404A; selection-background-color:rgba(20,124,184,130); }
            QProgressBar { background:#202830; color:#FFFFFF; border:1px solid #3A4650; text-align:center; min-height:18px; }
            QProgressBar::chunk { background:#1789C9; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        top = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("AT24C128 EEPROM 메모리 맵")
        title.setObjectName("title")
        self.subtitle = QLabel("GitHub 브랜치의 페이지 정의와 저장 구조체를 연결해 표시합니다.")
        self.subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        top.addLayout(title_box, 1)
        top.addWidget(QLabel("아이템"))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(280)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        top.addWidget(self.source_combo)
        refresh = QPushButton("지금 동기화")
        refresh.setObjectName("primary")
        refresh.clicked.connect(lambda: self.refresh(True))
        top.addWidget(refresh)
        configure = QPushButton("소스 설정…")
        configure.clicked.connect(self.open_settings)
        top.addWidget(configure)
        layout.addLayout(top)

        cards = QHBoxLayout()
        self.used_card = SummaryCard("사용량")
        self.free_card = SummaryCard("여유 공간")
        self.page_card = SummaryCard("사용 페이지")
        self.warning_card = SummaryCard("검토 항목")
        self.commit_card = SummaryCard("Git 커밋")
        for card in (self.used_card, self.free_card, self.page_card, self.warning_card, self.commit_card):
            cards.addWidget(card, 1)
        layout.addLayout(cards)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        map_page = QWidget()
        map_layout = QVBoxLayout(map_page)
        map_layout.setContentsMargins(8, 8, 8, 8)
        legend = QLabel("■ 사용 페이지    ■ 중복/충돌    ■ 선택 페이지    ■ 미사용")
        legend.setStyleSheet("color:#AAB6C0;")
        map_layout.addWidget(legend)
        self.canvas = MemoryMapCanvas()
        self.canvas.pageSelected.connect(self._select_page)
        map_layout.addWidget(self.canvas)
        self.region_table = QTableWidget(0, 12)
        self.region_table.setHorizontalHeaderLabels([
            "페이지", "시작 주소", "끝 주소", "영역/기호", "구조체", "Payload", "물리 할당",
            "여유", "접근", "상태", "소스", "행",
        ])
        self.region_table.setAlternatingRowColors(True)
        self.region_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.region_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.region_table.itemSelectionChanged.connect(self._region_selected)
        self.region_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.region_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.region_table.horizontalHeader().setSectionResizeMode(10, QHeaderView.Stretch)
        map_layout.addWidget(self.region_table, 1)
        self.tabs.addTab(map_page, "메모리 맵")

        structure_page = QWidget()
        structure_layout = QVBoxLayout(structure_page)
        structure_layout.setContentsMargins(8, 8, 8, 8)
        splitter = QSplitter(Qt.Vertical)
        self.structure_table = QTableWidget(0, 6)
        self.structure_table.setHorizontalHeaderLabels(["구조체/Union", "크기", "필드 수", "정렬", "파일", "행"])
        self.structure_table.setAlternatingRowColors(True)
        self.structure_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.structure_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.structure_table.itemSelectionChanged.connect(self._structure_selected)
        self.structure_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.structure_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        splitter.addWidget(self.structure_table)
        preview_frame = QFrame()
        preview_layout = QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(0, 4, 0, 0)
        self.structure_caption = QLabel("구조체를 선택하면 원본 C 선언을 표시합니다.")
        self.structure_caption.setStyleSheet("font-weight:600; color:#CFE8F7; padding:3px;")
        self.code_preview = QPlainTextEdit()
        self.code_preview.setReadOnly(True)
        self.code_preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.code_preview.setFont(QFont("Cascadia Mono", 10))
        self.highlighter = CDeclarationHighlighter(self.code_preview.document())
        preview_layout.addWidget(self.structure_caption)
        preview_layout.addWidget(self.code_preview, 1)
        splitter.addWidget(preview_frame)
        splitter.setSizes([300, 330])
        structure_layout.addWidget(splitter)
        self.tabs.addTab(structure_page, "저장 구조체")

        self.warning_view = QPlainTextEdit()
        self.warning_view.setReadOnly(True)
        self.warning_view.setFont(QFont("Cascadia Mono", 9))
        self.tabs.addTab(self.warning_view, "검토 및 분석 근거")

    def _reload_combo(self) -> None:
        current = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for config in self.configs:
            self.source_combo.addItem(config.display_name, config.id)
        index = self.source_combo.findData(current)
        self.source_combo.setCurrentIndex(index if index >= 0 else (0 if self.configs else -1))
        self.source_combo.blockSignals(False)

    def _current_config(self) -> EepromSourceConfig | None:
        identity = self.source_combo.currentData()
        return next((item for item in self.configs if item.id == identity), None)

    def _source_changed(self) -> None:
        config = self._current_config()
        self._reset_timer()
        if config and config.id in self.results:
            self._display(self.results[config.id])
        elif config:
            self.refresh(True)

    def _reset_timer(self) -> None:
        config = self._current_config()
        if config and config.auto_refresh:
            self.timer.start(config.refresh_minutes * 60 * 1000)
        else:
            self.timer.stop()

    def open_settings(self) -> None:
        dialog = EepromSourceSettingsDialog(self.configs, self.current_root, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.configs = dialog.configs()
            save_source_configs(self.settings, self.configs, dialog.deploy_default.isChecked())
        except OSError as error:
            QMessageBox.warning(self, "배포 기본값 저장", f"사용자 설정은 저장했지만 배포 기본값 파일을 기록하지 못했습니다.\n\n{error}")
        self._reload_combo()
        self._reset_timer()
        self.configsChanged.emit()
        if self.configs:
            self.refresh(True)

    def refresh(self, force: bool = False) -> None:
        config = self._current_config()
        if not config:
            QMessageBox.information(self, "EEPROM 소스", "먼저 EEPROM 소스 항목을 등록하십시오.")
            return
        if self.worker is not None:
            return
        previous = self.results.get(config.id)
        self._checking_only = bool(previous and not force)

        def task(progress):
            if previous and not force:
                progress(0, 0, f"{config.display_name}: 원격 변경 확인 중…")
                revision = source_revision(config, self.current_root)
                if revision == previous.commit:
                    return ("unchanged", previous)
            result = analyze_eeprom_source(config, self.current_root, self.cache_root, progress)
            return ("updated", result)

        worker = _Worker(task)
        self.worker = worker
        worker.signals.progress.connect(self._progress)
        worker.signals.result.connect(self._ready)
        worker.signals.error.connect(self._error)
        worker.signals.finished.connect(self._finished)
        self.progress.setRange(0, 0)
        self.progress.setFormat("GitHub 브랜치 확인 중…")
        self.progress.show()
        self.pool.start(worker)

    def _automatic_refresh(self) -> None:
        if self.isVisible():
            self.refresh(False)

    def _progress(self, current: int, total: int, message: str) -> None:
        self.subtitle.setText(message)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        else:
            self.progress.setRange(0, 0)
        self.progress.setFormat(message)

    def _ready(self, payload: object) -> None:
        state, result = payload
        if state == "unchanged":
            self.subtitle.setText(
                f"{result.config.display_name} · 원격 변경 없음 · {result.commit[:8] or '로컬'} · "
                f"{result.config.refresh_minutes}분 간격"
            )
            return
        self.results[result.config.id] = result
        if self._current_config() and self._current_config().id == result.config.id:
            self._display(result)

    def _error(self, message: str) -> None:
        self.subtitle.setText("EEPROM 소스 동기화 또는 분석 실패")
        QMessageBox.warning(self, "EEPROM 맵 분석", message)

    def _finished(self) -> None:
        self.progress.hide()
        self.worker = None

    def _display(self, result: EepromMapResult) -> None:
        config = result.config
        used_pages = len({
            page for region in result.regions if region.allocated
            for page in range(
                region.address // config.page_size,
                min(config.capacity // config.page_size, (region.address + region.size - 1) // config.page_size + 1),
            )
        })
        total_pages = config.capacity // config.page_size
        self.used_card.value.setText(f"{result.used_bytes:,} B · {result.usage_percent:.1f}%")
        self.free_card.value.setText(f"{max(0, config.capacity - result.used_bytes):,} B")
        self.page_card.value.setText(f"{used_pages} / {total_pages}")
        self.warning_card.value.setText(f"{len(result.warnings)}건")
        self.commit_card.value.setText(result.commit[:8] or "로컬")
        interval = f" · 자동 {config.refresh_minutes}분" if config.auto_refresh else " · 자동 동기화 꺼짐"
        self.subtitle.setText(f"{config.display_name} · {result.source_root} · {result.commit[:8] or '로컬'}{interval}")
        self.canvas.set_result(result)
        self.region_table.setRowCount(0)
        root = Path(result.source_root)
        for region in result.regions:
            row = self.region_table.rowCount()
            self.region_table.insertRow(row)
            try:
                source = str(Path(region.path).relative_to(root))
            except ValueError:
                source = Path(region.path).name
            values = [
                str(region.page), f"0x{region.address:04X}", f"0x{region.end_address:04X}",
                region.name, region.struct_name or "-", f"{region.payload_size:,} B",
                f"{region.size:,} B", f"{max(0, region.size - region.payload_size):,} B",
                region.access, region.status, source, ", ".join(map(str, region.lines)),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, region)
                if region.status == "중복 영역":
                    item.setBackground(QColor(150, 45, 45, 105))
                elif region.status == "용량 초과":
                    item.setBackground(QColor(180, 100, 25, 105))
                self.region_table.setItem(row, column, item)
        self.structure_table.setRowCount(0)
        for structure in result.structures:
            row = self.structure_table.rowCount()
            self.structure_table.insertRow(row)
            try:
                source = str(Path(structure.path).relative_to(root))
            except ValueError:
                source = Path(structure.path).name
            values = [
                structure.name, f"{structure.size:,} B", str(len(structure.fields)),
                "packed" if structure.packed else "compiler alignment", source, str(structure.line),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, structure)
                self.structure_table.setItem(row, column, item)
        warning_lines = [
            f"아이템: {config.display_name}", f"소스: {result.source_root}",
            f"Git commit: {result.commit or '로컬 폴더'}", "",
            "[검토 필요]",
            *(f"- {warning}" for warning in result.warnings), "",
            "[분석 원칙]",
            "- AT24C128: 16,384 bytes, 기본 물리 페이지 64 bytes",
            "- EEPROM_Read/EEPROM_Write 래퍼는 payload와 관계없이 64바이트 물리 페이지를 할당",
            "- 주소/페이지 매크로, 읽기/쓰기 호출, sizeof(구조체)를 교차 연결",
            "- 컴파일 옵션과 외부 typedef를 알 수 없는 구조체 크기는 목록에서 제외하거나 확인 필요로 유지",
        ]
        self.warning_view.setPlainText("\n".join(warning_lines))
        if self.structure_table.rowCount():
            self.structure_table.selectRow(0)

    def _select_page(self, page: int) -> None:
        for row in range(self.region_table.rowCount()):
            region = self.region_table.item(row, 0).data(Qt.UserRole)
            if region.page == page:
                self.region_table.selectRow(row)
                self.region_table.scrollToItem(self.region_table.item(row, 0))
                return

    def _region_selected(self) -> None:
        items = self.region_table.selectedItems()
        if not items:
            return
        region = items[0].data(Qt.UserRole)
        if not region or not region.struct_name:
            return
        for row in range(self.structure_table.rowCount()):
            structure = self.structure_table.item(row, 0).data(Qt.UserRole)
            if structure and structure.name == region.struct_name:
                self.structure_table.selectRow(row)
                break

    def _structure_selected(self) -> None:
        items = self.structure_table.selectedItems()
        if not items:
            self.structure_caption.setText("구조체를 선택하면 원본 C 선언을 표시합니다.")
            self.code_preview.clear()
            return
        structure: StructInfo = items[0].data(Qt.UserRole)
        self.structure_caption.setText(
            f"{structure.name} · {structure.size:,} bytes · {structure.path}:{structure.line}"
        )
        self.code_preview.setPlainText(structure.declaration or "원본 선언을 복원할 수 없습니다.")
