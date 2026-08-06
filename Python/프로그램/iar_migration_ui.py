from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from iar_project_migrator import (
    IarWorkspaceInfo, MigrationCancelled, MigrationError, MigrationEvent, MigrationOptions, MigrationResult,
    format_event, inspect_iar_workspace, migrate_iar_project, preview_iar_migration,
)
from iar_settings_backup import (
    IarSettingsError, create_settings_backup, default_backup_root,
    discover_settings_files, load_settings_backup,
)
from window_state import apply_dark_title_bar, restore_window_state, save_window_state


class _Signals(QObject):
    event = Signal(object)
    ready = Signal(object)
    error = Signal(object)
    finished = Signal()


class _Worker(QRunnable):
    def __init__(self, options: MigrationOptions, preview: bool) -> None:
        super().__init__()
        self.options = options
        self.preview = preview
        self.signals = _Signals()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            callback = lambda event: self._event(event)
            operation = preview_iar_migration if self.preview else migrate_iar_project
            result = operation(self.options, callback, self._cancelled.is_set)
            self.signals.ready.emit(result)
        except BaseException as error:
            self.signals.error.emit(error)
        finally:
            self.signals.finished.emit()

    def _event(self, event: MigrationEvent) -> None:
        print(format_event(event), flush=True)
        self.signals.event.emit(event)


class IarProjectMigrationDialog(QDialog):
    MAX_VISIBLE_EVENTS = 10_000

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.worker: _Worker | None = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.last_result: MigrationResult | None = None
        self.workspace_info: IarWorkspaceInfo | None = None
        self.live_watch_backup_dir = ""
        self.ctrace_backup_dir = ""
        self.setWindowTitle("IAR 프로젝트 복제 및 이름 변경")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)
        self.resize(1180, 780)
        self.setMinimumSize(900, 620)
        self._build_ui()
        self._restore_fields()
        self._startup_mode = restore_window_state(self, settings, "iarMigrationWindow", (1180, 780))
        apply_dark_title_bar(self)
        if self._startup_mode == "maximized":
            self.showMaximized()

    def _build_ui(self) -> None:
        self.setStyleSheet("""
            QDialog { background:#151A1F; color:#E5EDF3; }
            QLabel#title { color:#FFFFFF; font-size:22px; font-weight:600; }
            QLabel#subtitle { color:#9FADB8; }
            QFrame#section { background:#1B2228; border:1px solid #34414B; border-radius:5px; }
            QLabel#summaryValue { color:#F4F8FA; font-size:15px; font-weight:600; }
            QTableWidget { background:#10161B; alternate-background-color:#161E24; gridline-color:#303C45; }
            QPlainTextEdit { background:#0D1318; color:#C9D5DC; border:1px solid #34414A; }
            QProgressBar#slimProgress { background:#273139; border:0; min-height:4px; max-height:4px; }
            QProgressBar#slimProgress::chunk { background:#3F7D9E; border-radius:2px; }
            QLabel#status { color:#91A1AC; font-size:11px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(8)

        title = QLabel("IAR 프로젝트 복제 및 이름 변경")
        title.setObjectName("title")
        subtitle = QLabel(
            "EWARM 프로젝트를 새 위치에 안전하게 복제하고 파일명·내부 경로·프로젝트 표시 이름을 일괄 동기화합니다."
        )
        subtitle.setObjectName("subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        form_frame = QFrame()
        form_frame.setObjectName("section")
        form_layout = QFormLayout(form_frame)
        form_layout.setContentsMargins(14, 12, 14, 12)
        form_layout.setHorizontalSpacing(14)
        self.workspace_edit = self._path_row(form_layout, "기존 IAR 워크스페이스(.eww)", self._browse_workspace)
        self.workspace_edit.setPlaceholderText(".eww를 선택하면 기존 프로젝트 정보가 자동 입력됩니다.")
        self.new_first_path_edit = self._path_row(form_layout, "새 1차 폴더 경로", self._browse_new_first_path)
        self.new_first_path_edit.setPlaceholderText("선택한 폴더 자체가 새 프로젝트의 1차 폴더입니다.")
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("워크스페이스 분석 후 기존 이름이 자동 입력됩니다.")
        form_layout.addRow("새 프로젝트 이름", self.new_name)
        self.new_second_folder_edit = QLineEdit()
        self.new_second_folder_edit.setPlaceholderText("워크스페이스 분석 후 기존 2차 폴더명이 자동 입력됩니다.")
        form_layout.addRow("새 2차 폴더명", self.new_second_folder_edit)
        self.detected_summary = QLabel("기존 .eww 파일을 선택하면 원본 경로와 프로젝트 정보를 자동 분석합니다.")
        self.detected_summary.setWordWrap(True)
        self.detected_summary.setObjectName("subtitle")
        form_layout.addRow("자동 감지 정보", self.detected_summary)
        self.final_path_label = QLabel("-")
        self.final_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form_layout.addRow("최종 생성 경로", self.final_path_label)
        self.workspace_edit.textChanged.connect(self._workspace_path_edited)
        for edit in (self.new_first_path_edit, self.new_name, self.new_second_folder_edit):
            edit.textChanged.connect(self._update_action_state)
        layout.addWidget(form_frame)

        settings_frame = QFrame()
        settings_frame.setObjectName("section")
        settings_layout = QVBoxLayout(settings_frame)
        settings_layout.setContentsMargins(14, 10, 14, 10)
        settings_layout.setSpacing(7)
        settings_title = QLabel("IAR 디버그 설정")
        settings_title.setStyleSheet("font-weight:600; color:#F2F6F8;")
        settings_help = QLabel(
            "선택한 설정은 새 프로젝트의 EWARM/settings에 복원됩니다. "
            "Live Watch에서 대상 코드에 존재하지 않는 변수만 제외합니다."
        )
        settings_help.setObjectName("subtitle")
        settings_help.setWordWrap(True)
        settings_layout.addWidget(settings_title)
        settings_layout.addWidget(settings_help)
        self.live_watch_check, self.live_watch_source = self._settings_row(
            settings_layout, "Live Watch 복사", self._backup_live_watch, self._load_live_watch,
            self._clear_live_watch_backup,
        )
        self.ctrace_check, self.ctrace_source = self._settings_row(
            settings_layout, "C-Trace 복사", self._backup_ctrace, self._load_ctrace,
            self._clear_ctrace_backup,
        )
        layout.addWidget(settings_frame)

        command_row = QHBoxLayout()
        self.preview_button = QPushButton("사전 검사")
        self.preview_button.clicked.connect(lambda: self._start(True))
        command_row.addWidget(self.preview_button)
        self.run_button = QPushButton("안전 복제 시작")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(lambda: self._start(False))
        command_row.addWidget(self.run_button)
        self.cancel_button = QPushButton("작업 취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        command_row.addWidget(self.cancel_button)
        self.open_button = QPushButton("완료 폴더 열기")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_target)
        command_row.addWidget(self.open_button)
        command_row.addStretch(1)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.close)
        command_row.addWidget(close_button)
        layout.addLayout(command_row)

        summary = QHBoxLayout()
        self.summary_labels: dict[str, QLabel] = {}
        for key, caption in (
            ("files", "복사 파일"), ("renamed", "이름 변경"),
            ("modified", "내부 수정"), ("excluded", "제외 항목"),
        ):
            frame = QFrame()
            frame.setObjectName("section")
            box = QVBoxLayout(frame)
            box.setContentsMargins(10, 7, 10, 7)
            cap = QLabel(caption)
            cap.setStyleSheet("color:#9EACB7; font-size:11px;")
            value = QLabel("0")
            value.setObjectName("summaryValue")
            self.summary_labels[key] = value
            box.addWidget(cap)
            box.addWidget(value)
            summary.addWidget(frame, 1)
        layout.addLayout(summary)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["작업", "원본", "대상", "세부 내용"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        splitter.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log.setFont(QFont("Cascadia Mono", 9))
        self.log.document().setMaximumBlockCount(self.MAX_VISIBLE_EVENTS)
        splitter.addWidget(self.log)
        splitter.setSizes([310, 170])
        layout.addWidget(splitter, 1)

        status_frame = QFrame()
        status_frame.setFixedHeight(23)
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(2, 2, 2, 2)
        status_layout.setSpacing(10)
        self.status = QLabel("대기 중")
        self.status.setObjectName("status")
        status_layout.addWidget(self.status, 2)
        self.progress = QProgressBar()
        self.progress.setObjectName("slimProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        status_layout.addWidget(self.progress, 3)
        layout.addWidget(status_frame)

    def _path_row(self, layout: QFormLayout, label: str, callback) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit()
        row.addWidget(edit, 1)
        button = QPushButton("찾아보기…")
        button.clicked.connect(callback)
        row.addWidget(button)
        layout.addRow(label, row)
        return edit

    def _settings_row(self, layout: QVBoxLayout, caption: str, backup, load, clear):
        row = QHBoxLayout()
        check = QCheckBox(caption)
        row.addWidget(check)
        source = QLabel("원본 프로젝트 설정 자동 감지")
        source.setObjectName("subtitle")
        source.setToolTip(source.text())
        row.addWidget(source, 1)
        backup_button = QPushButton("백업")
        backup_button.clicked.connect(backup)
        row.addWidget(backup_button)
        load_button = QPushButton("불러오기…")
        load_button.clicked.connect(load)
        row.addWidget(load_button)
        clear_button = QPushButton("원본 사용")
        clear_button.clicked.connect(clear)
        row.addWidget(clear_button)
        layout.addLayout(row)
        return check, source

    def _browse_workspace(self) -> None:
        start = self.workspace_edit.text().strip() or str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self, "기존 IAR 워크스페이스 열기", start, "IAR Workspace (*.eww)"
        )
        if not selected:
            return
        self._load_workspace(selected, notify=True)

    def _load_workspace(self, selected: str, notify: bool = True) -> bool:
        try:
            info = inspect_iar_workspace(selected)
        except Exception as error:  # noqa: BLE001 - validation is shown to the user
            self.workspace_info = None
            self._update_action_state()
            if notify:
                QMessageBox.warning(self, "IAR 워크스페이스 확인", str(error))
            return False
        self.workspace_info = info
        self.workspace_edit.setText(info.workspace_file)
        old_first_path = Path(info.source_root).parent
        self.new_first_path_edit.setText(str(old_first_path))
        self.new_name.setText(info.project_keyword)
        self.new_second_folder_edit.setText(info.second_folder)
        project_count = len(info.referenced_projects)
        cubemx_count = len(list(Path(info.source_root).rglob("*.ioc")))
        self.detected_summary.setText(
            f"원본 1차 폴더: {old_first_path}\n"
            f"원본 2차 폴더: {info.second_folder} · 프로젝트: {info.project_keyword} · "
            f"IAR 참조 {project_count}개 · CubeMX .ioc {cubemx_count}개 · {info.encoding}"
        )
        detected = discover_settings_files(info.workspace_file)
        live_state = "감지됨" if ".dbgdt" in detected else "없음"
        trace_count = sum(1 for extension in (".crun", ".dnx", ".dbgdt", ".wsdt") if extension in detected)
        if not self.live_watch_backup_dir:
            self.live_watch_source.setText(f"원본 설정 · {live_state}")
            self.live_watch_source.setToolTip(str(detected.get(".dbgdt", "Live Watch 설정 없음")))
        if not self.ctrace_backup_dir:
            self.ctrace_source.setText(f"원본 설정 · {trace_count}/4 파일 감지")
            self.ctrace_source.setToolTip("\n".join(str(path) for path in detected.values()))
        self.status.setText(
            "워크스페이스 자동 인식 완료 · 새 1차 폴더 경로를 다른 위치로 지정하십시오."
        )
        self._update_action_state()
        return True

    def _require_workspace(self) -> IarWorkspaceInfo | None:
        if self.workspace_info is None:
            QMessageBox.warning(self, "IAR 설정", "기존 IAR 워크스페이스(.eww)를 먼저 선택하십시오.")
            return None
        return self.workspace_info

    def _create_backup(self, category: str) -> None:
        info = self._require_workspace()
        if info is None:
            return
        try:
            destination = create_settings_backup(info.workspace_file, info.project_keyword, category)
        except (IarSettingsError, OSError) as error:
            QMessageBox.warning(self, "IAR 설정 백업", str(error))
            return
        label = "Live Watch" if category == "live_watch" else "C-Trace"
        QMessageBox.information(
            self, "IAR 설정 백업 완료",
            f"{label} 설정을 프로젝트별 응용 프로그램 데이터 폴더에 백업했습니다.\n\n{destination}",
        )

    def _load_backup(self, category: str) -> None:
        info = self.workspace_info
        project = info.project_keyword if info else ""
        start = default_backup_root() / project if project else default_backup_root()
        if not start.exists():
            start = default_backup_root()
        selected = QFileDialog.getExistingDirectory(
            self, "IAR 설정 백업 폴더 선택", str(start)
        )
        if not selected:
            return
        try:
            bundle = load_settings_backup(selected, category)
        except (IarSettingsError, OSError) as error:
            QMessageBox.warning(self, "IAR 설정 불러오기", str(error))
            return
        if category == "live_watch":
            self.live_watch_backup_dir = bundle.root
            self.live_watch_check.setChecked(True)
            label = self.live_watch_source
        else:
            self.ctrace_backup_dir = bundle.root
            self.ctrace_check.setChecked(True)
            label = self.ctrace_source
        label.setText(f"백업 불러옴 · {Path(bundle.root).name}")
        label.setToolTip(bundle.manifest_path or bundle.root)
        self._update_action_state()

    def _backup_live_watch(self) -> None:
        self._create_backup("live_watch")

    def _backup_ctrace(self) -> None:
        self._create_backup("ctrace")

    def _load_live_watch(self) -> None:
        self._load_backup("live_watch")

    def _load_ctrace(self) -> None:
        self._load_backup("ctrace")

    def _clear_live_watch_backup(self) -> None:
        self.live_watch_backup_dir = ""
        self.live_watch_source.setText("원본 프로젝트 설정 자동 감지")
        if self.workspace_info:
            self._load_workspace(self.workspace_info.workspace_file, notify=False)

    def _clear_ctrace_backup(self) -> None:
        self.ctrace_backup_dir = ""
        self.ctrace_source.setText("원본 프로젝트 설정 자동 감지")
        if self.workspace_info:
            self._load_workspace(self.workspace_info.workspace_file, notify=False)

    def _workspace_path_edited(self) -> None:
        if self.workspace_info and Path(self.workspace_edit.text().strip()) != Path(self.workspace_info.workspace_file):
            self.workspace_info = None
            self.detected_summary.setText("변경된 .eww 경로를 찾아보기로 다시 선택하십시오.")
        self._update_action_state()

    def _browse_new_first_path(self) -> None:
        current = self.new_first_path_edit.text().strip()
        if current:
            start = current
        elif self.workspace_info:
            start = str(Path(self.workspace_info.source_root).parent)
        else:
            start = str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self, "새 프로젝트 1차 폴더 선택 (선택 폴더 자체가 1차 폴더)", start
        )
        if selected:
            self.new_first_path_edit.setText(selected)

    def _options(self) -> MigrationOptions:
        if self.workspace_info is None:
            raise MigrationError("기존 IAR 워크스페이스(.eww)를 먼저 선택하십시오.")
        invalid = set('<>:"/\\|?*')
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        for label, value in (("새 2차 폴더", self.new_second_folder_edit.text().strip()),):
            if not value or value in {".", ".."} or any(character in invalid for character in value):
                raise MigrationError(f"{label} 이름이 올바르지 않습니다: {value or '(비어 있음)'}")
            if value.rstrip(". ").upper() in reserved or value != value.rstrip(". "):
                raise MigrationError(f"Windows에서 사용할 수 없는 {label} 이름입니다: {value}")
        keyword = self.new_name.text().strip()
        if not keyword or any(character in invalid for character in keyword):
            raise MigrationError("새 프로젝트 핵심 이름에 파일명으로 사용할 수 없는 문자가 포함되어 있습니다.")
        first_path_text = self.new_first_path_edit.text().strip()
        if not first_path_text:
            raise MigrationError("새 1차 폴더 경로를 지정하십시오.")
        first_path = Path(first_path_text).expanduser().resolve(strict=False)
        old_first_path = Path(self.workspace_info.source_root).parent.resolve(strict=False)
        if str(first_path).casefold() == str(old_first_path).casefold():
            raise MigrationError("원본 보존을 위해 새 1차 폴더 경로는 기존 1차 폴더와 달라야 합니다.")
        second = self.new_second_folder_edit.text().strip()
        target_root = first_path / second
        return MigrationOptions(
            source_root=self.workspace_info.source_root,
            target_root=str(target_root),
            old_keyword=self.workspace_info.project_keyword,
            new_keyword=keyword,
            old_embedded_path=f"{self.workspace_info.first_folder}\\{self.workspace_info.second_folder}",
            new_embedded_path=f"{first_path.name}\\{second}",
            replace_source_text=True,
            rename_directories=True,
            source_workspace=self.workspace_info.workspace_file,
            copy_live_watch=self.live_watch_check.isChecked(),
            copy_ctrace=self.ctrace_check.isChecked(),
            live_watch_backup_dir=self.live_watch_backup_dir,
            ctrace_backup_dir=self.ctrace_backup_dir,
        )

    def _update_action_state(self) -> None:
        target_text = "-"
        valid = False
        reason = "기존 IAR 워크스페이스(.eww)를 선택하십시오."
        if self.workspace_info is not None:
            first_text = self.new_first_path_edit.text().strip()
            second = self.new_second_folder_edit.text().strip()
            if first_text and second:
                target_text = str(Path(first_text).expanduser() / second)
            try:
                self._options()
            except (MigrationError, OSError) as error:
                reason = str(error)
            else:
                valid = True
                reason = "복제 준비 완료 · 사전 검사 후 안전 복제를 시작하십시오."
        self.final_path_label.setText(target_text)
        if self.worker is None:
            self.preview_button.setEnabled(valid)
            self.run_button.setEnabled(valid)
            if not self.last_result:
                self.status.setText(reason)

    def _start(self, preview: bool) -> None:
        if self.worker is not None:
            return
        try:
            options = self._options()
        except MigrationError as error:
            QMessageBox.warning(self, "IAR 프로젝트 설정 확인", str(error))
            return
        if not preview:
            answer = QMessageBox.question(
                self, "IAR 프로젝트 안전 복제",
                "사전 검사를 마쳤다면 복제를 시작합니다.\n\n"
                "원본은 변경하지 않습니다. 대상 폴더가 이미 있으면 기존 파일을 보존하고,\n"
                "같은 상대 경로의 파일이 있을 때는 덮어쓰지 않고 작업을 중단합니다.\n"
                "CubeMX .ioc는 프로젝트 식별 항목만 변경하고 하드웨어 설정을 검증합니다.\n"
                "선택한 Live Watch/C-Trace 설정은 프로젝트명과 경로를 동기화해 복원합니다.\n"
                "계속하시겠습니까?",
            )
            if answer != QMessageBox.Yes:
                return
        self._save_fields()
        self.table.setRowCount(0)
        self.log.clear()
        self.last_result = None
        self.open_button.setEnabled(False)
        self._set_running(True, "사전 검사 중…" if preview else "프로젝트 안전 복제 중…")
        worker = _Worker(options, preview)
        self.worker = worker
        worker.signals.event.connect(self._event)
        worker.signals.ready.connect(lambda result: self._ready(result, preview))
        worker.signals.error.connect(self._error)
        worker.signals.finished.connect(self._finished)
        self.pool.start(worker)

    def _event(self, event: MigrationEvent) -> None:
        text = format_event(event)
        self.log.appendPlainText(text)
        self.status.setText(text[:150])
        row = self.table.rowCount()
        if row >= self.MAX_VISIBLE_EVENTS:
            if row == self.MAX_VISIBLE_EVENTS:
                self.status.setText("표시는 10,000건으로 제한하며 전체 작업은 계속 진행합니다. 콘솔 로그를 확인하십시오.")
            return
        self.table.insertRow(row)
        for column, value in enumerate((event.action, event.source, event.target, event.detail)):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()

    def _ready(self, result: MigrationResult, preview: bool) -> None:
        self.last_result = result
        self.summary_labels["files"].setText(f"{result.copied_files:,}")
        self.summary_labels["renamed"].setText(f"{result.renamed_files:,}")
        self.summary_labels["modified"].setText(f"{result.modified_files:,}")
        self.summary_labels["excluded"].setText(f"{result.skipped_directories + result.skipped_files:,}")
        if preview:
            self.status.setText("사전 검사 완료 · 내용을 확인한 뒤 안전 복제를 시작할 수 있습니다.")
        else:
            self.status.setText("복제 완료 · 원본 프로젝트는 변경되지 않았습니다.")
            self.open_button.setEnabled(True)
            QMessageBox.information(
                self, "IAR 프로젝트 복제 완료",
                f"새 프로젝트를 생성했습니다.\n\n{result.target_root}\n\n"
                f"복사 {result.copied_files:,}개 · 이름 변경 {result.renamed_files:,}개 · "
                f"내부 수정 {result.modified_files:,}개 · 설정 복원 {result.settings_files_written:,}개\n"
                f"Live Watch 유지 {result.watch_expressions_retained:,}개 · "
                f"없는 변수 제외 {len(result.watch_expressions_omitted):,}개",
            )

    def _error(self, error: BaseException) -> None:
        if isinstance(error, MigrationCancelled):
            self.status.setText("작업이 취소되었습니다. 임시 복사본은 제거되었습니다.")
            return
        self.status.setText("작업을 완료하지 못했습니다. 원본과 대상 기존 자료는 변경되지 않았습니다.")
        QMessageBox.warning(self, "IAR 프로젝트 복제", str(error))

    def _finished(self) -> None:
        self.worker = None
        self._set_running(False, self.status.text())
        self._update_action_state()

    def _set_running(self, running: bool, text: str) -> None:
        if running:
            self.preview_button.setEnabled(False)
            self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(running)
        self.progress.setRange(0, 0 if running else 1)
        self.progress.setValue(0)
        self.status.setText(text)

    def _cancel(self) -> None:
        if self.worker is not None:
            self.status.setText("현재 파일 처리를 마친 뒤 안전하게 취소합니다…")
            self.worker.cancel()

    def _open_target(self) -> None:
        path = self.last_result.target_root if self.last_result else self.final_path_label.text().strip()
        if path and Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _restore_fields(self) -> None:
        workspace = str(self.settings.value("iarMigration/workspaceFile", "") or "")
        saved_first = str(self.settings.value("iarMigration/newFirstPath", "") or "")
        saved_name = str(self.settings.value("iarMigration/newKeyword", "") or "")
        saved_second = str(self.settings.value("iarMigration/newSecondFolder", "") or "")
        self.live_watch_check.setChecked(
            str(self.settings.value("iarMigration/copyLiveWatch", "false")).lower() == "true"
        )
        self.ctrace_check.setChecked(
            str(self.settings.value("iarMigration/copyCTrace", "false")).lower() == "true"
        )
        self.live_watch_backup_dir = str(self.settings.value("iarMigration/liveWatchBackupDir", "") or "")
        self.ctrace_backup_dir = str(self.settings.value("iarMigration/cTraceBackupDir", "") or "")
        if self.live_watch_backup_dir and Path(self.live_watch_backup_dir).is_dir():
            self.live_watch_source.setText(f"백업 불러옴 · {Path(self.live_watch_backup_dir).name}")
            self.live_watch_source.setToolTip(self.live_watch_backup_dir)
        else:
            self.live_watch_backup_dir = ""
        if self.ctrace_backup_dir and Path(self.ctrace_backup_dir).is_dir():
            self.ctrace_source.setText(f"백업 불러옴 · {Path(self.ctrace_backup_dir).name}")
            self.ctrace_source.setToolTip(self.ctrace_backup_dir)
        else:
            self.ctrace_backup_dir = ""
        if workspace and Path(workspace).is_file() and self._load_workspace(workspace, notify=False):
            if saved_first:
                self.new_first_path_edit.setText(saved_first)
            if saved_name:
                self.new_name.setText(saved_name)
            if saved_second:
                self.new_second_folder_edit.setText(saved_second)
        elif workspace:
            self.workspace_edit.setText(workspace)
        self._update_action_state()

    def _save_fields(self) -> None:
        for widget, key in (
            (self.workspace_edit, "workspaceFile"),
            (self.new_first_path_edit, "newFirstPath"),
            (self.new_name, "newKeyword"),
            (self.new_second_folder_edit, "newSecondFolder"),
        ):
            self.settings.setValue(f"iarMigration/{key}", widget.text().strip())
        self.settings.setValue("iarMigration/copyLiveWatch", self.live_watch_check.isChecked())
        self.settings.setValue("iarMigration/copyCTrace", self.ctrace_check.isChecked())
        self.settings.setValue("iarMigration/liveWatchBackupDir", self.live_watch_backup_dir)
        self.settings.setValue("iarMigration/cTraceBackupDir", self.ctrace_backup_dir)
        self.settings.sync()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None:
            answer = QMessageBox.question(
                self, "작업 취소", "진행 중인 작업을 취소하고 창을 닫으시겠습니까?"
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.cancel()
        self._save_fields()
        save_window_state(self, self.settings, "iarMigrationWindow")
        super().closeEvent(event)
