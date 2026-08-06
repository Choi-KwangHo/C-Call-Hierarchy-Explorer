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

    def test_two_level_destination_and_thin_progress_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.IniFormat)
            dialog = IarProjectMigrationDialog(settings)
            dialog.target_base_edit.setText(str(Path(temporary) / "destination"))
            dialog.new_first_folder.setText("303-J-00-X-01")
            dialog.new_second_folder.setText("TEST_BD")
            self.assertEqual(
                dialog.target_edit.text(),
                str(Path(temporary) / "destination" / "303-J-00-X-01" / "TEST_BD"),
            )
            self.assertEqual(dialog.new_path.text(), r"303-J-00-X-01\TEST_BD")
            self.assertEqual(dialog.progress.minimumHeight(), 4)
            self.assertEqual(dialog.progress.maximumHeight(), 4)
            dialog.close()


if __name__ == "__main__":
    unittest.main()
