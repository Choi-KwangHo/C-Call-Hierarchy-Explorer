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

    def upload_project(self, request: RepositoryRequest, message: str = "Initial IAR project upload", progress=None) -> list[dict]:
        request.validate()
        if not request.local_path:
            raise GitHubRepositoryError("코드 업로드에는 로컬 폴더가 필요합니다.")
        files = iter_upload_files(request.local_path)
        results = []
        root = Path(request.local_path).resolve()
        if progress:
            progress(0, len(files), "업로드 대상 파일 검사 완료")
        for index, path in enumerate(files, 1):
            if progress:
                progress(index, len(files), str(path.relative_to(root)))
            results.append(self.upload_file(request.owner, request.name, str(path.relative_to(root)), path, message))
        return results
