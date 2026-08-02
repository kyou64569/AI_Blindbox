const fs = require("fs");
const src = fs.readFileSync("frontend/demo.html", "utf8");
const m = src.match(/<script>\s*([\s\S]*?)<\/script>/);
const code = m[1];
const lines = code.split("\n");

let i = 0;
let lineNo = 1;
let inTemplate = false;       // inside a `...` template literal (top-level)
let parenDepth = 0;
let braceDepth = 0;
let inLineComment = false;

// We track template-literal state char by char, ignoring ${} that are inside templates.
// Simpler: scan and track backtick parity, and ${ } depth separately, but ${} inside
// a template does NOT toggle template state. We'll track a stack of template boundaries.
let templateOpenLine = -1;
let stack = []; // each entry: {type:'tpl'|'{', line}

for (let idx = 0; idx < code.length; idx++) {
  const ch = code[idx];
  if (ch === "\n") { lineNo++; continue; }
  if (ch === "`") {
    if (stack.length && stack[stack.length - 1].type === "tpl") {
      stack.pop(); // close template
    } else {
      stack.push({ type: "tpl", line: lineNo });
      templateOpenLine = lineNo;
    }
    continue;
  }
  // only track braces/parens when NOT inside a template literal
  if (stack.length && stack[stack.length - 1].type === "tpl") {
    // inside template: skip brace tracking (they belong to the template)
    continue;
  }
  if (ch === "{") {
    // could be start of ${ or a plain brace. We'll just track depth.
    stack.push({ type: "{", line: lineNo });
  } else if (ch === "}") {
    if (stack.length && stack[stack.length - 1].type === "{") {
      stack.pop();
    } else {
      console.log("UNEXPECTED } at line " + lineNo + " (stack top=" + JSON.stringify(stack[stack.length-1]) + ")");
      // print context
      console.log(lines[lineNo-1]);
      process.exit(1);
    }
  }
}

if (stack.length) {
  console.log("UNCLOSED at end. Remaining stack (last 10):");
  for (const s of stack.slice(-10)) console.log("  " + s.type + " @line " + s.line);
  // find first unclosed template
  const tpl = stack.find(s => s.type === "tpl");
  if (tpl) console.log("FIRST UNCLOSED TEMPLATE opened at line " + tpl.line + ": " + lines[tpl.line-1]);
} else {
  console.log("BALANCED (brace/paren view). NOTE: this ignores ${} nesting inside templates.");
}
