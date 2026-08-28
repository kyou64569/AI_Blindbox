import React, { useEffect, useRef, useState } from "react";
import ModeSelector from "./components/ModeSelector.jsx";
import ResultPanel from "./components/ResultPanel.jsx";
import ConfigPanel from "./components/ConfigPanel.jsx";
import HistoryPanel from "./components/HistoryPanel.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import { fetchJson, postJson } from "./lib/http.js";

export default function App() {
  const [tab, setTab] = useState("box"); // box | history | config
  const [modes, setModes] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [customSeed, setCustomSeed] = useState("");
  // 异步开盒任务进度
  const [task, setTask] = useState(null);
  const pollRef = useRef(null);
  // 批量开盒
  const [batchCount, setBatchCount] = useState(3);
  const [batchTasks, setBatchTasks] = useState([]);
  const [batchError, setBatchError] = useState(null);
  const batchPollRef = useRef(null);

  // 历史记录
  const [history, setHistory] = useState([]);
  const [histLoading, setHistLoading] = useState(false);
  const [histError, setHistError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  // 历史搜索 / 筛选
  const [histQuery, setHistQuery] = useState("");
  const [histModeFilter, setHistModeFilter] = useState("");
  const [histFavOnly, setHistFavOnly] = useState(false);
  // 统计
  const [stats, setStats] = useState(null);
  const [statsLoading, setStatsLoading] = useState(false);
  // token 单价（来自配置，用于成本估算）
  const [tokensPrice, setTokensPrice] = useState(0);

  useEffect(() => {
    fetchJson("/api/modes")
      .then((d) => {
        setModes(d?.modes || []);
        if (d?.modes?.length) setSelected(d.modes[0].id);
      })
      .catch((e) => setError("加载模式失败: " + e.message));
    fetchJson("/api/config")
      .then((d) => setTokensPrice(Number(d?.config?.global?.tokens_price) || 0))
      .catch(() => {});
  }, []);

  const stopPoll = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };
  useEffect(() => stopPoll, []); // 卸载时清理

  // 网络/连接类错误给可操作提示（与 demo.html 保持一致）
  const errMsg = (e, fallback) => {
    if (e?.message === "Failed to fetch" || e?.message === "NetworkError") {
      return "与服务器连接中断。请确认前后端服务正常后重试。";
    }
    return fallback + (e?.message || "未知错误");
  };

  const runOpen = async (modeId, seed) => {
    const m = modeId || selected;
    if (!m) return;
    // 自定义种子优先于历史 seed
    const finalSeed = (customSeed || "").trim() || seed;
    stopPoll();
    setLoading(true);
    setError(null);
    setResult(null);
    setTask(null);
    // 1) 创建异步任务，立即拿 task_id
    let data;
    try {
      data = await postJson("/api/open", finalSeed ? { mode: m, seed: finalSeed } : { mode: m });
    } catch (e) {
      setLoading(false);
      setError(errMsg(e, "开盲盒失败: "));
      return;
    }
    const taskId = data?.task_id;
    if (!taskId) {
      setLoading(false);
      setError("服务未返回 task_id，请检查后端版本");
      return;
    }
    // 2) 轮询进度
    const poll = async () => {
      let t;
      try {
        t = await fetchJson(`/api/task/${taskId}`);
      } catch (e) {
        stopPoll();
        setLoading(false);
        setTask(null);
        setError(errMsg(e, "查询任务失败: "));
        return;
      }
      setTask({ task_id: taskId, stage_label: t.stage_label, progress: t.progress, elapsed: t.elapsed });
      if (t.status === "done") {
        stopPoll();
        setLoading(false);
        setTask(null);
        if (t.result) setResult(t.result);
        else setError("任务完成但结果为空");
      } else if (t.status === "cancelled") {
        stopPoll();
        setLoading(false);
        setTask(null);
        setError("任务已取消。");
      } else if (t.status === "error") {
        stopPoll();
        setLoading(false);
        setTask(null);
        setError("开盲盒失败: " + (t.error || "未知错误"));
      }
    };
    await poll();
    if (!pollRef.current) pollRef.current = setInterval(poll, 1200);
  };

  const cancelTask = async () => {
    const tid = task?.task_id;
    if (!tid) return;
    try {
      await postJson(`/api/task/${tid}/cancel`, {});
    } catch (e) {
      /* 忽略网络错误，轮询会兜底 */
    }
  };
  const openBox = () => runOpen(selected);

  // ---- 批量开盒 ----
  const stopBatchPoll = () => {
    if (batchPollRef.current) {
      clearInterval(batchPollRef.current);
      batchPollRef.current = null;
    }
  };
  const cancelBatch = async () => {
    stopBatchPoll();
    for (const t of batchTasks) {
      try {
        await postJson(`/api/task/${t.task_id}/cancel`, {});
      } catch (e) {
        /* ignore */
      }
    }
  };
  const runBatchOpen = async () => {
    const m = selected;
    if (!m) return;
    const finalSeed = (customSeed || "").trim();
    setLoading(true);
    setError(null);
    setBatchError(null);
    setResult(null);
    stopBatchPoll();
    let data;
    try {
      data = await postJson("/api/open-batch", finalSeed ? { mode: m, count: batchCount, seed: finalSeed } : { mode: m, count: batchCount });
    } catch (e) {
      setLoading(false);
      setBatchError(errMsg(e, "创建批量任务失败: "));
      return;
    }
    if (!data?.tasks?.length) {
      setLoading(false);
      setBatchError("服务未返回批量任务");
      return;
    }
    const tasks = data.tasks.map((t) => ({
      task_id: t.task_id,
      status: "running",
      stage_label: "排队中",
      progress: 0,
      elapsed: 0,
      result: null,
      error: null,
    }));
    setBatchTasks(tasks);
    const pollAll = async () => {
      const next = [];
      let allDone = true;
      for (const t of tasks) {
        let cur = null;
        try {
          cur = await fetchJson(`/api/task/${t.task_id}`);
        } catch (e) {
          cur = null;
        }
        if (cur?.status) {
          next.push({ ...t, status: cur.status, stage_label: cur.stage_label, progress: cur.progress, elapsed: cur.elapsed, result: cur.result, error: cur.error });
          if (!["done", "error", "cancelled"].includes(cur.status)) allDone = false;
        } else {
          next.push(t);
          allDone = false;
        }
      }
      setBatchTasks(next);
      if (allDone) {
        stopBatchPoll();
        setLoading(false);
        if (!next.some((t) => t.status === "done" && t.result)) {
          const errs = next.filter((t) => t.error).map((t) => t.error).join("；");
          setBatchError("批量任务均未成功: " + (errs || "未知"));
        }
      }
    };
    await pollAll();
    if (!batchPollRef.current) batchPollRef.current = setInterval(pollAll, 1200);
  };

  // 已下线模式的兜底名称（历史记录仍显示可读名）
  const LEGACY_MODE_NAMES = { novella: "中篇播客（已合并）", longpod: "长播客（已合并）" };
  const modeName = (id) => modes.find((m) => m.id === id)?.name || LEGACY_MODE_NAMES[id] || id || "";
  const loadHistory = async () => {
    setHistLoading(true);
    setHistError(null);
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (histQuery.trim()) params.set("q", histQuery.trim());
      if (histModeFilter) params.set("mode", histModeFilter);
      const data = await fetchJson(`/api/history?${params.toString()}`);
      const items = data?.items || [];
      setHistory(histFavOnly ? items.filter((x) => x.favorite) : items);
    } catch (e) {
      setHistError("加载历史失败: " + e.message);
    } finally {
      setHistLoading(false);
    }
  };

  const toggleFavorite = async (rid, fav) => {
    try {
      await postJson(`/api/history/${rid}/favorite`, { favorite: fav });
      setHistory((prev) => prev.map((x) => (x.id === rid ? { ...x, favorite: fav } : x)));
      // 只看收藏模式下取消收藏 -> 从列表移除
      if (histFavOnly && !fav) setHistory((prev) => prev.filter((x) => x.id !== rid));
    } catch (e) {
      setHistError("更新收藏失败: " + e.message);
    }
  };

  const rateItem = async (rid, rating) => {
    try {
      await postJson(`/api/history/${rid}/rating`, { rating });
      setHistory((prev) => prev.map((x) => (x.id === rid ? { ...x, rating } : x)));
    } catch (e) {
      setHistError("评分失败: " + e.message);
    }
  };

  const loadStats = async () => {
    setStatsLoading(true);
    try {
      const d = await fetchJson("/api/stats");
      setStats(d);
    } catch (e) {
      setError("加载统计失败: " + e.message);
    } finally {
      setStatsLoading(false);
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
          <div
            className={`tab ${tab === "stats" ? "active" : ""}`}
            onClick={() => {
              setTab("stats");
              loadStats();
            }}
          >
            统计
          </div>
          <div className={`tab ${tab === "config" ? "active" : ""}`} onClick={() => setTab("config")}>
            配置
          </div>
        </div>
      </header>

      <ErrorBoundary>
      {tab === "box" && (
        <>
          <div className="section-title">选择模式</div>
          <ModeSelector modes={modes} selected={selected} onSelect={setSelected} />
          <div className="control-card">
            <div className="seed-edit">
              <input
                className="seed-input"
                value={customSeed}
                placeholder="自定义种子（可选）：手写灵感词，锁定开盒方向，如「赛博茶馆 雨天」"
                onChange={(e) => setCustomSeed(e.target.value)}
              />
              {customSeed ? (
                <div className="seed-lock-note">
                  已锁定种子：{customSeed}（留空则随机抽取）
                </div>
              ) : null}
            </div>
            <div className="action-row">
              <button className="open-btn" disabled={loading || !selected} onClick={openBox}>
                {loading ? "⏳ 开启中…" : "🎁 开启盲盒"}
              </button>
              <div className="batch-group">
                <span className="batch-label">连开</span>
                <select
                  className="batch-count"
                  title="连开数量"
                  value={batchCount}
                  onChange={(e) => setBatchCount(Number(e.target.value))}
                >
                  {[2, 3, 4, 5, 6].map((n) => (
                    <option key={n} value={n}>
                      {n} 个
                    </option>
                  ))}
                </select>
                <button className="batch-btn" disabled={loading || !selected} onClick={runBatchOpen}>
                  {batchTasks.some((t) => t.status === "running") ? "连开中…" : "🎁 连开盲盒"}
                </button>
                {batchTasks.some((t) => t.status === "running") && (
                  <button className="batch-btn batch-cancel" onClick={cancelBatch}>
                    取消
                  </button>
                )}
              </div>
            </div>
          </div>
          {loading && task && (
            <div className="task-progress">
              <div className="task-bar-track">
                <div
                  className="task-bar-fill"
                  style={{ width: `${Math.round((task.progress || 0) * 100)}%` }}
                />
              </div>
              <div className="task-status">
                <span>{task.stage_label || "处理中"}</span>
                <span style={{ color: "var(--muted)" }}>
                  {task.elapsed >= 1 ? `已等待 ${Math.floor(task.elapsed)}s` : ""}
                </span>
              </div>
              <button
                className="save-btn"
                style={{ padding: "6px 14px", background: "var(--danger)", borderColor: "var(--danger)", marginTop: 6 }}
                onClick={cancelTask}
              >
                取消
              </button>
            </div>
          )}
          {error && <div className="error">{error}</div>}
          {batchError && <div className="error">{batchError}</div>}
          {batchTasks.length > 0 && (
            <div className="batch-grid">
              {batchTasks.map((bt, idx) => (
                <div className="batch-card" key={bt.task_id}>
                  <div className="batch-head">
                    #{idx + 1}{" "}
                    {bt.status === "done" ? "✓" : bt.status === "error" ? "✗" : bt.status === "cancelled" ? "✕" : "…"}
                  </div>
                  {bt.status === "done" && bt.result ? (
                    <>
                      <div className="seed-pill" style={{ fontSize: 11, marginBottom: 6 }}>
                        seed: {bt.result.seed || "（无）"}
                      </div>
                      <div className="concept" style={{ fontSize: 12 }}>
                        {(bt.result.concept || "").slice(0, 120)}
                        {(bt.result.concept || "").length > 120 ? "…" : ""}
                      </div>
                      <div className="batch-links">
                        <button className="save-btn" style={{ padding: "4px 10px", fontSize: 12, marginTop: 8 }} onClick={() => setResult(bt.result)}>
                          查看
                        </button>
                        <button
                          className="save-btn"
                          style={{ padding: "4px 10px", fontSize: 12, marginTop: 8, background: "transparent", color: "var(--muted)" }}
                          onClick={() => {
                            setSelected(bt.result.mode);
                            runOpen(bt.result.mode, bt.result.seed);
                          }}
                        >
                          同种子重开
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="task-bar-track" style={{ marginTop: 6 }}>
                        <div className="task-bar-fill" style={{ width: `${Math.round((bt.progress || 0) * 100)}%` }} />
                      </div>
                      <div className="task-status" style={{ marginTop: 4, fontSize: 12 }}>
                        <span>{bt.stage_label || "处理中"}</span>
                        <span style={{ color: "var(--muted)" }}>
                          {bt.elapsed >= 1 ? `${Math.floor(bt.elapsed)}s` : ""}
                        </span>
                      </div>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
          {result && <ResultPanel result={result} onReroll={openBox} />}
        </>
      )}

      {tab === "history" && (
        <>
          <div className="hist-filters">
            <input
              className="seed-input"
              style={{ maxWidth: 260 }}
              placeholder="搜索关键词（concept/plan/output/seed）"
              value={histQuery}
              onChange={(e) => setHistQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") loadHistory();
              }}
            />
            <button className="save-btn" onClick={loadHistory}>
              搜索
            </button>
            <select
              className="seed-input"
              style={{ maxWidth: 180 }}
              value={histModeFilter}
              onChange={(e) => {
                setHistModeFilter(e.target.value);
              }}
            >
              <option value="">全部模式</option>
              {modes.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
            <label className="fav-filter">
              <input
                type="checkbox"
                checked={histFavOnly}
                onChange={(e) => {
                  setHistFavOnly(e.target.checked);
                }}
              />
              只看收藏 ★
            </label>
            <button
              className="save-btn"
              style={{ background: "transparent", color: "var(--muted)" }}
              onClick={() => {
                setHistQuery("");
                setHistModeFilter("");
                setHistFavOnly(false);
                loadHistory();
              }}
            >
              重置
            </button>
          </div>
          <HistoryPanel
            history={history}
            loading={histLoading}
            error={histError}
            modeName={modeName}
            expandedId={expandedId}
            onExpand={setExpandedId}
            onRefresh={loadHistory}
            onReroll={rerollSeed}
            onToggleFavorite={toggleFavorite}
            onRate={rateItem}
            tokensPrice={tokensPrice}
          />
        </>
      )}

      {tab === "stats" && (
        <div>
          <div className="section-title">开盒统计</div>
          {statsLoading && <div className="note">统计中…</div>}
          {stats ? (
            <div className="stats-wrap">
              <div className="stats-cards">
                <div className="stat-card">
                  <div className="stat-num">{stats.total}</div>
                  <div className="stat-label">总开盒</div>
                </div>
                <div className="stat-card">
                  <div className="stat-num">{Math.round(stats.success_rate * 100)}%</div>
                  <div className="stat-label">成功率</div>
                </div>
                <div className="stat-card">
                  <div className="stat-num">{stats.favorites}</div>
                  <div className="stat-label">收藏</div>
                </div>
                <div className="stat-card">
                  <div className="stat-num">{stats.avg_rating ?? "—"}</div>
                  <div className="stat-label">平均评分</div>
                </div>
              </div>
              <div className="section-title">模式分布</div>
              <div className="stat-bars">
                {Object.entries(stats.by_mode || {}).map(([k, v]) => (
                  <div className="stat-bar-row" key={k}>
                    <span className="stat-bar-label">{modeName(k)}</span>
                    <div className="stat-bar-track">
                      <div
                        className="stat-bar-fill"
                        style={{ width: `${stats.total ? (v / stats.total) * 100 : 0}%` }}
                      />
                    </div>
                    <span className="stat-bar-num">{v}</span>
                  </div>
                ))}
              </div>
              <div className="section-title">模型使用（Top 8）</div>
              <div className="stat-bars">
                {Object.entries(stats.by_model || {})
                  .slice(0, 8)
                  .map(([k, v]) => (
                    <div className="stat-bar-row" key={k}>
                      <span className="stat-bar-label" style={{ maxWidth: "45%" }}>{k}</span>
                      <div className="stat-bar-track">
                        <div
                          className="stat-bar-fill fill-alt"
                          style={{ width: `${stats.total ? (v / stats.total) * 100 : 0}%` }}
                        />
                      </div>
                      <span className="stat-bar-num">{v}</span>
                    </div>
                  ))}
              </div>
              <div className="section-title">Token 用量</div>
              <div className="stats-cards">
                <div className="stat-card">
                  <div className="stat-num">{(stats.token_total?.total_tokens || 0).toLocaleString()}</div>
                  <div className="stat-label">累计 tokens</div>
                </div>
                {stats.cost_total > 0 && (
                  <div className="stat-card">
                    <div className="stat-num">¥{stats.cost_total}</div>
                    <div className="stat-label">预估成本（¥{stats.tokens_price}/千token）</div>
                  </div>
                )}
              </div>
            </div>
          ) : (
            !statsLoading && <div className="note">暂无统计数据。</div>
          )}
        </div>
      )}

      {tab === "config" && <ConfigPanel />}
      </ErrorBoundary>
    </div>
  );
}
