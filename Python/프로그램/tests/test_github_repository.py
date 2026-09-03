import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from github_repository import GitHubClient, GitHubRepositoryError, GitHubUploadCancelled, RepositoryRequest, detect_iar_projects, iter_upload_files


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
        self.assertEqual(request.owner_endpoint, "/user")
        self.assertFalse(request.is_organization)

    def test_personal_dashboard_uses_authenticated_user_creation_endpoint(self):
        request = RepositoryRequest("https://github.com/Choi-KwangHo", "demo")
        client = GitHubClient("test-token", api_base="https://example.invalid")
        calls = []
        client._request = lambda method, path, payload=None: calls.append((method, path, payload)) or {"html_url": "https://github.com/Choi-KwangHo/demo"}
        client.create_repository(request)
        self.assertEqual(calls[0][0:2], ("POST", "/user/repos"))

    def test_organization_dashboard_uses_organization_creation_endpoint(self):
        request = RepositoryRequest("https://github.com/orgs/Esol-Lab", "demo")
        client = GitHubClient("test-token", api_base="https://example.invalid")
        calls = []
        client._request = lambda method, path, payload=None: calls.append((method, path, payload)) or {"html_url": "https://github.com/Esol-Lab/demo"}
        client.create_repository(request)
        self.assertEqual(calls[0][0:2], ("POST", "/orgs/Esol-Lab/repos"))

    def test_repository_list_marks_empty_repository_for_initial_upload(self):
        request = RepositoryRequest("https://github.com/Choi-KwangHo", "demo")
        client = GitHubClient("test-token", api_base="https://example.invalid")
        client._request = lambda *_args, **_kwargs: [
            {"name": "empty", "full_name": "Choi-KwangHo/empty", "html_url": "https://github.com/Choi-KwangHo/empty", "size": 0, "private": True},
            {"name": "ready", "full_name": "Choi-KwangHo/ready", "html_url": "https://github.com/Choi-KwangHo/ready", "size": 12, "private": False},
        ]
        listed = client.list_repositories(request)
        self.assertTrue(listed[0].is_empty)
        self.assertFalse(listed[1].is_empty)

    def test_initial_upload_uses_single_git_commit_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            request = RepositoryRequest("https://github.com/Choi-KwangHo", "demo", local_path=str(root))
            client = GitHubClient("test-token", api_base="https://example.invalid")
            commands = []
            client._request = lambda *_args, **_kwargs: {"login": "Choi-KwangHo"}
            def fake_git(arguments, *_args, **_kwargs):
                commands.append(arguments)
                return subprocess.CompletedProcess(arguments, 1 if "diff" in arguments else 0, "", "")
            with patch.object(client, "_run_git", side_effect=fake_git), patch.object(client, "upload_file", side_effect=AssertionError("Contents API must not be used")):
                result = client.upload_project(request)
            self.assertEqual(result["uploaded_files"], 1)
            self.assertTrue(any(command[0] == "clone" for command in commands))
            self.assertTrue(any(command[-2:] == ["-m", "Initial IAR project upload"] for command in commands))
            self.assertTrue(any(command[0] == "-C" and "push" in command for command in commands))

    def test_same_remote_file_needs_no_push(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"; root.mkdir(); (root / "main.c").write_text("same", encoding="utf-8")
            request = RepositoryRequest("https://github.com/Choi-KwangHo", "demo", local_path=str(root))
            client = GitHubClient("test-token", api_base="https://example.invalid")
            commands = []
            client._request = lambda *_args, **_kwargs: {"login": "Choi-KwangHo"}
            def fake_git(arguments, _cwd, _env, *_args, **_kwargs):
                commands.append(arguments)
                if arguments[0] == "clone":
                    destination = Path(arguments[-1]); destination.mkdir(parents=True); (destination / "main.c").write_text("same", encoding="utf-8")
                return subprocess.CompletedProcess(arguments, 0, "", "")
            with patch.object(client, "_run_git", side_effect=fake_git):
                result = client.upload_project(request)
            self.assertEqual(result["uploaded_files"], 0)
            self.assertFalse(any("push" in command for command in commands))

    def test_cancel_before_upload_leaves_remote_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "main.c").write_text("int main(void){}", encoding="utf-8")
            request = RepositoryRequest("https://github.com/Choi-KwangHo", "demo", local_path=str(root))
            cancelled = threading.Event(); cancelled.set()
            with self.assertRaises(GitHubUploadCancelled):
                GitHubClient("test-token", api_base="https://example.invalid").upload_project(request, cancel_event=cancelled)


if __name__ == "__main__":
    unittest.main()
