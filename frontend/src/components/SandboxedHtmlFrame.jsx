import React from "react";

/**
 * 展示后端生成的 quiz / game 单文件 HTML。
 *
 * 安全：使用 sandbox="allow-scripts"，**不加** allow-same-origin —— 生成的 HTML 来自大模型，
 * 保留 origin 边界可阻断其访问父窗口的 cookie / DOM / navigation（XSS 防线）。
 *
 * 高度自适应：正因为没有 allow-same-origin，父页面读不到 iframe 的 contentDocument，
 * 所以改由子页面通过 postMessage 主动上报高度（上报脚本在后端 _inject_height_reporter 注入）。
 * 只接受 event.source === 本 iframe 的消息，避免其它窗口伪造。
 * 若上报脚本缺失（例如历史遗留文件），iframe 保持默认高度并可内部滚动，内容不会丢失。
 *
 * 注意：本组件必须是独立组件，不能内联成 IIFE ——
 * 内联在 `cond && (() => { useRef(); ... })()` 或 .map() 里会让 Hook 调用数量随渲染变化，
 * 触发 React "Rendered more hooks than during the previous render" 报错。
 */
export default function SandboxedHtmlFrame({ src, isQuiz, fallback }) {
  const iframeRef = React.useRef(null);
  const minHeight = isQuiz ? 400 : 520;

  React.useEffect(() => {
    const onMessage = (e) => {
      const node = iframeRef.current;
      if (!node || e.source !== node.contentWindow) return;
      const h = Number(e.data && e.data.__blindbox_height);
      if (Number.isFinite(h) && h > 0) {
        // 封顶：最高不超过视口 1.5 倍，超出由 iframe 内部滚动。
        // 防止游戏无限追加 DOM 时把外层页面撑到无限长、滚不到底。
        const CAP = Math.max(800, Math.floor(window.innerHeight * 1.5));
        node.style.height = Math.min(CAP, Math.max(minHeight, h + 20)) + "px";
        node.style.overflow = "auto";
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [minHeight]);

  if (!src) return <div className="plan">{fallback || ""}</div>;

  return (
    <iframe
      ref={iframeRef}
      src={src}
      sandbox="allow-scripts"
      title={isQuiz ? "趣味测试" : "小游戏"}
      style={{
        width: "100%",
        height: isQuiz ? 650 : 520,
        border: 0,
        borderRadius: 12,
        marginTop: 8,
        display: "block",
      }}
    />
  );
}
