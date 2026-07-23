const fs = require("fs");

const html = fs.readFileSync("CCodeTree.hta", "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) {
    throw new Error("HTA inline script was not found");
}
new Function(match[1]);
console.log("PASS: HTA inline JavaScript syntax");
