import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from iar_migration_ui import IarProjectMigrationDialog  # noqa: E402


class IarMigrationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_simplified_destination_requires_a_different_first_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "LegacyMain" / "Firmware"
            ewarm = source / "EWARM"
            ewarm.mkdir(parents=True)
            workspace = ewarm / "OLD_BOARD.eww"
            workspace.write_text(
                '<workspace><project><path>$WS_DIR$\\OLD_BOARD.ewp</path></project></workspace>',
                encoding="utf-8",
            )
            (ewarm / "OLD_BOARD.ewp").write_text("<project />", encoding="utf-8")
            settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
            dialog = IarProjectMigrationDialog(settings)
            self.assertTrue(dialog._load_workspace(str(workspace), notify=False))
            self.assertFalse(dialog.run_button.isEnabled())
            dialog.new_first_path_edit.setText(str(root / "303-J-00-X-01"))
            self.assertTrue(dialog.run_button.isEnabled())
            self.assertEqual(
                dialog._options().target_root,
                str(root / "303-J-00-X-01" / "Firmware"),
            )
            self.assertEqual(dialog._options().new_keyword, "OLD_BOARD")
            self.assertEqual(dialog.final_path_label.text(), str(root / "303-J-00-X-01" / "Firmware"))
            self.assertEqual(dialog.progress.minimumHeight(), 4)
            self.assertEqual(dialog.progress.maximumHeight(), 4)
            dialog.close()


if __name__ == "__main__":
    unittest.main()
