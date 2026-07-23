(function (global) {
    "use strict";

    var CONTROL_WORDS = {
        "if": 1, "for": 1, "while": 1, "switch": 1, "return": 1,
        "sizeof": 1, "do": 1, "case": 1, "else": 1, "typedef": 1,
        "defined": 1, "alignof": 1, "_Alignof": 1, "_Generic": 1,
        "_Static_assert": 1, "__attribute__": 1, "__declspec": 1
    };

    function normalizePath(path) {
        return String(path || "").replace(/\//g, "\\").toLowerCase();
    }

    function baseName(path) {
        var parts = String(path || "").replace(/\//g, "\\").split("\\");
        return parts[parts.length - 1];
    }

    function relativePath(path, root) {
        var p = String(path || "");
        var r = String(root || "").replace(/[\\\/]+$/, "");
        if (normalizePath(p).indexOf(normalizePath(r) + "\\") === 0) {
            return p.substring(r.length + 1);
        }
        return p;
    }

    function lineNumber(text, index) {
        var line = 1;
        var i;
        for (i = 0; i < index && i < text.length; i += 1) {
            if (text.charAt(i) === "\n") { line += 1; }
        }
        return line;
    }

    function buildLineStarts(text) {
        var starts = [0];
        var i;
        for (i = 0; i < text.length; i += 1) {
            if (text.charAt(i) === "\n") { starts.push(i + 1); }
        }
        return starts;
    }

    function indexedLineNumber(starts, index) {
        var low = 0, high = starts.length - 1, middle;
        while (low <= high) {
            middle = Math.floor((low + high) / 2);
            if (starts[middle] <= index) { low = middle + 1; }
            else { high = middle - 1; }
        }
        return high + 1;
    }

    // Replace comments and string/character contents with spaces. Newlines are
    // retained so every index maps back to the original source and line number.
    function maskNonCode(source) {
        var out = [];
        var i = 0;
        var state = "code";
        var c, n;
        while (i < source.length) {
            c = source.charAt(i);
            n = i + 1 < source.length ? source.charAt(i + 1) : "";
            if (state === "code") {
                if (c === "/" && n === "/") {
                    out.push(" ", " "); i += 2; state = "lineComment"; continue;
                }
                if (c === "/" && n === "*") {
                    out.push(" ", " "); i += 2; state = "blockComment"; continue;
                }
                if (c === "\"") { out.push(" "); i += 1; state = "string"; continue; }
                if (c === "'") { out.push(" "); i += 1; state = "char"; continue; }
                out.push(c); i += 1; continue;
            }
            if (state === "lineComment") {
                if (c === "\n" || c === "\r") { out.push(c); state = "code"; }
                else { out.push(" "); }
                i += 1; continue;
            }
            if (state === "blockComment") {
                if (c === "*" && n === "/") { out.push(" ", " "); i += 2; state = "code"; continue; }
                out.push(c === "\n" || c === "\r" ? c : " "); i += 1; continue;
            }
            if (state === "string" || state === "char") {
                if (c === "\\" && n !== "") {
                    out.push(" ", n === "\n" || n === "\r" ? n : " "); i += 2; continue;
                }
                if ((state === "string" && c === "\"") || (state === "char" && c === "'")) {
                    out.push(" "); i += 1; state = "code"; continue;
                }
                out.push(c === "\n" || c === "\r" ? c : " "); i += 1;
            }
        }
        return out.join("");
    }

    function findMatching(text, openIndex, openChar, closeChar) {
        var depth = 0;
        var i, c;
        for (i = openIndex; i < text.length; i += 1) {
            c = text.charAt(i);
            if (c === openChar) { depth += 1; }
            else if (c === closeChar) {
                depth -= 1;
                if (depth === 0) { return i; }
            }
        }
        return -1;
    }

    function previousBoundary(text, index) {
        var i;
        for (i = index - 1; i >= 0; i -= 1) {
            if (text.charAt(i) === ";" || text.charAt(i) === "}" || text.charAt(i) === "{") {
                return i + 1;
            }
        }
        return 0;
    }

    function compactDeclaration(text) {
        return String(text || "").replace(/\s+/g, " ").replace(/^\s+|\s+$/g, "");
    }

    function findFunctions(path, source) {
        var masked = maskNonCode(source);
        var lineStarts = buildLineStarts(source);
        var functions = [];
        var re = /([A-Za-z_]\w*)\s*\(([^;{}()]|\([^()]*\))*\)\s*\{/g;
        var match, name, openBrace, closeBrace, boundary, declaration, openParen;
        while ((match = re.exec(masked)) !== null) {
            name = match[1];
            if (CONTROL_WORDS[name]) { continue; }
            openBrace = match.index + match[0].lastIndexOf("{");
            closeBrace = findMatching(masked, openBrace, "{", "}");
            if (closeBrace < 0) { continue; }
            boundary = previousBoundary(masked, match.index);
            declaration = compactDeclaration(masked.substring(boundary, openBrace).replace(/^[ \t]*#.*$/gm, ""));
            // A preprocessor line or assignment before the candidate normally
            // indicates a macro/control expression rather than a definition.
            if (/=\s*[^=]/.test(declaration)) { continue; }
            openParen = masked.indexOf("(", match.index);
            functions.push({
                id: "",
                name: name,
                path: path,
                file: baseName(path),
                declaration: declaration,
                parameters: compactDeclaration(source.substring(openParen + 1, masked.lastIndexOf(")", openBrace))),
                startIndex: boundary,
                bodyStart: openBrace + 1,
                bodyEnd: closeBrace,
                startLine: indexedLineNumber(lineStarts, boundary),
                endLine: indexedLineNumber(lineStarts, closeBrace),
                calls: [],
                callers: []
            });
            re.lastIndex = closeBrace + 1;
        }
        return { functions: functions, masked: masked, lineStarts: lineStarts };
    }

    function contains(array, value) {
        var i;
        for (i = 0; i < array.length; i += 1) {
            if (array[i] === value) { return true; }
        }
        return false;
    }

    function pathParts(path) {
        return normalizePath(path).split("\\");
    }

    function targetAffinity(callerPath, targetPath) {
        var caller = pathParts(callerPath);
        var target = pathParts(targetPath);
        var common = 0;
        var limit = Math.min(caller.length - 1, target.length - 1);
        while (common < limit && caller[common] === target[common]) { common += 1; }
        return common * 1000 - ((caller.length - common) + (target.length - common)) * 10;
    }

    function bestTargetForCaller(caller, targets) {
        var best = null, bestScore = -999999999, score, i;
        for (i = 0; i < targets.length; i += 1) {
            if (normalizePath(targets[i].path) === normalizePath(caller.path)) { return targets[i]; }
            score = targetAffinity(caller.path, targets[i].path);
            if (score > bestScore) { bestScore = score; best = targets[i]; }
        }
        return best;
    }

    function parseFile(path, text) {
        var parsed = findFunctions(path, text);
        return {
            path: path,
            relativePath: "",
            text: text,
            masked: parsed.masked,
            lineStarts: parsed.lineStarts,
            functions: parsed.functions
        };
    }

    // Rebuild cross-file symbol links from already parsed file records. This is
    // intentionally separate from parsing so a live monitor can reparse only
    // the file whose DateLastModified changed.
    function createLinkSession(parsedFiles, root) {
        var result = { root: root || "", files: [], functions: [], unresolved: [], warnings: [] };
        var byName = {};
        var byId = {};
        var fileByPath = {};
        var parsed, i, j, fn, key;
        for (i = 0; i < parsedFiles.length; i += 1) {
            parsed = parsedFiles[i];
            parsed.relativePath = relativePath(parsed.path, root);
            result.files.push(parsed);
            fileByPath[normalizePath(parsed.path)] = parsed;
            for (j = 0; j < parsed.functions.length; j += 1) {
                fn = parsed.functions[j];
                fn.id = "fn" + result.functions.length;
                byId[fn.id] = fn;
                fn.calls = [];
                fn.callers = [];
                result.functions.push(fn);
                key = fn.name;
                if (!byName[key]) { byName[key] = []; }
                byName[key].push(fn);
            }
        }
        result.byName = byName;
        result.byId = byId;

        function linkFunction(fn) {
            var callRe, match, callName, targets, target, sameFile;
            var parsedFile = fileByPath[normalizePath(fn.path)];
            if (!parsedFile) { return; }
            callRe = /\b([A-Za-z_]\w*)\s*\(/g;
            callRe.lastIndex = fn.bodyStart;
            while ((match = callRe.exec(parsedFile.masked)) !== null && match.index < fn.bodyEnd) {
                callName = match[1];
                if (CONTROL_WORDS[callName]) { continue; }
                targets = byName[callName] || [];
                target = null;
                sameFile = null;
                for (j = 0; j < targets.length; j += 1) {
                    if (normalizePath(targets[j].path) === normalizePath(fn.path)) { sameFile = targets[j]; break; }
                }
                if (sameFile) { target = sameFile; }
                else if (targets.length === 1) { target = targets[0]; }
                else if (targets.length > 1) { target = bestTargetForCaller(fn, targets); }
                if (target) {
                    if (!contains(fn.calls, target.id)) { fn.calls.push(target.id); }
                    if (!contains(target.callers, fn.id)) { target.callers.push(fn.id); }
                } else if (!contains(fn.calls, "?" + callName)) {
                    fn.calls.push("?" + callName);
                    result.unresolved.push({
                        callerId: fn.id,
                        name: callName,
                        line: indexedLineNumber(parsedFile.lineStarts || buildLineStarts(parsedFile.text), match.index)
                    });
                }
            }
        }

        return {
            result: result,
            index: 0,
            total: result.functions.length,
            process: function (maxFunctions) {
                var limit = Math.min(this.index + maxFunctions, this.total);
                while (this.index < limit) {
                    linkFunction(result.functions[this.index]);
                    this.index += 1;
                }
                return this.index >= this.total;
            }
        };
    }

    function analyzeParsedFiles(parsedFiles, root) {
        var session = createLinkSession(parsedFiles, root);
        while (!session.process(200)) {}
        return session.result;
    }

    function analyzeFiles(files, root) {
        var parsedFiles = [];
        var i;
        for (i = 0; i < files.length; i += 1) {
            parsedFiles.push(parseFile(files[i].path, files[i].text));
        }
        return analyzeParsedFiles(parsedFiles, root);
    }

    function findFunction(result, id) {
        var i;
        if (result && result.byId && result.byId[id]) { return result.byId[id]; }
        for (i = 0; i < result.functions.length; i += 1) {
            if (result.functions[i].id === id) { return result.functions[i]; }
        }
        return null;
    }

    global.CAnalyzer = {
        analyzeFiles: analyzeFiles,
        analyzeParsedFiles: analyzeParsedFiles,
        createLinkSession: createLinkSession,
        parseFile: parseFile,
        findFunctions: findFunctions,
        findFunction: findFunction,
        maskNonCode: maskNonCode,
        relativePath: relativePath
    };
}(this));
