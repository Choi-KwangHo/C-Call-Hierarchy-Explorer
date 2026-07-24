from __future__ import annotations

import re
from dataclasses import dataclass, field

from analyzer import AnalysisResult, FunctionDef, is_interrupt_entry, is_main_entry, mask_non_code


@dataclass(slots=True)
class RuntimeObject:
    id: str
    kind: str
    name: str
    function_id: str | None
    file: str
    line: int
    confidence: str
    evidence: str
    attributes: dict[str, str] = field(default_factory=dict)


_CREATE_APIS: dict[str, tuple[str, int, dict[str, int]]] = {
    "xTaskCreate": ("task", 0, {"표시 이름": 1, "Stack": 2, "Priority": 4, "Handle": 5}),
    "xTaskCreateStatic": ("task", 0, {"표시 이름": 1, "Stack": 2, "Priority": 4, "Handle": 5}),
    "osThreadNew": ("task", 0, {"인자": 1, "속성": 2}),
    "xTimerCreate": ("timer", 4, {"표시 이름": 0, "주기": 1, "자동 재시작": 2}),
    "xTimerCreateStatic": ("timer", 5, {"표시 이름": 0, "주기": 1, "자동 재시작": 2}),
    "osTimerNew": ("timer", 0, {"타입": 1, "인자": 2, "속성": 3}),
}


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _matching_parenthesis(masked: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(masked)):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _split_arguments(source: str, masked: str, start: int, end: int) -> list[str]:
    arguments: list[str] = []
    item_start = start
    paren = bracket = brace = 0
    for index in range(start, end):
        char = masked[index]
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == "," and not (paren or bracket or brace):
            arguments.append(source[item_start:index].strip())
            item_start = index + 1
    arguments.append(source[item_start:end].strip())
    return arguments


def _symbol_name(argument: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", " ", argument)
    identifiers = re.findall(r"[A-Za-z_]\w*", cleaned)
    ignored = {"void", "const", "static", "TaskFunction_t", "TimerCallbackFunction_t", "NULL"}
    return next((name for name in reversed(identifiers) if name not in ignored), "")


def _display_value(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact.strip('"') if len(compact) >= 2 and compact[0] == compact[-1] == '"' else compact


def _created_objects(result: AnalysisResult) -> list[RuntimeObject]:
    objects: list[RuntimeObject] = []
    seen: set[tuple[str, str, int]] = set()
    for parsed in result.files:
        masked = mask_non_code(parsed.text)
        for api, (kind, function_argument, attribute_indexes) in _CREATE_APIS.items():
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", masked):
                opening = masked.find("(", match.start())
                closing = _matching_parenthesis(masked, opening)
                if closing < 0:
                    continue
                arguments = _split_arguments(parsed.text, masked, opening + 1, closing)
                if function_argument >= len(arguments):
                    continue
                symbol = _symbol_name(arguments[function_argument])
                if not symbol:
                    continue
                target = result.by_name.get(symbol, [None])[0]
                line = _line_number(parsed.text, match.start())
                key = (kind, symbol, line)
                if key in seen:
                    continue
                seen.add(key)
                attributes = {
                    label: _display_value(arguments[index])
                    for label, index in attribute_indexes.items()
                    if index < len(arguments) and _display_value(arguments[index])
                }
                objects.append(RuntimeObject(
                    id=f"{kind}|{parsed.path}|{line}|{symbol}",
                    kind=kind,
                    name=symbol,
                    function_id=target.id if target else None,
                    file=parsed.path,
                    line=line,
                    confidence="확정" if target else "추정",
                    evidence=f"{api}() 인자에서 실행 함수로 등록",
                    attributes=attributes,
                ))
    return objects


def build_runtime_objects(result: AnalysisResult) -> list[RuntimeObject]:
    objects: list[RuntimeObject] = []
    created = _created_objects(result)
    registered_ids = {item.function_id for item in created if item.function_id}

    for function in result.functions:
        if is_main_entry(function):
            objects.append(RuntimeObject(
                id=f"main|{function.id}",
                kind="main",
                name=function.name,
                function_id=function.id,
                file=function.path,
                line=function.start_line,
                confidence="확정",
                evidence="C 프로그램 main() 진입점",
            ))
        elif is_interrupt_entry(function):
            objects.append(RuntimeObject(
                id=f"isr|{function.id}",
                kind="isr",
                name=function.name,
                function_id=function.id,
                file=function.path,
                line=function.start_line,
                confidence="확정",
                evidence="IRQ/Exception Handler 명명 및 선언 패턴",
            ))

    objects.extend(created)
    for function in result.functions:
        if function.id in registered_ids or is_main_entry(function) or is_interrupt_entry(function):
            continue
        if re.search(r"(Callback|_Hook|Hook$)", function.name, re.IGNORECASE):
            objects.append(RuntimeObject(
                id=f"callback|{function.id}",
                kind="callback",
                name=function.name,
                function_id=function.id,
                file=function.path,
                line=function.start_line,
                confidence="추정",
                evidence="Callback/Hook 함수 명명 패턴",
            ))

    order = {"main": 0, "task": 1, "isr": 2, "timer": 3, "callback": 4}
    return sorted(objects, key=lambda item: (order.get(item.kind, 99), item.name.casefold(), item.file.casefold(), item.line))
