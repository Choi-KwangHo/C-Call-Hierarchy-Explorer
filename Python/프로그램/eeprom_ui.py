from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QStandardPaths, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPainter, QPen, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QFileDialog, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QScrollArea, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from eeprom_map import (
    AT24C128_CAPACITY, AT24C128_PAGE_SIZE, EepromMapResult,
    EepromSourceConfig, StructInfo, analyze_eeprom_source, load_source_configs,
    parse_github_location, save_source_configs, source_catalog_path, source_revision,
)
from eeprom_cache import EepromResultCacheStore
from window_state import apply_dark_title_bar, restore_window_state, save_window_state


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
        self.setMinimumHeight(335)
        self.setMinimumWidth(640)
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
        cell_height = max(17, (self.height() - top - 18) // rows)
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
        detail = "\n".join(
            f"{'◆' if item.actual_usage and item.definition_present else '●' if item.actual_usage else '◇'} "
            f"{item.name}: {item.payload_size}/{item.size} bytes ({item.status})"
            for item in matches
        )
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
        priority = {"free": 0, "defined": 1, "used": 2, "linked": 3, "conflict": 4}
        for region in self.result.regions:
            first = max(0, region.address // self.result.config.page_size)
            last = min(page_count - 1, max(region.address, region.address + region.size - 1) // self.result.config.page_size)
            state = (
                "conflict" if region.conflict or region.out_of_range
                else "linked" if region.actual_usage and region.struct_name
                else "used" if region.actual_usage
                else "defined"
            )
            for page in range(first, last + 1):
                if priority[state] > priority[states[page]]:
                    states[page] = state
        painter.setFont(QFont("Segoe UI", 8))
        for page in range(min(256, page_count)):
            row, column = divmod(page, columns)
            x, y = left + column * width, top + row * height
            color = QColor("#26313A")
            if states[page] == "used":
                color = QColor("#2583C5")
            elif states[page] == "linked":
                color = QColor("#249B78")
            elif states[page] == "defined":
                color = QColor("#52616C")
            elif states[page] == "conflict":
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
        for word in (
            "typedef", "struct", "union", "enum", "const", "volatile", "static", "extern",
            "unsigned", "signed", "char", "short", "int", "long", "float", "double", "void",
            "if", "else", "for", "while", "switch", "case", "return", "sizeof",
        ):
            self.rules.append((re.compile(rf"\b{word}\b"), keyword))
        type_format = QTextCharFormat()
        type_format.setForeground(QColor("#4EC9B0"))
        self.rules.append((re.compile(r"\b(?:u?int(?:8|16|32|64)_t|[us](?:8|16|32|64)|bool|size_t)\b"), type_format))
        number = QTextCharFormat()
        number.setForeground(QColor("#B5CEA8"))
        self.rules.append((re.compile(r"\b(?:0x[0-9A-Fa-f]+|\d+)\b"), number))
        string_format = QTextCharFormat()
        string_format.setForeground(QColor("#CE9178"))
        self.rules.append((re.compile(r'"(?:\\.|[^"\\])*"'), string_format))
        preprocessor = QTextCharFormat()
        preprocessor.setForeground(QColor("#C586C0"))
        self.rules.append((re.compile(r"^\s*#.*$"), preprocessor))
        self.comment_format = QTextCharFormat()
        self.comment_format.setForeground(QColor("#6A9955"))
        self.rules.append((re.compile(r"//.*$"), self.comment_format))

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        for pattern, text_format in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), text_format)
        self.setCurrentBlockState(0)
        start = 0 if self.previousBlockState() == 1 else text.find("/*")
        while start >= 0:
            end = text.find("*/", start + 2)
            if end < 0:
                self.setCurrentBlockState(1)
                length = len(text) - start
            else:
                length = end - start + 2
            self.setFormat(start, length, self.comment_format)
            if end < 0:
                break
            start = text.find("/*", start + length)


class EepromSourceSettingsDialog(QDialog):
    HEADERS = ["아이템 표시명", "소스 유형", "GitHub 주소 / 로컬 폴더", "브랜치", "용량", "페이지", "자동", "주기(분)"]

    def __init__(
        self, configs: list[EepromSourceConfig], current_root: str, parent=None,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)
        self.current_root = current_root
        self.embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.Widget)
        self.setWindowTitle("EEPROM 소스 및 자동 동기화 설정")
        if not embedded:
            apply_dark_title_bar(self)
            self.resize(1280, 560)
            self.setMinimumSize(900, 430)
        self.setStyleSheet("""
            QDialog { background:#181C20; color:#E6EDF3; }
            QTableWidget { background:#11161B; alternate-background-color:#171D23; color:#DCE5EC; gridline-color:#33404A; }
            QHeaderView::section { background:#27313A; color:#F2F6F8; padding:7px; border:0; border-right:1px solid #3C4A54; }
            QLineEdit, QSpinBox { background:#10151A; color:#E6EDF3; border:1px solid #46545E; padding:5px; }
            QComboBox { background:#10151A; color:#E6EDF3; border:1px solid #46545E; padding:5px; }
            QComboBox QAbstractItemView { background:#11161B; color:#E6EDF3; selection-background-color:#245E82; selection-color:#FFFFFF; }
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
            "GitHub 저장소 또는 이 PC의 로컬 펌웨어 폴더를 등록합니다. 저장소/폴더 아래의 모든 .c/.h 파일을 자동 검색하며 변경된 경우에만 다시 분석합니다. 로컬 폴더는 사용자 설정에만 저장됩니다."
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
            self._append(config, select=False)
        layout.addWidget(self.table, 1)
        controls = QHBoxLayout()
        add = QPushButton("＋ GitHub 항목 추가")
        add.clicked.connect(lambda: self._append(EepromSourceConfig.create("새 EEPROM 항목"), select=True))
        add_local = QPushButton("＋ 로컬 폴더 추가…")
        add_local.clicked.connect(self._add_local_folder)
        remove = QPushButton("선택 항목 삭제")
        remove.clicked.connect(self._remove_selected)
        controls.addWidget(add)
        controls.addWidget(add_local)
        controls.addWidget(remove)
        controls.addStretch(1)
        self.deploy_default = QCheckBox("현재 목록을 다음 배포 기본값에도 반영")
        self.deploy_default.setChecked(source_catalog_path() is not None)
        self.deploy_default.setEnabled(source_catalog_path() is not None)
        self.deploy_default.setToolTip("개발 소스에서 실행할 때 eeprom_sources.json에 기록되어 다음 release.bat 배포에 포함됩니다.")
        controls.addWidget(self.deploy_default)
        layout.addLayout(controls)
        if not embedded:
            buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
            buttons.button(QDialogButtonBox.Ok).setText("적용")
            buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
            buttons.button(QDialogButtonBox.Cancel).setText("취소")
            buttons.accepted.connect(self._validate_accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

    def _append(self, config: EepromSourceConfig, select: bool = True) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            config.display_name, "로컬 폴더" if config.is_local else "GitHub",
            config.repository_url, "-" if config.is_local else config.branch,
            str(config.capacity), str(config.page_size),
            "", str(config.refresh_minutes),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.UserRole, config.id)
            if column == 1:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setForeground(QColor("#69C0F0" if not config.is_local else "#8BD49C"))
            if column == 3 and config.is_local:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if column == 6:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if config.auto_refresh else Qt.Unchecked)
            self.table.setItem(row, column, item)
        if select:
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, 0))

    def _add_local_folder(self) -> None:
        start = self.current_root if Path(self.current_root).is_dir() else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "EEPROM 분석 로컬 폴더 선택", start)
        if not folder:
            return
        root = Path(folder)
        self._append(EepromSourceConfig.create(
            root.name or "로컬 EEPROM 프로젝트",
            source_type="local",
            repository_url=str(root.resolve()),
            branch="main",
        ))

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
                display_name=text(0), source_type="local" if text(1) == "로컬 폴더" else "github",
                repository_url=text(2), branch="main" if text(1) == "로컬 폴더" else (text(3) or "main"),
                subdirectory="", capacity=int(text(4)), page_size=int(text(5)),
                auto_refresh=self.table.item(row, 6).checkState() == Qt.Checked,
                refresh_minutes=int(text(7)),
            ))
        return values

    def selected_config_id(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.UserRole) or "") if item else ""

    def validated_configs(self) -> list[EepromSourceConfig]:
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
            if config.is_local and not Path(config.repository_url).is_dir():
                raise ValueError(f"{config.display_name}: 로컬 폴더를 찾을 수 없습니다.\n{config.repository_url}")
            parse_github_location(config.repository_url, config.branch)
        return configs

    def _validate_accept(self) -> None:
        try:
            self.validated_configs()
        except (ValueError, TypeError) as error:
            QMessageBox.warning(self, "EEPROM 설정 확인", str(error))
            return
        self.accept()


class EepromMapDialog(QDialog):
    configsChanged = Signal()
    settingsRequested = Signal()

    def __init__(self, settings: QSettings, current_root: str, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.current_root = current_root
        self.configs = load_source_configs(settings, current_root)
        self.results: dict[str, EepromMapResult] = {}
        self.result_cache = EepromResultCacheStore()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.worker: _Worker | None = None
        self._checking_only = False
        self.cache_root = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "eeprom-repositories"
        self.setWindowTitle("AT24C128 EEPROM 메모리 맵")
        apply_dark_title_bar(self)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)
        self.resize(1540, 920)
        self.setMinimumSize(1100, 700)
        self._build_ui()
        self._restore_splitter_sizes()
        self._startup_window_mode = restore_window_state(
            self, self.settings, "eepromMapWindow", (1540, 920)
        )
        QTimer.singleShot(0, self._apply_saved_window_mode)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._automatic_refresh)
        for config in self.configs:
            cached = self.result_cache.load(config)
            if cached is not None:
                self.results[config.id] = cached
        preferred = str(self.settings.value("eepromMapWindow/currentSourceId", "") or "")
        self._reload_combo(preferred)
        self._reset_timer()
        current = self._current_config()
        if current and current.id in self.results:
            self._display(self.results[current.id])
        QTimer.singleShot(0, lambda: self.refresh(False))

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
            QComboBox QAbstractItemView { background:#10161B; color:#E8EEF2; selection-background-color:#245E82; selection-color:#FFFFFF; border:1px solid #43515C; }
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
            QSplitter::handle { background:#26323B; }
            QSplitter::handle:hover { background:#1683C5; }
            QScrollBar:vertical { background:#11171C; width:13px; margin:0; }
            QScrollBar::handle:vertical { background:#485866; min-height:30px; border-radius:5px; margin:2px; }
            QScrollBar::handle:vertical:hover { background:#617587; }
            QScrollBar:horizontal { background:#11171C; height:13px; margin:0; }
            QScrollBar::handle:horizontal { background:#485866; min-width:30px; border-radius:5px; margin:2px; }
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
        configure = QPushButton("설정…")
        configure.clicked.connect(self.open_settings)
        top.addWidget(configure)
        self.fullscreen_button = QPushButton("전체 화면")
        self.fullscreen_button.setShortcut("F11")
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        top.addWidget(self.fullscreen_button)
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
        self.content_splitter = QSplitter(Qt.Horizontal)
        self.content_splitter.setHandleWidth(7)
        self.content_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.content_splitter, 1)

        self.left_splitter = QSplitter(Qt.Vertical)
        self.left_splitter.setHandleWidth(7)
        self.left_splitter.setChildrenCollapsible(False)
        map_panel = QFrame()
        map_panel.setFrameShape(QFrame.StyledPanel)
        map_layout = QVBoxLayout(map_panel)
        map_layout.setContentsMargins(8, 8, 8, 8)
        map_title = QLabel("EEPROM 물리 페이지 맵")
        map_title.setStyleSheet("font-size:14px; font-weight:600; color:#F2F6F8;")
        map_layout.addWidget(map_title)
        legend = QLabel("■ 정의만    ■ 실사용    ■ 실사용 + 구조체 연결    ■ 확정 충돌    ■ 선택 페이지")
        legend.setStyleSheet("color:#AAB6C0;")
        map_layout.addWidget(legend)
        self.canvas = MemoryMapCanvas()
        self.canvas.pageSelected.connect(self._select_page)
        self.map_scroll = QScrollArea()
        self.map_scroll.setWidgetResizable(True)
        self.map_scroll.setFrameShape(QFrame.NoFrame)
        self.map_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.map_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.map_scroll.setWidget(self.canvas)
        map_layout.addWidget(self.map_scroll, 1)
        self.left_splitter.addWidget(map_panel)

        region_panel = QFrame()
        region_layout = QVBoxLayout(region_panel)
        region_layout.setContentsMargins(8, 8, 8, 8)
        region_title = QLabel("메모리 할당 및 접근 목록")
        region_title.setStyleSheet("font-size:14px; font-weight:600; color:#F2F6F8;")
        region_layout.addWidget(region_title)
        self.region_table = QTableWidget(0, 12)
        self.region_table.setHorizontalHeaderLabels([
            "페이지", "시작 주소", "끝 주소", "영역/기호", "구조체", "Payload", "물리 할당",
            "여유", "접근", "상태", "소스", "행",
        ])
        self.region_table.setAlternatingRowColors(True)
        self.region_table.verticalHeader().hide()
        self.region_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.region_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.region_table.itemSelectionChanged.connect(self._region_selected)
        self.region_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.region_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.region_table.horizontalHeader().setSectionResizeMode(10, QHeaderView.Stretch)
        region_layout.addWidget(self.region_table, 1)
        self.left_splitter.addWidget(region_panel)
        self.left_splitter.setStretchFactor(0, 1)
        self.left_splitter.setStretchFactor(1, 1)
        self.left_splitter.setSizes([410, 360])

        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.setHandleWidth(7)
        self.right_splitter.setChildrenCollapsible(False)
        structure_panel = QFrame()
        structure_layout = QVBoxLayout(structure_panel)
        structure_layout.setContentsMargins(8, 8, 8, 8)
        structure_title = QLabel("감지된 저장 구조체")
        structure_title.setStyleSheet("font-size:14px; font-weight:600; color:#F2F6F8;")
        structure_layout.addWidget(structure_title)
        self.structure_table = QTableWidget(0, 6)
        self.structure_table.setHorizontalHeaderLabels(["구조체/Union", "크기", "필드 수", "정렬", "파일", "행"])
        self.structure_table.setAlternatingRowColors(True)
        self.structure_table.verticalHeader().hide()
        self.structure_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.structure_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.structure_table.itemSelectionChanged.connect(self._structure_selected)
        self.structure_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.structure_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        structure_layout.addWidget(self.structure_table, 1)
        self.right_splitter.addWidget(structure_panel)
        preview_frame = QFrame()
        preview_layout = QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        self.structure_caption = QLabel("구조체를 선택하면 원본 C 선언을 표시합니다.")
        self.structure_caption.setStyleSheet("font-weight:600; color:#CFE8F7; padding:3px;")
        preview_layout.addWidget(self.structure_caption)
        preview_tabs = QTabWidget()
        self.code_preview = QPlainTextEdit()
        self.code_preview.setReadOnly(True)
        self.code_preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.code_preview.setFont(QFont("Cascadia Mono", 10))
        self.highlighter = CDeclarationHighlighter(self.code_preview.document())
        preview_tabs.addTab(self.code_preview, "C 구조체 선언")
        self.warning_view = QPlainTextEdit()
        self.warning_view.setReadOnly(True)
        self.warning_view.setFont(QFont("Cascadia Mono", 9))
        preview_tabs.addTab(self.warning_view, "검토 및 분석 근거")
        preview_layout.addWidget(preview_tabs, 1)
        self.right_splitter.addWidget(preview_frame)
        self.right_splitter.setStretchFactor(0, 1)
        self.right_splitter.setStretchFactor(1, 2)
        self.right_splitter.setSizes([290, 480])

        self.content_splitter.addWidget(self.left_splitter)
        self.content_splitter.addWidget(self.right_splitter)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setSizes([760, 700])

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setText("전체 화면")
        else:
            self.showFullScreen()
            self.fullscreen_button.setText("전체 화면 종료")

    def _apply_saved_window_mode(self) -> None:
        if self._startup_window_mode == "fullscreen":
            self.showFullScreen()
            self.fullscreen_button.setText("전체 화면 종료")
        elif self._startup_window_mode == "maximized":
            self.showMaximized()

    def _restore_splitter_sizes(self) -> None:
        for splitter, key in (
            (self.content_splitter, "eepromMapWindow/contentSizes"),
            (self.left_splitter, "eepromMapWindow/leftSizes"),
            (self.right_splitter, "eepromMapWindow/rightSizes"),
        ):
            raw = self.settings.value(key, [])
            values = [raw] if isinstance(raw, (int, str)) else list(raw or [])
            try:
                sizes = [int(value) for value in values]
            except (TypeError, ValueError):
                continue
            if len(sizes) == splitter.count() and all(value >= 0 for value in sizes):
                splitter.setSizes(sizes)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.timer.stop()
        self.settings.setValue("eepromMapWindow/contentSizes", self.content_splitter.sizes())
        self.settings.setValue("eepromMapWindow/leftSizes", self.left_splitter.sizes())
        self.settings.setValue("eepromMapWindow/rightSizes", self.right_splitter.sizes())
        save_window_state(self, self.settings, "eepromMapWindow")
        super().closeEvent(event)

    def _reload_combo(self, preferred_id: str = "") -> None:
        current = preferred_id or self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for config in self.configs:
            source_label = "로컬" if config.is_local else "GitHub"
            self.source_combo.addItem(f"{config.display_name}  ·  {source_label}", config.id)
        index = self.source_combo.findData(current)
        self.source_combo.setCurrentIndex(index if index >= 0 else (0 if self.configs else -1))
        self.source_combo.blockSignals(False)

    def _current_config(self) -> EepromSourceConfig | None:
        identity = self.source_combo.currentData()
        return next((item for item in self.configs if item.id == identity), None)

    def _source_changed(self) -> None:
        config = self._current_config()
        if config:
            self.settings.setValue("eepromMapWindow/currentSourceId", config.id)
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
        self.settingsRequested.emit()

    def reload_configs(self, preferred_id: str = "") -> None:
        self.configs = load_source_configs(self.settings, self.current_root)
        for config in self.configs:
            cached = self.result_cache.load(config)
            if cached is not None:
                self.results[config.id] = cached
        self._reload_combo(preferred_id)
        self._reset_timer()
        current = self._current_config()
        if current and current.id in self.results:
            self._display(self.results[current.id])
        if current:
            self.refresh(False)

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
        try:
            self.result_cache.save(result)
        except Exception:  # noqa: BLE001 - cache failure must never block a completed analysis
            pass
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
            usage_mark = (
                "◆" if region.actual_usage and region.definition_present
                else "●" if region.actual_usage
                else "◇"
            )
            display_status = region.status
            if not region.conflict and not region.out_of_range:
                if region.actual_usage and region.definition_present:
                    display_status = "정의 + 실사용"
                elif region.actual_usage:
                    display_status = "실사용 (정의 미연결)"
                else:
                    display_status = "정의만 존재"
            values = [
                f"{usage_mark} {region.page}", f"0x{region.address:04X}", f"0x{region.end_address:04X}",
                region.name, region.struct_name or "-", f"{region.payload_size:,} B",
                f"{region.size:,} B", f"{max(0, region.size - region.payload_size):,} B",
                region.access, display_status, source, ", ".join(map(str, region.lines)),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, region)
                item.setToolTip(
                    "◆ 정의와 실제 API 사용이 모두 확인됨\n"
                    "● 실제 API 사용 확인, 대응 정의 미연결\n"
                    "◇ 정의/예약만 확인됨"
                )
                if region.conflict:
                    item.setBackground(QColor(150, 45, 45, 105))
                elif region.out_of_range:
                    item.setBackground(QColor(180, 100, 25, 105))
                elif region.actual_usage and region.struct_name:
                    item.setBackground(QColor(35, 137, 106, 55))
                elif region.actual_usage:
                    item.setBackground(QColor(37, 131, 197, 45))
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
        self.region_table.clearSelection()
        self._show_no_structure(f"페이지 {page}: 정의된 구조체 없음")

    def _region_selected(self) -> None:
        items = self.region_table.selectedItems()
        if not items:
            return
        region = items[0].data(Qt.UserRole)
        if not region or not region.struct_name:
            name = region.name if region else "선택 영역"
            self._show_no_structure(f"{name}: 정의된 구조체 없음")
            return
        for row in range(self.structure_table.rowCount()):
            structure = self.structure_table.item(row, 0).data(Qt.UserRole)
            if structure and structure.name == region.struct_name:
                self.structure_table.selectRow(row)
                return
        self._show_no_structure(f"{region.struct_name}: 구조체 선언을 찾을 수 없음")

    def _show_no_structure(self, message: str = "정의된 구조체 없음") -> None:
        self.structure_table.blockSignals(True)
        self.structure_table.clearSelection()
        self.structure_table.blockSignals(False)
        self.structure_caption.setText(message)
        self.code_preview.setPlainText(
            "정의된 구조체 없음\n\n"
            "이 EEPROM 영역에서는 주소 또는 읽기/쓰기 접근은 확인되었지만, "
            "버퍼와 연결되는 C struct/union 선언을 확정하지 못했습니다."
        )

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
