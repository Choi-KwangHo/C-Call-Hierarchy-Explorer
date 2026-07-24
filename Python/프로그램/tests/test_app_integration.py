from __future__ import annotations

import os
import hashlib
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QDesktopServices, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox  # noqa: E402

from app import (  # noqa: E402
    APP_VERSION, RELEASE_PAGE, MainWindow, cleanup_previous_installations,
    compact_file_path, sanitize_recent_folders, unique_output_path,
)
from project_cache import ProjectCacheStore  # noqa: E402
from settings_dialog import ProjectSettingsDialog, normalize_exclusions  # noqa: E402


class AppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings_temporary = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, cls.settings_temporary.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.processEvents()
        cls.settings_temporary.cleanup()

    def _wait(self, window: MainWindow, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while window.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(window.busy, "백그라운드 작업 시간 초과")

    def test_manual_update_auto_data_path_and_excel_button(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "main.c"
            source.write_text("void Watchdog_Task(void){}\nint main(void){Watchdog_Task();}\n", encoding="utf-8")
            (root / "empty.h").write_text("#define PROJECT_VALUE 1\n", encoding="utf-8")
            window = MainWindow()
            window.file_action.setChecked(True)
            window.source_action.setChecked(True)
            self.assertEqual(window.source_action.text(), "CODE 미리보기")
            self.assertEqual(window.release_action.text(), "GitHub releases/latest 열기")
            with patch.object(QDesktopServices, "openUrl", return_value=True) as open_release:
                window.release_action.trigger()
            self.assertEqual(open_release.call_args.args[0].toString(), RELEASE_PAGE)
            window.auto_check.setChecked(False)
            window._open_folder(str(root))
            self._wait(window)
            self.assertEqual(window.file_tree.topLevelItemCount(), 1)
            self.assertEqual(
                Path(window.file_tree.topLevelItem(0).data(0, Qt.UserRole)).name,
                "main.c",
            )
            self.assertEqual(len(window.result.by_name["main"][0].calls), 1)
            window.search_edit.setText("Watchdog_Task")
            window._search_move(1, True)
            self.assertEqual(window.call_tree.body._selected_key.endswith("Watchdog_Task#1"), True)
            self.assertEqual(window.search_count.text(), "1/1")
            window.search_edit.setText("main.c")
            window._search_move(1, True)
            self.assertGreaterEqual(int(window.search_count.text().split("/")[1]), 2)

            source.write_text(
                "void Watchdog_Task(void){}\nint main(void){Watchdog_Task(); Watchdog_Task();}\n",
                encoding="utf-8",
            )
            window._check_updates(False)
            self._wait(window)
            self.assertEqual(len(window.result.by_name["main"][0].calls), 2)
            self.assertEqual(sum(row.name == "Watchdog_Task" for row in window.view.rows), 1)

            source.write_text(
                "void Watchdog_Task(void){}\nint main(void){Watchdog_Task(); Watchdog_Task(); Watchdog_Task();}\n",
                encoding="utf-8",
            )
            window.auto_check.setChecked(True)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                self.app.processEvents()
                calls = window.result.by_name["main"][0].calls
                if len(calls) == 3 and not window.busy:
                    break
                time.sleep(0.02)
            self.assertEqual(len(window.result.by_name["main"][0].calls), 3)
            self.assertEqual(sum(row.name == "Watchdog_Task" for row in window.view.rows), 1)
            self.assertIn("마지막 변경 Date:", window.status_label.text())
            window.auto_check.setChecked(False)

            window._show_function("")
            self.assertIn("외부 또는 미확인 함수", window.source_view.toPlainText())

            output = root / "call-tree.xlsx"
            with (
                patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "Excel 통합 문서 (*.xlsx)")) as save_dialog,
                patch.object(QDesktopServices, "openUrl", return_value=True) as open_excel,
            ):
                window._export()
                self.assertIn(f"{root.name.strip(' ._')}_함수호출트리.xlsx", save_dialog.call_args.args[2])
                self._wait(window)
                open_excel.assert_called_once()
            self.assertTrue(zipfile.is_zipfile(output))
            self.assertEqual(window.progress.width(), 520)
            window.close()

            restored = MainWindow()
            self.assertTrue(restored.file_action.isChecked())
            self.assertTrue(restored.source_action.isChecked())
            restored.close()

    def test_unique_output_path_counts_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "tree.xlsx"
            original.touch()
            (root / "tree_1.xlsx").touch()
            self.assertEqual(unique_output_path(original).name, "tree_2.xlsx")

    def test_trace_center_lists_registered_rtos_task_without_editing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "main.c"
            original = (
                "void CommTask(void *argument){for(;;){}}\n"
                "int main(void){xTaskCreate(CommTask,\"COMM\",512,0,3,0);return 0;}\n"
            )
            source.write_text(original, encoding="utf-8")
            window = MainWindow()
            window.auto_check.setChecked(False)
            window._open_folder(str(root))
            self._wait(window)
            window._open_trace_center()
            self.app.processEvents()
            self.assertIsNotNone(window.trace_center)
            task_groups = window.trace_center.object_tree.findItems(
                "FreeRTOS / RTOS Task",
                Qt.MatchExactly | Qt.MatchRecursive,
                0,
            )
            self.assertEqual(len(task_groups), 1)
            self.assertEqual(task_groups[0].child(0).text(0), "CommTask")
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertFalse((root / ".cch-trace.json").exists())
            window.trace_center.accept()
            self.app.processEvents()
            window.close()

    def test_update_installer_waits_for_current_process_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "setup.exe"
            content = b"verified setup"
            installer.write_bytes(content)
            window = MainWindow()
            window.settings.setValue("update/pendingInstaller", str(installer))
            window.settings.setValue("update/pendingVersion", "9.9.9")
            window.settings.setValue("update/pendingSize", len(content))
            window.settings.setValue("update/pendingSha256", hashlib.sha256(content).hexdigest())
            with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
                self.assertTrue(window._offer_pending_update_retry())
            with (
                patch("app.QProcess.startDetached", return_value=(True, 1234)) as start_detached,
                patch("app.QApplication.quit"),
                patch.object(window, "_save_cache_now"),
            ):
                self.assertTrue(window._launch_update_installer(installer))
            arguments = start_detached.call_args.args[1]
            self.assertEqual(arguments[0], "--wait-pid")
            self.assertGreater(int(arguments[1]), 0)
            window._closing = False
            window._clear_pending_update()
            window.close()

    def test_startup_checks_for_updates_once_on_every_launch(self) -> None:
        window = MainWindow()
        window._clear_pending_update()
        window.settings.setValue("update/checkOnStartup", True)
        window.settings.setValue("update/lastCheckEpoch", time.time())
        with patch.object(window, "_check_program_update") as check:
            window._startup_update_check()
            window._startup_update_check()
        check.assert_called_once_with(False)
        window.close()

    def test_recent_folders_remove_test_temporary_paths_and_duplicates(self) -> None:
        legitimate = Path.home() / "Documents" / "C-Call-Hierarchy-Recent-Test"
        with tempfile.TemporaryDirectory() as temporary:
            nested_temporary = Path(temporary) / "project"
            cleaned = sanitize_recent_folders(
                [str(nested_temporary), str(legitimate), str(legitimate)]
            )
        self.assertEqual(cleaned, [str(legitimate.resolve())])

    def test_startup_cleanup_removes_only_inactive_version_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "CCallHierarchyExplorer"
            current = root / "app-1.1.20-current"
            old = root / "app-1.1.20-old"
            unrelated = root / "user-data"
            for directory in (current, old, unrelated):
                directory.mkdir(parents=True)
            executable = current / "C Call Hierarchy Explorer.exe"
            executable.touch()
            removed = cleanup_previous_installations(executable)
            self.assertIn(old, removed)
            self.assertTrue(current.exists())
            self.assertFalse(old.exists())
            self.assertTrue(unrelated.exists())

    def test_file_path_compacts_with_panel_width(self) -> None:
        window = MainWindow()
        metrics = window.file_tree.fontMetrics()
        relative = r"Core\Src\main.c"
        full = r"SelectedProject\Core\Src\main.c"
        self.assertEqual(compact_file_path("SelectedProject", relative, metrics.horizontalAdvance(full) + 1, metrics), full)
        compact = compact_file_path("SelectedProject", relative, metrics.horizontalAdvance(r"..\main.c"), metrics)
        self.assertEqual(compact, r"..\main.c")
        shortest = compact_file_path(
            "SelectedProject",
            r"Core\Src\main_controller.c",
            metrics.horizontalAdvance(r"..\m..r.c"),
            metrics,
        )
        self.assertTrue(shortest.startswith(r"..\m"))
        self.assertIn("..", shortest[3:])
        self.assertTrue(shortest.endswith("r.c"))
        self.assertGreaterEqual(window.file_tree.minimumWidth(), metrics.horizontalAdvance(r"..\MMMMMMMMMM") + 48)
        self.assertFalse(window.workspace_splitter.isCollapsible(0))
        self.assertEqual(APP_VERSION, "1.2.2")
        window.close()

    def test_vscode_style_project_settings_and_exclusion_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Parent" / "Child").mkdir(parents=True)
            (root / "Other").mkdir()
            (root / "Parent" / "keep.c").write_text("void keep(void){}\n", encoding="utf-8")
            dialog = ProjectSettingsDialog(
                str(root),
                [r"Parent\Child", "Parent", "Other"],
            )
            self.assertEqual(normalize_exclusions([r"Parent\Child", "Parent", "Other"]), ["Other", "Parent"])
            self.assertEqual(dialog.excluded_folders(), ["Other", "Parent"])
            self.assertIn("설정", dialog.windowTitle())
            tree = dialog.folder_tree
            children = {
                tree.root_item.child(index).text(0): tree.root_item.child(index)
                for index in range(tree.root_item.childCount())
            }
            self.assertEqual(children["Parent"].checkState(0), Qt.Unchecked)
            self.assertEqual(children["Other"].checkState(0), Qt.Unchecked)
            children["Other"].setCheckState(0, Qt.Checked)
            self.assertEqual(dialog.excluded_folders(), ["Parent"])
            children["Parent"].setCheckState(0, Qt.Checked)
            self.assertEqual(dialog.excluded_folders(), [])
            children["Parent"].setExpanded(True)
            self.assertEqual(children["Parent"].child(0).text(0), "Child")
            parent_children = {
                children["Parent"].child(index).text(0): children["Parent"].child(index)
                for index in range(children["Parent"].childCount())
            }
            self.assertIn("keep.c", parent_children)
            parent_children["Child"].setCheckState(0, Qt.Unchecked)
            self.assertEqual(dialog.excluded_folders(), [r"Parent\Child"])
            parent_children["keep.c"].setCheckState(0, Qt.Unchecked)
            self.assertEqual(
                dialog.excluded_folders(),
                [r"Parent\Child", r"Parent\keep.c"],
            )
            dialog.external_check.setChecked(False)
            self.assertFalse(dialog.show_external_functions())
            dialog.macro_check.setChecked(False)
            self.assertFalse(dialog.exclude_macro_functions())
            self.assertEqual(
                dialog.external_check.palette().color(QPalette.Active, QPalette.WindowText).name(),
                "#e7e7e7",
            )
            tree_highlight = dialog.folder_tree.palette().color(QPalette.Active, QPalette.Highlight)
            self.assertEqual(tree_highlight.alpha(), 90)
            self.assertEqual(
                dialog.folder_tree.palette().color(QPalette.Active, QPalette.HighlightedText).name(),
                "#ffffff",
            )
            before_enter = dialog.excluded_folders()
            dialog.search.setText("분석 범위")
            QTest.keyClick(dialog.search, Qt.Key_Return)
            self.app.processEvents()
            self.assertEqual(dialog.excluded_folders(), before_enter)
            self.assertEqual(dialog.result(), QDialog.Rejected)
            self.assertFalse(dialog.scope_card.isHidden())
            dialog.search.setText("분")
            self.assertFalse(dialog.scope_card.isHidden())
            dialog.search.setText("분석 외부")
            self.assertFalse(dialog.scope_card.isHidden())
            dialog.search.setText("타이")
            self.assertTrue(dialog.scope_card.isHidden())
            self.assertFalse(dialog.no_results.isHidden())
            self.assertIn("타이", dialog.no_results_detail.text())
            self.assertEqual(dialog.categories.count(), 2)
            dialog.close()

            window = MainWindow()
            menu_titles = [action.text() for action in window.menuBar().actions()]
            self.assertEqual(menu_titles[:2], ["파일", "설정"])
            window.close()

    def test_code_preview_distinguishes_parent_children_and_external_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.c").write_text(
                "void child(void){}\n"
                "int main(void){ child(); missing_api(); }\n",
                encoding="utf-8",
            )
            window = MainWindow()
            window.auto_check.setChecked(False)
            window._open_folder(str(root))
            self._wait(window)
            window._show_function(window.result.by_name["main"][0].id)

            summary = window.source_summary.toPlainText()
            self.assertIn("부모(자기 자신)", summary)
            self.assertIn("main()", summary)
            self.assertIn("자식 호출(정의 확인)", summary)
            self.assertIn("child()", summary)
            self.assertIn("외부/미확인 호출", summary)
            self.assertIn("missing_api()", summary)
            colors = {selection.format.background().color().name() for selection in window.source_view.extraSelections()}
            self.assertTrue({"#b7ddf7", "#cdeccf", "#ffe2a8"}.issubset(colors))
            window.close()

    def test_project_cache_restores_tree_before_metadata_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            source = root / "main.c"
            source.write_text(
                "void leaf(void){}\nvoid child(void){leaf();}\nint main(void){child();}\n",
                encoding="utf-8",
            )
            cache_directory = Path(temporary) / "cache"

            first = MainWindow()
            first.cache_store = ProjectCacheStore(cache_directory)
            first.auto_check.setChecked(False)
            first._open_folder(str(root))
            self._wait(first)
            child_index = next(index for index, row in enumerate(first.call_tree.body._view.rows) if row.name == "child")
            child_key = first.call_tree.body._view.rows[child_index].node_key
            self.assertTrue(first.call_tree.body._toggle_row(child_index))
            first._save_cache_now()
            cache_path = first.cache_store.path_for(str(root))
            self.assertTrue(cache_path.is_file())
            original_mtime = cache_path.stat().st_mtime_ns
            first.close()

            second = MainWindow()
            second.cache_store = ProjectCacheStore(cache_directory)
            second.auto_check.setChecked(False)
            with patch.object(second.session, "initial_scan", side_effect=AssertionError("캐시가 있는데 전체 분석을 실행함")):
                second._open_folder(str(root))
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    self.app.processEvents()
                    if not second.busy and "변경 없음" in second.status_label.text():
                        break
                    time.sleep(0.01)
            self.assertFalse(second.busy)
            self.assertIn(child_key, second.call_tree.body._collapsed)
            self.assertFalse(second._cache_dirty)
            second.close()
            self.assertEqual(cache_path.stat().st_mtime_ns, original_mtime)

            source.write_text(
                source.read_text(encoding="utf-8") + "void added_while_closed(void){leaf();}\n",
                encoding="utf-8",
            )
            third = MainWindow()
            third.cache_store = ProjectCacheStore(cache_directory)
            with patch.object(third.session, "initial_scan", side_effect=AssertionError("변경 확인 전에 전체 분석을 실행함")):
                third._open_folder(str(root))
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    self.app.processEvents()
                    if not third.busy and third.result and "added_while_closed" in third.result.by_name:
                        break
                    time.sleep(0.01)
            self.assertIn("added_while_closed", third.result.by_name)
            self.assertTrue(third._cache_dirty)
            third.close()
            self.assertGreater(cache_path.stat().st_mtime_ns, original_mtime)


if __name__ == "__main__":
    unittest.main()
