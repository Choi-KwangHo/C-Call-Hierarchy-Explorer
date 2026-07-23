var fso = new ActiveXObject("Scripting.FileSystemObject");

function readText(path) {
    var stream = new ActiveXObject("ADODB.Stream");
    stream.Type = 2;
    stream.Charset = "utf-8";
    stream.Open();
    stream.LoadFromFile(fso.GetAbsolutePathName(path));
    var text = stream.ReadText(-1);
    stream.Close();
    return text;
}

eval(readText("CAnalyzer.js"));

var files = [
    { path: fso.GetAbsolutePathName("tests\\fixture_a.c"), text: readText("tests\\fixture_a.c") },
    { path: fso.GetAbsolutePathName("tests\\fixture_b.c"), text: readText("tests\\fixture_b.c") },
    { path: fso.GetAbsolutePathName("tests\\fixture_b.h"), text: readText("tests\\fixture_b.h") }
];
var result = CAnalyzer.analyzeFiles(files, fso.GetAbsolutePathName("tests"));

function fail(message) { WScript.Echo("FAIL: " + message); WScript.Quit(1); }
function find(name) {
    var i;
    for (i = 0; i < result.functions.length; i += 1) {
        if (result.functions[i].name === name) { return result.functions[i]; }
    }
    return null;
}
function callNames(fn, sourceResult) {
    var names = [], i, target;
    sourceResult = sourceResult || result;
    for (i = 0; i < fn.calls.length; i += 1) {
        if (fn.calls[i].charAt(0) === "?") { names.push(fn.calls[i].substring(1)); }
        else {
            target = CAnalyzer.findFunction(sourceResult, fn.calls[i]);
            if (target) { names.push(target.name); }
        }
    }
    return names.join(",");
}

if (result.functions.length !== 4) { fail("expected 4 definitions, got " + result.functions.length); }
if (!find("calculate") || !find("helper") || !find("double_value") || !find("recursive_count")) {
    fail("one or more function definitions were not found");
}
var calls = callNames(find("calculate"));
if (calls.indexOf("helper") < 0 || calls.indexOf("double_value") < 0 || calls.indexOf("printf") < 0) {
    fail("calculate call graph is incomplete: " + calls);
}
if (calls.indexOf("ignored_call") >= 0 || calls.indexOf("fake_call") >= 0) {
    fail("comment or string was incorrectly parsed as a call: " + calls);
}
if (callNames(find("recursive_count")).indexOf("recursive_count") < 0) {
    fail("recursive call was not detected");
}
var parsed = [];
var p;
for (p = 0; p < files.length; p += 1) {
    parsed.push(CAnalyzer.parseFile(files[p].path, files[p].text));
}
var session = CAnalyzer.createLinkSession(parsed, fso.GetAbsolutePathName("tests"));
var passes = 0;
while (!session.process(1)) { passes += 1; }
if (passes < 2 || session.result.functions.length !== 4) {
    fail("chunked link session did not preserve the analysis result");
}

var duplicateResult = CAnalyzer.analyzeFiles([
    { path:"C:\\root\\projectA\\Core\\main.c", text:"int main(void) { App_Task(); return 0; }" },
    { path:"C:\\root\\projectA\\App\\App.c", text:"/* interrupt control helper */\nvoid App_Task(void) { Deep_Work(); }" },
    { path:"C:\\root\\projectA\\App\\Deep.c", text:"void Deep_Work(void) { Final_Work(); }\nvoid Final_Work(void) {}" },
    { path:"C:\\root\\projectB\\App\\App.c", text:"void App_Task(void) { Wrong_Project(); }\nvoid Wrong_Project(void) {}" }
], "C:\\root");
var duplicateMain = null, linkedApp = null, d;
for (d = 0; d < duplicateResult.functions.length; d += 1) {
    if (duplicateResult.functions[d].name === "main") { duplicateMain = duplicateResult.functions[d]; }
}
if (!duplicateMain || !duplicateMain.calls.length || duplicateMain.calls[0].charAt(0) === "?") {
    fail("duplicate App_Task definitions were not resolved by path affinity");
}
linkedApp = CAnalyzer.findFunction(duplicateResult, duplicateMain.calls[0]);
if (!linkedApp || linkedApp.path.toLowerCase().indexOf("projecta") < 0 || callNames(linkedApp, duplicateResult).indexOf("Deep_Work") < 0) {
    fail("the nearest project-local App_Task definition was not selected");
}
if (linkedApp.declaration.toLowerCase().indexOf("interrupt control") >= 0) {
    fail("a leading comment leaked into the function declaration");
}
WScript.Echo("PASS: " + result.files.length + " files, " + result.functions.length + " functions, " + result.unresolved.length + " unresolved calls");
