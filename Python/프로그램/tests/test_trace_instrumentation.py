from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trace_instrumentation import (
    apply_trace_point, generate_trace_runtime, load_trace_points,
    preview_add_trace_point, remove_trace_point,
)
from trace_ui import parse_iar_log, parse_trace_line


class TraceInstrumentationTests(unittest.TestCase):
    def test_user_selected_point_preview_apply_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "App" / "task.c"
            source.parent.mkdir()
            source.write_text(
                '#include "task.h"\n'
                "void ParsingData(void)\n"
                "{\n"
                "    process();\n"
                "}\n",
                encoding="utf-8",
            )
            point, updated, diff = preview_add_trace_point(
                root, source, 4, "FUNC_ENTER", "ParsingData 진입"
            )
            self.assertIn("CCH_TRACE_FUNC_ENTER", updated)
            self.assertIn("CCH-TRACE:BEGIN", diff)
            self.assertEqual(source.read_text(encoding="utf-8").count("CCH-TRACE"), 0)

            applied = apply_trace_point(
                root, source, 4, "FUNC_ENTER", "ParsingData 진입"
            )
            changed = source.read_text(encoding="utf-8")
            self.assertIn("CCH_TRACE_FUNC_ENTER", changed)
            self.assertIn("CCH_Trace/cch_trace.h", changed.replace("\\", "/"))
            self.assertEqual(len(load_trace_points(root)), 1)
            self.assertTrue(remove_trace_point(root, applied.id))
            restored = source.read_text(encoding="utf-8")
            self.assertNotIn("CCH-TRACE", restored)
            self.assertNotIn("cch_trace.h", restored)

    def test_generated_runtime_contains_reverse_fault_dump_and_iar_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            header, source = generate_trace_runtime(temporary)
            header_text = header.read_text(encoding="utf-8")
            source_text = source.read_text(encoding="utf-8")
            self.assertIn("CCH_TRACE_IAR_EVENT", header_text)
            self.assertIn("ITM_EVENT32_WITH_PC", header_text)
            self.assertIn("end - 1u - offset", source_text)
            self.assertIn("CCH|FAULT_BEGIN", source_text)
            self.assertIn("CCH_PrintRetainedFault", source_text)
            self.assertIn("CCH_TRACE_LIVE_PRINTF", header_text)
            self.assertIn('"CCH|%lu|EVT|%u|CTX|%u', source_text)
            self.assertTrue((Path(temporary) / "CCH_Trace" / "README.txt").is_file())

    def test_structured_trace_line_is_parsed_for_timeline(self) -> None:
        event = parse_trace_line("CCH|105910|FUNC_ENTER|ParsingData")
        self.assertIsNotNone(event)
        self.assertEqual(event.timestamp, 105910)
        self.assertEqual(event.event, "FUNC_ENTER")
        self.assertEqual(event.context, "ParsingData")
        dumped = parse_trace_line("CCH|105944|EVT|8|CTX|52|VAL|0x00000034|SEQ|7")
        self.assertIsNotNone(dumped)
        self.assertEqual(dumped.event, "ERROR")
        self.assertEqual(dumped.context, "52")
        self.assertIn("VAL=0x00000034", dumped.value)

    def test_cp949_and_crlf_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.c"
            original = (
                "/* 한글 주석 */\r\n"
                "void Legacy(void)\r\n"
                "{\r\n"
                "    work();\r\n"
                "}\r\n"
            )
            source.write_bytes(original.encode("cp949"))
            point = apply_trace_point(root, source, 4, "EVENT", "레거시")
            changed = source.read_bytes().decode("cp949")
            self.assertIn("한글 주석", changed)
            self.assertNotIn("\n", changed.replace("\r\n", ""))
            self.assertTrue(remove_trace_point(root, point.id))
            restored = source.read_bytes().decode("cp949")
            self.assertEqual(restored, original)

    def test_iar_log_import_is_non_mutating_parser(self) -> None:
        events = parse_iar_log("100\tChannel 1\tBoot\nCCH|101|TASK_IN|CommTask")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event, "IAR_EVENT")
        self.assertEqual(events[1].context, "CommTask")


if __name__ == "__main__":
    unittest.main()
