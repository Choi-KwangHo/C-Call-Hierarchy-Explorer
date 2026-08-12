from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(slots=True)
class MemoryRange:
    name: str
    start: int = 0
    end: int = 0
    size: int = 0
    found: bool = False
    source_line: str = ""


@dataclass(slots=True)
class StackUsage:
    category: str
    max_use: int
    total_use: int


@dataclass(slots=True)
class MapSymbol:
    address: int
    name: str
    size: int
    scope: str = ""


@dataclass(slots=True)
class MapAnalysis:
    path: str
    file_name: str
    signature: str
    readonly_code: int = 0
    readonly_data: int = 0
    readwrite_data: int = 0
    flash: MemoryRange = field(default_factory=lambda: MemoryRange("Flash"))
    sram: MemoryRange = field(default_factory=lambda: MemoryRange("SRAM"))
    stack_bottom: MemoryRange = field(default_factory=lambda: MemoryRange("STACK_BOTTOM_B"))
    cstack: MemoryRange = field(default_factory=lambda: MemoryRange("CSTACK"))
    heap: MemoryRange = field(default_factory=lambda: MemoryRange("HEAP"))
    stack_usage: list[StackUsage] = field(default_factory=list)
    symbols: list[MapSymbol] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)
    mcu_hint: str = ""
    icf_file: str = ""
    heap_handler: str = ""
    malloc_present: bool = False
    calloc_present: bool = False
    free_present: bool = False
    no_free: bool = False

    @property
    def flash_used(self) -> int:
        return self.readonly_code + self.readonly_data

    @property
    def static_rw(self) -> int:
        return max(0, self.readwrite_data - self.stack_bottom.size - self.cstack.size - self.heap.size)

    @property
    def max_stack(self) -> int:
        return max((item.max_use for item in self.stack_usage), default=0)


HEX_RE = r"(?:0x)?[0-9A-Fa-f]+(?:'[0-9A-Fa-f]{4})*"
SIZE_RE = re.compile(r"(?P<value>(?:0x)?[0-9A-Fa-f][0-9A-Fa-f',]*)\s*(?P<unit>K|KB|M|MB)?", re.I)


def _num(value: str) -> int:
    value = str(value or "").replace("'", "").replace(",", "").strip()
    if not value:
        return 0
    try:
        return int(value, 0 if value.lower().startswith("0x") else 10)
    except ValueError:
        try:
            return int(value, 16)
        except ValueError:
            return 0


def parse_size(value: str) -> int:
    match = SIZE_RE.search(str(value or ""))
    if not match:
        return _num(value)
    raw = match.group("value").replace("'", "").replace(",", "")
    if raw.lower().startswith("0x"):
        amount = float(int(raw, 16))
    else:
        try:
            amount = float(raw)
        except ValueError:
            return _num(raw)
    unit = (match.group("unit") or "").upper()
    if unit in {"K", "KB"}:
        amount *= 1024
    elif unit in {"M", "MB"}:
        amount *= 1024 * 1024
    return int(round(amount))


def _signature(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def _summary(text: str, label: str) -> int:
    pattern = re.compile(
        rf"{label}\s*[:=]?\s*(?:\([^)]*\))?\s*(?P<size>(?:0x)?[0-9A-Fa-f][0-9A-Fa-f',]*(?:\.\d+)?\s*(?:K|KB|M|MB)?)",
        re.I,
    )
    match = pattern.search(text)
    return parse_size(match.group("size")) if match else 0


def _range(text: str, name: str, default_start: int, default_end: int) -> MemoryRange:
    result = MemoryRange(name, default_start, default_end, max(0, default_end - default_start + 1))
    patterns = [
        rf"{re.escape(name)}[^\r\n]*?from\s+({HEX_RE})\s+to\s+({HEX_RE})",
        rf"{re.escape(name)}[^\r\n]*?({HEX_RE})\s*(?:-|\.\.)\s*({HEX_RE})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            result.start = _num(match.group(1))
            result.end = _num(match.group(2))
            result.size = max(0, result.end - result.start + 1)
            result.found = True
            result.source_line = next((line.strip() for line in text.splitlines() if name.lower() in line.lower()), "")
            return result
    return result


def _block_definition(text: str, name: str) -> int:
    patterns = [
        rf"define\s+block\s+{re.escape(name)}[^\r\n]*?size\s*=?\s*({HEX_RE}|[0-9][0-9',]*\s*(?:K|KB|M|MB)?)",
        rf"{re.escape(name)}[^\r\n]*?size\s*[:=]\s*({HEX_RE}|[0-9][0-9',]*\s*(?:K|KB|M|MB)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _num(match.group(1)) if match.group(1).lower().startswith("0x") else parse_size(match.group(1))
    return 0


def _place(text: str, name: str) -> MemoryRange:
    match = re.search(
        rf"{re.escape(name)}[^\r\n]*?(?:at|from)\s+({HEX_RE})(?:\s+to\s+({HEX_RE}))?",
        text,
        re.I,
    )
    if not match:
        return MemoryRange(name)
    start = _num(match.group(1))
    end = _num(match.group(2)) if match.group(2) else 0
    result = MemoryRange(name, start, end, max(0, end - start + 1), True)
    result.source_line = next((line.strip() for line in text.splitlines() if name.lower() in line.lower()), "")
    return result


def _stack_usage(text: str) -> list[StackUsage]:
    result: list[StackUsage] = []
    in_section = False
    for line in text.splitlines():
        if "STACK USAGE" in line.upper():
            in_section = True
            continue
        if not in_section:
            continue
        if not line.strip():
            if result:
                break
            continue
        numbers = re.findall(r"(?:0x)?[0-9A-Fa-f]+", line)
        if len(numbers) < 2:
            continue
        # IAR versions differ in column order; the final two numeric columns
        # are the useful max/total values in the supported reports.
        values = [_num(item) for item in numbers[-2:]]
        result.append(StackUsage(line.strip().split()[0], values[0], values[1]))
    return result


def _symbols(text: str, sram: MemoryRange) -> list[MapSymbol]:
    result: list[MapSymbol] = []
    for line in text.splitlines():
        match = re.match(r"\s*(0x[0-9A-Fa-f]+|[0-9A-Fa-f]{8})\s+([^\s]+)\s+(0x[0-9A-Fa-f]+|[0-9]+)\s*$", line)
        if not match:
            continue
        address = _num(match.group(1))
        size = _num(match.group(3))
        if sram.start <= address <= sram.end:
            result.append(MapSymbol(address, match.group(2), size, "SRAM"))
    return result[:5000]


def parse_map_text(text: str, path: str = "") -> MapAnalysis:
    file_path = Path(path) if path else Path("unknown.map")
    result = MapAnalysis(str(file_path), file_path.name, "")
    result.readonly_code = _summary(text, r"readonly\s+code\s+memory")
    result.readonly_data = _summary(text, r"readonly\s+data\s+memory")
    result.readwrite_data = _summary(text, r"readwrite\s+data\s+memory")
    result.flash = _range(text, "P5", 0x08000000, 0x0807FFFF)
    result.sram = _range(text, "P6", 0x20000000, 0x2000FFFF)
    # IAR names physical regions P5/P6 differently between linker files.
    # Classify by address rather than assuming P5=Flash/P6=SRAM.
    if result.flash.found and result.sram.found:
        flash_like = 0x08000000 <= result.flash.start < 0x10000000
        sram_like = 0x20000000 <= result.sram.start < 0x30000000
        if not flash_like and sram_like is False:
            pass
        elif not flash_like or not sram_like:
            result.flash, result.sram = result.sram, result.flash
            result.flash.name = "Flash"
            result.sram.name = "SRAM"
    result.stack_bottom = _place(text, "STACK_BOTTOM_B")
    result.cstack = _place(text, "CSTACK")
    result.heap = _place(text, "HEAP")
    for block in (result.stack_bottom, result.cstack, result.heap):
        if not block.size:
            block.size = _block_definition(text, block.name)
    if not result.sram.found and result.heap.end:
        result.sram.start = 0x20000000
        result.sram.end = result.heap.end
        result.sram.size = max(0, result.sram.end - result.sram.start + 1)
        result.sram.source_line = "P6 not parsed; derived from HEAP end"
    result.stack_usage = _stack_usage(text)
    result.symbols = _symbols(text, result.sram)
    result.heap_handler = (re.search(r"^__Heap_Handler\s*=\s*(\S+)", text, re.I | re.M) or ["", ""])[1]
    result.malloc_present = bool(re.search(r"\bmalloc\b|__no_free_malloc", text, re.I))
    result.calloc_present = bool(re.search(r"\bcalloc\b", text, re.I))
    result.free_present = bool(re.search(r"\bfree\s*\(", text, re.I))
    result.no_free = bool(re.search(r"__no_free_malloc|NoFree", text, re.I))
    icf = re.search(r"(?:config|icf)[^\r\n]*?([A-Za-z0-9_.-]+\.icf)", text, re.I)
    result.icf_file = icf.group(1) if icf else ""
    mcu = re.search(r"STM32[A-Z0-9]+", text, re.I)
    result.mcu_hint = mcu.group(0).upper() if mcu else ""
    # Prefer the exact device family encoded in the map/linker configuration.
    # STM32F103VCT6 is 256 KiB Flash / 48 KiB SRAM; this prevents a generic
    # STM32F103XE ICF hint from producing a 244 KiB SRAM denominator.
    if result.mcu_hint.startswith("STM32F103") and result.sram.size > 64 * 1024:
        result.warnings.append("SRAM 범위가 STM32F103 계열의 일반 용량을 초과합니다. ICF/프로젝트 MCU 설정을 확인하십시오.")
    result.raw_lines = [line.strip() for line in text.splitlines() if re.search(r"P[56]|STACK|HEAP|memory|MCU|ICF", line, re.I)][:80]
    if result.readwrite_data and result.sram.size and result.readwrite_data > result.sram.size:
        result.warnings.append("readwrite data가 MAP SRAM 범위를 초과합니다.")
    expected = {
        "STM32F103VCT6": (256 * 1024, 48 * 1024),
        "STM32F103VCT": (256 * 1024, 48 * 1024),
    }.get(result.mcu_hint)
    if expected:
        flash_expected, sram_expected = expected
        if result.flash.size and result.flash.size != flash_expected:
            result.warnings.append(
                f"MCU({result.mcu_hint}) 기준 Flash {flash_expected:,} B와 MAP 영역({result.flash.size:,} B)이 다릅니다. ICF를 확인하십시오."
            )
        if result.sram.size and result.sram.size != sram_expected:
            result.warnings.append(
                f"MCU({result.mcu_hint}) 기준 SRAM {sram_expected:,} B와 MAP 영역({result.sram.size:,} B)이 다릅니다. ICF를 확인하십시오."
            )
    if result.cstack.size and result.max_stack > result.cstack.size:
        result.warnings.append("IAR Stack Usage 최대값이 CSTACK 예약량을 초과합니다.")
    if result.heap.size and result.cstack.end and result.heap.start and result.heap.start < result.cstack.end:
        result.warnings.append("CSTACK과 HEAP 배치가 겹치거나 순서가 불명확합니다.")
    if not result.stack_usage:
        result.warnings.append("IAR Stack Usage 표를 찾지 못했습니다. 실제 Stack 여유는 런타임 측정이 필요합니다.")
    return result


def parse_map_file(path: str | Path) -> MapAnalysis:
    file_path = Path(path).resolve()
    text = file_path.read_text(encoding="utf-8", errors="replace")
    result = parse_map_text(text, str(file_path))
    # Resolve the device from the CubeMX project first.  The .ioc contains the
    # exact part number (Mcu.CPN), unlike a generic IAR library/ICF family.
    # The MAP may contain a generic library hint (for example STM32F103XE),
    # while the actual .ewp/.icf selects the exact STM32F103VCT6 device.
    search_roots = [file_path.parent, *file_path.parents[:2]]
    for root in search_roots:
        try:
            iocs = list(root.glob("*.ioc"))
        except OSError:
            iocs = []
        for ioc in iocs:
            try:
                ioc_text = ioc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            exact = re.search(r"(?mi)^Mcu\.CPN\s*=\s*(STM32[A-Z0-9]+)", ioc_text)
            device = exact.group(1).upper() if exact else ""
            if device:
                result.mcu_hint = device
                result.icf_file = result.icf_file or ioc.name
                break
        if result.mcu_hint:
            break

    # Fall back to the IAR linker configuration when no CubeMX project is
    # present beside the MAP file.
    for root in search_roots:
        try:
            candidates = list(root.glob("*.icf")) + list(root.rglob("*.icf"))
        except OSError:
            candidates = []
        for icf in candidates:
            name = icf.name.upper()
            match = re.search(r"STM32F103(V[AC]T6|VC|XE)", name)
            if match:
                result.mcu_hint = "STM32F103" + match.group(1)
                result.icf_file = icf.name
                break
        if result.mcu_hint.startswith("STM32F103VC"):
            break
    result.signature = _signature(file_path)
    return result


def discover_map_files(root: str | Path) -> list[Path]:
    root_path = Path(root).resolve()
    candidates: list[Path] = []
    for path in root_path.rglob("*.map"):
        if not path.is_file() or any(part.casefold() in {".git", ".venv", "build", "dist"} for part in path.parts):
            continue
        candidates.append(path)
    project_names = {p.stem.casefold() for p in root_path.rglob("*.ewp")}
    def score(path: Path) -> tuple[int, int, int, str]:
        lower = str(path).casefold()
        project_match = int(path.stem.casefold() in project_names)
        list_match = int("\\list\\" in lower or "/list/" in lower)
        debug_match = int("debug" in lower or "release" in lower)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        return (project_match, list_match, debug_match, mtime, str(path).casefold())
    return sorted(candidates, key=score, reverse=True)


def choose_map_file(root: str | Path, preferred: str = "") -> Path | None:
    candidates = discover_map_files(root)
    if preferred:
        preferred_path = Path(preferred)
        if preferred_path.is_file() and preferred_path.resolve() in [p.resolve() for p in candidates]:
            return preferred_path.resolve()
    return candidates[0] if candidates else None
