from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


GITHUB_OWNER = "Choi-KwangHo"
GITHUB_REPOSITORY = "C-Call-Hierarchy-Explorer"
LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
RELEASE_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
USER_AGENT = f"{GITHUB_REPOSITORY}-Updater"


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    title: str
    notes: str
    page_url: str
    published_at: str
    setup: ReleaseAsset


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*v?(\d+(?:\.\d+)*)\s*", value)
    if not match:
        raise UpdateError(f"지원하지 않는 버전 형식입니다: {value}")
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer_version(candidate: str, current: str) -> bool:
    left = version_tuple(candidate)
    right = version_tuple(current)
    length = max(len(left), len(right))
    return left + (0,) * (length - len(left)) > right + (0,) * (length - len(right))


def _trusted_https(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "github.com"
        or host == "api.github.com"
        or host.endswith(".githubusercontent.com")
    )


def _asset_from_json(value: dict) -> ReleaseAsset:
    name = str(value.get("name") or "")
    url = str(value.get("browser_download_url") or "")
    size = int(value.get("size") or 0)
    digest = str(value.get("digest") or "")
    if not _trusted_https(url):
        raise UpdateError(f"신뢰할 수 없는 업데이트 다운로드 주소입니다: {url}")
    if size <= 0:
        raise UpdateError(f"업데이트 파일 크기가 올바르지 않습니다: {name}")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise UpdateError(f"GitHub SHA-256 정보가 없거나 올바르지 않습니다: {name}")
    return ReleaseAsset(name=name, url=url, size=size, sha256=digest.split(":", 1)[1].lower())


def parse_release(payload: dict) -> ReleaseInfo:
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("정식 최신 릴리스가 아닙니다.")
    tag = str(payload.get("tag_name") or "")
    version = ".".join(str(part) for part in version_tuple(tag))
    expected = f"c-call-hierarchy-explorer-setup-{version}.exe".lower()
    setup_json = next(
        (item for item in payload.get("assets", []) if str(item.get("name", "")).lower() == expected),
        None,
    )
    if setup_json is None:
        raise UpdateError(f"설치 업데이트 파일을 찾지 못했습니다: {expected}")
    page_url = str(payload.get("html_url") or "")
    if not _trusted_https(page_url):
        raise UpdateError("릴리스 페이지 주소가 올바르지 않습니다.")
    return ReleaseInfo(
        version=version,
        tag=tag,
        title=str(payload.get("name") or tag),
        notes=str(payload.get("body") or "릴리스 설명이 없습니다."),
        page_url=page_url,
        published_at=str(payload.get("published_at") or ""),
        setup=_asset_from_json(setup_json),
    )


def fetch_latest_release(timeout: float = 15.0) -> ReleaseInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub 업데이트 확인 실패 (HTTP {error.code})") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"GitHub 업데이트 서버에 연결할 수 없습니다: {error}") from error
    except (ValueError, TypeError) as error:
        raise UpdateError("GitHub 릴리스 응답을 해석할 수 없습니다.") from error
    return parse_release(payload)


def update_download_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return base / "C Call Hierarchy Explorer" / "updates"


def verify_downloaded_asset(path: str | Path, expected_size: int, expected_sha256: str) -> Path:
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
    except OSError as error:
        raise UpdateError(f"다운로드한 설치 파일을 찾을 수 없습니다: {candidate}") from error
    if size != expected_size:
        raise UpdateError(f"다운로드 크기가 일치하지 않습니다: {size:,}/{expected_size:,} bytes")
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as error:
        raise UpdateError(f"다운로드한 설치 파일을 읽을 수 없습니다: {candidate}") from error
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise UpdateError("다운로드한 설치 파일의 SHA-256 검증에 실패했습니다.")
    return candidate


def download_asset(
    asset: ReleaseAsset,
    destination: str | Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
) -> Path:
    if not _trusted_https(asset.url):
        raise UpdateError("신뢰할 수 없는 업데이트 다운로드 주소입니다.")
    target = Path(destination) if destination else update_download_directory() / asset.name
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    written = 0
    request = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
                written += len(block)
                if progress:
                    progress(written, asset.size)
        if written != asset.size:
            raise UpdateError(f"다운로드 크기가 일치하지 않습니다: {written:,}/{asset.size:,} bytes")
        if digest.hexdigest().lower() != asset.sha256.lower():
            raise UpdateError("다운로드한 설치 파일의 SHA-256 검증에 실패했습니다.")
        partial.replace(target)
        return target
    except urllib.error.HTTPError as error:
        raise UpdateError(f"업데이트 다운로드 실패 (HTTP {error.code})") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"업데이트 파일을 다운로드할 수 없습니다: {error}") from error
    finally:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
