from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from eeprom_cache import EepromResultCacheStore  # noqa: E402
from eeprom_map import (  # noqa: E402
    AnalysisCancelled, EepromMapResult, EepromRegion, EepromSourceConfig,
    save_source_configs,
)
from eeprom_ui import EepromMapDialog, EepromSourceSettingsDialog  # noqa: E402


class EepromUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_remove_subdirectory_and_round_trip_local_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = EepromSourceConfig.create(
                "Local firmware", source_type="local", repository_url=temporary,
                subdirectory="legacy/path",
            )
            dialog = EepromSourceSettingsDialog([config], "")
            self.assertEqual(dialog.table.columnCount(), 8)
            saved = dialog.configs()[0]
            self.assertTrue(saved.is_local)
            self.assertEqual(saved.subdirectory, "")
            dialog.close()

    def test_integrated_map_structure_and_code_splitters_resize_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.c").write_text(
                """
                typedef unsigned char u8;
                #define EEPROM_PAGE0 0
                #define EEPROM_PAGE1 1
                typedef struct Settings { u8 mode; } Settings;
                Settings value;
                u8 raw[64];
                void save(void){ EEPROM_Write(EEPROM_PAGE0, (u8*)&value, sizeof(value), 0); }
                void load_raw(void){ EEPROM_Read(EEPROM_PAGE1, raw, 64, 0); }
                """,
                encoding="utf-8",
            )
            settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
            config = EepromSourceConfig.create(
                "Local firmware", source_type="local", repository_url=str(root),
                auto_refresh=False,
            )
            save_source_configs(settings, [config])
            dialog = EepromMapDialog(settings, "")
            dialog.show()
            deadline = time.monotonic() + 5
            while (dialog.worker is not None or not dialog.results) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertTrue(dialog.results)
            self.assertEqual(dialog.content_splitter.count(), 2)
            self.assertGreaterEqual(dialog.canvas.minimumHeight(), 335)
            self.assertGreaterEqual(dialog.canvas.minimumWidth(), 640)
            self.assertIs(dialog.map_scroll.widget(), dialog.canvas)
            dialog.map_scroll.resize(420, 360)
            self.app.processEvents()
            self.assertGreaterEqual(dialog.map_scroll.horizontalScrollBar().maximum(), 0)
            self.assertGreater(dialog.structure_table.rowCount(), 0)
            self.assertTrue(dialog.code_preview.verticalScrollBar() is not None)
            for row in range(dialog.region_table.rowCount()):
                region = dialog.region_table.item(row, 0).data(Qt.UserRole)
                if region.actual_usage and not region.struct_name:
                    dialog.region_table.selectRow(row)
                    self.app.processEvents()
                    break
            self.assertIn("정의된 구조체 없음", dialog.structure_caption.text())
            self.assertIn("정의된 구조체 없음", dialog.code_preview.toPlainText())
            self.assertIn("EEPROM_Read", dialog.code_preview.toPlainText())
            self.assertTrue(dialog.code_preview.extraSelections())
            self.assertIn("선택 영역 판단 근거", dialog.warning_view.toPlainText())
            self.assertEqual(dialog.progress.minimumHeight(), 4)
            self.assertEqual(dialog.progress.maximumHeight(), 4)
            dialog._toggle_fullscreen()
            self.assertTrue(dialog.isFullScreen())
            dialog._toggle_fullscreen()
            dialog.close()

    def test_result_cache_restores_previous_analysis_and_rejects_changed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = EepromSourceConfig.create(
                "Cached firmware", source_type="local", repository_url=str(root),
            )
            result = EepromMapResult(
                config=config, source_root=str(root), commit="local-revision",
                regions=[EepromRegion(
                    name="PAGE0", address=0, size=64, page=0, struct_name="",
                    path="memory.c", lines=[1], access="쓰기", confidence="확정",
                    evidence="test",
                )], structures=[], used_bytes=64,
            )
            store = EepromResultCacheStore(root / "cache")
            store.save(result)
            restored = store.load(config)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.commit, "local-revision")
            self.assertEqual(restored.used_bytes, 64)
            changed = EepromSourceConfig.from_dict({
                "id": config.id, "display_name": config.display_name,
                "source_type": "local", "repository_url": str(root),
                "capacity": 8192, "page_size": 64,
            })
            self.assertIsNone(store.load(changed))

    def test_corrupt_result_cache_is_quarantined_without_blocking_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = EepromSourceConfig.create(
                "Cached firmware", source_type="local", repository_url=str(root),
            )
            store = EepromResultCacheStore(root / "cache")
            cache_path = store.path_for(config)
            cache_path.parent.mkdir(parents=True)
            cache_path.write_bytes(b"not a valid cache")
            self.assertIsNone(store.load(config))
            self.assertFalse(cache_path.exists())
            self.assertEqual(len(list(cache_path.parent.glob(cache_path.name + ".corrupt-*"))), 1)

    def test_switching_source_cancels_old_transaction_and_clears_stale_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
            first = EepromSourceConfig.create(
                "First", source_type="local", repository_url=str(first_root), auto_refresh=False,
            )
            second = EepromSourceConfig.create(
                "Second", source_type="local", repository_url=str(second_root), auto_refresh=False,
            )
            save_source_configs(settings, [first, second])
            first_started = threading.Event()
            second_started = threading.Event()
            release_second = threading.Event()

            def fake_analyze(config, current_root, cache_root, progress=None, cancelled=None):
                if config.id == first.id:
                    first_started.set()
                    while not cancelled():
                        time.sleep(0.01)
                    raise AnalysisCancelled("cancelled")
                second_started.set()
                while not release_second.is_set():
                    if cancelled():
                        raise AnalysisCancelled("cancelled")
                    time.sleep(0.01)
                return EepromMapResult(
                    config=config, source_root=str(second_root), commit="second-revision",
                    regions=[], structures=[], used_bytes=0,
                )

            with patch("eeprom_ui.analyze_eeprom_source", side_effect=fake_analyze):
                dialog = EepromMapDialog(settings, "")
                dialog.show()
                deadline = time.monotonic() + 3
                while not first_started.is_set() and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(first_started.is_set())
                dialog.source_combo.setCurrentIndex(1)
                self.app.processEvents()
                self.assertEqual(dialog.region_table.rowCount(), 0)
                self.assertEqual(dialog.structure_table.rowCount(), 0)
                self.assertIn("Second", dialog.code_preview.toPlainText())
                deadline = time.monotonic() + 3
                while not second_started.is_set() and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(second_started.is_set())
                release_second.set()
                deadline = time.monotonic() + 3
                while dialog.worker is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertNotIn(first.id, dialog.results)
                self.assertIn(second.id, dialog.results)
                self.assertEqual(dialog._displayed_result.config.id, second.id)
                dialog.close()


if __name__ == "__main__":
    unittest.main()
