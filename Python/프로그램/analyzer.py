from __future__ import annotations

import bisect
import json
import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from clang import cindex
from tree_sitter import Language, Node, Parser, Query, QueryCursor
import tree_sitter_c


CONTROL_WORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "do", "case",
    "else", "typedef", "defined", "alignof", "_Alignof", "_Generic",
    "_Static_assert", "__attribute__", "__declspec",
}
EXCLUDED_DIRS = {".git", ".svn", ".hg", "node_modules", "dist", "build", ".vs", "cch_trace"}
FUNCTION_RE = re.compile(r"([A-Za-z_]\w*)\s*\((?:[^;{}()]|\([^()]*\))*\)\s*\{")
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
FUNCTION_MACRO_RE = re.compile(
    r"(?m)^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]*\("
)
C_LANGUAGE = Language(tree_sitter_c.language())
FUNCTION_QUERY = Query(C_LANGUAGE, "(function_definition) @function")
CALL_QUERY = Query(C_LANGUAGE, "(call_expression) @call")
_PARSER_LOCAL = threading.local()
MAX_VIEW_ROWS = 250_000


@dataclass(slots=True)
class CallRef:
    name: str
    target_id: str | None
    line: int


@dataclass(slots=True)
class FunctionDef:
    id: str
    name: str
    path: str
    file: str
    declaration: str
    parameters: str
    start_index: int
    body_start: int
    body_end: int
    start_line: int
    end_line: int
    calls: list[CallRef] = field(default_factory=list)
    callers: set[str] = field(default_factory=set)


@dataclass(slots=True)
class ParsedFile:
    path: str
    relative_path: str
    text: str
    functions: list[FunctionDef]
    modified_ns: int
    file_size: int


@dataclass(slots=True)
class AnalysisResult:
    root: str
    files: list[ParsedFile]
    functions: list[FunctionDef]
    by_id: dict[str, FunctionDef]
    by_name: dict[str, list[FunctionDef]]
    unresolved_count: int
    clang_files: int = 0
    compile_database: str = ""

    def function(self, function_id: str | None) -> FunctionDef | None:
        return self.by_id.get(function_id or "")


@dataclass(slots=True)
class ViewRow:
    kind: str
    depth: int = 0
    name: str = ""
    function_id: str | None = None
    file: str = ""
    line: int = 0
    state: str = "normal"
    title: str = ""
    path_names: tuple[str, ...] = ()
    node_key: str = ""
    parent_key: str = ""
    call_file: str = ""
    call_lines: tuple[int, ...] = ()


@dataclass(slots=True)
class CallView:
    rows: list[ViewRow]
    max_depth: int
    main_candidates: list[FunctionDef]
    selected_main_id: str | None
    interrupt_roots: int
    runtime_roots: int = 0


def _norm(path: str) -> str:
    return str(Path(path)).replace("/", "\\").lower()


def _relative(path: str, root: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return path


def _line_starts(text: str) -> list[int]:
    return [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]


def _line_number(starts: list[int], index: int) -> int:
    return bisect.bisect_right(starts, index)


def mask_non_code(source: str) -> str:
    out: list[str] = []
    index = 0
    state = "code"
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        if state == "code":
            if char == "/" and nxt == "/":
                out.extend((" ", " "))
                index += 2
                state = "line_comment"
                continue
            if char == "/" and nxt == "*":
                out.extend((" ", " "))
                index += 2
                state = "block_comment"
                continue
            if char == '"':
                out.append(" ")
                index += 1
                state = "string"
                continue
            if char == "'":
                out.append(" ")
                index += 1
                state = "char"
                continue
            out.append(char)
            index += 1
            continue
        if state == "line_comment":
            if char in "\r\n":
                out.append(char)
                state = "code"
            else:
                out.append(" ")
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                out.extend((" ", " "))
                index += 2
                state = "code"
                continue
            out.append(char if char in "\r\n" else " ")
            index += 1
            continue
        if char == "\\" and nxt:
            out.extend((" ", nxt if nxt in "\r\n" else " "))
            index += 2
            continue
        if (state == "string" and char == '"') or (state == "char" and char == "'"):
            out.append(" ")
            index += 1
            state = "code"
            continue
        out.append(char if char in "\r\n" else " ")
        index += 1
    return "".join(out)


def _find_matching(text: str, open_index: int, open_char: str, close_char: str) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _previous_boundary(text: str, index: int) -> int:
    for position in range(index - 1, -1, -1):
        if text[position] in ";}{":
            return position + 1
    return 0


def _linear_declaration_start(masked: str, name_index: int) -> int:
    """Skip comments and preprocessor guards preceding a linear-parser definition."""
    boundary = _previous_boundary(masked, name_index)
    position = boundary
    for line in masked[boundary:name_index].splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            position += len(line)
            continue
        break
    return position


def _is_function_macro_definition(masked: str, name_index: int) -> bool:
    line_start = masked.rfind("\n", 0, name_index) + 1
    prefix = masked[line_start:name_index]
    return bool(re.match(r"^[ \t]*#[ \t]*define\b", prefix))


def _deduplicate_functions(functions: list[FunctionDef]) -> list[FunctionDef]:
    """C cannot overload functions; keep one definition per file/name boundary."""
    unique: list[FunctionDef] = []
    by_name: dict[str, FunctionDef] = {}
    for function in sorted(functions, key=lambda item: item.start_index):
        existing = by_name.get(function.name)
        if existing is None:
            by_name[function.name] = function
            unique.append(function)
            continue
        seen_calls = {(call.name, call.line) for call in existing.calls}
        existing.calls.extend(
            call for call in function.calls
            if (call.name, call.line) not in seen_calls
        )
    return unique


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_declaration(text: str) -> str:
    """Return a compact C declaration without line or block comments."""
    return _compact(mask_non_code(text))


def read_source(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp949", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _mask_large_initializers(source: bytes) -> bytes:
    """Blank huge array/table initializer contents while preserving byte offsets."""
    if len(source) < 512 * 1024:
        return source
    output = bytearray(source)
    position = 0
    pattern = re.compile(rb"=\s*\{")
    while True:
        match = pattern.search(source, position)
        if not match:
            break
        opening = match.end() - 1
        index = opening + 1
        depth = 1
        state = "code"
        while index < len(source) and depth:
            byte = source[index]
            nxt = source[index + 1] if index + 1 < len(source) else 0
            if state == "code":
                if byte == 47 and nxt == 47:
                    state = "line_comment"
                    index += 2
                    continue
                if byte == 47 and nxt == 42:
                    state = "block_comment"
                    index += 2
                    continue
                if byte == 34:
                    state = "string"
                elif byte == 39:
                    state = "char"
                elif byte == 123:
                    depth += 1
                elif byte == 125:
                    depth -= 1
            elif state == "line_comment":
                if byte in (10, 13):
                    state = "code"
            elif state == "block_comment":
                if byte == 42 and nxt == 47:
                    state = "code"
                    index += 2
                    continue
            elif byte == 92:
                index += 2
                continue
            elif (state == "string" and byte == 34) or (state == "char" and byte == 39):
                state = "code"
            index += 1
        if depth == 0:
            closing = index - 1
            for blank in range(opening + 1, closing):
                if output[blank] not in (10, 13):
                    output[blank] = 32
            position = closing + 1
        else:
            position = opening + 1
    return bytes(output)


def _parser() -> Parser:
    parser = getattr(_PARSER_LOCAL, "parser", None)
    if parser is None:
        parser = Parser(C_LANGUAGE)
        _PARSER_LOCAL.parser = parser
    return parser


def _walk_nodes(root: Node) -> Iterable[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _node_text(source: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _declarator_name(source: bytes, declarator: Node | None) -> tuple[str, Node | None]:
    if declarator is None:
        return "", None
    for node in _walk_nodes(declarator):
        if node.type == "identifier":
            return _node_text(source, node), node
    return "", None


def _call_expression_name(source: bytes, call: Node) -> str:
    function_node = call.child_by_field_name("function")
    if function_node is None:
        return ""
    if function_node.type == "identifier":
        return _node_text(source, function_node)
    identifiers = [node for node in _walk_nodes(function_node) if node.type in {"identifier", "field_identifier"}]
    return _node_text(source, identifiers[-1]) if identifiers else ""


def _parse_large_file_linear(path: str, root: str, text: str, modified_ns: int, file_size: int) -> ParsedFile:
    """Linear fallback for large preprocessor-heavy files pathological to a full AST."""
    masked = mask_non_code(text)
    starts = _line_starts(text)
    functions: list[FunctionDef] = []
    covered_until = -1
    for match in FUNCTION_RE.finditer(masked):
        name = match.group(1)
        if (
            name in CONTROL_WORDS
            or match.start(1) <= covered_until
            or _is_function_macro_definition(masked, match.start(1))
        ):
            continue
        body_start = match.end() - 1
        body_end = _find_matching(masked, body_start, "{", "}")
        if body_end < 0:
            continue
        declaration_start = _linear_declaration_start(masked, match.start(1))
        if re.search(r"(?m)^[ \t]*#[ \t]*define\b", masked[declaration_start:body_start]):
            continue
        open_parenthesis = masked.find("(", match.start(1) + len(name), body_start)
        close_parenthesis = _find_matching(masked, open_parenthesis, "(", ")") if open_parenthesis >= 0 else -1
        function = FunctionDef(
            id=f"{_norm(path)}|{name}|{match.start(1)}",
            name=name,
            path=path,
            file=Path(path).name,
            declaration=_clean_declaration(text[declaration_start:body_start]),
            parameters=text[open_parenthesis + 1:close_parenthesis].strip() if close_parenthesis >= 0 else "",
            start_index=match.start(1),
            body_start=body_start,
            body_end=body_end,
            start_line=_line_number(starts, match.start(1)),
            end_line=_line_number(starts, body_end),
        )
        for call_match in CALL_RE.finditer(masked, body_start + 1, body_end):
            call_name = call_match.group(1)
            if call_name in CONTROL_WORDS:
                continue
            function.calls.append(CallRef(call_name, None, _line_number(starts, call_match.start(1))))
        functions.append(function)
        covered_until = body_end
    return ParsedFile(
        path=path,
        relative_path=_relative(path, root),
        text=text,
        functions=_deduplicate_functions(functions),
        modified_ns=modified_ns,
        file_size=file_size,
    )


def parse_file(
    path: str,
    root: str,
    modified_ns: int | None = None,
    file_size: int | None = None,
) -> ParsedFile:
    file_path = Path(path)
    text = read_source(file_path)
    source_bytes = text.encode("utf-8")
    stat = file_path.stat()
    stat_ns = modified_ns if modified_ns is not None else stat.st_mtime_ns
    stat_size = file_size if file_size is not None else stat.st_size
    if len(source_bytes) >= 64 * 1024:
        return _parse_large_file_linear(str(file_path), root, text, stat_ns, stat_size)
    tree = _parser().parse(_mask_large_initializers(source_bytes))
    # Tree-sitter already excludes comments and strings from syntax captures.
    # Avoid additional full-file Python character scans on large generated headers.
    starts = [0] + [index + 1 for index, byte in enumerate(source_bytes) if byte == 10]
    functions: list[FunctionDef] = []
    function_nodes = QueryCursor(FUNCTION_QUERY).captures(tree.root_node).get("function", [])
    for node in function_nodes:
        declarator = node.child_by_field_name("declarator")
        body = node.child_by_field_name("body")
        if declarator is None or body is None:
            continue
        node_start = node.start_byte
        node_end = node.end_byte
        body_start = body.start_byte
        body_end = body.end_byte
        start_line = _line_number(starts, node_start)
        end_line = _line_number(starts, node_end)
        declarator_text = _node_text(source_bytes, declarator)
        name_match = re.search(r"([A-Za-z_]\w*)\s*\(", declarator_text)
        name = name_match.group(1) if name_match else ""
        if not name or name in CONTROL_WORDS:
            continue
        open_parenthesis = declarator_text.find("(", name_match.start(1) + len(name))
        close_parenthesis = _find_matching(declarator_text, open_parenthesis, "(", ")")
        parameters = declarator_text[open_parenthesis + 1:close_parenthesis] if close_parenthesis >= 0 else ""
        declaration = _clean_declaration(
            source_bytes[node_start:body_start].decode("utf-8", errors="replace")
        )
        function_id = f"{_norm(path)}|{name}|{node_start}"
        functions.append(FunctionDef(
            id=function_id,
            name=name,
            path=str(file_path),
            file=file_path.name,
            declaration=declaration,
            parameters=parameters,
            start_index=node_start,
            body_start=body_start,
            body_end=body_end,
            start_line=start_line,
            end_line=end_line,
        ))
    call_nodes = sorted(
        QueryCursor(CALL_QUERY).captures(tree.root_node).get("call", []),
        key=lambda node: node.start_byte,
    )
    call_starts = [node.start_byte for node in call_nodes]
    for function in functions:
        first_call = bisect.bisect_left(call_starts, function.body_start)
        last_call = bisect.bisect_right(call_starts, function.body_end)
        for call_node in call_nodes[first_call:last_call]:
            name = _call_expression_name(source_bytes, call_node)
            if not name or name in CONTROL_WORDS:
                continue
            function.calls.append(CallRef(name=name, target_id=None, line=_line_number(starts, call_node.start_byte)))
    # py-tree-sitter Nodes borrow their Tree storage. Release every captured Node
    # before the local Tree so native cleanup order cannot dereference a dead tree.
    node = declarator = body = call_node = None
    call_nodes.clear()
    function_nodes.clear()
    # 변경 파일은 항상 새로 파싱하므로 Tree-sitter 원시 트리를 캐시에 보관할
    # 필요가 없다. 대형 프로젝트에서 수천 개의 native Tree가 남는 것을 막는다.
    tree = None
    return ParsedFile(
        path=str(file_path),
        relative_path=_relative(str(file_path), root),
        text=text,
        functions=_deduplicate_functions(functions),
        modified_ns=stat_ns,
        file_size=stat_size,
    )


def _target_affinity(caller_path: str, target_path: str) -> int:
    caller = _norm(caller_path).split("\\")
    target = _norm(target_path).split("\\")
    limit = min(len(caller) - 1, len(target) - 1)
    common = 0
    while common < limit and caller[common] == target[common]:
        common += 1
    return common * 1000 - ((len(caller) - common) + (len(target) - common)) * 10


def _best_target(caller: FunctionDef, targets: list[FunctionDef]) -> FunctionDef | None:
    if not targets:
        return None
    caller_path = _norm(caller.path)
    for target in targets:
        if _norm(target.path) == caller_path:
            return target
    return max(targets, key=lambda target: _target_affinity(caller.path, target.path))


def link_analysis(
    files: Iterable[ParsedFile],
    root: str,
    exclude_macro_functions: bool = True,
) -> AnalysisResult:
    parsed_files = sorted(files, key=lambda item: _norm(item.path))
    functions = [function for parsed in parsed_files for function in parsed.functions]
    macro_names = {
        match.group(1)
        for parsed in parsed_files
        for match in FUNCTION_MACRO_RE.finditer(parsed.text)
    }
    by_id = {function.id: function for function in functions}
    by_name: dict[str, list[FunctionDef]] = {}
    raw_calls = {function.id: list(function.calls) for function in functions}
    if not exclude_macro_functions and macro_names:
        parsed_by_path = {_norm(parsed.path): parsed for parsed in parsed_files}
        for function in functions:
            parsed = parsed_by_path.get(_norm(function.path))
            if parsed is None:
                continue
            source_lines = mask_non_code(parsed.text).splitlines()
            body_lines = source_lines[max(0, function.start_line - 1):function.end_line]
            known = {(call.name, call.line) for call in raw_calls[function.id]}
            for offset, line in enumerate(body_lines, function.start_line):
                for match in CALL_RE.finditer(line):
                    name = match.group(1)
                    if name in macro_names and (name, offset) not in known:
                        raw_calls[function.id].append(CallRef(name=name, target_id=None, line=offset))
                        known.add((name, offset))
    for function in functions:
        function.calls = []
        function.callers = set()
        by_name.setdefault(function.name, []).append(function)
    unresolved = 0
    for function in functions:
        for raw_call in raw_calls[function.id]:
            name = raw_call.name
            if exclude_macro_functions and name in macro_names:
                continue
            target = _best_target(function, by_name.get(name, []))
            if target:
                function.calls.append(CallRef(name=name, target_id=target.id, line=raw_call.line))
                target.callers.add(function.id)
            else:
                function.calls.append(CallRef(name=name, target_id=None, line=raw_call.line))
                unresolved += 1
    return AnalysisResult(
        root=root,
        files=parsed_files,
        functions=functions,
        by_id=by_id,
        by_name=by_name,
        unresolved_count=unresolved,
    )


class ClangResolver:
    """Use libclang to refine Tree-sitter call targets for relevant C files."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.index = cindex.Index.create()
        self.compile_database_path = ""
        self.arguments_by_file: dict[str, list[str]] = {}
        self._load_compile_database()

    def _load_compile_database(self) -> None:
        databases = sorted(Path(self.root).rglob("compile_commands.json"), key=lambda path: len(path.parts))
        if not databases:
            return
        database = databases[0]
        try:
            payload = json.loads(database.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        self.compile_database_path = str(database)
        for item in payload:
            file_name = item.get("file", "")
            directory = item.get("directory", str(database.parent))
            file_path = Path(file_name)
            if not file_path.is_absolute():
                file_path = Path(directory) / file_path
            arguments = item.get("arguments")
            if not arguments:
                arguments = shlex.split(item.get("command", ""), posix=False)
            self.arguments_by_file[_norm(str(file_path))] = self._clean_arguments(arguments, str(file_path), directory)

    @staticmethod
    def _clean_arguments(arguments: list[str], source_path: str, directory: str) -> list[str]:
        if not arguments:
            return ["-x", "c", "-std=gnu11"]
        output: list[str] = []
        skip_next = False
        for index, raw in enumerate(arguments):
            argument = str(raw).strip('"')
            if index == 0 or skip_next:
                skip_next = False
                continue
            if argument in {"-c", "/c"}:
                continue
            if argument in {"-o", "/Fo"}:
                skip_next = True
                continue
            if _norm(argument) == _norm(source_path):
                continue
            if argument.startswith("-I") and len(argument) > 2:
                include = Path(argument[2:].strip('"'))
                if not include.is_absolute():
                    include = Path(directory) / include
                output.append("-I" + str(include))
            else:
                output.append(argument)
        return output or ["-x", "c", "-std=gnu11"]

    @staticmethod
    def _function_for_cursor(result: AnalysisResult, cursor: cindex.Cursor) -> FunctionDef | None:
        location = cursor.location
        if not location.file:
            return None
        candidates = [
            function for function in result.by_name.get(cursor.spelling, [])
            if _norm(function.path) == _norm(location.file.name)
        ]
        return min(candidates, key=lambda fn: abs(fn.start_line - location.line), default=None)

    @staticmethod
    def _referenced_target(result: AnalysisResult, caller: FunctionDef, cursor: cindex.Cursor) -> FunctionDef | None:
        referenced = cursor.referenced
        if referenced is None:
            return None
        definition = referenced.get_definition() or referenced
        location = definition.location
        if location.file:
            candidates = [
                function for function in result.by_name.get(definition.spelling or cursor.spelling, [])
                if _norm(function.path) == _norm(location.file.name)
            ]
            if candidates:
                return min(candidates, key=lambda fn: abs(fn.start_line - location.line))
        return _best_target(caller, result.by_name.get(definition.spelling or cursor.spelling, []))

    def _translation_unit_arguments(self, path: str) -> list[str]:
        return self.arguments_by_file.get(_norm(path), ["-x", "c", "-std=gnu11"])

    def enrich(
        self,
        result: AnalysisResult,
        paths: Iterable[str] | None = None,
        progress: Callable[[str, int, int, str], None] | None = None,
        max_files: int = 300,
    ) -> int:
        requested = {_norm(path) for path in paths} if paths else _default_reachable_files(result)
        # A compile database makes each translation unit both faster and more useful.
        # Without it Tree-sitter remains the full-project source of truth and libclang
        # refines only a bounded set, so a large embedded SDK cannot delay first paint.
        effective_limit = max_files if self.compile_database_path else min(max_files, 30)
        source_files = [
            parsed for parsed in result.files
            if parsed.path.lower().endswith(".c") and _norm(parsed.path) in requested
        ][:effective_limit]
        parsed_count = 0
        for index, parsed in enumerate(source_files, 1):
            try:
                unit = self.index.parse(
                    parsed.path,
                    args=self._translation_unit_arguments(parsed.path),
                    options=cindex.TranslationUnit.PARSE_INCOMPLETE,
                )
            except cindex.TranslationUnitLoadError:
                continue

            def visit(cursor: cindex.Cursor, caller: FunctionDef | None = None) -> None:
                next_caller = caller
                if cursor.kind == cindex.CursorKind.FUNCTION_DECL and cursor.is_definition():
                    next_caller = self._function_for_cursor(result, cursor)
                elif cursor.kind == cindex.CursorKind.CALL_EXPR and caller:
                    target = self._referenced_target(result, caller, cursor)
                    if target:
                        call_name = cursor.spelling or target.name
                        existing = next((call for call in caller.calls if call.name == call_name), None)
                        if existing:
                            existing.target_id = target.id
                        else:
                            caller.calls.append(CallRef(call_name, target.id, cursor.location.line))
                for child in cursor.get_children():
                    visit(child, next_caller)

            visit(unit.cursor)
            parsed_count += 1
            if progress and (index == 1 or index % 5 == 0 or index == len(source_files)):
                progress("libclang 심볼 해석", index, len(source_files), parsed.path)

        for function in result.functions:
            function.callers.clear()
        unresolved = 0
        for function in result.functions:
            for call in function.calls:
                target = result.function(call.target_id)
                if target:
                    target.callers.add(function.id)
                else:
                    unresolved += 1
        result.unresolved_count = unresolved
        result.clang_files = parsed_count
        result.compile_database = self.compile_database_path
        return parsed_count


def is_main_entry(function: FunctionDef) -> bool:
    return function.name.lower() in {
        "main", "winmain", "wwinmain", "_tmain", "app_main", "main_loop", "mainloop", "main_task",
    }


def is_interrupt_entry(function: FunctionDef) -> bool:
    name = function.name.lower()
    core_handlers = {
        "nmi_handler", "hardfault_handler", "memmanage_handler", "busfault_handler",
        "usagefault_handler", "svc_handler", "debugmon_handler", "pendsv_handler",
        "systick_handler", "reset_handler", "default_handler",
    }
    return bool(
        name in core_handlers
        or re.search(r"(^|_)(isr|irq|nmi)(_|$)", name)
        or name.endswith("irqhandler")
        or name.endswith("_isr")
        or re.search(r"\b(__interrupt|interrupt|__irq)\b", function.declaration, re.IGNORECASE)
    )


def _is_library_or_generated(function: FunctionDef) -> bool:
    path = _norm(function.path)
    return bool(re.search(
        r"(^|\\)(drivers?|hal_driver|cmsis|libraries|library|lib|middleware|middlewares|"
        r"third_party|thirdparty|vendor|sdk|examples?|samples?|tests?|benchmark|backup)(\\|$)",
        path,
    ))


def _is_probable_independent_entry(function: FunctionDef) -> bool:
    if function.callers or _is_library_or_generated(function):
        return False
    if re.search(r"\bstatic\b", function.declaration):
        return False
    return bool(re.search(
        r"(^|_)(task|thread|entry|callback|hook|worker|process|runner)$",
        function.name,
        re.IGNORECASE,
    ))


def _project_prefix(function: FunctionDef) -> str:
    value = _norm(function.path)
    for marker in ("\\core\\", "\\source\\", "\\src\\"):
        position = value.find(marker)
        if position > 0:
            return value[:position]
    return value.rsplit("\\", 1)[0]


def _belongs_to_project(function: FunctionDef, main: FunctionDef | None) -> bool:
    return main is None or _norm(function.path).startswith(_project_prefix(main) + "\\")


def _main_score(function: FunctionDef, root: str) -> int:
    path = _relative(function.path, root).lower().replace("/", "\\")
    score = len(function.calls) * 5
    if function.file.lower() == "main.c":
        score += 250
    if re.search(r"(^|\\)(src|source|app|application)(\\|$)", path):
        score += 120
    if re.search(r"example|sample|demo|test|benchmark|third.party|external|cmsis|library", path):
        score -= 800
    return score - len(path.split("\\")) * 3


def main_candidates(result: AnalysisResult) -> list[FunctionDef]:
    return sorted((fn for fn in result.functions if is_main_entry(fn)), key=lambda fn: _norm(fn.path))


def choose_main(result: AnalysisResult, selected_id: str | None = None) -> FunctionDef | None:
    candidates = main_candidates(result)
    if selected_id:
        selected = result.function(selected_id)
        if selected and is_main_entry(selected):
            return selected
    return max(candidates, key=lambda fn: _main_score(fn, result.root), default=None)


def _mark_reachable(result: AnalysisResult, root: FunctionDef, marked: set[str]) -> None:
    stack = [root]
    while stack:
        function = stack.pop()
        if function.id in marked:
            continue
        marked.add(function.id)
        for call in function.calls:
            target = result.function(call.target_id)
            if target and target.id not in marked:
                stack.append(target)


def _default_reachable_files(result: AnalysisResult) -> set[str]:
    selected_main = choose_main(result)
    roots: list[FunctionDef] = [selected_main] if selected_main else []
    main_reachable: set[str] = set()
    if selected_main:
        _mark_reachable(result, selected_main, main_reachable)
    roots.extend(
        function for function in result.functions
        if not is_main_entry(function)
        and function.id not in main_reachable
        and is_interrupt_entry(function)
        and _belongs_to_project(function, selected_main)
        and not _is_library_or_generated(function)
    )
    marked: set[str] = set()
    for root in roots:
        _mark_reachable(result, root, marked)
    return {_norm(result.by_id[function_id].path) for function_id in marked}


def build_call_view(
    result: AnalysisResult,
    selected_main_id: str | None = None,
    include_other_roots: bool = False,
    search: str = "",
    include_external_calls: bool = True,
) -> CallView:
    selected_main = choose_main(result, selected_main_id)
    candidates = main_candidates(result)
    main_reachable: set[str] = set()
    if selected_main:
        _mark_reachable(result, selected_main, main_reachable)
    try:
        from runtime_model import build_runtime_objects

        runtime_roots = []
        runtime_ids: set[str] = set()
        for runtime_object in build_runtime_objects(result):
            if runtime_object.kind not in {"task", "timer"} or not runtime_object.function_id:
                continue
            function = result.function(runtime_object.function_id)
            if (
                function is not None
                and function.id not in runtime_ids
                and function.id != (selected_main.id if selected_main else None)
                and _belongs_to_project(function, selected_main)
                and not _is_library_or_generated(function)
            ):
                runtime_roots.append(function)
                runtime_ids.add(function.id)
    except (ImportError, AttributeError, TypeError):
        runtime_roots = []
    interrupt_roots = [
        fn for fn in result.functions
        if not is_main_entry(fn)
        and fn.id not in main_reachable
        and is_interrupt_entry(fn)
        and _belongs_to_project(fn, selected_main)
        and not _is_library_or_generated(fn)
    ]
    groups: list[tuple[str, str, list[FunctionDef]]] = [
        ("MAIN LOOP 시작점", "main", [selected_main] if selected_main else []),
        ("RTOS Task / Timer 시작점", "runtime", runtime_roots),
        ("인터럽트 / ISR 독립 시작점", "interrupt", interrupt_roots),
        ("확실한 기타 독립 시작점", "independent", []),
    ]
    marked: set[str] = set(main_reachable)
    for function in runtime_roots:
        _mark_reachable(result, function, marked)
    for function in interrupt_roots:
        _mark_reachable(result, function, marked)
    if include_other_roots:
        for function in result.functions:
            if (
                not is_main_entry(function)
                and function.id not in marked
                and _belongs_to_project(function, selected_main)
                and _is_probable_independent_entry(function)
            ):
                groups[-1][2].append(function)
                _mark_reachable(result, function, marked)
    query = search.strip().lower()
    if query:
        matches = [
            fn for fn in result.functions
            if (not is_main_entry(fn) or fn is selected_main)
            and (query in fn.name.lower() or query in fn.file.lower() or query in fn.path.lower())
        ]
        groups = [("검색 결과", "search", matches)]

    rows: list[ViewRow] = []
    max_depth = 1
    interrupt_count = sum(len(roots) for _, kind, roots in groups if kind == "interrupt")
    runtime_count = sum(len(roots) for _, kind, roots in groups if kind == "runtime")

    for title, group_type, roots in groups:
        for root in roots:
            rows.append(ViewRow(
                kind="section",
                title=f"{title} — {_relative(root.path, result.root)}:{root.start_line}",
                state=group_type,
            ))
            root_key = f"{group_type}|{_norm(root.path)}|{root.name}"
            stack: list[tuple[FunctionDef | None, str, int, frozenset[str], str, str, str, str, tuple[int, ...]]] = [
                (root, root.name, 1, frozenset(), "normal", root_key, "", "", ())
            ]
            while stack:
                if len(rows) >= MAX_VIEW_ROWS:
                    rows.append(ViewRow(
                        kind="section",
                        title=f"안전 제한: 호출 트리가 {MAX_VIEW_ROWS:,}행을 초과하여 나머지 조합 전개를 중단했습니다.",
                        state="independent",
                    ))
                    stack.clear()
                    break
                function, name, depth, path_ids, state, node_key, parent_key, call_file, call_lines = stack.pop()
                if function and state == "normal" and function.id in path_ids:
                    state = "cycle"
                rows.append(ViewRow(
                    kind="function",
                    depth=depth,
                    name=name,
                    function_id=function.id if function else None,
                    file=function.file if function else "",
                    line=function.start_line if function else 0,
                    state=state,
                    node_key=node_key,
                    parent_key=parent_key,
                    call_file=call_file,
                    call_lines=call_lines,
                ))
                max_depth = max(max_depth, depth)
                if function and state == "normal":
                    next_path = path_ids | {function.id}
                    children = []
                    name_counts: dict[str, int] = {}
                    grouped_calls: dict[str, list[CallRef]] = {}
                    call_order: list[str] = []
                    for call in function.calls:
                        group_key = call.target_id or f"external:{call.name}"
                        if group_key not in grouped_calls:
                            grouped_calls[group_key] = []
                            call_order.append(group_key)
                        grouped_calls[group_key].append(call)
                    for group_key in call_order:
                        calls = grouped_calls[group_key]
                        call = calls[0]
                        name_counts[call.name] = name_counts.get(call.name, 0) + 1
                        child_key = f"{node_key}/{call.name}#{name_counts[call.name]}"
                        target = result.function(call.target_id)
                        if target is None and not include_external_calls:
                            continue
                        distinct_lines = tuple(dict.fromkeys(item.line for item in calls))
                        children.append((
                            target,
                            call.name,
                            depth + 1,
                            frozenset(next_path),
                            "normal" if target else "external",
                            child_key,
                            node_key,
                            function.file,
                            distinct_lines,
                        ))
                    remaining = max(0, MAX_VIEW_ROWS - len(rows) - len(stack))
                    stack.extend(reversed(children[:remaining]))
            rows.append(ViewRow(kind="spacer"))
    return CallView(
        rows=rows,
        max_depth=max_depth,
        main_candidates=candidates,
        selected_main_id=selected_main.id if selected_main else None,
        interrupt_roots=interrupt_count,
        runtime_roots=runtime_count,
    )


class AnalyzerSession:
    def __init__(self) -> None:
        self.root = ""
        self.cache: dict[str, ParsedFile] = {}
        self.result: AnalysisResult | None = None
        self.clang_resolver: ClangResolver | None = None
        self.excluded_directories: tuple[str, ...] = ()
        self.exclude_macro_functions = True
        self._analysis_options_dirty = False
        self._lock = threading.RLock()

    def restore(
        self,
        root: str,
        cache: dict[str, ParsedFile],
        result: AnalysisResult,
        excluded_directories: Iterable[str] = (),
        exclude_macro_functions: bool = True,
    ) -> None:
        """디스크 캐시를 즉시 사용할 수 있는 분석 세션으로 복원한다."""
        with self._lock:
            self.root = str(Path(root).resolve())
            self.cache = cache
            self.result = result
            self.excluded_directories = tuple(excluded_directories)
            self.exclude_macro_functions = bool(exclude_macro_functions)
            self._analysis_options_dirty = False
            self.clang_resolver = ClangResolver(self.root)

    def set_excluded_directories(self, values: Iterable[str]) -> None:
        with self._lock:
            self.excluded_directories = tuple(values)

    def set_exclude_macro_functions(self, enabled: bool) -> None:
        with self._lock:
            enabled = bool(enabled)
            if enabled != self.exclude_macro_functions:
                self.exclude_macro_functions = enabled
                self._analysis_options_dirty = True

    def relink(self) -> AnalysisResult:
        with self._lock:
            result = link_analysis(
                self.cache.values(),
                self.root,
                exclude_macro_functions=self.exclude_macro_functions,
            )
            self.result = result
            self._analysis_options_dirty = False
            return result

    @staticmethod
    def scan_metadata(root: str, excluded_directories: Iterable[str] = ()) -> dict[str, tuple[str, int, int]]:
        output: dict[str, tuple[str, int, int]] = {}
        root_path = Path(root).resolve()
        excluded = tuple(_norm(str(root_path / value)).rstrip("\\") for value in excluded_directories)
        # os.walk(topdown=True) lets us prune SDK build/cache directories before
        # entering them. Path.rglob would still traverse those large trees.
        for directory, subdirectories, filenames in os.walk(root):
            kept: list[str] = []
            for name in subdirectories:
                candidate = _norm(str(Path(directory) / name)).rstrip("\\")
                if name.lower() in EXCLUDED_DIRS:
                    continue
                if any(candidate == blocked or candidate.startswith(blocked + "\\") for blocked in excluded):
                    continue
                kept.append(name)
            subdirectories[:] = kept
            for filename in filenames:
                if Path(filename).suffix.lower() not in {".c", ".h"}:
                    continue
                path = Path(directory) / filename
                candidate = _norm(str(path)).rstrip("\\")
                if any(candidate == blocked or candidate.startswith(blocked + "\\") for blocked in excluded):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                output[_norm(str(path))] = (str(path), stat.st_mtime_ns, stat.st_size)
        return output

    def initial_scan(
        self,
        root: str,
        progress: Callable[[str, int, int, str], None] | None = None,
        cancel: threading.Event | None = None,
        excluded_directories: Iterable[str] = (),
        exclude_macro_functions: bool = True,
    ) -> AnalysisResult:
        self.excluded_directories = tuple(excluded_directories)
        self.exclude_macro_functions = bool(exclude_macro_functions)
        self._analysis_options_dirty = False
        metadata = self.scan_metadata(root, self.excluded_directories)
        parsed: dict[str, ParsedFile] = {}
        items = sorted(metadata.items())
        for index, (key, (path, modified_ns, file_size)) in enumerate(items, 1):
            if cancel and cancel.is_set():
                raise RuntimeError("분석이 취소되었습니다.")
            try:
                parsed[key] = parse_file(path, root, modified_ns, file_size)
            except (OSError, UnicodeError):
                continue
            if progress and (index == 1 or index % 10 == 0 or index == len(items)):
                progress("C 파일 파싱", index, len(items), path)
        if progress:
            progress("호출 관계 연결", 0, len(parsed), "함수 정의를 연결하고 있습니다.")
        result = link_analysis(
            parsed.values(),
            root,
            exclude_macro_functions=self.exclude_macro_functions,
        )
        resolver = ClangResolver(root)
        resolver.enrich(result, progress=progress)
        with self._lock:
            self.root = root
            self.cache = parsed
            self.result = result
            self.clang_resolver = resolver
        return result

    def check_updates(
        self,
        progress: Callable[[str, int, int, str], None] | None = None,
    ) -> tuple[AnalysisResult, int, int]:
        with self._lock:
            root = self.root
            old_cache = dict(self.cache)
        metadata = self.scan_metadata(root, self.excluded_directories)
        changed = [
            (key, value) for key, value in metadata.items()
            if (
                key not in old_cache
                or old_cache[key].modified_ns != value[1]
                or old_cache[key].file_size != value[2]
            )
        ]
        deleted = [key for key in old_cache if key not in metadata]
        if not changed and not deleted and not self._analysis_options_dirty:
            if self.result is None:
                raise RuntimeError("분석 결과가 없습니다.")
            return self.result, 0, 0
        updated = old_cache
        for index, (key, (path, modified_ns, file_size)) in enumerate(changed, 1):
            try:
                updated[key] = parse_file(path, root, modified_ns, file_size)
            except (OSError, UnicodeError):
                continue
            if progress:
                progress("변경 파일 파싱", index, len(changed), path)
        for key in deleted:
            updated.pop(key, None)
        result = link_analysis(
            updated.values(),
            root,
            exclude_macro_functions=self.exclude_macro_functions,
        )
        resolver = self.clang_resolver or ClangResolver(root)
        resolver.enrich(result, paths=(path for _, (path, _, _) in changed), progress=progress)
        with self._lock:
            self.cache = updated
            self.result = result
            self.clang_resolver = resolver
            self._analysis_options_dirty = False
        return result, len(changed), len(deleted)
