import React, { useEffect, useRef, useState } from "react";
import SandboxedHtmlFrame from "./SandboxedHtmlFrame.jsx";
import { outUrl, mediaSrc } from "../lib/paths.js";

function parseCharFields(text) {
  if (!text) return [];
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const m = line.match(/^(?:【|[\[])?\s*(.+?)\s*(?:】|[\]]?)\s*[:|：]\s*(.+)/);
      if (m) return { label: m[1].trim(), value: m[2].trim() };
      return { label: "", value: line };
    })
    .filter((f) => f.value);
}

// 提取 spec 章节（【环境依赖清单】/【磁盘占用预估】等），取到下一个【章节】或结尾
function extractSection(text, name) {
  if (!text) return "";
  const re = new RegExp("【" + name + "】([\\s\\S]*?)(?=【|$)", "i");
  const m = text.match(re);
  return m ? m[1].trim().replace(/\n{3,}/g, "\n\n").slice(0, 300) : "";
}

// 环境成本卡：代码模式 spec 下方的依赖/磁盘预估 + 黑名单警告
function envCostCard(result) {
  const plan = result.plan || "";
  const warn = result.env_warning || "";
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

// 实时思考面板：开盒进行中随 SSE 增量逐字显示。
// 与静态 ThinkingPanel 的区别：数据来自 SSE 而非最终 result.reasoning。
// live 为 true 时末尾显示闪烁光标（表示模型还在思考）。
export function LiveThinkingPanel({ streams, live }) {
  const list = (streams || []).filter((s) => s.text);
  if (list.length === 0) return null;
  const totalChars = list.reduce((n, s) => n + (s.text || "").length, 0);

  return (
    <div
      style={{
        margin: "10px 0",
        border: "1px solid var(--accent, #7f77dd)",
        borderRadius: 10,
        background: "rgba(127,119,221,0.06)",
        padding: "10px 12px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
          fontWeight: 600,
          color: "var(--accent, #7f77dd)",
          letterSpacing: 0.5,
          marginBottom: 8,
        }}
      >
        <span>思考中…（{totalChars} 字）</span>
        {live ? (
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: "var(--accent, #7f77dd)",
              animation: "blink 1s steps(2, start) infinite",
              display: "inline-block",
            }}
          />
        ) : null}
      </div>
      {list.map((s, i) => (
        <div
          key={i}
          style={
            i === 0
              ? undefined
              : {
                  marginTop: 8,
                  paddingTop: 8,
                  borderTop: "1px dashed var(--border, #2a2f3a)",
                }
          }
        >
          <div
            style={{
              fontSize: 11,
              color: "var(--accent, #7f77dd)",
              fontWeight: 600,
              marginBottom: 4,
              display: "flex",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <span>{s.stage || "模型思考"}</span>
            {s.model ? (
              <span style={{ color: "var(--muted, #888)", fontWeight: 400 }}>· {s.model}</span>
            ) : null}
          </div>
          <div
            style={{
              fontSize: 13,
              lineHeight: 1.75,
              color: "var(--muted, #b9bdc7)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily:
                'ui-monospace, SFMono-Regular, Menlo, Consolas, "Microsoft YaHei", monospace',
            }}
          >
            {s.text}
            {live && i === list.length - 1 ? (
              <span
                style={{
                  display: "inline-block",
                  width: 7,
                  height: 13,
                  marginLeft: 2,
                  verticalAlign: "text-bottom",
                  background: "var(--accent, #7f77dd)",
                  animation: "blink 1s steps(2, start) infinite",
                }}
              />
            ) : null}
          </div>
        </div>
      ))}
    </div>
  );
}

// 单条思考内容打字机回放：按字符递增显示，模拟流式逐字输出的观感。
// 一次性拿到全文后前端自行播放，无需后端 SSE。
function useTypewriter(text, playing) {
  const full = text || "";
  const [shown, setShown] = useState(0);
  const rafRef = useRef(null);

  useEffect(() => {
    if (!playing || !full) {
      if (!playing) setShown(0);
      return;
    }
    const total = full.length;
    // 每帧推进量：短文本快些，长文本按上限控制总时长在 ~2.5s 内
    const step = Math.max(1, Math.ceil(total / 150));
    let cur = 0;
    let last = 0;
    const tick = (ts) => {
      if (!last) last = ts;
      if (ts - last >= 16) {
        cur = Math.min(total, cur + step);
        setShown(cur);
        last = ts;
      }
      if (cur < total) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [full, playing]);

  return playing ? full.slice(0, shown) : full;
}

// 单条思考：Hook 必须挂在独立组件里，不能在 map 回调里调用
function ThinkingEntry({ entry, playing, first }) {
  const text = useTypewriter(entry.text, playing);
  return (
    <div
      style={
        first
          ? undefined
          : {
              marginTop: 10,
              paddingTop: 10,
              borderTop: "1px dashed var(--border, #2a2f3a)",
            }
      }
    >
      <div
        style={{
          fontSize: 11,
          color: "var(--accent, #7f77dd)",
          fontWeight: 600,
          letterSpacing: 0.4,
          marginBottom: 5,
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          alignItems: "center",
        }}
      >
        <span>{entry.stage || "模型思考"}</span>
        {entry.model ? (
          <span style={{ color: "var(--muted, #888)", fontWeight: 400 }}>· {entry.model}</span>
        ) : null}
      </div>
      <div
        style={{
          fontSize: 13,
          lineHeight: 1.75,
          color: "var(--muted, #b9bdc7)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          fontFamily:
            'ui-monospace, SFMono-Regular, Menlo, Consolas, "Microsoft YaHei", monospace',
        }}
      >
        {text}
        {playing ? (
          <span
            style={{
              display: "inline-block",
              width: 7,
              height: 14,
              marginLeft: 2,
              verticalAlign: "text-bottom",
              background: "var(--accent, #7f77dd)",
              animation: "blink 1s steps(2, start) infinite",
            }}
          />
        ) : null}
      </div>
    </div>
  );
}

// 「思考过程」折叠面板：展示各阶段模型返回的 reasoning / thinking 文本。
// 无思考内容（reasoning 为空）时整块不渲染，避免占位噪音。
export function ThinkingPanel({ reasoning }) {
  const entries = React.useMemo(
    () => (Array.isArray(reasoning) ? reasoning.filter((e) => e && e.text) : []),
    [reasoning]
  );
  const [open, setOpen] = useState(false);
  // 展开后直接显示完整思考内容，不再做打字机动画。
  // 原因：实时思考已在开盒过程中逐字展示过，结果页再演一遍是重复劳动；
  // 且思考常有数千字，打字机要拖近 3 秒，用户想看内容却要干等。
  const playing = false;

  const toggle = () => setOpen((v) => !v);

  if (entries.length === 0) return null;

  const totalChars = entries.reduce((n, e) => n + (e.text || "").length, 0);
  const label = `思考过程（${entries.length} 段 · ${totalChars} 字）`;

  return (
    <div
      style={{
        margin: "10px 0",
        border: "1px solid var(--border, #2a2f3a)",
        borderRadius: 10,
        background: "var(--panel-2, #1f232c)",
        overflow: "hidden",
      }}
    >
      <button
        type="button"
        onClick={toggle}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          padding: "9px 12px",
          background: "transparent",
          border: "none",
          color: "var(--text, #e6e8ec)",
          fontSize: 13,
          fontWeight: 600,
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <span>思考过程（{entries.length} 段 · {totalChars} 字）</span>
        <span style={{ color: "var(--muted, #888)", fontWeight: 400, fontSize: 12 }}>
          {open ? "收起 ▲" : "展开 ▼"}
        </span>
      </button>

      {open && (
        <div style={{ padding: "0 12px 12px" }}>
          {entries.map((e, i) => (
            <ThinkingEntry key={i} entry={e} playing={playing} first={i === 0} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function ResultPanel({ result, onReroll }) {
  const { seed, concept, plan, output, output_kind, archived, tts_info, model_used, caption, image_url, env_warning } = result;

  return (
    <div className="result">
      <div className="seed-pill">seed: {seed}</div>

      <ThinkingPanel reasoning={result.reasoning} />

      <h3>概念卡（创意 Agent）</h3>
      <div className="concept">{concept}</div>

      <h3>规划输出（规划 Agent）</h3>
      <div className="plan">{plan}</div>

      <h3>成品</h3>
      {output_kind === "text" && <div className="concept">{output}</div>}
      {output_kind === "character_card" && (
        <div
          style={{
            margin: "8px auto 0",
            background: "var(--panel-2, #1f232c)",
            border: "1px solid var(--border, #2a2f3a)",
            borderRadius: 14,
            overflow: "hidden",
            maxWidth: 480,
            width: "100%",
          }}
        >
          {image_url && (
            <div style={{ width: "100%", paddingTop: "100%", position: "relative", background: "#0b1220" }}>
              <img
                src={mediaSrc(archived?.local_file, image_url) || ""}
                alt="avatar"
                style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", objectFit: "cover" }}
              />
            </div>
          )}
          <div style={{ padding: "14px 16px" }}>
            {parseCharFields(output).map((f, i) => (
              <div key={i} style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 11, color: "var(--accent, #7f77dd)", fontWeight: 600, letterSpacing: 0.5 }}>
                  {f.label}
                </div>
                <div style={{ fontSize: 14, color: "var(--text, #e6e8ec)", lineHeight: 1.5, marginTop: 2 }}>
                  {f.value}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {output_kind === "spec_md" && (
        <div>
          <div className="plan">
            已生成 spec 文件：<br />
            <code>{output}</code>
          </div>
          {envCostCard(result)}
        </div>
      )}
      {(output_kind === "game_html" || output_kind === "quiz_html") && (
        <SandboxedHtmlFrame
          src={
            // output 可能是 undefined（后端异常路径），直接 .startsWith 会 TypeError 白屏
            outUrl(archived?.local_file) ||
            (typeof output === "string" && output.startsWith("/output/") ? output : null)
          }
          isQuiz={output_kind === "quiz_html"}
          fallback={output}
        />
      )}
      {(output_kind === "image_url" || output_kind === "video_file" || output_kind === "audio_file") &&
        (() => {
          // 与主页面/历史页一致：归档后 output 可能仅是本地相对文件名（无 /output/ 前缀），
          // 须优先读取 archived.local_file 并补 /output/ 前缀，否则退回纯文本。
          const ms = mediaSrc(archived?.local_file, output);
          if (!ms) return <div className="plan">{output}</div>;
          if (output_kind === "audio_file") {
            return (
              <div>
                <audio
                  src={ms}
                  controls
                  preload="metadata"
                  style={{
                    width: "100%",
                    borderRadius: "12px",
                    marginTop: 8,
                    display: "block",
                  }}
                />
              </div>
            );
          }
          if (output_kind === "image_url") {
            const belowText = ["recipe", "tarot", "character"].includes(result.mode);
            if (belowText) {
              return (
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", marginTop: 8 }}>
                  <img
                    src={ms}
                    alt="result"
                    style={{
                      maxWidth: "480px",
                      width: "100%",
                      height: "auto",
                      objectFit: "contain",
                      borderRadius: "12px",
                      display: "block",
                      background: "var(--panel-2)",
                    }}
                  />
                  {caption ? (
                    <div
                      style={{
                        maxWidth: "480px",
                        width: "100%",
                        marginTop: 10,
                        padding: "12px 14px",
                        boxSizing: "border-box",
                        background: "var(--panel-2)",
                        border: "1px solid var(--border)",
                        borderRadius: "12px",
                        color: "var(--text)",
                        fontSize: 14,
                        lineHeight: 1.7,
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                        textAlign: "left",
                      }}
                    >
                      {caption}
                    </div>
                  ) : null}
                </div>
              );
            }
            return (
              <div style={{ position: "relative", display: "inline-block", maxWidth: "100%" }}>
                <img
                  src={ms}
                  alt="result"
                  style={{ maxWidth: "100%", borderRadius: "12px", marginTop: 8, display: "block" }}
                />
                {caption ? (
                  <div
                    style={{
                      position: "absolute",
                      left: 0,
                      right: 0,
                      bottom: 0,
                      marginTop: 8,
                      padding: "10px 12px",
                      borderRadius: "0 0 12px 12px",
                      color: "#fff",
                      background: "linear-gradient(0deg, rgba(0,0,0,0.75), rgba(0,0,0,0))",
                      fontSize: 20,
                      fontWeight: 700,
                      textAlign: "center",
                      textShadow: "0 1px 3px rgba(0,0,0,0.5)",
                    }}
                  >
                    {caption}
                  </div>
                ) : null}
              </div>
            );
          }
          if (output_kind === "video_file") {
            return (
              <video
                src={ms}
                controls
                style={{ maxWidth: "100%", borderRadius: "12px", marginTop: 8, display: "block" }}
              />
            );
          }
          return null;
        })()}

      {output_kind === "image_strip" && (() => {
        // output is a JSON string: {"pages": [{"url":...,"local":...}, ...], "count": N}
        let pages = [];
        try {
          const parsed = JSON.parse(output || "{}");
          if (Array.isArray(parsed.pages)) pages = parsed.pages;
        } catch (e) {
          console.error("[ResultPanel] Failed to parse image_strip JSON:", e);
        }
        if (pages.length === 0) {
          return <div className="plan">（插画故事生成中，未获取到图片）</div>;
        }
        return (
          <div style={{
            display: "flex",
            gap: 10,
            overflowX: "auto",
            padding: "8px 0",
            marginTop: 8,
            scrollSnapType: "x mandatory",
            WebkitOverflowScrolling: "touch",
          }}>
            {pages.map((p, i) => {
              const src = mediaSrc(p?.local, p?.url);
              return (
                <div key={i} style={{
                  flex: "0 0 auto",
                  width: "75%",
                  maxWidth: 400,
                  scrollSnapAlign: "start",
                  borderRadius: 12,
                  overflow: "hidden",
                  background: "#0b1220",
                }}>
                  {src ? (
                    <img src={src} alt={`page-${i + 1}`} style={{
                      width: "100%",
                      display: "block",
                      borderRadius: 12,
                    }} />
                  ) : (
                    <div style={{
                      padding: "40px 16px",
                      color: "var(--muted, #888)",
                      textAlign: "center",
                      fontSize: 13,
                    }}>
                      第 {i + 1} 页（图片未生成）
                    </div>
                  )}
                  <div style={{
                    padding: "6px 10px",
                    fontSize: 11,
                    color: "var(--muted, #888)",
                    background: "var(--panel-2, #1f232c)",
                  }}>
                    第 {i + 1} / {pages.length} 页
                  </div>
                </div>
              );
            })}
          </div>
        );
      })()}

      {output_kind === "song" && (() => {
        // output is a JSON string: {"audio": "relative/path", "lyrics": "...", "tts_info": "..."}
        let audio = "", lyrics = "", ttsInfo = "";
        try {
          const parsed = JSON.parse(output || "{}");
          audio = parsed.audio || "";
          lyrics = parsed.lyrics || "";
          ttsInfo = parsed.tts_info || "";
        } catch (e) {
          console.error("[ResultPanel] Failed to parse song JSON:", e);
        }
        const audioSrc = mediaSrc(null, audio);
        return (
          <div style={{ marginTop: 8 }}>
            <div style={{
              background: "var(--panel-2, #1f232c)",
              border: "1px solid var(--border, #2a2f3a)",
              borderRadius: 14,
              padding: "16px 18px",
              whiteSpace: "pre-wrap",
              lineHeight: 1.8,
              fontSize: 15,
              color: "var(--text, #e6e8ec)",
              fontFamily: '"PingFang SC", "Microsoft YaHei", serif',
              letterSpacing: 0.5,
            }}>
              {lyrics || "（歌词为空）"}
            </div>
            {audioSrc && (
              <audio
                src={audioSrc}
                controls
                preload="metadata"
                style={{
                  width: "100%",
                  borderRadius: "12px",
                  marginTop: 10,
                  display: "block",
                }}
              />
            )}
            {ttsInfo && (
              <div style={{ marginTop: 4, fontSize: 12, color: "var(--muted, #888)" }}>
                {ttsInfo}
              </div>
            )}
          </div>
        );
      })()}
      {model_used ? (
        <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted, #888)" }}>
          由 {model_used} 生成
        </div>
      ) : null}

      {archived && (
        <div className="archive-box">
          <span className="archive-badge">✓ 已自动归档</span>
          <span className="archive-time">{archived.timestamp}</span>
          <div className="archive-links">
            {outUrl(archived.json) && (
              <a href={outUrl(archived.json)} target="_blank" rel="noreferrer">
                归档记录 (JSON)
              </a>
            )}
            {outUrl(archived.local_file) ? (
              <a href={outUrl(archived.local_file)} target="_blank" rel="noreferrer">
                本地成品
              </a>
            ) : (output_kind === "image_url" || output_kind === "video_file" || output_kind === "image_strip") ? (
              <span className="archive-note">（远程成品未下载到本地，可手动保存）</span>
            ) : null}
          </div>
        </div>
      )}

      <button className="open-btn" style={{ marginTop: 16 }} onClick={onReroll}>
        再抽一次（换种子）
      </button>
    </div>
  );
}
