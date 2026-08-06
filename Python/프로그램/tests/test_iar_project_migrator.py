import tempfile
import unittest
import json
from pathlib import Path

from iar_project_migrator import (
    MigrationError, MigrationOptions, inspect_iar_workspace, migrate_iar_project,
    preview_iar_migration, synchronize_cubemx_ioc, synchronize_ewp_project_name,
)
from iar_settings_backup import create_settings_backup, load_settings_backup


def options(source: Path, target: Path) -> MigrationOptions:
    return MigrationOptions(
        str(source), str(target), "OLD_BOARD", "NEW-BOARD",
        r"LegacyProduct\OLD_BOARD", r"NewProduct\NEW-BOARD",
    )


class IarProjectMigratorTests(unittest.TestCase):
    @staticmethod
    def _write_debug_settings(ewarm: Path) -> Path:
        settings = ewarm / "settings"
        settings.mkdir(parents=True)
        dbgdt = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<Project><WindowStorage><ChildIdMap><WIN_STATIC_WATCH>7</WIN_STATIC_WATCH>"
            "</ChildIdMap><Desktop><IarPane-7><expressions>"
            "<item>present_value</item><item>removed_value</item><item></item>"
            "</expressions></IarPane-7></Desktop></WindowStorage></Project>"
        )
        (settings / "OLD_BOARD_ClassB.dbgdt").write_text(dbgdt, encoding="utf-8")
        (settings / "OLD_BOARD_ClassB.dnx").write_text(
            "<settings><EventLog><LogEnabled>1</LogEnabled></EventLog>"
            "<SWOTraceHWSettings><ITMlogFile>$PROJ_DIR$\\OLD_BOARD.log</ITMlogFile>"
            "</SWOTraceHWSettings></settings>", encoding="utf-8",
        )
        (settings / "OLD_BOARD_ClassB.crun").write_text(
            "<crun><filter_entries><filter><action_log>1</action_log></filter>"
            "</filter_entries></crun>", encoding="utf-8",
        )
        (settings / "Project.wsdt").write_text(
            "<Workspace><ConfigDictionary><CurrentConfigs><Project>OLD_BOARD/OLD_BOARD"
            "</Project></CurrentConfigs></ConfigDictionary></Workspace>", encoding="utf-8",
        )
        return settings

    def test_transactional_clone_renames_and_rewrites_iar_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "output" / "NEW-BOARD"
            ewarm = source / "OLD_BOARD" / "EWARM"
            ewarm.mkdir(parents=True)
            (ewarm / "OLD_BOARD_ClassB.ewp").write_text(
                "<root><project><name>OLD_BOARD--ClassB</name></project></root>\n"
                r"LegacyProduct\OLD_BOARD\Src" + "\n",
                encoding="utf-8",
            )
            (ewarm / "OLD_BOARD.eww").write_text(
                "OLD_BOARD\nLegacyProduct/OLD_BOARD/EWARM\n", encoding="utf-8",
            )
            (source / "OLD_BOARD_ClassB.ioc").write_text(
                "#MicroXplorer Configuration settings - do not modify\n"
                "Mcu.Name=STM32F103VCTx\n"
                "RCC.HSE_VALUE=8000000\n"
                "ProjectManager.ProjectFileName=OLD_BOARD_ClassB.ioc\n"
                "ProjectManager.ProjectName=OLD_BOARD_ClassB\n",
                encoding="utf-8",
            )
            (source / ".mxproject").write_text(
                "[PreviousGenFiles]\nSourcePath#0=..\\Core\\Src\n", encoding="utf-8"
            )
            (source / "Src").mkdir()
            original_source = "// 한글\nconst char *project = \"OLD_BOARD\";\n"
            (source / "Src" / "main.c").write_bytes(original_source.encode("cp949"))
            for ignored in ("Debug", "Release", ".iar", "settings"):
                folder = source / ignored
                folder.mkdir()
                (folder / "ignored.bin").write_bytes(b"ignored")
            (source / "build.dep").write_text("ignored")
            (source / "database.pbd").write_text("ignored")

            result = migrate_iar_project(options(source, target))

            project = target / "NEW-BOARD" / "EWARM" / "NEW-BOARD_ClassB.ewp"
            workspace = target / "NEW-BOARD" / "EWARM" / "NEW-BOARD.eww"
            ioc = target / "NEW-BOARD_ClassB.ioc"
            self.assertTrue(project.is_file())
            self.assertTrue(workspace.is_file())
            project_text = project.read_text(encoding="utf-8")
            self.assertIn("<name>NEW-BOARD_ClassB</name>", project_text)
            self.assertIn(r"NewProduct\NEW-BOARD\Src", project_text)
            self.assertIn("NewProduct/NEW-BOARD/EWARM", workspace.read_text(encoding="utf-8"))
            ioc_text = ioc.read_text(encoding="utf-8")
            self.assertIn("ProjectManager.ProjectFileName=NEW-BOARD_ClassB.ioc", ioc_text)
            self.assertIn("ProjectManager.ProjectName=NEW-BOARD_ClassB", ioc_text)
            self.assertIn("RCC.HSE_VALUE=8000000", ioc_text)
            self.assertEqual(
                (target / ".mxproject").read_text(encoding="utf-8"),
                "[PreviousGenFiles]\nSourcePath#0=..\\Core\\Src\n",
            )
            self.assertIn("NEW-BOARD", (target / "Src" / "main.c").read_bytes().decode("cp949"))
            for ignored in ("Debug", "Release", ".iar", "settings", "build.dep", "database.pbd"):
                self.assertFalse((target / ignored).exists())
            self.assertEqual((source / "Src" / "main.c").read_bytes().decode("cp949"), original_source)
            self.assertEqual(result.renamed_files, 3)
            self.assertEqual(result.modified_files, 4)
            self.assertEqual(result.project_names_updated, 1)

    def test_preview_does_not_create_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "OLD_BOARD.eww").write_text("OLD_BOARD", encoding="utf-8")
            target = root / "target"
            result = preview_iar_migration(options(source, target))
            self.assertEqual(result.copied_files, 1)
            self.assertEqual(result.renamed_files, 1)
            self.assertEqual(result.modified_files, 1)
            self.assertFalse(target.exists())

    def test_non_empty_target_is_preserved_when_paths_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "main.c").write_text("OLD_BOARD", encoding="utf-8")
            protected = target / "keep.txt"
            protected.write_text("do not change", encoding="utf-8")
            migrate_iar_project(options(source, target))
            self.assertEqual(protected.read_text(encoding="utf-8"), "do not change")
            self.assertEqual((target / "main.c").read_text(encoding="utf-8"), "NEW-BOARD")

    def test_existing_target_collision_never_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "OLD_BOARD.ewp").write_text("OLD_BOARD", encoding="utf-8")
            protected = target / "NEW-BOARD.ewp"
            protected.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(MigrationError, "충돌"):
                migrate_iar_project(options(source, target))
            self.assertEqual(protected.read_text(encoding="utf-8"), "keep")

    def test_rename_collision_leaves_no_partial_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "OLD_BOARD.ewp").write_text("OLD_BOARD", encoding="utf-8")
            (source / "NEW-BOARD.ewp").write_text("NEW-BOARD", encoding="utf-8")
            target = root / "target"
            with self.assertRaisesRegex(MigrationError, "충돌"):
                migrate_iar_project(options(source, target))
            self.assertFalse(target.exists())
            self.assertFalse(list(root.glob(".target.iar-migrate-*")))

    def test_cancelled_clone_removes_staging_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for index in range(4):
                (source / f"file{index}.c").write_text("OLD_BOARD", encoding="utf-8")
            target = root / "new-main" / "new-sub"
            calls = 0

            def cancelled() -> bool:
                nonlocal calls
                calls += 1
                return calls > 3

            with self.assertRaisesRegex(RuntimeError, "취소"):
                migrate_iar_project(options(source, target), cancelled=cancelled)
            self.assertFalse(target.exists())
            self.assertFalse((root / "new-main").exists())
            self.assertFalse(list(root.glob(".new-sub.iar-migrate-*")))

    def test_project_name_sync_preserves_surrounding_xml(self) -> None:
        source = "<root>\n<project attr=\"1\">\n  <name>old</name>\n</project>\n</root>"
        updated, count = synchronize_ewp_project_name(source, "new_name")
        self.assertEqual(count, 1)
        self.assertIn('<project attr="1">\n  <name>new_name</name>', updated)
        self.assertTrue(updated.startswith("<root>\n"))

    def test_workspace_auto_detection_uses_two_level_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "LegacyMain" / "Firmware"
            ewarm = root / "EWARM"
            ewarm.mkdir(parents=True)
            workspace = ewarm / "PCB031_48_ESS_UPS_V1.2.eww"
            workspace.write_text(
                '<workspace><project><path>$WS_DIR$\\PCB031_48_ESS_UPS_V1.2_ClassB.ewp</path></project></workspace>',
                encoding="utf-8",
            )
            info = inspect_iar_workspace(workspace)
            self.assertEqual(info.source_root, str(root.resolve()))
            self.assertEqual(info.first_folder, "LegacyMain")
            self.assertEqual(info.second_folder, "Firmware")
            self.assertEqual(info.project_keyword, "PCB031_48_ESS_UPS_V1.2")
            self.assertEqual(len(info.referenced_projects), 1)

    def test_generic_project_workspace_detects_keyword_from_iar_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Susan-Heavy-duty-lift-48V" / "48V_HDL"
            ewarm = root / "EWARM"
            ewarm.mkdir(parents=True)
            workspace = ewarm / "Project.eww"
            workspace.write_text(
                '<workspace><project><path>$WS_DIR$\\PCB031_48_ESS_UPS_V1.2_ClassB.ewp</path></project></workspace>',
                encoding="utf-8",
            )
            (ewarm / "PCB031_48_ESS_UPS_V1.2_ClassB.ewp").write_text("<project />", encoding="utf-8")
            (ewarm / "PCB031_48_ESS_UPS_V1.2.ewt").write_text("template", encoding="utf-8")
            info = inspect_iar_workspace(workspace)
            self.assertEqual(info.project_keyword, "PCB031_48_ESS_UPS_V1.2")

    def test_cubemx_ioc_changes_only_project_manager_identity(self) -> None:
        source = (
            "Mcu.Name=OLD_BOARD\n"
            "PA0.Signal=ADC1_IN0\n"
            "ProjectManager.ProjectFileName=OLD_BOARD.ioc\n"
            "ProjectManager.ProjectName=OLD_BOARD\n"
            "ProjectManager.functionlistsort=OLD_BOARD-runtime-label\n"
        )
        updated, count = synchronize_cubemx_ioc(source, options(Path("source"), Path("target")))
        self.assertEqual(count, 2)
        self.assertIn("Mcu.Name=OLD_BOARD", updated)
        self.assertIn("PA0.Signal=ADC1_IN0", updated)
        self.assertIn("ProjectManager.ProjectFileName=NEW-BOARD.ioc", updated)
        self.assertIn("ProjectManager.functionlistsort=OLD_BOARD-runtime-label", updated)

    def test_same_project_name_is_allowed_when_only_location_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "old" / "Firmware"
            source.mkdir(parents=True)
            (source / "main.c").write_text("OLD_BOARD", encoding="utf-8")
            target = root / "new" / "Firmware"
            same_name = MigrationOptions(str(source), str(target), "OLD_BOARD", "OLD_BOARD")
            migrate_iar_project(same_name)
            self.assertTrue((target / "main.c").is_file())

    def test_live_watch_and_ctrace_are_restored_and_missing_variable_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "old" / "Firmware"
            ewarm = source / "EWARM"
            ewarm.mkdir(parents=True)
            workspace = ewarm / "OLD_BOARD.eww"
            workspace.write_text("<workspace>OLD_BOARD</workspace>", encoding="utf-8")
            self._write_debug_settings(ewarm)
            (source / "main.c").write_text("int present_value;", encoding="utf-8")
            target = root / "new" / "Firmware"
            configured = options(source, target)
            configured.source_workspace = str(workspace)
            configured.copy_live_watch = True
            configured.copy_ctrace = True

            result = migrate_iar_project(configured)

            restored = target / "EWARM" / "settings"
            self.assertTrue((restored / "NEW-BOARD_ClassB.dnx").is_file())
            self.assertTrue((restored / "NEW-BOARD_ClassB.crun").is_file())
            dbgdt = (restored / "NEW-BOARD_ClassB.dbgdt").read_text(encoding="utf-8")
            self.assertIn("present_value", dbgdt)
            self.assertNotIn("removed_value", dbgdt)
            self.assertEqual(result.watch_expressions_retained, 1)
            self.assertEqual(result.watch_expressions_omitted, ["removed_value"])
            self.assertEqual(result.settings_files_written, 4)
            self.assertIn("NEW-BOARD.log", (restored / "NEW-BOARD_ClassB.dnx").read_text(encoding="utf-8"))

    def test_project_scoped_backup_and_external_folder_autoload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ewarm = root / "source" / "EWARM"
            ewarm.mkdir(parents=True)
            workspace = ewarm / "OLD_BOARD.eww"
            workspace.write_text("<workspace />", encoding="utf-8")
            self._write_debug_settings(ewarm)
            backup_root = root / "appdata-backups"

            saved = create_settings_backup(workspace, "OLD_BOARD", "live_watch", backup_root)
            self.assertEqual(saved.parents[2], backup_root)
            self.assertEqual(saved.parents[1].name, "OLD_BOARD")
            manifest = json.loads((saved / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["category"], "live_watch")

            # Selecting the parent project folder automatically resolves the
            # newest compatible snapshot below it.
            loaded = load_settings_backup(backup_root / "OLD_BOARD", "live_watch")
            self.assertTrue(any(name.endswith(".dbgdt") for name in loaded.files))


if __name__ == "__main__":
    unittest.main()
