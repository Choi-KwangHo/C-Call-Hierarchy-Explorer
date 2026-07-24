from __future__ import annotations

import difflib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


MANIFEST_NAME = ".cch-trace.json"
GENERATED_DIR = "CCH_Trace"
BEGIN_MARKER = "/* CCH-TRACE:BEGIN id={id} */"
END_MARKER = "/* CCH-TRACE:END id={id} */"
INCLUDE_BEGIN = "/* CCH-TRACE:INCLUDE-BEGIN */"
INCLUDE_END = "/* CCH-TRACE:INCLUDE-END */"


@dataclass(slots=True)
class TracePoint:
    id: str
    file: str
    line: int
    event: str
    label: str
    value: str = "0u"
    channel: int = 1


@dataclass(slots=True)
class SourceText:
    text: str
    encoding: str
    newline: str


def _read_source(path: Path) -> SourceText:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            text = raw.decode(encoding)
            newline = "\r\n" if "\r\n" in text else "\n"
            return SourceText(text=text, encoding=encoding, newline=newline)
        except UnicodeDecodeError:
            continue
    text = raw.decode("latin-1")
    return SourceText(text=text, encoding="latin-1", newline="\r\n" if "\r\n" in text else "\n")


def _write_source(path: Path, source: SourceText, text: str) -> None:
    path.write_bytes(text.encode(source.encoding))


def _manifest_path(root: str | Path) -> Path:
    return Path(root) / MANIFEST_NAME


def load_trace_points(root: str | Path) -> list[TracePoint]:
    path = _manifest_path(root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [TracePoint(**item) for item in payload.get("points", [])]
    except (OSError, ValueError, TypeError):
        return []


def save_trace_points(root: str | Path, points: list[TracePoint]) -> None:
    path = _manifest_path(root)
    payload = {"format": 1, "points": [asdict(point) for point in points]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _event_number(point_id: str) -> int:
    return int(point_id.replace("-", "")[:8], 16) & 0xFFFF


def _event_macro(point: TracePoint) -> str:
    event_id = _event_number(point.id)
    value = point.value.strip() or "0u"
    if point.event == "FUNC_ENTER":
        return f"CCH_TRACE_FUNC_ENTER(0x{event_id:04X}u);"
    if point.event == "FUNC_EXIT":
        return f"CCH_TRACE_FUNC_EXIT(0x{event_id:04X}u);"
    if point.event == "ERROR":
        return f"CCH_TRACE_ERROR(0x{event_id:04X}u, ({value}));"
    if point.event == "HARDFAULT_DUMP":
        return "CCH_HARDFAULT_DUMP();"
    if point.event == "IAR_EVENT":
        return f"CCH_TRACE_IAR_EVENT({point.channel}u, 0x{event_id:04X}u, ({value}));"
    return f"CCH_TRACE_EVENT(0x{event_id:04X}u, ({value}));"


def _managed_block(point: TracePoint, newline: str) -> list[str]:
    return [
        BEGIN_MARKER.format(id=point.id) + newline,
        _event_macro(point) + newline,
        END_MARKER.format(id=point.id) + newline,
    ]


def _include_block(source_path: Path, generated_header: Path, newline: str) -> list[str]:
    relative = os.path.relpath(generated_header, source_path.parent).replace("\\", "/")
    return [
        INCLUDE_BEGIN + newline,
        f'#include "{relative}"{newline}',
        INCLUDE_END + newline,
    ]


def _ensure_include(
    lines: list[str], source_path: Path, generated_header: Path, newline: str
) -> tuple[list[str], int, int]:
    if any(INCLUDE_BEGIN in line for line in lines):
        return lines, -1, 0
    insert_at = 0
    for index, line in enumerate(lines):
        if re.match(r"^\s*#\s*include\b", line):
            insert_at = index + 1
        elif insert_at and line.strip() and not line.lstrip().startswith(("//", "/*", "*")):
            break
    block = _include_block(source_path, generated_header, newline)
    return lines[:insert_at] + block + lines[insert_at:], insert_at, len(block)


def preview_add_trace_point(
    root: str | Path,
    file_path: str | Path,
    line: int,
    event: str,
    label: str,
    value: str = "0u",
    channel: int = 1,
    point_id: str | None = None,
) -> tuple[TracePoint, str, str]:
    root_path = Path(root).resolve()
    source_path = Path(file_path).resolve()
    source_path.relative_to(root_path)
    source = _read_source(source_path)
    original = source.text
    lines = original.splitlines(keepends=True)
    if not 1 <= line <= len(lines) + 1:
        raise ValueError(f"삽입 행은 1~{len(lines) + 1} 범위여야 합니다.")
    point = TracePoint(
        id=point_id or uuid.uuid4().hex,
        file=str(source_path.relative_to(root_path)),
        line=line,
        event=event,
        label=label.strip() or event,
        value=value.strip() or "0u",
        channel=max(1, min(4, int(channel))),
    )
    generated_header = root_path / GENERATED_DIR / "cch_trace.h"
    original_target = min(len(lines), max(0, line - 1))
    lines, include_at, inserted_include_lines = _ensure_include(
        lines, source_path, generated_header, source.newline
    )
    shift = inserted_include_lines if 0 <= include_at <= original_target else 0
    target = min(len(lines), original_target + shift)
    updated_lines = lines[:target] + _managed_block(point, source.newline) + lines[target:]
    updated = "".join(updated_lines)
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=str(source_path),
        tofile=str(source_path),
    ))
    return point, updated, diff


def apply_trace_point(
    root: str | Path,
    file_path: str | Path,
    line: int,
    event: str,
    label: str,
    value: str = "0u",
    channel: int = 1,
) -> TracePoint:
    point, updated, _ = preview_add_trace_point(
        root, file_path, line, event, label, value, channel
    )
    source_path = Path(file_path).resolve()
    source = _read_source(source_path)
    _write_source(source_path, source, updated)
    points = load_trace_points(root)
    points.append(point)
    save_trace_points(root, points)
    generate_trace_runtime(root, points)
    return point


def remove_trace_point(root: str | Path, point_id: str) -> bool:
    root_path = Path(root).resolve()
    points = load_trace_points(root)
    point = next((item for item in points if item.id == point_id), None)
    if point is None:
        return False
    source_path = (root_path / point.file).resolve()
    source_path.relative_to(root_path)
    source = _read_source(source_path)
    text = source.text
    pattern = re.compile(
        rf"(?m)^[ \t]*/\* CCH-TRACE:BEGIN id={re.escape(point_id)} \*/\r?\n"
        rf".*?\r?\n"
        rf"[ \t]*/\* CCH-TRACE:END id={re.escape(point_id)} \*/\r?\n?"
    )
    updated, count = pattern.subn("", text, count=1)
    if count != 1:
        raise ValueError("관리 중인 계측 블록을 찾지 못했습니다. 소스 변경 내용을 먼저 확인하십시오.")
    remaining = [item for item in points if item.id != point_id]
    same_file = any(item.file.casefold() == point.file.casefold() for item in remaining)
    if not same_file:
        updated = re.sub(
            rf"(?m)^[ \t]*{re.escape(INCLUDE_BEGIN)}\r?\n"
            rf"[ \t]*#include[^\r\n]*\r?\n"
            rf"[ \t]*{re.escape(INCLUDE_END)}\r?\n?",
            "",
            updated,
            count=1,
        )
    _write_source(source_path, source, updated)
    save_trace_points(root_path, remaining)
    generate_trace_runtime(root_path, remaining)
    return True


def _labels_header(points: list[TracePoint]) -> str:
    lines = ["/* Trace point ID map (generated). */"]
    for point in points:
        label = re.sub(r"[^A-Za-z0-9_]", "_", point.label.upper()).strip("_") or "EVENT"
        lines.append(f"#define CCH_ID_{label}_{_event_number(point.id):04X} 0x{_event_number(point.id):04X}u")
    return "\n".join(lines)


def generate_trace_runtime(root: str | Path, points: list[TracePoint] | None = None) -> tuple[Path, Path]:
    root_path = Path(root).resolve()
    points = list(points if points is not None else load_trace_points(root_path))
    generated = root_path / GENERATED_DIR
    generated.mkdir(parents=True, exist_ok=True)
    header = generated / "cch_trace.h"
    source = generated / "cch_trace.c"
    header.write_text(_HEADER_TEMPLATE.replace("/*__CCH_IDS__*/", _labels_header(points)), encoding="utf-8")
    source.write_text(_SOURCE_TEMPLATE, encoding="utf-8")
    (generated / "README.txt").write_text(_RUNTIME_README, encoding="utf-8")
    return header, source


_HEADER_TEMPLATE = r'''#ifndef CCH_TRACE_H
#define CCH_TRACE_H

#include <stdint.h>

#ifndef CCH_TRACE_ENABLE
#define CCH_TRACE_ENABLE 1
#endif
#ifndef CCH_TRACE_USE_IAR_ITM
#define CCH_TRACE_USE_IAR_ITM 1
#endif
#ifndef CCH_TRACE_BUFFER_SIZE
#define CCH_TRACE_BUFFER_SIZE 256u
#endif
#ifndef CCH_TRACE_TIMESTAMP
#define CCH_TRACE_TIMESTAMP() (0u)
#endif
#ifndef CCH_TRACE_CONTEXT
#define CCH_TRACE_CONTEXT() (0u)
#endif
#ifndef CCH_TRACE_LIVE_PRINTF
#define CCH_TRACE_LIVE_PRINTF 0
#endif

enum {
    CCH_EVT_TASK_IN = 1,
    CCH_EVT_TASK_OUT = 2,
    CCH_EVT_ISR_ENTER = 3,
    CCH_EVT_ISR_EXIT = 4,
    CCH_EVT_FUNC_ENTER = 5,
    CCH_EVT_FUNC_EXIT = 6,
    CCH_EVT_EVENT = 7,
    CCH_EVT_ERROR = 8,
    CCH_EVT_HARDFAULT = 9
};

typedef struct {
    uint32_t sequence;
    uint32_t timestamp;
    uint16_t event;
    uint16_t context;
    uint32_t value;
} CCH_TraceRecord;

typedef struct {
    uint32_t magic;
    uint32_t exc_return;
    uint32_t msp;
    uint32_t psp;
    uint32_t r0;
    uint32_t r1;
    uint32_t r2;
    uint32_t r3;
    uint32_t r12;
    uint32_t lr;
    uint32_t pc;
    uint32_t xpsr;
    uint32_t cfsr;
    uint32_t hfsr;
    uint32_t mmfar;
    uint32_t bfar;
} CCH_FaultRecord;

void CCH_TraceWrite(uint16_t event, uint16_t context, uint32_t value);
void CCH_TraceTaskIn(uint16_t task_id);
void CCH_TraceTaskOut(uint16_t task_id);
void CCH_TraceIsrEnter(uint16_t isr_id);
void CCH_TraceIsrExit(uint16_t isr_id);
void CCH_HardFaultDump(uint32_t msp, uint32_t psp, uint32_t exc_return);
void CCH_PrintRetainedFault(void);
int CCH_FaultSwoWrite(const char *data, uint32_t length);

#if CCH_TRACE_ENABLE
#define CCH_TRACE_FUNC_ENTER(ID) CCH_TraceWrite(CCH_EVT_FUNC_ENTER, (uint16_t)CCH_TRACE_CONTEXT(), (uint32_t)(ID))
#define CCH_TRACE_FUNC_EXIT(ID) CCH_TraceWrite(CCH_EVT_FUNC_EXIT, (uint16_t)CCH_TRACE_CONTEXT(), (uint32_t)(ID))
#define CCH_TRACE_EVENT(ID, VALUE) CCH_TraceWrite(CCH_EVT_EVENT, (uint16_t)(ID), (uint32_t)(VALUE))
#define CCH_TRACE_ERROR(ID, VALUE) CCH_TraceWrite(CCH_EVT_ERROR, (uint16_t)(ID), (uint32_t)(VALUE))
#define CCH_HARDFAULT_DUMP() CCH_HardFaultDump((uint32_t)__get_MSP(), (uint32_t)__get_PSP(), (uint32_t)__get_LR())
#else
#define CCH_TRACE_FUNC_ENTER(ID) ((void)0)
#define CCH_TRACE_FUNC_EXIT(ID) ((void)0)
#define CCH_TRACE_EVENT(ID, VALUE) ((void)0)
#define CCH_TRACE_ERROR(ID, VALUE) ((void)0)
#define CCH_HARDFAULT_DUMP() ((void)0)
#endif

#if CCH_TRACE_ENABLE && CCH_TRACE_USE_IAR_ITM && defined(__ICCARM__)
#include <arm_itm.h>
#define CCH_TRACE_IAR_EVENT(CH, ID, VALUE) do { \
    CCH_TraceWrite(CCH_EVT_EVENT, (uint16_t)(ID), (uint32_t)(VALUE)); \
    ITM_EVENT32_WITH_PC((CH), (VALUE)); \
} while (0)
#else
#define CCH_TRACE_IAR_EVENT(CH, ID, VALUE) CCH_TRACE_EVENT((ID), (VALUE))
#endif

/*
 * FreeRTOS trace hook adapter (opt-in).
 * FreeRTOSConfig.h에서 CCH_TRACE_ENABLE_FREERTOS_HOOKS=1을 정의한 경우에만
 * 아래 기본 hook을 제공합니다. 프로젝트가 이미 같은 hook을 정의했다면
 * 기존 hook 안에서 CCH_TraceTaskIn/Out을 직접 호출하십시오.
 */
#if defined(CCH_TRACE_ENABLE_FREERTOS_HOOKS) && CCH_TRACE_ENABLE_FREERTOS_HOOKS
#ifndef CCH_TRACE_TASK_ID
#define CCH_TRACE_TASK_ID(TCB) ((uint16_t)((uintptr_t)(TCB) & 0xFFFFu))
#endif
#ifndef traceTASK_SWITCHED_IN
#define traceTASK_SWITCHED_IN() CCH_TraceTaskIn(CCH_TRACE_TASK_ID(pxCurrentTCB))
#endif
#ifndef traceTASK_SWITCHED_OUT
#define traceTASK_SWITCHED_OUT() CCH_TraceTaskOut(CCH_TRACE_TASK_ID(pxCurrentTCB))
#endif
#endif

/*__CCH_IDS__*/

#endif
'''


_SOURCE_TEMPLATE = r'''#include "cch_trace.h"

#if CCH_TRACE_LIVE_PRINTF
#include <stdio.h>
#endif

#define CCH_FAULT_MAGIC 0x43434846u
#define CCH_ITM_TCR (*(volatile uint32_t *)0xE0000E80u)
#define CCH_ITM_TER (*(volatile uint32_t *)0xE0000E00u)
#define CCH_ITM_PORT0 (*(volatile uint32_t *)0xE0000000u)
#define CCH_SCB_CFSR (*(volatile uint32_t *)0xE000ED28u)
#define CCH_SCB_HFSR (*(volatile uint32_t *)0xE000ED2Cu)
#define CCH_SCB_MMFAR (*(volatile uint32_t *)0xE000ED34u)
#define CCH_SCB_BFAR (*(volatile uint32_t *)0xE000ED38u)

#if defined(__ICCARM__)
#pragma location=".noinit"
__no_init static CCH_TraceRecord g_cch_buffer[CCH_TRACE_BUFFER_SIZE];
#pragma location=".noinit"
__no_init static volatile uint32_t g_cch_write_index;
#pragma location=".noinit"
__no_init static CCH_FaultRecord g_cch_fault;
#elif defined(__GNUC__)
static CCH_TraceRecord g_cch_buffer[CCH_TRACE_BUFFER_SIZE] __attribute__((section(".noinit")));
static volatile uint32_t g_cch_write_index __attribute__((section(".noinit")));
static CCH_FaultRecord g_cch_fault __attribute__((section(".noinit")));
#else
static CCH_TraceRecord g_cch_buffer[CCH_TRACE_BUFFER_SIZE];
static volatile uint32_t g_cch_write_index;
static CCH_FaultRecord g_cch_fault;
#endif

static uint32_t cch_lock(void)
{
#if defined(__ICCARM__)
    uint32_t state = (uint32_t)__get_interrupt_state();
    __disable_interrupt();
    return state;
#elif defined(__GNUC__) && (defined(__arm__) || defined(__thumb__))
    uint32_t state;
    __asm volatile("mrs %0, primask\ncpsid i" : "=r"(state) :: "memory");
    return state;
#else
    return 0u;
#endif
}

static void cch_unlock(uint32_t state)
{
#if defined(__ICCARM__)
    __set_interrupt_state(state);
#elif defined(__GNUC__) && (defined(__arm__) || defined(__thumb__))
    __asm volatile("msr primask, %0" :: "r"(state) : "memory");
#else
    (void)state;
#endif
}

void CCH_TraceWrite(uint16_t event, uint16_t context, uint32_t value)
{
#if CCH_TRACE_ENABLE
    uint32_t state;
    uint32_t sequence;
    uint32_t timestamp;
    CCH_TraceRecord *record;
    state = cch_lock();
    sequence = g_cch_write_index++;
    timestamp = (uint32_t)CCH_TRACE_TIMESTAMP();
    record = &g_cch_buffer[sequence % CCH_TRACE_BUFFER_SIZE];
    record->sequence = sequence;
    record->timestamp = timestamp;
    record->event = event;
    record->context = context;
    record->value = value;
    cch_unlock(state);
#if CCH_TRACE_LIVE_PRINTF
    /*
     * Opt-in only: printf can be expensive or unsafe in interrupt context.
     * Enable it only when the project's retargeted stdout is verified.
     */
    (void)printf(
        "CCH|%lu|EVT|%u|CTX|%u|VAL|0x%08lX|SEQ|%lu\r\n",
        (unsigned long)timestamp,
        (unsigned int)event,
        (unsigned int)context,
        (unsigned long)value,
        (unsigned long)sequence
    );
#endif
#else
    (void)event; (void)context; (void)value;
#endif
}

void CCH_TraceTaskIn(uint16_t task_id) { CCH_TraceWrite(CCH_EVT_TASK_IN, task_id, 0u); }
void CCH_TraceTaskOut(uint16_t task_id) { CCH_TraceWrite(CCH_EVT_TASK_OUT, task_id, 0u); }
void CCH_TraceIsrEnter(uint16_t isr_id) { CCH_TraceWrite(CCH_EVT_ISR_ENTER, isr_id, 0u); }
void CCH_TraceIsrExit(uint16_t isr_id) { CCH_TraceWrite(CCH_EVT_ISR_EXIT, isr_id, 0u); }

static int cch_swo_char(char value)
{
    uint32_t timeout = 10000u;
    if ((CCH_ITM_TCR & 1u) == 0u || (CCH_ITM_TER & 1u) == 0u) {
        return 0;
    }
    while (CCH_ITM_PORT0 == 0u && timeout-- != 0u) { }
    if (timeout == 0u) {
        return 0;
    }
    *(volatile uint8_t *)0xE0000000u = (uint8_t)value;
    return 1;
}

int CCH_FaultSwoWrite(const char *data, uint32_t length)
{
    uint32_t index;
    for (index = 0u; index < length; ++index) {
        if (!cch_swo_char(data[index])) {
            return 0;
        }
    }
    return 1;
}

static void cch_puts(const char *text)
{
    const char *cursor = text;
    while (*cursor != '\0') { ++cursor; }
    (void)CCH_FaultSwoWrite(text, (uint32_t)(cursor - text));
}

static void cch_hex(uint32_t value)
{
    char digits[8];
    uint32_t index;
    static const char map[] = "0123456789ABCDEF";
    for (index = 0u; index < 8u; ++index) {
        digits[7u - index] = map[value & 0xFu];
        value >>= 4u;
    }
    (void)CCH_FaultSwoWrite(digits, 8u);
}

static void cch_dec(uint32_t value)
{
    char digits[10];
    uint32_t count = 0u;
    do {
        digits[count++] = (char)('0' + (value % 10u));
        value /= 10u;
    } while (value != 0u && count < 10u);
    while (count != 0u) {
        --count;
        (void)cch_swo_char(digits[count]);
    }
}

static void cch_print_fault(void)
{
    uint32_t count;
    uint32_t offset;
    uint32_t end = g_cch_write_index;
    cch_puts("CCH|FAULT_BEGIN\r\nCCH|FAULT_REG|PC=");
    cch_hex(g_cch_fault.pc);
    cch_puts("|LR="); cch_hex(g_cch_fault.lr);
    cch_puts("|XPSR="); cch_hex(g_cch_fault.xpsr);
    cch_puts("\r\nCCH|FAULT_REG|CFSR="); cch_hex(g_cch_fault.cfsr);
    cch_puts("|HFSR="); cch_hex(g_cch_fault.hfsr);
    cch_puts("|MMFAR="); cch_hex(g_cch_fault.mmfar);
    cch_puts("|BFAR="); cch_hex(g_cch_fault.bfar);
    cch_puts("\r\n");
    count = end < CCH_TRACE_BUFFER_SIZE ? end : CCH_TRACE_BUFFER_SIZE;
    for (offset = 0u; offset < count; ++offset) {
        CCH_TraceRecord *record = &g_cch_buffer[(end - 1u - offset) % CCH_TRACE_BUFFER_SIZE];
        cch_puts("CCH|"); cch_dec(record->timestamp);
        cch_puts("|EVT|"); cch_dec(record->event);
        cch_puts("|CTX|"); cch_dec(record->context);
        cch_puts("|VAL|0x"); cch_hex(record->value);
        cch_puts("|SEQ|"); cch_dec(record->sequence);
        cch_puts("\r\n");
    }
    cch_puts("CCH|FAULT_END\r\n");
}

void CCH_HardFaultDump(uint32_t msp, uint32_t psp, uint32_t exc_return)
{
    uint32_t *stack = (exc_return & 4u) != 0u ? (uint32_t *)psp : (uint32_t *)msp;
    g_cch_fault.magic = CCH_FAULT_MAGIC;
    g_cch_fault.exc_return = exc_return;
    g_cch_fault.msp = msp;
    g_cch_fault.psp = psp;
    if (stack != (uint32_t *)0) {
        g_cch_fault.r0 = stack[0]; g_cch_fault.r1 = stack[1];
        g_cch_fault.r2 = stack[2]; g_cch_fault.r3 = stack[3];
        g_cch_fault.r12 = stack[4]; g_cch_fault.lr = stack[5];
        g_cch_fault.pc = stack[6]; g_cch_fault.xpsr = stack[7];
    }
    g_cch_fault.cfsr = CCH_SCB_CFSR;
    g_cch_fault.hfsr = CCH_SCB_HFSR;
    g_cch_fault.mmfar = CCH_SCB_MMFAR;
    g_cch_fault.bfar = CCH_SCB_BFAR;
    CCH_TraceWrite(CCH_EVT_HARDFAULT, 0u, g_cch_fault.pc);
    cch_print_fault();
}

void CCH_PrintRetainedFault(void)
{
    if (g_cch_fault.magic == CCH_FAULT_MAGIC) {
        cch_print_fault();
        g_cch_fault.magic = 0u;
    }
}
'''


_RUNTIME_README = """C Call Hierarchy Explorer Trace Runtime
=======================================

1. cch_trace.c를 펌웨어 빌드 대상에 추가하고 CCH_Trace 폴더를 include 경로에 넣습니다.
2. CCH_TRACE_TIMESTAMP()를 마이크로초 또는 CPU tick을 반환하는 식으로 정의합니다.
   UART/retarget stdout을 통한 실시간 I/O Timeline이 필요하면
   CCH_TRACE_LIVE_PRINTF=1을 정의하십시오. printf의 ISR 안전성과 실행 시간을
   먼저 확인해야 하며, 기본값은 안전을 위해 0입니다.
3. 프로그램 시작 시 CCH_PrintRetainedFault()를 한 번 호출하면 이전 HardFault 기록을
   SWO ITM 포트 0으로 최근 실행부터 과거 순서로 재출력합니다.
4. HardFault_Handler 안의 안전한 위치에는 Trace 센터에서 HARDFAULT_DUMP 계측점을
   사용자가 직접 선택하고 변경 미리보기를 승인한 뒤 삽입하십시오.
5. FreeRTOS task switch 기록은 FreeRTOSConfig.h에서
   CCH_TRACE_ENABLE_FREERTOS_HOOKS=1로 활성화할 수 있습니다. 기존
   traceTASK_SWITCHED_IN/OUT hook이 있으면 덮어쓰지 않으므로 그 hook에서
   CCH_TraceTaskIn/Out을 직접 호출하십시오.
6. IAR ITM Event는 자동 삽입하지 않습니다. Trace 센터에서 파일, 함수, 행, 채널,
   값 식을 선택하고 미리보기와 확인 대화상자를 승인한 위치에만 삽입합니다.

주의:
- 계측 전 소스 관리 상태를 확인하고 먼저 빌드하십시오.
- HardFault 경로에서는 동적 메모리와 일반 printf에 의존하지 않으며 SWO 전송은
  제한 시간 후 중단됩니다.
- 링 버퍼와 fault record는 .noinit 섹션을 사용합니다. IAR linker 설정에서
  해당 섹션이 초기화되지 않도록 유지하십시오.
"""
