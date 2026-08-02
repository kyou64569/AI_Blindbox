const esbuild = require("C:/Users/asus/AppData/Local/npm-cache/_npx/50d6b1f34829f5bc/node_modules/esbuild");
const fs = require("fs");

const files = [
  "D:/work/AI_Blindbox/frontend/src/components/ConfigPanel.jsx",
  "D:/work/AI_Blindbox/backend/_demo_main.js",
];

let allOk = true;
for (const f of files) {
  const code = fs.readFileSync(f, "utf-8");
  try {
    esbuild.transformSync(code, { loader: "jsx", sourcefile: f });
    console.log("OK   :", f);
  } catch (e) {
    allOk = false;
    console.log("FAIL :", f, "\n  ->", e.message.split("\n").slice(0, 6).join("\n  "));
  }
}
console.log(allOk ? "ALL_OK" : "HAS_ERRORS");
