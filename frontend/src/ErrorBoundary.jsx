import React from "react";

/**
 * 错误边界：捕获子树渲染/生命周期（含 effect）中抛出的异常，
 * 避免整棵 React 树被卸载导致「整页变黑、什么也不显示」。
 * 同时把错误信息和堆栈显示在页面上，便于快速定位根因。
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // 同时打到控制台，方便开发者工具查看完整堆栈
    console.error("[ErrorBoundary] 捕获到渲染异常:", error, info);
  }

  render() {
    if (this.state.error) {
      const e = this.state.error;
      return (
        <div
          style={{
            padding: 24,
            color: "#ff8a8a",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontSize: 13,
            lineHeight: 1.7,
          }}
        >
          <h2 style={{ color: "#ff5c5c", marginTop: 0 }}>⚠️ 页面渲染出错（已被错误边界捕获，未导致整页崩溃）</h2>
          <div style={{ marginBottom: 10 }}>{String(e && e.message ? e.message : e)}</div>
          {e && e.stack ? (
            <div style={{ opacity: 0.75, borderTop: "1px solid #444", paddingTop: 10 }}>
              {e.stack}
            </div>
          ) : null}
          <div style={{ marginTop: 14, opacity: 0.7 }}>
            请按 F12 打开控制台复制完整报错，或把上方红字反馈给开发者。
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
