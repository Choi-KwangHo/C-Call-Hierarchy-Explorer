import json
import tempfile
import unittest
from pathlib import Path

from eeprom_map import (
    EepromSourceConfig, analyze_eeprom_source, parse_github_location,
)


class EepromMapTests(unittest.TestCase):
    def test_github_repository_and_tree_urls_are_normalized(self) -> None:
        clone, branch, subpath = parse_github_location(
            "https://github.com/Esol-Lab/Susan-Heavy-duty-lift-48V.git", "main"
        )
        self.assertEqual(clone, "https://github.com/Esol-Lab/Susan-Heavy-duty-lift-48V.git")
        self.assertEqual(branch, "main")
        self.assertEqual(subpath, "")

        clone, branch, subpath = parse_github_location(
            "https://github.com/example/firmware/tree/develop/App", ""
        )
        self.assertEqual(clone, "https://github.com/example/firmware.git")
        self.assertEqual(branch, "develop")
        self.assertEqual(subpath, "App")

    def test_at24c128_pages_structures_and_physical_usage_are_linked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.h").write_text(
                """
                typedef unsigned char u8;
                typedef unsigned short u16;
                #define EEPROM_PAGE_SIZE 64
                #define EEPROM_ADDR_PAGE0 0
                #define EEPROM_ADDR_PAGE1 (EEPROM_ADDR_PAGE0 + EEPROM_PAGE_SIZE)
                typedef struct eeSettings {
                    u16 voltage;
                    u8 mode;
                    u8 reserved;
                } eeSettings;
                extern eeSettings settings;
                """,
                encoding="utf-8",
            )
            (root / "memory.c").write_text(
                """
                #include "memory.h"
                eeSettings settings;
                void load(void) {
                    EEPROM_Read(EEPROM_ADDR_PAGE1, (u8*)&settings, sizeof(settings), 1);
                }
                void save(void) {
                    EEPROM_Write(EEPROM_ADDR_PAGE1, (u8*)&settings, sizeof(settings), 1);
                }
                """,
                encoding="utf-8",
            )
            config = EepromSourceConfig.create(
                "fixture", repository_url=str(root), capacity=16384,
                page_size=64, auto_refresh=True, refresh_minutes=3,
            )
            result = analyze_eeprom_source(config, "", root / "cache")
            allocated = [item for item in result.regions if item.allocated]
            self.assertEqual(len(allocated), 1)
            self.assertEqual(allocated[0].address, 64)
            self.assertEqual(allocated[0].payload_size, 4)
            self.assertEqual(allocated[0].size, 64)
            self.assertEqual(allocated[0].struct_name, "eeSettings")
            self.assertEqual(result.used_bytes, 64)
            self.assertEqual([item.name for item in result.structures], ["eeSettings"])
            self.assertIn("typedef struct eeSettings", result.structures[0].declaration)

    def test_overlap_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "map.c").write_text(
                """
                #define EEPROM_ADDR_A 0
                #define EEPROM_ADDR_B 32
                void f(void) {
                    EEPROM_Write(EEPROM_ADDR_A, data, 64, 0);
                    EEPROM_Write(EEPROM_ADDR_B, data, 64, 0);
                }
                """,
                encoding="utf-8",
            )
            result = analyze_eeprom_source(
                EepromSourceConfig.create("overlap", repository_url=str(root)), "", root / "cache"
            )
            self.assertTrue(any("겹칩니다" in warning for warning in result.warnings))
            self.assertTrue(all(item.status == "중복 영역" for item in result.regions if item.allocated))


if __name__ == "__main__":
    unittest.main()
