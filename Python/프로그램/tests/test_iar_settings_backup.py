import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from iar_settings_backup import (
    apply_settings_to_current_project, ensure_default_global_settings,
    load_settings_backup, save_current_as_global_settings,
)


DBGDT = b"""<?xml version='1.0' encoding='utf-8'?>
<settings><WindowStorage><ChildIdMap><WIN_STATIC_WATCH>1</WIN_STATIC_WATCH>
<WIN_TIMELINE_GRAPH>2</WIN_TIMELINE_GRAPH></ChildIdMap><Desktop>
<IarPane-1><expressions><item>watched_value</item></expressions></IarPane-1>
<IarPane-2><TimelineMode>SOURCE</TimelineMode></IarPane-2>
</Desktop></WindowStorage></settings>"""


def dnx(hardware: str, trace: str) -> bytes:
    return (
        "<settings><JLinkDriver><MemConfigValue>" + hardware + "</MemConfigValue>"
        "<SWOTrace>" + trace + "</SWOTrace></JLinkDriver>"
        "<EventLog><Enabled>1</Enabled></EventLog></settings>"
    ).encode()


class IarGlobalSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "Project" / "EWARM"
        self.settings = self.project / "settings"
        self.settings.mkdir(parents=True)
        self.workspace = self.project / "Project.eww"
        self.workspace.write_text("<workspace><project><path>Target_ClassB.ewp</path></project></workspace>")
        (self.project / "Target_ClassB.ewp").write_text("<project />")
        (self.root / "Project" / "main.c").write_text("int watched_value; int main(void){return watched_value;}")
        (self.settings / "Target_ClassB.dbgdt").write_bytes(DBGDT.replace(b"SOURCE", b"TARGET"))
        (self.settings / "Target_ClassB.dnx").write_bytes(dnx("TARGET_DEVICE", "old"))
        (self.settings / "Target_ClassB.crun").write_text("<settings><Run>target</Run></settings>")

    def tearDown(self):
        self.temp.cleanup()

    def _global_folder(self) -> Path:
        folder = self.root / "global"
        folder.mkdir()
        (folder / "Global.dbgdt").write_bytes(DBGDT)
        (folder / "Global.dnx").write_bytes(dnx("WRONG_DEVICE", "new"))
        (folder / "Global.crun").write_text("<settings><Run>global</Run></settings>")
        return folder

    def test_apply_preserves_target_hardware_and_renames_for_active_project(self):
        folder = self._global_folder()
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")}):
            result = apply_settings_to_current_project(
                self.workspace,
                load_settings_backup(folder, "live_watch"),
                load_settings_backup(folder, "ctrace"),
            )
        root = ET.fromstring((self.settings / "Target_ClassB.dnx").read_bytes())
        self.assertEqual(root.findtext("./JLinkDriver/MemConfigValue"), "TARGET_DEVICE")
        self.assertEqual(root.findtext("./JLinkDriver/SWOTrace"), "new")
        self.assertTrue(result.preserved_hardware_settings)
        self.assertTrue(result.backup_folders)
        self.assertFalse((self.settings / "Global.dnx").exists())

    def test_save_current_as_global_uses_generic_names_and_keeps_history(self):
        folder = self.root / "global"
        save_current_as_global_settings(self.workspace, folder, True, True)
        self.assertTrue((folder / "Global.dbgdt").is_file())
        self.assertTrue((folder / "Global.dnx").is_file())
        manifest = json.loads((folder / "global-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_project"], "Target")
        save_current_as_global_settings(self.workspace, folder, True, True)
        self.assertTrue(any((folder / ".history").rglob("Global.dnx")))

    def test_bundled_defaults_are_generic_and_never_overwrite_custom_file(self):
        destination = self.root / "defaults"
        seeded = ensure_default_global_settings(destination)
        self.assertTrue((seeded / "Global.dbgdt").is_file())
        self.assertTrue((seeded / "Global.dnx").is_file())
        self.assertFalse(any("PCB031" in path.name for path in seeded.iterdir()))
        custom = seeded / "Global.crun"
        custom.write_text("<settings><custom>1</custom></settings>")
        ensure_default_global_settings(destination)
        self.assertIn("custom", custom.read_text())


if __name__ == "__main__":
    unittest.main()
