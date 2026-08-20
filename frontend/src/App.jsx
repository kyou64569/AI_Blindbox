import React, { useEffect, useState } from "react";
import ModeSelector from "./components/ModeSelector.jsx";
import ResultPanel from "./components/ResultPanel.jsx";
import ConfigPanel from "./components/ConfigPanel.jsx";
import HistoryPanel from "./components/HistoryPanel.jsx";
import { fetchJson, postJson } from "./lib/http.js";

export default function App() {
  const [tab, setTab] = useState("box"); // box | history | config
  const [modes, setModes] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  // 历史记录
  const [history, setHistory] = useState([]);
  const [histLoading, setHistLoading] = useState(false);
  const [histError, setHistError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);

  useEffect(() => {
    fetchJson("/api/modes")
      .then((d) => {
        setModes(d?.modes || []);
        if (d?.modes?.length) setSelected(d.modes[0].id);
      })
      .catch((e) => setError("加载模式失败: " + e.message));
  }, []);

  const runOpen = async (modeId, seed) => {
    const m = modeId || selected;
    if (!m) return;
    setLoading(true);
    setError(null);
    try {
      const data = await postJson("/api/open", seed ? { mode: m, seed } : { mode: m });
      setResult(data);
    } catch (e) {
      setError("开盲盒失败: " + e.message);
    } finally {
      setLoading(false);
    }
  };
  const openBox = () => runOpen(selected);

  // 已下线模式的兜底名称（历史记录仍显示可读名）
  const LEGACY_MODE_NAMES = { novella: "中篇播客（已合并）", longpod: "长播客（已合并）" };
  const modeName = (id) => modes.find((m) => m.id === id)?.name || LEGACY_MODE_NAMES[id] || id || "";
  const loadHistory = async () => {
    setHistLoading(true);
    setHistError(null);
    try {
      const data = await fetchJson("/api/history?limit=100");
      setHistory(data?.items || []);
    } catch (e) {
      setHistError("加载历史失败: " + e.message);
    } finally {
      setHistLoading(false);
    }
  };

  const rerollSeed = (modeId, seed) => {
    setSelected(modeId);
    setTab("box");
    runOpen(modeId, seed);
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>AI 盲盒</h1>
        <div className="tabs">
          <div className={`tab ${tab === "box" ? "active" : ""}`} onClick={() => setTab("box")}>
            开盲盒
          </div>
          <div
            className={`tab ${tab === "history" ? "active" : ""}`}
            onClick={() => {
              setTab("history");
              loadHistory();
            }}
          >
            历史
          </div>
          <div className={`tab ${tab === "config" ? "active" : ""}`} onClick={() => setTab("config")}>
            配置
          </div>
        </div>
      </header>

      {tab === "box" && (
        <>
          <div className="section-title">选择模式</div>
          <ModeSelector modes={modes} selected={selected} onSelect={setSelected} />
          <button className="open-btn" disabled={loading || !selected} onClick={openBox}>
            {loading ? "开启中…" : "🎁 开启盲盒"}
          </button>
          {error && <div className="error">{error}</div>}
          {result && <ResultPanel result={result} onReroll={openBox} />}
        </>
      )}

      {tab === "history" && (
        <HistoryPanel
          history={history}
          loading={histLoading}
          error={histError}
          modeName={modeName}
          expandedId={expandedId}
          onExpand={setExpandedId}
          onRefresh={loadHistory}
          onReroll={rerollSeed}
        />
      )}

      {tab === "config" && <ConfigPanel />}
    </div>
  );
}
