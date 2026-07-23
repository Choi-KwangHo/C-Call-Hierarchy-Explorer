from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path

from analyzer import AnalyzerSession, build_call_view


class AnalyzerTests(unittest.TestCase):
    def test_tree_sitter_depth_and_incremental_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "main.c"
            source.write_text(
                "void c(void){}\nvoid b(void){c();}\nvoid a(void){b();}\nint main(void){a();}\n",
                encoding="utf-8",
            )
            session = AnalyzerSession()
            result = session.initial_scan(str(root))
            view = build_call_view(result)
            self.assertGreaterEqual(view.max_depth, 4)
            self.assertEqual([fn.name for fn in view.main_candidates], ["main"])

            original_count = len(result.functions)
            source.write_text(source.read_text(encoding="utf-8") + "void added(void){c();}\n", encoding="utf-8")
            result, changed, deleted = session.check_updates()
            self.assertEqual((changed, deleted), (1, 0))
            self.assertEqual(len(result.functions), original_count + 1)

    def test_external_calls_can_be_hidden_without_removing_resolved_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.c").write_text(
                "void child(void){}\nint main(void){child(); missing_api();}\n",
                encoding="utf-8",
            )
            result = AnalyzerSession().initial_scan(str(root))
            visible = build_call_view(result)
            hidden = build_call_view(result, include_external_calls=False)
            self.assertIn("missing_api", [row.name for row in visible.rows])
            self.assertNotIn("missing_api", [row.name for row in hidden.rows])
            self.assertIn("child", [row.name for row in hidden.rows])

    def test_duplicate_calls_and_same_timestamp_size_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "main.c"
            source.write_text("void Watchdog_Task(void){}\nint main(void){Watchdog_Task();}\n", encoding="utf-8")
            session = AnalyzerSession()
            result = session.initial_scan(str(root))
            original_mtime = source.stat().st_mtime_ns
            self.assertEqual(len(result.by_name["main"][0].calls), 1)

            source.write_text(
                "void Watchdog_Task(void){}\nint main(void){Watchdog_Task(); Watchdog_Task();}\n",
                encoding="utf-8",
            )
            os.utime(source, ns=(original_mtime, original_mtime))
            result, changed, deleted = session.check_updates()
            self.assertEqual((changed, deleted), (1, 0))
            self.assertEqual(len(result.by_name["main"][0].calls), 2)
            view = build_call_view(result)
            watchdog_rows = [row for row in view.rows if row.name == "Watchdog_Task"]
            self.assertEqual(len(watchdog_rows), 1)
            self.assertEqual(watchdog_rows[0].call_lines, (2,))

            source.write_text(
                "void Watchdog_Task(void){}\n"
                "int main(void){\nWatchdog_Task();\nWatchdog_Task();\n}\n",
                encoding="utf-8",
            )
            result, changed, deleted = session.check_updates()
            view = build_call_view(result)
            watchdog = next(row for row in view.rows if row.name == "Watchdog_Task")
            self.assertEqual(watchdog.call_lines, (3, 4))

    def test_roots_exclude_main_reachable_and_library_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Project"
            core = root / "Core" / "Src"
            app = root / "App" / "Src"
            hal = root / "Drivers" / "STM32_HAL_Driver" / "Src"
            core.mkdir(parents=True)
            app.mkdir(parents=True)
            hal.mkdir(parents=True)
            (core / "main.c").write_text(
                "void App_Task(void);\nint main(void){App_Task();}\n",
                encoding="utf-8",
            )
            (core / "stm32f1xx_it.c").write_text(
                "void TIM2_IRQHandler(void){}\nvoid Fake_Handler(void){}\n",
                encoding="utf-8",
            )
            (app / "tasks.c").write_text(
                "void App_Task(void){}\n"
                "void Background_Task(void){}\n"
                "static void Hidden_Task(void){}\n",
                encoding="utf-8",
            )
            (hal / "hal.c").write_text("void HAL_IRQHandler(void){}\n", encoding="utf-8")

            result = AnalyzerSession().initial_scan(str(root))
            view = build_call_view(result, include_other_roots=True)
            section_titles = [row.title for row in view.rows if row.kind == "section"]
            function_names = [row.name for row in view.rows if row.kind == "function"]

            self.assertTrue(any("인터럽트 / ISR" in title and "stm32f1xx_it.c" in title for title in section_titles))
            self.assertEqual(function_names.count("TIM2_IRQHandler"), 1)
            self.assertEqual(function_names.count("App_Task"), 1)  # main 아래에서만 표시
            self.assertEqual(function_names.count("Background_Task"), 1)
            self.assertNotIn("Fake_Handler", function_names)
            self.assertNotIn("HAL_IRQHandler", function_names)
            self.assertNotIn("Hidden_Task", function_names)

    def test_project_subfolder_exclusion_updates_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keep = root / "Keep"
            skip = root / "Skip"
            keep.mkdir()
            skip.mkdir()
            (root / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            (keep / "keep.c").write_text("void keep_fn(void){}\n", encoding="utf-8")
            (skip / "skip.c").write_text("void skip_fn(void){}\n", encoding="utf-8")

            session = AnalyzerSession()
            result = session.initial_scan(str(root), excluded_directories=["Skip"])
            self.assertIn("keep_fn", result.by_name)
            self.assertNotIn("skip_fn", result.by_name)

            session.set_excluded_directories([])
            result, changed, deleted = session.check_updates()
            self.assertEqual((changed, deleted), (1, 0))
            self.assertIn("skip_fn", result.by_name)

            session.set_excluded_directories(["Keep"])
            result, changed, deleted = session.check_updates()
            self.assertEqual((changed, deleted), (0, 1))
            self.assertNotIn("keep_fn", result.by_name)


if __name__ == "__main__":
    unittest.main()
