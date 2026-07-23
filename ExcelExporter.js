(function (global) {
    "use strict";

    function repeatText(text, count) {
        var out = "";
        while (count > 0) { out += text; count -= 1; }
        return out;
    }

    function findFunction(result, id) {
        return global.CAnalyzer.findFunction(result, id);
    }

    function functionSheetData(result) {
        var rows = [];
        var groups = [];
        var i, j, file, fn, start;
        for (i = 0; i < result.files.length; i += 1) {
            file = result.files[i];
            rows.push([0, file.relativePath, "파일", "", file.relativePath, "", "", "", file.functions.length, ""]);
            start = rows.length + 2;
            for (j = 0; j < file.functions.length; j += 1) {
                fn = file.functions[j];
                rows.push([1, fn.name + "()", "함수", fn.name, file.relativePath,
                    fn.startLine, fn.endLine, fn.declaration, fn.calls.length, fn.callers.length]);
            }
            if (file.functions.length > 0) {
                groups.push({ start: start, end: rows.length + 1, depth: 1 });
            }
        }
        return { rows: rows, groups: groups };
    }

    function callSheetData(result, selectedMainId, includeOtherRoots) {
        var rows = [];
        var groups = [];
        var maxRows = 30000;
        var maxDepth = 7; // Excel outline supports at most eight levels.

        function isMainEntry(fn) {
            var name = fn.name.toLowerCase();
            return name === "main" || name === "winmain" || name === "wwinmain" || name === "_tmain" ||
                name === "app_main" || name === "main_loop" || name === "mainloop" || name === "main_task";
        }
        function isInterruptEntry(fn) {
            return /(^|_)(isr|irq|nmi)(_|$)/i.test(fn.name) || /(irqhandler|_handler|isr)$/i.test(fn.name) ||
                /\b(__interrupt|interrupt|__irq)\b/i.test(fn.declaration);
        }
        function projectPrefixForMain(fn) {
            var value = String(fn.path || "").replace(/\//g, "\\").toLowerCase();
            var markers = ["\\core\\", "\\source\\", "\\src\\"];
            var position = -1, i;
            for (i = 0; i < markers.length; i += 1) {
                position = value.indexOf(markers[i]);
                if (position > 0) { break; }
            }
            return position > 0 ? value.substring(0, position) : value.substring(0, value.lastIndexOf("\\"));
        }
        function belongsToSelectedProject(fn, mainFn) {
            var path, prefix;
            if (!mainFn) { return true; }
            path = String(fn.path || "").replace(/\//g, "\\").toLowerCase();
            prefix = projectPrefixForMain(mainFn);
            return path.indexOf(prefix + "\\") === 0;
        }
        function markReachable(root, marked) {
            var stack = [root], fn, i, target;
            while (stack.length) {
                fn = stack.pop();
                if (!fn || marked[fn.id]) { continue; }
                marked[fn.id] = true;
                for (i = 0; i < fn.calls.length; i += 1) {
                    if (fn.calls[i].charAt(0) !== "?") {
                        target = findFunction(result, fn.calls[i]);
                        if (target && !marked[target.id]) { stack.push(target); }
                    }
                }
            }
        }
        function entryGroups() {
            var output = [
                { title:"MAIN LOOP 시작점", roots:[] },
                { title:"인터럽트 / ISR 독립 시작점", roots:[] },
                { title:"기타 독립 시작점", roots:[] }
            ];
            var marked = {}, i, fn, selectedMain = selectedMainId ? findFunction(result, selectedMainId) : null;
            if (selectedMain) { output[0].roots.push(selectedMain); }
            for (i = 0; i < result.functions.length; i += 1) {
                fn = result.functions[i];
                if (!isMainEntry(fn) && isInterruptEntry(fn) && belongsToSelectedProject(fn, selectedMain)) { output[1].roots.push(fn); }
            }
            for (i = 0; i < output[0].roots.length; i += 1) { markReachable(output[0].roots[i], marked); }
            for (i = 0; i < output[1].roots.length; i += 1) { markReachable(output[1].roots[i], marked); }
            if (includeOtherRoots) {
                for (i = 0; i < result.functions.length; i += 1) {
                    fn = result.functions[i];
                    if (!isMainEntry(fn) && !marked[fn.id] && fn.callers.length === 0) { output[2].roots.push(fn); markReachable(fn, marked); }
                }
                for (i = 0; i < result.functions.length; i += 1) {
                    fn = result.functions[i];
                    if (!isMainEntry(fn) && !marked[fn.id]) { output[2].roots.push(fn); markReachable(fn, marked); }
                }
            }
            return output;
        }

        function addExternal(name, caller, depth) {
            rows.push([depth, repeatText("  ", depth) + name + "()", "외부/미확인",
                name, caller ? caller.file : "", "", "", "정의를 찾지 못함"]);
        }

        function addFunction(fn, depth, stack, state, visited) {
            var childStart, childEnd, i, ref, target, nextStack;
            if (rows.length >= maxRows) { return; }
            if (visited[fn.id]) {
                rows.push([depth, repeatText("  ", depth) + fn.name + "()", "참조",
                    fn.name, fn.file, fn.startLine, fn.endLine, "위에서 전개됨"]);
                return;
            }
            visited[fn.id] = true;
            rows.push([depth, repeatText("  ", depth) + fn.name + "()", state || "함수",
                fn.name, fn.file, fn.startLine, fn.endLine, fn.declaration]);
            if (depth >= maxDepth && fn.calls.length > 0) {
                rows.push([depth + 1, repeatText("  ", depth + 1) + "…", "깊이 제한", "", fn.file, "", "", "Excel 윤곽선 8단계 제한"]);
                return;
            }
            childStart = rows.length + 2;
            nextStack = stack + "|" + fn.id + "|";
            for (i = 0; i < fn.calls.length && rows.length < maxRows; i += 1) {
                ref = fn.calls[i];
                if (ref.charAt(0) === "?") {
                    addExternal(ref.substring(1), fn, depth + 1);
                } else {
                    target = findFunction(result, ref);
                    if (!target) { continue; }
                    if (nextStack.indexOf("|" + target.id + "|") >= 0) {
                        rows.push([depth + 1, repeatText("  ", depth + 1) + target.name + "()",
                            "순환 호출", target.name, target.file, target.startLine, target.endLine, "재귀/순환 지점"]);
                    } else {
                        addFunction(target, depth + 1, nextStack, "호출", visited);
                    }
                }
            }
            childEnd = rows.length + 1;
            if (childEnd >= childStart) {
                groups.push({ start: childStart, end: childEnd, depth: depth + 1 });
            }
        }

        var i, j, entry, groupStart, roots = entryGroups();
        for (i = 0; i < roots.length && rows.length < maxRows; i += 1) {
            if (!roots[i].roots.length) { continue; }
            rows.push([0, roots[i].title, "시작점 그룹", "", "", "", "", roots[i].roots.length + "개 시작점"]);
            groupStart = rows.length + 2;
            for (j = 0; j < roots[i].roots.length && rows.length < maxRows; j += 1) {
                entry = roots[i].roots[j];
                addFunction(entry, 1, "", "시작 함수", {});
            }
            if (rows.length + 1 >= groupStart) { groups.push({ start:groupStart, end:rows.length + 1, depth:1 }); }
        }
        if (rows.length >= maxRows) {
            rows.push([0, "출력이 30,000행에서 제한되었습니다.", "출력 제한", "", "", "", "", ""]);
        }
        return { rows: rows, groups: groups };
    }

    function writeSheet(sheet, title, headers, data) {
        var i, j, row, group;
        sheet.Name = title;
        for (j = 0; j < headers.length; j += 1) {
            sheet.Cells(1, j + 1).Value = headers[j];
        }
        for (i = 0; i < data.rows.length; i += 1) {
            row = data.rows[i];
            for (j = 0; j < row.length; j += 1) {
                sheet.Cells(i + 2, j + 1).Value = row[j];
            }
            if (row[0] > 0 && row[0] < 16) { sheet.Cells(i + 2, 2).IndentLevel = row[0]; }
        }
        sheet.Range(sheet.Cells(1, 1), sheet.Cells(1, headers.length)).Font.Bold = true;
        sheet.Range(sheet.Cells(1, 1), sheet.Cells(1, headers.length)).Interior.Color = 0xD9EAD3;
        sheet.Range(sheet.Cells(1, 1), sheet.Cells(data.rows.length + 1, headers.length)).VerticalAlignment = -4160;
        sheet.Range(sheet.Cells(1, 1), sheet.Cells(data.rows.length + 1, headers.length)).AutoFilter();
        sheet.Columns(1).ColumnWidth = 8;
        sheet.Columns(2).ColumnWidth = 42;
        sheet.Columns(3).ColumnWidth = 14;
        sheet.Columns(4).ColumnWidth = 24;
        sheet.Columns(5).ColumnWidth = 42;
        sheet.Columns(6).ColumnWidth = 10;
        sheet.Columns(7).ColumnWidth = 10;
        sheet.Columns(8).ColumnWidth = 70;
        sheet.Columns(8).WrapText = true;
        sheet.Outline.SummaryRow = 0; // summary row is above its detail rows
        data.groups.sort(function (a, b) { return b.depth - a.depth; });
        for (i = 0; i < data.groups.length; i += 1) {
            group = data.groups[i];
            try { sheet.Rows(group.start + ":" + group.end).Group(); } catch (ignore) {}
        }
    }

    function exportWorkbook(result, savePath, selectedMainId, includeOtherRoots) {
        var excel = new ActiveXObject("Excel.Application");
        var workbook = excel.Workbooks.Add();
        var functions = functionSheetData(result);
        var calls = callSheetData(result, selectedMainId, includeOtherRoots);
        excel.DisplayAlerts = false;
        while (workbook.Worksheets.Count < 2) { workbook.Worksheets.Add(); }
        while (workbook.Worksheets.Count > 2) { workbook.Worksheets(workbook.Worksheets.Count).Delete(); }
        writeSheet(workbook.Worksheets(1), "함수 트리",
            ["레벨", "트리", "유형", "함수", "파일", "시작 줄", "종료 줄", "선언", "호출 수", "호출자 수"], functions);
        writeSheet(workbook.Worksheets(2), "호출 관계",
            ["레벨", "호출 트리", "유형", "함수", "파일", "시작 줄", "종료 줄", "선언/상태"], calls);
        workbook.Worksheets(1).Activate();
        excel.ActiveWindow.SplitRow = 1;
        excel.ActiveWindow.FreezePanes = true;
        workbook.SaveAs(savePath, 51); // xlOpenXMLWorkbook (.xlsx)
        excel.DisplayAlerts = true;
        excel.Visible = true;
        return savePath;
    }

    global.ExcelTreeExporter = { exportWorkbook: exportWorkbook };
}(this));
