from __future__ import annotations

import bisect

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QToolTip, QVBoxLayout, QWidget

from analyzer import CallView, ViewRow


COLUMN_WIDTH = 285
FUNCTION_HEIGHT = 28
SECTION_HEIGHT = 36
SPACER_HEIGHT = 12


class StickyHeader(QWidget):
    depthClicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(36)
        self._max_depth = 1
        self._offset = 0
        self._path: tuple[str, ...] = ()
        self._actions: tuple[str, ...] = ()
        self.setFont(QFont("Malgun Gothic", 9))

    def update_state(
        self,
        max_depth: int,
        offset: int,
        path: tuple[str, ...],
        actions: tuple[str, ...] | None = None,
    ) -> None:
        self._max_depth = max(1, max_depth)
        self._offset = offset
        self._path = path
        if actions is not None:
            self._actions = actions
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#D7E5EF"))
        painter.setPen(QPen(QColor("#9EB1C0"), 1))
        for depth in range(self._max_depth):
            x = depth * COLUMN_WIDTH - self._offset
            rect = QRect(x, 0, COLUMN_WIDTH, self.height())
            painter.fillRect(rect, QColor("#CBDDE9" if depth % 2 else "#D7E5EF"))
            painter.drawLine(x + COLUMN_WIDTH - 1, 0, x + COLUMN_WIDTH - 1, self.height())
            painter.setPen(QColor("#17324D"))
            title = f"{depth + 1}단계"
            name = f"  {self._path[depth]}()" if depth < len(self._path) else ""
            action = self._actions[depth] if depth < len(self._actions) else ""
            action_text = f"모두 {action}" if action else ""
            action_width = painter.fontMetrics().horizontalAdvance(action_text) + 16 if action_text else 0
            if action_text:
                action_rect = QRect(x + COLUMN_WIDTH - action_width - 7, 7, action_width, self.height() - 14)
                painter.setBrush(QColor("#AFC8D8"))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(action_rect, 4, 4)
                painter.setPen(QColor("#17324D"))
                painter.drawText(action_rect, Qt.AlignCenter, action_text)
            available = COLUMN_WIDTH - 16 - action_width - (8 if action_width else 0)
            text = painter.fontMetrics().elidedText(title + name, Qt.ElideRight, max(20, available))
            painter.setPen(QColor("#17324D"))
            painter.drawText(x + 8, 0, max(20, available), self.height(), Qt.AlignVCenter | Qt.AlignLeft, text)
            painter.setPen(QPen(QColor("#9EB1C0"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            depth = int((event.position().x() + self._offset) / COLUMN_WIDTH) + 1
            if 1 <= depth <= len(self._actions) and self._actions[depth - 1]:
                self.depthClicked.emit(depth)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        depth = int((event.position().x() + self._offset) / COLUMN_WIDTH) + 1
        actionable = 1 <= depth <= len(self._actions) and bool(self._actions[depth - 1])
        self.setCursor(Qt.PointingHandCursor if actionable else Qt.ArrowCursor)
        if actionable:
            action = self._actions[depth - 1]
            self.setToolTip(f"{depth}단계의 모든 하위 트리를 {action}합니다.")
        else:
            self.setToolTip("")
        super().mouseMoveEvent(event)


class VirtualCallBody(QAbstractScrollArea):
    functionActivated = Signal(str)
    pathChanged = Signal(tuple)
    horizontalChanged = Signal(int)
    depthStateChanged = Signal(tuple)
    stateChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view = CallView([], 1, [], None, 0)
        self._base_view = self._view
        self._all_rows: list[ViewRow] = []
        self._visible_source_indices: list[int] = []
        self._collapsed: set[str] = set()
        self._children: dict[int, list[int]] = {}
        self._subtree_end: dict[int, int] = {}
        self._index_by_key: dict[str, int] = {}
        self._merged_indices: list[int] = []
        self._source_subtree_end: list[int] = []
        self._source_child_keys_by_depth: dict[int, set[str]] = {}
        self._search_query = ""
        self._search_keys: list[str] = []
        self._search_position = -1
        self._selected_key = ""
        self._current_index = -1
        self._offsets = [0]
        self._total_height = 0
        self.setFont(QFont("Consolas", 9))
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self.horizontalScrollBar().valueChanged.connect(self._horizontal_changed)
        self.verticalScrollBar().setSingleStep(FUNCTION_HEIGHT)

    @staticmethod
    def _row_height(row: ViewRow) -> int:
        if row.kind == "section":
            return SECTION_HEIGHT
        if row.kind == "spacer":
            return SPACER_HEIGHT
        return FUNCTION_HEIGHT

    def set_view(self, view: CallView, preserve_scroll: bool = True) -> None:
        old_y = self.verticalScrollBar().value() if preserve_scroll else 0
        old_x = self.horizontalScrollBar().value() if preserve_scroll else 0
        old_collapsed = set(self._collapsed) if preserve_scroll else set()
        old_selected = self._selected_key if preserve_scroll else ""
        self._base_view = view
        self._all_rows = view.rows
        self._search_query = ""
        self._search_keys = []
        self._search_position = -1
        self._build_source_index()
        child_keys = set().union(*self._source_child_keys_by_depth.values()) if self._source_child_keys_by_depth else set()
        self._collapsed = old_collapsed & child_keys
        self._rebuild_visible_rows()
        self._select_by_key(old_selected, False)
        self._restore_scroll(old_x, old_y)

    def export_state(self) -> dict[str, object]:
        return {
            "collapsed": sorted(self._collapsed),
            "selected_key": self._selected_key,
            "horizontal": self.horizontalScrollBar().value(),
            "vertical": self.verticalScrollBar().value(),
            "search_query": self._search_query,
            "search_position": self._search_position,
        }

    def restore_state(self, state: dict[str, object]) -> None:
        child_keys = set().union(*self._source_child_keys_by_depth.values()) if self._source_child_keys_by_depth else set()
        self._collapsed = set(state.get("collapsed", [])) & child_keys
        self._rebuild_visible_rows()
        selected_key = str(state.get("selected_key", ""))
        self._select_by_key(selected_key, True)
        self._search_query = str(state.get("search_query", "")).casefold()
        self._search_keys = [
            row.node_key
            for row in self._all_rows
            if self._search_query
            and row.kind == "function"
            and row.node_key
            and self._matches_search(row, self._search_query)
        ]
        saved_position = int(state.get("search_position", -1))
        if selected_key in self._search_keys:
            self._search_position = self._search_keys.index(selected_key)
        elif self._search_keys:
            self._search_position = max(0, min(saved_position, len(self._search_keys) - 1))
        else:
            self._search_position = -1
        self._restore_scroll(int(state.get("horizontal", 0)), int(state.get("vertical", 0)))

    def search_state(self) -> tuple[int, int]:
        return (self._search_position + 1, len(self._search_keys)) if self._search_keys else (0, 0)

    @staticmethod
    def _matches_search(row: ViewRow, query: str) -> bool:
        """함수 셀에 실제 표시되는 함수명/파일명만 검색한다."""
        if query in row.name.casefold():
            return True
        if row.state in {"external", "cycle", "safety_limit"}:
            return False
        # 일반 노드의 call_file은 화면에 보이지 않는 내부 호출자 정보다. 이를
        # 검색하면 호출자 파일의 모든 자식 함수가 거짓 결과로 포함된다.
        displayed_file = row.call_file if len(row.call_lines) > 1 and row.call_file else row.file
        return bool(displayed_file and query in displayed_file.casefold())

    def _restore_scroll(self, old_x: int, old_y: int) -> None:
        self._update_ranges()
        self.verticalScrollBar().setValue(min(old_y, self.verticalScrollBar().maximum()))
        self.horizontalScrollBar().setValue(min(old_x, self.horizontalScrollBar().maximum()))
        self._emit_path()
        self.viewport().update()

    def _build_source_index(self) -> None:
        self._source_subtree_end = list(range(len(self._all_rows)))
        self._source_child_keys_by_depth = {}
        open_nodes: list[tuple[int, int]] = []
        last_function = -1
        for index, row in enumerate(self._all_rows):
            if row.kind != "function":
                while open_nodes:
                    _, parent = open_nodes.pop()
                    self._source_subtree_end[parent] = max(parent, last_function)
                last_function = -1
                continue
            while open_nodes and open_nodes[-1][0] >= row.depth:
                _, parent = open_nodes.pop()
                self._source_subtree_end[parent] = max(parent, last_function)
            open_nodes.append((row.depth, index))
            last_function = index
        while open_nodes:
            _, parent = open_nodes.pop()
            self._source_subtree_end[parent] = max(parent, last_function)
        for index, row in enumerate(self._all_rows):
            if row.kind == "function" and row.node_key and self._source_subtree_end[index] > index:
                self._source_child_keys_by_depth.setdefault(row.depth, set()).add(row.node_key)

    def _rebuild_visible_rows(self) -> None:
        visible_rows: list[ViewRow] = []
        visible_indices: list[int] = []
        index = 0
        while index < len(self._all_rows):
            row = self._all_rows[index]
            visible_rows.append(row)
            visible_indices.append(index)
            if row.kind == "function" and row.node_key in self._collapsed:
                index = self._source_subtree_end[index] + 1
                continue
            index += 1
        self._visible_source_indices = visible_indices
        self._view = CallView(
            visible_rows,
            self._base_view.max_depth,
            self._base_view.main_candidates,
            self._base_view.selected_main_id,
            self._base_view.interrupt_roots,
        )
        self._offsets = [0]
        for row in visible_rows:
            self._offsets.append(self._offsets[-1] + self._row_height(row))
        self._total_height = self._offsets[-1]
        self._build_connections()
        self._emit_depth_states()

    def _emit_depth_states(self) -> None:
        states: list[str] = []
        for depth in range(1, self._base_view.max_depth + 1):
            keys = self._source_child_keys_by_depth.get(depth, set())
            if not keys:
                states.append("")
            elif keys.issubset(self._collapsed):
                states.append("펼치기")
            else:
                states.append("접기")
        self.depthStateChanged.emit(tuple(states))

    def toggle_depth(self, depth: int) -> None:
        keys = self._source_child_keys_by_depth.get(depth, set())
        if not keys:
            return
        old_x = self.horizontalScrollBar().value()
        old_y = self.verticalScrollBar().value()
        selected = self._selected_key
        if keys.issubset(self._collapsed):
            self._collapsed.difference_update(keys)
        else:
            self._collapsed.update(keys)
        self._rebuild_visible_rows()
        self._select_by_key(selected, False)
        self._restore_scroll(old_x, old_y)
        self.stateChanged.emit()

    def _build_connections(self) -> None:
        self._children = {}
        self._subtree_end = {}
        self._index_by_key = {}
        parents: dict[int, int] = {}
        open_nodes: list[tuple[int, int]] = []
        last_function = -1
        for visible_index, row in enumerate(self._view.rows):
            if row.kind == "section":
                parents.clear()
                while open_nodes:
                    _, parent_index = open_nodes.pop()
                    self._subtree_end[parent_index] = max(parent_index, last_function)
                last_function = -1
                continue
            if row.kind != "function":
                while open_nodes:
                    _, parent_index = open_nodes.pop()
                    self._subtree_end[parent_index] = max(parent_index, last_function)
                last_function = -1
                continue
            while open_nodes and open_nodes[-1][0] >= row.depth:
                _, parent_index = open_nodes.pop()
                self._subtree_end[parent_index] = max(parent_index, last_function)
            for depth in [key for key in parents if key >= row.depth]:
                parents.pop(depth, None)
            if row.depth > 1 and row.depth - 1 in parents:
                parent = parents[row.depth - 1]
                self._children.setdefault(parent, []).append(visible_index)
            parents[row.depth] = visible_index
            if row.node_key:
                self._index_by_key[row.node_key] = visible_index
            open_nodes.append((row.depth, visible_index))
            last_function = visible_index
        while open_nodes:
            _, parent_index = open_nodes.pop()
            self._subtree_end[parent_index] = max(parent_index, last_function)
        self._merged_indices = sorted(index for index, children in self._children.items() if children)

    def _cell_rect(self, index: int, x_offset: int = 0, y_offset: int = 0) -> QRect:
        row = self._view.rows[index]
        end = self._subtree_end.get(index, index) if self._children.get(index) else index
        left = (row.depth - 1) * COLUMN_WIDTH - x_offset
        top = self._offsets[index] - y_offset
        bottom = self._offsets[end + 1] - y_offset
        return QRect(left, top, COLUMN_WIDTH, max(1, bottom - top))

    def _cell_index_at(self, x: int, y: int) -> int:
        row_index = self._row_index_at(self.verticalScrollBar().value() + y)
        if row_index < 0 or row_index >= len(self._view.rows):
            return -1
        row = self._view.rows[row_index]
        if row.kind != "function":
            return -1
        target_depth = int((x + self.horizontalScrollBar().value()) / COLUMN_WIDTH) + 1
        if target_depth < 1 or target_depth > row.depth:
            return -1
        current = row
        current_index = row_index
        while current.depth > target_depth and current.parent_key:
            current_index = self._index_by_key.get(current.parent_key, -1)
            if current_index < 0:
                return -1
            current = self._view.rows[current_index]
        return current_index if current.depth == target_depth else -1

    @staticmethod
    def _display_text(row: ViewRow) -> str:
        status = ""
        if row.state == "external":
            status = "  외부/미확인"
        elif row.state == "cycle":
            status = "  순환 호출"
        elif row.state == "safety_limit":
            status = "  안전 제한"
        elif len(row.call_lines) > 1 and row.call_file:
            status = f"  {row.call_file}:{', '.join(map(str, row.call_lines))}"
        elif row.file:
            status = f"  {row.file}:{row.line}"
        return row.name + "()" + status

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.ToolTip and self._view.rows:
            position = event.pos()
            index = self._cell_index_at(position.x(), position.y())
            if 0 <= index < len(self._view.rows):
                row = self._view.rows[index]
                if row.kind == "function":
                    rect = self._cell_rect(
                        index,
                        self.horizontalScrollBar().value(),
                        self.verticalScrollBar().value(),
                    )
                    QToolTip.showText(event.globalPos(), self._display_text(row), self.viewport(), rect)
                    return True
            QToolTip.hideText()
            event.ignore()
            return True
        return super().viewportEvent(event)

    def _source_has_children(self, source_index: int) -> bool:
        return (
            0 <= source_index < len(self._source_subtree_end)
            and self._source_subtree_end[source_index] > source_index
        )

    def _toggle_row(self, visible_index: int) -> bool:
        if visible_index < 0 or visible_index >= len(self._visible_source_indices):
            return False
        source_index = self._visible_source_indices[visible_index]
        if not self._source_has_children(source_index):
            return False
        old_x = self.horizontalScrollBar().value()
        old_y = self.verticalScrollBar().value()
        key = self._all_rows[source_index].node_key
        if key in self._collapsed:
            self._collapsed.remove(key)
        else:
            self._collapsed.add(key)
        self._rebuild_visible_rows()
        self._select_by_key(key, False)
        self._restore_scroll(old_x, old_y)
        self.stateChanged.emit()
        return True

    def _child_toggle_state(self, parent_index: int) -> tuple[list[str], str]:
        keys: list[str] = []
        for child_index in self._children.get(parent_index, []):
            source_index = self._visible_source_indices[child_index]
            row = self._view.rows[child_index]
            if row.node_key and self._source_has_children(source_index):
                keys.append(row.node_key)
        if not keys:
            return [], ""
        action = "펼치기" if all(key in self._collapsed for key in keys) else "접기"
        return keys, action

    def _child_control_rect(self, parent_index: int, x_offset: int, y_offset: int) -> QRect:
        row = self._view.rows[parent_index]
        left = row.depth * COLUMN_WIDTH - x_offset + 38
        top = self._offsets[parent_index] - y_offset + 4
        return QRect(left, top, 92, FUNCTION_HEIGHT - 8)

    def _toggle_child_subtrees(self, parent_index: int) -> bool:
        keys, action = self._child_toggle_state(parent_index)
        if not keys:
            return False
        old_x = self.horizontalScrollBar().value()
        old_y = self.verticalScrollBar().value()
        selected = self._selected_key
        if action == "펼치기":
            self._collapsed.difference_update(keys)
        else:
            self._collapsed.update(keys)
        self._rebuild_visible_rows()
        self._select_by_key(selected, False)
        self._restore_scroll(old_x, old_y)
        self.stateChanged.emit()
        return True

    def _select_by_key(self, key: str, activate: bool, ensure_visible: bool = False) -> None:
        target = self._index_by_key.get(key, -1) if key else -1
        ancestor_key = key
        while target < 0 and "/" in ancestor_key:
            ancestor_key = ancestor_key.rsplit("/", 1)[0]
            target = self._index_by_key.get(ancestor_key, -1)
        if target < 0:
            target = next((index for index, row in enumerate(self._view.rows) if row.kind == "function"), -1)
        self._set_current(target, activate, ensure_visible)

    def find_text(self, text: str, direction: int = 1, restart: bool = False) -> tuple[int, int]:
        """현재 전체 트리에서 함수/파일을 찾아 필요한 부모 경로만 펼친다."""
        query = text.strip().casefold()
        if not query:
            self._search_query = ""
            self._search_keys = []
            self._search_position = -1
            return 0, 0
        if query != self._search_query:
            self._search_query = query
            self._search_keys = [
                row.node_key
                for row in self._all_rows
                if row.kind == "function"
                and row.node_key
                and self._matches_search(row, query)
            ]
            self._search_position = -1
            restart = True
        if not self._search_keys:
            return 0, 0
        if restart:
            self._search_position = 0 if direction >= 0 else len(self._search_keys) - 1
        else:
            self._search_position = (self._search_position + (1 if direction >= 0 else -1)) % len(self._search_keys)
        key = self._search_keys[self._search_position]

        # 대상 자신과 그 하위의 접기 상태는 건드리지 않고 부모 경로만 연다.
        parent_key = key.rsplit("/", 1)[0] if "/" in key else ""
        changed = False
        while parent_key:
            if parent_key in self._collapsed:
                self._collapsed.remove(parent_key)
                changed = True
            parent_key = parent_key.rsplit("/", 1)[0] if "/" in parent_key else ""
        if changed:
            self._rebuild_visible_rows()
            self.stateChanged.emit()
        self._select_by_key(key, True, True)
        self._emit_path()
        return self._search_position + 1, len(self._search_keys)

    def _set_current(self, index: int, activate: bool = True, ensure_visible: bool = True) -> None:
        if index < 0 or index >= len(self._view.rows) or self._view.rows[index].kind != "function":
            return
        previous_key = self._selected_key
        self._current_index = index
        row = self._view.rows[index]
        self._selected_key = row.node_key
        if ensure_visible:
            top = self._offsets[index]
            bottom = self._offsets[index + 1]
            vertical = self.verticalScrollBar()
            if top < vertical.value():
                vertical.setValue(top)
            elif bottom > vertical.value() + self.viewport().height():
                vertical.setValue(bottom - self.viewport().height())
            left = (row.depth - 1) * COLUMN_WIDTH
            right = left + COLUMN_WIDTH
            horizontal = self.horizontalScrollBar()
            if left < horizontal.value():
                horizontal.setValue(left)
            elif right > horizontal.value() + self.viewport().width():
                horizontal.setValue(right - self.viewport().width())
        if activate:
            self.functionActivated.emit(row.function_id or "")
        if row.node_key != previous_key:
            self.stateChanged.emit()
        self.viewport().update()

    def _adjacent_function(self, start: int, direction: int) -> int:
        index = start + direction
        while 0 <= index < len(self._view.rows):
            if self._view.rows[index].kind == "function":
                return index
            index += direction
        return start

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._current_index < 0:
            self._select_by_key("", True)
            event.accept()
            return
        row = self._view.rows[self._current_index]
        if event.key() == Qt.Key_Up:
            self._set_current(self._adjacent_function(self._current_index, -1))
        elif event.key() == Qt.Key_Down:
            self._set_current(self._adjacent_function(self._current_index, 1))
        elif event.key() == Qt.Key_Right:
            source_index = self._visible_source_indices[self._current_index]
            if self._source_has_children(source_index) and row.node_key in self._collapsed:
                self._toggle_row(self._current_index)
            else:
                children = self._children.get(self._current_index, [])
                if children:
                    self._set_current(children[0])
        elif event.key() == Qt.Key_Left:
            source_index = self._visible_source_indices[self._current_index]
            if self._source_has_children(source_index) and row.node_key not in self._collapsed:
                self._toggle_row(self._current_index)
            elif row.parent_key:
                parent = next(
                    (index for index, candidate in enumerate(self._view.rows) if candidate.node_key == row.parent_key),
                    self._current_index,
                )
                self._set_current(parent)
        elif event.key() in {Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space}:
            self._toggle_row(self._current_index)
        elif event.key() == Qt.Key_Home:
            self._select_by_key("", True)
        elif event.key() == Qt.Key_End:
            last = next((index for index in range(len(self._view.rows) - 1, -1, -1) if self._view.rows[index].kind == "function"), -1)
            self._set_current(last)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _update_ranges(self) -> None:
        self.verticalScrollBar().setRange(0, max(0, self._total_height - self.viewport().height()))
        self.verticalScrollBar().setPageStep(self.viewport().height())
        width = max(self.viewport().width(), self._view.max_depth * COLUMN_WIDTH)
        self.horizontalScrollBar().setRange(0, max(0, width - self.viewport().width()))
        self.horizontalScrollBar().setPageStep(self.viewport().width())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_ranges()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.modifiers() & Qt.ShiftModifier or event.angleDelta().y() == 0:
            super().wheelEvent(event)
            return
        steps = int(event.angleDelta().y() / 120)
        if steps == 0:
            steps = 1 if event.angleDelta().y() > 0 else -1
        self.verticalScrollBar().setValue(
            self.verticalScrollBar().value() - steps * FUNCTION_HEIGHT
        )
        event.accept()

    def _horizontal_changed(self, value: int) -> None:
        self.horizontalChanged.emit(value)
        self.stateChanged.emit()
        self.viewport().update()

    def _scroll_changed(self, value: int) -> None:
        self._emit_path()
        self.stateChanged.emit()
        self.viewport().update()

    def _row_index_at(self, y: int) -> int:
        return max(0, min(len(self._view.rows) - 1, bisect.bisect_right(self._offsets, y) - 1))

    def _emit_path(self) -> None:
        if not self._view.rows:
            self.pathChanged.emit(())
            return
        index = self._row_index_at(self.verticalScrollBar().value() + 2)
        while index < len(self._view.rows) and self._view.rows[index].kind != "function":
            index += 1
        names: list[str] = []
        while 0 <= index < len(self._view.rows):
            row = self._view.rows[index]
            if row.kind != "function":
                break
            names.append(row.name)
            if not row.parent_key:
                break
            index = self._index_by_key.get(row.parent_key, -1)
        self.pathChanged.emit(tuple(reversed(names)))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._view.rows:
            row_index = self._row_index_at(self.verticalScrollBar().value() + int(event.position().y()))
            if 0 <= row_index < len(self._view.rows) and self._view.rows[row_index].kind == "function":
                control = self._child_control_rect(
                    row_index,
                    self.horizontalScrollBar().value(),
                    self.verticalScrollBar().value(),
                )
                keys, _ = self._child_toggle_state(row_index)
                if keys and control.contains(int(event.position().x()), int(event.position().y())):
                    if self._toggle_child_subtrees(row_index):
                        event.accept()
                        return
            index = self._cell_index_at(int(event.position().x()), int(event.position().y()))
            if index < 0:
                super().mousePressEvent(event)
                return
            row = self._view.rows[index]
            if row.kind == "function":
                self.setFocus()
                self._set_current(index)
                column_x = (row.depth - 1) * COLUMN_WIDTH - self.horizontalScrollBar().value()
                if column_x <= event.position().x() <= column_x + 28 and self._toggle_row(index):
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._view.rows:
            index = self._cell_index_at(int(event.position().x()), int(event.position().y()))
            if self._toggle_row(index):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _paint_connections(self, painter: QPainter, x_offset: int, y_offset: int) -> None:
        painter.setPen(QPen(QColor("#7697AE"), 1))
        viewport_height = self.viewport().height()
        for parent_index, child_indices in self._children.items():
            if not child_indices:
                continue
            parent = self._view.rows[parent_index]
            parent_y = self._offsets[parent_index] - y_offset + self._row_height(parent) // 2
            last_child = child_indices[-1]
            last_row = self._view.rows[last_child]
            last_y = self._offsets[last_child] - y_offset + self._row_height(last_row) // 2
            if last_y < 0 or parent_y > viewport_height:
                continue
            parent_anchor = parent.depth * COLUMN_WIDTH - x_offset - 10
            child_anchor = parent.depth * COLUMN_WIDTH - x_offset + 13
            painter.drawLine(parent_anchor, parent_y, child_anchor, parent_y)
            painter.drawLine(child_anchor, parent_y, child_anchor, last_y)
            for child_index in child_indices:
                child = self._view.rows[child_index]
                child_y = self._offsets[child_index] - y_offset + self._row_height(child) // 2
                if -FUNCTION_HEIGHT <= child_y <= viewport_height + FUNCTION_HEIGHT:
                    painter.drawLine(child_anchor, child_y, child_anchor + 13, child_y)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor("#FFFFFF"))
        if not self._view.rows:
            painter.setPen(QColor("#6B7D8C"))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, "분석할 폴더를 선택하세요.")
            return
        x_offset = self.horizontalScrollBar().value()
        y_offset = self.verticalScrollBar().value()
        first = self._row_index_at(y_offset)
        last_y = y_offset + self.viewport().height()

        for depth in range(self._view.max_depth):
            x = depth * COLUMN_WIDTH - x_offset
            painter.fillRect(x, 0, COLUMN_WIDTH, self.viewport().height(), QColor("#EDF4F8" if depth % 2 else "#F8FBFD"))
            painter.setPen(QColor("#BDCBD6"))
            painter.drawLine(x + COLUMN_WIDTH - 1, 0, x + COLUMN_WIDTH - 1, self.viewport().height())

        for merged_index in self._merged_indices:
            rect = self._cell_rect(merged_index, x_offset, y_offset)
            if rect.top() > self.viewport().height():
                break
            if rect.bottom() < 0:
                continue
            depth = self._view.rows[merged_index].depth
            painter.fillRect(rect, QColor("#EDF4F8" if depth % 2 == 0 else "#F8FBFD"))
            painter.setPen(QPen(QColor("#BDCBD6"), 1))
            painter.drawLine(rect.left() - 1, rect.top() - 1, rect.right(), rect.top() - 1)
            painter.drawLine(rect.left(), rect.bottom() - 1, rect.right(), rect.bottom() - 1)
            painter.drawLine(rect.left() - 1, rect.top() - 1, rect.left() - 1, rect.bottom() - 1)
            painter.drawLine(rect.right(), rect.top() - 1, rect.right(), rect.bottom() - 1)

        selected_rect = QRect()
        if 0 <= self._current_index < len(self._view.rows):
            selected = self._view.rows[self._current_index]
            if selected.kind == "function":
                selected_rect = self._cell_rect(self._current_index, x_offset, y_offset)
                painter.fillRect(selected_rect.adjusted(1, 1, -2, -2), QColor(22, 131, 216, 38))

        self._paint_connections(painter, x_offset, y_offset)

        index = first
        while index < len(self._view.rows) and self._offsets[index] < last_y:
            row = self._view.rows[index]
            top = self._offsets[index] - y_offset
            height = self._row_height(row)
            if row.kind == "section":
                color = "#FFF0D5" if row.state == "interrupt" else "#E8EDF1" if row.state in {"independent", "search"} else "#DFEEFA"
                painter.fillRect(0, top, self.viewport().width(), height, QColor(color))
                painter.setPen(QColor("#8FA6B8"))
                painter.drawLine(0, top + height - 1, self.viewport().width(), top + height - 1)
                painter.setPen(QColor("#244B68"))
                painter.drawText(12, top, self.viewport().width() - 24, height, Qt.AlignVCenter | Qt.AlignLeft, row.title)
            elif row.kind == "function":
                x = (row.depth - 1) * COLUMN_WIDTH - x_offset
                source_index = self._visible_source_indices[index]
                has_children = self._source_has_children(source_index)
                line_start = x + (COLUMN_WIDTH if self._children.get(index) else 0)
                painter.setPen(QColor("#D6E0E7"))
                painter.drawLine(line_start, top + height - 1, self.viewport().width(), top + height - 1)
                if has_children:
                    box_x = x + 9
                    box_y = top + (height - 11) // 2
                    painter.setBrush(QColor("#FFFFFF"))
                    painter.setPen(QPen(QColor("#7697AE"), 1))
                    painter.drawRect(box_x, box_y, 10, 10)
                    painter.drawLine(box_x + 2, box_y + 5, box_x + 8, box_y + 5)
                    if row.node_key in self._collapsed:
                        painter.drawLine(box_x + 5, box_y + 2, box_x + 5, box_y + 8)
                color = QColor("#172B3A")
                if row.state == "external":
                    color = QColor("#9A6700")
                elif row.state == "cycle":
                    color = QColor("#B42318")
                elif row.state == "safety_limit":
                    color = QColor("#805500")
                full_text = self._display_text(row)
                text_width = max(1, COLUMN_WIDTH - 40)
                visible_text = painter.fontMetrics().elidedText(full_text, Qt.ElideRight, text_width)
                painter.setPen(color)
                painter.drawText(x + 28, top, text_width, height, Qt.AlignVCenter | Qt.AlignLeft, visible_text)
            index += 1

        control_index = first
        while control_index < len(self._view.rows) and self._offsets[control_index] < last_y:
            row = self._view.rows[control_index]
            if row.kind == "function":
                keys, action = self._child_toggle_state(control_index)
                if keys:
                    rect = self._child_control_rect(control_index, x_offset, y_offset)
                    if rect.bottom() >= 0 and rect.top() <= self.viewport().height():
                        painter.setBrush(QColor("#D7E5EF"))
                        painter.setPen(QPen(QColor("#7697AE"), 1))
                        painter.drawRoundedRect(rect, 4, 4)
                        painter.setPen(QColor("#17324D"))
                        painter.drawText(rect, Qt.AlignCenter, f"모두 {action}")
            control_index += 1

        if not selected_rect.isNull():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#1683D8"), 2))
            painter.drawRect(selected_rect.adjusted(1, 1, -2, -2))


class CallTreeWidget(QWidget):
    functionActivated = Signal(str)
    stateChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.header = StickyHeader(self)
        self.body = VirtualCallBody(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.body, 1)
        self.body.functionActivated.connect(self.functionActivated)
        self.body.pathChanged.connect(self._path_changed)
        self.body.horizontalChanged.connect(self._horizontal_changed)
        self.body.depthStateChanged.connect(self._depth_states_changed)
        self.body.stateChanged.connect(self.stateChanged)
        self.header.depthClicked.connect(self.body.toggle_depth)
        self._depth_actions: tuple[str, ...] = ()

    def set_view(self, view: CallView, preserve_scroll: bool = True) -> None:
        self.header.update_state(view.max_depth, self.body.horizontalScrollBar().value(), (), self._depth_actions)
        self.body.set_view(view, preserve_scroll)

    def find_text(self, text: str, direction: int = 1, restart: bool = False) -> tuple[int, int]:
        return self.body.find_text(text, direction, restart)

    def export_state(self) -> dict[str, object]:
        return self.body.export_state()

    def restore_state(self, state: dict[str, object]) -> None:
        self.body.restore_state(state)

    def search_state(self) -> tuple[int, int]:
        return self.body.search_state()

    def _path_changed(self, path: tuple[str, ...]) -> None:
        self.header.update_state(self.body._view.max_depth, self.body.horizontalScrollBar().value(), path, self._depth_actions)

    def _horizontal_changed(self, value: int) -> None:
        self.header.update_state(self.body._view.max_depth, value, self.header._path, self._depth_actions)

    def _depth_states_changed(self, states: tuple[str, ...]) -> None:
        self._depth_actions = states
        self.header.update_state(
            self.body._view.max_depth,
            self.body.horizontalScrollBar().value(),
            self.header._path,
            states,
        )
