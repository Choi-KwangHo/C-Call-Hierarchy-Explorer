const fs = require("fs");
const path = require("path");
const CAnalyzer = require("../CAnalyzer.js").CAnalyzer;

const root = process.argv[2];
if (!root) {
    console.error("Usage: node tests/diagnose_project.js <source-folder>");
    process.exit(2);
}
const excluded = new Set([".git", ".svn", ".hg", "node_modules", "dist", "build", ".vs"]);
const files = [];
function walk(folder) {
    for (const entry of fs.readdirSync(folder, { withFileTypes: true })) {
        const full = path.join(folder, entry.name);
        if (entry.isDirectory() && !excluded.has(entry.name.toLowerCase())) walk(full);
        else if (entry.isFile() && /\.(c|h)$/i.test(entry.name)) files.push({ path: full, text: fs.readFileSync(full, "utf8") });
    }
}
walk(root);
const result = CAnalyzer.analyzeFiles(files, root);
const mains = result.functions.filter(fn => fn.name.toLowerCase() === "main");
function reachableDepth(start) {
    const queue = [{ fn:start, depth:1 }];
    const seen = new Set();
    let maximum = 1;
    while (queue.length) {
        const current = queue.shift();
        if (seen.has(current.fn.id)) continue;
        seen.add(current.fn.id);
        maximum = Math.max(maximum, current.depth);
        for (const ref of current.fn.calls) {
            if (ref.charAt(0) === "?") continue;
            const target = CAnalyzer.findFunction(result, ref);
            if (target && !seen.has(target.id)) queue.push({ fn:target, depth:current.depth + 1 });
        }
    }
    return maximum;
}
for (const main of mains) {
    const appRef = main.calls.find(ref => {
        const target = ref.charAt(0) === "?" ? null : CAnalyzer.findFunction(result, ref);
        return ref === "?App_Task" || (target && target.name === "App_Task");
    });
    if (!appRef) continue;
    const app = appRef.charAt(0) === "?" ? null : CAnalyzer.findFunction(result, appRef);
    console.log(JSON.stringify({
        main: CAnalyzer.relativePath(main.path, root) + ":" + main.startLine,
        appTaskResolved: Boolean(app),
        appTask: app ? CAnalyzer.relativePath(app.path, root) + ":" + app.startLine : "unresolved",
        appTaskCalls: app ? app.calls.length : 0,
        mainReachableDepth: reachableDepth(main),
        appTaskReachableDepth: app ? reachableDepth(app) : 0
    }));
}
const noCallers = result.functions.filter(fn => fn.callers.length === 0 && fn.name.toLowerCase() !== "main").length;
const selected = mains.find(fn => !fn.path.toLowerCase().includes("\\backup\\")) || mains[0];
const selectedPath = selected ? selected.path.toLowerCase() : "";
const coreAt = selectedPath.indexOf("\\core\\");
const projectPrefix = coreAt > 0 ? selectedPath.substring(0, coreAt) : path.dirname(selectedPath);
const interruptRoots = result.functions.filter(fn => {
    const declaration = fn.declaration || "";
    return fn.path.toLowerCase().startsWith(projectPrefix + "\\") &&
        (/(^|_)(isr|irq|nmi)(_|$)/i.test(fn.name) || /(irqhandler|_handler|isr)$/i.test(fn.name) || /\b(__interrupt|interrupt|__irq)\b/i.test(declaration));
}).length;
console.log(JSON.stringify({ files: result.files.length, functions: result.functions.length, unresolved: result.unresolved.length, noCallerFunctions: noCallers, defaultInterruptRoots: interruptRoots }));
