"""pipeline.py 核心纯函数的单元测试。全部本地执行，不真实调用任何 API。

运行：
  <解释器> -m pytest backend/tests/test_pipeline_pure.py -q

覆盖（均为无外部依赖的纯函数）：
  - _slugify             文件名安全化
  - _core_image          模板前缀剥离
  - _seems_anchored      锚定检测（2 字滑窗）
  - _clean_seed_text     种子清洗（维度标签/编号/引号/标点）
  - _extract_block       结构化 [TAG] 块提取
  - _extract_shots       分镜块提取
  - _check_blocked_env   重型环境黑名单扫描（含约束章节剔除）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import pipeline  # noqa: E402


# ---------------- _slugify ----------------

class TestSlugify:
    def test_empty_returns_untitled(self):
        assert pipeline._slugify("") == "untitled"
        assert pipeline._slugify(None) == "untitled"

    def test_keeps_chinese_and_alnum(self):
        assert pipeline._slugify("一只橘猫") == "一只橘猫"
        assert pipeline._slugify("hello world 123") == "hello_world_123"

    def test_strips_windows_illegal_chars(self):
        # < > : " / \ | ? * 全部去掉
        assert "<>" not in pipeline._slugify('a<>b:"/\\|?*c')
        assert pipeline._slugify('a<>b') == "ab"

    def test_whitespace_collapsed(self):
        assert pipeline._slugify("a   b\t c") == "a_b_c"

    def test_trims_dots_and_spaces(self):
        assert pipeline._slugify("  .标题.  ") == "标题"
        assert not pipeline._slugify("..").endswith(".")

    def test_max_len_truncate(self):
        out = pipeline._slugify("一二三四五六七八九十", max_len=6)
        assert len(out) <= 6
        # 截断后不应以 _ . 结尾
        assert not out.endswith("_") and not out.endswith(".")

    def test_all_illegal_becomes_untitled(self):
        assert pipeline._slugify(":::") == "untitled"


# ---------------- _core_image ----------------

class TestCoreImage:
    def test_plain_text_unchanged(self):
        assert pipeline._core_image("机械敦煌") == "机械敦煌"

    def test_strips_template_prefixes(self):
        assert pipeline._core_image("必须出现一只橘猫") == "橘猫"
        assert pipeline._core_image("画面中央有一扇门") == "门"

    def test_loop_strip_multi_prefix(self):
        # 先剥「必须出现」得「一只白鹭」，再剥「一只」得「白鹭」
        assert pipeline._core_image("必须出现一只白鹭") == "白鹭"

    def test_no_prefix_no_change(self):
        assert pipeline._core_image("霓虹苗寨") == "霓虹苗寨"


# ---------------- _seems_anchored ----------------

class TestSeemsAnchored:
    def test_exact_match_is_anchored(self):
        assert pipeline._seems_anchored("橘猫", ["橘猫"]) is True

    def test_shared_two_char_ngram_is_anchored(self):
        # 「机械橘猫」与「橘猫」共享 2 字片段「橘猫」
        assert pipeline._seems_anchored("一只机械橘猫", ["橘猫"]) is True

    def test_template_prefix_not_false_positive(self):
        # 已有条目是「必须出现一只白鹭」，核心意象是「白鹭」；
        # 新种子说「栖息的候鸟」不共享「白鹭」的 2 字片段 → 不算锚定
        assert pipeline._seems_anchored("栖息的候鸟", ["必须出现一只白鹭"]) is False

    def test_unrelated_not_anchored(self):
        assert pipeline._seems_anchored("深海图书馆", ["霓虹苗寨"]) is False

    def test_empty_safe(self):
        assert pipeline._seems_anchored("", []) is False
        assert pipeline._seems_anchored("x", ["", None]) is False
        assert pipeline._seems_anchored("x", [123, None, ""]) is False

    def test_one_char_core_no_crash(self):
        # 核心意象只有一个字时滑窗不产生 2 字片段，不应崩溃也不应误判
        assert pipeline._seems_anchored("abc", ["门"]) is False


# ---------------- _clean_seed_text ----------------

class TestCleanSeedText:
    def test_strip_dimension_label(self):
        assert pipeline._clean_seed_text("【主题】量子禅院") == "量子禅院"
        assert pipeline._clean_seed_text("[风格] 故障艺术") == "故障艺术"

    def test_strip_numbering(self):
        assert pipeline._clean_seed_text("1. 量子禅院") == "量子禅院"
        assert pipeline._clean_seed_text("2、【主题】机械敦煌") == "机械敦煌"

    def test_strip_quotes_and_trailing_punct(self):
        assert pipeline._clean_seed_text('"深海图书馆"。') == "深海图书馆"
        assert pipeline._clean_seed_text("「废土茶道」") == "废土茶道"

    def test_first_line_only(self):
        assert pipeline._clean_seed_text("量子禅院\n解释文字") == "量子禅院"

    def test_empty(self):
        assert pipeline._clean_seed_text("") == ""
        assert pipeline._clean_seed_text("  ") == ""


# ---------------- _extract_block ----------------

class TestExtractBlock:
    def test_simple_block(self):
        text = "[IMAGE] a cat\n[CAPTION] 一只猫"
        assert pipeline._extract_block(text, "IMAGE") == "a cat"

    def test_case_and_bracket_tolerant(self):
        text = "[caption] 一只猫\n[image] a dog"
        assert pipeline._extract_block(text, "CAPTION") == "一只猫"

    def test_missing_returns_empty(self):
        assert pipeline._extract_block("no tags here", "IMAGE") == ""
        assert pipeline._extract_block("", "IMAGE") == ""

    def test_last_block_to_end(self):
        text = "[IMAGE] a\n[CAPTION] 结尾没有下一个标签"
        assert pipeline._extract_block(text, "CAPTION") == "结尾没有下一个标签"

    def test_content_with_newline_kept(self):
        text = "[IMAGE] line1\nline2\n[CAPTION] x"
        assert "line2" in pipeline._extract_block(text, "IMAGE")


# ---------------- _extract_shots ----------------

class TestExtractShots:
    def test_extract_shot_blocks(self):
        plan = "STORYBOARD:\n[SHOT 1] establish the greenhouse\n[SHOT 2] the cat reveals the seed"
        shots = pipeline._extract_shots(plan)
        assert len(shots) == 2
        assert "greenhouse" in shots[0]
        assert "reveals" in shots[1]

    def test_shot_tag_variants(self):
        # 兼容 [SHOT_1] / [SHOT1] / 带冒号
        plan = "[SHOT_1]: a\n[SHOT2] b\n[SHOT 3] c"
        shots = pipeline._extract_shots(plan)
        assert shots == ["a", "b", "c"]

    def test_fallback_whole_plan(self):
        plan = "没有任何分镜标记的普通文本"
        shots = pipeline._extract_shots(plan)
        assert len(shots) == 1
        assert shots[0] == plan

    def test_empty(self):
        assert pipeline._extract_shots("") == []


# ---------------- _check_blocked_env ----------------

class TestCheckBlockedEnv:
    def test_hit_returns_keywords_and_warns(self):
        hits, warns = pipeline._check_blocked_env("技术选型：Python + Unity 引擎")
        assert "unity" in [h.lower() for h in hits]
        assert len(warns) == 2

    def test_clean_returns_empty(self):
        hits, warns = pipeline._check_blocked_env("技术选型：Python + FastAPI + 纯前端 HTML")
        assert hits == []
        assert warns == []

    def test_constraint_section_not_false_positive(self):
        # 约束章节复述禁令（如「不要用 Unity/MySQL」）不应被误判为真实选型
        spec = (
            "【技术栈】Python + FastAPI\n\n"
            "【技术栈环境约束】禁止使用 Unity、Unreal、MySQL 等重型环境，"
            "只允许轻量实现。"
        )
        hits, warns = pipeline._check_blocked_env(spec)
        assert hits == []

    def test_case_insensitive(self):
        hits, _ = pipeline._check_blocked_env("技术选型：DOCKER 容器")
        assert "docker" in [h.lower() for h in hits]

    def test_custom_blocked_env_from_mode(self):
        hits, _ = pipeline._check_blocked_env("技术选型：ansible", {"blocked_env": ["ansible"]})
        assert "ansible" in hits

    def test_empty_text(self):
        assert pipeline._check_blocked_env("") == ([], [])
