import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import "./styles.css";

// 全局兜底：捕获未被 React 错误边界兜住的运行时错误（如异步 / effect 异常），
// 在页面顶部显示可读错误信息，避免「整页变黑却毫无提示」、且无从排查。
function showFatal(msg) {
  let el = document.getElementById("__fatal");
  if (!el) {
    el = document.createElement("div");
    el.id = "__fatal";
    el.style.cssText =
      "position:fixed;left:0;right:0;top:0;z-index:99999;background:#2a0d0d;color:#ff8a8a;" +
      "font:13px/1.6 ui-monospace,Menlo,monospace;padding:14px 18px;white-space:pre-wrap;" +
      "word-break:break-word;border-bottom:1px solid #ff5c5c;max-height:50vh;overflow:auto;";
    document.body.appendChild(el);
  }
  el.textContent = "⚠️ 运行时错误：\n" + msg;
}
window.addEventListener("error", (e) => showFatal((e.error && e.error.stack) || e.message || String(e)));
window.addEventListener("unhandledrejection", (e) =>
  showFatal("Unhandled Promise: " + ((e.reason && (e.reason.stack || e.reason.message)) || e.reason))
);

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
