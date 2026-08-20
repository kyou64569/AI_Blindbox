import React from "react";
import SandboxedHtmlFrame from "./SandboxedHtmlFrame.jsx";
import { outUrl, mediaSrc } from "../lib/paths.js";

// 提取 spec 章节（【环境依赖清单】/【磁盘占用预估】等），取到下一个【章节】或结尾
function extractSection(text, name) {
  if (!text) return "";
  const re = new RegExp("【" + name + "】([\\s\\S]*?)(?=【|$)", "i");
  const m = text.match(re);
  return m ? m[1].trim().replace(/\n{3,}/g, "\n\n").slice(0, 300) : "";
}

// 环境成本卡：代码模式历史条目 spec 下方的依赖/磁盘预估 + 黑名单警告
function specCostCard(item) {
  const plan = item.plan || "";
  const warn = item.env_warning || "";
  const dep = extractSection(plan, "环境依赖清单");
  const size = extractSection(plan, "磁盘占用预估");
  const rows = [];
  if (warn) {
    rows.push(
      <div
        key="warn"
        style={{
          marginTop: 10,
          padding: "10px 12px",
          background: "rgba(227,75,74,0.12)",
          border: "1px solid rgba(227,75,74,0.5)",
          borderRadius: 10,
          color: "#f09595",
          fontSize: 13,
          lineHeight: 1.6,
          whiteSpace: "pre-wrap",
        }}
      >
        {warn}
      </div>
    );
  }
  if (dep || size) {
    rows.push(
      <div
        key="cost"
        style={{
          marginTop: 10,
          padding: "12px 14px",
          background: "var(--panel-2, #1f232c)",
          border: "1px solid var(--border, #2a2f3a)",
          borderRadius: 10,
          fontSize: 13,
          lineHeight: 1.7,
        }}
      >
        <div
          style={{
            fontSize: 12,
            color: "var(--accent, #7f77dd)",
            fontWeight: 600,
            marginBottom: 6,
            letterSpacing: 0.5,
          }}
        >
          环境成本
        </div>
        {dep ? <div style={{ color: "var(--muted, #888)" }}>依赖：{dep}</div> : null}
        {size ? (
          <div style={{ color: "var(--muted, #888)", marginTop: 4 }}>磁盘：{size}</div>
        ) : null}
      </div>
    );
  }
  return rows.length ? rows : null;
}

export default function HistoryPanel({
  history,
  loading,
  error,
  modeName,
  expandedId,
  onExpand,
  onRefresh,
  onReroll,
}) {
  return (
    <>
      <div className="section-title">历史记录（{history.length} 条）</div>
      {loading && <div className="note">加载中…</div>}
      {error && <div className="error">{error}</div>}
      {!loading && !error && history.length === 0 && (
        <div className="note">还没有开盒记录，去「开盲盒」抽一个吧。</div>
      )}
      <div className="hist-list">
        {history.map((it) => {
          const open = expandedId === it.id;
          return (
            <div className="hist-item" key={it.id}>
              <div className="hist-head" onClick={() => onExpand(open ? null : it.id)}>
                <span className="hist-mode">{modeName(it.mode)}</span>
                <span className="hist-time">{it.timestamp || ""}</span>
                <span className="hist-kind">{it.output_kind || ""}</span>
              </div>
              <div className="seed-pill">seed: {it.seed || "（无）"}</div>
              {open ? (
                <>
                  <h3>概念卡</h3>
                  <div className="concept">{it.concept || ""}</div>
                  <h3>成品</h3>
                  {it.output_kind === "text" && (
                    <div className="concept">{it.output || ""}</div>
                  )}
                  {it.output_kind === "spec_md" && (
                    <div>
                      <div className="plan">
                        <code>{it.output || ""}</code>
                      </div>
                      {specCostCard(it)}
                    </div>
                  )}
                  {(it.output_kind === "image_url" || it.output_kind === "video_file" || it.output_kind === "image_strip" || it.output_kind === "song" || it.output_kind === "game_html" || it.output_kind === "quiz_html") && (() => {
                    if (it.output_kind === "image_strip") {
                      let pages = [];
                      try {
                        const parsed = JSON.parse(it.output || "{}");
                        if (Array.isArray(parsed.pages)) pages = parsed.pages;
                      } catch (e) {
                        console.error("[HistoryPanel] Failed to parse image_strip JSON:", e);
                      }
                      if (pages.length === 0) return <div className="plan">（插画故事 · 共 0 页）</div>;
                      return (
                        <div style={{ display: "flex", gap: 8, overflowX: "auto" }}>
                          {pages.map((p, i) => (
                            <div key={i} style={{ flex: "0 0 auto", width: "60%", maxWidth: 280, borderRadius: 8, overflow: "hidden", background: "#0b1220" }}>
                              {mediaSrc(p?.local, p?.url)
                                ? <img src={mediaSrc(p.local, p.url)} alt={`p${i+1}`} style={{ width: "100%", display: "block" }} />
                                : <div style={{ padding: 20, color: "#888", textAlign: "center", fontSize: 12 }}>第 {i+1} 页（未生成）</div>}
                              <div style={{ padding: "4px 8px", fontSize: 10, color: "#888", background: "#1f232c" }}>第 {i + 1} / {pages.length} 页</div>
                            </div>
                          ))}
                        </div>
                      );
                    }
                    if (it.output_kind === "song") {
                      let lyrics = "";
                      try {
                        const p = JSON.parse(it.output || "{}");
                        lyrics = p.lyrics || "";
                      } catch (e) {
                        console.error("[HistoryPanel] Failed to parse song JSON:", e);
                      }
                       return <div className="concept" style={{ whiteSpace: "pre-wrap", fontFamily: '"PingFang SC","Microsoft YaHei",serif', lineHeight: 1.8 }}>{lyrics || "（歌词为空）"}</div>;
                    }
                    if (it.output_kind === "game_html" || it.output_kind === "quiz_html") {
                      return (
                        <SandboxedHtmlFrame
                          src={mediaSrc(it.local_file, it.output)}
                          isQuiz={it.output_kind === "quiz_html"}
                          fallback={it.output}
                        />
                      );
                    }
                    // 其余：image_url / video_file
                    return (it.output_kind === "video_file" ? (
                      <video
                        src={mediaSrc(it.local_file, it.output)}
                        controls
                        style={{ maxWidth: "100%", borderRadius: "12px", marginTop: 8 }}
                      />
                    ) : (
                      (["recipe", "tarot", "character"].includes(it.mode) ? (
                        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", marginTop: 8 }}>
                          <img src={mediaSrc(it.local_file, it.output)} alt="result" style={{ maxWidth: "480px", width: "100%", height: "auto", objectFit: "contain", borderRadius: "12px", display: "block", background: "var(--panel-2)" }} />
                          {it.caption ? (
                            <div style={{ maxWidth: "480px", width: "100%", marginTop: 10, padding: "12px 14px", boxSizing: "border-box", background: "var(--panel-2)", border: "1px solid var(--border)", borderRadius: "12px", color: "var(--text)", fontSize: 14, lineHeight: 1.7, whiteSpace: "pre-wrap", wordBreak: "break-word", textAlign: "left" }}>
                              {it.caption}
                            </div>
                          ) : null}
                        </div>
                      ) : (
                        <div style={{ position: "relative", display: "inline-block", maxWidth: "100%" }}>
                          <img src={mediaSrc(it.local_file, it.output)} alt="result" style={{ maxWidth: "100%", borderRadius: "12px", marginTop: 8, display: "block" }} />
                          {it.caption ? (
                            <div style={{ position: "absolute", left: 0, right: 0, bottom: 0, marginTop: 8, padding: "10px 12px", borderRadius: "0 0 12px 12px", color: "#fff", background: "linear-gradient(0deg, rgba(0,0,0,0.75), rgba(0,0,0,0))", fontSize: 20, fontWeight: 700, textAlign: "center", textShadow: "0 1px 3px rgba(0,0,0,0.5)" }}>
                              {it.caption}
                            </div>
                          ) : null}
                        </div>
                      ))
                    ))} )}
                  {it.model_used ? (
                    <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted, #888)" }}>
                      由 {it.model_used} 生成
                    </div>
                  ) : null}
                  <div className="archive-links">
                    {outUrl(it.json) && (
                      <a href={outUrl(it.json)} target="_blank" rel="noreferrer">
                        归档 JSON
                      </a>
                    )}
                    {outUrl(it.local_file) && (
                      <a href={outUrl(it.local_file)} target="_blank" rel="noreferrer">
                        {it.local_file.endsWith(".md") ? "文字成品" : "图片成品"}
                      </a>
                    )}
                    {it.text_file && it.text_file !== it.local_file && outUrl(it.text_file) && (
                      <a href={outUrl(it.text_file)} target="_blank" rel="noreferrer">
                        文字成品
                      </a>
                    )}
                    {it.image_url && /\.(png|jpe?g|gif|webp)$/i.test(it.image_url) && outUrl(it.image_url) && (
                      <a href={outUrl(it.image_url)} target="_blank" rel="noreferrer">
                        图片成品
                      </a>
                    )}
                  </div>
                  <button
                    className="save-btn"
                    style={{ marginTop: 10 }}
                    onClick={() => onReroll(it.mode, it.seed)}
                  >
                    用此 seed 重抽
                  </button>
                </>
              ) : (
                <div className="hist-snippet">{(it.output || "").slice(0, 90)}</div>
              )}
            </div>
          );
        })}
      </div>
      {history.length > 0 && (
        <button
          className="save-btn"
          style={{ marginTop: 12, background: "transparent", color: "var(--muted)" }}
          onClick={onRefresh}
        >
          ↻ 刷新
        </button>
      )}
    </>
  );
}
