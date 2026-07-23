from __future__ import annotations

import sys
import traceback
import re
import html
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QProcess, QRunnable, QSettings, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QTextEdit,
    QFrame, QHBoxLayout, QSplitter, QStackedWidget, QStyle, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from analyzer import AnalysisResult, AnalyzerSession, CallView, build_call_view
from project_cache import ProjectCacheStore
from settings_dialog import ProjectSettingsDialog, normalize_exclusions
from virtual_tree import CallTreeWidget
from xlsx_exporter import export_xlsx
from update_service import (
    RELEASE_PAGE, ReleaseInfo, UpdateError, download_asset, fetch_latest_release,
    is_newer_version, verify_downloaded_asset,
)


APP_NAME = "C Call Hierarchy Explorer"
APP_VERSION = "1.1.13"
APP_PUBLISHER = "Call Hierarchy Tools"


def resource_path(relative: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / relative)


def sanitize_recent_folders(values: list[str]) -> list[str]:
    """Remove test/smoke-test folders and duplicate entries from persisted history."""
    temporary_root = Path(tempfile.gettempdir()).resolve()
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not str(value).strip():
            continue
        try:
            resolved = Path(value).resolve()
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(temporary_root)
            continue
        except ValueError:
            pass
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(str(resolved))
    return cleaned[:10]


def unique_output_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        numbered = candidate.with_name(f"{candidate.stem}_{index}{candidate.suffix}")
        if not numbered.exists():
            return numbered
        index += 1


def compact_file_path(
    root_name: str,
    relative_path: str,
    available_width: int,
    metrics,
    directory_depth: int | None = None,
    show_full: bool = False,
) -> str:
    """패널 폭에 맞춰 파일명을 우선 보존하며 경로의 왼쪽부터 축약한다."""
    parts = [part for part in relative_path.replace("/", "\\").split("\\") if part]
    if not parts:
        parts = [relative_path]
    full = "\\".join([root_name, *parts]) if root_name else "\\".join(parts)
    if show_full or (directory_depth is None and metrics.horizontalAdvance(full) <= available_width):
        return full
    if directory_depth is None:
        for start in range(0, len(parts)):
            candidate = "..\\" + "\\".join(parts[start:])
            if metrics.horizontalAdvance(candidate) <= available_width:
                return candidate
        prefix = "..\\"
    else:
        directories = parts[:-1]
        visible_directories = directories[-directory_depth:] if directory_depth > 0 else []
        prefix = "..\\" + ("\\".join(visible_directories) + "\\" if visible_directories else "")
        candidate = prefix + parts[-1]
        if metrics.horizontalAdvance(candidate) <= available_width:
            return candidate

    filename = parts[-1]
    suffix = Path(filename).suffix
    stem = filename[:-len(suffix)] if suffix else filename
    for remaining in range(max(2, len(stem) - 1), 1, -1):
        left = max(1, (remaining + 1) // 2)
        right = max(1, remaining - left)
        shortened = stem[:left] + ".." + stem[-right:] + suffix
        candidate = prefix + shortened
        if metrics.horizontalAdvance(candidate) <= available_width:
            return candidate
    return prefix + stem[:1] + ".." + suffix


class ResponsiveFileTree(QTreeWidget):
    resized = Signal()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.resized.emit()


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(str, int, int, str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, task: Callable, show_progress: bool = True) -> None:
        super().__init__()
        self.task = task
        self.show_progress = show_progress
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.task(self.signals.progress.emit)
            self.signals.result.emit(result)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class RecentStartPage(QWidget):
    openRequested = Signal()
    recentRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("startPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#startPage { background: #181818; color: #CCCCCC; }
            QLabel#title { color: #F0F0F0; font-size: 30px; font-weight: 300; }
            QLabel#subtitle { color: #929292; font-size: 15px; }
            QLabel#section { color: #E5E5E5; font-size: 18px; margin-top: 14px; }
            QPushButton#startLink, QPushButton#recentLink {
                border: none; background: transparent; color: #4DAAFC;
                text-align: left; padding: 5px 4px; font-size: 13px;
            }
            QPushButton#startLink:hover, QPushButton#recentLink:hover {
                background: #2A2D2E; color: #75BEFF;
            }
            QFrame#infoCard { background: #252526; border-radius: 6px; }
            QLabel#cardTitle { color: #F0F0F0; font-size: 16px; font-weight: 600; }
            QLabel#cardText { color: #B8B8B8; font-size: 12px; }
        """)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(820)
        columns = QHBoxLayout(content)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(70)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel("C 프로젝트 호출 구조 탐색기 · 개발 버전")
        subtitle.setObjectName("subtitle")
        left_layout.addWidget(title)
        left_layout.addWidget(subtitle)
        start_title = QLabel("시작")
        start_title.setObjectName("section")
        left_layout.addWidget(start_title)
        open_button = QPushButton("폴더 열기…")
        open_button.setObjectName("startLink")
        open_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        open_button.clicked.connect(self.openRequested.emit)
        left_layout.addWidget(open_button)
        recent_title = QLabel("최근 항목")
        recent_title.setObjectName("section")
        left_layout.addWidget(recent_title)
        self.recent_layout = QVBoxLayout()
        self.recent_layout.setSpacing(1)
        left_layout.addLayout(self.recent_layout)
        left_layout.addStretch(1)

        card = QFrame()
        card.setObjectName("infoCard")
        card.setMinimumWidth(300)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 20)
        card_title = QLabel("프로젝트 분석")
        card_title.setObjectName("cardTitle")
        card_text = QLabel(
            "C 소스 폴더를 열면 main()과 독립 ISR 시작점부터\n"
            "호출 관계를 분석합니다. 최근 폴더는 다음 실행에도\n"
            "유지되며 전체 경로는 항목 위에서 확인할 수 있습니다."
        )
        card_text.setObjectName("cardText")
        card_text.setWordWrap(True)
        card_layout.addWidget(card_title)
        card_layout.addSpacing(8)
        card_layout.addWidget(card_text)
        card_layout.addStretch(1)

        columns.addWidget(left, 1)
        columns.addWidget(card, 1)
        outer.addWidget(content, 0, Qt.AlignHCenter)
        outer.addStretch(2)

    def set_recent_folders(self, folders: list[str]) -> None:
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not folders:
            empty = QLabel("최근에 연 폴더가 없습니다.")
            empty.setStyleSheet("color: #858585; padding: 5px 4px;")
            self.recent_layout.addWidget(empty)
            return
        for folder in folders[:5]:
            path = Path(folder)
            parent = str(path.parent)
            if len(parent) > 48:
                parent = "…" + parent[-47:]
            button = QPushButton(f"{path.name}    {parent}")
            button.setObjectName("recentLink")
            button.setToolTip(folder)
            button.clicked.connect(lambda checked=False, selected=folder: self.recentRequested.emit(selected))
            self.recent_layout.addWidget(button)
        if len(folders) > 5:
            more = QLabel("나머지 항목은 파일 → 최근 폴더에서 열 수 있습니다.")
            more.setStyleSheet("color: #858585; padding: 5px 4px;")
            self.recent_layout.addWidget(more)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1480, 900)
        self.session = AnalyzerSession()
        self.result: AnalysisResult | None = None
        self.view: CallView | None = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.cache_pool = QThreadPool(self)
        self.cache_pool.setMaxThreadCount(1)
        self.update_pool = QThreadPool(self)
        self.update_pool.setMaxThreadCount(1)
        self._workers: set[Worker] = set()
        self._cache_workers: set[Worker] = set()
        self._update_workers: set[Worker] = set()
        self.busy = False
        self._closing = False
        self._combo_refresh = False
        self._pending_manual_check = False
        self._refresh_after_cache = False
        self._restoring_state = True
        self._cache_dirty = False
        self._cache_generation = 0
        self._cache_saving = False
        self.show_external_functions = True
        self.cache_store = ProjectCacheStore()
        self.settings = QSettings("CCodeTree", "CFunctionCallTree")
        stored = self.settings.value("recentFolders", [])
        stored_folders = [stored] if isinstance(stored, str) and stored else list(stored or [])
        self.recent_folders = sanitize_recent_folders([str(value) for value in stored_folders])
        if self.recent_folders != stored_folders:
            self.settings.setValue("recentFolders", self.recent_folders)
        self._build_ui()
        self._restoring_state = False

        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(2000)
        self.monitor_timer.timeout.connect(self._monitor_tick)
        self.monitor_timer.start()
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(lambda: self._search_move(1, True))
        self.cache_timer = QTimer(self)
        self.cache_timer.setInterval(5 * 60 * 1000)
        self.cache_timer.timeout.connect(self._save_cache_async)
        self.cache_timer.start()
        QTimer.singleShot(5000, self._startup_update_check)

    def _build_ui(self) -> None:
        file_menu = self.menuBar().addMenu("파일")
        open_action = QAction("폴더 열기…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._choose_folder)
        file_menu.addAction(open_action)
        self.recent_menu = file_menu.addMenu("최근 폴더")
        start_action = QAction("시작 화면", self)
        start_action.triggered.connect(self._show_start_page)
        file_menu.addAction(start_action)
        file_menu.addSeparator()
        exit_action = QAction("끝내기", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        settings_menu = self.menuBar().addMenu("설정")
        project_settings_action = QAction("프로젝트 설정 열기…", self)
        project_settings_action.setShortcut("Ctrl+,")
        project_settings_action.triggered.connect(self._open_project_settings)
        settings_menu.addAction(project_settings_action)

        self.toolbar = QToolBar("주 도구", self)
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)
        check = QPushButton("지금 변경 확인")
        check.clicked.connect(lambda: self._check_updates(False))
        self.toolbar.addWidget(check)
        export = QPushButton("Excel 트리 출력")
        export.clicked.connect(self._export)
        self.toolbar.addWidget(export)
        self.toolbar.addSeparator()
        self.auto_check = QCheckBox("2초 자동 감시")
        self.auto_check.setChecked(True)
        self.toolbar.addWidget(self.auto_check)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(QLabel("검색 "))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("함수명 또는 파일명")
        self.search_edit.setFixedWidth(240)
        self.search_edit.textChanged.connect(lambda: self.search_timer.start())
        self.search_edit.returnPressed.connect(lambda: self._search_move(1, False))
        self.toolbar.addWidget(self.search_edit)
        self.search_previous = QPushButton("◀")
        self.search_previous.setFixedWidth(30)
        self.search_previous.setToolTip("이전 검색 결과")
        self.search_previous.clicked.connect(lambda: self._search_move(-1, False))
        self.toolbar.addWidget(self.search_previous)
        self.search_next = QPushButton("▶")
        self.search_next.setFixedWidth(30)
        self.search_next.setToolTip("다음 검색 결과")
        self.search_next.clicked.connect(lambda: self._search_move(1, False))
        self.toolbar.addWidget(self.search_next)
        self.search_count = QLabel("")
        self.search_count.setMinimumWidth(58)
        self.toolbar.addWidget(self.search_count)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(QLabel("시작점 "))
        self.main_combo = QComboBox()
        self.main_combo.setMinimumWidth(300)
        self.main_combo.currentIndexChanged.connect(self._main_changed)
        self.toolbar.addWidget(self.main_combo)

        view_menu = self.menuBar().addMenu("보기")
        self.file_action = QAction("파일 / 함수 트리", self, checkable=True)
        self.source_action = QAction("CODE 미리보기", self, checkable=True)
        self.other_roots_action = QAction("기타 독립 시작점", self, checkable=True)
        self.file_action.setChecked(self.settings.value("showFileTree", False, type=bool))
        self.source_action.setChecked(self.settings.value("showCodePreview", False, type=bool))
        view_menu.addAction(self.file_action)
        view_menu.addAction(self.source_action)
        view_menu.addSeparator()
        view_menu.addAction(self.other_roots_action)

        help_menu = self.menuBar().addMenu("도움말")
        update_action = QAction("업데이트 확인…", self)
        update_action.triggered.connect(lambda: self._check_program_update(True))
        help_menu.addAction(update_action)
        update_test_action = QAction("업데이트 다운로드 검증…", self)
        update_test_action.triggered.connect(self._test_update_download)
        help_menu.addAction(update_test_action)
        help_menu.addSeparator()
        release_action = QAction("GitHub 릴리스 페이지", self)
        release_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(RELEASE_PAGE)))
        help_menu.addAction(release_action)
        about_action = QAction("프로그램 정보", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

        self.file_tree = ResponsiveFileTree()
        self.file_tree.setHeaderLabel("파일 / 함수")
        minimum_path = self.file_tree.fontMetrics().horizontalAdvance(r"..\MMMMMMMMMM")
        self.file_tree.setMinimumWidth(minimum_path + 48)
        self.file_tree.itemExpanded.connect(self._expand_file)
        self.file_tree.itemExpanded.connect(lambda item: self._mark_cache_dirty())
        self.file_tree.itemCollapsed.connect(lambda item: self._mark_cache_dirty())
        self.file_tree.itemActivated.connect(self._file_item_activated)
        self.file_tree.resized.connect(self._refresh_file_tree_paths)
        self.file_tree.setVisible(self.file_action.isChecked())
        self.call_tree = CallTreeWidget()
        self.call_tree.functionActivated.connect(self._show_function)
        self.call_tree.stateChanged.connect(self._mark_cache_dirty)
        self.source_panel = QWidget()
        source_layout = QVBoxLayout(self.source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(0)
        self.source_summary = QTextEdit()
        self.source_summary.setReadOnly(True)
        self.source_summary.setFixedHeight(112)
        self.source_summary.setStyleSheet(
            "QTextEdit { background: #F6F8FA; border: 0; border-bottom: 1px solid #B8C5CE; padding: 5px; }"
        )
        self.source_view = QPlainTextEdit()
        self.source_view.setReadOnly(True)
        self.source_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.source_panel.setMinimumWidth(360)
        self.source_panel.setVisible(self.source_action.isChecked())
        source_layout.addWidget(self.source_summary)
        source_layout.addWidget(self.source_view, 1)
        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.addWidget(self.file_tree)
        self.workspace_splitter.addWidget(self.call_tree)
        self.workspace_splitter.addWidget(self.source_panel)
        self.workspace_splitter.setCollapsible(0, False)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setStretchFactor(2, 0)
        self.workspace_splitter.splitterMoved.connect(lambda position, index: self._mark_cache_dirty())
        self.start_page = RecentStartPage()
        self.start_page.openRequested.connect(self._choose_folder)
        self.start_page.recentRequested.connect(self._open_recent_folder)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.start_page)
        self.pages.addWidget(self.workspace_splitter)
        self.setCentralWidget(self.pages)
        self.file_action.toggled.connect(self._set_file_panel_visible)
        self.source_action.toggled.connect(self._set_code_panel_visible)
        self.other_roots_action.toggled.connect(self._rebuild_view)
        self.other_roots_action.toggled.connect(lambda checked: self._mark_cache_dirty())
        self.search_edit.textChanged.connect(lambda text: self._mark_cache_dirty())

        self.status_label = QLabel("폴더를 선택하면 C 소스 분석을 시작합니다.")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(520)
        self.progress.hide()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self._refresh_recent_views()
        self._show_start_page()

    def _set_file_panel_visible(self, visible: bool) -> None:
        self.file_tree.setVisible(visible)
        if visible:
            minimum = self.file_tree.minimumWidth()
            sizes = self.workspace_splitter.sizes()
            if sizes and sizes[0] < minimum:
                shortage = minimum - sizes[0]
                sizes[0] = minimum
                if len(sizes) > 1:
                    sizes[1] = max(self.call_tree.minimumWidth(), sizes[1] - shortage)
                self.workspace_splitter.setSizes(sizes)
            QTimer.singleShot(0, self._refresh_file_tree_paths)
        self.settings.setValue("showFileTree", visible)
        self._mark_cache_dirty()

    def _set_code_panel_visible(self, visible: bool) -> None:
        self.source_panel.setVisible(visible)
        self.settings.setValue("showCodePreview", visible)
        self._mark_cache_dirty()

    def _choose_folder(self) -> None:
        initial = self.session.root or (self.recent_folders[0] if self.recent_folders else str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, "C 소스 폴더 선택", initial)
        if not folder:
            return
        self._open_folder(folder)

    def _project_settings_group(self, root: str) -> str:
        return f"projects/{self.cache_store.path_for(root).stem}"

    def _load_excluded_folders(self, root: str) -> list[str]:
        value = self.settings.value(f"{self._project_settings_group(root)}/excludedFolders", [])
        values = [value] if isinstance(value, str) and value else list(value or [])
        return normalize_exclusions([str(item) for item in values])

    def _store_excluded_folders(self, root: str, values: list[str]) -> None:
        self.settings.setValue(
            f"{self._project_settings_group(root)}/excludedFolders",
            normalize_exclusions(values),
        )

    def _load_show_external_functions(self, root: str) -> bool:
        return self.settings.value(
            f"{self._project_settings_group(root)}/showExternalFunctions",
            True,
            type=bool,
        )

    def _store_show_external_functions(self, root: str, visible: bool) -> None:
        self.settings.setValue(
            f"{self._project_settings_group(root)}/showExternalFunctions",
            visible,
        )

    def _open_project_settings(self) -> None:
        if not self.result or not self.session.root:
            QMessageBox.information(self, "프로젝트 설정", "먼저 분석할 메인 폴더를 열어 주십시오.")
            return
        current = list(self.session.excluded_directories)
        current_external = self.show_external_functions
        dialog = ProjectSettingsDialog(
            self.session.root,
            current,
            self,
            show_external_functions=current_external,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        updated = dialog.excluded_folders()
        updated_external = dialog.show_external_functions()
        folders_changed = [value.casefold() for value in updated] != [value.casefold() for value in current]
        external_changed = updated_external != current_external
        if not folders_changed and not external_changed:
            return
        self._store_excluded_folders(self.session.root, updated)
        self._store_show_external_functions(self.session.root, updated_external)
        self.session.set_excluded_directories(updated)
        self.show_external_functions = updated_external
        self._mark_cache_dirty()
        if folders_changed:
            self.status_label.setText("분석 범위 설정 적용 중…")
            self._check_updates(False, external_changed)
        else:
            self._rebuild_view()

    def _open_folder(self, folder: str) -> None:
        folder = str(Path(folder).resolve())
        if self.busy:
            self.status_label.setText("진행 중인 작업이 끝난 후 다른 폴더를 열어 주십시오.")
            return
        if not Path(folder).is_dir():
            self._remove_recent(folder)
            QMessageBox.warning(self, "폴더 없음", f"폴더를 찾을 수 없습니다.\n{folder}")
            return
        if self.result and self.session.root and self.session.root.casefold() != folder.casefold():
            self._save_cache_now()
        self._cache_dirty = False
        self._cache_generation = 0
        self.pages.setCurrentWidget(self.workspace_splitter)
        self.toolbar.show()
        excluded_folders = self._load_excluded_folders(folder)
        show_external_functions = self._load_show_external_functions(folder)

        def task(progress):
            cached = self.cache_store.load(folder)
            if cached:
                result = cached["result"]
                ui_state = cached.get("ui_state") if isinstance(cached.get("ui_state"), dict) else {}
                cached_exclusions = normalize_exclusions([str(value) for value in ui_state.get("excluded_folders", [])])
                self.session.restore(folder, cached["session_cache"], result, cached_exclusions)
                selected = ui_state.get("selected_main")
                include_other = bool(ui_state.get("include_other", False))
                view = build_call_view(
                    result,
                    selected,
                    include_other,
                    include_external_calls=show_external_functions,
                )
                return result, view, ui_state, True, excluded_folders, show_external_functions
            result = self.session.initial_scan(folder, progress, excluded_directories=excluded_folders)
            view = build_call_view(result, include_external_calls=show_external_functions)
            return result, view, {}, False, excluded_folders, show_external_functions

        self._start_job(task, self._initial_ready, True, "저장된 프로젝트 상태 확인 중…")

    def _open_recent_folder(self, folder: str) -> None:
        self._open_folder(folder)

    def _show_start_page(self) -> None:
        self._refresh_recent_views()
        self.pages.setCurrentWidget(self.start_page)
        self.toolbar.hide()
        self.status_label.setText("폴더 열기 또는 최근 항목을 선택하십시오.")

    def _remember_folder(self, folder: str) -> None:
        normalized = str(Path(folder).resolve())
        self.recent_folders = [
            value for value in self.recent_folders
            if str(Path(value)).lower() != normalized.lower()
        ]
        self.recent_folders.insert(0, normalized)
        self.recent_folders = self.recent_folders[:10]
        self.settings.setValue("recentFolders", self.recent_folders)
        self._refresh_recent_views()

    def _remove_recent(self, folder: str) -> None:
        self.recent_folders = [value for value in self.recent_folders if value.lower() != folder.lower()]
        self.settings.setValue("recentFolders", self.recent_folders)
        self._refresh_recent_views()

    def _clear_recent(self) -> None:
        self.recent_folders = []
        self.settings.setValue("recentFolders", [])
        self._refresh_recent_views()

    def _refresh_recent_views(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.clear()
        if self.recent_folders:
            for folder in self.recent_folders:
                path = Path(folder)
                action = self.recent_menu.addAction(f"{path.name} — {path.parent}")
                action.setToolTip(folder)
                action.triggered.connect(lambda checked=False, selected=folder: self._open_recent_folder(selected))
            self.recent_menu.addSeparator()
        clear_action = self.recent_menu.addAction("최근 항목 지우기")
        clear_action.setEnabled(bool(self.recent_folders))
        clear_action.triggered.connect(self._clear_recent)
        if hasattr(self, "start_page"):
            self.start_page.set_recent_folders(self.recent_folders)

    def _start_job(self, task: Callable, callback: Callable, show_progress: bool, message: str) -> bool:
        if self.busy or self._closing:
            if show_progress:
                self.status_label.setText("다른 분석 작업이 진행 중입니다.")
            return False
        self.busy = True
        self.status_label.setText(message)
        self.progress.setVisible(show_progress)
        self.progress.setRange(0, 0)
        worker = Worker(task, show_progress)
        worker.signals.result.connect(callback)
        worker.signals.progress.connect(lambda phase, current, total, detail: self._progress(phase, current, total, detail, show_progress))
        worker.signals.error.connect(self._job_error)
        worker.signals.finished.connect(lambda current=worker: self._worker_finished(current, show_progress))
        self._workers.add(worker)
        self.pool.start(worker)
        return True

    def _worker_finished(self, worker: Worker, show_progress: bool) -> None:
        self._workers.discard(worker)
        self._job_finished(show_progress)
        if self._refresh_after_cache and not self._closing:
            self._refresh_after_cache = False
            QTimer.singleShot(0, lambda: self._check_updates(True))

    def _progress(self, phase: str, current: int, total: int, detail: str, show: bool) -> None:
        self.status_label.setText(f"{phase}: {current:,}/{total:,}  {Path(detail).name if detail else ''}")
        if show:
            self.progress.setRange(0, max(0, total))
            self.progress.setValue(current)

    def _job_error(self, detail: str) -> None:
        self.status_label.setText("작업 중 오류가 발생했습니다.")
        QMessageBox.critical(self, "분석 오류", detail)

    def _job_finished(self, show_progress: bool) -> None:
        self.busy = False
        if show_progress:
            self.progress.hide()
        if self._pending_manual_check and not self._closing:
            self._pending_manual_check = False
            QTimer.singleShot(0, lambda: self._check_updates(False))

    def _initial_ready(self, payload: object) -> None:
        result, view, ui_state, from_cache, desired_exclusions, show_external_functions = payload
        self._restoring_state = True
        self.show_external_functions = show_external_functions
        self._remember_folder(result.root)
        self.pages.setCurrentWidget(self.workspace_splitter)
        self.toolbar.show()
        self._apply_result(result, view, False)
        if from_cache:
            self._restore_project_ui(ui_state)
        cached_exclusions = normalize_exclusions([str(value) for value in ui_state.get("excluded_folders", [])])
        exclusions_changed = [value.casefold() for value in cached_exclusions] != [value.casefold() for value in desired_exclusions]
        self.session.set_excluded_directories(desired_exclusions)
        self._restoring_state = False
        if from_cache:
            self._cache_dirty = False
            self._cache_generation = 0
            if exclusions_changed:
                self._mark_cache_dirty()
            self._refresh_after_cache = True
            self.status_label.setText("마지막 저장 트리 표시 완료 · 최신 변경 확인 준비 중…")
        else:
            self._mark_cache_dirty()

    def _apply_result(
        self,
        result: AnalysisResult,
        view: CallView,
        preserve_scroll: bool = True,
        mark_dirty: bool = False,
    ) -> None:
        self.result, self.view = result, view
        self.call_tree.set_view(view, preserve_scroll)
        self._refresh_main_combo(view)
        self._populate_files()
        self._refresh_selected_preview()
        clang_text = f"libclang {result.clang_files:,}개 파일 해석"
        database = "compile_commands.json 사용" if result.compile_database else "기본 C 옵션 사용"
        self.status_label.setText(
            f"완료: {len(result.files):,}개 파일 · {len(result.functions):,}개 함수 · "
            f"최대 {view.max_depth}단계 · {clang_text} · {database}"
        )
        if mark_dirty:
            self._mark_cache_dirty()

    def _refresh_main_combo(self, view: CallView) -> None:
        current = view.selected_main_id
        self._combo_refresh = True
        self.main_combo.clear()
        if not view.main_candidates:
            self.main_combo.addItem("main 시작점 없음", None)
        for function in view.main_candidates:
            self.main_combo.addItem(f"{function.name}() — {function.path}:{function.start_line}", function.id)
            if function.id == current:
                self.main_combo.setCurrentIndex(self.main_combo.count() - 1)
        self._combo_refresh = False

    def _selected_main(self) -> str | None:
        return self.main_combo.currentData()

    def _main_changed(self) -> None:
        if not self._combo_refresh:
            self._mark_cache_dirty()
            self._rebuild_view()

    def _rebuild_view(self) -> None:
        if not self.result or self.busy:
            return
        selected = self._selected_main()
        include_other = self.other_roots_action.isChecked()
        include_external = self.show_external_functions
        result = self.result

        def task(progress):
            return build_call_view(
                result,
                selected,
                include_other,
                include_external_calls=include_external,
            )

        self._start_job(task, lambda view: self._view_ready(view), False, "트리 다시 구성 중…")

    def _view_ready(self, view: CallView) -> None:
        if not self.result:
            return
        self.view = view
        self.call_tree.set_view(view, True)
        self._refresh_selected_preview()
        self.status_label.setText(f"트리 표시: {len(view.rows):,}행 · 최대 {view.max_depth}단계")
        self._mark_cache_dirty()

    def _search_move(self, direction: int, restart: bool) -> None:
        self.search_timer.stop()
        query = self.search_edit.text().strip()
        if not query or not self.view:
            self.search_count.setText("")
            return
        position, total = self.call_tree.find_text(query, direction, restart)
        self.search_count.setText(f"{position}/{total}" if total else "0/0")
        if total:
            self.status_label.setText(f"검색: '{query}' · {position}/{total}")
        else:
            self.status_label.setText(f"검색 결과 없음: '{query}'")

    def _mark_cache_dirty(self) -> None:
        if self._restoring_state or self._closing or not self.result:
            return
        self._cache_dirty = True
        self._cache_generation += 1

    def _project_ui_state(self) -> dict[str, object]:
        expanded_files = [
            self.file_tree.topLevelItem(index).data(0, Qt.UserRole)
            for index in range(self.file_tree.topLevelItemCount())
            if self.file_tree.topLevelItem(index).isExpanded()
        ]
        return {
            "selected_main": self._selected_main(),
            "include_other": self.other_roots_action.isChecked(),
            "show_external_functions": self.show_external_functions,
            "show_file_tree": self.file_action.isChecked(),
            "show_code_preview": self.source_action.isChecked(),
            "auto_check": self.auto_check.isChecked(),
            "search_text": self.search_edit.text(),
            "splitter_sizes": self.workspace_splitter.sizes(),
            "expanded_files": expanded_files,
            "file_scroll": self.file_tree.verticalScrollBar().value(),
            "file_horizontal_scroll": self.file_tree.horizontalScrollBar().value(),
            "source_scroll": self.source_view.verticalScrollBar().value(),
            "source_horizontal_scroll": self.source_view.horizontalScrollBar().value(),
            "call_tree": self.call_tree.export_state(),
            "excluded_folders": list(self.session.excluded_directories),
        }

    def _restore_project_ui(self, state: dict[str, object]) -> None:
        self.other_roots_action.setChecked(bool(state.get("include_other", False)))
        self.file_action.setChecked(bool(state.get("show_file_tree", self.file_action.isChecked())))
        self.source_action.setChecked(bool(state.get("show_code_preview", self.source_action.isChecked())))
        self.auto_check.setChecked(bool(state.get("auto_check", True)))
        self.search_edit.blockSignals(True)
        self.search_edit.setText(str(state.get("search_text", "")))
        self.search_edit.blockSignals(False)
        sizes = state.get("splitter_sizes", [])
        if isinstance(sizes, list) and len(sizes) == 3:
            self.workspace_splitter.setSizes([int(value) for value in sizes])
        expanded = {str(value).casefold() for value in state.get("expanded_files", [])}
        for index in range(self.file_tree.topLevelItemCount()):
            item = self.file_tree.topLevelItem(index)
            if str(item.data(0, Qt.UserRole)).casefold() in expanded:
                item.setExpanded(True)
        tree_state = state.get("call_tree")
        if isinstance(tree_state, dict):
            self.call_tree.restore_state(tree_state)
        search_position, search_total = self.call_tree.search_state()
        self.search_count.setText(f"{search_position}/{search_total}" if search_total else "")
        self.file_tree.verticalScrollBar().setValue(int(state.get("file_scroll", 0)))
        self.file_tree.horizontalScrollBar().setValue(int(state.get("file_horizontal_scroll", 0)))
        self.source_view.verticalScrollBar().setValue(int(state.get("source_scroll", 0)))
        self.source_view.horizontalScrollBar().setValue(int(state.get("source_horizontal_scroll", 0)))

    def _cache_saved(self, generation: int, path: object) -> None:
        if generation == self._cache_generation:
            self._cache_dirty = False

    def _cache_save_error(self, detail: str) -> None:
        self.status_label.setText("프로젝트 캐시 저장 실패 · 다음 주기 또는 종료 시 다시 시도합니다.")

    def _cache_worker_finished(self, worker: Worker) -> None:
        self._cache_workers.discard(worker)
        self._cache_saving = False
        if self._pending_manual_check and not self.busy and not self._closing:
            self._pending_manual_check = False
            QTimer.singleShot(0, lambda: self._check_updates(False))

    def _save_cache_async(self) -> None:
        if (
            not self._cache_dirty
            or self._cache_saving
            or self.busy
            or self._closing
            or not self.result
            or not self.session.root
        ):
            return
        generation = self._cache_generation
        root = self.session.root
        session_cache = self.session.cache
        result = self.result
        ui_state = self._project_ui_state()
        self._cache_saving = True

        def task(progress):
            return self.cache_store.save(root, session_cache, result, ui_state)

        worker = Worker(task, False)
        worker.signals.result.connect(lambda path, saved=generation: self._cache_saved(saved, path))
        worker.signals.error.connect(self._cache_save_error)
        worker.signals.finished.connect(lambda current=worker: self._cache_worker_finished(current))
        self._cache_workers.add(worker)
        self.cache_pool.start(worker)

    def _save_cache_now(self) -> None:
        if self._cache_saving:
            self.cache_pool.waitForDone()
            QApplication.processEvents()
        if not self._cache_dirty or not self.result or not self.session.root:
            return
        try:
            self.cache_store.save(
                self.session.root,
                self.session.cache,
                self.result,
                self._project_ui_state(),
            )
            self._cache_dirty = False
        except OSError:
            pass

    def _monitor_tick(self) -> None:
        if self.auto_check.isChecked() and self.result and not self.busy and not self._cache_saving:
            self._check_updates(True)

    def _check_updates(self, quiet: bool, force_view: bool = False) -> None:
        if not self.result:
            if not quiet:
                self.status_label.setText("먼저 분석할 폴더를 선택하십시오.")
            return
        if self.busy or self._cache_saving:
            if not quiet:
                self._pending_manual_check = True
                self.status_label.setText("진행 중인 작업 또는 캐시 저장 다음에 변경 확인을 실행합니다.")
            return
        selected = self._selected_main()
        include_other = self.other_roots_action.isChecked()
        include_external = self.show_external_functions

        def task(progress):
            result, changed, deleted = self.session.check_updates(None if quiet else progress)
            view = (
                build_call_view(
                    result,
                    selected,
                    include_other,
                    include_external_calls=include_external,
                )
                if changed or deleted or force_view else None
            )
            return result, view, changed, deleted

        self._start_job(task, self._updates_ready, not quiet, "수정 시각 확인 중…")

    def _updates_ready(self, payload: object) -> None:
        result, view, changed, deleted = payload
        last_change = self._last_change_date(result)
        if view is None:
            self.status_label.setText(
                f"자동 감시 중 · 변경 없음 · 2초 간격 - 마지막 변경 Date: {last_change}"
            )
            return
        self._apply_result(result, view, True, True)
        self.status_label.setText(
            f"자동 반영 완료 · 변경 {changed}개 · 삭제 {deleted}개 · "
            f"2초 간격 - 마지막 변경 Date: {last_change}"
        )

    @staticmethod
    def _last_change_date(result: AnalysisResult) -> str:
        latest = max((parsed.modified_ns for parsed in result.files), default=0)
        if latest <= 0:
            return "기록 없음"
        return datetime.fromtimestamp(latest / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")

    def _populate_files(self) -> None:
        self.file_tree.clear()
        if not self.result:
            return
        for parsed in self.result.files:
            item = QTreeWidgetItem([parsed.relative_path])
            item.setData(0, Qt.UserRole, parsed.path)
            item.setData(0, Qt.UserRole + 1, parsed.relative_path)
            item.setToolTip(0, parsed.path)
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator if parsed.functions else QTreeWidgetItem.DontShowIndicator)
            self.file_tree.addTopLevelItem(item)
        self._refresh_file_tree_paths()

    def _refresh_file_tree_paths(self) -> None:
        if not self.result or not hasattr(self, "file_tree"):
            return
        root_name = Path(self.result.root).name
        available = max(24, self.file_tree.viewport().width() - 34)
        metrics = self.file_tree.fontMetrics()
        items = [self.file_tree.topLevelItem(index) for index in range(self.file_tree.topLevelItemCount())]
        relatives = [str(item.data(0, Qt.UserRole + 1) or "") for item in items]
        full_paths = [
            "\\".join([root_name, *[part for part in relative.replace("/", "\\").split("\\") if part]])
            for relative in relatives
        ]
        show_full = bool(full_paths) and all(metrics.horizontalAdvance(path) <= available for path in full_paths)
        common_depth = 0
        if not show_full:
            maximum_depth = max(
                (max(0, len([part for part in relative.replace("/", "\\").split("\\") if part]) - 1) for relative in relatives),
                default=0,
            )
            for depth in range(maximum_depth, -1, -1):
                candidates: list[str] = []
                for relative in relatives:
                    parts = [part for part in relative.replace("/", "\\").split("\\") if part]
                    directories = parts[:-1]
                    visible = directories[-depth:] if depth > 0 else []
                    candidates.append("..\\" + ("\\".join(visible) + "\\" if visible else "") + parts[-1])
                if all(metrics.horizontalAdvance(candidate) <= available for candidate in candidates):
                    common_depth = depth
                    break
        for item, relative in zip(items, relatives):
            item.setText(0, compact_file_path(
                root_name,
                relative,
                available,
                metrics,
                directory_depth=common_depth,
                show_full=show_full,
            ))

    def _expand_file(self, item: QTreeWidgetItem) -> None:
        if item.parent() or item.childCount() or not self.result:
            return
        path = item.data(0, Qt.UserRole)
        parsed = next((value for value in self.result.files if value.path == path), None)
        if not parsed:
            return
        for function in parsed.functions:
            child = QTreeWidgetItem([f"{function.name}()  :{function.start_line}"])
            child.setData(0, Qt.UserRole, function.id)
            item.addChild(child)

    def _file_item_activated(self, item: QTreeWidgetItem) -> None:
        if item.parent():
            self._show_function(item.data(0, Qt.UserRole))

    def _set_function_summary(self, function) -> tuple[list[str], list[str]]:
        resolved: list[str] = []
        external: list[str] = []
        for call in function.calls:
            bucket = resolved if self.result and self.result.function(call.target_id) else external
            if call.name not in bucket:
                bucket.append(call.name)
        if not self.show_external_functions:
            external = []

        def names(values: list[str]) -> str:
            return ", ".join(f"{html.escape(value)}()" for value in values) if values else "없음"

        self.source_summary.setHtml(
            "<b>CODE 함수 구분</b><br>"
            f'<span style="background:#B7DDF7;color:#0B3D66;">&nbsp;부모(자기 자신)&nbsp;</span> '
            f"{html.escape(function.name)}()<br>"
            f'<span style="background:#CDECCF;color:#135C1F;">&nbsp;자식 호출(정의 확인)&nbsp;</span> '
            f"{names(resolved)}<br>"
            f'<span style="background:#FFE2A8;color:#8A5200;">&nbsp;외부/미확인 호출&nbsp;</span> '
            f"{names(external)}"
        )
        return resolved, external

    def _refresh_selected_preview(self) -> None:
        body = self.call_tree.body
        index = body._current_index
        if 0 <= index < len(body._view.rows):
            row = body._view.rows[index]
            if row.kind == "function":
                self._show_function(row.function_id or "")

    def _highlight_function_names(
        self,
        text: str,
        parent_name: str,
        resolved: list[str],
        external: list[str],
    ) -> None:
        selections: list[QTextEdit.ExtraSelection] = []

        def add(names: list[str], background: str, foreground: str, bold: bool = False) -> None:
            text_format = QTextCharFormat()
            text_format.setBackground(QColor(background))
            text_format.setForeground(QColor(foreground))
            if bold:
                text_format.setFontWeight(QFont.Bold)
            for name in names:
                pattern = re.compile(rf"\b{re.escape(name)}\b(?=\s*\()")
                for match in pattern.finditer(text):
                    cursor = QTextCursor(self.source_view.document())
                    cursor.setPosition(match.start())
                    cursor.setPosition(match.end(), QTextCursor.KeepAnchor)
                    selection = QTextEdit.ExtraSelection()
                    selection.cursor = cursor
                    selection.format = text_format
                    selections.append(selection)

        add(external, "#FFE2A8", "#8A5200")
        add(resolved, "#CDECCF", "#135C1F")
        add([parent_name], "#B7DDF7", "#0B3D66", True)
        self.source_view.setExtraSelections(selections)

    def _show_function(self, function_id: str) -> None:
        if not self.result:
            return
        function = self.result.function(function_id)
        if not function:
            self.source_summary.setHtml(
                '<b>CODE 함수 구분</b><br>'
                '<span style="background:#FFE2A8;color:#8A5200;">&nbsp;외부/미확인 함수 선택&nbsp;</span><br>'
                '현재 분석 폴더에서 함수 정의를 찾지 못했습니다.'
            )
            self.source_view.setPlainText(
                "CODE 미리보기\n\n"
                "외부 또는 미확인 함수입니다.\n"
                "현재 분석 폴더에서 함수 정의를 찾지 못해 표시할 소스 코드가 없습니다."
            )
            self.source_view.setExtraSelections([])
            return
        parsed = next((value for value in self.result.files if value.path == function.path), None)
        if not parsed:
            return
        lines = parsed.text.splitlines()
        start = max(0, function.start_line - 1)
        end = min(len(lines), function.end_line)
        numbered = "\n".join(f"{line_no:6d}  {lines[line_no - 1]}" for line_no in range(start + 1, end + 1))
        preview = f"{function.path}\n\n{numbered}"
        resolved, external = self._set_function_summary(function)
        self.source_view.setPlainText(preview)
        self._highlight_function_names(preview, function.name, resolved, external)

    def _export(self) -> None:
        if not self.result or not self.view:
            self.status_label.setText("먼저 소스 폴더를 분석하십시오.")
            return
        if self.busy:
            self.status_label.setText("진행 중인 분석이 끝난 후 Excel 출력을 다시 누르십시오.")
            return
        folder_name = re.sub(r'[<>:"/\\|?*]+', "_", Path(self.result.root).name).strip(" ._") or "C_프로젝트"
        default = str(unique_output_path(Path(self.result.root) / f"{folder_name}_함수호출트리.xlsx"))
        self.monitor_timer.stop()
        try:
            path, _ = QFileDialog.getSaveFileName(self, "Excel 트리 저장", default, "Excel 통합 문서 (*.xlsx)")
        finally:
            self.monitor_timer.start()
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        path = str(unique_output_path(path))
        result, view = self.result, self.view

        def task(progress):
            export_xlsx(
                path,
                result,
                view,
                lambda current, total, detail: progress("Excel 생성", current, total, detail),
            )
            return path

        self._start_job(task, self._excel_ready, True, "Excel 생성 중…")

    def _excel_ready(self, saved: str) -> None:
        self.status_label.setText(f"Excel 저장 완료 · 파일 여는 중: {saved}")
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(saved)):
            self.status_label.setText(f"Excel 저장 완료 · 자동 열기 실패: {saved}")

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "프로그램 정보",
            f"<b>{APP_NAME}</b><br>버전 {APP_VERSION}<br><br>"
            "C 소스 함수 호출 관계 분석 및 트리 탐색기<br><br>"
            f'<a href="{RELEASE_PAGE}">GitHub 릴리스 페이지</a>',
        )

    def _startup_update_check(self) -> None:
        if self._closing or not self.settings.value("update/checkOnStartup", True, type=bool):
            return
        if self.busy:
            QTimer.singleShot(15000, self._startup_update_check)
            return
        if self._offer_pending_update_retry():
            return
        last_check = float(self.settings.value("update/lastCheckEpoch", 0) or 0)
        if time.time() - last_check >= 24 * 60 * 60:
            self._check_program_update(False)

    def _start_update_task(
        self,
        task: Callable,
        callback: Callable,
        message: str,
        show_progress: bool = False,
    ) -> bool:
        if self._closing or self._update_workers:
            if not self._closing:
                self.status_label.setText("업데이트 작업이 이미 진행 중입니다.")
            return False
        worker = Worker(task, show_progress)
        worker.signals.result.connect(callback)
        worker.signals.progress.connect(
            lambda phase, current, total, detail: self._progress(phase, current, total, detail, show_progress)
        )
        worker.signals.error.connect(self._update_error)
        worker.signals.finished.connect(lambda current=worker: self._update_worker_finished(current, show_progress))
        self._update_workers.add(worker)
        self.status_label.setText(message)
        if show_progress:
            self.progress.setRange(0, 0)
            self.progress.show()
        self.update_pool.start(worker)
        return True

    def _update_worker_finished(self, worker: Worker, show_progress: bool) -> None:
        self._update_workers.discard(worker)
        if show_progress and not self.busy:
            self.progress.hide()

    def _update_error(self, detail: str) -> None:
        message = next(
            (line.strip() for line in reversed(detail.splitlines()) if "UpdateError:" in line),
            "업데이트 작업 중 오류가 발생했습니다.",
        )
        message = message.split("UpdateError:", 1)[-1].strip()
        self.status_label.setText("업데이트 확인 실패")
        QMessageBox.warning(
            self,
            "업데이트 오류",
            f"{message}\n\n인터넷 연결 또는 GitHub 접속 정책을 확인하십시오.",
        )

    def _check_program_update(self, manual: bool = True) -> None:
        if self.busy and manual:
            self.status_label.setText("분석 작업이 끝난 후 업데이트 확인을 다시 실행하십시오.")
            return

        def task(progress):
            return fetch_latest_release()

        def ready(release: ReleaseInfo) -> None:
            self.settings.setValue("update/lastCheckEpoch", time.time())
            if is_newer_version(release.version, APP_VERSION):
                if not manual and self.settings.value("update/skippedVersion", "") == release.version:
                    return
                self._offer_update(release)
            elif manual:
                self.status_label.setText(f"최신 버전 사용 중 · {APP_VERSION}")
                QMessageBox.information(
                    self,
                    "업데이트 확인",
                    f"현재 최신 버전을 사용하고 있습니다.\n\n현재 버전: {APP_VERSION}\n"
                    f"GitHub 최신 버전: {release.version}\n자산: {release.setup.name}",
                )

        self._start_update_task(task, ready, "GitHub에서 최신 버전 확인 중…")

    def _offer_update(self, release: ReleaseInfo) -> None:
        notes = release.notes.strip()
        if len(notes) > 1800:
            notes = notes[:1800].rstrip() + "…"
        box = QMessageBox(self)
        box.setWindowTitle("새 업데이트")
        box.setIcon(QMessageBox.Information)
        box.setText(f"{APP_NAME} {release.version} 버전을 사용할 수 있습니다.")
        box.setInformativeText(
            f"현재 버전: {APP_VERSION}\n게시 버전: {release.version}\n"
            f"설치 파일: {release.setup.name} ({release.setup.size / 1024 / 1024:.1f} MB)"
        )
        box.setDetailedText(notes or "릴리스 설명이 없습니다.")
        install = box.addButton("다운로드 및 설치", QMessageBox.AcceptRole)
        skip = box.addButton("이 버전 건너뛰기", QMessageBox.DestructiveRole)
        box.addButton("나중에", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is install:
            self._download_update(release)
        elif box.clickedButton() is skip:
            self.settings.setValue("update/skippedVersion", release.version)
            self.status_label.setText(f"버전 {release.version} 업데이트 알림을 건너뜁니다.")

    def _download_update(self, release: ReleaseInfo) -> None:
        def task(progress):
            return download_asset(
                release.setup,
                progress=lambda current, total: progress("업데이트 다운로드", current, total, release.setup.name),
            )

        self._start_update_task(
            task,
            lambda path: self._update_download_ready(path, release),
            "업데이트 설치 파일 준비 중…",
            True,
        )

    def _update_download_ready(self, path: Path, release: ReleaseInfo) -> None:
        self.settings.setValue("update/pendingInstaller", str(path))
        self.settings.setValue("update/pendingVersion", release.version)
        self.settings.setValue("update/pendingSize", release.setup.size)
        self.settings.setValue("update/pendingSha256", release.setup.sha256)
        self.settings.sync()
        self.status_label.setText(f"업데이트 검증 완료 · {path.name}")
        answer = QMessageBox.question(
            self,
            "업데이트 설치 준비 완료",
            "설치 파일의 크기와 SHA-256 검증이 완료되었습니다.\n\n"
            "프로그램을 종료하고 설치 프로그램을 실행하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        self._launch_update_installer(path)

    def _launch_update_installer(self, path: Path) -> bool:
        started = QProcess.startDetached(str(path), ["--wait-pid", str(QApplication.applicationPid())])
        success = started[0] if isinstance(started, tuple) else bool(started)
        if not success:
            QMessageBox.critical(self, "업데이트 실행 실패", f"설치 프로그램을 실행하지 못했습니다.\n{path}")
            return False
        self._save_cache_now()
        self._closing = True
        QApplication.quit()
        return True

    def _clear_pending_update(self) -> None:
        for key in ("pendingInstaller", "pendingVersion", "pendingSize", "pendingSha256"):
            self.settings.remove(f"update/{key}")

    def _offer_pending_update_retry(self) -> bool:
        version = str(self.settings.value("update/pendingVersion", "") or "")
        path_value = str(self.settings.value("update/pendingInstaller", "") or "")
        if not version or not path_value:
            return False
        try:
            if not is_newer_version(version, APP_VERSION):
                self._clear_pending_update()
                return False
        except UpdateError:
            self._clear_pending_update()
            return False
        path = Path(path_value)
        try:
            expected_size = int(self.settings.value("update/pendingSize", 0) or 0)
            expected_sha256 = str(self.settings.value("update/pendingSha256", "") or "")
            verify_downloaded_asset(path, expected_size, expected_sha256)
        except (UpdateError, TypeError, ValueError):
            self._clear_pending_update()
            self.settings.setValue("update/lastCheckEpoch", 0)
            return False
        answer = QMessageBox.question(
            self,
            "업데이트 설치 재시도",
            f"이전에 다운로드한 {version} 설치 파일이 남아 있습니다.\n"
            "이전 설치가 완료되지 않았다면 지금 다시 실행할 수 있습니다.\n\n"
            f"설치 파일: {path.name}\n\n다시 설치하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._launch_update_installer(path)
        return True

    def _test_update_download(self) -> None:
        answer = QMessageBox.question(
            self,
            "업데이트 다운로드 검증",
            "GitHub 최신 설치 파일을 실제로 다운로드하여 파일 크기와 SHA-256을 검증합니다.\n"
            "현재 릴리스 기준 약 69MB의 네트워크를 사용하며 설치는 하지 않습니다. 계속하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        def task(progress):
            release = fetch_latest_release()
            temporary = Path(tempfile.gettempdir()) / f"{release.setup.name}.verification-test"
            saved = download_asset(
                release.setup,
                temporary,
                lambda current, total: progress("업데이트 검증", current, total, release.setup.name),
            )
            saved.unlink(missing_ok=True)
            return release

        def ready(release: ReleaseInfo) -> None:
            self.settings.setValue("update/lastCheckEpoch", time.time())
            self.status_label.setText(f"업데이트 다운로드 검증 성공 · {release.tag}")
            QMessageBox.information(
                self,
                "업데이트 테스트 성공",
                f"GitHub 연결, 릴리스 조회, 전체 다운로드 및 SHA-256 검증이 모두 성공했습니다.\n\n"
                f"태그: {release.tag}\n파일: {release.setup.name}\n"
                f"크기: {release.setup.size:,} bytes\nSHA-256: {release.setup.sha256}",
            )

        self._start_update_task(task, ready, "업데이트 다운로드 검증 준비 중…", True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.monitor_timer.stop()
        self.search_timer.stop()
        self.cache_timer.stop()
        if self.busy:
            self.pool.waitForDone()
            QApplication.processEvents()
        self._save_cache_now()
        self._closing = True
        self.pool.clear()
        self.cache_pool.clear()
        self.update_pool.clear()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_PUBLISHER)
    app.setWindowIcon(QIcon(resource_path("assets/CallHierarchyExplorer.ico")))
    app.setStyle("Fusion")
    if "--smoke-test" in sys.argv:
        with tempfile.TemporaryDirectory() as temporary:
            settings_directory = Path(temporary) / "settings"
            QSettings.setDefaultFormat(QSettings.IniFormat)
            QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(settings_directory))
            source = Path(temporary) / "main.c"
            source.write_text("void child(void){}\nint main(void){child();}\n", encoding="utf-8")
            result = AnalyzerSession().initial_scan(temporary)
            view = build_call_view(result)
            if not result.functions or view.max_depth < 2:
                return 2
            window = MainWindow()
            window.close()
        return 0
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
