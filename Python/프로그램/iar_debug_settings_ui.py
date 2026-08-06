from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from iar_settings_backup import (
    IarSettingsError, apply_settings_to_current_project, create_settings_backup,
    default_backup_root, default_global_settings_root, discover_settings_files,
    ensure_default_global_settings, load_settings_backup, save_current_as_global_settings,
)
from window_state import apply_dark_title_bar, restore_window_state, save_window_state


class _Signals(QObject):
    ready = Signal(object)
    error = Signal(object)
    finished = Signal()


class _ApplyWorker(QRunnable):
    def __init__(self, workspace: str, folder: str, live: bool, trace: bool) -> None:
        super().__init__()
        self.workspace, self.folder = workspace, folder
        self.live, self.trace = live, trace
        self.signals = _Signals()

    @Slot()
    def run(self) -> None:
        try:
            live_bundle = load_settings_backup(self.folder, "live_watch") if self.live else None
            trace_bundle = load_settings_backup(self.folder, "ctrace") if self.trace else None
            result = apply_settings_to_current_project(
                self.workspace, live_bundle, trace_bundle, backup_before_apply=True
            )
            self.signals.ready.emit(result)
        except BaseException as error:
            self.signals.error.emit(error)
        finally:
            self.signals.finished.emit()


class IarDebugSettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.worker: _ApplyWorker | None = None
        self.setWindowTitle("IAR 디버그 설정")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(920, 560)
        self.setMinimumSize(760, 500)
        self._build_ui()
        self._restore_fields()
        self._startup_mode = restore_window_state(self, settings, "iarDebugSettingsWindow", (920, 560))
        apply_dark_title_bar(self)
        if self._startup_mode == "maximized":
            self.showMaximized()

    def _build_ui(self) -> None:
        self.setStyleSheet("""
            QDialog { background:#151A1F; color:#E5EDF3; }
            QLabel#title { color:#FFFFFF; font-size:22px; font-weight:600; }
            QLabel#subtitle { color:#9FADB8; }
            QFrame#section { background:#1B2228; border:1px solid #34414B; border-radius:5px; }
            QLineEdit { background:#0F151A; color:#E5EDF3; border:1px solid #3A4852; padding:6px; }
            QPushButton { background:#27343D; color:#EAF2F6; border:1px solid #465761; padding:6px 12px; }
            QPushButton:hover { background:#335063; }
            QPushButton#primary { background:#1677AE; border-color:#238CC5; }
            QCheckBox { color:#DCE6EC; spacing:7px; }
            QProgressBar { background:#273139; border:0; min-height:4px; max-height:4px; }
            QProgressBar::chunk { background:#3F7D9E; }
            QLabel#status { color:#9BAAB4; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 14)
        layout.setSpacing(10)
        title = QLabel("IAR 디버그 글로벌 설정")
        title.setObjectName("title")
        subtitle = QLabel(
            "프로젝트 복제 없이 현재 EWARM 프로젝트에 Live Watch와 C-Trace 설정을 적용하거나, "
            "현재 설정을 이후 프로젝트에서 사용할 글로벌 기본값으로 저장합니다."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        section = QFrame()
        section.setObjectName("section")
        form = QFormLayout(section)
        form.setContentsMargins(14, 14, 14, 14)
        form.setSpacing(10)
        self.workspace_edit = self._path_row(form, "현재 IAR 워크스페이스", self._browse_workspace)
        self.workspace_edit.setPlaceholderText("현재 프로젝트의 .eww 파일")
        self.global_edit = self._path_row(form, "글로벌 설정 폴더", self._browse_global)
        self.global_edit.setPlaceholderText("기본 또는 사용자 지정 글로벌 설정 폴더")
        layout.addWidget(section)

        options = QFrame()
        options.setObjectName("section")
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(14, 12, 14, 12)
        options_layout.addWidget(QLabel("적용 및 저장 범위"))
        self.live_check = QCheckBox("Live Watch 변수 및 창 설정")
        self.trace_check = QCheckBox("C-Trace (Event Log, Timeline, Interrupt Log, SWO 관련 설정)")
        options_layout.addWidget(self.live_check)
        options_layout.addWidget(self.trace_check)
        self.summary = QLabel()
        self.summary.setObjectName("status")
        self.summary.setWordWrap(True)
        options_layout.addWidget(self.summary)
        layout.addWidget(options)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel("대기 중")
        self.status.setObjectName("status")
        layout.addWidget(self.status)
        layout.addStretch(1)

        row = QHBoxLayout()
        self.apply_button = QPushButton("현재 프로젝트에 적용")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self._apply)
        self.backup_button = QPushButton("현재 설정 백업")
        self.backup_button.clicked.connect(self._backup)
        self.save_global_button = QPushButton("현재 설정을 글로벌로 저장")
        self.save_global_button.clicked.connect(self._save_global)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        row.addWidget(self.apply_button)
        row.addWidget(self.backup_button)
        row.addWidget(self.save_global_button)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

        self.workspace_edit.textChanged.connect(self._refresh_summary)
        self.global_edit.textChanged.connect(self._refresh_summary)

    def _path_row(self, form: QFormLayout, label: str, callback) -> QLineEdit:
        edit = QLineEdit()
        button = QPushButton("찾아보기…")
        button.clicked.connect(callback)
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(button)
        form.addRow(label, row)
        return edit

    def _restore_fields(self) -> None:
        self.workspace_edit.setText(self.settings.value("iarDebugSettings/workspaceFile", "", str))
        requested = self.settings.value("iarDebugSettings/globalFolder", "", str)
        try:
            default_folder = str(ensure_default_global_settings())
        except IarSettingsError as error:
            default_folder = str(default_global_settings_root())
            self.status.setText(str(error))
        self.global_edit.setText(requested or default_folder)
        self.live_check.setChecked(self.settings.value("iarDebugSettings/liveWatch", True, bool))
        self.trace_check.setChecked(self.settings.value("iarDebugSettings/ctrace", True, bool))
        self._refresh_summary()

    def _browse_workspace(self) -> None:
        start = self.workspace_edit.text() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "현재 IAR 워크스페이스", start, "IAR Workspace (*.eww)")
        if path:
            self.workspace_edit.setText(path)

    def _browse_global(self) -> None:
        start = self.global_edit.text() or str(default_global_settings_root())
        path = QFileDialog.getExistingDirectory(self, "IAR 글로벌 설정 폴더", start)
        if path:
            self.global_edit.setText(path)

    def _refresh_summary(self) -> None:
        workspace = Path(self.workspace_edit.text().strip())
        folder = Path(self.global_edit.text().strip())
        current = []
        if workspace.is_file():
            try:
                current = [path.name for path in discover_settings_files(workspace).values()]
            except (OSError, IarSettingsError):
                pass
        globals_found = []
        if folder.is_dir():
            globals_found = sorted(path.name for path in folder.iterdir() if path.suffix.lower() in {".dbgdt", ".dnx", ".crun", ".wsdt"})
        self.summary.setText(
            "현재: " + (", ".join(current) if current else "설정 파일 미감지") +
            "\n글로벌: " + (", ".join(globals_found) if globals_found else "설정 파일 미감지")
        )

    def _validate(self) -> tuple[str, str, bool, bool]:
        workspace = self.workspace_edit.text().strip()
        folder = self.global_edit.text().strip()
        live, trace = self.live_check.isChecked(), self.trace_check.isChecked()
        if not Path(workspace).is_file() or Path(workspace).suffix.lower() != ".eww":
            raise IarSettingsError("현재 프로젝트의 .eww 파일을 선택하십시오.")
        if not Path(folder).is_dir():
            raise IarSettingsError("글로벌 설정 폴더를 선택하십시오.")
        if not live and not trace:
            raise IarSettingsError("Live Watch 또는 C-Trace 중 하나 이상을 선택하십시오.")
        return workspace, folder, live, trace

    def _apply(self) -> None:
        try:
            workspace, folder, live, trace = self._validate()
        except IarSettingsError as error:
            QMessageBox.warning(self, "IAR 디버그 설정", str(error))
            return
        answer = QMessageBox.question(
            self, "현재 프로젝트에 적용",
            "IAR에서 현재 프로젝트를 닫은 뒤 적용하는 것이 안전합니다.\n"
            "기존 설정은 자동 백업되며 대상 디바이스/디버거 설정은 보존됩니다. 계속하시겠습니까?",
        )
        if answer != QMessageBox.Yes:
            return
        self._set_busy(True, "글로벌 설정을 검증하고 현재 프로젝트에 적용하는 중…")
        self.worker = _ApplyWorker(workspace, folder, live, trace)
        self.worker.signals.ready.connect(self._apply_ready)
        self.worker.signals.error.connect(self._operation_error)
        self.worker.signals.finished.connect(lambda: self._set_busy(False, self.status.text()))
        self.pool.start(self.worker)

    def _apply_ready(self, result) -> None:
        message = f"{len(result.written_files)}개 설정 파일을 적용했습니다."
        if result.retained_watch_expressions or result.omitted_watch_expressions:
            message += f"\nLive Watch 유지 {len(result.retained_watch_expressions)}개 / 제외 {len(result.omitted_watch_expressions)}개"
        if result.backup_folders:
            message += "\n자동 백업: " + "\n".join(result.backup_folders)
        self.status.setText(message.replace("\n", " · "))
        self._refresh_summary()
        QMessageBox.information(self, "적용 완료", message)

    def _backup(self) -> None:
        try:
            workspace, _, live, trace = self._validate()
            detected = discover_settings_files(workspace)
            project = next(
                (path.stem for extension, path in detected.items() if extension in {".dnx", ".dbgdt"}),
                Path(workspace).stem,
            )
            folders = []
            if live:
                folders.append(create_settings_backup(workspace, project, "live_watch"))
            if trace:
                folders.append(create_settings_backup(workspace, project, "ctrace"))
            message = "현재 설정을 백업했습니다.\n" + "\n".join(map(str, folders))
            self.status.setText(f"백업 완료 · {default_backup_root()}")
            QMessageBox.information(self, "백업 완료", message)
        except (OSError, IarSettingsError) as error:
            self._operation_error(error)

    def _save_global(self) -> None:
        try:
            workspace, folder, live, trace = self._validate()
        except IarSettingsError as error:
            self._operation_error(error)
            return
        answer = QMessageBox.question(
            self, "글로벌 기본값 갱신",
            "현재 프로젝트에서 사용 중인 설정을 글로벌 기본값으로 저장합니다.\n"
            "기존 글로벌 기본값은 .history 폴더에 보존됩니다. 계속하시겠습니까?",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            destination = save_current_as_global_settings(workspace, folder, live, trace)
            self.status.setText(f"글로벌 기본값 갱신 완료 · {destination}")
            self._refresh_summary()
            QMessageBox.information(self, "저장 완료", f"글로벌 기본값을 갱신했습니다.\n{destination}")
        except (OSError, IarSettingsError) as error:
            self._operation_error(error)

    def _operation_error(self, error: object) -> None:
        self.status.setText(str(error))
        QMessageBox.critical(self, "IAR 디버그 설정 오류", str(error))

    def _set_busy(self, busy: bool, text: str) -> None:
        for button in (self.apply_button, self.backup_button, self.save_global_button):
            button.setEnabled(not busy)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(0)
        self.status.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None and self.pool.activeThreadCount():
            QMessageBox.information(self, "작업 진행 중", "설정 적용이 끝난 뒤 창을 닫으십시오.")
            event.ignore()
            return
        self.settings.setValue("iarDebugSettings/workspaceFile", self.workspace_edit.text().strip())
        self.settings.setValue("iarDebugSettings/globalFolder", self.global_edit.text().strip())
        self.settings.setValue("iarDebugSettings/liveWatch", self.live_check.isChecked())
        self.settings.setValue("iarDebugSettings/ctrace", self.trace_check.isChecked())
        save_window_state(self, self.settings, "iarDebugSettingsWindow")
        super().closeEvent(event)
