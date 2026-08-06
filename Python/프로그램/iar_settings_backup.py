from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


BACKUP_SCHEMA = "cch-iar-settings-backup"
BACKUP_VERSION = 1
SETTING_EXTENSIONS = {".crun", ".dbgdt", ".dnx", ".wsdt"}
CATEGORY_EXTENSIONS = {
    "live_watch": {".dbgdt"},
    "ctrace": {".crun", ".dbgdt", ".dnx", ".wsdt"},
}


class IarSettingsError(RuntimeError):
    pass


@dataclass(slots=True)
class IarSettingsBundle:
    category: str
    root: str
    project_name: str
    files: dict[str, bytes] = field(default_factory=dict)
    manifest_path: str = ""


@dataclass(slots=True)
class IarSettingsRestoreResult:
    written_files: list[str] = field(default_factory=list)
    omitted_watch_expressions: list[str] = field(default_factory=list)
    retained_watch_expressions: list[str] = field(default_factory=list)


def default_backup_root() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Call Hierarchy Tools" / "C Call Hierarchy Explorer" / "iar-settings-backups"


def _safe_folder_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .")
    return cleaned or "IAR-Project"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_xml(data: bytes, label: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise IarSettingsError(f"IAR 설정 XML을 해석할 수 없습니다: {label} ({error})") from error


def discover_settings_files(workspace_file: str | Path) -> dict[str, Path]:
    workspace = Path(workspace_file).expanduser().resolve(strict=True)
    candidates: list[Path] = []
    direct = workspace.parent / "settings"
    if direct.is_dir():
        candidates.extend(path for path in direct.iterdir() if path.is_file())
    # Some EWARM versions place settings beside the workspace or one level below.
    candidates.extend(path for path in workspace.parent.iterdir() if path.is_file())
    active_stem = ""
    wsdt = direct / "Project.wsdt"
    if wsdt.is_file():
        try:
            wsdt_root = _parse_xml(wsdt.read_bytes(), wsdt.name)
            active_text = wsdt_root.findtext("./ConfigDictionary/CurrentConfigs/Project", "")
            active_stem = active_text.split("/", 1)[0].strip()
        except (IarSettingsError, OSError):
            pass
    result: dict[str, Path] = {}
    ordered = sorted(
        set(candidates),
        key=lambda item: (
            item.suffix.casefold(),
            0 if active_stem and item.stem.casefold() == active_stem.casefold() else 1,
            item.name.casefold(),
        ),
    )
    for path in ordered:
        extension = path.suffix.casefold()
        if extension not in SETTING_EXTENSIONS:
            continue
        if extension == ".wsdt" or workspace.stem.casefold() in path.stem.casefold():
            result.setdefault(extension, path)
    # Generic Project.eww normally has project-specific debugger filenames.
    for path in candidates:
        extension = path.suffix.casefold()
        if extension in SETTING_EXTENSIONS:
            result.setdefault(extension, path)
    return result


def _collect_folder_files(folder: Path, category: str) -> dict[str, bytes]:
    allowed = CATEGORY_EXTENSIONS[category]
    found: dict[str, Path] = {}
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.casefold() in allowed:
            current = found.get(path.suffix.casefold())
            if current is None or path.stat().st_mtime_ns > current.stat().st_mtime_ns:
                found[path.suffix.casefold()] = path
    return {path.name: path.read_bytes() for path in found.values()}


def create_settings_backup(
    workspace_file: str | Path,
    project_name: str,
    category: str,
    backup_root: str | Path | None = None,
) -> Path:
    if category not in CATEGORY_EXTENSIONS:
        raise IarSettingsError(f"지원하지 않는 IAR 설정 백업 종류입니다: {category}")
    sources = discover_settings_files(workspace_file)
    selected = {
        extension: path for extension, path in sources.items()
        if extension in CATEGORY_EXTENSIONS[category]
    }
    required = ".dbgdt" if category == "live_watch" else ".dnx"
    if required not in selected:
        raise IarSettingsError(f"백업할 {required} 설정 파일을 찾지 못했습니다.")
    root = Path(backup_root) if backup_root else default_backup_root()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = root / _safe_folder_name(project_name) / timestamp / (
        "LiveWatch" if category == "live_watch" else "CTrace"
    )
    suffix = 1
    base = destination
    while destination.exists():
        destination = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    try:
        for extension, source in sorted(selected.items()):
            before = source.stat()
            data = source.read_bytes()
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise IarSettingsError(
                    f"백업 중 IAR 설정 파일이 변경되었습니다. IAR 저장이 끝난 뒤 다시 시도하십시오: {source}"
                )
            _parse_xml(data, source.name)
            target = destination / source.name
            target.write_bytes(data)
            records.append({"name": source.name, "extension": extension, "sha256": _sha256(data)})
        manifest = {
            "schema": BACKUP_SCHEMA,
            "version": BACKUP_VERSION,
            "category": category,
            "project_name": project_name,
            "source_workspace": str(Path(workspace_file).resolve(strict=False)),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "files": records,
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def _candidate_manifest(folder: Path, category: str) -> Path | None:
    manifests: list[Path] = []
    direct = folder / "manifest.json"
    if direct.is_file():
        manifests.append(direct)
    manifests.extend(folder.rglob("manifest.json"))
    compatible: list[Path] = []
    for path in set(manifests):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("schema") == BACKUP_SCHEMA and payload.get("category") == category:
            compatible.append(path)
    return max(compatible, key=lambda path: path.stat().st_mtime_ns) if compatible else None


def load_settings_backup(folder: str | Path, category: str) -> IarSettingsBundle:
    if category not in CATEGORY_EXTENSIONS:
        raise IarSettingsError(f"지원하지 않는 IAR 설정 종류입니다: {category}")
    root = Path(folder).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise IarSettingsError(f"IAR 설정 백업 폴더가 아닙니다: {root}")
    manifest_path = _candidate_manifest(root, category)
    files: dict[str, bytes] = {}
    project_name = root.name
    if manifest_path is not None:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        project_name = str(payload.get("project_name") or project_name)
        for record in payload.get("files", []):
            name = Path(str(record.get("name", ""))).name
            path = manifest_path.parent / name
            if not name or not path.is_file():
                raise IarSettingsError(f"백업 구성 파일이 없습니다: {name}")
            data = path.read_bytes()
            if _sha256(data) != record.get("sha256"):
                raise IarSettingsError(f"백업 파일 무결성 검사가 실패했습니다: {name}")
            if path.suffix.casefold() in CATEGORY_EXTENSIONS[category]:
                _parse_xml(data, name)
                files[name] = data
    else:
        files = _collect_folder_files(root, category)
        for name, data in files.items():
            _parse_xml(data, name)
    required = ".dbgdt" if category == "live_watch" else ".dnx"
    if not any(Path(name).suffix.casefold() == required for name in files):
        raise IarSettingsError(
            f"선택한 폴더와 하위 폴더에서 {required} 파일을 찾지 못했습니다."
        )
    return IarSettingsBundle(
        category=category, root=str(root), project_name=project_name, files=files,
        manifest_path=str(manifest_path or ""),
    )


def bundle_from_workspace(workspace_file: str | Path, project_name: str, category: str) -> IarSettingsBundle:
    discovered = discover_settings_files(workspace_file)
    files = {
        path.name: path.read_bytes() for extension, path in discovered.items()
        if extension in CATEGORY_EXTENSIONS[category]
    }
    required = ".dbgdt" if category == "live_watch" else ".dnx"
    if not any(Path(name).suffix.casefold() == required for name in files):
        raise IarSettingsError(f"원본 프로젝트에서 {required} 설정 파일을 찾지 못했습니다.")
    return IarSettingsBundle(category, str(Path(workspace_file).parent / "settings"), project_name, files)


def _file_by_extension(bundle: IarSettingsBundle | None, extension: str) -> tuple[str, bytes] | None:
    if bundle is None:
        return None
    for name, data in bundle.files.items():
        if Path(name).suffix.casefold() == extension:
            return name, data
    return None


def _static_watch_expressions(root: ET.Element) -> tuple[ET.Element | None, list[ET.Element]]:
    child_map = root.find("./WindowStorage/ChildIdMap")
    desktop = root.find("./WindowStorage/Desktop")
    if child_map is None or desktop is None:
        return None, []
    identifier = child_map.findtext("WIN_STATIC_WATCH", "").strip()
    pane = desktop.find(f"IarPane-{identifier}") if identifier else None
    if pane is None:
        for candidate in desktop:
            if candidate.find("expressions") is not None:
                pane = candidate
                break
    expressions = pane.find("expressions") if pane is not None else None
    return expressions, list(expressions) if expressions is not None else []


def _expression_root(expression: str) -> str:
    value = re.sub(r"^\s*\([^)]*\)\s*", "", expression.strip())
    value = re.sub(r"^[*&]+\s*", "", value)
    match = re.search(r"[A-Za-z_]\w*", value)
    return match.group(0) if match else ""


def _is_debugger_expression(expression: str) -> bool:
    value = expression.strip()
    if value.startswith(("$", "@")):
        return True
    root = _expression_root(value).casefold()
    return root in {
        "pc", "sp", "lr", "xpsr", "msp", "psp", "primask", "basepri",
        "faultmask", "control",
    } or bool(re.fullmatch(r"r(?:1[0-5]|[0-9])", root))


def _decode_settings_text(data: bytes, label: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "windows-1252"):
        try:
            text = data.decode(encoding)
            return re.sub(
                r"(<\?xml\b[^>]*\bencoding\s*=\s*['\"])[^'\"]+(['\"])",
                r"\1UTF-8\2", text, count=1, flags=re.IGNORECASE,
            )
        except UnicodeDecodeError:
            continue
    raise IarSettingsError(f"IAR 설정 파일 인코딩을 판별할 수 없습니다: {label}")


def _source_identifiers(project_root: Path) -> set[str]:
    identifiers: set[str] = set()
    block_comment = re.compile(r"/\*.*?\*/", re.DOTALL)
    line_comment = re.compile(r"//[^\r\n]*")
    string_literal = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
    for path in project_root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in {".c", ".h", ".cpp", ".hpp"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="cp949")
            except (OSError, UnicodeDecodeError):
                continue
        except OSError:
            continue
        cleaned = string_literal.sub(" ", line_comment.sub(" ", block_comment.sub(" ", text)))
        identifiers.update(re.findall(r"\b[A-Za-z_]\w*\b", cleaned))
    return identifiers


def _serialize_xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _prepare_dbgdt(
    base_data: bytes,
    live_data: bytes | None,
    project_root: Path,
    include_live_watch: bool,
) -> tuple[bytes, list[str], list[str]]:
    root = _parse_xml(base_data, ".dbgdt")
    target_expressions, _ = _static_watch_expressions(root)
    if live_data is not None:
        live_root = _parse_xml(live_data, "Live Watch .dbgdt")
        source_expressions, _ = _static_watch_expressions(live_root)
        if source_expressions is not None and target_expressions is not None:
            target_expressions.clear()
            for item in source_expressions:
                target_expressions.append(ET.fromstring(ET.tostring(item, encoding="unicode")))
    retained: list[str] = []
    omitted: list[str] = []
    expressions, items = _static_watch_expressions(root)
    if expressions is not None:
        if not include_live_watch:
            expressions.clear()
        else:
            identifiers = _source_identifiers(project_root)
            for item in items:
                value = (item.text or "").strip()
                if not value:
                    continue
                root_name = _expression_root(value)
                # Keep expressions that cannot be reduced safely. Only remove a
                # variable when its root identifier is definitely absent.
                if root_name and root_name not in identifiers and not _is_debugger_expression(value):
                    expressions.remove(item)
                    omitted.append(value)
                else:
                    retained.append(value)
    return _serialize_xml(root), retained, omitted


def restore_settings_to_project(
    target_root: str | Path,
    old_keyword: str,
    new_keyword: str,
    old_path: str,
    new_path: str,
    live_bundle: IarSettingsBundle | None,
    ctrace_bundle: IarSettingsBundle | None,
    replace_text: Callable[[str], str],
    settings_dir: str | Path | None = None,
) -> IarSettingsRestoreResult:
    target = Path(target_root)
    if settings_dir is None:
        workspaces = sorted(target.rglob("*.eww"))
        if not workspaces:
            raise IarSettingsError("새 프로젝트에서 IAR 워크스페이스(.eww)를 찾지 못했습니다.")
        settings_dir = workspaces[0].parent / "settings"
    else:
        settings_dir = Path(settings_dir)
    settings_dir.mkdir(parents=True, exist_ok=True)
    result = IarSettingsRestoreResult()
    base_dbgdt = _file_by_extension(ctrace_bundle, ".dbgdt") or _file_by_extension(live_bundle, ".dbgdt")
    live_dbgdt = _file_by_extension(live_bundle, ".dbgdt")
    selected: dict[str, tuple[str, bytes]] = {}
    if ctrace_bundle is not None:
        for extension in (".crun", ".dnx", ".wsdt"):
            found = _file_by_extension(ctrace_bundle, extension)
            if found:
                selected[extension] = found
    if base_dbgdt is not None:
        data, retained, omitted = _prepare_dbgdt(
            base_dbgdt[1], live_dbgdt[1] if live_dbgdt else None,
            target, live_bundle is not None,
        )
        selected[".dbgdt"] = (base_dbgdt[0], data)
        result.retained_watch_expressions = retained
        result.omitted_watch_expressions = omitted
    for extension, (source_name, data) in selected.items():
        text = _decode_settings_text(data, source_name)
        text = replace_text(text)
        output_name = source_name.replace(old_keyword, new_keyword)
        if extension == ".wsdt":
            output_name = "Project.wsdt"
        output = settings_dir / output_name
        if output.exists():
            raise IarSettingsError(
                f"기존 대상의 IAR 설정 파일과 충돌하여 덮어쓰지 않았습니다: {output}"
            )
        output.write_text(text, encoding="utf-8", newline="")
        _parse_xml(output.read_bytes(), output.name)
        result.written_files.append(str(output))
    return result
