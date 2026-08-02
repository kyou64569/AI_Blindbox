"""开盒结果「由哪个模型完成」提示 (model_used) 的回归测试，全部 mock，不真实调用 API。

运行：
  C:/Users/asus/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest ^
      D:/work/AI_Blindbox/backend/tests/ -q

覆盖：
  - text / image / video / game / tts 在 mock 模式下 model_used == "Mock 模拟"
  - code-spec 模式 model_used is None（无模型调用，不应展示提示）
  - archive_result 落盘的 result_*.json 含 model_used 字段（贯通 API 返回链路）
"""
import json
import os
import sys

# 把 backend 加入路径，确保 `from app import ...` 可导入（pipeline 内部用相对导入）
sys.path.insert(0, r"D:/work/AI_Blindbox/backend")

from app import pipeline, config


def _set_mock(raw, monkeypatch, tmp_path):
    """临时把内存单例置为 mock 模式，并把产物目录重定向到临时目录，测试后还原。"""
    saved = json.loads(json.dumps(raw))  # 深拷贝，供 finally 还原
    raw["mock"] = True
    monkeypatch.setattr(config.SETTINGS, "output_dir", tmp_path)
    return saved


def _new_result(output_kind: str) -> pipeline.BlindBoxResult:
    return pipeline.BlindBoxResult(
        seed="test-seed",
        concept="c",
        plan="p",
        output="",
        output_kind=output_kind,
    )


# ---------------------------------------------------------------------------
# 用例 1：text 模式（mock）→ model_used == "Mock 模拟"
# ---------------------------------------------------------------------------
def test_mock_text_model_used(tmp_path, monkeypatch):
    raw = config.SETTINGS.raw
    saved = _set_mock(raw, monkeypatch, tmp_path)
    try:
        result = _new_result("text")
        pipeline._run_executor("text", "p", {}, result)
        assert result.model_used == "Mock 模拟"
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 2：image 模式（mock）→ model_used == "Mock 模拟"
# ---------------------------------------------------------------------------
def test_mock_image_model_used(tmp_path, monkeypatch):
    raw = config.SETTINGS.raw
    saved = _set_mock(raw, monkeypatch, tmp_path)
    try:
        result = _new_result("image")
        pipeline._run_executor("image", "p", {}, result)
        assert result.model_used == "Mock 模拟"
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 3：video 模式（mock）→ model_used == "Mock 模拟"
# ---------------------------------------------------------------------------
def test_mock_video_model_used(tmp_path, monkeypatch):
    raw = config.SETTINGS.raw
    saved = _set_mock(raw, monkeypatch, tmp_path)
    try:
        result = _new_result("video")
        pipeline._run_executor("video", "p", {}, result)
        assert result.model_used == "Mock 模拟"
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 4：game 模式（mock）→ model_used == "Mock 模拟"
# ---------------------------------------------------------------------------
def test_mock_game_model_used(tmp_path, monkeypatch):
    raw = config.SETTINGS.raw
    saved = _set_mock(raw, monkeypatch, tmp_path)
    try:
        result = _new_result("game_html")
        pipeline._run_executor("game", "p", {}, result)
        assert result.model_used == "Mock 模拟"
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 5：tts 模式（mock）→ model_used == "Mock 模拟"，且产物为 wav
# ---------------------------------------------------------------------------
def test_mock_tts_model_used(tmp_path, monkeypatch):
    raw = config.SETTINGS.raw
    saved = _set_mock(raw, monkeypatch, tmp_path)
    try:
        result = _new_result("audio_file")
        path, info = pipeline._run_tts("你好世界，这是播客台词。", {}, result)
        assert result.model_used == "Mock 模拟"
        assert path.endswith(".wav"), f"mock 应返回本地占位音频路径，实际: {path}"
        assert os.path.exists(path), f"占位音频文件未生成: {path}"
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 6：code-spec 模式无模型调用 → model_used is None（不展示提示）
# ---------------------------------------------------------------------------
def test_code_spec_model_used_none(tmp_path, monkeypatch):
    raw = config.SETTINGS.raw
    saved = json.loads(json.dumps(raw))
    monkeypatch.setattr(config.SETTINGS, "output_dir", tmp_path)
    try:
        result = _new_result("spec_md")
        out = pipeline._run_executor("code-spec", "p", {}, result)
        assert out.endswith(".md"), f"code-spec 应落 spec md，实际: {out}"
        assert result.model_used is None
    finally:
        config.SETTINGS.raw = saved


# ---------------------------------------------------------------------------
# 用例 7：archive_result 落盘的 result_*.json 含 model_used 字段（贯通 API 链路）
# ---------------------------------------------------------------------------
def test_archive_and_api_model_used(tmp_path, monkeypatch):
    raw = config.SETTINGS.raw
    saved = json.loads(json.dumps(raw))
    monkeypatch.setattr(config.SETTINGS, "output_dir", tmp_path)
    try:
        result = pipeline.BlindBoxResult(
            seed="s",
            concept="c",
            plan="p",
            output="这是一段用于归档断言的测试正文。",
            output_kind="text",
            model_used="gpt-test-model",
        )
        archive = pipeline.archive_result(result, "novel")
        json_path = tmp_path / archive["json"]
        assert json_path.exists(), f"归档 JSON 未生成: {json_path}"
        rec = json.loads(json_path.read_text(encoding="utf-8"))
        assert "model_used" in rec, "归档记录缺少 model_used 字段"
        assert rec["model_used"] == "gpt-test-model"
    finally:
        config.SETTINGS.raw = saved
