from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
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
RELEASE_FEED = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases.atom"
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


def parse_release_feed(payload: bytes) -> tuple[str, str, str, str]:
    """Return the highest stable version published in GitHub's release feed."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise UpdateError("GitHub 릴리스 태그 목록을 해석하지 못했습니다.") from error
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    releases: list[tuple[tuple[int, ...], str, str, str, str, str]] = []
    for entry in root.findall("atom:entry", namespace):
        link = entry.find("atom:link", namespace)
        href = str(link.get("href") if link is not None else "")
        match = re.search(r"/releases/tag/(?P<tag>v\d+\.\d+\.\d+)$", href)
        if not match:
            continue
        tag = match.group("tag")
        version = ".".join(str(part) for part in version_tuple(tag))
        title = entry.findtext("atom:title", tag, namespace)
        published = entry.findtext("atom:updated", "", namespace)
        raw_notes = entry.findtext("atom:content", "", namespace)
        notes = html.unescape(re.sub(r"<[^>]+>", "", raw_notes)).strip()
        releases.append((version_tuple(version), version, tag, title, published, notes))
    if not releases:
        raise UpdateError("GitHub 릴리스 피드에서 정식 버전 태그를 찾지 못했습니다.")
    _, version, tag, title, published, notes = max(releases, key=lambda item: item[0])
    return version, tag, title, published + "\n" + notes


def parse_checksum_manifest(payload: str, expected_name: str) -> str:
    for line in payload.splitlines():
        match = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*", line)
        if match and match.group(2).casefold() == expected_name.casefold():
            return match.group(1).lower()
    raise UpdateError(f"SHA256SUMS.txt에서 설치 파일 검증값을 찾지 못했습니다: {expected_name}")


def fetch_latest_release_from_feed(timeout: float = 15.0) -> ReleaseInfo:
    try:
        feed_request = urllib.request.Request(RELEASE_FEED, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(feed_request, timeout=timeout) as response:
            version, tag, title, metadata = parse_release_feed(response.read())
        published_at, _, notes = metadata.partition("\n")

        setup_name = f"C-Call-Hierarchy-Explorer-Setup-{version}.exe"
        asset_base = (
            f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/download/{tag}"
        )
        checksum_request = urllib.request.Request(
            f"{asset_base}/SHA256SUMS.txt",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(checksum_request, timeout=timeout) as response:
            digest = parse_checksum_manifest(response.read().decode("utf-8-sig"), setup_name)

        setup_url = f"{asset_base}/{setup_name}"
        setup_request = urllib.request.Request(
            setup_url,
            headers={"User-Agent": USER_AGENT},
            method="HEAD",
        )
        with urllib.request.urlopen(setup_request, timeout=timeout) as response:
            final_url = response.geturl()
            size = int(response.headers.get("Content-Length") or 0)
        if not _trusted_https(final_url) or size <= 0:
            raise UpdateError(f"{tag} 설치 빌드 파일을 확인하지 못했습니다: {setup_name}")
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub 최신 태그 또는 빌드 확인 실패 (HTTP {error.code})") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"GitHub 최신 태그 또는 빌드 서버에 연결하지 못했습니다: {error}") from error
    except (ValueError, TypeError, UnicodeError) as error:
        raise UpdateError("GitHub 최신 태그 또는 빌드 정보를 해석하지 못했습니다.") from error
    return ReleaseInfo(
        version=version,
        tag=tag,
        title=title or tag,
        notes=notes or "릴리스 설명이 없습니다.",
        page_url=f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/tag/{tag}",
        published_at=published_at,
        setup=ReleaseAsset(setup_name, setup_url, size, digest),
    )


def fetch_latest_release(timeout: float = 15.0) -> ReleaseInfo:
    """Check the latest tag and its exact build without consuming GitHub API quota."""
    return fetch_latest_release_from_feed(timeout)


def fetch_latest_release_from_api(timeout: float = 15.0) -> ReleaseInfo:
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
