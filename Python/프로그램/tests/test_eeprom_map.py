import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eeprom_map import (
    EepromSourceConfig, analyze_eeprom_source, parse_github_location,
    load_source_configs, save_source_configs, source_revision,
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
    def test_implicit_local_source_id_is_stable_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing_catalog = Path(temporary) / "missing-eeprom-sources.json"
            with patch("eeprom_map.default_catalog_path", return_value=missing_catalog):
                first = load_source_configs(MemorySettings(), temporary)
                second = load_source_configs(MemorySettings(), temporary)
            self.assertEqual(first[0].id, second[0].id)
            self.assertTrue(first[0].id.startswith("auto-"))

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
            self.assertTrue(any(item.kind == "주소 정의" for item in allocated[0].evidence_items))
            self.assertTrue(any(item.kind == "읽기" for item in allocated[0].evidence_items))
            self.assertTrue(any(item.kind == "쓰기" for item in allocated[0].evidence_items))
            self.assertEqual(result.used_bytes, 64)
            self.assertEqual([item.name for item in result.structures], ["eeSettings"])
            self.assertIn("typedef struct eeSettings", result.structures[0].declaration)

    def test_typed_pointer_cast_links_buffer_without_sizeof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.c").write_text(
                """
                typedef unsigned char u8;
                #define EEPROM_ADDR_CONFIG 0x40
                typedef struct DeviceConfig { u8 mode; u8 flags; } DeviceConfig;
                u8 raw[64];
                void load(void) {
                    EEPROM_Read(EEPROM_ADDR_CONFIG, (DeviceConfig *)&raw[0], 64, 0);
                }
                """,
                encoding="utf-8",
            )
            result = analyze_eeprom_source(
                EepromSourceConfig.create("cast", repository_url=str(root)), "", root / "cache"
            )
            used = next(item for item in result.regions if item.actual_usage)
            self.assertEqual(used.struct_name, "DeviceConfig")
            self.assertEqual(used.payload_size, 2)

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
            self.assertTrue(all(item.status == "확정 충돌" for item in result.regions if item.allocated))
            self.assertTrue(all(item.conflict for item in result.regions if item.allocated))

    def test_device_address_and_timeout_are_not_memory_allocations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bsp.c").write_text(
                """
                #define EEPROM_I2C_ADDRESS 0xA0
                void probe(void) {
                    EEPROM_IO_IsDeviceReady(EEPROM_I2C_ADDRESS, 300);
                    EEPROM_IO_ReadData(EEPROM_I2C_ADDRESS, buffer, 64);
                }
                """,
                encoding="utf-8",
            )
            result = analyze_eeprom_source(
                EepromSourceConfig.create("bsp", repository_url=str(root)), "", root / "cache"
            )
            self.assertFalse([item for item in result.regions if item.allocated])
            self.assertEqual(result.used_bytes, 0)

    def test_definition_and_actual_usage_are_preserved_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "map.c").write_text(
                """
                #define EEPROM_ADDR_PAGE0 0
                #define EEPROM_ADDR_PAGE1 64
                void load(void) { EEPROM_Read(EEPROM_ADDR_PAGE1, data, 12, 0); }
                """,
                encoding="utf-8",
            )
            result = analyze_eeprom_source(
                EepromSourceConfig.create("combined", repository_url=str(root)), "", root / "cache"
            )
            actual = next(item for item in result.regions if item.allocated)
            defined_only = next(item for item in result.regions if not item.allocated)
            self.assertTrue(actual.actual_usage)
            self.assertTrue(actual.definition_present)
            self.assertEqual(defined_only.name, "EEPROM_ADDR_PAGE0")
            self.assertFalse(defined_only.actual_usage)
            self.assertTrue(defined_only.definition_present)

    def test_function_style_page_macro_is_resolved_and_linked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "map.c").write_text(
                """
                #define EEPROM_PAGE_SIZE 64
                #define EEPROM_ADDR_PAGE0 0
                #define EEPROM_ADDR_PAGE1 64
                #define EEPROM_ADDR_PAGE2 128
                #define EEPROM_ADDR_PAGE(n) ((n) * EEPROM_PAGE_SIZE)
                typedef struct eeData { unsigned short value; } eeData;
                eeData stored;
                void load(void) {
                    EEPROM_Read(EEPROM_ADDR_PAGE(2), (unsigned char*)&stored, sizeof(stored), 0);
                }
                """,
                encoding="utf-8",
            )
            result = analyze_eeprom_source(
                EepromSourceConfig.create("function-macro", repository_url=str(root)), "", root / "cache"
            )
            actual = next(item for item in result.regions if item.allocated)
            self.assertEqual(actual.address, 128)
            self.assertEqual(actual.page, 2)
            self.assertEqual(actual.struct_name, "eeData")
            self.assertTrue(actual.definition_present)
            self.assertTrue(actual.actual_usage)

    def test_common_at24_library_supports_direct_address_and_arbitrary_structure_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "customer.c").write_text(
                """
                typedef struct CustomerPersistentBlock {
                    unsigned short serial;
                    unsigned char flags[6];
                } CustomerPersistentBlock;
                CustomerPersistentBlock block;
                void load(void) {
                    AT24Cxx_read_byte_buffer(device, (unsigned char*)&block, 0x0200, sizeof(block));
                }
                void save(void) {
                    AT24Cxx_write_byte_buffer(device, (unsigned char*)&block, 0x0200, sizeof(block));
                }
                """,
                encoding="utf-8",
            )
            result = analyze_eeprom_source(
                EepromSourceConfig.create("arbitrary", repository_url=str(root)), "", root / "cache"
            )
            actual = [item for item in result.regions if item.allocated]
            self.assertEqual(len(actual), 1)
            self.assertEqual(actual[0].address, 0x0200)
            self.assertEqual(actual[0].payload_size, 8)
            self.assertEqual(actual[0].size, 8)
            self.assertEqual(actual[0].struct_name, "CustomerPersistentBlock")
            self.assertIn("읽기", actual[0].access)
            self.assertIn("쓰기", actual[0].access)

    def test_named_at24_page_keeps_physical_page_separate_from_crc_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.c").write_text(
                """
                typedef unsigned short vu16;
                #define EEPROM_PAGE_SIZE 64
                #define EEPROM_ADDR_PAGE7 0x01C0
                #define EEPROM_ADDR_PAGE8 (EEPROM_ADDR_PAGE7 + EEPROM_PAGE_SIZE)
                vu16 eeprom_crc[8];
                void load(void) {
                    AT24Cxx_read_byte_buffer(device, (unsigned char*)&eeprom_crc,
                                             EEPROM_ADDR_PAGE8, sizeof(eeprom_crc));
                }
                """,
                encoding="utf-8",
            )
            result = analyze_eeprom_source(
                EepromSourceConfig.create("crc-page", repository_url=str(root)), "", root / "cache"
            )
            region = next(item for item in result.regions if item.name == "EEPROM_ADDR_PAGE8")
            self.assertEqual(region.address, 0x0200)
            self.assertEqual(region.payload_size, 16)
            self.assertEqual(region.size, 64)
            self.assertEqual(region.size - region.payload_size, 48)
            self.assertTrue(region.definition_present)
            self.assertTrue(region.actual_usage)

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
