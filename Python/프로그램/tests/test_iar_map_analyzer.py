from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from iar_map_analyzer import choose_map_file, discover_map_files, parse_map_file, parse_map_text


SAMPLE = """
Module=STM32F103VE
config file: project.icf
readonly code memory  0x00008000
readonly data memory  0x00000400
readwrite data memory 0x00003000
P5: place in [from 0x08000000 to 0x0807FFFF] { ro }
P6: place in [from 0x20000000 to 0x2000FFFF] { rw }
define block STACK_BOTTOM_B with size = 0x00000100 { }
CSTACK at 0x20001000 to 0x200017FF
HEAP at 0x20001800 to 0x20001FFF
__Heap_Handler = __no_free_malloc
*** STACK USAGE
main 0x20 0x30
CommTask 0x40 0x80

0x20000020 g_state 0x20
"""


class IarMapAnalyzerTests(unittest.TestCase):
    def test_parse_memory_summary_and_warnings(self) -> None:
        result = parse_map_text(SAMPLE, "project.map")
        self.assertEqual(result.readonly_code, 0x8000)
        self.assertEqual(result.readwrite_data, 0x3000)
        self.assertEqual(result.flash.start, 0x08000000)
        self.assertEqual(result.sram.end, 0x2000FFFF)
        self.assertEqual(result.cstack.size, 0x800)
        self.assertEqual(result.heap.size, 0x800)
        self.assertEqual(result.max_stack, 0x40)
        self.assertTrue(result.no_free)
        self.assertEqual(result.mcu_hint, "STM32F103VE")
        self.assertEqual(result.icf_file, "project.icf")
        self.assertEqual(len(result.symbols), 1)

    def test_discovery_prefers_project_list_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project.ewp").write_text("", encoding="utf-8")
            (root / "Debug" / "List").mkdir(parents=True)
            preferred = root / "Debug" / "List" / "project.map"
            preferred.write_text(SAMPLE, encoding="utf-8")
            other = root / "other.map"
            other.write_text(SAMPLE, encoding="utf-8")
            candidates = discover_map_files(root)
            self.assertEqual(candidates[0].resolve(), preferred.resolve())
            self.assertEqual(choose_map_file(root).resolve(), preferred.resolve())
            parsed = parse_map_file(preferred)
            self.assertIn("project.map", parsed.path)

    def test_main_workspace_exposes_integrated_analysis_tabs(self) -> None:
        from PySide6.QtWidgets import QApplication
        from app import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        self.assertEqual(window.analysis_tabs.count(), 4)
        self.assertEqual(window.analysis_tabs.tabText(0), "함수 트리")
        self.assertEqual(window.analysis_tabs.tabText(1), "EEPROM 메모리 맵")
        self.assertEqual(window.analysis_tabs.tabText(2), "IAR MAP Analyzer")
        self.assertEqual(window.analysis_tabs.tabText(3), "Trace")
        window.close()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
