import React from "react";

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

export default function ResultPanel({ result, onReroll }) {
  const { seed, concept, plan, output, output_kind, archived, tts_info, model_used, caption, image_url } = result;

  return (
    <div className="result">
      <div className="seed-pill">seed: {seed}</div>

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
                src={(() => {
                  if (archived?.local_file) return `/output/${archived.local_file}`;
                  if (/^https?:\/\//.test(image_url) || image_url.startsWith("/output/")) return image_url;
                  if (image_url && !/^[a-zA-Z]:[\\/]/.test(image_url) && !image_url.startsWith("/"))
                    return `/output/${image_url}`;
                  return image_url;
                })()}
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
        <div className="plan">
          已生成 spec 文件：<br />
          <code>{output}</code>
        </div>
      )}
      {output_kind === "game_html" && (() => {
        const src = archived?.local_file
          ? `/output/${archived.local_file}`
          : output.startsWith("/output/")
          ? output
          : null;
        return src ? (
          <iframe
            src={src}
            sandbox="allow-scripts"
            title="小游戏"
            scrolling="no"
            style={{
              width: "100%",
              height: 520,
              border: 0,
              borderRadius: 12,
              marginTop: 8,
              display: "block",
              overflow: "hidden",
            }}
          />
        ) : (
          <div className="plan">{output}</div>
        );
      })()}
      {(output_kind === "image_url" || output_kind === "video_file" || output_kind === "audio_file") &&
        (() => {
          // 与主页面/历史页一致：归档后 output 可能仅是本地相对文件名（无 /output/ 前缀），
          // 须优先读取 archived.local_file 并补 /output/ 前缀，否则退回纯文本。
          const ms = archived?.local_file
            ? `/output/${archived.local_file}`
            : output.startsWith("http") || output.startsWith("/output/")
            ? output
            : !/^[a-zA-Z]:[\\/]/.test(output) && !output.startsWith("/")
            ? `/output/${output}`
            : null;
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
          return output_kind === "video_file" ? (
            <video
              src={ms}
              controls
              style={{
                maxWidth: "100%",
                borderRadius: "12px",
                marginTop: 8,
                display: "block",
              }}
            />
          ) : (
            <div style={{ position: "relative", display: "inline-block", maxWidth: "100%" }}>
              <img
                src={ms}
                alt="result"
                style={{
                  maxWidth: "100%",
                  borderRadius: "12px",
                  marginTop: 8,
                  display: "block",
                }}
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
                    background:
                      "linear-gradient(0deg, rgba(0,0,0,0.75), rgba(0,0,0,0))",
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
              const src = p.local
                ? `/output/${p.local}`
                : p.url && (p.url.startsWith("http") || p.url.startsWith("/output/"))
                ? p.url
                : p.url && !/^[a-zA-Z]:[\\/]/.test(p.url) && !p.url.startsWith("/")
                ? `/output/${p.url}`
                : null;
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
        const audioSrc = audio
          ? (audio.startsWith("http") || audio.startsWith("/output/")
            ? audio
            : !/^[a-zA-Z]:[\\/]/.test(audio) && !audio.startsWith("/")
            ? `/output/${audio}`
            : null)
          : null;
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
            <a href={`/output/${archived.json}`} target="_blank" rel="noreferrer">
              归档记录 (JSON)
            </a>
            {archived.local_file ? (
              <a href={`/output/${archived.local_file}`} target="_blank" rel="noreferrer">
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
