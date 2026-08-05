from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse


AT24C128_CAPACITY = 16 * 1024
AT24C128_PAGE_SIZE = 64
CATALOG_FILE = "eeprom_sources.json"
SKIP_DIRECTORIES = {
    ".git", ".svn", ".hg", ".vs", ".venv", "build", "dist",
    "node_modules", "cch_trace", "__pycache__",
}


@dataclass(slots=True)
class EepromSourceConfig:
    id: str
    display_name: str
    source_type: str = "github"
    repository_url: str = ""
    branch: str = "main"
    subdirectory: str = ""
    capacity: int = AT24C128_CAPACITY
    page_size: int = AT24C128_PAGE_SIZE
    auto_refresh: bool = True
    refresh_minutes: int = 5

    @classmethod
    def create(cls, display_name: str, **values) -> "EepromSourceConfig":
        return cls(id=uuid.uuid4().hex, display_name=display_name, **values)

    @classmethod
    def from_dict(cls, value: dict) -> "EepromSourceConfig":
        repository_url = str(value.get("repository_url") or "")
        source_type = str(value.get("source_type") or "").strip().lower()
        if source_type not in {"github", "local"}:
            source_type = "github" if repository_url.lower().startswith("https://github.com/") else "local"
        return cls(
            id=str(value.get("id") or uuid.uuid4().hex),
            display_name=str(value.get("display_name") or value.get("name") or "EEPROM 항목"),
            source_type=source_type,
            repository_url=repository_url,
            branch=str(value.get("branch") or "main"),
            # Since v1.3.2 every registered source is analyzed from its root.
            # Keep the field for backward-compatible settings decoding only.
            subdirectory="",
            capacity=max(1, int(value.get("capacity") or AT24C128_CAPACITY)),
            page_size=max(1, int(value.get("page_size") or AT24C128_PAGE_SIZE)),
            auto_refresh=bool(value.get("auto_refresh", True)),
            refresh_minutes=min(10, max(1, int(value.get("refresh_minutes") or 5))),
        )

    @property
    def is_local(self) -> bool:
        return self.source_type == "local" or not self.repository_url or not self.repository_url.lower().startswith("https://github.com/")


@dataclass(slots=True)
class StructField:
    name: str
    type_name: str
    count: int
    size: int


@dataclass(slots=True)
class StructInfo:
    name: str
    size: int
    fields: list[StructField]
    path: str
    line: int
    packed: bool = False
    declaration: str = ""


@dataclass(slots=True)
class EepromRegion:
    name: str
    address: int
    size: int
    page: int
    struct_name: str
    path: str
    lines: list[int]
    access: str
    confidence: str
    evidence: str
    payload_size: int = 0
    status: str = "정상"
    allocated: bool = True
    definition_present: bool = False
    actual_usage: bool = True
    conflict: bool = False
    out_of_range: bool = False

    @property
    def end_address(self) -> int:
        return self.address + max(0, self.size) - 1


@dataclass(slots=True)
class EepromMapResult:
    config: EepromSourceConfig
    source_root: str
    commit: str
    regions: list[EepromRegion]
    structures: list[StructInfo]
    warnings: list[str] = field(default_factory=list)
    used_bytes: int = 0

    @property
    def usage_percent(self) -> float:
        return min(100.0, self.used_bytes * 100.0 / max(1, self.config.capacity))


def default_catalog_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / CATALOG_FILE


def source_catalog_path() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    path = Path(__file__).resolve().parent / CATALOG_FILE
    return path if path.parent.is_dir() else None


def _decode_catalog(raw: object) -> list[EepromSourceConfig]:
    if not raw:
        return []
    try:
        value = json.loads(str(raw)) if isinstance(raw, str) else raw
        rows = value.get("items", []) if isinstance(value, dict) else value
        return [EepromSourceConfig.from_dict(item) for item in rows if isinstance(item, dict)]
    except (ValueError, TypeError, json.JSONDecodeError):
        return []


def load_source_configs(settings, current_root: str = "") -> list[EepromSourceConfig]:
    configured = _decode_catalog(settings.value("eeprom/sourceItems", ""))
    if configured:
        return configured
    try:
        defaults = _decode_catalog(default_catalog_path().read_text(encoding="utf-8"))
    except OSError:
        defaults = []
    if defaults:
        return defaults
    if current_root:
        root = Path(current_root)
        return [EepromSourceConfig.create(
            root.name or "현재 프로젝트",
            source_type="local",
            repository_url=str(root.resolve()),
        )]
    return []


def save_source_configs(settings, configs: Iterable[EepromSourceConfig], deploy_default: bool = False) -> None:
    items = list(configs)
    payload = {"schema": 2, "items": [asdict(item) for item in items]}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    settings.setValue("eeprom/sourceItems", encoded)
    settings.sync()
    if deploy_default:
        destination = source_catalog_path()
        if destination is None:
            raise OSError("설치된 프로그램에서는 배포 기본값 파일을 수정할 수 없습니다.")
        # Local paths are machine-specific and must never be shipped to other users.
        deploy_payload = {
            "schema": 2,
            "items": [asdict(item) for item in items if not item.is_local],
        }
        deploy_encoded = json.dumps(deploy_payload, ensure_ascii=False, indent=2)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(deploy_encoded + "\n", encoding="utf-8")
        temporary.replace(destination)


def parse_github_location(repository_url: str, configured_branch: str) -> tuple[str, str, str]:
    value = repository_url.strip()
    if not value:
        return "", configured_branch.strip() or "main", ""
    local = Path(value)
    if local.exists():
        return str(local.resolve()), configured_branch.strip() or "main", ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("GitHub HTTPS 주소 또는 접근 가능한 로컬 폴더만 사용할 수 있습니다.")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("GitHub 주소는 owner/repository 형식이어야 합니다.")
    owner, repository = parts[0], parts[1]
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
        raise ValueError("올바르지 않은 GitHub 저장소 주소입니다.")
    branch = configured_branch.strip() or "main"
    subpath = ""
    if len(parts) >= 4 and parts[2] == "tree":
        if not configured_branch.strip():
            branch = parts[3]
        # A /tree/ URL may still provide the branch, but its folder portion is
        # intentionally ignored: EEPROM analysis always searches the full repo.
    if branch.startswith("-") or ".." in branch or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch):
        raise ValueError("안전하지 않은 Git 브랜치 이름입니다.")
    return f"https://github.com/{owner}/{repository}.git", branch, subpath


def _run_git(arguments: list[str], cwd: Path | None = None, timeout: int = 120) -> str:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    startup = subprocess.STARTUPINFO() if os.name == "nt" else None
    if startup is not None:
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    process = subprocess.run(
        ["git", *arguments], cwd=str(cwd) if cwd else None, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, startupinfo=startup,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()
        raise RuntimeError(f"Git 명령 실패: {detail or process.returncode}")
    return process.stdout.strip()


def _local_source_revision(root: Path) -> str:
    """Create a cheap revision from C source metadata without reading every file."""
    digest = hashlib.sha256()
    for path in sorted(_source_files(root), key=lambda item: str(item).casefold()):
        try:
            stat = path.stat()
            relative = path.relative_to(root)
        except (OSError, ValueError):
            continue
        digest.update(str(relative).replace("\\", "/").encode("utf-8", errors="replace"))
        digest.update(f"\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode("ascii"))
    return f"local-{digest.hexdigest()}"


def synchronize_source(
    config: EepromSourceConfig,
    current_root: str,
    cache_root: str | Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[Path, str]:
    notify = progress or (lambda *_: None)
    clone_url, branch, url_subpath = parse_github_location(config.repository_url, config.branch)
    local_source = not clone_url or Path(clone_url).is_dir()
    if not clone_url:
        root = Path(current_root).resolve()
        if not root.is_dir():
            raise RuntimeError("현재 분석 프로젝트가 열려 있지 않습니다.")
        commit = ""
    elif Path(clone_url).is_dir():
        root = Path(clone_url).resolve()
        commit = ""
    else:
        cache = Path(cache_root).resolve()
        cache.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256(f"{clone_url}\n{branch}".encode()).hexdigest()[:20]
        root = cache / identity
        notify(0, 3, f"{config.display_name}: GitHub 브랜치 동기화 중…")
        if (root / ".git").is_dir():
            _run_git(["-C", str(root), "fetch", "--depth", "1", "origin", branch])
            _run_git(["-C", str(root), "checkout", "--detach", "FETCH_HEAD"])
        else:
            temporary = Path(tempfile.mkdtemp(prefix=f"{identity}-", dir=str(cache)))
            try:
                _run_git(["clone", "--depth", "1", "--branch", branch, "--single-branch", clone_url, str(temporary)])
                if root.exists():
                    shutil.rmtree(root)
                temporary.replace(root)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        commit = _run_git(["-C", str(root), "rev-parse", "HEAD"], timeout=15)
    if local_source:
        commit = _local_source_revision(root)
    notify(1, 3, f"{config.display_name}: C 소스 검색 중…")
    return root, commit


def source_revision(config: EepromSourceConfig, current_root: str) -> str:
    """Return the current source revision without modifying the cached checkout."""
    clone_url, branch, _ = parse_github_location(config.repository_url, config.branch)
    if not clone_url:
        root = Path(current_root).resolve()
        return _local_source_revision(root)
    if Path(clone_url).is_dir():
        root = Path(clone_url).resolve()
        return _local_source_revision(root)
    output = _run_git(["ls-remote", clone_url, f"refs/heads/{branch}"], timeout=30)
    revision = output.split(None, 1)[0] if output else ""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise RuntimeError(f"원격 브랜치를 찾을 수 없습니다: {branch}")
    return revision.lower()


def _mask_comments_and_strings(source: str) -> str:
    output = list(source)
    index = 0
    state = "code"
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "line"
                continue
            if char == "/" and nxt == "*":
                output[index] = output[index + 1] = " "
                index += 2
                state = "block"
                continue
            if char in {'"', "'"}:
                output[index] = " "
                state = char
            index += 1
            continue
        if state == "line":
            if char in "\r\n":
                state = "code"
            else:
                output[index] = " "
            index += 1
            continue
        if state == "block":
            if char == "*" and nxt == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "code"
            else:
                if char not in "\r\n":
                    output[index] = " "
                index += 1
            continue
        if char == "\\" and nxt:
            output[index] = output[index + 1] = " "
            index += 2
            continue
        if char == state:
            output[index] = " "
            state = "code"
        elif char not in "\r\n":
            output[index] = " "
        index += 1
    return "".join(output)


_CAST_RE = re.compile(
    r"\(\s*(?:const\s+|volatile\s+)*(?:u?int(?:8|16|32|64)_t|size_t|unsigned|signed|long|short|char|int)\s*\*?\s*\)"
)
_INTEGER_SUFFIX_RE = re.compile(r"(?i)(0x[0-9a-f]+|\b\d+)(?:u|l)+\b")


def _safe_integer(expression: str, values: dict[str, int]) -> int | None:
    cleaned = expression.split("//", 1)[0].strip()
    cleaned = _CAST_RE.sub("", cleaned)
    cleaned = _INTEGER_SUFFIX_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\bsizeof\s*\([^)]*\)", "0", cleaned)
    if not cleaned or "?" in cleaned:
        return None
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError:
        return None

    def evaluate(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return int(node.value)
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert)):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value if isinstance(node.op, ast.USub) else ~value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div, ast.Mod,
                      ast.LShift, ast.RShift, ast.BitOr, ast.BitAnd, ast.BitXor)
        ):
            left, right = evaluate(node.left), evaluate(node.right)
            operations = {
                ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
                ast.Mult: lambda: left * right, ast.FloorDiv: lambda: left // right,
                ast.Div: lambda: left // right, ast.Mod: lambda: left % right,
                ast.LShift: lambda: left << right, ast.RShift: lambda: left >> right,
                ast.BitOr: lambda: left | right, ast.BitAnd: lambda: left & right,
                ast.BitXor: lambda: left ^ right,
            }
            return operations[type(node.op)]()
        raise ValueError

    try:
        return int(evaluate(tree))
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in {".c", ".h"}:
            continue
        if any(part.casefold() in SKIP_DIRECTORIES for part in path.relative_to(root).parts[:-1]):
            continue
        files.append(path)
    return sorted(files, key=lambda value: str(value).casefold())


def _read_source(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _collect_macros(sources: list[tuple[Path, str]]) -> tuple[dict[str, str], dict[str, int], dict[str, tuple[Path, int]]]:
    expressions: dict[str, str] = {}
    origins: dict[str, tuple[Path, int]] = {}
    pattern = re.compile(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)\s+([^\r\n\\]+)")
    for path, source in sources:
        masked = _mask_comments_and_strings(source)
        for match in pattern.finditer(masked):
            name, expression = match.group(1), match.group(2).strip()
            if "(" in name or not expression:
                continue
            expressions[name] = expression
            origins[name] = (path, source.count("\n", 0, match.start()) + 1)
        for match in re.finditer(
            r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)\s*\([^\r\n)]*\)\s+[^\r\n\\]+",
            masked,
        ):
            origins.setdefault(
                match.group(1),
                (path, source.count("\n", 0, match.start()) + 1),
            )
    values: dict[str, int] = {}
    for _ in range(max(4, len(expressions) + 1)):
        changed = False
        for name, expression in expressions.items():
            if name in values:
                continue
            resolved = _safe_integer(expression, values)
            if resolved is not None:
                values[name] = resolved
                changed = True
        if not changed:
            break
    return expressions, values, origins


_TYPE_SIZES = {
    "char": 1, "signed char": 1, "unsigned char": 1, "int8_t": 1, "uint8_t": 1,
    "short": 2, "short int": 2, "unsigned short": 2, "unsigned short int": 2,
    "int16_t": 2, "uint16_t": 2, "int": 4, "unsigned": 4, "unsigned int": 4,
    "long": 4, "unsigned long": 4, "int32_t": 4, "uint32_t": 4, "float": 4,
    "double": 8, "int64_t": 8, "uint64_t": 8,
    "u8": 1, "s8": 1, "vu8": 1, "u16": 2, "s16": 2, "vu16": 2,
    "u32": 4, "s32": 4, "vu32": 4, "bool": 1,
}


def _normalized_type(value: str) -> str:
    value = re.sub(r"\b(const|volatile|static|register|__IO|__I|__O)\b", " ", value)
    value = re.sub(r"\b(struct|enum)\s+", "", value)
    return " ".join(value.replace("*", " * ").split())


def _balanced_aggregate_candidates(
    masked: str, original: str, path: Path,
) -> list[tuple[str, str, str, Path, int, bool, str]]:
    results: list[tuple[str, str, str, Path, int, bool, str]] = []
    pattern = re.compile(r"\btypedef\s+(?P<packed>__packed\s+)?(?P<kind>struct|union)(?:\s+[A-Za-z_]\w*)?\s*\{")
    for match in pattern.finditer(masked):
        opening = masked.find("{", match.start(), match.end())
        depth = 1
        index = opening + 1
        while index < len(masked) and depth:
            if masked[index] == "{":
                depth += 1
            elif masked[index] == "}":
                depth -= 1
            index += 1
        if depth:
            continue
        tail = masked[index:index + 160]
        alias = re.match(
            r"\s*(?P<packed>__attribute__\s*\(\(\s*packed\s*\)\)\s*)?(?P<name>[A-Za-z_]\w*)\s*;",
            tail,
        )
        if not alias:
            continue
        results.append((
            match.group("kind"), alias.group("name"), masked[opening + 1:index - 1],
            path, masked.count("\n", 0, match.start()) + 1,
            bool(match.group("packed") or alias.group("packed")),
            original[match.start():index + alias.end()],
        ))
    return results


def _parse_structures(sources: list[tuple[Path, str]], macro_values: dict[str, int]) -> list[StructInfo]:
    candidates: list[tuple[str, str, str, Path, int, bool, str]] = []
    for path, source in sources:
        masked = _mask_comments_and_strings(source)
        candidates.extend(_balanced_aggregate_candidates(masked, source, path))
    known = dict(_TYPE_SIZES)
    results: dict[str, StructInfo] = {}
    pending = list(candidates)
    for _ in range(max(2, len(pending) + 1)):
        next_pending = []
        changed = False
        for kind, name, body, path, line, packed, declaration in pending:
            fields: list[StructField] = []
            offset = 0
            union_size = 0
            maximum_alignment = 1
            valid = True
            for statement in body.split(";"):
                statement = re.sub(r"(?m)^\s*#.*$", "", statement).strip()
                if not statement or "(" in statement or statement.startswith(("struct {", "union {")):
                    continue
                if statement == "}" or statement.startswith("}"):
                    continue
                was_bitfield = bool(re.search(r":\s*\d+\s*$", statement))
                statement = re.sub(r"\s*:\s*\d+\s*$", "", statement)
                match = re.match(
                    r"(?P<type>(?:[A-Za-z_]\w*\s+)*[A-Za-z_]\w*(?:\s*\*)?)\s+"
                    r"(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*(?P<count>[^\]]+)\s*\])?$",
                    statement,
                )
                if not match and was_bitfield:
                    # Anonymous union/struct bitfield members are represented by
                    # their scalar value member and do not change the aggregate size.
                    continue
                if not match:
                    valid = False
                    break
                type_name = _normalized_type(match.group("type"))
                base_size = 4 if "*" in type_name else known.get(type_name.replace(" *", ""))
                if base_size is None:
                    if was_bitfield:
                        continue
                    valid = False
                    break
                count = 1
                if match.group("count"):
                    count_value = _safe_integer(match.group("count"), macro_values)
                    if count_value is None or count_value < 0:
                        valid = False
                        break
                    count = count_value
                alignment = 1 if packed else min(base_size, 4)
                if offset % alignment:
                    offset += alignment - (offset % alignment)
                field_size = base_size * count
                fields.append(StructField(match.group("name"), type_name, count, field_size))
                if kind == "union":
                    union_size = max(union_size, field_size)
                else:
                    offset += field_size
                maximum_alignment = max(maximum_alignment, alignment)
            if not valid:
                next_pending.append((kind, name, body, path, line, packed, declaration))
                continue
            if kind == "union":
                offset = union_size
            if not packed and offset % maximum_alignment:
                offset += maximum_alignment - (offset % maximum_alignment)
            result = StructInfo(name, offset, fields, str(path), line, packed, declaration.strip())
            results[name] = result
            known[name] = offset
            changed = True
        pending = next_pending
        if not changed:
            break
    return sorted(results.values(), key=lambda item: (item.path.casefold(), item.line, item.name.casefold()))


def _split_arguments(value: str) -> list[str]:
    arguments: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char in "([{" :
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            arguments.append(value[start:index].strip())
            start = index + 1
    arguments.append(value[start:].strip())
    return arguments


def _call_expressions(masked: str) -> Iterable[tuple[str, str, int]]:
    matcher = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
    for match in matcher.finditer(masked):
        name = match.group(1)
        lowered = name.casefold()
        driver_name = "eeprom" in lowered or "at24" in lowered or "24c" in lowered or lowered.startswith("ee_")
        data_access = any(word in lowered for word in ("read", "write", "save", "load", "store", "program", "byte_buffer"))
        if not (driver_name and data_access):
            continue
        depth = 1
        index = match.end()
        while index < len(masked) and depth:
            if masked[index] == "(":
                depth += 1
            elif masked[index] == ")":
                depth -= 1
            index += 1
        if depth == 0:
            yield name, masked[match.end():index - 1], match.start()


def _base_symbol(name: str) -> str:
    value = name.upper()
    value = re.sub(r"^(AT24C?\d*_|EEPROM_|EE_)", "", value)
    value = re.sub(r"_(ADDR(?:ESS)?|OFFSET|PAGE(?:_NO|_NUM|_INDEX)?|SIZE|LEN(?:GTH)?)$", "", value)
    return value


def _is_storage_address_symbol(name: str) -> bool:
    """Accept EEPROM memory addresses but reject I2C/device addresses."""
    upper = name.upper()
    if re.search(r"I2C|DEVICE|DEV_|SLAVE|BASE_ADDR|CHIP_ADDR", upper):
        return False
    return bool(re.search(r"ADDR|ADDRESS|OFFSET|PAGE", upper))


def _is_storage_access_function(name: str) -> bool:
    """Reject readiness/initialisation and board transport helper calls."""
    lowered = name.casefold()
    if any(word in lowered for word in (
        "isdeviceready", "is_device_ready", "ready", "init", "deinit",
        "get_pg_size", "get_page_size", "get_max_addr", "add_dev", "get_dev",
    )):
        return False
    if re.search(r"(?:^|_)io_(?:read|write)", lowered):
        return False
    return any(word in lowered for word in (
        "read", "write", "save", "load", "store", "program", "byte_buffer",
    ))


def _resolve_size(
    argument: str, macro_values: dict[str, int], structures: dict[str, StructInfo],
    variable_types: dict[str, str], variable_sizes: dict[str, int],
) -> tuple[int | None, str]:
    match = re.search(r"\bsizeof\s*\(\s*(?:struct\s+)?([A-Za-z_]\w*)\s*\)", argument)
    if match:
        symbol = match.group(1)
        if symbol in structures:
            structure = structures[symbol]
            return structure.size, structure.name
        if symbol in variable_sizes:
            return variable_sizes[symbol], variable_types.get(symbol, "")
    value = _safe_integer(argument, macro_values)
    return value, ""


def _infer_regions(
    sources: list[tuple[Path, str]], config: EepromSourceConfig,
    expressions: dict[str, str], macro_values: dict[str, int],
    origins: dict[str, tuple[Path, int]], structures: list[StructInfo],
) -> list[EepromRegion]:
    structure_map = {item.name: item for item in structures}
    regions: list[EepromRegion] = []
    known_sizes = {**_TYPE_SIZES, **{name: item.size for name, item in structure_map.items()}}
    global_variable_types: dict[str, str] = {}
    global_variable_sizes: dict[str, int] = {}
    declaration_pattern = re.compile(
        r"\b(?:extern\s+|static\s+|volatile\s+|const\s+)*([A-Za-z_]\w*)\s+"
        r"([A-Za-z_]\w*)\s*(?:\[\s*([^\]]+)\s*\])?\s*[;=]"
    )
    for _, source in sources:
        masked_source = _mask_comments_and_strings(source)
        for match in declaration_pattern.finditer(masked_source):
            type_name, variable = match.group(1), match.group(2)
            if type_name not in known_sizes:
                continue
            count = _safe_integer(match.group(3), macro_values) if match.group(3) else 1
            if count is None:
                continue
            global_variable_types[variable] = type_name if type_name in structure_map else ""
            global_variable_sizes[variable] = known_sizes[type_name] * count
    for path, source in sources:
        masked = _mask_comments_and_strings(source)
        variable_types = dict(global_variable_types)
        variable_sizes = dict(global_variable_sizes)
        for match in re.finditer(
            r"\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:\[\s*([^\]]+)\s*\])?\s*[;=]", masked
        ):
            type_name, variable = match.group(1), match.group(2)
            if type_name not in known_sizes:
                continue
            count = _safe_integer(match.group(3), macro_values) if match.group(3) else 1
            if count is None:
                continue
            variable_types[variable] = type_name if type_name in structure_map else ""
            variable_sizes[variable] = known_sizes[type_name] * count
        for function, raw_arguments, start in _call_expressions(masked):
            if not _is_storage_access_function(function):
                continue
            arguments = _split_arguments(raw_arguments)
            address = None
            address_name = ""
            address_index = -1
            known_address_index = (
                0 if function in {"EEPROM_Read", "EEPROM_Write"}
                else 2 if "byte_buffer" in function.casefold() and ("at24" in function.casefold() or "24c" in function.casefold())
                else -1
            )
            if 0 <= known_address_index < len(arguments):
                direct_address = _safe_integer(arguments[known_address_index], macro_values)
                if direct_address is not None:
                    address = direct_address
                    address_index = known_address_index
                    address_name = next(
                        (
                            name for name in re.findall(r"\b[A-Za-z_]\w*\b", arguments[known_address_index])
                            if name in origins and _is_storage_address_symbol(name)
                        ),
                        f"0x{direct_address:04X}",
                    )
            for index, argument in enumerate(arguments):
                if address is not None:
                    break
                function_macro = re.fullmatch(
                    r"\s*([A-Za-z_]\w*)\s*\(\s*([^()]*)\s*\)\s*",
                    argument,
                )
                if function_macro and _is_storage_address_symbol(function_macro.group(1)):
                    page_value = _safe_integer(function_macro.group(2), macro_values)
                    if page_value is not None:
                        address_name = function_macro.group(1)
                        address = page_value * config.page_size
                        address_index = index
                        break
                names = re.findall(r"\b[A-Za-z_]\w*\b", argument)
                address_symbols = [
                    name for name in names
                    if name in macro_values
                    and _is_storage_address_symbol(name)
                    and not re.search(r"SIZE|COUNT|MAX|LIMIT", name, re.I)
                ]
                resolved = _safe_integer(argument, macro_values)
                if address_symbols:
                    address_name = address_symbols[0]
                    if resolved is None:
                        # A dynamic expression such as PAGE8 + page * 2 cannot
                        # safely be collapsed to the base macro address.
                        continue
                    address = resolved
                    address_index = index
                    break
            if address is None:
                continue
            is_page = bool(re.search(r"PAGE", address_name, re.I) and not re.search(r"ADDR|OFFSET|SIZE", address_name, re.I))
            if is_page:
                address *= config.page_size
            size = None
            struct_name = ""
            preferred_size_index = -1
            if function in {"EEPROM_Read", "EEPROM_Write"} and len(arguments) > 2:
                preferred_size_index = 2
            elif ("at24" in function.casefold() or "24c" in function.casefold()):
                if "byte_buffer" in function.casefold() and address_index + 1 < len(arguments):
                    preferred_size_index = address_index + 1
            if preferred_size_index >= 0:
                size, struct_name = _resolve_size(
                    arguments[preferred_size_index], macro_values, structure_map,
                    variable_types, variable_sizes,
                )
            for index, argument in enumerate(arguments):
                if index == address_index:
                    continue
                candidate, candidate_struct = _resolve_size(
                    argument, macro_values, structure_map, variable_types, variable_sizes
                )
                if candidate_struct and size is None:
                    size, struct_name = candidate, candidate_struct
                    break
                variable_match = re.fullmatch(r"\s*&?\s*([A-Za-z_]\w*)\s*", argument)
                if size is None and variable_match and variable_types.get(variable_match.group(1)):
                    struct_name = variable_types[variable_match.group(1)]
                    size = structure_map[struct_name].size
            # Do not guess from an arbitrary numeric argument.  In HAL/BSP
            # calls it is commonly a timeout or retry count (for example 300).
            if size is None:
                base = _base_symbol(address_name)
                matched = next((item for item in structures if _base_symbol(item.name) == base), None)
                if matched:
                    size, struct_name = matched.size, matched.name
            payload_size = size or config.page_size
            lowered = function.casefold()
            wrapper_uses_full_page = function in {"EEPROM_Read", "EEPROM_Write"}
            size = config.page_size if wrapper_uses_full_page else payload_size
            access = "쓰기" if any(word in lowered for word in ("write", "save", "store", "program")) else "읽기" if any(word in lowered for word in ("read", "load", "get")) else "접근"
            line = source.count("\n", 0, start) + 1
            regions.append(EepromRegion(
                address_name or function, address, size, address // config.page_size,
                struct_name, str(path), [line], access,
                "높음" if struct_name else "중간",
                f"{function} 호출의 {address_name or '주소 인자'}",
                payload_size,
                definition_present=address_name in origins,
                actual_usage=True,
            ))

    # Address/page macros not referenced by a recognizable driver call still
    # describe legitimate layouts in many embedded projects. Keep them as
    # lower-confidence records instead of silently losing the allocation.
    used_symbols = {region.name for region in regions}
    for name, value in macro_values.items():
        upper = name.upper()
        layout_macro = bool(
            re.search(r"EEPROM.*(?:ADDR_)?PAGE\d+$", upper)
            or re.search(r"(?:EEPROM|^EE_).*(?:OFFSET|START_ADDR|START_ADDRESS)$", upper)
        )
        if not layout_macro:
            continue
        is_address = bool(re.search(r"(?:^|_)(?:ADDR(?:ESS)?|OFFSET)$", upper))
        is_page = bool(re.search(r"PAGE\d+$", upper) and "ADDR_PAGE" not in upper)
        if "ADDR_PAGE" in upper:
            is_address = True
        if name in used_symbols or not (is_address or is_page):
            continue
        if any(token in upper for token in ("PAGE_SIZE", "PAGE_COUNT", "MAX_PAGE", "LAST_PAGE")):
            continue
        address = value * config.page_size if is_page else value
        if address < 0 or address >= config.capacity * 4:
            continue
        base = _base_symbol(name)
        size = None
        for suffix in ("_SIZE", "_LEN", "_LENGTH"):
            for prefix in (base, f"EEPROM_{base}", f"EE_{base}"):
                if prefix + suffix in macro_values:
                    size = macro_values[prefix + suffix]
                    break
            if size is not None:
                break
        matched = next((item for item in structures if _base_symbol(item.name) == base), None)
        struct_name = matched.name if matched else ""
        size = size or (matched.size if matched else config.page_size)
        origin, line = origins.get(name, (Path(""), 0))
        existing = next(
            (item for item in regions if item.allocated and item.address == address),
            None,
        )
        if existing is not None:
            existing.definition_present = True
            continue
        regions.append(EepromRegion(
            name, address, size, address // config.page_size, struct_name,
            str(origin), [line], "정의", "낮음", "주소/페이지 매크로에서 추정",
            size, "정의만 존재", False, True, False,
        ))

    merged: dict[tuple[int, int, str, bool], EepromRegion] = {}
    for region in regions:
        key = (region.address, region.size, region.struct_name, region.allocated)
        existing = merged.get(key)
        if existing:
            existing.lines = sorted(set(existing.lines + region.lines))
            if region.access not in existing.access:
                existing.access = f"{existing.access}/{region.access}"
            existing.definition_present = existing.definition_present or region.definition_present
            existing.actual_usage = existing.actual_usage or region.actual_usage
        else:
            merged[key] = region
    return sorted(merged.values(), key=lambda item: (item.address, item.size, item.name.casefold()))


def _apply_region_status(regions: list[EepromRegion], capacity: int) -> tuple[list[str], int]:
    warnings: list[str] = []
    intervals: list[tuple[int, int]] = []
    for region in regions:
        if not region.allocated:
            continue
        if region.size <= 0:
            region.status = "크기 확인 필요"
            warnings.append(f"{region.name}: 크기가 0 이하입니다.")
            continue
        start, end = region.address, region.address + region.size
        if start < 0 or end > capacity:
            region.status = "용량 초과"
            region.out_of_range = True
            warnings.append(f"{region.name}: 0x{start:04X}~0x{end - 1:04X}가 EEPROM 용량을 벗어납니다.")
        for previous in regions:
            if previous is region or previous.address > region.address:
                break
            if not previous.allocated:
                continue
            previous_end = previous.address + max(0, previous.size)
            if previous_end > start and previous.address < end:
                if (
                    previous.address == start
                    and previous.size == region.size
                    and previous.struct_name == region.struct_name
                ):
                    continue
                region.status = "확정 충돌"
                region.conflict = True
                previous.status = "확정 충돌"
                previous.conflict = True
                warnings.append(f"{previous.name} ↔ {region.name}: EEPROM 주소 범위가 겹칩니다.")
        clipped_start, clipped_end = max(0, start), min(capacity, end)
        if clipped_end > clipped_start:
            intervals.append((clipped_start, clipped_end))
    intervals.sort()
    used = 0
    current_start = current_end = -1
    for start, end in intervals:
        if current_start < 0:
            current_start, current_end = start, end
        elif start <= current_end:
            current_end = max(current_end, end)
        else:
            used += current_end - current_start
            current_start, current_end = start, end
    if current_start >= 0:
        used += current_end - current_start
    return list(dict.fromkeys(warnings)), used


def analyze_eeprom_source(
    config: EepromSourceConfig,
    current_root: str,
    cache_root: str | Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> EepromMapResult:
    notify = progress or (lambda *_: None)
    root, commit = synchronize_source(config, current_root, cache_root, notify)
    paths = _source_files(root)
    sources: list[tuple[Path, str]] = []
    total = max(1, len(paths))
    for index, path in enumerate(paths, 1):
        sources.append((path, _read_source(path)))
        if index == total or index % 50 == 0:
            notify(index, total, f"{config.display_name}: {index:,}/{total:,}개 파일 읽는 중…")
    expressions, macro_values, origins = _collect_macros(sources)
    notify(2, 3, f"{config.display_name}: 구조체와 EEPROM 영역 연결 중…")
    structures = _parse_structures(sources, macro_values)
    regions = _infer_regions(sources, config, expressions, macro_values, origins, structures)
    warnings, used = _apply_region_status(regions, config.capacity)
    related_names = {
        item.struct_name for item in regions if item.struct_name
    } | {
        item.name for item in structures
        if "eeprom" in item.name.casefold() or item.name.casefold().startswith("ee")
    }
    changed = True
    while changed:
        changed = False
        for structure in structures:
            if structure.name not in related_names:
                continue
            for field in structure.fields:
                dependency = field.type_name.replace(" *", "")
                if dependency not in related_names and any(item.name == dependency for item in structures):
                    related_names.add(dependency)
                    changed = True
    related_structures = [item for item in structures if item.name in related_names]
    if not paths:
        warnings.append("분석 범위에 .c/.h 파일이 없습니다.")
    if not regions:
        warnings.append("인식 가능한 EEPROM 주소/페이지 정의 또는 AT24C128 호출을 찾지 못했습니다.")
    notify(3, 3, f"{config.display_name}: EEPROM 맵 분석 완료")
    return EepromMapResult(config, str(root), commit, regions, related_structures, warnings, used)
