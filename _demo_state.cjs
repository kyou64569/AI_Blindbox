const fs = require("fs");
const src = fs.readFileSync("frontend/demo.html", "utf8");
const m = src.match(/<script>\s*([\s\S]*?)<\/script>/);
const code = m[1];
const lines = code.split("\n");

// stack of frames: {state:"NORMAL"|"TEMPLATE"|"EXPR", line:Number}
let stack = [{ state: "NORMAL", line: 0 }];
let lineNo = 1;

for (let i = 0; i < code.length; i++) {
  const ch = code[i];
  const nxt = code[i + 1];
  if (ch === "\n") { lineNo++; continue; }
  const top = stack[stack.length - 1];

  if (ch === "`") {
    if (top.state === "TEMPLATE") {
      stack.pop();
    } else {
      stack.push({ state: "TEMPLATE", line: lineNo });
    }
    continue;
  }
  if (top.state === "TEMPLATE") {
    if (ch === "$" && nxt === "{") {
      stack.push({ state: "EXPR", line: lineNo });
      i++; // consume '{'
    }
    continue;
  }
  // NORMAL or EXPR
  if (ch === "{") {
    stack.push({ state: "EXPR", line: lineNo });
  } else if (ch === "}") {
    if (top.state === "EXPR") {
      stack.pop();
    } else {
      console.log("UNEXPECTED } at line " + lineNo + ": " + lines[lineNo - 1].trim());
      console.log("stack: " + JSON.stringify(stack.map(s => s.state + "@" + s.line)));
      process.exit(1);
    }
  }
}

if (stack.length !== 1 || stack[0].state !== "NORMAL") {
  console.log("UNCLOSED at EOF. Frames (top->bottom):");
  for (let k = stack.length - 1; k >= 0; k--) console.log("  " + stack[k].state + " @line " + stack[k].line);
  // the deepest unclosed TEMPLATE is the one whose close is missing first
  const tplFrames = stack.filter(s => s.state === "TEMPLATE");
  if (tplFrames.length) {
    const first = tplFrames[0];
    console.log("FIRST UNCLOSED TEMPLATE opened at line " + first.line + ":");
    console.log("  >> " + lines[first.line - 1].trim());
  }
} else {
  console.log("BALANCED: all templates and braces matched.");
}
