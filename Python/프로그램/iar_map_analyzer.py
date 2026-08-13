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
class MapRegion:
    name: str
    start: int
    end: int
    rule: str = ""
    placement_id: str = ""
    sections: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    used: int = 0
    category: str = "General"

    @property
    def size(self) -> int:
        return max(0, self.end - self.start + 1)


@dataclass(slots=True)
class PlacementSymbol:
    name: str
    start: int
    size: int
    object_file: str = ""
    section: str = ""
    kind: str = "Function"
    region: str = ""
    source_line: int = 0

    @property
    def end(self) -> int:
        return self.start + max(0, self.size - 1)


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
    regions: list[MapRegion] = field(default_factory=list)
    placement_symbols: list[PlacementSymbol] = field(default_factory=list)
    raw_map_text: str = ""

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


def _category(name: str) -> str:
    upper = name.upper()
    if "VECTOR" in upper:
        return "Vector"
    if "SAFETY" in upper:
        return "Safety ROM"
    if "CLASS_B_RAM_REV" in upper:
        return "Class B RAM Reverse"
    if "CLASS_B_RAM" in upper:
        return "Class B RAM"
    if "RAM" in upper:
        return "RAM"
    if "ROM" in upper or "APP" in upper:
        return "ROM"
    return "General"


def _region_category(name: str, rule: str, start: int) -> str:
    text = (name + " " + rule).upper()
    if "INTVEC" in text or "VECTOR" in text:
        return "Vector"
    if "SAFETY" in text or "STL" in text:
        return "Safety ROM"
    if "CLASS_B_RAM_REV" in text:
        return "Class B RAM Reverse"
    if "CLASS_B_RAM" in text:
        return "Class B RAM"
    if 0x08000000 <= start < 0x10000000:
        return "ROM"
    if 0x20000000 <= start < 0x30000000:
        return "RAM"
    return _category(name)


def _icf_regions(text: str) -> list[MapRegion]:
    regions: list[MapRegion] = []
    pattern = re.compile(r"define\s+region\s+(\w+)\s*=\s*mem:\s*\[\s*from\s+(%s)\s+to\s+(%s)\s*\]\s*;" % (HEX_RE, HEX_RE), re.I | re.S)
    for match in pattern.finditer(text):
        name, start, end = match.group(1), _num(match.group(2)), _num(match.group(3))
        tail = text[match.end(): match.end() + 1200]
        place = re.search(r"place\s+in\s+" + re.escape(name) + r"\s*\{(?P<rule>.*?)\}", tail, re.I | re.S)
        rule = re.sub(r"\s+", " ", place.group("rule").strip()) if place else ""
        sections = re.findall(r"(?:section|block)\s+([.\w$]+)", rule, re.I)
        regions.append(MapRegion(name, start, end, rule, sections=sections, category=_category(name)))
    return regions


def _map_placements(text: str) -> list[MapRegion]:
    result: list[MapRegion] = []
    pattern = re.compile(r'"?(P\d+(?:\|P\d+)*)"?\s*:\s*place\s+in\s*\[\s*from\s+(%s)\s+to\s+(%s)\s*\]\s*\{(?P<rule>.*?)\}' % (HEX_RE, HEX_RE), re.I | re.S)
    for match in pattern.finditer(text):
        rule = re.sub(r"\s+", " ", match.group("rule").strip())
        result.append(MapRegion(match.group(1), _num(match.group(2)), _num(match.group(3)), rule, match.group(1), re.findall(r"(?:section|block)\s+([.\w$]+)", rule, re.I)))
    return result


def _map_function_symbols(text: str) -> list[PlacementSymbol]:
    symbols: list[PlacementSymbol] = []
    seen: set[tuple[str, int]] = set()
    # Typical IAR: Function 0x800'59ed 0x6a Code Safe_FailSafe.o [1]
    patterns = [
        re.compile(r"^\s*([~\w:$<>.]+)\s+(%s)\s+(%s)\s+(Code|Data)\b(?:.*?\s+([\w.-]+\.o))?" % (HEX_RE, HEX_RE), re.I),
        re.compile(r"^\s*(%s)\s+(%s)\s+(Code|Data)\s+([~\w:$<>.]+)(?:.*?\s+([\w.-]+\.o))?" % (HEX_RE, HEX_RE), re.I),
    ]
    for line_no, line in enumerate(text.splitlines(), 1):
        for index, pattern in enumerate(patterns):
            match = pattern.search(line)
            if not match:
                continue
            if index == 0:
                name, address, size, kind, obj = match.groups()
            else:
                address, size, kind, name, obj = match.groups()
            start, byte_size = _num(address), _num(size)
            if not start or not byte_size or name.startswith("__") or "$$" in name or name.upper().startswith("INTVEC"):
                break
            key = (name, start)
            if key not in seen:
                seen.add(key)
                symbols.append(PlacementSymbol(name, start, byte_size, obj or "", kind="Function" if kind.casefold() == "code" else "Variable", source_line=line_no))
            break
    return symbols


def _map_sections(text: str) -> list[tuple[str, int, int, str]]:
    sections: list[tuple[str, int, int, str]] = []
    pattern = re.compile(r"^\s*([.]?[\w.$]+)\s+(?:ro|rw|zi|zero|code|data)[^\r\n]*?(%s)\s+(%s)(?:\s+([\w.-]+\.o))?" % (HEX_RE, HEX_RE), re.I)
    for line in text.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        name, start, size, obj = match.groups()
        sections.append((name, _num(start), _num(size), obj or ""))
    return sections


def _enrich_layout(result: MapAnalysis, icf_text: str = "") -> None:
    mapped = _map_placements(result.raw_map_text)
    regions = _icf_regions(icf_text)
    for placement in mapped:
        matches = [region for region in regions if region.start == placement.start and region.end == placement.end]
        if matches:
            region = matches[0]
            region.placement_id = placement.placement_id
            if not region.rule:
                region.rule = placement.rule
            region.sections = list(dict.fromkeys([*region.sections, *placement.sections]))
        else:
            regions.append(placement)
    for region in regions:
        region.category = _region_category(region.name, region.rule, region.start)
    symbols = _map_function_symbols(result.raw_map_text)
    sections = _map_sections(result.raw_map_text)
    for symbol in symbols:
        for section, section_start, section_size, section_object in sections:
            if section_start <= symbol.start < section_start + section_size:
                symbol.section = section
                if not symbol.object_file:
                    symbol.object_file = section_object
                break
        hits = [region for region in regions if region.start <= symbol.start <= region.end]
        if len(hits) == 1:
            symbol.region = hits[0].name
            hits[0].used += symbol.size
            if symbol.object_file and symbol.object_file not in hits[0].objects:
                hits[0].objects.append(symbol.object_file)
        elif len(hits) > 1:
            result.warnings.append(f"Symbol {symbol.name} belongs to overlapping regions.")
        else:
            result.warnings.append(f"Symbol {symbol.name} is outside every defined region.")
        if hits and symbol.end > hits[0].end:
            result.warnings.append(f"Symbol {symbol.name} crosses region boundary.")
    # Linker MAP files often expose INTVEC only as a block, not as individual
    # symbols. Show addressable vector slots so the placement can still be
    # audited; named vector symbols, when present, remain preferred.
    for region in regions:
        if region.category != "Vector" or any(item.region == region.name for item in symbols):
            continue
        vector_bytes = min(region.size, 0x130)
        for offset in range(0, vector_bytes, 4):
            symbols.append(PlacementSymbol(f"Vector[{offset // 4:02d}]", region.start + offset, 4, kind="Vector", region=region.name))
        region.used = max(region.used, vector_bytes)
    for region in regions:
        region_symbols = [item for item in symbols if item.region == region.name]
        if region.category == "Safety ROM" and any(item.section in {".text", ".rodata"} for item in region_symbols):
            result.warnings.append(f"General section found in safety region {region.name}.")
        if region.category == "Vector" and any(item.kind == "Function" for item in region_symbols):
            result.warnings.append(f"Function symbol found in vector region {region.name}.")
        if region.category.startswith("Class B RAM") and any(item.kind != "Variable" for item in region_symbols):
            result.warnings.append(f"Non-variable symbol found in Class B RAM region {region.name}.")
    result.regions = sorted(regions, key=lambda item: (item.start, item.end, item.name))
    result.placement_symbols = symbols


def _validate_selected_mcu(result: MapAnalysis) -> None:
    """Validate the final MCU after .ioc/.icf resolution has completed."""
    expected = {"STM32F103VCT6": (256 * 1024, 48 * 1024)}.get(result.mcu_hint)
    if not expected:
        return
    flash_expected, sram_expected = expected
    if result.flash.size and result.flash.size != flash_expected:
        result.warnings.append(f"MCU Flash capacity mismatch: expected {flash_expected:,} B, MAP {result.flash.size:,} B.")
    if result.sram.size and result.sram.size != sram_expected:
        result.warnings.append(f"MCU SRAM capacity mismatch: expected {sram_expected:,} B, MAP {result.sram.size:,} B.")


def parse_map_text(text: str, path: str = "") -> MapAnalysis:
    file_path = Path(path) if path else Path("unknown.map")
    result = MapAnalysis(str(file_path), file_path.name, "")
    result.raw_map_text = text
    result.readonly_code = _summary(text, r"readonly\s+code\s+memory")
    result.readonly_data = _summary(text, r"readonly\s+data\s+memory")
    result.readwrite_data = _summary(text, r"readwrite\s+data\s+memory")
    result.flash = _range(text, "P5", 0x08000000, 0x0807FFFF)
    result.sram = _range(text, "P6", 0x20000000, 0x2000FFFF)
    # IAR names physical regions P5/P6 differently between linker files.
    # Classify by address rather than assuming P5=Flash/P6=SRAM.
    if result.flash.found and result.sram.found:
        p5_is_sram = 0x20000000 <= result.flash.start < 0x30000000
        p6_is_flash = 0x08000000 <= result.sram.start < 0x10000000
        if p5_is_sram and p6_is_flash:
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
    _enrich_layout(result)
    return result


def parse_map_file(path: str | Path) -> MapAnalysis:
    file_path = Path(path).resolve()
    text = file_path.read_text(encoding="utf-8", errors="replace")
    result = parse_map_text(text, str(file_path))
    # Resolve the device from the CubeMX project first.  The .ioc contains the
    # exact part number (Mcu.CPN), unlike a generic IAR library/ICF family.
    # The MAP may contain a generic library hint (for example STM32F103XE),
    # while the actual .ewp/.icf selects the exact STM32F103VCT6 device.
    # MAP files are commonly under EWARM/<config>/List while CubeMX .ioc is
    # several levels above the build directory.
    search_roots = [file_path.parent, *file_path.parents[:8]]
    ioc_found = False
    resolved_icf: Path | None = None
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
                result.icf_file = ioc.name
                ioc_found = True
                break
        if ioc_found:
            break

    # Fall back to the IAR linker configuration when no CubeMX project is
    # present beside the MAP file.
    for root in search_roots:
        if ioc_found:
            break
        try:
            # Searching every descendant of each ancestor makes opening a MAP
            # file unexpectedly expensive. ICF files are normally next to the
            # project/MAP or referenced by name, so inspect direct children.
            candidates = list(root.glob("*.icf"))
        except OSError:
            candidates = []
        for icf in candidates:
            name = icf.name.upper()
            match = re.search(r"STM32F103(V[AC]T6|VC|XE)", name)
            if match:
                result.mcu_hint = "STM32F103" + match.group(1)
                result.icf_file = icf.name
                resolved_icf = icf
                break
        if result.mcu_hint.startswith("STM32F103VC"):
            break
    # Even when the file name has no device marker, use the ICF identified by
    # the MAP or nearest project directory for region and placement metadata.
    if resolved_icf is None:
        for root in search_roots:
            try:
                candidate = next(root.glob("*.icf"))
            except (OSError, StopIteration):
                continue
            resolved_icf = candidate
            result.icf_file = candidate.name
            break
    if resolved_icf is not None:
        try:
            _enrich_layout(result, resolved_icf.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            result.warnings.append("ICF file could not be read; placement-only layout is shown.")
    _validate_selected_mcu(result)
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
