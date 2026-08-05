from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from window_state import restore_window_state, save_window_state, screen_topology  # noqa: E402


class WindowStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_same_monitor_topology_restores_normal_geometry_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.IniFormat)
            first = QWidget()
            first.setGeometry(90, 110, 900, 620)
            save_window_state(first, settings, "window")
            settings.setValue("window/mode", "fullscreen")

            second = QWidget()
            mode = restore_window_state(second, settings, "window", (640, 480))
            self.assertEqual(mode, "fullscreen")
            self.assertEqual(
                (second.geometry().x(), second.geometry().y(), second.width(), second.height()),
                (90, 110, 900, 620),
            )

    def test_changed_monitor_topology_resets_to_primary_screen_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.IniFormat)
            settings.setValue("window/screenTopology", screen_topology() + "changed")
            settings.setValue("window/normalGeometry", "[9000,9000,1200,800]")
            settings.setValue("window/mode", "fullscreen")
            widget = QWidget()
            mode = restore_window_state(widget, settings, "window", (800, 600))
            primary = QApplication.primaryScreen().availableGeometry()
            self.assertEqual(mode, "normal")
            self.assertTrue(primary.contains(widget.geometry().center()))
            self.assertLessEqual(widget.width(), primary.width())
            self.assertLessEqual(widget.height(), primary.height())


if __name__ == "__main__":
    unittest.main()
