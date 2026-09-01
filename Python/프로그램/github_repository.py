"""Safe GitHub repository creation and first-project upload helpers.

The module is deliberately UI-independent.  It supports the three creation
modes used by the application: no local source, a local folder without an IAR
project, and a local folder containing an IAR project.  Network mutations are
only performed when the caller explicitly invokes the methods.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GitHubRepositoryError(RuntimeError):
    pass


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
        return "/orgs/" if "/orgs/" in self.dashboard_url.casefold() else "/users/"

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
        except (HTTPError, URLError, json.JSONDecodeError) as error:
            raise GitHubRepositoryError(f"GitHub 요청 실패: {error}") from error

    def create_repository(self, request: RepositoryRequest) -> dict:
        request.validate()
        payload = {"name": request.name.strip(), "description": request.description.strip(), "private": request.private, "auto_init": request.include_readme}
        if request.gitignore_template:
            payload["gitignore_template"] = request.gitignore_template
        if request.license_template:
            payload["license_template"] = request.license_template
        return self._request("POST", f"{request.owner_endpoint}{request.owner}/repos", payload)

    def upload_file(self, owner: str, repository: str, relative_path: str, source: Path, message: str = "Initial IAR project upload") -> dict:
        encoded = base64.b64encode(source.read_bytes()).decode("ascii")
        return self._request("PUT", f"/repos/{owner}/{repository}/contents/{relative_path.replace(chr(92), '/')}", {"message": message, "content": encoded})

    def upload_project(self, request: RepositoryRequest, message: str = "Initial IAR project upload") -> list[dict]:
        request.validate()
        if not request.local_path:
            raise GitHubRepositoryError("코드 업로드에는 로컬 폴더가 필요합니다.")
        return [self.upload_file(request.owner, request.name, str(path.relative_to(Path(request.local_path).resolve())), path, message) for path in iter_upload_files(request.local_path)]
