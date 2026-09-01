import tempfile
import unittest
from pathlib import Path

from github_repository import GitHubRepositoryError, RepositoryRequest, detect_iar_projects, iter_upload_files


class GitHubRepositoryTests(unittest.TestCase):
    def test_repository_only_mode(self):
        request = RepositoryRequest("https://github.com/orgs/Esol-Lab", "demo", "desc")
        self.assertEqual(request.owner, "Esol-Lab")
        self.assertEqual(request.owner_endpoint, "/orgs/")
        self.assertEqual(request.mode, "repository_only")

    def test_folder_without_iar_mode_and_filters_build_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "App").mkdir()
            (root / "App" / "main.c").write_text("int main(void){}", encoding="utf-8")
            (root / "Debug").mkdir()
            (root / "Debug" / "main.map").write_text("build", encoding="utf-8")
            request = RepositoryRequest("https://github.com/orgs/Esol-Lab", "demo", local_path=str(root))
            self.assertEqual(request.mode, "folder_without_iar")
            self.assertEqual([item.name for item in iter_upload_files(root)], ["main.c"])

    def test_iar_project_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "EWARM").mkdir()
            (root / "EWARM" / "project.ewp").write_text("", encoding="utf-8")
            request = RepositoryRequest("https://github.com/orgs/Esol-Lab", "demo", local_path=str(root))
            self.assertEqual(request.mode, "iar_project_present")
            self.assertEqual(len(detect_iar_projects(root)), 1)

    def test_validation_rejects_invalid_dashboard(self):
        with self.assertRaises(GitHubRepositoryError):
            RepositoryRequest("https://example.com/org/Esol-Lab", "demo").validate()

    def test_user_dashboard_is_supported(self):
        request = RepositoryRequest("https://github.com/Choi-KwangHo", "demo")
        self.assertEqual(request.owner, "Choi-KwangHo")
        self.assertEqual(request.owner_endpoint, "/users/")


if __name__ == "__main__":
    unittest.main()
