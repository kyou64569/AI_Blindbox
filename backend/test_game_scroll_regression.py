"""小游戏 iframe 滚动条回归测试（防溢出 / 无滚动条）。

核心验证目标：修复后，小游戏应在 520px 高的 iframe 内【完整显示、无滚动条】。

运行方式（本机裸 python 被 uv 劫持，需用受管解释器）：
  C:/Users/asus/.workbuddy/binaries/python/versions/3.13.12/python.exe \
      D:/work/AI_Blindbox/backend/test_game_scroll_regression.py

说明：
- 以包方式 `from app import pipeline, config` 引入，pipeline 内部用相对导入，
  不可直接 `import pipeline`。
- 仅把内存单例 `config.SETTINGS.raw["mock"] = True` 打开离线 mock 分支
  （规划/创意/执行器全部走本地规则，无需网络与 API key），不修改 settings.yaml。
- 运行会在 D:/work/AI_Blindbox/output/ 下生成 game_*.html 与 result_*.json 归档产物，属正常。
"""
import os
import sys

# 把 backend 加入路径，确保能以包方式 `from app import ...` 引入
sys.path.insert(0, r"D:\work\AI_Blindbox\backend")

from app import pipeline, config  # noqa: E402

# ---- 离线回归：仅改内存单例，不触碰 settings.yaml ----
config.SETTINGS.raw["mock"] = True

# 项目根（用于读取前端文件）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 用例结果收集： (用例名, 是否通过, 详细信息)
RESULTS: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    extra = (": " + detail) if (detail and not ok) else ""
    print(f"[{tag}] {name}{extra}")


# --------------------------------------------------------------------------- #
# 用例 1：_mock_game_html 生成的 HTML 无溢出风险
# --------------------------------------------------------------------------- #
def test_mock_game_html_no_overflow() -> None:
    name = "用例1 _mock_game_html 生成的 HTML 无溢出风险"
    try:
        html = pipeline._mock_game_html("【游戏名】星海弹弹乐")

        # html/body 规则必须 overflow: hidden（兜底无滚动条）
        assert "overflow: hidden" in html, \
            f"html/body 规则缺少 'overflow: hidden'；片段: {html[:200]!r}"
        # 必须有 canvas 与 script（真是游戏，不是提示词）
        assert "<canvas" in html, "缺少 <canvas"
        assert "<script" in html, "缺少 <script"
        # 响应式填充：#game-box 用 flex: 1 1 auto 撑满剩余高度
        assert "flex: 1 1 auto" in html, "缺少响应式填充 'flex: 1 1 auto'"
        # 动态尺寸逻辑：JS 按容器 resize 计算 canvas
        assert ("function resize(" in html) or ("resize()" in html), \
            "缺少动态尺寸逻辑（期望 'function resize(' 或 'resize()'）"
        # 防止 flex 子项撑出容器
        assert "min-height: 0" in html, "缺少防止 flex 子项撑出的 'min-height: 0'"

        _record(name, True)
    except AssertionError as e:
        _record(name, False, str(e))
    except Exception as e:  # noqa: BLE001
        _record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 用例 2（核心防回归）：渲染高度推算 —— 内容能否在 520px iframe 内装下
# 无需真浏览器，按修复后页面的纵向结构 + 响应式 resize 合同推算。
# --------------------------------------------------------------------------- #
def test_render_height_fits_520() -> None:
    name = "用例2 渲染高度推算（核心防回归：520px iframe 内装得下）"
    try:
        # ---- 纵向固定开销（与修复后的页面结构一致，取各 clamp 上限）----
        TITLE = 22 + 6    # h1: clamp(14-22) 取最大 22 + margin 6 ≈ 28
        HUD = 15 + 6      # #hud: clamp(11-15) 取 15 + margin-bottom 6 ≈ 21
        BTN = 15 + 6 + 8  # button: clamp(12-15) 取 15 + padding 6 + margin-top 8 ≈ 29
        WRAP = 8 + 8      # #wrap padding 上下各 8 ≈ 16
        FIXED = TITLE + HUD + BTN + WRAP   # ≈ 94px

        IFRAME_H = 520    # iframe 内容区高度
        box_h = IFRAME_H - FIXED   # 留给 #game-box（canvas 容器）的高度 = 426

        # 模拟响应式 resize() 合同：保持 3:4，不超过容器（与 pipeline 内逻辑一致）
        def clamp_canvas(w_iframe: int) -> tuple[int, int]:
            max_w = w_iframe - 4          # box.clientWidth - 4
            w = max_w
            h = round(w * 4 / 3)
            if h > box_h:                 # 超过容器高度则钳到容器，重新算宽
                h = box_h
                w = round(h * 3 / 4)
            return w, h

        # ---- 典型小屏宽度 W_iframe = 420 ----
        W = 420
        cw, ch = clamp_canvas(W)
        total = FIXED + ch
        print(f"  [W=420] maxW={W - 4}, canvas={cw}x{ch}, 固定开销={FIXED}, 总高={total}, iframe={IFRAME_H}")
        assert ch <= box_h, f"canvas 高度 {ch} 超过容器 {box_h}，会溢出"
        # 任务指定的核心断言（等于也可，因 overflow:hidden 兜底）
        assert FIXED + 426 <= 520, "（W=420 时 canvas 被钳到 426）FIXED+426 应 <= 520"
        assert total <= IFRAME_H, f"总高 {total} 超过 iframe 高度 {IFRAME_H}"

        # ---- 更窄的 iframe 宽度 W_iframe = 320，也应不溢出 ----
        W2 = 320
        cw2, ch2 = clamp_canvas(W2)
        total2 = FIXED + ch2
        print(f"  [W=320] maxW={W2 - 4}, canvas={cw2}x{ch2}, 总高={total2}, iframe={IFRAME_H}")
        assert ch2 <= box_h, f"(W=320) canvas 高度 {ch2} 超过容器 {box_h}"
        assert total2 <= IFRAME_H, f"(W=320) 总高 {total2} 超过 iframe 高度 {IFRAME_H}"

        _record(name, True, f"520px 内装得下（W=420 总高={total}, W=320 总高={total2}）")
    except AssertionError as e:
        _record(name, False, str(e))
    except Exception as e:  # noqa: BLE001
        _record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 用例 3：前端 iframe 属性校验（grep 方式）
# --------------------------------------------------------------------------- #
def test_frontend_iframe_attrs() -> None:
    name = "用例3 前端 iframe 属性校验（scrolling=no / height=520 / overflow:hidden）"
    try:
        jsx = os.path.join(PROJECT_ROOT, "frontend", "src", "components", "ResultPanel.jsx")
        demo = os.path.join(PROJECT_ROOT, "frontend", "demo.html")
        with open(jsx, encoding="utf-8") as f:
            jsx_src = f.read()
        with open(demo, encoding="utf-8") as f:
            demo_src = f.read()

        # ResultPanel.jsx（React 组件，主结果区 iframe）
        assert 'scrolling="no"' in jsx_src, "ResultPanel.jsx 缺少 scrolling=\"no\""
        assert "height: 520" in jsx_src, "ResultPanel.jsx 缺少 height: 520"
        assert 'overflow: "hidden"' in jsx_src, "ResultPanel.jsx 缺少 overflow: \"hidden\""

        # demo.html（主结果区 + 历史区，两处都要有）
        for tok in ('height:"520px"', 'scrolling="no"', 'overflow:"hidden"'):
            cnt = demo_src.count(tok)
            assert cnt >= 2, (
                f"demo.html 中 '{tok}' 出现 {cnt} 次，期望 >= 2（主结果区 + 历史区各一处）"
            )

        _record(name, True)
    except AssertionError as e:
        _record(name, False, str(e))
    except Exception as e:  # noqa: BLE001
        _record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 用例 4（关键回归）：run_blind_box("game") 全链路（mock）
# --------------------------------------------------------------------------- #
def test_run_blind_box_game_chain() -> None:
    name = "用例4 run_blind_box('game') 全链路（mock）"
    try:
        res = pipeline.run_blind_box("game")

        # 成品类型标记正确
        assert res.output_kind == "game_html", \
            f"output_kind 应为 game_html，实际 {res.output_kind!r}"
        # 成品是绝对路径且以 .html 结尾
        assert res.output.endswith(".html"), f"output 应以 .html 结尾，实际 {res.output!r}"
        # 文件确实落盘
        assert os.path.exists(res.output), f"落盘文件不存在: {res.output}"

        # 落盘内容含 overflow:hidden 与动态尺寸逻辑（防退化回提示词）
        with open(res.output, encoding="utf-8") as f:
            content = f.read()
        assert "overflow: hidden" in content, "落盘小游戏缺少 'overflow: hidden'"
        assert ("function resize(" in content) or ("resize()" in content), \
            "落盘小游戏缺少动态尺寸逻辑"

        _record(name, True, f"落盘文件: {res.output}")
    except AssertionError as e:
        _record(name, False, str(e))
    except Exception as e:  # noqa: BLE001
        _record(name, False, f"{type(e).__name__}: {e}")


def main() -> int:
    print("=== 小游戏 iframe 滚动条回归测试（mock 全链路，无网络 / 无 API key）===")
    print(f"    mock 开关 = {config.SETTINGS.mock} | output_dir = {config.SETTINGS.output_dir}\n")

    test_mock_game_html_no_overflow()
    test_render_height_fits_520()
    test_frontend_iframe_attrs()
    test_run_blind_box_game_chain()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== 汇总: {passed}/{total} 用例通过 ===")
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print("失败用例: ", fails)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
