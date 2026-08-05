import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eeprom_map import (
    EepromSourceConfig, analyze_eeprom_source, parse_github_location,
    save_source_configs, source_revision,
)


class MemorySettings:
    def __init__(self) -> None:
        self.values = {}

    def value(self, key, default=""):
        return self.values.get(key, default)

    def setValue(self, key, value) -> None:  # noqa: N802 - Qt-compatible test stub
        self.values[key] = value

    def sync(self) -> None:
        pass


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
        self.assertEqual(subpath, "")

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

    def test_local_source_change_updates_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "map.c"
            source.write_text("void first(void) {}\n", encoding="utf-8")
            config = EepromSourceConfig.create(
                "local", source_type="local", repository_url=str(root)
            )
            before = source_revision(config, "")
            source.write_text("void second(void) { int changed = 1; }\n", encoding="utf-8")
            after = source_revision(config, "")
            self.assertNotEqual(before, after)

    def test_legacy_subdirectory_is_ignored_and_full_root_is_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "map.c").write_text(
                "#define EEPROM_PAGE0 0\nvoid save(void){EEPROM_Write(EEPROM_PAGE0,data,8,0);}\n",
                encoding="utf-8",
            )
            config = EepromSourceConfig.create(
                "full-root", source_type="local", repository_url=str(root),
                subdirectory="folder-that-does-not-exist",
            )
            result = analyze_eeprom_source(config, "", root / "cache")
            self.assertTrue(result.regions)
            self.assertEqual(Path(result.source_root), root)

    def test_local_sources_are_saved_for_user_but_excluded_from_deploy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "eeprom_sources.json"
            settings = MemorySettings()
            github = EepromSourceConfig.create(
                "remote", source_type="github",
                repository_url="https://github.com/example/firmware.git",
            )
            local = EepromSourceConfig.create(
                "local", source_type="local", repository_url=str(root)
            )
            with patch("eeprom_map.source_catalog_path", return_value=catalog):
                save_source_configs(settings, [github, local], deploy_default=True)

            user_items = json.loads(settings.values["eeprom/sourceItems"])["items"]
            deploy_items = json.loads(catalog.read_text(encoding="utf-8"))["items"]
            self.assertEqual([item["display_name"] for item in user_items], ["remote", "local"])
            self.assertEqual([item["display_name"] for item in deploy_items], ["remote"])


if __name__ == "__main__":
    unittest.main()
