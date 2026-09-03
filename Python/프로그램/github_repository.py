"""Safe GitHub repository creation and first-project upload helpers.

The module is deliberately UI-independent.  It supports the three creation
modes used by the application: no local source, a local folder without an IAR
project, and a local folder containing an IAR project.  Network mutations are
only performed when the caller explicitly invokes the methods.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GitHubRepositoryError(RuntimeError):
    pass


class GitHubUploadCancelled(GitHubRepositoryError):
    """Raised only after the active Git process has been stopped safely."""
    pass


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    name: str
    full_name: str
    html_url: str
    is_empty: bool
    private: bool


@dataclass(slots=True)
class RepositoryRequest:
    dashboard_url: str
    name: str
    description: str = ""
    private: bool = True
    include_readme: bool = True
    gitignore_template: str = ""
    license_template: str = ""
    local_path: str = ""

    @property
    def owner(self) -> str:
        match = re.fullmatch(r"https?://github\.com/(?:orgs/|users/)?([^/]+)/?", self.dashboard_url.strip(), re.I)
        if not match:
            raise GitHubRepositoryError("GitHub Dashboard 주소는 https://github.com/orgs/OWNER 형식이어야 합니다.")
        return match.group(1)

    @property
    def owner_endpoint(self) -> str:
        """Return the repository-creation endpoint for this dashboard.

        GitHub only permits repository creation for a personal account through
        ``POST /user/repos``.  ``/users/{login}/repos`` is a read-only listing
        endpoint and previously caused the misleading HTTP 404 shown to users.
        """
        return "/orgs/" if "/orgs/" in self.dashboard_url.casefold() else "/user"

    @property
    def is_organization(self) -> bool:
        return self.owner_endpoint == "/orgs/"

    def validate(self) -> None:
        self.owner
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.name.strip()):
            raise GitHubRepositoryError("Repository 이름은 GitHub에서 허용하는 형식이어야 합니다.")
        if len(self.description) > 350:
            raise GitHubRepositoryError("Description은 350자 이내여야 합니다.")
        if self.local_path and not Path(self.local_path).is_dir():
            raise GitHubRepositoryError("지정한 로컬 폴더를 찾을 수 없습니다.")

    @property
    def mode(self) -> str:
        if not self.local_path:
            return "repository_only"
        if detect_iar_projects(self.local_path):
            return "iar_project_present"
        return "folder_without_iar"


@dataclass(slots=True)
class IarProjectInfo:
    root: Path
    files: list[Path] = field(default_factory=list)


IAR_PROJECT_SUFFIXES = {".eww", ".ewp", ".ewd", ".ewt", ".ioc"}
EXCLUDED_DIRECTORIES = {".git", ".vs", ".vscode", "debug", "release", "settings", ".iar", "__pycache__"}
EXCLUDED_SUFFIXES = {".bin", ".hex", ".map", ".o", ".obj", ".dep", ".pbd", ".browse", ".bak", ".zip"}
SENSITIVE_NAMES = {"id_rsa", "id_ed25519", ".env", "credentials", "secrets", "token", "private_key"}


def detect_iar_projects(local_path: str | Path) -> list[IarProjectInfo]:
    root = Path(local_path).resolve()
    grouped: dict[Path, list[Path]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IAR_PROJECT_SUFFIXES:
            continue
        if any(part.casefold() in EXCLUDED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        grouped.setdefault(path.parent, []).append(path)
    return [IarProjectInfo(parent, sorted(files)) for parent, files in sorted(grouped.items())]


def iter_upload_files(local_path: str | Path) -> list[Path]:
    root = Path(local_path).resolve()
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.casefold() in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.suffix.casefold() in EXCLUDED_SUFFIXES:
            continue
        if path.name.casefold() in SENSITIVE_NAMES or any(token in path.name.casefold() for token in ("password", "secret", "credential", "private")):
            continue
        result.append(path)
    return sorted(result)


class GitHubClient:
    """Small GitHub REST client for repository creation and contents upload."""

    def __init__(self, token: str, api_base: str = "https://api.github.com") -> None:
        if not token.strip():
            raise GitHubRepositoryError("GitHub 인증 토큰이 필요합니다.")
        self.token = token.strip()
        self.api_base = api_base.rstrip("/")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.api_base + path,
            data=data,
            method=method,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            detail = ""
            try:
                payload = json.loads(error.read().decode("utf-8"))
                detail = str(payload.get("message", "")).strip()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            message = f"GitHub 요청 실패: HTTP {error.code}"
            if detail:
                message += f" · {detail}"
            raise GitHubRepositoryError(message) from error
        except (URLError, json.JSONDecodeError) as error:
            raise GitHubRepositoryError(f"GitHub 요청 실패: {error}") from error

    def create_repository(self, request: RepositoryRequest) -> dict:
        request.validate()
        payload = {"name": request.name.strip(), "description": request.description.strip(), "private": request.private, "auto_init": request.include_readme}
        if request.gitignore_template:
            payload["gitignore_template"] = request.gitignore_template
        if request.license_template:
            payload["license_template"] = request.license_template
        path = f"/orgs/{request.owner}/repos" if request.is_organization else "/user/repos"
        try:
            return self._request("POST", path, payload)
        except GitHubRepositoryError as error:
            if "HTTP 404" in str(error):
                target = f"조직 '{request.owner}'" if request.is_organization else "개인 계정"
                raise GitHubRepositoryError(
                    f"{error}\n\n"
                    f"대상: {target}\n"
                    "확인: Dashboard 주소 유형, 토큰의 Administration: Write 권한, "
                    "조직의 Fine-grained 토큰 승인 상태를 확인하십시오. "
                    "Classic 토큰은 필수가 아닙니다."
                ) from error
            raise

    def list_repositories(self, request: RepositoryRequest) -> list[RepositoryInfo]:
        request.owner
        path = f"/orgs/{request.owner}/repos?per_page=100&sort=updated" if request.is_organization else "/user/repos?affiliation=owner&per_page=100&sort=updated"
        data = self._request("GET", path)
        if not isinstance(data, list):
            return []
        return [RepositoryInfo(
            name=str(value.get("name", "")), full_name=str(value.get("full_name", "")),
            html_url=str(value.get("html_url", "")), is_empty=int(value.get("size", 0) or 0) == 0,
            private=bool(value.get("private", False)),
        ) for value in data]

    def upload_file(self, owner: str, repository: str, relative_path: str, source: Path, message: str = "Initial IAR project upload") -> dict:
        encoded = base64.b64encode(source.read_bytes()).decode("ascii")
        return self._request("PUT", f"/repos/{owner}/{repository}/contents/{relative_path.replace(chr(92), '/')}", {"message": message, "content": encoded})

    def upload_project(self, request: RepositoryRequest, message: str = "Initial IAR project upload", progress=None,
                       cancel_event: threading.Event | None = None) -> dict:
        """Create one Git commit and push it, rather than one API call per file.

        The previous Contents API implementation performed an HTTP request per
        source file and made large IAR uploads painfully slow.  Git packs the
        complete project locally and transfers a single initial commit.
        """
        request.validate()
        if not request.local_path:
            raise GitHubRepositoryError("코드 업로드에는 로컬 폴더가 필요합니다.")
        files = iter_upload_files(request.local_path)
        root = Path(request.local_path).resolve()
        self._ensure_not_cancelled(cancel_event)
        if progress:
            progress(-1, 6, f"[STAGE:1] 업로드 대상 파일 검사 완료 · {len(files):,}개")
        if not files:
            raise GitHubRepositoryError("업로드 제외 규칙을 통과한 파일이 없습니다.")
        with tempfile.TemporaryDirectory(prefix="EmbedForge-git-upload-") as temporary:
            staging = Path(temporary) / "repository"
            askpass = Path(temporary) / "git-askpass.cmd"
            askpass.write_text(
                "@echo off\r\n"
                "echo %~1 | findstr /i \"username\" >nul\r\n"
                "if not errorlevel 1 (echo x-access-token) else (echo %EMBEDFORGE_GITHUB_TOKEN%)\r\n",
                encoding="ascii",
            )
            environment = os.environ.copy()
            environment.update({
                "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass),
                "EMBEDFORGE_GITHUB_TOKEN": self.token,
            })
            remote = f"https://github.com/{request.owner}/{request.name}.git"
            self._run_git(["clone", "--quiet", remote, str(staging)], temporary, environment, progress, 2, "Repository clone", cancel_event)
            self._ensure_not_cancelled(cancel_event)
            if progress:
                progress(-3, 6, "[STAGE:3] 제외 규칙 통과 파일을 임시 Git 작업 폴더에 추가")
            copied = 0
            unchanged = 0
            for index, source in enumerate(files, start=1):
                self._ensure_not_cancelled(cancel_event)
                target = staging / source.relative_to(root)
                if target.exists() and target.is_file() and self._same_file(source, target):
                    unchanged += 1
                    if progress:
                        progress(index, len(files), f"동일 파일 확인 · {source.relative_to(root)}")
                    continue
                # A pre-existing file with a different digest may be a user's
                # README/configuration or a newer remote change.  Never turn a
                # recovery operation into an implicit overwrite.
                if target.exists():
                    raise GitHubRepositoryError(
                        "복구 충돌: 원격 저장소의 파일이 로컬 파일과 다릅니다. "
                        f"자동 덮어쓰기를 중단했습니다: {source.relative_to(root)}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied += 1
                if progress:
                    progress(index, len(files), f"파일 준비 {index}/{len(files)} · {source.relative_to(root)}")
            self._ensure_not_cancelled(cancel_event)
            login = self._request("GET", "/user").get("login", request.owner)
            self._run_git(["-C", str(staging), "config", "user.name", "EmbedForge"], temporary, environment, None, 3, "Git 작성자 설정", cancel_event)
            self._run_git(["-C", str(staging), "config", "user.email", f"{login}@users.noreply.github.com"], temporary, environment, None, 3, "Git 작성자 설정", cancel_event)
            self._run_git(["-C", str(staging), "add", "--all"], temporary, environment, None, 3, "파일 스테이징", cancel_event)
            changed = self._run_git(["-C", str(staging), "diff", "--cached", "--quiet"], temporary, environment, None, 3, "변경 파일 확인", cancel_event, allow_exit_codes={0, 1})
            if changed.returncode == 0:
                if progress:
                    progress(-5, 6, "[STAGE:5] 원격 파일이 모두 동일합니다 · Push 없이 복구 확인 완료")
                return {"uploaded_files": 0, "unchanged_files": unchanged, "message": "동일 파일 확인 완료 · 추가 Push가 필요하지 않습니다."}
            self._run_git(["-C", str(staging), "commit", "--quiet", "-m", message], temporary, environment, None, 3, "복구 커밋 생성", cancel_event)
            self._run_git(["-C", str(staging), "push", "--quiet", "origin", "HEAD"], temporary, environment, progress, 4, "Git push", cancel_event)
        if progress:
            progress(-5, 6, "[STAGE:5] 최초 커밋 Push 및 원격 검증 완료")
        return {"uploaded_files": copied, "unchanged_files": unchanged, "message": message}

    @staticmethod
    def _ensure_not_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            raise GitHubUploadCancelled("사용자 요청으로 중단되었습니다. 원격 저장소는 변경하지 않았습니다.")

    @staticmethod
    def _same_file(first: Path, second: Path) -> bool:
        def digest(path: Path) -> bytes:
            value = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            return value.digest()
        return first.stat().st_size == second.stat().st_size and digest(first) == digest(second)

    @staticmethod
    def _run_git(arguments, working_directory, environment, progress, stage, label, cancel_event=None, allow_exit_codes={0}) -> subprocess.CompletedProcess:
        if progress:
            progress(-stage, 6, f"[STAGE:{stage}] {label}")
        try:
            process = subprocess.Popen(
                ["git", *arguments], cwd=working_directory, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as error:
            raise GitHubRepositoryError("Git 실행 프로그램을 찾을 수 없습니다. Git for Windows를 설치하십시오.") from error
        try:
            for _ in range(600):
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
                    raise GitHubUploadCancelled("중단 요청을 처리했습니다. 다음 업로드 시 동일 파일을 자동 확인하여 복구합니다.")
                if process.poll() is not None:
                    break
                threading.Event().wait(0.5)
            else:
                process.kill(); process.wait(timeout=10)
                raise GitHubRepositoryError(f"{label} 시간이 초과되었습니다 (5분). 네트워크 연결을 확인하십시오.")
            stdout, stderr = process.communicate()
            completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        finally:
            if process.poll() is None:
                process.kill()
        if completed.returncode not in allow_exit_codes:
            detail = (completed.stderr or completed.stdout or "알 수 없는 Git 오류").strip().splitlines()[-1]
            raise GitHubRepositoryError(f"{label} 실패: {detail}")
        return completed
