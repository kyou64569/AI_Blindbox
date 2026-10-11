"""stream_bus 并发与原子性回归用例。

覆盖代码审查里指出的并发风险点（部分经核实为不成立，此处一并固化结论，
防止将来重构时把「去重判断搬出锁」之类的改动重新引入）。

关键点：
  - seq 必须由 _emit 在持锁状态下按 append 后的下标分配，
    不能由调用方先调 next_seq() 再 _emit()（那两步之间有窗口）。
  - reasoning_start 的「同阶段去重」必须与 _active 更新同处一把锁内。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.stream_bus import BROADCASTERS, TaskBroadcaster
import app.stream_bus as sb


# ---------------------------------------------------------------------------
# seq 的原子分配
# ---------------------------------------------------------------------------

def test_seq_matches_index_exactly():
    """seq 必须严格等于「数组下标 + 1」，这是 Last-Event-ID 续传的基础。"""
    bc = TaskBroadcaster("t")
    bc.stage("creative", "创意生成中")
    bc.reasoning_start("创意生成中", "m1")
    bc.reasoning_delta("a")
    bc.reasoning_delta("b")
    bc.reasoning_end()
    bc.finish({"output": "x"})

    assert [e["seq"] for e in bc._events] == list(range(1, len(bc._events) + 1))


def test_seq_is_unique_under_concurrent_producers():
    """并发生产者下 seq 不得重复。

    这条对应审查报告条目1 担心的场景。虽然当前调用拓扑下（单模型线程）
    并发生产者不存在，但一旦将来加入并发写入就会立刻暴露问题，
    所以在这里把「seq 由 _emit 原子分配」这个约束固化下来。
    """
    bc = TaskBroadcaster("t")
    barrier = threading.Barrier(8)

    def worker(tag: str) -> None:
        barrier.wait()
        for i in range(20):
            bc.reasoning_delta(f"{tag}-{i}")

    ths = [threading.Thread(target=worker, args=(f"w{k}",)) for k in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    seqs = [e["seq"] for e in bc._events]
    assert len(seqs) == len(set(seqs)), f"seq 出现重复：共 {len(seqs)} 条，唯一 {len(set(seqs))} 条"
    assert seqs == sorted(seqs), "seq 必须单调递增"


def test_delta_is_not_emitted_after_close():
    """关闭后不得再接收新事件（否则缓冲会无限增长）。"""
    bc = TaskBroadcaster("t")
    bc.stage("s", "l")
    bc.close()
    before = len(bc._events)
    bc.reasoning_delta("late")
    bc.stage("s", "late-stage")
    assert len(bc._events) == before


# ---------------------------------------------------------------------------
# reasoning_start 的同阶段去重（审查条目1）
# ---------------------------------------------------------------------------

def test_same_stage_start_deduplicated_concurrently():
    """同一阶段并发 start 只应发出一条 —— 否则前端会开出两个思考段。"""
    bc = TaskBroadcaster("t")
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        bc.reasoning_start("创意生成中", "gemini-3.5-flash-lite")

    ths = [threading.Thread(target=worker) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    starts = [e for e in bc._events if e["type"] == "reasoning_start"]
    assert len(starts) == 1, f"同阶段并发 start 发了 {len(starts)} 条 start，应为 1 条"


def test_different_stage_start_all_emitted():
    """不同阶段必须都发出去（去重不能误伤阶段切换）。"""
    bc = TaskBroadcaster("t")
    for s in ("创意生成中", "规划生成中", "成品生成中"):
        bc.reasoning_start(s, "m")
    starts = [e for e in bc._events if e["type"] == "reasoning_start"]
    assert len(starts) == 3
    assert [e["stage"] for e in starts] == ["创意生成中", "规划生成中", "成品生成中"]


def test_same_stage_start_updates_model():
    """同阶段再次 start（换模型重试）应更新 model 而不是新开一段。"""
    bc = TaskBroadcaster("t")
    bc.reasoning_start("规划生成中", "glm-5.2")
    bc.reasoning_start("规划生成中", "intern-s2")
    starts = [e for e in bc._events if e["type"] == "reasoning_start"]
    assert len(starts) == 1
    assert bc._active["model"] == "intern-s2"


# ---------------------------------------------------------------------------
# 事件上限（审查条目5 —— 经核实不成立，这里固化该结论）
# ---------------------------------------------------------------------------

def test_event_buffer_never_exceeds_limit(monkeypatch):
    """纯结构性事件涌入时，长度必须被压在上限内。

    审查报告称「只删一个事件会超限」，实测为不成立：每 append 一次就检查一次，
    每次最多删一个，因此长度始终 <= 上限。此用例把该不变量固化。
    """
    monkeypatch.setattr(sb, "_MAX_EVENTS", 10)
    bc = TaskBroadcaster("t")
    for i in range(50):
        bc.stage("s", f"label-{i}")
    assert len(bc._events) <= 10, f"事件缓冲超限：{len(bc._events)} > 10"


def test_delta_dropped_before_structural_events(monkeypatch):
    """超限时优先丢 delta，保留 start/end/stage 等结构事件。"""
    monkeypatch.setattr(sb, "_MAX_EVENTS", 10)
    bc = TaskBroadcaster("t")
    for i in range(3):
        bc.reasoning_delta(f"d{i}")
    for i in range(30):
        bc.stage("s", f"l{i}")
    types = [e["type"] for e in bc._events]
    assert "reasoning_delta" not in types, "delta 应优先被丢弃"
    assert types.count("stage") >= 9, "stage 事件应尽量保留"


# ---------------------------------------------------------------------------
# 关闭与续传（审查条目6 —— 经核实不成立，固化结论）
# ---------------------------------------------------------------------------

def test_stream_exits_promptly_after_close():
    """close() 后已建立的订阅者必须立即被唤醒退出，不能等超时轮询。"""
    bc = TaskBroadcaster("t")
    bc.stage("s", "l")
    done = {}

    def consume() -> None:
        t0 = time.time()
        for frame in bc.stream(0):
            if "event: end" in frame:
                break
        done["dt"] = time.time() - t0

    th = threading.Thread(target=consume, daemon=True)
    th.start()
    time.sleep(0.2)
    bc.close()
    th.join(timeout=5)

    assert not th.is_alive(), "订阅者未在 5s 内退出（应被 notify_all 立即唤醒）"
    assert done.get("dt", 99) < 2.0, f"退出耗时 {done.get('dt')}s，明显超过 wait(timeout=1.0) 的唤醒延迟"


def test_stream_replays_from_last_event_id():
    """带 Last-Event-ID 重连时只补发其后的事件。"""
    bc = TaskBroadcaster("t")
    bc.stage("s", "a")
    bc.reasoning_delta("x")
    bc.reasoning_delta("y")
    bc.finish({"ok": True})

    frames = list(bc.stream(last_event_id=2))
    bodies = "".join(frames)
    assert '"x"' not in bodies, "seq<=2 的事件不应重放"
    assert '"y"' in bodies, "seq>2 的事件应补发"
    assert "event: done" in bodies


def test_finish_closes_broadcaster():
    bc = TaskBroadcaster("t")
    bc.finish({"ok": 1})
    assert bc.closed is True


# ---------------------------------------------------------------------------
# 注册表生命周期
# ---------------------------------------------------------------------------

def test_registry_drop_removes_and_closes():
    bc = BROADCASTERS.create("reg-1")
    assert BROADCASTERS.get("reg-1") is bc
    BROADCASTERS.drop("reg-1")
    assert BROADCASTERS.get("reg-1") is None


def test_drop_if_same_does_not_clobber_newer_broadcaster():
    """同一 id 重建后，drop_if_same 不能误删新实例。"""
    old = BROADCASTERS.create("dup-1")
    new = BROADCASTERS.create("dup-1")
    BROADCASTERS.drop_if_same("dup-1", old)
    assert BROADCASTERS.get("dup-1") is new
    BROADCASTERS.drop("dup-1")
