"""Conservative source-file encoding scan and UTF-8 conversion helpers."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


TEXT_SUFFIXES = {".c", ".h", ".cpp", ".hpp", ".s", ".asm", ".inc", ".ewp", ".eww", ".ewd", ".ewt", ".ioc", ".xml", ".txt", ".md", ".ini", ".cfg", ".json", ".csv"}
EXCLUDED_DIRECTORIES = {".git", ".svn", ".hg", "debug", "release", "obj", "list", "__pycache__", ".vscode", ".vs"}


@dataclass(frozen=True, slots=True)
class EncodingItem:
    path: Path
    relative_path: Path
    encoding: str
    newline: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConversionResult:
    item: EncodingItem
    action: str
    detail: str = ""


def _newline(data: bytes) -> str:
    return "CRLF" if b"\r\n" in data else "CR" if b"\r" in data else "LF"


def inspect_file(path: Path, root: Path) -> EncodingItem:
    relative = path.relative_to(root)
    try:
        data = path.read_bytes()
    except OSError as error:
        return EncodingItem(path, relative, "-", "-", "읽기 실패", str(error))
    if b"\x00" in data[:8192]:
        return EncodingItem(path, relative, "-", "-", "제외", "바이너리 또는 UTF-16 파일")
    newline = _newline(data)
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            data.decode("utf-8-sig")
            return EncodingItem(path, relative, "UTF-8 BOM", newline, "변환 가능", "UTF-8 무 BOM으로 정리")
        except UnicodeDecodeError:
            return EncodingItem(path, relative, "-", newline, "검토 필요", "UTF-8 BOM 뒤의 텍스트가 손상됨")
    try:
        data.decode("utf-8")
        return EncodingItem(path, relative, "UTF-8", newline, "유지", "이미 UTF-8")
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp949")
    except UnicodeDecodeError:
        return EncodingItem(path, relative, "알 수 없음", newline, "검토 필요", "UTF-8/CP949로 안전하게 판별할 수 없음")
    # CP949 is a superset of EUC-KR.  Require Korean text for non-UTF-8 data
    # so arbitrary binary or another legacy locale is never silently converted.
    if not any("가" <= char <= "힣" for char in text):
        return EncodingItem(path, relative, "알 수 없음", newline, "검토 필요", "한글 근거가 없어 CP949 변환을 보류")
    return EncodingItem(path, relative, "CP949/EUC-KR", newline, "변환 가능", "UTF-8 무 BOM으로 변환")


def scan_folder(folder: str | Path) -> list[EncodingItem]:
    root = Path(folder).resolve()
    if not root.is_dir():
        raise ValueError("변환할 폴더를 찾을 수 없습니다.")
    items = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part.casefold() in EXCLUDED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        items.append(inspect_file(path, root))
    return sorted(items, key=lambda item: str(item.relative_path).casefold())


def convert_items(items: list[EncodingItem], mode: str, output_root: str | Path = "") -> list[ConversionResult]:
    """Convert only positively identified files; preserve originals on every failure.

    Modes are ``backup`` (in-place plus .bak) and ``folder`` (write under
    output_root). UTF-8 files are deliberately left untouched.
    """
    if mode not in {"backup", "folder"}:
        raise ValueError("지원하지 않는 변환 방식입니다.")
    destination_root = Path(output_root).resolve() if mode == "folder" else None
    if mode == "folder" and not destination_root:
        raise ValueError("별도 출력 폴더를 지정하십시오.")
    results: list[ConversionResult] = []
    for item in items:
        if item.status != "변환 가능":
            results.append(ConversionResult(item, "건너뜀", item.detail or item.status))
            continue
        try:
            raw = item.path.read_bytes()
            text = raw.decode("utf-8-sig" if item.encoding == "UTF-8 BOM" else "cp949")
            encoded = text.encode("utf-8")  # UTF-8 without BOM; decoded newlines are intact.
            if mode == "folder":
                target = destination_root / item.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(encoded)
                results.append(ConversionResult(item, "별도 폴더 변환", str(target)))
            else:
                backup = item.path.with_name(item.path.name + ".bak")
                if not backup.exists():
                    shutil.copy2(item.path, backup)
                temporary = item.path.with_name(item.path.name + ".embedforge.tmp")
                temporary.write_bytes(encoded)
                temporary.replace(item.path)
                results.append(ConversionResult(item, "변환 및 .bak 백업", str(backup)))
        except (OSError, UnicodeError) as error:
            results.append(ConversionResult(item, "실패", str(error)))
    return results


def summary(items: list[EncodingItem] | list[ConversionResult]) -> str:
    rows = items
    converted = sum(1 for item in rows if getattr(item, "status", "") == "변환 가능" or getattr(item, "action", "") not in {"건너뜀", "실패"})
    review = sum(1 for item in rows if getattr(item, "status", "") == "검토 필요" or getattr(item, "action", "") == "실패")
    return f"대상 {len(rows):,}개 · 변환 대상/완료 {converted:,}개 · 검토/실패 {review:,}개"
