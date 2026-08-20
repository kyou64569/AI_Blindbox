import React, { useEffect, useState, useRef } from "react";
import { fetchJson, postJson } from "../lib/http.js";

function Field({ label, value, onChange, type = "text", placeholder = "" }) {
  return (
    <div className="form-row">
      <label>{label}</label>
      <input
        type={type}
        value={value || ""}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

/**
 * 一组「模型配置」：base_url + api_key + 模型（可下拉选择 / 手填）。
 * basePath 指向该组在 config 中的路径前缀，例如 "agents.creative"。
 * keyMap 把界面字段映射到配置里的实际字段名（全局默认用 default_*，其余用 model/base_url/api_key）。
 */
// 模型/音色选择器：有选项列表时用 <select>（可随时改选），下拉恒含「自定义…」项，
// 选择后显示手填输入框（保留自由输入）；无列表时退化为单个普通输入框
function ModelPicker({ value, options, onPick, placeholder, emptyLabel }) {
  const [custom, setCustom] = useState(false);
  const v = (value || "").trim();
  const opts = options || [];
  if (opts.length === 0) {
    return (
      <input
        value={v}
        placeholder={placeholder || "手填"}
        onChange={(e) => onPick(e.target.value)}
        style={{ flex: 1 }}
      />
    );
  }
  const inList = opts.includes(v);
  const effective = custom || (v !== "" && !inList);
  const selValue = effective ? "__custom__" : v;
  return (
    <div style={{ display: "flex", gap: 6, flex: 1, minWidth: 0 }}>
      <select
        value={selValue}
        onChange={(e) => {
          const sv = e.target.value;
          if (sv === "__custom__") {
            setCustom(true);
          } else {
            setCustom(false);
            onPick(sv);
          }
        }}
        style={{
          flex: 1,
          minWidth: 0,
          height: 36,
          background: "var(--panel-2, #1f232c)",
          color: "var(--text, #e6e8ec)",
          border: "1px solid var(--border, #2a2f3a)",
          borderRadius: 8,
          padding: "0 8px",
          fontSize: 13,
        }}
      >
        {!effective && v === "" ? <option value="">{emptyLabel || "（留空）"}</option> : null}
        <option value="__custom__">（自定义…）</option>
        {opts.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
      {effective ? (
        <input
          value={v}
          placeholder={placeholder || "自定义"}
          onChange={(e) => {
            const newValue = e.target.value;
            onPick(newValue);
            // 如果输入的值匹配选项列表中的某个值，自动切换回选择模式
            if (opts.includes(newValue.trim())) {
              setCustom(false);
            }
          }}
          style={{ flex: 1 }}
        />
      ) : null}
    </div>
  );
}

function ModelSlot({ title, note, basePath, keyMap, config, update }) {
  const [models, setModels] = useState([]);
  const [fetching, setFetching] = useState(false);
  const [err, setErr] = useState(null);

  const get = (uiKey) => {
    const cfgKey = keyMap[uiKey];
    // 嵌套解析 basePath（如 "agents.creative"），避免 config["agents.creative"] 直接索引为 undefined
    const node = basePath.split(".").reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), config);
    return node?.[cfgKey];
  };
  const set = (uiKey, val) => update(`${basePath}.${keyMap[uiKey]}`, val);

  const fetchModels = async () => {
    setErr(null);
    setFetching(true);
    try {
      const data = await postJson("/api/models", {
        base_url: get("base_url") || config.global?.default_base_url || "",
        api_key: get("api_key") || config.global?.default_api_key || "",
      });
      setModels((data && data.models) || []);
    } catch (e) {
      setErr(e.message);
    } finally {
      setFetching(false);
    }
  };

  return (
    <div>
      <div className="section-title">{title}</div>
      {note && <div className="note" style={{ marginBottom: 8 }}>{note}</div>}
      <div className="config-form">
        <Field
          label="API Base URL"
          value={get("base_url")}
          onChange={(v) => set("base_url", v)}
          placeholder="留空=用全局默认"
        />
        <Field
          label="API Key"
          type="password"
          value={get("api_key")}
          onChange={(v) => set("api_key", v)}
          placeholder="留空=用全局默认"
        />
        <div className="form-row">
          <label>模型 model（填好上方 base_url / key 后可点按钮拉取列表选择）</label>
          <div style={{ display: "flex", gap: 8 }}>
            <ModelPicker
              value={get("model") || ""}
              options={models}
              onPick={(v) => set("model", v)}
              placeholder="留空=用全局默认；或从下拉选 / 手填"
              emptyLabel="（留空=用全局默认）"
            />
            <button
              type="button"
              className="save-btn"
              style={{ padding: "8px 12px", whiteSpace: "nowrap" }}
              disabled={fetching}
              onClick={fetchModels}
            >
              {fetching ? "获取中…" : "获取模型列表"}
            </button>
          </div>
          {models.length > 0 && (
            <div className="note">已获取 {models.length} 个模型，点输入框可下拉选择。</div>
          )}
          {err && <div className="error">{err}</div>}
        </div>
      </div>
    </div>
  );
}

/**
 * 多模型卡片列表编辑器：折叠 / 手动排序↑↓ / 删除(无确认) / 新增 / 空提示。
 * 每个 section（如 agents.creative）在 config 中对应一个 models: [] 卡片数组，
 * 运行时按序 fallback（上一个失败自动用下一个）。折叠状态用 localStorage 持久化。
 */
function ModelCard({ basePath, idx, card, total, config, update }) {
  const [models, setModels] = useState([]);
  const [fetching, setFetching] = useState(false);
  const [err, setErr] = useState(null);
  const getv = (k) => (card && card[k] != null ? card[k] : "");
  const setv = (k, v) => update(`${basePath}.models.${idx}.${k}`, v);

  const swap = (dir) => {
    const node = basePath.split(".").reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), config);
    const arr = (node && Array.isArray(node.models)) ? node.models : [];
    const j = idx + dir;
    if (j < 0 || j >= arr.length) return;
    const next = [...arr];
    const t = next[idx]; next[idx] = next[j]; next[j] = t;
    update(`${basePath}.models`, next);
  };
  const del = () => {
    // 删除无需确认，直接移除该卡片
    const node = basePath.split(".").reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), config);
    const arr = (node && Array.isArray(node.models)) ? node.models : [];
    update(`${basePath}.models`, arr.filter((_, i) => i !== idx));
  };
  const fetchModels = async () => {
    setErr(null);
    setFetching(true);
    try {
      const data = await postJson("/api/models", {
        base_url: getv("base_url") || config.global?.default_base_url || "",
        api_key: getv("api_key") || config.global?.default_api_key || "",
      });
      setModels((data && data.models) || []);
    } catch (e) {
      setErr(e.message);
    } finally {
      setFetching(false);
    }
  };

  const tv = getv("temperature");
  return (
    <div className="config-form" style={{ marginTop: "10px", border: "1px solid var(--border)", borderRadius: "10px", padding: "12px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
        <div className="note" style={{ margin: 0 }}>模型 #{idx + 1}</div>
        <div style={{ display: "flex", gap: "6px" }}>
          <button type="button" className="save-btn" style={{ padding: "4px 8px", background: "transparent", color: "var(--muted)", borderColor: "var(--border)" }} disabled={idx === 0} onClick={() => swap(-1)} title="上移">↑</button>
          <button type="button" className="save-btn" style={{ padding: "4px 8px", background: "transparent", color: "var(--muted)", borderColor: "var(--border)" }} disabled={idx === total - 1} onClick={() => swap(1)} title="下移">↓</button>
          <button type="button" className="save-btn" style={{ padding: "4px 8px", background: "var(--danger)", borderColor: "var(--danger)" }} onClick={del} title="删除（无确认）">删除</button>
        </div>
      </div>
      <div className="form-row">
        <label>模型 model</label>
        <div style={{ display: "flex", gap: 8 }}>
          <ModelPicker
            value={getv("model") || ""}
            options={models}
            onPick={(v) => setv("model", v)}
            placeholder="模型名，如 gpt-4o-mini-tts"
            emptyLabel="（手填）"
          />
          <button type="button" className="save-btn" style={{ padding: "8px 12px", whiteSpace: "nowrap" }} disabled={fetching} onClick={fetchModels}>
            {fetching ? "获取中…" : "获取列表"}
          </button>
        </div>
        {models.length > 0 && <div className="note">已获取 {models.length} 个模型，点下拉可随时改选。</div>}
        {err && <div className="error">{err}</div>}
      </div>
      <Field label="API Base URL" value={getv("base_url")} onChange={(v) => setv("base_url", v)} placeholder="留空=用全局默认" />
      <Field label="API Key" type="password" value={getv("api_key")} onChange={(v) => setv("api_key", v)} placeholder="留空=用全局默认" />
      <div className="form-row">
        <label>温度 temperature（可选，留空=用全局/默认）</label>
        <input type="number" step="0.1" value={tv === "" || tv == null ? "" : tv}
          onChange={(e) => setv("temperature", e.target.value === "" ? null : Number(e.target.value))} placeholder="如 0.8" />
      </div>
    </div>
  );
}

function ModelCards({ title, note, basePath, config, update }) {
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    const k = "mf:collapse:" + basePath;
    try { setCollapsed(localStorage.getItem(k) === "1"); } catch (e) {}
  }, [basePath]);
  const toggle = () => {
    setCollapsed((c) => {
      const nv = !c;
      try { localStorage.setItem("mf:collapse:" + basePath, nv ? "1" : "0"); } catch (e) {}
      return nv;
    });
  };
  const node = basePath.split(".").reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), config);
  const models = (node && Array.isArray(node.models)) ? node.models : [];
  const add = () => update(`${basePath}.models`, [...models, { _id: "card_" + Math.random().toString(36).slice(2), model: "", base_url: "", api_key: "", temperature: null }]);
  return (
    <div>
      <div className="section-title">{title}</div>
      {note && <div className="note" style={{ marginBottom: 8 }}>{note}</div>}
      <div className="config-form">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div className="note" style={{ margin: 0 }}>{models.length} 个模型 · 运行时按序 fallback（上一个失败自动用下一个）</div>
          <button type="button" className="save-btn" style={{ padding: "6px 12px", background: "transparent", color: "var(--accent)", borderColor: "var(--accent)" }} onClick={toggle}>{collapsed ? "展开" : "折叠"}</button>
        </div>
        {collapsed
          ? <div className="note">已折叠（共 {models.length} 个模型）。点击「展开」编辑。</div>
          : models.length === 0
            ? <div className="note" style={{ color: "var(--muted)" }}>暂无模型卡片。若留空：Agent 执行回退全局默认模型；Provider 图片/视频回退另一 Provider 置顶项；TTS 回退免费 Edge TTS。</div>
            : models.map((card, idx) => (
                <ModelCard key={card._id || idx} basePath={basePath} idx={idx} card={card} total={models.length} config={config} update={update} />
              ))}
        <button type="button" className="save-btn" style={{ marginTop: 8 }} onClick={add}>+ 添加模型</button>
      </div>
    </div>
  );
}


const GLOBAL_KEYMAP = { model: "default_model", base_url: "default_base_url", api_key: "default_api_key" };

export default function ConfigPanel() {
  const [config, setConfig] = useState(null);
  const [paths, setPaths] = useState({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);
  const [poolUI, setPoolUI] = useState({ addText: {}, gen: {}, genErr: {} });
  const initialPoolRef = useRef(null);

  useEffect(() => {
    fetchJson("/api/config")
      .then((d) => {
        setConfig(d.config);
        setPaths(d.paths || {});
        if (initialPoolRef.current === null) initialPoolRef.current = d.config?.randomness?.pool || {};
      })
      .catch((e) => setError("加载配置失败: " + e.message));
  }, []);

  if (!config) return <div className="note">加载中…</div>;

  const update = (path, value) => {
    setConfig((prev) => {
      const next = JSON.parse(JSON.stringify(prev));
      let node = next;
      const keys = path.split(".");
      for (let i = 0; i < keys.length - 1; i++) node = node[keys[i]];
      node[keys[keys.length - 1]] = value;
      return next;
    });
    setSaved(false);
  };

  // ---- twist pool 编辑器（推荐混合方案：4 维可手动增删改 + 🎲 大模型生成） ----
  const poolDims = ["theme", "style", "constraint", "twist"];
  const dimCn = { theme: "主题", style: "风格", constraint: "约束", twist: "反转" };
  const getPool = (dim) => config?.randomness?.pool?.[dim] || [];

  const editItem = (dim, idx, val) => {
    const arr = [...getPool(dim)];
    arr[idx] = val;
    update(`randomness.pool.${dim}`, arr);
  };
  const removeItem = (dim, idx) => {
    update(`randomness.pool.${dim}`, getPool(dim).filter((_, i) => i !== idx));
  };
  const addItem = (dim) => {
    const v = (poolUI.addText?.[dim] || "").trim();
    if (!v) return;
    if (getPool(dim).includes(v)) {
      setPoolUI((s) => ({ ...s, addText: { ...s.addText, [dim]: "" } }));
      return;
    }
    update(`randomness.pool.${dim}`, [...getPool(dim), v]);
    setPoolUI((s) => ({ ...s, addText: { ...s.addText, [dim]: "" } }));
  };
  const genSeed = async (dim) => {
    setPoolUI((s) => ({ ...s, gen: { ...s.gen, [dim]: true }, genErr: { ...s.genErr, [dim]: null } }));
    try {
      const data = await postJson("/api/generate-seed", { dimension: dim, existing: getPool(dim) });
      const v = (data.seed || "").trim();
      if (!v) throw new Error("模型返回为空");
      if (!getPool(dim).includes(v)) update(`randomness.pool.${dim}`, [...getPool(dim), v]);
    } catch (e) {
      setPoolUI((s) => ({ ...s, genErr: { ...s.genErr, [dim]: e.message } }));
    } finally {
      setPoolUI((s) => ({ ...s, gen: { ...s.gen, [dim]: false } }));
    }
  };
  const resetPool = () => {
    if (initialPoolRef.current) update("randomness.pool", JSON.parse(JSON.stringify(initialPoolRef.current)));
  };

  // ---- code 模式独立工具池（与全局池同能力：手动增删 + 🎲 生成 + 整体刷新） ----
  const codeModeIdx = () => (config?.modes || []).findIndex((m) => m.id === "code");
  const getModePool = (dim) => {
    const i = codeModeIdx();
    return (i >= 0 && config?.modes?.[i]?.pool?.[dim]) || [];
  };
  const [modePoolUI, setModePoolUI] = useState({ addText: {}, gen: {}, genErr: {} });
  const [modeRefreshing, setModeRefreshing] = useState(false);
  const [modeRefreshErr, setModeRefreshErr] = useState(null);
  const modePoolPath = (dim) => {
    const idx = codeModeIdx();
    if (idx < 0 || idx >= (config?.modes || []).length) return null;
    return `modes.${idx}.pool.${dim}`;
  };
  const editModeItem = (dim, idx, val) => {
    const path = modePoolPath(dim);
    if (!path) return;
    const arr = [...getModePool(dim)];
    arr[idx] = val;
    update(path, arr);
  };
  const removeModeItem = (dim, idx) => {
    const path = modePoolPath(dim);
    if (!path) return;
    update(path, getModePool(dim).filter((_, i) => i !== idx));
  };
  const addModeItem = (dim) => {
    const path = modePoolPath(dim);
    if (!path) return;
    const v = (modePoolUI.addText?.[dim] || "").trim();
    if (!v || getModePool(dim).includes(v)) return;
    update(path, [...getModePool(dim), v]);
    setModePoolUI((s) => ({ ...s, addText: { ...s.addText, [dim]: "" } }));
  };
  const genModeSeed = async (dim) => {
    setModePoolUI((s) => ({ ...s, gen: { ...s.gen, [dim]: true }, genErr: { ...s.genErr, [dim]: null } }));
    try {
      const data = await postJson("/api/generate-seed", { dimension: dim, existing: getModePool(dim), mode_id: "code" });
      const v = (data.seed || "").trim();
      if (!v) throw new Error("模型返回为空");
      const path = modePoolPath(dim);
      if (path && !getModePool(dim).includes(v)) update(path, [...getModePool(dim), v]);
    } catch (e) {
      setModePoolUI((s) => ({ ...s, genErr: { ...s.genErr, [dim]: e.message } }));
    } finally {
      setModePoolUI((s) => ({ ...s, gen: { ...s.gen, [dim]: false } }));
    }
  };
  const refreshModeSeeds = async () => {
    const idx = codeModeIdx();
    if (idx < 0) return;
    setModeRefreshing(true);
    setModeRefreshErr(null);
    try {
      const data = await postJson("/api/seed/refresh-all", { mode_id: "code" });
      update(`modes.${idx}.pool`, data.pool);
    } catch (e) {
      setModeRefreshErr(e.message);
    } finally {
      setModeRefreshing(false);
    }
  };

  const save = async () => {
    setError(null);
    try {
      await postJson("/api/config", { config });
      setSaved(true);
    } catch (e) {
      setError("保存失败: " + e.message);
    }
  };

  const g = config.global || {};
  const rnd = config.randomness || {};
  const tp = config?.providers?.tts || {};

  return (
    <div>
      <div className="section-title">全局配置</div>
      <div className="config-form">
        <div className="form-row">
          <label>
            <input
              type="checkbox"
              checked={!!config.mock}
              onChange={(e) => update("mock", e.target.checked)}
              style={{ width: "auto", marginRight: 8 }}
            />
            Mock 模式（无真实 key 时本地模拟，便于先跑通流程）
          </label>
        </div>

        <div className="section-title">随机性</div>
        <Field
          label="每抽抽取种子数 (per_open_draw)"
          value={rnd.per_open_draw ?? 2}
          onChange={(v) => update("randomness.per_open_draw", Number(v))}
        />

        <div className="section-title">随机种子池（twist pool）</div>
        <div className="note">四个维度各维护一组候选词条，每次开盲盒从中抽取。可手动增删改，也可用「🎲 生成」调用大模型补一条（已自动避开重复）。</div>
        {poolDims.map((dim) => (
          <div className="config-form" style={{ marginTop: "10px" }} key={dim}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div className="section-title" style={{ margin: 0 }}>{dimCn[dim]}（{dim}）</div>
              <button type="button" className="save-btn" style={{ padding: "6px 12px" }}
                disabled={poolUI.gen?.[dim]} onClick={() => genSeed(dim)}>
                {poolUI.gen?.[dim] ? "生成中…" : "🎲 生成新种子"}
              </button>
            </div>
            {getPool(dim).map((it, idx) => (
              <div style={{ display: "flex", gap: "6px", marginTop: "6px" }} key={idx}>
                <input value={it} onChange={(e) => editItem(dim, idx, e.target.value)} style={{ flex: 1 }} />
                <button type="button" className="save-btn"
                  style={{ padding: "6px 10px", background: "var(--danger)", borderColor: "var(--danger)" }}
                  onClick={() => removeItem(dim, idx)}>×</button>
              </div>
            ))}
            <div style={{ display: "flex", gap: "6px", marginTop: "6px" }}>
              <input placeholder={`新增一条${dimCn[dim]}…`}
                value={poolUI.addText?.[dim] || ""}
                onChange={(e) => setPoolUI((s) => ({ ...s, addText: { ...s.addText, [dim]: e.target.value } }))}
                onKeyDown={(e) => { if (e.key === "Enter") addItem(dim); }} style={{ flex: 1 }} />
              <button type="button" className="save-btn" style={{ padding: "6px 10px" }} onClick={() => addItem(dim)}>添加</button>
            </div>
            {poolUI.genErr?.[dim] && <div className="error">{poolUI.genErr[dim]}</div>}
          </div>
        ))}
        <button type="button" className="save-btn"
          style={{ marginTop: "10px", background: "transparent", color: "var(--muted)" }}
          onClick={resetPool}>↺ 恢复默认种子池</button>

        {codeModeIdx() >= 0 && (
          <>
            <div className="section-title" style={{ marginTop: 18 }}>代码模式独立工具池（code 专用）</div>
            <div className="note">代码盲盒使用独立种子池（与上方全局池互不影响），抽取「实用工具」方向。同样支持手动增删改、🎲 生成、一键刷新。</div>
            {poolDims.map((dim) => (
              <div className="config-form" style={{ marginTop: "10px" }} key={`mode-${dim}`}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div className="section-title" style={{ margin: 0 }}>{dimCn[dim]}（{dim}）</div>
                  <button type="button" className="save-btn" style={{ padding: "6px 12px" }}
                    disabled={modePoolUI.gen?.[dim]} onClick={() => genModeSeed(dim)}>
                    {modePoolUI.gen?.[dim] ? "生成中…" : "🎲 生成新种子"}
                  </button>
                </div>
                {getModePool(dim).map((it, idx) => (
                  <div style={{ display: "flex", gap: "6px", marginTop: "6px" }} key={idx}>
                    <input value={it} onChange={(e) => editModeItem(dim, idx, e.target.value)} style={{ flex: 1 }} />
                    <button type="button" className="save-btn"
                      style={{ padding: "6px 10px", background: "var(--danger)", borderColor: "var(--danger)" }}
                      onClick={() => removeModeItem(dim, idx)}>×</button>
                  </div>
                ))}
                <div style={{ display: "flex", gap: "6px", marginTop: "6px" }}>
                  <input placeholder={`新增一条${dimCn[dim]}…`}
                    value={modePoolUI.addText?.[dim] || ""}
                    onChange={(e) => setModePoolUI((s) => ({ ...s, addText: { ...s.addText, [dim]: e.target.value } }))}
                    onKeyDown={(e) => { if (e.key === "Enter") addModeItem(dim); }} style={{ flex: 1 }} />
                  <button type="button" className="save-btn" style={{ padding: "6px 10px" }} onClick={() => addModeItem(dim)}>添加</button>
                </div>
                {modePoolUI.genErr?.[dim] && <div className="error">{modePoolUI.genErr[dim]}</div>}
              </div>
            ))}
            <button type="button" className="save-btn"
              style={{ marginTop: "10px", background: "var(--accent)", color: "#fff", width: "100%" }}
              disabled={modeRefreshing}
              onClick={refreshModeSeeds}>
              {modeRefreshing ? "⏳ 正在刷新..." : "🔀 一键刷新 code 工具池"}
            </button>
            {modeRefreshErr && <div className="error">{modeRefreshErr}</div>}
          </>
        )}
      </div>

      <ModelSlot
        title="全局默认模型（各 Agent / Provider 留空时回退）"
        basePath="global"
        keyMap={GLOBAL_KEYMAP}
        config={config}
        update={update}
      />

      <ModelCards title="Agent1 创意（高温度，脑洞大）" basePath="agents.creative" config={config} update={update} />
      <ModelCards title="Agent2 规划（低温度，严谨格式化）" basePath="agents.planner" config={config} update={update} />
      <ModelCards title="Agent3 执行（文本类；图片/视频由下方 provider 覆盖）" basePath="agents.executor" config={config} update={update} />

      <div className="section-title">图片 / 视频 模型（执行 Agent 调多模态时覆盖，运行时按序 fallback）</div>
      <ModelCards title="图片模型" basePath="providers.image" config={config} update={update} />
      <ModelCards title="视频模型" basePath="providers.video" config={config} update={update} />

      <div className="section-title">播客 TTS 语音合成（文字 → 语音）</div>
      <div className="config-form">
        <div className="note">
          无需选择引擎：<b>不填 base_url / api_key</b> → 自动使用免费 Edge TTS；
          <b>同时填好 base_url / api_key</b> → 走云端大模型（用下方 model 指定模型，任意兼容 OpenAI /audio/speech 的厂商均可）；
          云端调用出错会自动回退 Edge TTS。
        </div>
        <Field
          label="音色 voice（edge-tts 例：zh-CN-XiaoxiaoNeural；云端例：alloy / echo / 厂商自定义名）"
          value={tp.voice}
          onChange={(v) => update("providers.tts.voice", v)}
          placeholder="留空=各引擎默认音色"
        />
      </div>
      <ModelCards
        title="TTS 模型（云端模式必填 model / base_url / api_key；未填 base_url/api_key 则默认免费 Edge TTS）"
        note="未填 base_url / api_key → 免费 Edge TTS（忽略 model）；同时填了 → 用此处 model 调用云端大模型，出错自动回退 Edge。"
        basePath="providers.tts"
        config={config}
        update={update}
      />

      <div className="paths">
        大文件目录（默认项目目录内，可配置）：<br />
        缓存: {paths.cache_dir}<br />
        产物: {paths.output_dir}<br />
        上传: {paths.data_dir}
      </div>

      {saved && <div className="note" style={{ color: "var(--accent-2)" }}>✓ 已保存</div>}
      {error && <div className="error">{error}</div>}
      <button className="save-btn" style={{ marginTop: 12 }} onClick={save}>
        保存配置
      </button>
    </div>
  );
}
