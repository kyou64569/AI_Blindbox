import React from "react";

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
                    <div className="plan">
                      <code>{it.output || ""}</code>
                    </div>
                  )}
                  {(it.output_kind === "image_url" || it.output_kind === "video_file") &&
                    (it.output_kind === "video_file" ? (
                      <video
                        src={it.local_file ? "/output/" + it.local_file : it.output}
                        controls
                        style={{ maxWidth: "100%", borderRadius: "12px", marginTop: 8 }}
                      />
                    ) : (
                      <div style={{ position: "relative", display: "inline-block", maxWidth: "100%" }}>
                        <img
                          src={it.local_file ? "/output/" + it.local_file : it.output}
                          alt="result"
                          style={{ maxWidth: "100%", borderRadius: "12px", marginTop: 8 }}
                        />
                        {it.caption ? (
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
                            {it.caption}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  {it.model_used ? (
                    <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted, #888)" }}>
                      由 {it.model_used} 生成
                    </div>
                  ) : null}
                  <div className="archive-links">
                    <a href={`/output/${it.json}`} target="_blank" rel="noreferrer">
                      归档 JSON
                    </a>
                    {it.local_file && (
                      <a href={`/output/${it.local_file}`} target="_blank" rel="noreferrer">
                        本地成品
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
