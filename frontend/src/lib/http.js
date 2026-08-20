/**
 * 统一的 JSON 请求封装。
 *
 * 为什么需要：项目里原本到处是
 *     const data = await resp.json();
 *     if (!resp.ok) throw new Error(data.detail || "xxx失败");
 * 这个顺序有问题 —— 出错响应往往不是 JSON（前端代理返回 HTML 错误页、
 * 502/504 网关页、uvicorn 崩溃时的空 body），此时 `resp.json()` 先抛
 * `SyntaxError: Unexpected token '<'`，真正的 HTTP 状态码被彻底吞掉，
 * 用户只看到一句莫名其妙的语法错误。
 *
 * 正确顺序：先把 body 取成文本 → 再按 ok / content-type 决定如何解释。
 */

/** 从错误响应体里尽力提取一句人类可读的原因。 */
function extractDetail(text, status, statusText) {
  const head = `HTTP ${status}${statusText ? " " + statusText : ""}`;
  if (!text) return head;
  try {
    const obj = JSON.parse(text);
    const d = obj && (obj.detail ?? obj.message ?? obj.error);
    if (typeof d === "string" && d.trim()) return d.trim();
    // FastAPI 的 422 是 detail: [{loc, msg, ...}]
    if (Array.isArray(d)) {
      const msgs = d.map((x) => (x && (x.msg || x.message)) || "").filter(Boolean);
      if (msgs.length) return `${head} 参数错误: ${msgs.join("; ")}`;
    }
  } catch {
    /* 不是 JSON，走下面的文本兜底 */
  }
  // HTML 错误页只取前若干字符，避免把整页塞进提示框
  const plain = text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  return plain ? `${head}: ${plain.slice(0, 200)}` : head;
}

/**
 * 发起请求并返回解析后的 JSON。
 * 非 2xx、或 2xx 但 body 不是合法 JSON，都会抛出带可读信息的 Error。
 *
 * @param {string} url
 * @param {RequestInit} [options]
 * @returns {Promise<any>}
 */
export async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  // 先读文本：无论成功失败都只消费一次 body，不会出现 "body already read"
  let text = "";
  try {
    text = await resp.text();
  } catch (e) {
    throw new Error(`HTTP ${resp.status} 响应体读取失败: ${e.message}`);
  }
  if (!resp.ok) {
    const err = new Error(extractDetail(text, resp.status, resp.statusText));
    err.status = resp.status;
    throw err;
  }
  if (!text.trim()) return null; // 204 / 空 body
  try {
    return JSON.parse(text);
  } catch {
    const plain = text.replace(/\s+/g, " ").trim().slice(0, 200);
    throw new Error(`服务返回了非 JSON 内容（HTTP ${resp.status}）: ${plain}`);
  }
}

/** POST JSON 的便捷包装。 */
export function postJson(url, body, options) {
  return fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    body: JSON.stringify(body ?? {}),
    ...options,
  });
}
