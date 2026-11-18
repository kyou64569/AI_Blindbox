import React, { useEffect, useRef, useState } from "react";
import { fetchJson, postJson } from "../lib/http.js";

/* ============================================================
 * 基础输入组件
 * ============================================================ */

function Field({ label, value, onChange, type = "text", placeholder = "", step }) {
  return (
    <div className="form-row">
      <label>{label}</label>
      <input
        type={type}
        step={step}
        value={value === null || value === undefined ? "" : value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

/**
 * 温度输入框。
 *
 * 为什么不能用受控 `<input type="number">` + `Number(value)`：
 * 输入 "0.8" 到 "0" 后立刻把 0 存回 state，浏览器此时再收到 "." 会认为
 * "0." 不是合法数字而把 value 置空，小数点根本进不来（中文输入法下更明显）。
 *
 * 改为「本地字符串态 + 失焦提交」：编辑期间不碰外部 state，
 * 用户可以自由输入 "0." / "0.8" / "1"，失焦或按回车时才解析成数字写入配置。
 * 解析不通过（空/非法）时回退为 null，即"留空=用全局默认"。
 */
function TemperatureInput({ value, onChange, id }) {
  const [draft, setDraft] = useState(() =>
    value === null || value === undefined || value === "" ? "" : String(value)
  );
  const lastExternal = useRef(value);

  // 外部值变化（如切换模型卡片/保存后重载）时同步回本地编辑态
  useEffect(() => {
    if (value === lastExternal.current) return;
    lastExternal.current = value;
    setDraft(value === null || value === undefined || value === "" ? "" : String(value));
  }, [value]);

  const commit = () => {
    const s = draft.trim();
    if (s === "") {
      lastExternal.current = null;
      onChange(null);
      return;
    }
    const n = Number(s);
    if (Number.isFinite(n)) {
      // 界面承诺 0~2，超范围直接收敛：否则 5 会原样写进配置发给供应商（多数端点 400）
      const clamped = Math.min(2, Math.max(0, n));
      lastExternal.current = clamped;
      onChange(clamped);
      if (clamped !== n) setDraft(String(clamped));
    } else {
      // 非法输入：还原成当前有效值，不把垃圾写进配置
      const cur = value === null || value === undefined || value === "" ? "" : String(value);
      lastExternal.current = value;
      setDraft(cur);
    }
  };

  return (
    <div className="form-row">
      <label>温度 temperature（可选，留空=用全局/默认）</label>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        autoComplete="off"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            e.currentTarget.blur();
          }
        }}
        placeholder="如 0.8"
      />
      <div className="note">支持 0 ~ 2 的小数，直接键入即可（如 0.8 / 1.2），回车或失焦生效。</div>
    </div>
  );
}

// 思考强度可选项：与后端 agents._THINKING_VALUES 一一对应
// 说明：Gemini 3+ 原生端点用 thinkingLevel（不支持 thinkingBudget），"关闭"映射为 minimal；
// OpenAI 兼容层由下方「思考参数方言」决定具体字段（不同网关写法不同）。
const THINKING_OPTIONS = [
  { value: "default", label: "默认（不指定，沿用模型默认思考水平）" },
  { value: "off", label: "关闭（Gemini 3 映射为 minimal，最接近关闭）" },
  { value: "low", label: "低（更快、更省 token）" },
  { value: "medium", label: "中（平衡）" },
  { value: "high", label: "高（推理最深，最慢）" },
];

// 思考参数方言：与后端 thinking_profiles.THINKING_PROFILES 一一对应。
// 新增厂商 = 后端注册表加一条 + 这里加一个选项，主流程零改动。
const THINKING_PROFILE_OPTIONS = [
  { value: "", label: "默认（未设置）：仅 reasoning_effort", hint: "兼容旧配置，请求体不变；接新网关时选下方对应方言" },
  { value: "openai-native", label: "openai-native · Qwen3 / vLLM", hint: "顶层 enable_thinking 开关；开启时附 reasoning_effort" },
  { value: "chat-template", label: "chat-template · Agnes 3.0 / 小红书 Dots", hint: "chat_template_kwargs.enable_thinking；顶层字段会被忽略" },
  { value: "thinking-flag", label: "thinking-flag · MiniMax-M3（字符串）", hint: 'thinking:"enabled"/"disabled"，附 reasoning_effort' },
  { value: "thinking-type", label: "thinking-type · 腾讯混元 hy3/hy4（对象）", hint: 'thinking:{type:"enabled"/"disabled"}；与上一条同名不同形状' },
  { value: "effort-only", label: "effort-only · 仅 reasoning_effort", hint: 'off 发 reasoning_effort:"none"；无独立 thinking 开关' },
  { value: "thinking-mode", label: "thinking-mode · Intern-S1（布尔）", hint: "thinking_mode:true/false，无强度档；须配 binary" },
  { value: "nvidia", label: "nvidia · NVIDIA NIM（布尔）", hint: "thinking:true/false，无强度档；发字符串会 400；须配 binary" },
];
const THINKING_CAPABILITY_OPTIONS = [
  { value: "", label: "默认 graded（真分级）", hint: "low / medium / high 各自生效" },
  { value: "graded", label: "graded · 真分级", hint: "低/中/高档分别下发" },
  { value: "binary", label: "binary · 只支持开/关", hint: "低/中/高归一成满血 + 高预算；NVIDIA NIM / Intern-S1 必选" },
  { value: "none", label: "none · 无思考能力", hint: "一个思考参数都不发" },
];

// 通用下拉：值非法时回退到第一项（空 = 未配置），并显示当前项 hint
function DialectSelect({ label, options, value, onChange }) {
  const cur = options.some((o) => o.value === value) ? value : "";
  const hit = options.find((o) => o.value === cur) || options[0];
  return (
    <div className="form-row">
      <label>{label}</label>
      <select value={cur} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <div className="note">{hit ? hit.hint : ""}</div>
    </div>
  );
}

function ThinkingProfileSelect({ value, onChange }) {
  return (
    <DialectSelect
      label="思考参数方言 thinking_profile（按网关选择）"
      options={THINKING_PROFILE_OPTIONS}
      value={value}
      onChange={onChange}
    />
  );
}

function ThinkingCapabilitySelect({ value, onChange }) {
  return (
    <DialectSelect
      label="思考能力 thinking_capability"
      options={THINKING_CAPABILITY_OPTIONS}
      value={value}
      onChange={onChange}
    />
  );
}

// 思考强度下拉：默认收起说明，避免每张卡片都被大段文字撑高
function ThinkingSelect({ value, onChange }) {
  // 值收敛到合法选项：配置文件里若被手改成非法值（如 "HIGH"/"关闭"/乱码），
  // 直接透传给 <select> 会让下拉显示空白，这里兜底回 default。
  const cur = THINKING_OPTIONS.some((o) => o.value === value) ? value : "default";
  const hit = THINKING_OPTIONS.find((o) => o.value === cur) || THINKING_OPTIONS[0];
  return (
    <div className="form-row">
      <label>思考强度 thinking（可选）</label>
      <select value={cur} onChange={(e) => onChange(e.target.value)}>
        {THINKING_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <div className="note">当前：{hit.label}。Gemini 原生端点走 thinkingLevel；OpenAI 兼容层按上方「思考参数方言」注入字段。</div>
    </div>
  );
}

// 最大输出 token 输入框：复用 TemperatureInput 的「本地字符串态 + 失焦提交」模式，
// 但解析为非负整数；空 -> null（留空=用模型默认上限）。
function MaxTokensInput({ value, onChange, id }) {
  const [draft, setDraft] = useState(() =>
    value === null || value === undefined || value === "" ? "" : String(value)
  );
  const lastExternal = useRef(value);
  useEffect(() => {
    if (value === lastExternal.current) return;
    lastExternal.current = value;
    setDraft(value === null || value === undefined || value === "" ? "" : String(value));
  }, [value]);
  const commit = () => {
    const s = draft.trim();
    if (s === "") {
      lastExternal.current = null;
      onChange(null);
      return;
    }
    const n = Number(s);
    if (Number.isFinite(n) && n > 0) {
      const v = Math.floor(n);
      lastExternal.current = v;
      onChange(v);
    } else {
      const cur = value === null || value === undefined || value === "" ? "" : String(value);
      lastExternal.current = value;
      setDraft(cur);
    }
  };
  return (
    <div className="form-row">
      <label>最大输出 token max_tokens（可选，留空=用模型默认上限）</label>
      <input
        id={id}
        type="text"
        inputMode="numeric"
        autoComplete="off"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            e.currentTarget.blur();
          }
        }}
        placeholder="如 4096"
      />
      <div className="note">限制模型单次输出的最大 token 数，正整数。留空则不限制，沿用模型默认上限。</div>
    </div>
  );
}

// 请求超时输入框：复用 TemperatureInput 的「本地字符串态 + 失焦提交」模式，
// 解析为正数（秒，允许小数）；空 -> null（留空=用调用点默认超时）。
function TimeoutInput({ value, onChange, id }) {
  const [draft, setDraft] = useState(() =>
    value === null || value === undefined || value === "" ? "" : String(value)
  );
  const lastExternal = useRef(value);
  useEffect(() => {
    if (value === lastExternal.current) return;
    lastExternal.current = value;
    setDraft(value === null || value === undefined || value === "" ? "" : String(value));
  }, [value]);
  const commit = () => {
    const s = draft.trim();
    if (s === "") {
      lastExternal.current = null;
      onChange(null);
      return;
    }
    const n = Number(s);
    if (Number.isFinite(n) && n > 0) {
      lastExternal.current = n;
      onChange(n);
    } else {
      const cur = value === null || value === undefined || value === "" ? "" : String(value);
      lastExternal.current = value;
      setDraft(cur);
    }
  };
  return (
    <div className="form-row">
      <label>请求超时 timeout（秒，可选，留空=用默认 90/120/180）</label>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        autoComplete="off"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            e.currentTarget.blur();
          }
        }}
        placeholder="如 180"
      />
      <div className="note">单次 API 调用最长等待秒数。调大 max_tokens 且开启思考时，长输出可能超时，可在此调高。留空沿用默认。</div>
    </div>
  );
}

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

/* ============================================================
 * 配置页通用纯函数 / 常量
 * ============================================================ */

const CFG_NAV = [
  { id: "connect", label: "模型与连接" },
  { id: "behavior", label: "行为与成本" },
  { id: "seeds", label: "种子池" },
  { id: "modes", label: "模式管理" },
  { id: "storage", label: "存储与路径" },
];

const SLOT_SECTIONS = ["agents", "providers"];

// 模型分组（可折叠）：顺序即页面顺序，global 是单槽位，其余是卡片数组
const GROUP_PATHS = [
  "global",
  "agents.creative",
  "agents.planner",
  "agents.executor",
  "providers.image",
  "providers.video",
  "providers.game",
  "providers.tts",
];
// 折叠状态持久化键。「1」= 折叠；与 demo.html 完全一致，便于两版切换不跳状态
const GRP_KEY = "mf:grp:";

const GLOBAL_KEYMAP = {
  model: "default_model",
  base_url: "default_base_url",
  api_key: "default_api_key",
  thinking: "default_thinking",
  thinking_profile: "default_thinking_profile",
  thinking_capability: "default_thinking_capability",
  max_tokens: "default_max_tokens",
  timeout: "default_timeout",
};

const cloneCfg = (o) => {
  try {
    return JSON.parse(JSON.stringify(o));
  } catch (e) {
    return null;
  }
};

// 递归比对叶子差异，数组按整体长度变化计 1 处，其余逐元素递归
function countDiff(a, b) {
  if (a === b) return 0;
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return 1;
  if (Array.isArray(a) !== Array.isArray(b)) return 1;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return 1;
    let n = 0;
    for (let i = 0; i < a.length; i++) n += countDiff(a[i], b[i]);
    return n;
  }
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  let n = 0;
  keys.forEach((k) => {
    n += countDiff(a[k], b[k]);
  });
  return n;
}

// 从 base_url 提取 host 用于摘要行展示
function slotHost(u) {
  const s = (u || "").trim();
  if (!s) return "";
  try {
    return new URL(s.replace(/\/+$/, "")).host;
  } catch (e) {
    return s.replace(/^https?:\/\//i, "").split("/")[0];
  }
}

const connListOf = (config) => (Array.isArray(config?.connections) ? config.connections : []);

const connById = (connections, id) =>
  (connections || []).find((c) => String(c.id ?? "").trim() === (id || "").trim()) || null;

// 统计某连接被多少个槽位/卡片引用
function connUsage(config, id) {
  let n = 0;
  SLOT_SECTIONS.forEach((sec) => {
    Object.values(config?.[sec] || {}).forEach((node) => {
      if (!node || typeof node !== "object") return;
      (node.models || []).forEach((c) => {
        if (c && c.connection === id) n++;
      });
      if (node.connection === id) n++;
    });
  });
  return n;
}

// 模型槽位卡片总数（用于顶部汇总）
function slotTotal(config) {
  let n = 0;
  SLOT_SECTIONS.forEach((sec) => {
    Object.values(config?.[sec] || {}).forEach((node) => {
      if (node && typeof node === "object" && Array.isArray(node.models)) n += node.models.length;
    });
  });
  return n;
}

// 高级折叠区的标题摘要：当前思考强度 / 方言 / 预算 / 超时
function advBadgeOf(getv) {
  return [
    getv("thinking") && getv("thinking") !== "default" ? getv("thinking") : "",
    getv("thinking_profile") || "",
    getv("max_tokens") ? "max " + getv("max_tokens") : "",
    getv("timeout") ? getv("timeout") + "s" : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/* ============================================================
 * 连接池（connections）：共享的 base_url + api_key，供所有槽位按 id 引用
 * ============================================================ */

function ConnectionsBlock({ config, update, openSlots, toggleSlot, setSlot, clearQuery }) {
  const conns = connListOf(config);
  const [testing, setTesting] = useState({});

  const add = () => {
    const id = "conn_" + Math.random().toString(36).slice(2, 8);
    update("connections", [...conns, { id, name: "新连接", base_url: "", api_key: "" }]);
    // 新增后立刻展开该行并清空搜索，避免"点了按钮什么都没发生"
    setSlot("conn-" + id, true);
    if (clearQuery) clearQuery();
  };
  const patch = (id, field, value) =>
    update("connections", conns.map((c) => (c.id === id ? { ...c, [field]: value } : c)));

  // 删除连接：引用处「落地」凭据后再解除引用 —— 等价于撤销共享，
  // 模型不会因为删掉连接而突然失去 base_url/key（否则下次开盒直接失败）。
  const del = (id) => {
    const conn = connById(conns, id) || {};
    SLOT_SECTIONS.forEach((sec) => {
      Object.entries(config?.[sec] || {}).forEach(([k, node]) => {
        if (!node || typeof node !== "object") return;
        (node.models || []).forEach((c, i) => {
          if (!c || c.connection !== id) return;
          if (!(c.base_url || "").trim()) update(`${sec}.${k}.models.${i}.base_url`, conn.base_url || "");
          if (!(c.api_key || "").trim()) update(`${sec}.${k}.models.${i}.api_key`, conn.api_key || "");
          update(`${sec}.${k}.models.${i}.connection`, "");
        });
        if (node.connection === id) {
          if (!(node.base_url || "").trim()) update(`${sec}.${k}.base_url`, conn.base_url || "");
          if (!(node.api_key || "").trim()) update(`${sec}.${k}.api_key`, conn.api_key || "");
          update(`${sec}.${k}.connection`, "");
        }
      });
    });
    update("connections", conns.filter((c) => c.id !== id));
    setTesting((m) => {
      const n = { ...m };
      delete n[id];
      return n;
    });
  };

  // 测试连通：直接复用 /api/models（能拉到模型列表即视为可用）
  const test = async (c) => {
    setTesting((m) => ({ ...m, [c.id]: { state: "loading" } }));
    try {
      const data = await postJson("/api/models", {
        base_url: c.base_url || "",
        api_key: c.api_key || "",
      });
      const n = ((data && data.models) || []).length;
      setTesting((m) => ({ ...m, [c.id]: n ? { state: "ok", count: n } : { state: "empty" } }));
    } catch (e) {
      setTesting((m) => ({ ...m, [c.id]: { state: "fail", err: e.message } }));
    }
  };

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "2px 0 6px" }}>
        <div className="cfg-sec-hint" style={{ flex: "1 1 auto" }}>
          供下方全部槽位复用，改一处即全局生效
        </div>
        <button type="button" className="icon-btn" onClick={add}>
          + 新增连接
        </button>
      </div>
      {conns.length === 0 ? (
        <div className="config-form" style={{ marginTop: 4 }}>
          <div className="note" style={{ margin: 0 }}>
            还没有连接。加一个连接（base_url + api_key），下面所有模型槽位就能直接选用，不必逐个重复填写；改 key 只需改这一处。
          </div>
        </div>
      ) : (
        <div className="slot-list">
          {conns.map((c) => {
            const t = testing[c.id] || {};
            const used = connUsage(config, c.id);
            const key = "conn-" + c.id;
            const open = !!openSlots[key];
            return (
              <div className="slot-item" key={c.id}>
                <div className={"slot-row" + (open ? " open" : "")} onClick={() => toggleSlot(key)}>
                  <span className={"slot-dot" + (t.state === "ok" ? " on" : "")}></span>
                  <span className="slot-name">{c.name || c.id}</span>
                  <span className="slot-model">{c.base_url || "（未填 base_url）"}</span>
                  <span className="slot-meta">{c.api_key ? "key ✓" : "无 key"}</span>
                  <span className="slot-meta">{used} 引用</span>
                  <span className="slot-actions" onClick={(e) => e.stopPropagation()}>
                    <button
                      type="button"
                      className="icon-btn"
                      disabled={t.state === "loading"}
                      onClick={() => test(c)}
                    >
                      {t.state === "loading" ? "…" : "测试"}
                    </button>
                    <button
                      type="button"
                      className="icon-btn danger"
                      onClick={() => del(c.id)}
                      title={"删除并清除 " + used + " 处引用"}
                    >
                      删
                    </button>
                  </span>
                  <span className="slot-meta">{open ? "▾" : "▸"}</span>
                </div>
                {t.state === "fail" && (
                  <div className="error" style={{ padding: "0 12px 8px", margin: 0 }}>{t.err}</div>
                )}
                {t.state === "ok" && (
                  <div className="note" style={{ padding: "0 12px 8px", margin: 0, color: "var(--accent-2)" }}>
                    连通正常，可用模型 {t.count} 个
                  </div>
                )}
                {t.state === "empty" && (
                  <div className="note" style={{ padding: "0 12px 8px", margin: 0 }}>
                    接口可达，但未返回模型列表（部分厂商不开放 /models）
                  </div>
                )}
                {open && (
                  <div className="slot-body">
                    <div className="form-row" style={{ marginTop: 10 }}>
                      <label>显示名称</label>
                      <input
                        value={c.name || ""}
                        onChange={(e) => patch(c.id, "name", e.target.value)}
                        placeholder="如 鲸鱼云 / 阶跃"
                      />
                    </div>
                    <div className="form-row">
                      <label>API Base URL</label>
                      <input
                        value={c.base_url || ""}
                        onChange={(e) => patch(c.id, "base_url", e.target.value)}
                        placeholder="https://api.example.com/v1"
                      />
                    </div>
                    <div className="form-row">
                      <label>API Key</label>
                      <input
                        type="password"
                        value={c.api_key || ""}
                        onChange={(e) => patch(c.id, "api_key", e.target.value)}
                        placeholder="sk-..."
                      />
                    </div>
                    <div className="note" style={{ margin: 0 }}>
                      连接 id：<code>{c.id}</code>（改名不影响引用）
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

/* ============================================================
 * 全局默认模型槽位（default_* 键）：带「高级」折叠，默认收起
 * ============================================================ */

function ModelSlot({ title, note, basePath, keyMap, config, update, connections, query, groupCollapsed, toggleGroup }) {
  const [models, setModels] = useState([]);
  const [fetching, setFetching] = useState(false);
  const [err, setErr] = useState(null);
  const [adv, setAdv] = useState(false);

  const node =
    basePath.split(".").reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), config) || {};
  // 嵌套解析 basePath（如 "agents.creative"），避免 config["agents.creative"] 直接索引为 undefined
  const getv = (k) => node[keyMap[k]] || "";
  const setv = (k, v) => update(`${basePath}.${keyMap[k]}`, v);

  const fetchModels = async () => {
    setErr(null);
    setFetching(true);
    try {
      // 槽位若引用了连接，凭据以连接为准（与运行时 _resolve_models 的优先级一致）
      const conn = connById(connections, node.connection) || {};
      const data = await postJson("/api/models", {
        base_url: conn.base_url || getv("base_url") || config.global?.default_base_url || "",
        api_key: conn.api_key || getv("api_key") || config.global?.default_api_key || "",
      });
      setModels((data && data.models) || []);
    } catch (e) {
      setErr(e.message);
    } finally {
      setFetching(false);
    }
  };

  // 搜索过滤：模型名 / 地址（无命中就整段隐藏）
  if (query) {
    const hay = [getv("model"), getv("base_url"), config.global?.default_model].join(" ").toLowerCase();
    if (!hay.includes(query)) return null;
  }

  const badge = advBadgeOf(getv);
  const mdl = (getv("model") || "").trim();
  const hostOf = slotHost(getv("base_url"));
  // 搜索命中时强制展开，否则「搜到了却看不见」
  const isOpen = query ? true : !groupCollapsed?.[basePath];

  return (
    <div className="slot-group">
      <div className={"slot-group-head" + (isOpen ? " open" : "")} onClick={() => toggleGroup(basePath)}>
        <span className="slot-group-caret">{isOpen ? "▾" : "▸"}</span>
        <span className="slot-group-title">{title}</span>
        {mdl ? (
          <span className="slot-group-model">{mdl}</span>
        ) : (
          <span className="slot-group-model empty">未配置 · 回退内置默认</span>
        )}
        {hostOf && <span className="slot-group-meta">{hostOf}</span>}
      </div>
      {isOpen && (
      <div className="slot-group-body">
        {note && <div className="note" style={{ marginBottom: 8 }}>{note}</div>}
        <div className="form-row">
          <label>API Base URL</label>
          <input
            value={getv("base_url")}
            onChange={(e) => setv("base_url", e.target.value)}
            placeholder="留空=用全局默认"
          />
        </div>
        <div className="form-row">
          <label>API Key</label>
          <input
            type="password"
            value={getv("api_key")}
            onChange={(e) => setv("api_key", e.target.value)}
            placeholder="留空=用全局默认"
          />
        </div>
        <div className="form-row">
          <label>模型 model（填好上方 base_url / key 后可点按钮拉取列表选择）</label>
          <div style={{ display: "flex", gap: 8 }}>
            <ModelPicker
              value={getv("model") || ""}
              options={models}
              onPick={(v) => setv("model", v)}
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
          {models.length > 0 && <div className="note">已获取 {models.length} 个模型，点下拉可随时改选。</div>}
          {err && <div className="error">{err}</div>}
        </div>
        <div className="adv-toggle" onClick={() => setAdv((v) => !v)}>
          {adv ? "▾" : "▸"} 高级：思考模式 / 预算 / 超时{badge ? "（" + badge + "）" : ""}
        </div>
        {adv && (
          <div className="adv-body">
            <ThinkingSelect value={getv("thinking")} onChange={(v) => setv("thinking", v)} />
            <ThinkingProfileSelect value={getv("thinking_profile")} onChange={(v) => setv("thinking_profile", v)} />
            <ThinkingCapabilitySelect value={getv("thinking_capability")} onChange={(v) => setv("thinking_capability", v)} />
            <MaxTokensInput id={`maxtok-${basePath}`} value={getv("max_tokens")} onChange={(v) => setv("max_tokens", v)} />
            <TimeoutInput id={`to-${basePath}`} value={getv("timeout")} onChange={(v) => setv("timeout", v)} />
          </div>
        )}
      </div>
      )}
    </div>
  );
}

/* ============================================================
 * 多模型卡片：单行摘要 + 点击展开编辑
 * ============================================================ */

function ModelCard({
  basePath,
  idx,
  card,
  total,
  config,
  update,
  connections,
  open,
  onToggle,
  advOpen,
  onToggleAdv,
}) {
  const [models, setModels] = useState([]);
  const [fetching, setFetching] = useState(false);
  const [err, setErr] = useState(null);

  const cardPath = `${basePath}.models.${idx}`;
  const getv = (k) => (card && card[k] != null ? card[k] : "");
  const setv = (k, v) => update(`${cardPath}.${k}`, v);

  const getModelsList = () => {
    const node = basePath
      .split(".")
      .reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), config);
    return node && Array.isArray(node.models) ? node.models : [];
  };

  const swap = (dir) => {
    const arr = getModelsList();
    const j = idx + dir;
    if (j < 0 || j >= arr.length) return;
    const next = [...arr];
    const t = next[idx];
    next[idx] = next[j];
    next[j] = t;
    update(`${basePath}.models`, next);
  };
  const del = () => {
    const arr = getModelsList();
    update(
      `${basePath}.models`,
      arr.filter((_, i) => i !== idx)
    );
  };
  const fetchModels = async () => {
    setErr(null);
    setFetching(true);
    try {
      const conn = connById(connections, getv("connection")) || {};
      const data = await postJson("/api/models", {
        base_url: conn.base_url || getv("base_url") || config.global?.default_base_url || "",
        api_key: conn.api_key || getv("api_key") || config.global?.default_api_key || "",
      });
      setModels((data && data.models) || []);
    } catch (e) {
      setErr(e.message);
    } finally {
      setFetching(false);
    }
  };

  const tv = getv("temperature");
  const mdl = (getv("model") || "").trim();
  const connId = (getv("connection") || "").trim();
  const conn = connById(connections, connId);
  // 摘要行显示「生效地址」：选了连接就用连接地址，否则用卡片自身的
  const host = slotHost(conn ? conn.base_url : getv("base_url"));
  const badge = advBadgeOf(getv);

  // 折叠态 = 单行摘要（序号 · 模型 · host · 温度 · 状态点 · 排序/删除）；点击整行展开编辑。
  // 展开态只常显「模型 / 连接 / base_url / key / 温度」，思考与预算收进「高级」折叠区。
  return (
    <div className="slot-item">
      <div className={"slot-row" + (open ? " open" : "")} onClick={onToggle}>
        <span className="slot-idx">{idx + 1}</span>
        <span className={"slot-model" + (mdl ? "" : " empty")}>{mdl || "（未填 · 回退上级默认）"}</span>
        {conn ? (
          <span className="slot-meta" style={{ color: "var(--accent-2)" }}>
            {conn.name || conn.id}
          </span>
        ) : null}
        {host ? <span className="slot-meta">{host}</span> : null}
        {tv !== "" && tv != null ? <span className="slot-meta">T {tv}</span> : null}
        <span className={"slot-dot" + (mdl ? " on" : "")}></span>
        <span className="slot-actions" onClick={(e) => e.stopPropagation()}>
          <button type="button" className="icon-btn" disabled={idx === 0} onClick={() => swap(-1)} title="上移">
            ↑
          </button>
          <button type="button" className="icon-btn" disabled={idx === total - 1} onClick={() => swap(1)} title="下移">
            ↓
          </button>
          <button type="button" className="icon-btn danger" onClick={del} title="删除（无确认）">
            删
          </button>
        </span>
        <span className="slot-meta">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <div className="slot-body">
          <div className="form-row" style={{ marginTop: 10 }}>
            <label>模型 model</label>
            <div style={{ display: "flex", gap: 8 }}>
              <ModelPicker
                value={getv("model") || ""}
                options={models}
                onPick={(v) => setv("model", v)}
                placeholder="模型名，如 gpt-4o-mini-tts"
                emptyLabel="（手填）"
              />
              <button
                type="button"
                className="save-btn"
                style={{ padding: "8px 12px", whiteSpace: "nowrap" }}
                disabled={fetching}
                onClick={fetchModels}
              >
                {fetching ? "获取中…" : "获取列表"}
              </button>
            </div>
            {models.length > 0 && <div className="note">已获取 {models.length} 个模型，点下拉可随时改选。</div>}
            {err && <div className="error">{err}</div>}
          </div>
          <div className="form-row">
            <label>连接（复用 base_url + api_key）</label>
            <select
              value={connId}
              onChange={(e) => {
                const v = e.target.value;
                setv("connection", v);
                // 选中连接后清空卡片自身的凭据，避免两处生效造成误解
                if (v) {
                  setv("base_url", "");
                  setv("api_key", "");
                }
              }}
              style={{
                height: 36,
                background: "var(--panel)",
                color: "var(--text)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: "0 8px",
                fontSize: 13,
              }}
            >
              <option value="">（不用连接 · 下方手填）</option>
              {(connections || []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name || c.id} — {slotHost(c.base_url)}
                </option>
              ))}
            </select>
          </div>
          {conn ? (
            <div className="note" style={{ margin: "-6px 0 12px" }}>
              走连接「{conn.name || conn.id}」：{conn.base_url || "（地址为空）"} ·{" "}
              {conn.api_key ? "key 已配置" : "key 未配置"}
            </div>
          ) : (
            <>
              <div className="form-row">
                <label>API Base URL</label>
                <input
                  value={getv("base_url")}
                  onChange={(e) => setv("base_url", e.target.value)}
                  placeholder="留空=用全局默认"
                />
              </div>
              <div className="form-row">
                <label>API Key</label>
                <input
                  type="password"
                  value={getv("api_key")}
                  onChange={(e) => setv("api_key", e.target.value)}
                  placeholder="留空=用全局默认"
                />
              </div>
            </>
          )}
          <TemperatureInput id={`temp-${basePath}-${idx}`} value={tv} onChange={(v) => setv("temperature", v)} />
          <div className="adv-toggle" onClick={onToggleAdv}>
            {advOpen ? "▾" : "▸"} 高级：思考模式 / 预算 / 超时{badge ? "（" + badge + "）" : ""}
          </div>
          {advOpen && (
            <div className="adv-body">
              <MaxTokensInput
                id={`maxtok-${basePath}-${idx}`}
                value={getv("max_tokens")}
                onChange={(v) => setv("max_tokens", v)}
              />
              <TimeoutInput id={`to-${basePath}-${idx}`} value={getv("timeout")} onChange={(v) => setv("timeout", v)} />
              <ThinkingSelect value={getv("thinking")} onChange={(v) => setv("thinking", v)} />
              <ThinkingProfileSelect value={getv("thinking_profile")} onChange={(v) => setv("thinking_profile", v)} />
              <ThinkingCapabilitySelect
                value={getv("thinking_capability")}
                onChange={(v) => setv("thinking_capability", v)}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ModelCards({
  title,
  note,
  basePath,
  config,
  update,
  connections,
  query,
  openSlots,
  toggleSlot,
  advOpen,
  toggleAdv,
  groupCollapsed,
  toggleGroup,
}) {

  const node = basePath
    .split(".")
    .reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), config);
  const all = node && Array.isArray(node.models) ? node.models : [];

  // 搜索命中：模型名 / 卡片地址 / 连接名 / 连接地址。
  // 注意：idx 必须指回原数组（all），否则编辑会写到错误的卡片上。
  const pairs = query
    ? all
        .map((card, idx) => ({ card, idx }))
        .filter(({ card }) => {
          const c = connById(connections, card.connection) || {};
          return [card.model, card.base_url, c.name, c.base_url].join(" ").toLowerCase().includes(query);
        })
    : all.map((card, idx) => ({ card, idx }));

  if (query && pairs.length === 0) return null;

  // 搜索时强制展开，否则「搜到了却看不见」
  const isOpen = query ? true : !groupCollapsed?.[basePath];
  const first = all[0] || {};
  const primary = (first.model || "").trim();
  const hostOf = slotHost(first.base_url || (connById(connections, first.connection) || {}).base_url);
  const add = () =>
    update(`${basePath}.models`, [
      ...all,
      {
        _id: "card_" + Math.random().toString(36).slice(2),
        model: "",
        base_url: "",
        api_key: "",
        connection: "",
        temperature: null,
        thinking: "default",
        max_tokens: null,
        timeout: null,
      },
    ]);

  return (
    <div className="slot-group">
      <div className={"slot-group-head" + (isOpen ? " open" : "")} onClick={() => toggleGroup(basePath)}>
        <span className="slot-group-caret">{isOpen ? "▾" : "▸"}</span>
        <span className="slot-group-title">{title}</span>
        <span className="slot-group-count">{all.length} 个</span>
        {primary ? (
          <span className="slot-group-model">{primary}</span>
        ) : (
          <span className="slot-group-model empty">未配置 · 回退上级</span>
        )}
        {primary && all.length > 1 && <span className="slot-group-meta">+{all.length - 1}</span>}
        {hostOf && <span className="slot-group-meta">{hostOf}</span>}
      </div>
      {isOpen && (
      <div className="slot-group-body">
        {note && <div className="note" style={{ marginBottom: 8 }}>{note}</div>}
        <div className="cfg-sec-hint" style={{ margin: "0 0 6px" }}>
          {pairs.length}
          {query ? " / " + all.length : ""} 个模型 · 运行时按序 fallback（上一个失败自动用下一个）
        </div>
        {all.length === 0 ? (
        <div className="note" style={{ color: "var(--muted)" }}>
          暂无模型卡片。若留空：Agent 执行回退全局默认模型；Provider 图片/视频回退另一 Provider 置顶项；TTS 回退免费 Edge TTS。
        </div>
      ) : (
        <div className="slot-list" style={{ marginTop: 0 }}>
          {pairs.map(({ card, idx }) => {
            const cardPath = `${basePath}.models.${idx}`;
            return (
              <ModelCard
                key={card._id || cardPath}
                basePath={basePath}
                idx={idx}
                card={card}
                total={all.length}
                config={config}
                update={update}
                connections={connections}
                open={!!openSlots[cardPath]}
                onToggle={() => toggleSlot(cardPath)}
                advOpen={!!advOpen["adv-" + cardPath]}
                onToggleAdv={() => toggleAdv("adv-" + cardPath)}
              />
            );
          })}
        </div>
      )}
        <button
          type="button"
          className="save-btn"
          style={{ marginTop: 10, padding: "7px 14px", background: "transparent", color: "var(--accent)", borderColor: "var(--accent)" }}
          onClick={add}
        >
          + 添加模型
        </button>
      </div>
      )}
    </div>
  );
}

/* ============================================================
 * 种子池：单行摘要（维度 / 条数 / 示例 / 生成 / 展开）
 * ============================================================ */

function SeedPoolBlock({ dims, dimCn, getPool, keyPrefix, openSlots, toggleSlot, genUI, onGen, onEdit, onRemove, onAdd, addText, setAddText }) {
  return (
    <div className="slot-list">
      {dims.map((dim) => {
        // 展开键必须带池前缀：全局池与 code 池的维度名相同，
        // 共用 "pool-<dim>" 会导致展开一个连带展开另一个
        const key = `${keyPrefix}-${dim}`;
        const items = getPool(dim);
        const open = !!openSlots[key];
        return (
          <div className="slot-item" key={dim}>
            <div className={"slot-row" + (open ? " open" : "")} onClick={() => toggleSlot(key)}>
              <span className="slot-name" style={{ minWidth: 40 }}>{dimCn[dim]}</span>
              <span className="slot-model empty">
                {items.length} 条{items.length ? " · 例：" + (items[0] || "") : ""}
              </span>
              <span className="slot-actions" onClick={(e) => e.stopPropagation()}>
                <button type="button" className="icon-btn" disabled={genUI.gen?.[dim]} onClick={() => onGen(dim)}>
                  {genUI.gen?.[dim] ? "生成中…" : "🎲 生成"}
                </button>
              </span>
              <span className="slot-meta">{open ? "▾" : "▸"}</span>
            </div>
            {open && (
              <div className="slot-body" style={{ paddingTop: 10 }}>
                {items.map((it, idx) => (
                  <div style={{ display: "flex", gap: 6, marginBottom: 6 }} key={idx}>
                    <input value={it} onChange={(e) => onEdit(dim, idx, e.target.value)} style={{ flex: 1 }} />
                    <button type="button" className="icon-btn danger" onClick={() => onRemove(dim, idx)}>
                      ×
                    </button>
                  </div>
                ))}
                <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  <input
                    placeholder={"新增一条" + dimCn[dim] + "…"}
                    value={addText?.[dim] || ""}
                    onChange={(e) => setAddText(dim, e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") onAdd(dim);
                    }}
                    style={{ flex: 1 }}
                  />
                  <button type="button" className="icon-btn" onClick={() => onAdd(dim)}>
                    添加
                  </button>
                </div>
                {genUI.genErr?.[dim] && <div className="error">{genUI.genErr[dim]}</div>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ============================================================
 * 主面板
 * ============================================================ */

const VALID_EXECUTOR_TYPES = [
  "text", "image", "video", "tts", "game", "quiz", "character_card",
  "code-spec", "code-cli", "illustrated_story", "video_storyboard", "song",
];
const VALID_OUTPUT_HANDLERS = [
  "text", "image_url", "video_file", "audio_file", "spec_md", "game_html",
  "quiz_html", "character_card", "image_strip", "song",
];

const POOL_DIMS = ["theme", "style", "constraint", "twist"];
const DIM_CN = { theme: "主题", style: "风格", constraint: "约束", twist: "反转" };

export default function ConfigPanel() {
  const [config, setConfig] = useState(null);
  const [paths, setPaths] = useState({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);
  // 加载 / 保存时的配置快照：用于「N 处未保存改动」计数与撤销
  const [savedSnap, setSavedSnap] = useState(null);

  // 折叠 / 导航 / 搜索 UI 状态（仅在本次会话内有效）
  const [openSlots, setOpenSlots] = useState({});
  const [advOpen, setAdvOpen] = useState({});
  const [cfgActive, setCfgActive] = useState("connect");
  const [cfgQuery, setCfgQuery] = useState("");

  // 模型分组的折叠状态：默认折叠（槽位一多，全展开就是 8 屏），localStorage 持久化。
  // 键与语义与 demo.html 的 mf:grp:<basePath> 保持一致：「1」= 折叠。
  const [groupCollapsed, setGroupCollapsed] = useState(() => {
    const m = {};
    GROUP_PATHS.forEach((bp) => {
      let v = true;
      try {
        const raw = localStorage.getItem(GRP_KEY + bp);
        if (raw !== null) v = raw === "1";
      } catch (e) {
        /* localStorage 不可用时按默认（折叠） */
      }
      m[bp] = v;
    });
    return m;
  });

  const [poolUI, setPoolUI] = useState({ addText: {}, gen: {}, genErr: {} });
  const initialPoolRef = useRef(null);

  // 自定义模式表单状态（必须在提前 return 之前声明，否则触发 Hooks 顺序违规）
  const [showNewMode, setShowNewMode] = useState(false);
  const [newMode, setNewMode] = useState({});
  const [newModeErr, setNewModeErr] = useState(null);
  // code 模式独立工具池 UI 状态（同样必须在提前 return 之前声明）
  const [modePoolUI, setModePoolUI] = useState({ addText: {}, gen: {}, genErr: {} });
  const [modeRefreshing, setModeRefreshing] = useState(false);
  const [modeRefreshErr, setModeRefreshErr] = useState(null);
  // 全局种子池「一键刷新」状态（同样必须在提前 return 之前声明）
  const [refreshingAll, setRefreshingAll] = useState(false);
  const [refreshErr, setRefreshErr] = useState(null);
  // TTS 可用音色（Edge + 云端），来自 /api/tts/voices
  const [voices, setVoices] = useState({ edge: [], cloud: [], current: "" });

  const ready = !!config;

  const loadVoices = () => {
    fetchJson("/api/tts/voices")
      .then((d) => setVoices({ edge: d?.edge || [], cloud: d?.cloud || [], current: d?.current || "" }))
      .catch(() => {});
  };

  useEffect(() => {
    fetchJson("/api/config")
      .then((d) => {
        setConfig(d.config);
        setPaths(d.paths || {});
        setSavedSnap(cloneCfg(d.config));
        if (initialPoolRef.current === null) initialPoolRef.current = d.config?.randomness?.pool || {};
      })
      .catch((e) => setError("加载配置失败: " + e.message));
    loadVoices();
  }, []);

  // 滚动联动高亮：取最后一个已滚过阈值线的分组
  useEffect(() => {
    if (!ready) return undefined;
    const onScroll = () => {
      let cur = CFG_NAV[0].id;
      CFG_NAV.forEach((s) => {
        const el = document.getElementById("cfg-" + s.id);
        if (el && el.getBoundingClientRect().top <= 130) cur = s.id;
      });
      setCfgActive((prev) => (prev === cur ? prev : cur));
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, [ready]);

  if (!config) return <div className="note">加载中…</div>;

  const update = (path, value) => {
    setConfig((prev) => {
      // prev 为 null（配置还没加载完）时直接跳过，避免 JSON.stringify(null) -> "null"
      if (!prev || typeof prev !== "object") return prev;
      const next = JSON.parse(JSON.stringify(prev));
      const keys = path.split(".");
      let node = next;
      for (let i = 0; i < keys.length - 1; i++) {
        const k = keys[i];
        // 中间层缺失或不是对象时按需创建：原实现直接 node = node[k]，
        // 一旦某层不存在，下一轮就是 undefined[k] -> TypeError，整个面板白屏。
        // 下一段 key 是纯数字则建数组，否则建对象。
        if (node[k] === null || typeof node[k] !== "object") {
          node[k] = /^\d+$/.test(keys[i + 1]) ? [] : {};
        }
        node = node[k];
      }
      node[keys[keys.length - 1]] = value;
      return next;
    });
    setSaved(false);
  };

  // ---- 未保存改动计数 / 撤销 / 导航 ----
  const dirtyCount = savedSnap ? countDiff(savedSnap, config) : 0;
  const undoConfig = () => {
    if (!savedSnap) return;
    setConfig(cloneCfg(savedSnap));
    setSaved(false);
    setError(null);
  };
  const jumpCfg = (id) => {
    setCfgActive(id);
    const el = document.getElementById("cfg-" + id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const toggleSlot = (k) => setOpenSlots((m) => ({ ...m, [k]: !m[k] }));
  const setSlot = (k, v) => setOpenSlots((m) => ({ ...m, [k]: v }));
  const toggleAdv = (k) => setAdvOpen((m) => ({ ...m, [k]: !m[k] }));
  const toggleGroup = (bp) =>
    setGroupCollapsed((m) => {
      const nv = !m[bp];
      try {
        localStorage.setItem(GRP_KEY + bp, nv ? "1" : "0");
      } catch (e) {
        /* ignore */
      }
      return { ...m, [bp]: nv };
    });
  const setAllGroups = (collapsed) => {
    const m = {};
    GROUP_PATHS.forEach((bp) => {
      m[bp] = collapsed;
      try {
        localStorage.setItem(GRP_KEY + bp, collapsed ? "1" : "0");
      } catch (e) {
        /* ignore */
      }
    });
    setGroupCollapsed(m);
  };
  const connections = connListOf(config);
  const query = (cfgQuery || "").trim().toLowerCase();

  // ---- twist pool 编辑器（推荐混合方案：4 维可手动增删改 + 🎲 大模型生成） ----
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
  const refreshAllSeeds = async () => {
    setRefreshingAll(true);
    setRefreshErr(null);
    try {
      const data = await postJson("/api/seed/refresh-all", {});
      update("randomness.pool", data.pool);
    } catch (e) {
      setRefreshErr(e.message);
    } finally {
      setRefreshingAll(false);
    }
  };

  // ---- code 模式独立工具池（与全局池同能力：手动增删 + 🎲 生成 + 整体刷新） ----
  const codeModeIdx = () => (config?.modes || []).findIndex((m) => m.id === "code");
  const getModePool = (dim) => {
    const i = codeModeIdx();
    return (i >= 0 && config?.modes?.[i]?.pool?.[dim]) || [];
  };
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
      const data = await postJson("/api/generate-seed", {
        dimension: dim,
        existing: getModePool(dim),
        mode_id: "code",
      });
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
      setSavedSnap(cloneCfg(config));
      // 保存后刷新可用音色，反映新配置
      loadVoices();
    } catch (e) {
      setError("保存失败: " + e.message);
    }
  };

  // ---- 自定义模式 ----
  const addCustomMode = () => {
    setNewModeErr(null);
    const m = newMode || {};
    const id = (m.id || "").trim();
    const name = (m.name || "").trim();
    if (!id) {
      setNewModeErr("模式 id 不能为空");
      return;
    }
    if (!/^[a-z][a-z0-9-]*$/.test(id)) {
      setNewModeErr("id 只能包含小写字母、数字与中划线，且以字母开头");
      return;
    }
    if (!name) {
      setNewModeErr("模式名称不能为空");
      return;
    }
    const etype = m.executor_type || "text";
    if (!VALID_EXECUTOR_TYPES.includes(etype)) {
      setNewModeErr("executor_type 不在合法枚举内");
      return;
    }
    const oh = m.output_handler || "text";
    if (!VALID_OUTPUT_HANDLERS.includes(oh)) {
      setNewModeErr("output_handler 不在合法枚举内");
      return;
    }
    if ((config.modes || []).some((x) => x.id === id)) {
      setNewModeErr("该 id 已存在，请换一个");
      return;
    }
    update("modes", [
      ...(config.modes || []),
      {
        id,
        name,
        executor_type: etype,
        output_handler: oh,
        creative_prompt: (m.creative_prompt || "").trim(),
        planner_template: (m.planner_template || "").trim(),
      },
    ]);
    setShowNewMode(false);
    setNewMode({});
    setSaved(false);
  };

  const g = config.global || {};
  const rnd = config.randomness || {};
  const tp = config?.providers?.tts || {};
  const voiceOptions = [
    ...(voices.edge || []).map((v) => v && v.id),
    ...(voices.cloud || []).map((v) => v && v.id),
  ].filter(Boolean);

  // 段落级搜索：查询时若本段所有卡片都不命中，ModelCards 自己会返回 null
  const shared = { config, update, connections, query, openSlots, toggleSlot, advOpen, toggleAdv, groupCollapsed, toggleGroup };

  return (
    <div>
      <div className="cfg-layout">
        <nav className="cfg-nav">
          {CFG_NAV.map((s) => (
            <div
              key={s.id}
              className={"cfg-nav-item" + (cfgActive === s.id ? " active" : "")}
              onClick={() => jumpCfg(s.id)}
            >
              {s.label}
            </div>
          ))}
        </nav>

        <div className="cfg-main">
          {/* ---------- 1. 模型与连接 ---------- */}
          <section id="cfg-connect" className="cfg-sec">
            <div className="cfg-sec-head">
              模型与连接
              <span className="cfg-sec-hint">同一槽位内自上而下按序 fallback；留空即回退全局默认</span>
            </div>

            <div className="cfg-toolbar">
              <input
                className="seed-input"
                style={{ flex: "1 1 auto", minWidth: 150, maxWidth: 320 }}
                placeholder="搜索模型 / host / 连接名…"
                value={cfgQuery}
                onChange={(e) => setCfgQuery(e.target.value)}
              />
              {cfgQuery && (
                <button type="button" className="icon-btn" onClick={() => setCfgQuery("")}>
                  清除
                </button>
              )}
              <button type="button" className="icon-btn" onClick={() => setAllGroups(false)}>
                全部展开
              </button>
              <button type="button" className="icon-btn" onClick={() => setAllGroups(true)}>
                全部折叠
              </button>
              <span className="cfg-sec-hint" style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>
                {connections.length} 个连接 · {slotTotal(config)} 个模型槽位
              </span>
            </div>

            <ConnectionsBlock
              config={config}
              update={update}
              openSlots={openSlots}
              toggleSlot={toggleSlot}
              setSlot={setSlot}
              clearQuery={() => setCfgQuery("")}
            />

            <ModelSlot
              title="全局默认模型"
              note="各 Agent / Provider 留空时回退到这里"
              basePath="global"
              keyMap={GLOBAL_KEYMAP}
              config={config}
              update={update}
              connections={connections}
              query={query}
              groupCollapsed={groupCollapsed}
              toggleGroup={toggleGroup}
            />

            <ModelCards title="Agent1 · 创意" note="高温度，脑洞大" basePath="agents.creative" {...shared} />
            <ModelCards title="Agent2 · 规划" note="低温度，严谨格式化" basePath="agents.planner" {...shared} />
            <ModelCards
              title="Agent3 · 执行"
              note="图片 / 视频由下方 provider 覆盖"
              basePath="agents.executor"
              {...shared}
            />

            <div className="slot-group-sep">多模态（执行 Agent 调图片 / 视频 / 游戏时覆盖）</div>
            <ModelCards title="图片模型" basePath="providers.image" {...shared} />
            <ModelCards title="视频模型" basePath="providers.video" {...shared} />
            <ModelCards title="小游戏模型" basePath="providers.game" {...shared} />

            <div className="slot-group-sep">播客 TTS 语音合成（文字 → 语音）</div>
            <div className="config-form" style={{ marginTop: 8 }}>
              <div className="note" style={{ marginTop: 0 }}>
                无需选择引擎：<b>不填 base_url / api_key</b> → 自动使用免费 Edge TTS；
                <b>同时填好 base_url / api_key</b> → 走云端大模型（用下方 model 指定模型，任意兼容 OpenAI
                /audio/speech 的厂商均可）；云端调用出错会自动回退 Edge TTS。
              </div>
              <div className="form-row">
                <label>音色 Voice（不选=各引擎默认；Edge 例 zh-CN-XiaoxiaoNeural，云端例 alloy / 厂商自定义名）</label>
                <ModelPicker
                  value={tp.voice}
                  options={voiceOptions}
                  onPick={(v) => update("providers.tts.voice", v)}
                  placeholder="Edge 例: zh-CN-XiaoxiaoNeural · 云端直接手写音色名"
                  emptyLabel="（留空=默认音色）"
                />
                <div className="note">可从下拉建议中随时改选，也可手写任意音色名</div>
              </div>
            </div>
            <ModelCards
              title="TTS 模型"
              note="未填 base_url / api_key → 免费 Edge TTS（忽略 model）；同时填了 → 用此处 model 调用云端大模型，出错自动回退 Edge。"
              basePath="providers.tts"
              {...shared}
            />
          </section>

          {/* ---------- 2. 行为与成本 ---------- */}
          <section id="cfg-behavior" className="cfg-sec">
            <div className="cfg-sec-head">
              行为与成本
              <span className="cfg-sec-hint">Mock 开关 / 随机性 / Token 成本换算</span>
            </div>
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
            </div>
            <div className="config-form">
              <div className="section-title" style={{ marginTop: 0 }}>成本估算（Token 追踪）</div>
              <Field
                label="每千 token 单价（¥，0 = 不估算成本；历史面板与统计页按此换算）"
                type="number"
                step="0.001"
                value={g.tokens_price ?? 0}
                onChange={(v) => update("global.tokens_price", v === "" ? 0 : Number(v))}
                placeholder="如 0.01（即每千 token 一分钱）"
              />
            </div>
          </section>

          {/* ---------- 3. 种子池 ---------- */}
          <section id="cfg-seeds" className="cfg-sec">
            <div className="cfg-sec-head">
              种子池
              <span className="cfg-sec-hint">四维候选词 + code 专用工具池（手填 / 生成 / 一键刷新）</span>
            </div>
            <div className="config-form">
              <div className="note" style={{ marginTop: 0 }}>
                四个维度各维护一组候选词条，每次开盲盒从中抽取。可手动增删改，也可用「🎲 生成」调用大模型补一条（已自动避开重复）。
              </div>
              <SeedPoolBlock
                dims={POOL_DIMS}
                dimCn={DIM_CN}
                getPool={getPool}
                keyPrefix="pool"
                openSlots={openSlots}
                toggleSlot={toggleSlot}
                genUI={poolUI}
                onGen={genSeed}
                onEdit={editItem}
                onRemove={removeItem}
                onAdd={addItem}
                addText={poolUI.addText}
                setAddText={(dim, v) => setPoolUI((s) => ({ ...s, addText: { ...s.addText, [dim]: v } }))}
              />
              <button
                type="button"
                className="save-btn"
                style={{ marginTop: 10, background: "transparent", color: "var(--muted)" }}
                onClick={resetPool}
              >
                ↺ 恢复默认种子池
              </button>
              <button
                type="button"
                className="save-btn"
                style={{ marginTop: 6, background: "var(--accent)", color: "#fff", width: "100%" }}
                disabled={refreshingAll}
                onClick={refreshAllSeeds}
              >
                {refreshingAll ? "⏳ 正在刷新..." : "🔀 一键刷新种子池"}
              </button>
              {refreshErr && <div className="error">{refreshErr}</div>}

              {codeModeIdx() >= 0 && (
                <>
                  <div className="section-title" style={{ marginTop: 18 }}>代码模式独立工具池（code 专用）</div>
                  <div className="note">
                    代码盲盒使用独立种子池（与上方全局池互不影响），抽取「实用工具」方向。同样支持手动增删改、🎲 生成、一键刷新。
                  </div>
                  <SeedPoolBlock
                    dims={POOL_DIMS}
                    dimCn={DIM_CN}
                    getPool={getModePool}
                    keyPrefix="mpool"
                    openSlots={openSlots}
                    toggleSlot={toggleSlot}
                    genUI={modePoolUI}
                    onGen={genModeSeed}
                    onEdit={editModeItem}
                    onRemove={removeModeItem}
                    onAdd={addModeItem}
                    addText={modePoolUI.addText}
                    setAddText={(dim, v) => setModePoolUI((s) => ({ ...s, addText: { ...s.addText, [dim]: v } }))}
                  />
                  <button
                    type="button"
                    className="save-btn"
                    style={{ marginTop: 10, background: "var(--accent)", color: "#fff", width: "100%" }}
                    disabled={modeRefreshing}
                    onClick={refreshModeSeeds}
                  >
                    {modeRefreshing ? "⏳ 正在刷新..." : "🔀 一键刷新 code 工具池"}
                  </button>
                  {modeRefreshErr && <div className="error">{modeRefreshErr}</div>}
                </>
              )}
            </div>
          </section>

          {/* ---------- 4. 模式管理 ---------- */}
          <section id="cfg-modes" className="cfg-sec">
            <div className="cfg-sec-head">
              模式管理
              <span className="cfg-sec-hint">新增自定义模式，保存后可在开盲盒页选择</span>
            </div>
            <div className="config-form">
              <div className="note" style={{ marginTop: 0 }}>
                已有 {config.modes?.length || 0} 个模式。可新增自定义模式（id 需唯一、executor_type 需在合法枚举内），保存后即可在开盲盒页选择。
              </div>
              {!showNewMode ? (
                <button type="button" className="save-btn" onClick={() => setShowNewMode(true)}>
                  ＋ 新建模式
                </button>
              ) : (
                <>
                  <div className="form-row">
                    <label>模式 id（唯一，英文小写+中划线，如 my-game）</label>
                    <input
                      value={newMode.id || ""}
                      onChange={(e) => setNewMode((s) => ({ ...s, id: e.target.value }))}
                      placeholder="如 my-mode"
                    />
                  </div>
                  <div className="form-row">
                    <label>模式名称 *</label>
                    <input
                      value={newMode.name || ""}
                      onChange={(e) => setNewMode((s) => ({ ...s, name: e.target.value }))}
                      placeholder="如 我的小游戏"
                    />
                  </div>
                  <div className="form-row">
                    <label>执行器类型 executor_type *</label>
                    <select
                      value={newMode.executor_type || "text"}
                      onChange={(e) => setNewMode((s) => ({ ...s, executor_type: e.target.value }))}
                    >
                      {VALID_EXECUTOR_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </select>
                    <div className="note">
                      text=文本 · image=图片 · video=视频 · tts=播客 · game=小游戏 · quiz=测验 · song=歌曲 ·
                      illustrated_story=插画故事 · video_storyboard=视频分镜 · code-spec=代码 spec
                    </div>
                  </div>
                  <div className="form-row">
                    <label>成品类型 output_handler</label>
                    <select
                      value={newMode.output_handler || "text"}
                      onChange={(e) => setNewMode((s) => ({ ...s, output_handler: e.target.value }))}
                    >
                      {VALID_OUTPUT_HANDLERS.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="form-row">
                    <label>创意提示词 creative_prompt（可选）</label>
                    <textarea
                      value={newMode.creative_prompt || ""}
                      onChange={(e) => setNewMode((s) => ({ ...s, creative_prompt: e.target.value }))}
                      placeholder="留空则用默认创意 Agent 提示词"
                    />
                  </div>
                  <div className="form-row">
                    <label>规划模板 planner_template（可选）</label>
                    <textarea
                      value={newMode.planner_template || ""}
                      onChange={(e) => setNewMode((s) => ({ ...s, planner_template: e.target.value }))}
                      placeholder="留空则用默认规划模板"
                    />
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button type="button" className="save-btn" onClick={addCustomMode}>
                      添加模式
                    </button>
                    <button
                      type="button"
                      className="save-btn"
                      style={{ background: "transparent", color: "var(--muted)" }}
                      onClick={() => setShowNewMode(false)}
                    >
                      取消
                    </button>
                  </div>
                  {newModeErr && <div className="error">{newModeErr}</div>}
                </>
              )}
            </div>
          </section>

          {/* ---------- 5. 存储与路径 ---------- */}
          <section id="cfg-storage" className="cfg-sec">
            <div className="cfg-sec-head">
              存储与路径
              <span className="cfg-sec-hint">缓存 / 产物 / 上传目录</span>
            </div>
            <div className="config-form">
              <div className="paths">
                大文件目录（默认项目目录内，可配置）：
                <br />
                缓存: {paths.cache_dir}
                <br />
                产物: {paths.output_dir}
                <br />
                上传: {paths.data_dir}
              </div>
            </div>
          </section>
        </div>
      </div>

      {/* 底部常驻保存条：任何滚动位置都能直接保存 */}
      <div className="cfg-savebar">
        <span className={"sb-text" + (dirtyCount ? " dirty" : "")}>
          {dirtyCount ? dirtyCount + " 处未保存改动" : saved ? "✓ 已保存" : "尚未修改"}
        </span>
        <button type="button" className="icon-btn" style={{ padding: "7px 14px" }} disabled={!dirtyCount} onClick={undoConfig}>
          撤销
        </button>
        <button type="button" className="save-btn" disabled={!dirtyCount} onClick={save}>
          保存配置
        </button>
        {error && (
          <div className="error" style={{ flex: "1 1 100%", margin: 0 }}>
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
