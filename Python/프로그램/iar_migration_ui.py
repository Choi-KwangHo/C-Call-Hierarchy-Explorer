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
    MigrationCancelled, MigrationError, MigrationEvent, MigrationOptions, MigrationResult,
    format_event, inspect_iar_workspace, migrate_iar_project, preview_iar_migration,
    suggested_embedded_path,
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
        self.source_edit = self._path_row(form_layout, "복제할 기존 프로젝트 루트", self._browse_source)
        old_folders = QHBoxLayout()
        self.old_first_folder = QLineEdit()
        self.old_first_folder.setPlaceholderText("기존 1차(메인) 폴더")
        self.old_second_folder = QLineEdit()
        self.old_second_folder.setPlaceholderText("기존 2차(프로젝트) 폴더")
        old_folders.addWidget(self.old_first_folder)
        old_folders.addWidget(self.old_second_folder)
        form_layout.addRow("자동 감지된 기존 2단 폴더", old_folders)

        self.target_base_edit = self._path_row(form_layout, "새 프로젝트 저장 기준 폴더", self._browse_target_base)
        new_folders = QHBoxLayout()
        self.new_first_folder = QLineEdit()
        self.new_first_folder.setPlaceholderText("새 1차(메인) 폴더")
        self.new_second_folder = QLineEdit()
        self.new_second_folder.setPlaceholderText("새 2차(프로젝트) 폴더")
        new_folders.addWidget(self.new_first_folder)
        new_folders.addWidget(self.new_second_folder)
        form_layout.addRow("새로 만들 2단 폴더", new_folders)
        self.target_edit = QLineEdit()
        self.target_edit.setReadOnly(True)
        self.target_edit.setPlaceholderText("저장 기준 폴더 + 새 1차 폴더 + 새 2차 폴더")
        form_layout.addRow("최종 생성 경로", self.target_edit)
        self.old_name = QLineEdit()
        self.old_name.setPlaceholderText("예: PCB031_48_ESS_UPS_V1.2")
        form_layout.addRow("기존 프로젝트 핵심 이름", self.old_name)
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("예: 303-J-00-X-01")
        form_layout.addRow("새 프로젝트 핵심 이름", self.new_name)
        self.old_path = QLineEdit()
        self.old_path.setPlaceholderText("예: Susan-Heavy-duty-lift-48V\\48V_HDL")
        form_layout.addRow("파일 내부의 기존 폴더 경로", self.old_path)
        self.new_path = QLineEdit()
        self.new_path.setPlaceholderText("예: 303-J-00-X-01\\TEST_BD")
        form_layout.addRow("파일 내부의 새 폴더 경로", self.new_path)
        for edit in (self.target_base_edit, self.new_first_folder, self.new_second_folder):
            edit.textChanged.connect(self._update_derived_paths)
        self.new_first_folder.textChanged.connect(self._suggest_new_keyword)
        for edit in (self.old_first_folder, self.old_second_folder):
            edit.textChanged.connect(self._update_old_embedded_path)
        option_row = QHBoxLayout()
        self.source_text_check = QCheckBox("C/C++ 및 관련 텍스트 파일 내부도 함께 치환")
        self.source_text_check.setChecked(True)
        self.source_text_check.setToolTip("IAR 설정 파일뿐 아니라 .c/.h/.icf 등에서 프로젝트명과 경로를 함께 바꿉니다.")
        option_row.addWidget(self.source_text_check)
        self.rename_dir_check = QCheckBox("기존 프로젝트명이 포함된 하위 폴더명 변경")
        self.rename_dir_check.setChecked(True)
        option_row.addWidget(self.rename_dir_check)
        option_row.addStretch(1)
        form_layout.addRow("추가 범위", option_row)
        layout.addWidget(form_frame)

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

    def _browse_source(self) -> None:
        start = self.source_edit.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "기존 IAR 프로젝트 폴더", start)
        if not selected:
            return
        self._apply_source_root(Path(selected))

    def _browse_workspace(self) -> None:
        start = self.workspace_edit.text().strip() or self.source_edit.text().strip() or str(Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self, "기존 IAR 워크스페이스 열기", start, "IAR Workspace (*.eww)"
        )
        if not selected:
            return
        try:
            info = inspect_iar_workspace(selected)
        except Exception as error:  # noqa: BLE001 - validation is shown to the user
            QMessageBox.warning(self, "IAR 워크스페이스 확인", str(error))
            return
        previous_old_keyword = self.old_name.text()
        self.workspace_edit.setText(info.workspace_file)
        self.source_edit.setText(info.source_root)
        self.old_first_folder.setText(info.first_folder)
        self.old_second_folder.setText(info.second_folder)
        self.old_name.setText(info.project_keyword)
        if not self.new_name.text().strip() or self.new_name.text() == previous_old_keyword:
            self.new_name.setText(self.new_first_folder.text().strip())
        self.old_path.setText(f"{info.first_folder}\\{info.second_folder}")
        project_count = len(info.referenced_projects)
        self.status.setText(
            f"워크스페이스 자동 인식 완료 · {info.encoding} · 참조 프로젝트 {project_count}개"
        )

    def _apply_source_root(self, selected: Path) -> None:
        self.source_edit.setText(str(selected))
        self.old_first_folder.setText(selected.parent.name)
        self.old_second_folder.setText(selected.name)
        self.old_path.setText(suggested_embedded_path(selected))

    def _browse_target_base(self) -> None:
        current = self.target_base_edit.text().strip()
        start = current or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "새 프로젝트 저장 기준 폴더", start)
        if selected:
            self.target_base_edit.setText(selected)

    def _update_old_embedded_path(self) -> None:
        parts = [self.old_first_folder.text().strip(), self.old_second_folder.text().strip()]
        if all(parts):
            self.old_path.setText("\\".join(parts))

    def _update_derived_paths(self) -> None:
        base = self.target_base_edit.text().strip()
        first = self.new_first_folder.text().strip()
        second = self.new_second_folder.text().strip()
        if base and first and second:
            self.target_edit.setText(str(Path(base) / first / second))
            self.new_path.setText(f"{first}\\{second}")
        else:
            self.target_edit.clear()

    def _suggest_new_keyword(self, value: str) -> None:
        if not self.new_name.text().strip():
            self.new_name.setText(value.strip())

    def _options(self) -> MigrationOptions:
        invalid = set('<>:"/\\|?*')
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        for label, value in (
            ("새 1차 폴더", self.new_first_folder.text().strip()),
            ("새 2차 폴더", self.new_second_folder.text().strip()),
        ):
            if not value or value in {".", ".."} or any(character in invalid for character in value):
                raise MigrationError(f"{label} 이름이 올바르지 않습니다: {value or '(비어 있음)'}")
            if value.rstrip(". ").upper() in reserved or value != value.rstrip(". "):
                raise MigrationError(f"Windows에서 사용할 수 없는 {label} 이름입니다: {value}")
        keyword = self.new_name.text()
        if any(character in invalid for character in keyword):
            raise MigrationError("새 프로젝트 핵심 이름에 파일명으로 사용할 수 없는 문자가 포함되어 있습니다.")
        return MigrationOptions(
            source_root=self.source_edit.text().strip(),
            target_root=self.target_edit.text().strip(),
            old_keyword=self.old_name.text(),
            new_keyword=self.new_name.text(),
            old_embedded_path=self.old_path.text().strip(),
            new_embedded_path=self.new_path.text().strip(),
            replace_source_text=self.source_text_check.isChecked(),
            rename_directories=self.rename_dir_check.isChecked(),
        )

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
                "원본은 변경하지 않으며, 대상 폴더가 비어 있지 않으면 자동으로 중단합니다.\n"
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
                f"내부 수정 {result.modified_files:,}개",
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

    def _set_running(self, running: bool, text: str) -> None:
        self.preview_button.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.progress.setRange(0, 0 if running else 1)
        self.progress.setValue(0)
        self.status.setText(text)

    def _cancel(self) -> None:
        if self.worker is not None:
            self.status.setText("현재 파일 처리를 마친 뒤 안전하게 취소합니다…")
            self.worker.cancel()

    def _open_target(self) -> None:
        path = self.last_result.target_root if self.last_result else self.target_edit.text().strip()
        if path and Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _restore_fields(self) -> None:
        for widget, key in (
            (self.workspace_edit, "workspaceFile"), (self.source_edit, "sourceRoot"),
            (self.target_base_edit, "targetBase"),
            (self.old_first_folder, "oldFirstFolder"), (self.old_second_folder, "oldSecondFolder"),
            (self.new_first_folder, "newFirstFolder"), (self.new_second_folder, "newSecondFolder"),
            (self.old_name, "oldKeyword"), (self.new_name, "newKeyword"),
            (self.old_path, "oldEmbeddedPath"), (self.new_path, "newEmbeddedPath"),
        ):
            widget.setText(str(self.settings.value(f"iarMigration/{key}", "") or ""))
        # Migrate the pre-1.4 target field into the new two-level destination model.
        if not self.target_base_edit.text().strip():
            legacy = str(self.settings.value("iarMigration/targetRoot", "") or "")
            if legacy:
                legacy_path = Path(legacy)
                self.target_base_edit.setText(str(legacy_path.parent.parent))
                self.new_first_folder.setText(legacy_path.parent.name)
                self.new_second_folder.setText(legacy_path.name)
        self.source_text_check.setChecked(
            str(self.settings.value("iarMigration/replaceSourceText", "true")).lower() != "false"
        )
        self.rename_dir_check.setChecked(
            str(self.settings.value("iarMigration/renameDirectories", "true")).lower() != "false"
        )
        self._update_derived_paths()

    def _save_fields(self) -> None:
        for widget, key in (
            (self.workspace_edit, "workspaceFile"), (self.source_edit, "sourceRoot"),
            (self.target_base_edit, "targetBase"),
            (self.old_first_folder, "oldFirstFolder"), (self.old_second_folder, "oldSecondFolder"),
            (self.new_first_folder, "newFirstFolder"), (self.new_second_folder, "newSecondFolder"),
            (self.old_name, "oldKeyword"), (self.new_name, "newKeyword"),
            (self.old_path, "oldEmbeddedPath"), (self.new_path, "newEmbeddedPath"),
        ):
            self.settings.setValue(f"iarMigration/{key}", widget.text().strip())
        self.settings.setValue("iarMigration/targetRoot", self.target_edit.text().strip())
        self.settings.setValue("iarMigration/replaceSourceText", self.source_text_check.isChecked())
        self.settings.setValue("iarMigration/renameDirectories", self.rename_dir_check.isChecked())
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
