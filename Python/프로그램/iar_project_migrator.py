from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


# Stand-alone use defaults.  The integrated UI supplies these values directly.
SOURCE_ROOT = r""
TARGET_ROOT = r""
OLD_PROJECT_KEYWORD = ""
NEW_PROJECT_KEYWORD = ""
OLD_EMBEDDED_PATH = ""
NEW_EMBEDDED_PATH = ""

IAR_EXTENSIONS = {".eww", ".ewp", ".ewd", ".ewt", ".ewf", ".ewg"}
SOURCE_TEXT_EXTENSIONS = {
    ".c", ".h", ".cpp", ".cxx", ".cc", ".hpp", ".hxx", ".s", ".asm",
    ".inc", ".icf", ".xcl", ".txt", ".xml", ".json", ".yml", ".yaml",
}
SKIP_DIRECTORY_NAMES = {"debug", "release", ".iar", "settings"}
SKIP_FILE_EXTENSIONS = {".dep", ".pbd"}
ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "windows-1252")
MAX_RETAINED_EVENTS = 20_000


class MigrationCancelled(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


@dataclass(slots=True)
class MigrationOptions:
    source_root: str
    target_root: str
    old_keyword: str
    new_keyword: str
    old_embedded_path: str = ""
    new_embedded_path: str = ""
    replace_source_text: bool = True
    rename_directories: bool = True


@dataclass(slots=True)
class MigrationEvent:
    action: str
    source: str = ""
    target: str = ""
    detail: str = ""


@dataclass(slots=True)
class MigrationResult:
    source_root: str
    target_root: str
    copied_files: int = 0
    skipped_directories: int = 0
    skipped_files: int = 0
    renamed_files: int = 0
    modified_files: int = 0
    replacement_count: int = 0
    project_names_updated: int = 0
    events: list[MigrationEvent] = field(default_factory=list)
    omitted_events: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IarWorkspaceInfo:
    workspace_file: str
    source_root: str
    first_folder: str
    second_folder: str
    project_keyword: str
    referenced_projects: list[str] = field(default_factory=list)
    encoding: str = "utf-8"


def suggested_embedded_path(root: str | Path, depth: int = 2) -> str:
    path = Path(root)
    parts = [part for part in path.parts if part not in {path.anchor, "\\", "/"}]
    return "\\".join(parts[-max(1, depth):])


def inspect_iar_workspace(workspace_file: str | Path) -> IarWorkspaceInfo:
    workspace = Path(workspace_file).expanduser().resolve(strict=True)
    if not workspace.is_file() or workspace.suffix.casefold() != ".eww":
        raise MigrationError("IAR 워크스페이스(.eww) 파일을 선택하십시오.")
    try:
        text, encoding, _ = _decode_text(workspace.read_bytes())
    except (OSError, UnicodeDecodeError) as error:
        raise MigrationError(f"워크스페이스 파일을 읽을 수 없습니다: {error}") from error
    references: list[str] = []
    for value in re.findall(r"[^\"'<>\r\n]+\.ewp", text, re.IGNORECASE):
        cleaned = value.strip().replace("/", "\\")
        if cleaned not in references:
            references.append(cleaned)
    # Conventional EWARM layouts keep .eww under <project-root>/EWARM.  When
    # another folder name is used, use the workspace directory itself so the
    # user can correct the inferred root without any file-system mutation.
    source_root = workspace.parent.parent if workspace.parent.name.casefold() in {"ewarm", "iar"} else workspace.parent
    first = source_root.parent.name
    second = source_root.name
    stems = [workspace.stem]
    stems.extend(Path(reference.replace("\\", "/")).stem for reference in references)
    keyword = workspace.stem
    if len(stems) > 1:
        common = os.path.commonprefix(stems).rstrip("_- .")
        if common:
            keyword = common
    return IarWorkspaceInfo(
        workspace_file=str(workspace), source_root=str(source_root),
        first_folder=first, second_folder=second, project_keyword=keyword,
        referenced_projects=references, encoding=encoding,
    )


def _resolved(path: str, label: str, must_exist: bool = False) -> Path:
    if not str(path).strip():
        raise MigrationError(f"{label}을(를) 입력하십시오.")
    result = Path(path).expanduser().resolve(strict=False)
    if must_exist and not result.is_dir():
        raise MigrationError(f"{label}이 존재하는 폴더가 아닙니다: {result}")
    return result


def validate_options(options: MigrationOptions) -> tuple[Path, Path]:
    source = _resolved(options.source_root, "기존 프로젝트 폴더", True)
    target = _resolved(options.target_root, "새 프로젝트 폴더")
    if not options.old_keyword:
        raise MigrationError("기존 프로젝트 핵심 이름을 입력하십시오.")
    if not options.new_keyword:
        raise MigrationError("새 프로젝트 핵심 이름을 입력하십시오.")
    if options.old_keyword == options.new_keyword:
        raise MigrationError("기존 이름과 새 이름이 같습니다.")
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise MigrationError("새 프로젝트 폴더는 기존 프로젝트 폴더 내부일 수 없습니다.")
    try:
        source.relative_to(target)
    except ValueError:
        pass
    else:
        raise MigrationError("기존 프로젝트 폴더는 새 프로젝트 폴더 내부일 수 없습니다.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise MigrationError(
            "새 프로젝트 폴더가 비어 있지 않습니다. 기존 자료 보호를 위해 덮어쓰지 않습니다: "
            f"{target}"
        )
    if target.is_symlink():
        raise MigrationError(f"새 프로젝트 폴더는 심볼릭 링크일 수 없습니다: {target}")
    return source, target


def _cancelled(callback: Callable[[], bool] | None) -> None:
    if callback and callback():
        raise MigrationCancelled("IAR 프로젝트 복제 작업이 취소되었습니다.")


def _log(
    result: MigrationResult,
    event: MigrationEvent,
    progress: Callable[[MigrationEvent], None] | None,
) -> None:
    if len(result.events) < MAX_RETAINED_EVENTS:
        result.events.append(event)
    else:
        result.omitted_events += 1
    if progress:
        progress(event)


def _iter_source_files(
    source: Path,
    result: MigrationResult,
    progress: Callable[[MigrationEvent], None] | None,
    cancelled: Callable[[], bool] | None,
    included_directories: list[Path] | None = None,
) -> Iterable[tuple[Path, Path]]:
    for current, directories, filenames in os.walk(source, topdown=True, followlinks=False):
        _cancelled(cancelled)
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(directories, key=str.casefold):
            path = current_path / name
            if name.casefold() in SKIP_DIRECTORY_NAMES or path.is_symlink():
                result.skipped_directories += 1
                detail = "제외 폴더" if not path.is_symlink() else "심볼릭 링크 폴더 제외"
                _log(result, MigrationEvent("skip_dir", str(path), detail=detail), progress)
            else:
                kept.append(name)
        directories[:] = kept
        if included_directories is not None:
            included_directories.extend((current_path / name).relative_to(source) for name in kept)
        for name in sorted(filenames, key=str.casefold):
            _cancelled(cancelled)
            path = current_path / name
            if path.suffix.casefold() in SKIP_FILE_EXTENSIONS or path.is_symlink():
                result.skipped_files += 1
                detail = "제외 확장자" if not path.is_symlink() else "심볼릭 링크 파일 제외"
                _log(result, MigrationEvent("skip_file", str(path), detail=detail), progress)
                continue
            yield path, path.relative_to(source)


def _decode_text(data: bytes) -> tuple[str, str, bool]:
    if b"\x00" in data:
        raise UnicodeDecodeError("text", data, 0, min(1, len(data)), "NUL 문자가 포함된 바이너리 파일")
    has_bom = data.startswith(b"\xef\xbb\xbf")
    encodings = ENCODINGS if has_bom else ("utf-8", "cp949", "windows-1252")
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding, has_bom
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("supported", data, 0, len(data), "지원 인코딩으로 해석할 수 없음")


def _encode_text(text: str, encoding: str, had_bom: bool) -> bytes:
    encoded = text.encode(encoding)
    if had_bom and encoding == "utf-8" and not encoded.startswith(b"\xef\xbb\xbf"):
        encoded = b"\xef\xbb\xbf" + encoded
    return encoded


def _path_variants(value: str) -> tuple[str, ...]:
    normalized = str(value or "").strip().strip("\\/")
    if not normalized:
        return ()
    parts = [part for part in re.split(r"[\\/]+", normalized) if part]
    return ("\\".join(parts), "/".join(parts))


def _replace_paths(text: str, old_path: str, new_path: str) -> tuple[str, int]:
    old_variants = _path_variants(old_path)
    new_variants = _path_variants(new_path)
    if not old_variants or not new_variants:
        return text, 0
    components = [re.escape(part) for part in re.split(r"[\\/]+", old_variants[0]) if part]
    pattern = re.compile(r"[\\/]".join(components), re.IGNORECASE)
    count = 0

    def replacement(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        separator = "/" if "/" in match.group(0) and "\\" not in match.group(0) else "\\"
        return new_variants[1] if separator == "/" else new_variants[0]

    return pattern.sub(replacement, text), count


_PROJECT_BLOCK = re.compile(r"(<project\b[^>]*>)(.*?)(</project\s*>)", re.IGNORECASE | re.DOTALL)
_NAME_TAG = re.compile(r"(<name\b[^>]*>)(.*?)(</name\s*>)", re.IGNORECASE | re.DOTALL)


def synchronize_ewp_project_name(text: str, project_name: str) -> tuple[str, int]:
    """Synchronize project/name nodes without reformatting the IAR XML file."""
    replacements = 0

    def block_replacement(block_match: re.Match[str]) -> str:
        nonlocal replacements
        opening, body, closing = block_match.groups()
        name_match = _NAME_TAG.search(body)
        if not name_match:
            return block_match.group(0)
        # Avoid configuration/name nodes: only accept a name before another nested tag closes.
        prefix = body[:name_match.start()]
        if re.search(r"<(configuration|group|file)\b", prefix, re.IGNORECASE):
            return block_match.group(0)
        current = name_match.group(2)
        if current == project_name:
            return block_match.group(0)
        updated_body = body[:name_match.start()] + name_match.group(1) + project_name + name_match.group(3) + body[name_match.end():]
        replacements += 1
        return opening + updated_body + closing

    updated = _PROJECT_BLOCK.sub(block_replacement, text)
    return updated, replacements


def _text_extensions(options: MigrationOptions) -> set[str]:
    return IAR_EXTENSIONS | (SOURCE_TEXT_EXTENSIONS if options.replace_source_text else set())


def _modify_text_file(
    path: Path,
    options: MigrationOptions,
    result: MigrationResult,
    progress: Callable[[MigrationEvent], None] | None,
) -> None:
    if path.suffix.casefold() not in _text_extensions(options):
        return
    raw = path.read_bytes()
    try:
        text, encoding, had_bom = _decode_text(raw)
    except UnicodeDecodeError:
        warning = f"텍스트 인코딩을 판별하지 못해 내부 치환을 건너뜀: {path}"
        result.warnings.append(warning)
        _log(result, MigrationEvent("warning", str(path), detail=warning), progress)
        return
    original = text
    text, path_count = _replace_paths(text, options.old_embedded_path, options.new_embedded_path)
    text, absolute_path_count = _replace_paths(text, options.source_root, options.target_root)
    path_count += absolute_path_count
    keyword_count = text.count(options.old_keyword)
    if keyword_count:
        text = text.replace(options.old_keyword, options.new_keyword)
    project_count = 0
    if path.suffix.casefold() == ".ewp":
        text, project_count = synchronize_ewp_project_name(text, path.stem)
    if text == original:
        return
    path.write_bytes(_encode_text(text, encoding, had_bom))
    result.modified_files += 1
    result.replacement_count += keyword_count + path_count
    result.project_names_updated += project_count
    _log(
        result,
        MigrationEvent(
            "modify", str(path), str(path),
            f"문자열 {keyword_count + path_count}건 · 프로젝트 표시 이름 {project_count}건 · {encoding}",
        ),
        progress,
    )


def _renamed_relative(relative: Path, options: MigrationOptions) -> Path:
    parts = list(relative.parts)
    if options.rename_directories:
        parts[:-1] = [part.replace(options.old_keyword, options.new_keyword) for part in parts[:-1]]
    if relative.suffix.casefold() in IAR_EXTENSIONS:
        parts[-1] = parts[-1].replace(options.old_keyword, options.new_keyword)
    return Path(*parts)


def _renamed_directory_relative(relative: Path, options: MigrationOptions) -> Path:
    if not options.rename_directories:
        return relative
    return Path(*(part.replace(options.old_keyword, options.new_keyword) for part in relative.parts))


def preview_iar_migration(
    options: MigrationOptions,
    progress: Callable[[MigrationEvent], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> MigrationResult:
    source, target = validate_options(options)
    result = MigrationResult(str(source), str(target))
    destinations: set[str] = set()
    _log(result, MigrationEvent("preview", str(source), str(target), "사전 검사 시작"), progress)
    for source_file, relative in _iter_source_files(source, result, progress, cancelled):
        _cancelled(cancelled)
        renamed_relative = _renamed_relative(relative, options)
        destination = target / renamed_relative
        collision_key = str(destination).casefold()
        if collision_key in destinations:
            raise MigrationError(f"이름 변경 후 파일 경로가 충돌합니다: {renamed_relative}")
        destinations.add(collision_key)
        result.copied_files += 1
        renamed = renamed_relative != relative
        if renamed:
            result.renamed_files += 1
        action = "rename_copy" if renamed else "copy"
        _log(result, MigrationEvent(action, str(source_file), str(destination)), progress)
        if source_file.suffix.casefold() not in _text_extensions(options):
            continue
        try:
            text, _, _ = _decode_text(source_file.read_bytes())
        except (OSError, UnicodeDecodeError):
            continue
        original = text
        text, path_count = _replace_paths(text, options.old_embedded_path, options.new_embedded_path)
        text, absolute_path_count = _replace_paths(text, options.source_root, options.target_root)
        path_count += absolute_path_count
        keyword_count = text.count(options.old_keyword)
        if keyword_count:
            text = text.replace(options.old_keyword, options.new_keyword)
        project_count = 0
        if source_file.suffix.casefold() == ".ewp":
            text, project_count = synchronize_ewp_project_name(text, destination.stem)
        if text != original:
            result.modified_files += 1
            result.replacement_count += keyword_count + path_count
            result.project_names_updated += project_count
            _log(
                result,
                MigrationEvent(
                    "would_modify", str(source_file), str(destination),
                    f"문자열 {keyword_count + path_count}건 · 프로젝트 표시 이름 {project_count}건",
                ),
                progress,
            )
    _log(result, MigrationEvent("preview_complete", str(source), str(target), "사전 검사 완료"), progress)
    return result


def migrate_iar_project(
    options: MigrationOptions,
    progress: Callable[[MigrationEvent], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> MigrationResult:
    """Create a transactional clone of an IAR EWARM project.

    The source tree is never written.  A staging directory is populated and modified,
    then atomically renamed to the requested target only after every step succeeds.
    """
    source, target = validate_options(options)
    result = MigrationResult(str(source), str(target))
    staging_parent = target.parent
    while not staging_parent.exists() and staging_parent != staging_parent.parent:
        staging_parent = staging_parent.parent
    if not staging_parent.is_dir():
        raise MigrationError(f"새 프로젝트를 생성할 상위 폴더를 찾을 수 없습니다: {target.parent}")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.iar-migrate-", dir=staging_parent))
    target_was_empty = target.exists()
    created_parent = False
    try:
        _log(result, MigrationEvent("start", str(source), str(target), "안전 복제 시작"), progress)
        destinations: set[str] = set()
        copied: list[Path] = []
        included_directories: list[Path] = []
        for source_file, relative in _iter_source_files(
            source, result, progress, cancelled, included_directories
        ):
            _cancelled(cancelled)
            renamed_relative = _renamed_relative(relative, options)
            destination = staging / renamed_relative
            collision_key = str(destination).casefold()
            if collision_key in destinations or destination.exists():
                raise MigrationError(f"이름 변경 후 파일 경로가 충돌합니다: {renamed_relative}")
            destinations.add(collision_key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            before = source_file.stat()
            shutil.copy2(source_file, destination)
            after = source_file.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise MigrationError(f"복사 중 원본 파일이 변경되었습니다. 다시 시도하십시오: {source_file}")
            try:
                destination.chmod(destination.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
            result.copied_files += 1
            copied.append(destination)
            action = "rename_copy" if renamed_relative != relative else "copy"
            if action == "rename_copy":
                result.renamed_files += 1
            _log(result, MigrationEvent(action, str(source_file), str(destination)), progress)

        for relative_directory in included_directories:
            _cancelled(cancelled)
            destination_directory = staging / _renamed_directory_relative(relative_directory, options)
            destination_directory.mkdir(parents=True, exist_ok=True)

        for destination in copied:
            _cancelled(cancelled)
            _modify_text_file(destination, options, result, progress)

        _cancelled(cancelled)
        if target_was_empty:
            target.rmdir()
        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=False)
            created_parent = True
        staging.replace(target)
        _log(result, MigrationEvent("complete", str(source), str(target), "복제 및 마이그레이션 완료"), progress)
        return result
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if created_parent:
            current = target.parent
            while current != staging_parent and current.exists():
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent
        if target_was_empty and not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        raise


def format_event(event: MigrationEvent) -> str:
    labels = {
        "start": "시작", "preview": "사전 검사", "copy": "복사", "rename_copy": "이름 변경·복사",
        "modify": "내부 치환", "would_modify": "내부 치환 예정", "skip_dir": "폴더 제외", "skip_file": "파일 제외",
        "warning": "경고", "complete": "완료", "preview_complete": "사전 검사 완료",
    }
    label = labels.get(event.action, event.action)
    route = event.source
    if event.target and event.target != event.source:
        route += f"  →  {event.target}"
    if event.detail:
        route += f"  ·  {event.detail}"
    return f"[{label}] {route}"


def main() -> int:
    options = MigrationOptions(
        SOURCE_ROOT, TARGET_ROOT, OLD_PROJECT_KEYWORD, NEW_PROJECT_KEYWORD,
        OLD_EMBEDDED_PATH, NEW_EMBEDDED_PATH,
    )
    try:
        result = migrate_iar_project(options, lambda event: print(format_event(event), flush=True))
    except (MigrationError, MigrationCancelled, OSError) as error:
        print(f"[오류] {error}")
        return 1
    print(
        f"복사 {result.copied_files}개, 이름 변경 {result.renamed_files}개, "
        f"내부 수정 {result.modified_files}개, 문자열 치환 {result.replacement_count}건"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
