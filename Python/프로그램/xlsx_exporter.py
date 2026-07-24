from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path
from typing import Callable

from analyzer import AnalysisResult, CallView


_INVALID_XML = re.compile("[\x00-\x08\x0B\x0C\x0E-\x1F\uFFFE\uFFFF]")
_EXCEL_CELL_CHARACTER_LIMIT = 32_000


def _excel_text(value: object) -> str:
    cleaned = _INVALID_XML.sub("", str(value))
    if len(cleaned) <= _EXCEL_CELL_CHARACTER_LIMIT and all(ord(char) <= 0xFFFF for char in cleaned):
        return cleaned
    output: list[str] = []
    units = 0
    for char in cleaned:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}:
            continue
        char_units = 2 if codepoint > 0xFFFF else 1
        if units + char_units > _EXCEL_CELL_CHARACTER_LIMIT:
            break
        output.append(char)
        units += char_units
    return "".join(output)


def _xml(value: object) -> str:
    return html.escape(_excel_text(value), quote=False)


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(row: int, column: int, value: object, style: int = 0) -> str:
    reference = f"{_column_name(column)}{row}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    return f'<c r="{reference}" s="{style}" t="inlineStr"><is><t>{_xml(value)}</t></is></c>'


def _sheet(
    rows: list[list[tuple[object, int]]],
    widths: list[int],
    freeze: bool = True,
    merges: list[str] | None = None,
) -> str:
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )
    body = []
    for row_number, row in enumerate(rows, 1):
        cells = "".join(_cell(row_number, column, value, style) for column, (value, style) in enumerate(row, 1))
        body.append(f'<row r="{row_number}">{cells}</row>')
    pane = '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>' if freeze else ""
    merge_xml = ""
    if merges:
        merge_xml = f'<mergeCells count="{len(merges)}">' + "".join(f'<mergeCell ref="{reference}"/>' for reference in merges) + '</mergeCells>'
    dimension = f"A1:{_column_name(max((len(row) for row in rows), default=1))}{max(1, len(rows))}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/><sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>'
        f'<cols>{columns}</cols><sheetData>{"".join(body)}</sheetData>'
        f'<autoFilter ref="A1:{_column_name(max((len(row) for row in rows), default=1))}1"/>'
        f'{merge_xml}'
        '</worksheet>'
    )


def export_xlsx(
    path: str,
    result: AnalysisResult,
    view: CallView,
    progress: Callable[[int, int, str], None] | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    total = len(view.rows) + len(result.functions) + 4
    completed = 0

    def report(detail: str, force: bool = False) -> None:
        if progress and (force or completed % 250 == 0):
            progress(completed, total, detail)

    report("호출 관계 행 준비", True)
    depth_count = max(1, view.max_depth)
    call_rows: list[list[tuple[object, int]]] = [
        [(f"{depth}단계", 1) for depth in range(1, depth_count + 1)]
        + [("종류", 1), ("파일", 1), ("행", 1)]
    ]
    excel_row_by_view: dict[int, int] = {}
    section_rows: list[int] = []
    for view_index, row in enumerate(view.rows):
        if row.kind == "section":
            values = [(row.title, 2)] + [("", 2)] * (depth_count - 1)
            call_rows.append(values + [("", 2), ("", 2), ("", 2)])
            excel_row_by_view[view_index] = len(call_rows)
            section_rows.append(len(call_rows))
        elif row.kind == "function":
            values = [("", 3 if index % 2 else 4) for index in range(1, depth_count + 1)]
            values[row.depth - 1] = (row.name + "()", 5)
            multiple_calls = len(row.call_lines) > 1 and bool(row.call_file)
            display_file = row.call_file if multiple_calls else row.file
            display_line: object = ", ".join(map(str, row.call_lines)) if multiple_calls else row.line
            call_rows.append(values + [(row.state, 0), (display_file, 0), (display_line, 0)])
            excel_row_by_view[view_index] = len(call_rows)
        completed += 1
        report("호출 관계 행 준비")

    subtree_end: dict[int, int] = {}
    open_nodes: list[tuple[int, int]] = []
    last_function = -1
    for view_index, row in enumerate(view.rows):
        if row.kind != "function":
            while open_nodes:
                _, parent = open_nodes.pop()
                subtree_end[parent] = max(parent, last_function)
            last_function = -1
            continue
        while open_nodes and open_nodes[-1][0] >= row.depth:
            _, parent = open_nodes.pop()
            subtree_end[parent] = max(parent, last_function)
        open_nodes.append((row.depth, view_index))
        last_function = view_index
    while open_nodes:
        _, parent = open_nodes.pop()
        subtree_end[parent] = max(parent, last_function)

    call_merges: list[str] = []
    last_column = depth_count + 3
    for row_number in section_rows:
        call_merges.append(f"A{row_number}:{_column_name(last_column)}{row_number}")
    for view_index, end_index in subtree_end.items():
        if end_index <= view_index:
            continue
        start_row = excel_row_by_view.get(view_index)
        end_row = excel_row_by_view.get(end_index)
        if not start_row or not end_row or end_row <= start_row:
            continue
        column = _column_name(view.rows[view_index].depth)
        call_merges.append(f"{column}{start_row}:{column}{end_row}")

    for view_index, excel_row in excel_row_by_view.items():
        row = view.rows[view_index]
        if row.kind != "function":
            continue
        if row.depth >= depth_count:
            continue
        start_column = row.depth + 1
        end_column = depth_count
        for column_index in range(start_column - 1, end_column):
            call_rows[excel_row - 1][column_index] = ("", 6)
        if start_column < end_column:
            call_merges.append(
                f"{_column_name(start_column)}{excel_row}:{_column_name(end_column)}{excel_row}"
            )

    function_rows: list[list[tuple[object, int]]] = [[
        ("파일", 1), ("함수", 1), ("시작 행", 1), ("종료 행", 1),
        ("함수 행 수", 1), ("선언", 1), ("호출 수", 1), ("호출자 수", 1),
    ]]
    function_merges: list[str] = []
    sorted_functions = sorted(
        result.functions,
        key=lambda item: (item.file.casefold(), item.path.casefold(), item.start_line),
    )
    group_start = 0
    previous_file_key = ""
    for index, function in enumerate(sorted_functions):
        file_key = function.file.casefold()
        if file_key != previous_file_key:
            if index - group_start > 1:
                function_merges.append(f"A{group_start + 2}:A{index + 1}")
            group_start = index
            previous_file_key = file_key
        function_rows.append([
            (function.file if index == group_start else "", 7 if index == group_start else 0),
            (function.name, 0), (function.start_line, 0),
            (function.end_line, 0), (function.end_line - function.start_line + 1, 0),
            (function.declaration, 0),
            (len(function.calls), 0), (len(function.callers), 0),
        ])
        completed += 1
        report("함수 목록 행 준비")
    if len(sorted_functions) - group_start > 1:
        function_merges.append(f"A{group_start + 2}:A{len(sorted_functions) + 1}")

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="호출 관계" sheetId="1" r:id="rId1"/><sheet name="함수 목록" sheetId="2" r:id="rId2"/></sheets></workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="10"/><name val="Malgun Gothic"/></font><font><b/><sz val="10"/><name val="Malgun Gothic"/></font></fonts>
<fills count="7"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD7E5EF"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFF0D5"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF8FBFD"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEDF4F8"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE2E6E9"/></patternFill></fill></fills>
<borders count="2"><border/><border><left style="thin"><color rgb="FFBDCBD6"/></left><right style="thin"><color rgb="FFBDCBD6"/></right><top style="thin"><color rgb="FFBDCBD6"/></top><bottom style="thin"><color rgb="FFBDCBD6"/></bottom></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFill="1"/><xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFill="1"/><xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1"/><xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1"/><xf numFmtId="0" fontId="1" fillId="5" borderId="1" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="top"/></xf><xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFill="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''

    sheet1 = _sheet(call_rows, [28] * depth_count + [18, 32, 10], merges=call_merges)
    completed += 1
    report("호출 관계 시트 생성", True)
    sheet2 = _sheet(
        function_rows,
        [32, 28, 12, 12, 14, 80, 12, 12],
        merges=function_merges,
    )
    completed += 1
    report("함수 목록 시트 생성", True)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", relationships)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
            archive.writestr("xl/styles.xml", styles)
            archive.writestr("xl/worksheets/sheet1.xml", sheet1)
            archive.writestr("xl/worksheets/sheet2.xml", sheet2)
        completed += 1
        report("Excel 파일 기록", True)
        temporary.replace(target)
        completed += 1
        report("저장 완료", True)
    finally:
        if temporary.exists():
            temporary.unlink()
