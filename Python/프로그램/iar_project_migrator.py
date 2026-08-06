from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from iar_settings_backup import (
    IarSettingsBundle, IarSettingsError, bundle_from_workspace,
    load_settings_backup, restore_settings_to_project,
)


# Stand-alone use defaults.  The integrated UI supplies these values directly.
SOURCE_ROOT = r""
TARGET_ROOT = r""
OLD_PROJECT_KEYWORD = ""
NEW_PROJECT_KEYWORD = ""
OLD_EMBEDDED_PATH = ""
NEW_EMBEDDED_PATH = ""

IAR_EXTENSIONS = {".eww", ".ewp", ".ewd", ".ewt", ".ewf", ".ewg"}
CUBEMX_EXTENSIONS = {".ioc", ".mxproject"}
SOURCE_TEXT_EXTENSIONS = {
    ".c", ".h", ".cpp", ".cxx", ".cc", ".hpp", ".hxx", ".s", ".asm",
    ".inc", ".icf", ".xcl", ".txt", ".xml", ".json", ".yml", ".yaml",
}
SKIP_DIRECTORY_NAMES = {"debug", "release", ".iar", "settings", ".git"}
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
    source_workspace: str = ""
    copy_live_watch: bool = False
    copy_ctrace: bool = False
    live_watch_backup_dir: str = ""
    ctrace_backup_dir: str = ""


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
    settings_files_written: int = 0
    watch_expressions_retained: int = 0
    watch_expressions_omitted: list[str] = field(default_factory=list)


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
    generic_workspace_names = {"project", "workspace", "iar", "ewarm"}
    candidate_stems = [Path(reference.replace("\\", "/")).stem for reference in references]
    candidate_stems.extend(
        path.stem for path in workspace.parent.iterdir()
        if path.is_file() and path.suffix.casefold() in IAR_EXTENSIONS - {".eww"}
    )
    unique_candidates = list(dict.fromkeys(candidate_stems))
    keyword = workspace.stem
    if unique_candidates:
        common = os.path.commonprefix(unique_candidates).rstrip("_- .")
        if common:
            keyword = common
        elif workspace.stem.casefold() in generic_workspace_names:
            keyword = min(unique_candidates, key=len)
        keyword = re.sub(
            r"(?:[_ .-](?:class[a-z0-9]+|debug|release))$", "", keyword,
            flags=re.IGNORECASE,
        ) or keyword
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
    if target.exists() and not target.is_dir():
        raise MigrationError(f"새 프로젝트 경로에 같은 이름의 파일이 있습니다: {target}")
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
            generated_iar_output = (
                current_path.name.casefold() in {"ewarm", "iar"}
                and path.is_dir()
                and any((path / child).is_dir() for child in ("Obj", "BrowseInfo", "List", "Exe"))
            )
            if name.casefold() in SKIP_DIRECTORY_NAMES or path.is_symlink() or generated_iar_output:
                result.skipped_directories += 1
                if generated_iar_output:
                    detail = "IAR 빌드 산출물 폴더 제외"
                else:
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
    return IAR_EXTENSIONS | CUBEMX_EXTENSIONS | (SOURCE_TEXT_EXTENSIONS if options.replace_source_text else set())


def _is_text_file(path: Path, options: MigrationOptions) -> bool:
    return path.name.casefold() == ".mxproject" or path.suffix.casefold() in _text_extensions(options)


_IOC_IDENTITY_KEYS = {
    "ProjectManager.ProjectName",
    "ProjectManager.ProjectFileName",
}


def synchronize_cubemx_ioc(
    text: str,
    options: MigrationOptions,
) -> tuple[str, int]:
    """Update only CubeMX project identity/path fields and preserve all hardware settings.

    STM32CubeMX .ioc files are line-oriented key/value documents.  Pin, clock,
    middleware and code-generation options must remain byte-for-byte identical.
    Therefore only ProjectManager identity values and ProjectManager path values
    that actually contain the old project path/name are eligible for replacement.
    """
    lines = text.splitlines(keepends=True)
    changed = 0
    updated: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        if "=" not in body or body.lstrip().startswith("#"):
            updated.append(line)
            continue
        key, value = body.split("=", 1)
        if not key.startswith("ProjectManager."):
            updated.append(line)
            continue
        replacement = value
        replacement, path_count = _replace_paths(
            replacement, options.old_embedded_path, options.new_embedded_path
        )
        replacement, absolute_count = _replace_paths(
            replacement, options.source_root, options.target_root
        )
        keyword_count = 0
        if (
            key in _IOC_IDENTITY_KEYS
            and options.old_keyword != options.new_keyword
            and options.old_keyword in replacement
        ):
            keyword_count = replacement.count(options.old_keyword)
            replacement = replacement.replace(options.old_keyword, options.new_keyword)
        eligible = key in _IOC_IDENTITY_KEYS or path_count or absolute_count or keyword_count
        if eligible and replacement != value:
            changed += path_count + absolute_count + keyword_count
            updated.append(f"{key}={replacement}{newline}")
        else:
            updated.append(line)

    result = "".join(updated)
    # Safety invariant: every non-ProjectManager line (MCU/pin/clock/middleware)
    # must remain exactly unchanged, including order and line endings.
    before_protected = [line for line in lines if not line.startswith("ProjectManager.")]
    after_protected = [line for line in result.splitlines(keepends=True) if not line.startswith("ProjectManager.")]
    if before_protected != after_protected:
        raise MigrationError("CubeMX .ioc 하드웨어 설정 보존 검증에 실패했습니다.")
    return result, changed


def synchronize_mxproject(text: str, options: MigrationOptions) -> tuple[str, int]:
    """Preserve CubeMX metadata and adjust only explicit migrated path values."""
    lines = text.splitlines(keepends=True)
    changed = 0
    updated: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        if "=" not in body or body.lstrip().startswith(("#", ";")):
            updated.append(line)
            continue
        key, value = body.split("=", 1)
        replacement, embedded_count = _replace_paths(
            value, options.old_embedded_path, options.new_embedded_path
        )
        replacement, absolute_count = _replace_paths(
            replacement, options.source_root, options.target_root
        )
        count = embedded_count + absolute_count
        if count and replacement != value:
            updated.append(f"{key}={replacement}{newline}")
            changed += count
        else:
            updated.append(line)
    return "".join(updated), changed


def _modify_text_file(
    path: Path,
    options: MigrationOptions,
    result: MigrationResult,
    progress: Callable[[MigrationEvent], None] | None,
) -> None:
    if not _is_text_file(path, options):
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
    if path.suffix.casefold() == ".ioc":
        text, ioc_count = synchronize_cubemx_ioc(text, options)
        path_count = ioc_count
        keyword_count = 0
    elif path.name.casefold() == ".mxproject":
        text, path_count = synchronize_mxproject(text, options)
        keyword_count = 0
    else:
        text, path_count = _replace_paths(text, options.old_embedded_path, options.new_embedded_path)
        text, absolute_path_count = _replace_paths(text, options.source_root, options.target_root)
        path_count += absolute_path_count
        keyword_count = 0
        if options.old_keyword != options.new_keyword:
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
            f"문자열 {keyword_count + path_count}건 · 프로젝트 표시 이름 {project_count}건 · {encoding}"
            + (" · CubeMX 설정 보존 검증 완료" if path.suffix.casefold() == ".ioc" else ""),
        ),
        progress,
    )


def _renamed_relative(relative: Path, options: MigrationOptions) -> Path:
    parts = list(relative.parts)
    if options.rename_directories:
        parts[:-1] = [part.replace(options.old_keyword, options.new_keyword) for part in parts[:-1]]
    if relative.suffix.casefold() in IAR_EXTENSIONS | CUBEMX_EXTENSIONS:
        parts[-1] = parts[-1].replace(options.old_keyword, options.new_keyword)
    return Path(*parts)


def _renamed_directory_relative(relative: Path, options: MigrationOptions) -> Path:
    if not options.rename_directories:
        return relative
    return Path(*(part.replace(options.old_keyword, options.new_keyword) for part in relative.parts))


def _source_workspace(options: MigrationOptions, source: Path) -> Path:
    if options.source_workspace:
        workspace = Path(options.source_workspace).expanduser().resolve(strict=True)
        if workspace.suffix.casefold() != ".eww":
            raise MigrationError("IAR 설정 복원 기준 파일은 .eww 워크스페이스여야 합니다.")
        return workspace
    workspaces = sorted(source.rglob("*.eww"))
    if not workspaces:
        raise MigrationError("IAR 설정 복원을 위한 원본 .eww 파일을 찾지 못했습니다.")
    return workspaces[0]


def _settings_bundles(
    options: MigrationOptions,
    source: Path,
) -> tuple[IarSettingsBundle | None, IarSettingsBundle | None]:
    if not options.copy_live_watch and not options.copy_ctrace:
        return None, None
    workspace = _source_workspace(options, source)
    try:
        live = (
            load_settings_backup(options.live_watch_backup_dir, "live_watch")
            if options.copy_live_watch and options.live_watch_backup_dir
            else bundle_from_workspace(workspace, options.old_keyword, "live_watch")
            if options.copy_live_watch else None
        )
        ctrace = (
            load_settings_backup(options.ctrace_backup_dir, "ctrace")
            if options.copy_ctrace and options.ctrace_backup_dir
            else bundle_from_workspace(workspace, options.old_keyword, "ctrace")
            if options.copy_ctrace else None
        )
    except (IarSettingsError, OSError) as error:
        raise MigrationError(str(error)) from error
    return live, ctrace


def _replace_setting_text(text: str, options: MigrationOptions) -> str:
    text, _ = _replace_paths(text, options.old_embedded_path, options.new_embedded_path)
    text, _ = _replace_paths(text, options.source_root, options.target_root)
    if options.old_keyword != options.new_keyword:
        text = text.replace(options.old_keyword, options.new_keyword)
    return text


def _preview_settings(
    options: MigrationOptions,
    source: Path,
    target: Path,
    result: MigrationResult,
    progress: Callable[[MigrationEvent], None] | None,
) -> None:
    live, ctrace = _settings_bundles(options, source)
    workspace = _source_workspace(options, source)
    try:
        workspace_relative = workspace.relative_to(source)
        settings_target = target / _renamed_relative(workspace_relative, options).parent / "settings"
    except ValueError:
        settings_target = target / "EWARM" / "settings"
    seen: set[str] = set()
    for bundle, label in ((live, "Live Watch"), (ctrace, "C-Trace")):
        if bundle is None:
            continue
        for name in sorted(bundle.files):
            key = Path(name).suffix.casefold()
            if key in seen:
                continue
            seen.add(key)
            output_name = name.replace(options.old_keyword, options.new_keyword)
            _log(
                result,
                MigrationEvent(
                    "settings_preview", str(Path(bundle.root) / name),
                    str(settings_target / output_name), f"{label} 설정 복원 예정",
                ),
                progress,
            )


def preview_iar_migration(
    options: MigrationOptions,
    progress: Callable[[MigrationEvent], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> MigrationResult:
    source, target = validate_options(options)
    result = MigrationResult(str(source), str(target))
    destinations: set[str] = set()
    existing_paths: set[str] = set()
    if target.is_dir():
        existing_paths = {
            str(path.relative_to(target)).casefold()
            for path in target.rglob("*")
        }
    _log(result, MigrationEvent("preview", str(source), str(target), "사전 검사 시작"), progress)
    for source_file, relative in _iter_source_files(source, result, progress, cancelled):
        _cancelled(cancelled)
        renamed_relative = _renamed_relative(relative, options)
        destination = target / renamed_relative
        collision_key = str(destination).casefold()
        relative_key = str(renamed_relative).casefold()
        if collision_key in destinations or relative_key in existing_paths:
            raise MigrationError(f"이름 변경 후 파일 경로가 충돌합니다: {renamed_relative}")
        destinations.add(collision_key)
        result.copied_files += 1
        renamed = renamed_relative != relative
        if renamed:
            result.renamed_files += 1
        action = "rename_copy" if renamed else "copy"
        _log(result, MigrationEvent(action, str(source_file), str(destination)), progress)
        if not _is_text_file(source_file, options):
            continue
        try:
            text, _, _ = _decode_text(source_file.read_bytes())
        except (OSError, UnicodeDecodeError):
            continue
        original = text
        if source_file.suffix.casefold() == ".ioc":
            text, path_count = synchronize_cubemx_ioc(text, options)
            keyword_count = 0
        elif source_file.name.casefold() == ".mxproject":
            text, path_count = synchronize_mxproject(text, options)
            keyword_count = 0
        else:
            text, path_count = _replace_paths(text, options.old_embedded_path, options.new_embedded_path)
            text, absolute_path_count = _replace_paths(text, options.source_root, options.target_root)
            path_count += absolute_path_count
            keyword_count = 0
            if options.old_keyword != options.new_keyword:
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
    _preview_settings(options, source, target, result, progress)
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
    target_existed = target.exists()
    created_parent = False
    backup: Path | None = None
    try:
        _log(result, MigrationEvent("start", str(source), str(target), "안전 복제 시작"), progress)
        if target_existed:
            shutil.copytree(target, staging, dirs_exist_ok=True, symlinks=True)
            _log(result, MigrationEvent("preserve", str(target), str(staging), "기존 대상 내용 보존"), progress)
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
            if collision_key in destinations or destination.exists() or destination.is_symlink():
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

        live_bundle, ctrace_bundle = _settings_bundles(options, source)
        if live_bundle is not None or ctrace_bundle is not None:
            _cancelled(cancelled)
            source_workspace = _source_workspace(options, source)
            try:
                workspace_relative = source_workspace.relative_to(source)
                settings_dir = staging / _renamed_relative(workspace_relative, options).parent / "settings"
            except ValueError:
                settings_dir = None
            try:
                settings_result = restore_settings_to_project(
                    staging, options.old_keyword, options.new_keyword,
                    options.old_embedded_path, options.new_embedded_path,
                    live_bundle, ctrace_bundle,
                    lambda text: _replace_setting_text(text, options),
                    settings_dir,
                )
            except IarSettingsError as error:
                raise MigrationError(str(error)) from error
            result.settings_files_written = len(settings_result.written_files)
            result.watch_expressions_retained = len(settings_result.retained_watch_expressions)
            result.watch_expressions_omitted = settings_result.omitted_watch_expressions
            for path in settings_result.written_files:
                _log(result, MigrationEvent("settings_restore", "", path, "IAR 사용자 설정 복원"), progress)
            for expression in settings_result.omitted_watch_expressions:
                warning = f"대상 소스에서 찾지 못해 Live Watch 항목 제외: {expression}"
                result.warnings.append(warning)
                _log(result, MigrationEvent("watch_omit", expression, detail=warning), progress)

        _cancelled(cancelled)
        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=False)
            created_parent = True
        if target_existed:
            backup = target.with_name(f".{target.name}.iar-backup-{uuid.uuid4().hex[:10]}")
            target.replace(backup)
        try:
            staging.replace(target)
        except BaseException:
            if backup is not None and backup.exists() and not target.exists():
                backup.replace(target)
            raise
        if backup is not None and backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError as error:
                warning = f"이전 대상 백업 폴더를 자동 삭제하지 못했습니다: {backup} ({error})"
                result.warnings.append(warning)
                _log(result, MigrationEvent("warning", str(backup), detail=warning), progress)
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
        raise


def format_event(event: MigrationEvent) -> str:
    labels = {
        "start": "시작", "preview": "사전 검사", "copy": "복사", "rename_copy": "이름 변경·복사",
        "modify": "내부 치환", "would_modify": "내부 치환 예정", "skip_dir": "폴더 제외", "skip_file": "파일 제외",
        "warning": "경고", "complete": "완료", "preview_complete": "사전 검사 완료",
        "preserve": "기존 대상 보존",
        "settings_preview": "설정 복원 예정", "settings_restore": "설정 복원",
        "watch_omit": "Live Watch 제외",
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
