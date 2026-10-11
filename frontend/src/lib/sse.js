/**
 * SSE 客户端：订阅 /api/task/{id}/stream，实时接收思考增量与阶段切换。
 *
 * 为什么还要保留轮询：
 *   - SSE 是「增强」而非替代。若代理掐断连接或事件流异常，轮询作为兜底保证不卡死。
 *   - SSE 失败时自动降级为纯轮询（onError 里不抛，用户无感）。
 *
 * 事件（与后端 stream_bus.py 一一对应）：
 *   stage           -> onStage(stage, label)
 *   reasoning_start -> 开始一段思考（stage/model）
 *   reasoning_delta -> 思考增量 text（逐字）
 *   reasoning_end   -> 一段思考结束
 *   done/error/cancelled -> 终态，带 result 或 error
 */

/** 创建 SSE 连接。返回 { close() }。不抛异常——连接失败由 onError 回调兜底。 */
export function subscribeTaskStream(taskId, handlers = {}, opts = {}) {
  if (typeof window === "undefined" || typeof window.EventSource === "undefined") return null;
  const { onStage, onReasonStart, onDelta, onReasonEnd, onDone, onError, onFallback } = handlers;
  // 无事件时的静默阈值：超过则通知上层降级（避免一直挂着没反应）
  const fallbackMs = opts.fallbackMs || 15000;

  let closed = false;
  let gotAny = false;
  let es;
  try {
    es = new window.EventSource(`/api/task/${encodeURIComponent(taskId)}/stream`);
  } catch (e) {
    onFallback?.(e);
    return null;
  }

  const parse = (ev) => {
    try {
      return JSON.parse(ev.data);
    } catch (e) {
      return null;
    }
  };

  es.onopen = () => {
    gotAny = true;
  };

  es.addEventListener("stage", (ev) => {
    const d = parse(ev);
    if (d) onStage?.(d.stage, d.label, d);
  });

  es.addEventListener("reasoning_start", (ev) => {
    const d = parse(ev);
    if (d) onReasonStart?.(d.stage, d.model, d);
  });

  es.addEventListener("reasoning_delta", (ev) => {
    const d = parse(ev);
    if (d && d.text) {
      gotAny = true;
      onDelta?.(d.text, d);
    }
  });

  es.addEventListener("reasoning_end", (ev) => {
    const d = parse(ev);
    onReasonEnd?.(d);
  });

  es.addEventListener("done", (ev) => {
    const d = parse(ev);
    closed = true;
    try {
      es.close();
    } catch (e) {
      /* ignore */
    }
    onDone?.(d?.result, d);
  });

  es.addEventListener("error", (ev) => {
    // 终态 error/cancelled 也走这里（服务端用 event 名区分）
    if (ev && typeof ev.data === "string" && ev.data && ev.data !== "{}") {
      const d = parse(ev);
      if (d && (d.type === "error" || d.type === "cancelled" || d.type === "cancelling")) {
        closed = true;
        try {
          es.close();
        } catch (e) {
          /* ignore */
        }
        onError?.(d.error || "开盒失败", d);
        return;
      }
    }
    if (closed) return;
    // 连接层错误：通知上层降级到轮询
    onError?.("SSE 连接中断", null);
    onFallback?.(ev);
    try {
      es.close();
    } catch (e) {
      /* ignore */
    }
  });

  es.addEventListener("end", () => {
    closed = true;
    try {
      es.close();
    } catch (e) {
      /* ignore */
    }
  });

  // 静默兜底：连接上了但一直没数据（如后端不支持 SSE 老版本），通知上层走轮询
  const timer = setTimeout(() => {
    if (!gotAny && !closed) onFallback?.(new Error("SSE 无响应"));
  }, fallbackMs);

  return {
    close() {
      closed = true;
      clearTimeout(timer);
      try {
        es?.close();
      } catch (e) {
        /* ignore */
      }
    },
  };
}