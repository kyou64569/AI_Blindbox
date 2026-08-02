const { execSync } = require("child_process");
const fs = require("fs");
const vm = require("vm");

function extractAndCheck(label, htmlContent) {
  const m = htmlContent.match(/<script>\s*([\s\S]*?)<\/script>/);
  if (!m) { console.log(label + ": NO_INLINE_SCRIPT"); return false; }
  const code = m[1];
  try {
    new vm.Script(code, { filename: label + ".js" });
    console.log(label + ": SYNTAX_OK (len=" + code.length + ")");
    return true;
  } catch (e) {
    console.log(label + ": SYNTAX_ERROR: " + e.message);
    return false;
  }
}

// current
const cur = fs.readFileSync("D:/work/AI_Blindbox/frontend/demo.html", "utf8");
extractAndCheck("CURRENT", cur);

// committed HEAD
try {
  const out = execSync('git show HEAD:frontend/demo.html', { cwd: "D:/work/AI_Blindbox" }).toString();
  extractAndCheck("COMMITTED_HEAD", out);
} catch (e) {
  console.log("COMMITTED_HEAD: could not read (" + e.message + ")");
}
