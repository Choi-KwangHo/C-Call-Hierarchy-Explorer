from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from analyzer import CallView, ViewRow  # noqa: E402
from virtual_tree import COLUMN_WIDTH, FUNCTION_HEIGHT, CallTreeWidget  # noqa: E402


def make_view(extra_grandchild: bool = False) -> CallView:
    rows = [
        ViewRow(kind="section", title="MAIN"),
        ViewRow(kind="function", depth=1, name="main", function_id="main", node_key="r", path_names=("main",)),
        ViewRow(kind="function", depth=2, name="task", function_id="task", node_key="r/task#1", parent_key="r", path_names=("main", "task")),
        ViewRow(kind="function", depth=3, name="work", function_id="work", node_key="r/task#1/work#1", parent_key="r/task#1", path_names=("main", "task", "work")),
    ]
    if extra_grandchild:
        rows.append(ViewRow(kind="function", depth=3, name="added", function_id="added", node_key="r/task#1/added#1", parent_key="r/task#1", path_names=("main", "task", "added")))
    rows.extend([
        ViewRow(kind="function", depth=2, name="other", function_id="other", node_key="r/other#1", parent_key="r", path_names=("main", "other")),
        ViewRow(kind="spacer"),
    ])
    return CallView(rows, 3, [], "main", 0)


class VirtualTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_collapse_survives_refresh_and_keyboard_moves_to_parent(self) -> None:
        widget = CallTreeWidget()
        widget.resize(900, 400)
        widget.set_view(make_view(), False)
        widget.show()
        self.app.processEvents()

        self.assertTrue(widget.body._toggle_row(2))
        widget.set_view(make_view(True), True)
        self.assertIn("r/task#1", widget.body._collapsed)
        self.assertNotIn("added", [row.name for row in widget.body._view.rows])

        task_index = next(index for index, row in enumerate(widget.body._view.rows) if row.node_key == "r/task#1")
        widget.body._toggle_row(task_index)
        self.assertIn("added", [row.name for row in widget.body._view.rows])

        widget.body._select_by_key("r", False)
        QTest.keyClick(widget.body, Qt.Key_Right)
        self.assertEqual(widget.body._selected_key, "r/task#1")
        QTest.keyClick(widget.body, Qt.Key_Right)
        self.assertEqual(widget.body._selected_key, "r/task#1/work#1")
        QTest.keyClick(widget.body, Qt.Key_Left)
        self.assertEqual(widget.body._selected_key, "r/task#1")
        QTest.keyClick(widget.body, Qt.Key_Left)
        self.assertIn("r/task#1", widget.body._collapsed)
        QTest.keyClick(widget.body, Qt.Key_Left)
        self.assertEqual(widget.body._selected_key, "r")
        widget.close()

    def test_selected_merged_cell_keeps_translucent_contents(self) -> None:
        widget = CallTreeWidget()
        widget.resize(900, 320)
        widget.set_view(make_view(True), False)
        widget.show()
        self.app.processEvents()

        widget.body._select_by_key("r/task#1", False)
        self.app.processEvents()
        selected = widget.body._cell_rect(widget.body._current_index)
        self.assertEqual(selected.height(), 3 * 28)
        image = widget.body.viewport().grab().toImage()
        self.assertNotEqual(image.pixelColor(selected.center()), QColor("#FFFFFF"))
        widget.close()

    def test_external_function_cell_can_be_selected(self) -> None:
        widget = CallTreeWidget()
        widget.resize(900, 320)
        view = make_view()
        external = view.rows[3]
        external.function_id = None
        external.state = "external"
        widget.set_view(view, False)
        activated: list[str] = []
        widget.functionActivated.connect(activated.append)
        widget.show()
        self.app.processEvents()

        QTest.mouseClick(widget.body.viewport(), Qt.LeftButton, pos=widget.body._cell_rect(3).center())
        self.assertEqual(widget.body._selected_key, external.node_key)
        self.assertEqual(activated[-1], "")
        widget.close()

    def test_mouse_drag_pans_to_cell_boundaries_without_moving_selection(self) -> None:
        rows = [ViewRow(kind="section", title="MAIN")]
        for index in range(30):
            rows.append(ViewRow(
                kind="function",
                depth=1,
                name=f"task_{index}",
                node_key=f"r/task#{index}",
                path_names=(f"task_{index}",),
            ))
        rows.append(ViewRow(kind="spacer"))
        widget = CallTreeWidget()
        widget.resize(620, 260)
        widget.set_view(CallView(rows, 6, [], "main", 0), False)
        widget.show()
        self.app.processEvents()

        body = widget.body
        start = QPoint(100, body.viewport().height() // 2)
        selected_index = body._cell_index_at(start.x(), start.y())
        body._set_current(selected_index)
        selected = body._selected_key
        QTest.mousePress(body.viewport(), Qt.LeftButton, pos=start)
        self.assertTrue(body._drag_scrolling)
        body.verticalScrollBar().setValue(FUNCTION_HEIGHT * 10)
        body.horizontalScrollBar().setValue(COLUMN_WIDTH * 2)
        body._drag_start_vertical = body.verticalScrollBar().value()
        body._drag_start_horizontal = body.horizontalScrollBar().value()
        start_vertical = body._drag_start_vertical
        start_horizontal = body._drag_start_horizontal

        body._drag_position = start + QPoint(COLUMN_WIDTH * 2 + 20, FUNCTION_HEIGHT * 2 + 3)
        body._drag_scroll_step()
        self.assertEqual(body.verticalScrollBar().value(), start_vertical - FUNCTION_HEIGHT * 2)
        self.assertEqual(body.horizontalScrollBar().value(), max(0, start_horizontal - COLUMN_WIDTH * 2))
        self.assertEqual(body._selected_key, selected)

        body._drag_position = start - QPoint(COLUMN_WIDTH + 30, FUNCTION_HEIGHT * 3 + 4)
        body._drag_scroll_step()
        self.assertEqual(body.verticalScrollBar().value(), start_vertical + FUNCTION_HEIGHT * 3)
        self.assertEqual(body.horizontalScrollBar().value(), start_horizontal + COLUMN_WIDTH)
        self.assertEqual(body._selected_key, selected)

        QTest.mouseRelease(body.viewport(), Qt.LeftButton, pos=start)
        self.assertFalse(body._drag_scrolling)
        widget.close()

    def test_stage_header_toggles_every_subtree_at_that_depth(self) -> None:
        widget = CallTreeWidget()
        widget.resize(900, 400)
        widget.set_view(make_view(True), False)
        widget.show()
        self.app.processEvents()

        self.assertEqual(widget._depth_actions, ("접기", "접기", ""))
        widget.body.toggle_depth(2)
        self.assertEqual(widget._depth_actions[1], "펼치기")
        self.assertNotIn("r/task#1/work#1", [row.node_key for row in widget.body._view.rows])
        self.assertNotIn("added", [row.name for row in widget.body._view.rows])

        # 헤더의 2단계 컬럼을 누르면 현재 표시된 동작(펼치기)이 실행된다.
        QTest.mouseClick(widget.header, Qt.LeftButton, pos=QPoint(285 + 140, 18))
        self.assertEqual(widget._depth_actions[1], "접기")
        self.assertIn("work", [row.name for row in widget.body._view.rows])
        self.assertIn("added", [row.name for row in widget.body._view.rows])
        widget.close()

    def test_search_reveals_only_ancestors_and_cycles_matches(self) -> None:
        view = make_view()
        spacer = view.rows.pop()
        view.rows.append(ViewRow(
            kind="function", depth=3, name="work", file="second.c", function_id="work2",
            node_key="r/other#1/work#1", parent_key="r/other#1",
        ))
        view.rows.append(spacer)
        widget = CallTreeWidget()
        widget.resize(900, 320)
        widget.set_view(view, False)
        widget.show()
        self.app.processEvents()

        # task 자신과 root를 접은 뒤 task를 찾으면 root만 열리고 task 아래는 유지된다.
        self.assertTrue(widget.body._toggle_row(2))
        self.assertTrue(widget.body._toggle_row(1))
        self.assertIn("r", widget.body._collapsed)
        self.assertIn("r/task#1", widget.body._collapsed)
        self.assertEqual(widget.find_text("task", restart=True), (1, 1))
        self.assertNotIn("r", widget.body._collapsed)
        self.assertIn("r/task#1", widget.body._collapsed)
        self.assertEqual(widget.body._selected_key, "r/task#1")
        self.assertNotIn("r/task#1/work#1", [row.node_key for row in widget.body._view.rows])

        # 다음 검색은 첫 번째 work의 부모만 열고, 다시 누르면 두 번째 호출로 이동한다.
        self.assertEqual(widget.find_text("work", restart=True), (1, 2))
        self.assertNotIn("r/task#1", widget.body._collapsed)
        first = widget.body._selected_key
        self.assertEqual(widget.find_text("work", direction=1), (2, 2))
        self.assertNotEqual(widget.body._selected_key, first)
        self.assertEqual(widget.body._selected_key, "r/other#1/work#1")
        widget.close()

    def test_search_ignores_hidden_single_call_caller_file(self) -> None:
        rows = [
            ViewRow(kind="section", title="MAIN"),
            ViewRow(kind="function", depth=1, name="main", file="main.c", function_id="main", node_key="r"),
            ViewRow(
                kind="function", depth=2, name="SystemClock_Config", file="system.c",
                function_id="system", node_key="r/system#1", parent_key="r",
                call_file="main.c", call_lines=(10,),
            ),
            ViewRow(
                kind="function", depth=2, name="ff_mutex_delete", file="ff.c",
                function_id="ff", node_key="r/ff#1", parent_key="r",
                call_file="System_Service.c", call_lines=(20,),
            ),
            ViewRow(kind="spacer"),
        ]
        widget = CallTreeWidget()
        widget.resize(900, 320)
        widget.set_view(CallView(rows, 2, [], "main", 0), False)
        activated: list[str] = []
        widget.functionActivated.connect(activated.append)
        widget.show()
        self.app.processEvents()

        self.assertEqual(widget.find_text("System", restart=True), (1, 1))
        self.assertEqual(widget.body._selected_key, "r/system#1")
        self.assertEqual(activated[-1], "system")
        self.assertEqual(widget.find_text("System", direction=1), (1, 1))
        self.assertEqual(widget.body._selected_key, "r/system#1")
        self.assertNotIn("ff", activated)
        widget.close()

    def test_parent_empty_cell_toggles_only_direct_child_subtrees(self) -> None:
        rows = [
            ViewRow(kind="section", title="MAIN"),
            ViewRow(kind="function", depth=1, name="main", node_key="r"),
            ViewRow(kind="function", depth=2, name="parent", node_key="r/p", parent_key="r"),
            ViewRow(kind="function", depth=3, name="child_a", node_key="r/p/a", parent_key="r/p"),
            ViewRow(kind="function", depth=4, name="grand_a", node_key="r/p/a/g", parent_key="r/p/a"),
            ViewRow(kind="function", depth=3, name="child_b", node_key="r/p/b", parent_key="r/p"),
            ViewRow(kind="function", depth=4, name="grand_b", node_key="r/p/b/g", parent_key="r/p/b"),
            ViewRow(kind="spacer"),
        ]
        widget = CallTreeWidget()
        widget.resize(1200, 360)
        widget.set_view(CallView(rows, 4, [], "r", 0), False)
        widget.show()
        self.app.processEvents()

        keys, action = widget.body._child_toggle_state(2)
        self.assertEqual((keys, action), (["r/p/a", "r/p/b"], "접기"))
        control = widget.body._child_control_rect(2, 0, 0)
        QTest.mouseClick(widget.body.viewport(), Qt.LeftButton, pos=control.center())
        self.assertEqual(widget.body._child_toggle_state(2)[1], "펼치기")
        self.assertNotIn("r/p", widget.body._collapsed)
        self.assertTrue({"r/p/a", "r/p/b"}.issubset(widget.body._collapsed))
        self.assertNotIn("grand_a", [row.name for row in widget.body._view.rows])
        self.assertIn("child_a", [row.name for row in widget.body._view.rows])

        control = widget.body._child_control_rect(2, 0, 0)
        QTest.mouseClick(widget.body.viewport(), Qt.LeftButton, pos=control.center())
        self.assertEqual(widget.body._child_toggle_state(2)[1], "접기")
        self.assertIn("grand_a", [row.name for row in widget.body._view.rows])
        self.assertIn("grand_b", [row.name for row in widget.body._view.rows])
        widget.close()


if __name__ == "__main__":
    unittest.main()
