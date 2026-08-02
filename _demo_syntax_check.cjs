const fs = require("fs");
const vm = require("vm");
const path = require("path");

const htmlPath = "D:/work/AI_Blindbox/frontend/demo.html";
const src = fs.readFileSync(htmlPath, "utf8");

// 匹配无属性的 <script>...</script>（内联脚本，排除 vendor src 的）
const m = src.match(/<script>\s*([\s\S]*?)<\/script>/);
if (!m) {
  console.log("NO_INLINE_SCRIPT_FOUND");
  process.exit(2);
}
const code = m[1];
fs.writeFileSync("D:/work/AI_Blindbox/_demo_inline_extracted.js", code, "utf8");
try {
  new vm.Script(code, { filename: "demo_inline.js" });
  console.log("SYNTAX_OK inline script length=" + code.length);
} catch (e) {
  console.log("SYNTAX_ERROR: " + e.message);
  if (e.stack) {
    const lineMatch = e.stack.match(/demo_inline\.js:(\d+)/);
    if (lineMatch) {
      const ln = parseInt(lineMatch[1], 10);
      const lines = code.split("\n");
      for (let i = Math.max(0, ln - 3); i < Math.min(lines.length, ln + 2); i++) {
        console.log((i + 1) + ": " + lines[i]);
      }
    }
  }
  process.exit(1);
}
