from __future__ import annotations

import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from analyzer import AnalyzerSession, build_call_view
from xlsx_exporter import export_xlsx


class ExcelTests(unittest.TestCase):
    def test_export_is_valid_xlsx_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.c").write_text(
                "void leaf(void){}\n"
                "void deep(void){leaf();}\n"
                "void child(void){deep();}\n"
                "void sibling(void){}\n"
                "int main(void){\nchild();\nsibling();\nsibling();\n}\n",
                encoding="utf-8",
            )
            result = AnalyzerSession().initial_scan(str(root))
            result.functions[0].declaration = "x" * 40_000 + "\x01"
            output = root / "tree.xlsx"
            updates: list[tuple[int, int, str]] = []
            export_xlsx(str(output), result, build_call_view(result), lambda current, total, detail: updates.append((current, total, detail)))
            self.assertTrue(zipfile.is_zipfile(output))
            self.assertEqual(updates[-1][0], updates[-1][1])
            with zipfile.ZipFile(output) as archive:
                self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
                self.assertIn("함수 목록", archive.read("xl/workbook.xml").decode("utf-8"))
                self.assertIn("<mergeCells", archive.read("xl/worksheets/sheet1.xml").decode("utf-8"))
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                styles = archive.read("xl/styles.xml").decode("utf-8")
                self.assertNotIn("outlineLevel=", sheet)
                self.assertNotIn("showOutlineSymbols", sheet)
                self.assertNotIn("⊟", sheet)
                self.assertIn('mergeCell ref="B3:D3"', sheet)
                self.assertIn('mergeCell ref="C7:D7"', sheet)
                self.assertIn(">7, 8<", sheet)
                self.assertIn("FFE2E6E9", styles)
                self.assertNotIn("FFFFE5E5", styles)
                self.assertNotIn("FFFFF4D6", styles)
                sheet2 = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
                strings = [node.text or "" for node in sheet2.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
                self.assertLessEqual(max(map(len, strings)), 32_000)
                self.assertIn("함수 행 수", strings)
                function_sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
                self.assertIn('<c r="E2" s="0"><v>1</v></c>', function_sheet)


if __name__ == "__main__":
    unittest.main()
