from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QProxyStyle,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


BUILT_IN_EXCLUDED_DIRS = {".git", ".svn", ".hg", "node_modules", "dist", "build", ".vs"}
PATH_ROLE = Qt.UserRole
RELATIVE_ROLE = Qt.UserRole + 1
LOADED_ROLE = Qt.UserRole + 2


class BrightCheckStyle(QProxyStyle):
    """Draw high-contrast tree checkboxes consistently across Windows themes."""

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802
        if element not in (QStyle.PE_IndicatorItemViewItemCheck, QStyle.PE_IndicatorCheckBox):
            super().drawPrimitive(element, option, painter, widget)
            return
        rect = option.rect.adjusted(1, 1, -1, -1)
        checked = bool(option.state & QStyle.State_On)
        partial = bool(option.state & QStyle.State_NoChange)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#AFC7D8"), 1))
        painter.setBrush(QColor("#1683C4") if checked or partial else QColor("#F2F5F7"))
        painter.drawRoundedRect(rect, 2, 2)
        painter.setPen(QPen(QColor("#FFFFFF"), 2))
        if checked:
            painter.drawLine(rect.left() + 3, rect.center().y(), rect.center().x() - 1, rect.bottom() - 3)
            painter.drawLine(rect.center().x() - 1, rect.bottom() - 3, rect.right() - 2, rect.top() + 3)
        elif partial:
            painter.drawLine(rect.left() + 3, rect.center().y(), rect.right() - 3, rect.center().y())
        painter.restore()


def normalize_exclusions(values: list[str]) -> list[str]:
    """Keep only the shallowest excluded folder for each excluded branch."""
    normalized: list[str] = []
    cleaned = {item.strip(r" \/") for item in values if item.strip(r" \/")}
    for value in sorted(cleaned, key=lambda item: (item.count("\\"), item.casefold())):
        folded = value.casefold()
        if any(folded == parent.casefold() or folded.startswith(parent.casefold() + "\\") for parent in normalized):
            continue
        normalized.append(value)
    return normalized


class FolderSelectionTree(QTreeWidget):
    """Lazy checkbox tree backed by a compact list of excluded directory roots."""

    def __init__(self, root: Path, excluded: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root_path = root
        self._excluded = normalize_exclusions(excluded)
        self._updating = True
        self.setHeaderLabels(["폴더", "분석 상태"])
        self.setColumnWidth(0, 430)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setMinimumHeight(300)
        self._bright_check_style = BrightCheckStyle()
        self.setStyle(self._bright_check_style)
        self.root_item = QTreeWidgetItem(self, [root.name or str(root), "포함"])
        self.root_item.setData(0, PATH_ROLE, str(root))
        self.root_item.setData(0, RELATIVE_ROLE, "")
        self.root_item.setData(0, LOADED_ROLE, False)
        self.root_item.setFlags(self.root_item.flags() | Qt.ItemIsUserCheckable)
        self.root_item.setToolTip(0, str(root))
        self._add_placeholder_if_needed(self.root_item, root)
        self._ensure_loaded(self.root_item)
        self.root_item.setExpanded(True)
        self.itemExpanded.connect(self._ensure_loaded)
        self.itemChanged.connect(self._item_changed)
        self._updating = False
        self._refresh_states()

    def excluded_folders(self) -> list[str]:
        return normalize_exclusions(self._excluded)

    def include_all(self) -> None:
        self._excluded = []
        self._refresh_states()

    def exclude_all_subfolders(self) -> None:
        self._excluded = [
            child.data(0, RELATIVE_ROLE)
            for child in self._real_children(self.root_item)
            if child.data(0, RELATIVE_ROLE)
        ]
        self._excluded = normalize_exclusions(self._excluded)
        self._refresh_states()

    @staticmethod
    def _real_children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        return [item.child(index) for index in range(item.childCount()) if item.child(index).data(0, PATH_ROLE)]

    @staticmethod
    def _directory_children(path: Path) -> list[Path]:
        try:
            children = [
                child for child in path.iterdir()
                if child.is_dir() and child.name.casefold() not in BUILT_IN_EXCLUDED_DIRS
            ]
        except OSError:
            return []
        return sorted(children, key=lambda child: child.name.casefold())

    def _add_placeholder_if_needed(self, item: QTreeWidgetItem, path: Path) -> None:
        if self._directory_children(path):
            QTreeWidgetItem(item, [""])

    def _ensure_loaded(self, item: QTreeWidgetItem) -> None:
        if bool(item.data(0, LOADED_ROLE)):
            return
        path_text = item.data(0, PATH_ROLE)
        if not path_text:
            return
        self._updating = True
        try:
            item.takeChildren()
            path = Path(path_text)
            for child_path in self._directory_children(path):
                try:
                    relative = str(child_path.relative_to(self.root_path)).replace("/", "\\")
                except ValueError:
                    continue
                child = QTreeWidgetItem(item, [child_path.name, "포함"])
                child.setData(0, PATH_ROLE, str(child_path))
                child.setData(0, RELATIVE_ROLE, relative)
                child.setData(0, LOADED_ROLE, False)
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setToolTip(0, str(child_path))
                self._add_placeholder_if_needed(child, child_path)
            item.setData(0, LOADED_ROLE, True)
        finally:
            self._updating = False
        self._refresh_states()

    def _state_for(self, relative: str) -> Qt.CheckState:
        if not relative:
            if not self._excluded:
                return Qt.Checked
            children = self._real_children(self.root_item)
            if children and all(self._state_for(str(child.data(0, RELATIVE_ROLE))) == Qt.Unchecked for child in children):
                return Qt.Unchecked
            return Qt.PartiallyChecked
        folded = relative.casefold()
        if any(folded == value.casefold() or folded.startswith(value.casefold() + "\\") for value in self._excluded):
            return Qt.Unchecked
        if any(value.casefold().startswith(folded + "\\") for value in self._excluded):
            return Qt.PartiallyChecked
        return Qt.Checked

    def _refresh_states(self) -> None:
        self._updating = True
        try:
            stack = [self.root_item]
            while stack:
                item = stack.pop()
                relative = str(item.data(0, RELATIVE_ROLE) or "")
                state = self._state_for(relative)
                item.setCheckState(0, state)
                item.setText(1, "포함" if state == Qt.Checked else "일부 포함" if state == Qt.PartiallyChecked else "제외")
                stack.extend(self._real_children(item))
        finally:
            self._updating = False

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating or column != 0:
            return
        relative = str(item.data(0, RELATIVE_ROLE) or "")
        state = item.checkState(0)
        if not relative:
            if state == Qt.Checked:
                self._excluded = []
            elif state == Qt.Unchecked:
                self.exclude_all_subfolders()
                return
            self._refresh_states()
            return

        folded = relative.casefold()
        if state == Qt.Unchecked:
            self._excluded = normalize_exclusions([
                value for value in self._excluded
                if not value.casefold().startswith(folded + "\\")
            ] + [relative])
        else:
            self._include_branch(relative)
        self._refresh_states()

    def _include_branch(self, relative: str) -> None:
        """Include a branch, splitting an excluded ancestor into excluded siblings."""
        folded = relative.casefold()
        ancestor = next(
            (value for value in self._excluded if folded == value.casefold() or folded.startswith(value.casefold() + "\\")),
            None,
        )
        self._excluded = [
            value for value in self._excluded
            if value.casefold() != folded and not value.casefold().startswith(folded + "\\")
        ]
        if ancestor and ancestor.casefold() != folded:
            self._excluded = [value for value in self._excluded if value.casefold() != ancestor.casefold()]
            target_parts = relative.split("\\")
            ancestor_parts = ancestor.split("\\")
            current = self.root_path.joinpath(*ancestor_parts)
            for depth in range(len(ancestor_parts), len(target_parts)):
                wanted = target_parts[depth]
                for sibling in self._directory_children(current):
                    if sibling.name.casefold() != wanted.casefold():
                        sibling_rel = str(sibling.relative_to(self.root_path)).replace("/", "\\")
                        self._excluded.append(sibling_rel)
                current /= wanted
        self._excluded = normalize_exclusions(self._excluded)


class ProjectSettingsDialog(QDialog):
    def __init__(
        self,
        root: str,
        excluded_folders: list[str],
        parent: QWidget | None = None,
        show_external_functions: bool = True,
    ) -> None:
        super().__init__(parent)
        self.root = Path(root).resolve()
        self.setFont(QFont("Malgun Gothic", 9))
        self.setWindowTitle("설정 — C Call Hierarchy Explorer")
        self.resize(980, 680)
        self.setMinimumSize(760, 520)
        self.setStyleSheet("""
            QDialog { background:#181818; color:#CCCCCC; }
            QDialog QWidget { color:#CCCCCC; }
            QLineEdit { background:#1F1F1F; border:1px solid #3C3C3C; color:#DDDDDD; padding:7px;
                        selection-background-color:rgba(22,131,216,145); selection-color:#FFFFFF; }
            QLineEdit:focus { border:1px solid #007ACC; }
            QLineEdit:disabled { background:#202020; color:#858585; }
            QListWidget { background:#181818; border:0; color:#CCCCCC; outline:0; }
            QListWidget::item { padding:8px 10px; }
            QListWidget::item:hover { background:rgba(70,70,75,115); color:#FFFFFF; }
            QListWidget::item:selected { background:rgba(22,131,216,105); color:#FFFFFF; }
            QTreeWidget { background:#181818; color:#CCCCCC; border:1px solid #303030; alternate-background-color:#1D1D1D; outline:0; }
            QTreeWidget::item { min-height:23px; }
            QTreeWidget::item:hover { background:rgba(70,70,75,105); color:#FFFFFF; }
            QTreeWidget::item:selected { background:rgba(22,131,216,90); color:#FFFFFF; }
            QHeaderView::section { background:#252526; color:#CCCCCC; border:0; border-right:1px solid #3C3C3C; padding:6px; }
            QFrame#settingCard { background:#1F1F1F; border:1px solid #303030; border-radius:5px; }
            QLabel#pageTitle { color:#F2F2F2; font-size:25px; font-weight:600; }
            QLabel#settingTitle { color:#E7E7E7; font-size:15px; font-weight:600; }
            QLabel#description { color:#AFAFAF; font-size:12px; }
            QLabel#rootPath { color:#75BEFF; background:#252526; padding:8px; border-radius:3px; }
            QCheckBox { color:#E7E7E7; spacing:7px; }
            QCheckBox:disabled { color:#858585; }
            QPushButton { background:#2D2D30; color:#EEEEEE; border:1px solid #454545; padding:6px 12px; }
            QPushButton:hover { background:#3A3A3D; }
            QPushButton:disabled { background:#252526; color:#777777; border-color:#353535; }
            QPushButton#primary { background:#0E639C; border-color:#0E639C; color:#FFFFFF; }
            QPushButton#primary:hover { background:#1177BB; }
            QToolTip { background:#252526; color:#F2F2F2; border:1px solid #5A5A5A; padding:4px; }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("설정 검색 (예: 분석 범위, 외부 함수)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_settings)
        # Settings search is an instant filter. Enter must never activate an
        # unrelated button such as "모두 선택" or "적용".
        self.search.returnPressed.connect(self.search.selectAll)
        outer.addWidget(self.search)

        body = QHBoxLayout()
        body.setSpacing(20)
        self.categories = QListWidget()
        self.categories.setFixedWidth(190)
        self.categories.addItems(["프로젝트", "  분석 범위"])
        self.categories.setCurrentRow(1)
        body.addWidget(self.categories)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 4, 10, 4)
        self.page_title = QLabel("프로젝트 분석 설정")
        self.page_title.setObjectName("pageTitle")
        content_layout.addWidget(self.page_title)
        content_layout.addSpacing(12)

        self.scope_card = QFrame()
        self.scope_card.setObjectName("settingCard")
        card_layout = QVBoxLayout(self.scope_card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        setting_title = QLabel("C 분석: 포함할 하위 폴더")
        setting_title.setObjectName("settingTitle")
        description = QLabel(
            "체크한 폴더의 .c/.h 파일만 함수 목록, 호출 트리, 파일/함수 보기, CODE 미리보기 및 자동 감시에 포함합니다.\n"
            "체크를 해제하면 해당 폴더와 모든 하위 폴더가 분석에서 제외됩니다. 설정은 프로젝트별로 저장됩니다."
        )
        description.setObjectName("description")
        description.setWordWrap(True)
        root_label = QLabel(f"메인 폴더  {self.root}")
        root_label.setObjectName("rootPath")
        root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card_layout.addWidget(setting_title)
        card_layout.addWidget(description)
        card_layout.addSpacing(8)
        card_layout.addWidget(root_label)

        self.folder_tree = FolderSelectionTree(self.root, excluded_folders)
        tree_palette = self.folder_tree.palette()
        translucent_selection = QColor(22, 131, 216, 90)
        for group in (QPalette.Active, QPalette.Inactive):
            tree_palette.setColor(group, QPalette.Highlight, translucent_selection)
            tree_palette.setColor(group, QPalette.HighlightedText, QColor("#FFFFFF"))
        self.folder_tree.setPalette(tree_palette)
        card_layout.addWidget(self.folder_tree, 1)
        self.external_check = QCheckBox("외부/미확인 함수 표시")
        self._external_check_style = BrightCheckStyle()
        self.external_check.setStyle(self._external_check_style)
        external_palette = self.external_check.palette()
        external_palette.setColor(QPalette.Active, QPalette.WindowText, QColor("#E7E7E7"))
        external_palette.setColor(QPalette.Inactive, QPalette.WindowText, QColor("#E7E7E7"))
        external_palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#858585"))
        self.external_check.setPalette(external_palette)
        self.external_check.setChecked(show_external_functions)
        self.external_check.setToolTip(
            "현재 분석 범위에서 정의를 찾지 못한 호출을 함수 호출 트리와 CODE 함수 구분에 표시합니다."
        )
        card_layout.addWidget(self.external_check)
        controls = QHBoxLayout()
        include_all = QPushButton("모두 선택")
        include_all.setObjectName("primary")
        include_all.setAutoDefault(False)
        include_all.setDefault(False)
        include_all.clicked.connect(self.folder_tree.include_all)
        exclude_all = QPushButton("모두 해제")
        exclude_all.setAutoDefault(False)
        exclude_all.setDefault(False)
        exclude_all.clicked.connect(self.folder_tree.exclude_all_subfolders)
        controls.addWidget(include_all)
        controls.addWidget(exclude_all)
        controls.addStretch(1)
        card_layout.addLayout(controls)
        content_layout.addWidget(self.scope_card, 1)

        self.no_results = QFrame()
        no_results_layout = QVBoxLayout(self.no_results)
        no_results_layout.setContentsMargins(20, 40, 20, 40)
        no_results_layout.addStretch(1)
        no_results_title = QLabel("일치하는 설정이 없습니다")
        no_results_title.setAlignment(Qt.AlignCenter)
        no_results_title.setStyleSheet("color:#F2F2F2; font-size:19px; font-weight:600;")
        self.no_results_detail = QLabel("")
        self.no_results_detail.setAlignment(Qt.AlignCenter)
        self.no_results_detail.setWordWrap(True)
        self.no_results_detail.setStyleSheet("color:#AFAFAF; font-size:12px;")
        clear_search = QPushButton("검색어 지우기")
        clear_search.setAutoDefault(False)
        clear_search.setDefault(False)
        clear_search.clicked.connect(self.search.clear)
        clear_row = QHBoxLayout()
        clear_row.addStretch(1)
        clear_row.addWidget(clear_search)
        clear_row.addStretch(1)
        no_results_layout.addWidget(no_results_title)
        no_results_layout.addSpacing(8)
        no_results_layout.addWidget(self.no_results_detail)
        no_results_layout.addSpacing(14)
        no_results_layout.addLayout(clear_row)
        no_results_layout.addStretch(2)
        self.no_results.hide()
        content_layout.addWidget(self.no_results, 1)
        body.addWidget(content, 1)
        outer.addLayout(body, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("취소")
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        cancel_button.clicked.connect(self.reject)
        apply_button = QPushButton("적용")
        apply_button.setObjectName("primary")
        apply_button.setAutoDefault(False)
        apply_button.setDefault(False)
        apply_button.clicked.connect(self.accept)
        footer.addWidget(cancel_button)
        footer.addWidget(apply_button)
        outer.addLayout(footer)

    def excluded_folders(self) -> list[str]:
        return self.folder_tree.excluded_folders()

    def show_external_functions(self) -> bool:
        return self.external_check.isChecked()

    def _filter_settings(self, text: str) -> None:
        query = text.strip().casefold()
        keywords = "프로젝트 분석 범위 포함 제외 하위 폴더 c h 소스 함수 트리 외부 미확인 함수 표시 자동 감시 화면"
        terms = [term for term in query.split() if term]
        matched = not terms or all(term in keywords.casefold() for term in terms)
        self.scope_card.setVisible(matched)
        self.page_title.setVisible(matched)
        self.no_results.setVisible(not matched)
        if not matched:
            self.no_results_detail.setText(
                f"‘{text.strip()}’에 해당하는 설정을 찾지 못했습니다.\n"
                "다른 검색어를 입력하거나 검색어를 지워 전체 설정을 표시하십시오."
            )
