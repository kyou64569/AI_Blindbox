"""小游戏（game）模式回归测试。

核心验证目标：开启小游戏盲盒时，成品必须是真正落盘的 HTML 游戏文件，
绝不能退化回「只输出规划提示词」。

运行方式（本机裸 python 可能被环境管理器劫持，建议使用受管解释器）：
  <解释器> backend/test_game_regression.py

说明：
- 测试以包方式 `from app import pipeline, config` 引入，pipeline 内部用
  相对导入 `from .config import ...`，不可直接 `import pipeline`。
- 临时把内存单例 `config.SETTINGS.raw["mock"] = True` 打开离线 mock 分支
  （规划/创意/执行器全部走本地规则，无需网络与 API key），不修改 settings.yaml。
- 运行会在 output/ 下生成 game_*.html 与 result_*.json 归档产物，属正常。
"""
import os
import sys

# 把 backend 加入路径，确保能以包方式 `from app import ...` 引入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import pipeline, config  # noqa: E402

# ---- 离线回归：仅改内存单例，不触碰 settings.yaml ----
config.SETTINGS.raw["mock"] = True

# 用例结果收集： (用例名, 是否通过, 详细信息)
RESULTS: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    extra = (": " + detail) if (detail and not ok) else ""
    print(f"[{tag}] {name}{extra}")


# --------------------------------------------------------------------------- #
# 用例 1：_extract_html 围栏剥离
# --------------------------------------------------------------------------- #
def test_extract_html() -> None:
    name = "用例1 _extract_html 围栏剥离"
    try:
        # 1a) ```html ... ``` 包裹 -> 去掉围栏，含 <!DOCTYPE 或 <html
        c = "```html\n<!DOCTYPE html>\n<html><body>x</body></html>\n```"
        out = pipeline._extract_html(c)
        assert ("<!DOCTYPE" in out or "<html" in out), \
            f"1a 剥离后应含 <!DOCTYPE/<html>，实际: {out!r}"
        assert "```" not in out, f"1a 不应残留围栏，实际: {out!r}"

        # 1b) ``` ... ``` 无 html 标识 -> 同样剥离返回纯 HTML
        c = "```\n<html><body>hi</body></html>\n```"
        out = pipeline._extract_html(c)
        assert "<html" in out, f"1b 无 html 标识围栏也应剥离，实际: {out!r}"
        assert "```" not in out, f"1b 不应残留围栏，实际: {out!r}"

        # 1c) 纯 HTML（无围栏）-> 原样返回
        c = "<!DOCTYPE html><html><body></body></html>"
        out = pipeline._extract_html(c)
        assert out == c, f"1c 纯 HTML 应原样返回，实际: {out!r}"

        # 1d) 纯文本「【游戏名】xxx」（不含 html 标签）-> 返回空串 ""
        c = "【游戏名】xxx"
        out = pipeline._extract_html(c)
        assert out == "", f"1d 非 HTML 文本应返回空串，实际: {out!r}"

        _record(name, True)
    except AssertionError as e:
        _record(name, False, str(e))
    except Exception as e:  # noqa: BLE001
        _record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 用例 2：_mock_game_html 产出有效的单文件小游戏
# --------------------------------------------------------------------------- #
def test_mock_game_html() -> None:
    name = "用例2 _mock_game_html 产出有效小游戏"
    try:
        html = pipeline._mock_game_html("")
        for tok in ("<!DOCTYPE html", "</html>", "<canvas", "<script", "</script>"):
            assert tok in html, f"内置游戏缺少必要片段 {tok}"

        # 传入含「【游戏名】星海弹弹乐」的 plan -> 标题应含该名
        plan = "（无关前缀文本）\n【游戏名】星海弹弹乐\n（无关后缀文本）"
        html2 = pipeline._mock_game_html(plan)
        assert "星海弹弹乐" in html2, f"标题未含『星海弹弹乐』，片段: {html2[:300]!r}"
        in_title = ("<title>星海弹弹乐</title>" in html2) or ("<h1>星海弹弹乐</h1>" in html2)
        assert in_title, f"『星海弹弹乐』未出现在 <title> 或 <h1> 中"

        _record(name, True)
    except AssertionError as e:
        _record(name, False, str(e))
    except Exception as e:  # noqa: BLE001
        _record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 用例 3（关键回归）：mock 全链路 run_blind_box("game")
# --------------------------------------------------------------------------- #
def test_run_blind_box_game() -> None:
    name = "用例3 mock 全链路 run_blind_box('game') 落盘真实 HTML"
    try:
        res = pipeline.run_blind_box("game")

        # 成品类型标记正确
        assert res.output_kind == "game_html", \
            f"output_kind 应为 game_html，实际 {res.output_kind!r}"

        # 成品是绝对路径且以 .html 结尾
        assert isinstance(res.output, str), f"output 应为字符串，实际 {type(res.output)}"
        assert res.output.endswith(".html"), f"output 应以 .html 结尾，实际 {res.output!r}"
        assert os.path.isabs(res.output), f"output 应为绝对路径，实际 {res.output!r}"

        # 文件确实落盘
        assert os.path.exists(res.output), f"落盘文件不存在: {res.output}"

        # 落盘内容含 <canvas 与 <script —— 是真游戏代码，不是提示词
        with open(res.output, encoding="utf-8") as f:
            content = f.read()
        assert "<canvas" in content and "<script" in content, \
            f"落盘文件非真实游戏代码（缺 <canvas/<script）: {res.output}"

        # 核心防回归：成品绝不是规划 Agent 的需求提示词本身
        assert res.output != res.plan, \
            f"防回归失败：成品等于规划提示词 res.plan={res.plan!r}"
        # 成品也不应以规划文本「【游戏名】」开头
        assert not res.output.startswith("【游戏名】"), \
            f"成品不应以规划文本『【游戏名】』开头: {res.output!r}"

        # 归档流程把绝对路径转成相对 local_file，前端 iframe 才能预览
        assert res.archived is not None, "archived 不应为 None"
        assert res.archived.get("local_file"), \
            f"archived.local_file 应为非空（前端预览依赖），实际: {res.archived}"

        _record(name, True, f"落盘文件: {res.output}")
    except AssertionError as e:
        _record(name, False, str(e))
    except Exception as e:  # noqa: BLE001
        _record(name, False, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# 用例 4（仅说明，不真跑旧代码）：回归依据（防退化断言的来源）
# --------------------------------------------------------------------------- #
def print_regression_note() -> None:
    print("\n[回归依据 / 用例4 说明]")
    print("  若该 game 分支缺失，则 _run_executor 会走到函数末尾 `return plan`：")
    print("    - res.output 会等于 res.plan（即规划 Agent 的需求提示词文本）；")
    print("    - res.output 不会以 .html 结尾，也不是真实落盘文件；")
    print("    - archived.local_file 为空（因为 output 不是文件绝对路径）。")
    print("  本测试『用例3』中的 `res.output != res.plan`、`endswith('.html')`、")
    print("  `os.path.exists(res.output)`、`archived.local_file 非空` 正是用来捕获这种退化。")


def main() -> int:
    print("=== 小游戏模式回归测试（mock 全链路，无网络 / 无 API key）===")
    print(f"    mock 开关 = {config.SETTINGS.mock} | output_dir = {config.SETTINGS.output_dir}\n")

    test_extract_html()
    test_mock_game_html()
    test_run_blind_box_game()
    print_regression_note()

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
