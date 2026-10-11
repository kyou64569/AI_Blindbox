"""SSE 事件广播：把开盒过程中的实时事件推给前端。

设计目标：让「思考内容」真正流式到达，而不是等模型整段返回后前端自己演 typewriter。

为什么用 broadcaster 而不是直接改 _TASKS：
  - _TASKS 是「状态快照」，前端 1.2s 轮询读它；delta 是高频增量，混进去会撑爆任务表
  - delta 只需在任务存活期间保留，任务结束即丢弃，不进历史归档
  - 每个订阅者（浏览器标签）维护自己的游标，从断点续传，不互相干扰

事件格式（SSE data 行的 JSON）：
  {"type": "reasoning_start", "seq": 1, "stage": "创意生成中", "model": "gemini-3-pro"}
  {"type": "reasoning_delta", "seq": 2, "text": "先分析种子"}
  {"type": "reasoning_end",   "seq": 3}
  {"type": "stage", "seq": 4, "stage": "planner", "label": "规划生成中"}
  {"type": "done", "seq": 5, "result": {...}}   # 或 {"type":"error", ...} / {"type":"cancelled"}

seq 单调递增，订阅者用 Last-Event-ID 续传；慢消费者丢帧不影响正确性
（reasoning_start/end 让前端能自愈重建，delta 缺失时最终文本仍由 result.reasoning 校正）。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Iterator

# 单任务最多缓存的事件数；超出丢最旧的（防超长思考把内存打满）
_MAX_EVENTS = 4000
# 单个 delta 事件最大字符数（超长片段切分，避免单条事件过大）
_MAX_DELTA_CHARS = 400


class TaskBroadcaster:
    """单个开盒任务的事件广播器。线程安全，可被多个订阅者共享。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._closed = False
        # 每个订阅者的等待信号：new 一个就唤醒所有订阅者重新读
        self._cond = threading.Condition(self._lock)
        # 当前正在流式输出的思考段（用于 reasoning_delta 自动归组）
        self._active: dict | None = None

    # ---------------- 生产端（后台线程调用） ----------------

    def _emit(self, ev: dict) -> None:
        """把事件追加进缓冲并唤醒订阅者。

        传入的 ev 若没有 seq，会在这里原子地补上（基于 append 后的下标）。
        seq 是 SSE 的 Last-Event-ID 断点游标，必须与数组下标严格对应，
        所以「分配 seq + append」必须在同一把锁内完成，不能分两步。
        """
        with self._cond:
            if self._closed:
                return
            if not ev.get("seq"):
                ev["seq"] = len(self._events) + 1
            self._events.append(ev)
            if len(self._events) > _MAX_EVENTS:
                # 丢最旧的 delta（保留 start/end/stage 等结构性事件优先）
                for i, e in enumerate(self._events):
                    if e.get("type") == "reasoning_delta":
                        self._events.pop(i)
                        break
                else:
                    self._events.pop(0)
            self._cond.notify_all()

    def next_seq(self) -> int:
        """下一个将分配的 seq。仅供诊断/测试用；生产路径请让 _emit 自行分配。"""
        with self._lock:
            return len(self._events) + 1

    def reasoning_start(self, stage: str, model: str = "") -> None:
        """开始一段新的思考流。重复调用同一段时只更新 model，不重复开段。"""
        # 去重判断与 _active 更新在同一把锁内完成：若放到 _emit 之后，
        # 并发调用可能都通过检查而各发一条 start，前端就会开出两个思考段。
        with self._cond:
            if self._active and self._active.get("stage") == stage:
                self._active["model"] = model or self._active.get("model", "")
                return
            self._active = {"stage": stage, "model": model}
            if self._closed:
                return
            ev = {
                "type": "reasoning_start",
                "seq": len(self._events) + 1,
                "stage": stage,
                "model": model,
            }
            self._events.append(ev)
            self._cond.notify_all()

    def reasoning_delta(self, text: str) -> None:
        """推送思考增量。长文本切片，保证单事件不过大。"""
        if not text:
            return
        # 超长片段切片下发，避免单条事件过大撑爆 SSE 帧
        for i in range(0, len(text), _MAX_DELTA_CHARS):
            self._emit({"type": "reasoning_delta", "text": text[i : i + _MAX_DELTA_CHARS]})

    def reasoning_end(self) -> None:
        """结束当前思考流。"""
        with self._cond:
            if not self._active:
                return
            self._active = None
        self._emit({"type": "reasoning_end"})

    def stage(self, stage: str, label: str) -> None:
        """阶段切换：先隐式结束进行中的思考流，避免两段思考混在一起。"""
        self.reasoning_end()
        self._emit({"type": "stage", "stage": stage, "label": label})

    def finish(self, payload: dict) -> None:
        """任务结束：发终态事件并关闭广播（前端据此关闭 EventSource）。"""
        self.reasoning_end()
        self._emit({"type": "done", "result": payload, "status": "done"})
        self.close()

    def fail(self, error: str, status: str = "error") -> None:
        """任务失败/取消：发终态事件并关闭。"""
        self.reasoning_end()
        self._emit({"type": status, "error": error, "status": status})
        self.close()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    # ---------------- 消费端（SSE 生成器调用） ----------------

    def stream(self, last_event_id: int = 0) -> "Iterator[str]":
        """产出 SSE 帧字符串。last_event_id 为已收到的最大 seq（用于断线续传）。

        阻塞点：没有新事件时在条件变量上等，收到事件或广播关闭即唤醒。
        心跳由调用方用超时控制（本函数不自己 sleep，避免关闭后仍阻塞）。
        """
        cursor = max(0, last_event_id)
        # 先做一次立即 yield，让 HTTP 响应头尽早 flush（否则代理可能缓冲首包）
        yield ": connected\n\n"
        while True:
            with self._cond:
                if cursor < len(self._events):
                    ev = self._events[cursor]
                    cursor += 1
                elif self._closed:
                    # 关闭且已发完 -> 收尾
                    yield "event: end\ndata: {}\n\n"
                    return
                else:
                    # 等新事件（超时由外层 generator 控制）
                    self._cond.wait(timeout=1.0)
                    if self._closed and cursor >= len(self._events):
                        yield "event: end\ndata: {}\n\n"
                        return
                    continue
            frame = _to_sse(ev)
            if frame:
                yield frame


def _to_sse(ev: dict) -> str:
    """把事件 dict 序列化为 SSE 帧；非 delta 类事件带 event 名便于前端 addEventListener。"""
    try:
        data = json.dumps(ev, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — 序列化失败不能弄崩整个流
        return ""
    name = ev.get("type") or "message"
    # id 用 seq，浏览器断线重连时会自动带 Last-Event-ID
    return f"id: {ev.get('seq', 0)}\nevent: {name}\ndata: {data}\n\n"


class BroadcasterRegistry:
    """task_id -> TaskBroadcaster 的全局注册表。任务结束后条目即删除。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._map: dict[str, TaskBroadcaster] = {}

    def create(self, task_id: str) -> TaskBroadcaster:
        with self._lock:
            bc = TaskBroadcaster(task_id)
            self._map[task_id] = bc
            return bc

    def get(self, task_id: str) -> "TaskBroadcaster | None":
        with self._lock:
            return self._map.get(task_id)

    def drop(self, task_id: str) -> None:
        with self._lock:
            bc = self._map.pop(task_id, None)
        if bc is not None:
            bc.close()

    def drop_if_same(self, task_id: str, bc: TaskBroadcaster) -> None:
        """只在注册表里还是同一个实例时才删——防止误删同id 的新广播器。"""
        with self._lock:
            cur = self._map.get(task_id)
            if cur is bc:
                self._map.pop(task_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)


# 进程级单例
BROADCASTERS = BroadcasterRegistry()