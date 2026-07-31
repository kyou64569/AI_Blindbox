import React, { useEffect, useState } from "react";
import ModeSelector from "./components/ModeSelector.jsx";
import ResultPanel from "./components/ResultPanel.jsx";
import ConfigPanel from "./components/ConfigPanel.jsx";
import HistoryPanel from "./components/HistoryPanel.jsx";

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
    fetch("/api/modes")
      .then((r) => r.json())
      .then((d) => {
        setModes(d.modes || []);
        if (d.modes && d.modes.length) setSelected(d.modes[0].id);
      })
      .catch((e) => setError("加载模式失败: " + e.message));
  }, []);

  const runOpen = async (modeId, seed) => {
    const m = modeId || selected;
    if (!m) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch("/api/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(seed ? { mode: m, seed } : { mode: m }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "开盲盒失败");
      setResult(data);
    } catch (e) {
      setError("开盲盒失败: " + e.message);
    } finally {
      setLoading(false);
    }
  };
  const openBox = () => runOpen(selected);

  const modeName = (id) => modes.find((m) => m.id === id)?.name || id || "";
  const loadHistory = async () => {
    setHistLoading(true);
    setHistError(null);
    try {
      const resp = await fetch("/api/history?limit=100");
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "加载历史失败");
      setHistory(data.items || []);
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
