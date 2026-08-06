from __future__ import annotations

import hashlib
import base64
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
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
    source_workspace: str = ""


@dataclass(slots=True)
class IarSettingsRestoreResult:
    written_files: list[str] = field(default_factory=list)
    omitted_watch_expressions: list[str] = field(default_factory=list)
    retained_watch_expressions: list[str] = field(default_factory=list)
    backup_folders: list[str] = field(default_factory=list)
    preserved_hardware_settings: bool = False


def default_backup_root() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Call Hierarchy Tools" / "C Call Hierarchy Explorer" / "iar-settings-backups"


def default_global_settings_root() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Call Hierarchy Tools" / "C Call Hierarchy Explorer" / "iar-global-settings" / "Default"


def _bundled_global_settings_asset() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / "iar_default_settings.zip.b64"


def ensure_default_global_settings(
    destination: str | Path | None = None,
    asset_path: str | Path | None = None,
) -> Path:
    """Install the bundled generic template once without overwriting user customizations."""
    target = Path(destination) if destination else default_global_settings_root()
    manifest = target / "global-manifest.json"
    if manifest.is_file():
        return target
    asset = Path(asset_path) if asset_path else _bundled_global_settings_asset()
    if not asset.is_file():
        raise IarSettingsError(f"기본 IAR 디버그 설정 자산을 찾을 수 없습니다: {asset}")
    try:
        archive = base64.b64decode(asset.read_text(encoding="ascii").strip(), validate=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            allowed = {"Global.crun", "Global.dbgdt", "Global.dnx", "Project.wsdt", "global-manifest.json"}
            names = set(zipped.namelist())
            if not names or not names.issubset(allowed) or "global-manifest.json" not in names:
                raise IarSettingsError("기본 IAR 디버그 설정 패키지 구성이 올바르지 않습니다.")
            staging: Path | None = Path(
                tempfile.mkdtemp(prefix=".cch-default-", dir=target.parent if target.parent.exists() else None)
            )
            try:
                for name in names:
                    data = zipped.read(name)
                    if Path(name).suffix.casefold() in SETTING_EXTENSIONS:
                        _parse_xml(data, name)
                    (staging / name).write_bytes(data)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # A user-created folder is authoritative; fill only missing initial files.
                    target.mkdir(parents=True, exist_ok=True)
                    for source in staging.iterdir():
                        destination_file = target / source.name
                        if not destination_file.exists():
                            shutil.copy2(source, destination_file)
                else:
                    shutil.move(str(staging), str(target))
                    staging = None
            finally:
                if staging is not None and staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        raise IarSettingsError(f"기본 IAR 디버그 설정 설치에 실패했습니다: {error}") from error
    return target


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
    source_workspace = ""
    if manifest_path is not None:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        project_name = str(payload.get("project_name") or project_name)
        source_workspace = str(payload.get("source_workspace") or "")
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
        global_manifest = root / "global-manifest.json"
        if global_manifest.is_file():
            try:
                global_payload = json.loads(global_manifest.read_text(encoding="utf-8"))
                project_name = str(global_payload.get("source_project") or project_name)
                source_workspace = str(global_payload.get("source_workspace") or "")
            except (OSError, ValueError):
                pass
        for name, data in files.items():
            _parse_xml(data, name)
        stems = [
            Path(name).stem for name in files
            if Path(name).suffix.casefold() in {".crun", ".dbgdt", ".dnx"}
        ]
        if stems:
            common = os.path.commonprefix(stems).rstrip("_- .") or stems[0]
            project_name = re.sub(
                r"(?:[_ .-](?:class[a-z0-9]+|debug|release))$", "", common,
                flags=re.IGNORECASE,
            ) or common
    required = ".dbgdt" if category == "live_watch" else ".dnx"
    if not any(Path(name).suffix.casefold() == required for name in files):
        raise IarSettingsError(
            f"선택한 폴더와 하위 폴더에서 {required} 파일을 찾지 못했습니다."
        )
    return IarSettingsBundle(
        category=category, root=str(root), project_name=project_name, files=files,
        manifest_path=str(manifest_path or ""), source_workspace=source_workspace,
    )


def save_current_as_global_settings(
    workspace_file: str | Path,
    global_folder: str | Path,
    include_live_watch: bool = True,
    include_ctrace: bool = True,
) -> Path:
    workspace = Path(workspace_file).expanduser().resolve(strict=True)
    if workspace.suffix.casefold() != ".eww":
        raise IarSettingsError("현재 프로젝트의 IAR 워크스페이스(.eww)를 선택하십시오.")
    if not include_live_watch and not include_ctrace:
        raise IarSettingsError("글로벌로 저장할 Live Watch 또는 C-Trace 설정을 선택하십시오.")
    discovered = discover_settings_files(workspace)
    selected: dict[str, Path] = {}
    if include_live_watch:
        if ".dbgdt" not in discovered:
            raise IarSettingsError("현재 프로젝트에서 Live Watch .dbgdt를 찾지 못했습니다.")
        selected["Global.dbgdt"] = discovered[".dbgdt"]
    if include_ctrace:
        if ".dnx" not in discovered:
            raise IarSettingsError("현재 프로젝트에서 C-Trace .dnx를 찾지 못했습니다.")
        for extension, output_name in (
            (".dnx", "Global.dnx"), (".crun", "Global.crun"), (".wsdt", "Project.wsdt"),
        ):
            if extension in discovered:
                selected[output_name] = discovered[extension]
        if ".dbgdt" in discovered:
            selected["Global.dbgdt"] = discovered[".dbgdt"]
    destination = Path(global_folder).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cch-global-", dir=destination.parent))
    history: Path | None = None
    try:
        records = []
        for output_name, source in selected.items():
            before = source.stat()
            data = source.read_bytes()
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise IarSettingsError(
                    f"저장 중 IAR 설정이 변경되었습니다. IAR 저장 완료 후 다시 시도하십시오: {source}"
                )
            _parse_xml(data, source.name)
            (staging / output_name).write_bytes(data)
            records.append({"name": output_name, "sha256": _sha256(data)})
        manifest = {
            "schema": "cch-iar-global-settings",
            "version": 1,
            "source_project": _base_project_name(_active_project_stem(workspace)),
            "source_workspace": str(workspace),
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "include_live_watch": include_live_watch,
            "include_ctrace": include_ctrace,
            "files": records,
        }
        (staging / "global-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        existing = [destination / name for name in selected]
        existing.append(destination / "global-manifest.json")
        if any(path.exists() for path in existing):
            history = destination / ".history" / datetime.now().strftime("%Y%m%d-%H%M%S")
            history.mkdir(parents=True, exist_ok=False)
            for path in existing:
                if path.exists():
                    shutil.copy2(path, history / path.name)
        for path in staging.iterdir():
            target = destination / path.name
            temporary_target = destination / f".{path.name}.new-{uuid.uuid4().hex[:8]}"
            shutil.copy2(path, temporary_target)
            temporary_target.replace(target)
    except BaseException:
        if history is not None and history.exists():
            for path in history.iterdir():
                shutil.copy2(path, destination / path.name)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


def bundle_from_workspace(workspace_file: str | Path, project_name: str, category: str) -> IarSettingsBundle:
    discovered = discover_settings_files(workspace_file)
    files = {
        path.name: path.read_bytes() for extension, path in discovered.items()
        if extension in CATEGORY_EXTENSIONS[category]
    }
    required = ".dbgdt" if category == "live_watch" else ".dnx"
    if not any(Path(name).suffix.casefold() == required for name in files):
        raise IarSettingsError(f"원본 프로젝트에서 {required} 설정 파일을 찾지 못했습니다.")
    return IarSettingsBundle(
        category, str(Path(workspace_file).parent / "settings"), project_name, files,
        source_workspace=str(Path(workspace_file).resolve(strict=False)),
    )


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


_CTRACE_ROOT_NODES = {
    "InterruptStripe", "EventStripe", "Trace1", "Trace2", "SWOTraceHWSettings",
    "SWOTraceWindow", "PowerLog", "DataLog", "InterruptLog", "EventLog",
    "CallStackLog", "CallStackStripe", "TermIOLog",
}


def _clone_element(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="unicode"))


def _replace_child(parent: ET.Element, source: ET.Element) -> None:
    current = parent.find(source.tag)
    replacement = _clone_element(source)
    if current is None:
        parent.append(replacement)
        return
    index = list(parent).index(current)
    parent.remove(current)
    parent.insert(index, replacement)


def _pane(root: ET.Element, map_name: str) -> ET.Element | None:
    child_map = root.find("./WindowStorage/ChildIdMap")
    desktop = root.find("./WindowStorage/Desktop")
    if child_map is None or desktop is None:
        return None
    identifier = child_map.findtext(map_name, "").strip()
    return desktop.find(f"IarPane-{identifier}") if identifier else None


def _merge_dbgdt(
    target_data: bytes | None,
    live_data: bytes | None,
    trace_data: bytes | None,
    project_root: Path,
) -> tuple[bytes, list[str], list[str]]:
    base_data = target_data or trace_data or live_data
    if base_data is None:
        raise IarSettingsError("적용할 .dbgdt 설정이 없습니다.")
    root = _parse_xml(base_data, "target .dbgdt")
    if trace_data is not None:
        trace_root = _parse_xml(trace_data, "C-Trace .dbgdt")
        source_pane = _pane(trace_root, "WIN_TIMELINE_GRAPH")
        target_pane = _pane(root, "WIN_TIMELINE_GRAPH")
        if source_pane is not None and target_pane is not None:
            for child in source_pane:
                if child.tag.startswith("Timeline"):
                    _replace_child(target_pane, child)
    if live_data is not None:
        live_root = _parse_xml(live_data, "Live Watch .dbgdt")
        source_expressions, _ = _static_watch_expressions(live_root)
        target_expressions, _ = _static_watch_expressions(root)
        if source_expressions is None:
            raise IarSettingsError("선택한 .dbgdt에서 Live Watch 변수 목록을 찾지 못했습니다.")
        if target_expressions is None:
            # A source debugger desktop is the safest complete base when the
            # current project has never created a Static Watch pane.
            root = live_root
            target_expressions, _ = _static_watch_expressions(root)
        if target_expressions is not None:
            target_expressions.clear()
            for item in source_expressions:
                target_expressions.append(_clone_element(item))
    retained: list[str] = []
    omitted: list[str] = []
    expressions, items = _static_watch_expressions(root)
    if expressions is not None and live_data is not None:
        identifiers = _source_identifiers(project_root)
        for item in items:
            value = (item.text or "").strip()
            if not value:
                continue
            root_name = _expression_root(value)
            if root_name and root_name not in identifiers and not _is_debugger_expression(value):
                expressions.remove(item)
                omitted.append(value)
            else:
                retained.append(value)
    # If the live source had to become the base because the current project
    # had no Static Watch pane, restore the selected timeline configuration.
    if trace_data is not None:
        trace_root = _parse_xml(trace_data, "C-Trace .dbgdt")
        source_pane = _pane(trace_root, "WIN_TIMELINE_GRAPH")
        target_pane = _pane(root, "WIN_TIMELINE_GRAPH")
        if source_pane is not None and target_pane is not None:
            for child in source_pane:
                if child.tag.startswith("Timeline"):
                    _replace_child(target_pane, child)
    return _serialize_xml(root), retained, omitted


def _merge_dnx(target_data: bytes | None, source_data: bytes) -> bytes:
    source_root = _parse_xml(source_data, "global .dnx")
    # Never seed device/flash/download settings from the global sample. If the
    # current project has no dnx yet, start with an empty settings document.
    target_root = _parse_xml(target_data, "current .dnx") if target_data else ET.Element("settings")
    for child in source_root:
        if child.tag in _CTRACE_ROOT_NODES:
            _replace_child(target_root, child)
    source_jlink = source_root.find("JLinkDriver")
    if source_jlink is not None:
        target_jlink = target_root.find("JLinkDriver")
        if target_jlink is None:
            target_jlink = ET.SubElement(target_root, "JLinkDriver")
        for child in source_jlink:
            if "trace" in child.tag.casefold() or "swo" in child.tag.casefold():
                _replace_child(target_jlink, child)
    return _serialize_xml(target_root)


def _active_project_stem(workspace: Path) -> str:
    settings_dir = workspace.parent / "settings"
    wsdt = settings_dir / "Project.wsdt"
    if wsdt.is_file():
        try:
            root = _parse_xml(wsdt.read_bytes(), wsdt.name)
            configured = root.findtext("./ConfigDictionary/CurrentConfigs/Project", "")
            if configured.strip():
                return configured.split("/", 1)[0].strip()
        except (IarSettingsError, OSError):
            pass
    try:
        text = _decode_settings_text(workspace.read_bytes(), workspace.name)
    except (OSError, IarSettingsError):
        text = ""
    references = re.findall(r"[^\"'<>\r\n]+\.ewp", text, re.IGNORECASE)
    if references:
        return Path(references[0].strip().replace("\\", "/")).stem
    projects = sorted(workspace.parent.glob("*.ewp"))
    return projects[0].stem if projects else workspace.stem


def _base_project_name(stem: str) -> str:
    return re.sub(
        r"(?:[_ .-](?:class[a-z0-9]+|debug|release))$", "", stem,
        flags=re.IGNORECASE,
    ) or stem


def _replace_setting_identity(text: str, bundle: IarSettingsBundle, workspace: Path) -> str:
    target_stem = _active_project_stem(workspace)
    target_base = _base_project_name(target_stem)
    if bundle.project_name and bundle.project_name != target_base:
        text = text.replace(bundle.project_name, target_base)
    source_workspace = bundle.source_workspace
    if source_workspace:
        source_root = Path(source_workspace).parent.parent
        target_root = workspace.parent.parent
        for old, new in (
            (str(source_root), str(target_root)),
            (str(source_root).replace("\\", "/"), str(target_root).replace("\\", "/")),
        ):
            text = text.replace(old, new)
    return text


def apply_settings_to_current_project(
    workspace_file: str | Path,
    live_bundle: IarSettingsBundle | None,
    ctrace_bundle: IarSettingsBundle | None,
    backup_before_apply: bool = True,
) -> IarSettingsRestoreResult:
    workspace = Path(workspace_file).expanduser().resolve(strict=True)
    if workspace.suffix.casefold() != ".eww":
        raise IarSettingsError("현재 프로젝트의 IAR 워크스페이스(.eww)를 선택하십시오.")
    if live_bundle is None and ctrace_bundle is None:
        raise IarSettingsError("적용할 Live Watch 또는 C-Trace 설정을 선택하십시오.")
    project_root = workspace.parent.parent if workspace.parent.name.casefold() in {"ewarm", "iar"} else workspace.parent
    settings_dir = workspace.parent / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    active_stem = _active_project_stem(workspace)
    project_name = _base_project_name(active_stem)
    result = IarSettingsRestoreResult()
    if backup_before_apply:
        if live_bundle is not None and (settings_dir / f"{active_stem}.dbgdt").is_file():
            result.backup_folders.append(str(create_settings_backup(workspace, project_name, "live_watch")))
        if ctrace_bundle is not None and (settings_dir / f"{active_stem}.dnx").is_file():
            result.backup_folders.append(str(create_settings_backup(workspace, project_name, "ctrace")))

    current = discover_settings_files(workspace)
    current_dbgdt = current.get(".dbgdt").read_bytes() if current.get(".dbgdt") else None
    live_dbgdt = _file_by_extension(live_bundle, ".dbgdt")
    trace_dbgdt = _file_by_extension(ctrace_bundle, ".dbgdt")
    outputs: dict[str, bytes] = {}
    dbgdt, retained, omitted = _merge_dbgdt(
        current_dbgdt,
        live_dbgdt[1] if live_dbgdt else None,
        trace_dbgdt[1] if trace_dbgdt else None,
        project_root,
    )
    outputs[f"{active_stem}.dbgdt"] = dbgdt
    result.retained_watch_expressions = retained
    result.omitted_watch_expressions = omitted
    if ctrace_bundle is not None:
        dnx_source = _file_by_extension(ctrace_bundle, ".dnx")
        if dnx_source is None:
            raise IarSettingsError("C-Trace 글로벌 설정에서 .dnx를 찾지 못했습니다.")
        current_dnx = current.get(".dnx").read_bytes() if current.get(".dnx") else None
        outputs[f"{active_stem}.dnx"] = _merge_dnx(current_dnx, dnx_source[1])
        crun_source = _file_by_extension(ctrace_bundle, ".crun")
        if crun_source is not None:
            outputs[f"{active_stem}.crun"] = crun_source[1]
        result.preserved_hardware_settings = current_dnx is not None
        # Project.wsdt is not overwritten: it is workspace/editor state, not a
        # Trace definition. It remains in backups and bundled samples only.

    prepared: dict[str, bytes] = {}
    for name, data in outputs.items():
        text = _decode_settings_text(data, name)
        # The Live Watch and C-Trace templates may originate from different
        # projects, so normalize both identities before writing the target.
        for source_bundle in (ctrace_bundle, live_bundle):
            if source_bundle is not None:
                text = _replace_setting_identity(text, source_bundle, workspace)
        encoded = text.encode("utf-8")
        _parse_xml(encoded, name)
        prepared[name] = encoded

    staging = Path(tempfile.mkdtemp(prefix=".cch-iar-settings-", dir=settings_dir.parent))
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for name, data in prepared.items():
            (staging / name).write_bytes(data)
        for name in prepared:
            destination = settings_dir / name
            old_copy: Path | None = None
            if destination.exists():
                old_copy = settings_dir / f".{name}.cch-old-{uuid.uuid4().hex[:8]}"
                destination.replace(old_copy)
            try:
                (staging / name).replace(destination)
            except BaseException:
                if old_copy is not None and old_copy.exists() and not destination.exists():
                    old_copy.replace(destination)
                raise
            replaced.append((destination, old_copy))
            result.written_files.append(str(destination))
        for _, old_copy in replaced:
            if old_copy is not None and old_copy.exists():
                old_copy.unlink()
    except BaseException as error:
        for destination, old_copy in reversed(replaced):
            if destination.exists():
                destination.unlink()
            if old_copy is not None and old_copy.exists():
                old_copy.replace(destination)
        raise IarSettingsError(
            "IAR 설정 적용에 실패하여 기존 설정을 복원했습니다. "
            "IAR가 실행 중이면 프로젝트를 닫고 다시 시도하십시오. " + str(error)
        ) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return result
