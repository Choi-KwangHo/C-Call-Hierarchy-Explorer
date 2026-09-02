import tempfile
import unittest
from pathlib import Path

from source_encoding_converter import convert_items, scan_folder


class SourceEncodingConverterTests(unittest.TestCase):
    def test_scan_distinguishes_utf8_bom_cp949_and_ambiguous_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "utf8.c").write_bytes(b"int main(void) { return 0; }\r\n")
            (root / "bom.h").write_bytes(b"\xef\xbb\xbf#define NAME \xea\xb0\x80\xeb\x82\x98\n")
            (root / "legacy.c").write_bytes("// 한글\r\nint value;\r\n".encode("cp949"))
            (root / "unknown.c").write_bytes(b"\x81\x40\x81\x41")
            (root / "Debug").mkdir(); (root / "Debug" / "skip.c").write_bytes("// 한글".encode("cp949"))
            items = {item.relative_path.name: item for item in scan_folder(root)}
            self.assertEqual(items["utf8.c"].status, "유지")
            self.assertEqual(items["bom.h"].encoding, "UTF-8 BOM")
            self.assertEqual(items["legacy.c"].encoding, "CP949/EUC-KR")
            self.assertEqual(items["legacy.c"].newline, "CRLF")
            self.assertEqual(items["unknown.c"].status, "검토 필요")
            self.assertNotIn("skip.c", items)

    def test_backup_conversion_preserves_original_and_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.c"
            original = "// 한글\r\nint value;\r\n".encode("cp949")
            source.write_bytes(original)
            results = convert_items(scan_folder(root), "backup")
            self.assertEqual(results[0].action, "변환 및 .bak 백업")
            self.assertEqual(source.with_name("legacy.c.bak").read_bytes(), original)
            self.assertEqual(source.read_bytes(), "// 한글\r\nint value;\r\n".encode("utf-8"))

    def test_folder_conversion_does_not_modify_original_or_unknown_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"; root.mkdir()
            output = Path(directory) / "output"
            legacy = root / "legacy.c"; raw = "// 한글\n".encode("cp949"); legacy.write_bytes(raw)
            unknown = root / "unknown.c"; unknown.write_bytes(b"\x81\x40")
            convert_items(scan_folder(root), "folder", output)
            self.assertEqual(legacy.read_bytes(), raw)
            self.assertEqual((output / "legacy.c").read_text(encoding="utf-8"), "// 한글\n")
            self.assertFalse((output / "unknown.c").exists())


if __name__ == "__main__":
    unittest.main()
