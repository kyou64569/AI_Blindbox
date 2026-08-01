import React from "react";

export default function ResultPanel({ result, onReroll }) {
  const { seed, concept, plan, output, output_kind, archived, tts_info } = result;

  return (
    <div className="result">
      <div className="seed-pill">seed: {seed}</div>

      <h3>概念卡（创意 Agent）</h3>
      <div className="concept">{concept}</div>

      <h3>规划输出（规划 Agent）</h3>
      <div className="plan">{plan}</div>

      <h3>成品</h3>
      {output_kind === "text" && <div className="concept">{output}</div>}
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
            style={{
              width: "100%",
              height: 420,
              border: 0,
              borderRadius: 12,
              marginTop: 8,
              display: "block",
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
                {tts_info ? (
                  <div className="plan" style={{ marginTop: 6 }}>
                    🎙 合成：{tts_info}
                  </div>
                ) : null}
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
          );
        })()}

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
            ) : (output_kind === "image_url" || output_kind === "video_file") ? (
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
