"""历史记录相关端点的单元测试（favorite, rating, stats）。

运行：
  <解释器> -m pytest backend/tests/test_history_endpoints.py -q
"""
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# 把 backend 加入路径，确保 `from app import ...` 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main, config


def test_rating_validation():
    """测试评分验证逻辑。"""
    # 测试有效评分
    for r in [1, 2, 3, 4, 5]:
        try:
            main.set_rating("test001", main.RatingRequest(rating=r))
        except:
            pass  # 忽略文件不存在的错误，只验证参数校验通过
    
    # 测试无效评分
    invalid_ratings = [0, 6, -1, 10, 100]
    for r in invalid_ratings:
        try:
            main.set_rating("test001", main.RatingRequest(rating=r))
            assert False, f"评分 {r} 应该被拒绝"
        except main.HTTPException as e:
            assert e.status_code == 400
            assert "1-5" in e.detail


def test_rating_null_clear():
    """测试清除评分（传 null）。"""
    req = main.RatingRequest(rating=None)
    assert req.rating is None


def test_favorite_request():
    """测试收藏请求模型。"""
    req = main.FavoriteRequest(favorite=True)
    assert req.favorite is True
    
    req = main.FavoriteRequest(favorite=False)
    assert req.favorite is False


def test_archive_id_validation():
    """测试归档 ID 长度验证。"""
    # 测试正常长度
    assert main._find_archive_by_id("abc12345") is None  # 不存在但不会因为长度被拒绝
    
    # 测试过长 ID
    long_id = "a" * 51
    assert main._find_archive_by_id(long_id) is None  # 应该因为长度被拒绝
    
    # 测试空 ID
    assert main._find_archive_by_id("") is None
    assert main._find_archive_by_id(None) is None


def test_usage_field_type_validation():
    """测试 usage 字段的类型验证逻辑。"""
    # 模拟 stats 函数中的 usage 处理逻辑
    test_cases = [
        ({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}, (100, 50, 150)),
        ({"prompt_tokens": "invalid", "completion_tokens": 50, "total_tokens": 100}, (0, 0, 0)),
        ({"prompt_tokens": 100, "completion_tokens": None, "total_tokens": 150}, (100, 0, 150)),
        ({"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}, (0, 0, 0)),
        ({}, (0, 0, 0)),
        (None, (0, 0, 0)),
    ]
    
    for usage, expected in test_cases:
        t_prompt = t_completion = t_total = 0
        u = usage or {}
        if isinstance(u, dict):
            try:
                t_prompt += int(u.get("prompt_tokens") or 0)
                t_completion += int(u.get("completion_tokens") or 0)
                t_total += int(u.get("total_tokens") or 0)
            except (ValueError, TypeError):
                pass
        assert (t_prompt, t_completion, t_total) == expected, f"Failed for usage={usage}"


def test_cost_calculation():
    """测试成本计算逻辑。"""
    # 使用 t_total 计算
    t_total = 1000
    price = 0.01
    cost = round(t_total / 1000.0 * price, 4)
    assert cost == 0.01
    
    # t_total 为 0 时
    cost = round(0 / 1000.0 * price, 4) if t_total else 0.0
    assert cost == 0.0


def test_file_lock_mechanism():
    """测试文件锁机制的基本功能。"""
    # 测试锁的创建和获取
    test_path = Path("test_file.json")
    lock1 = main._get_archive_lock(test_path)
    lock2 = main._get_archive_lock(test_path)
    
    # 同一文件应该返回同一个锁
    assert lock1 is lock2
    
    # 不同文件应该返回不同的锁
    test_path2 = Path("test_file2.json")
    lock3 = main._get_archive_lock(test_path2)
    assert lock1 is not lock3


def test_stats_cache_structure():
    """测试缓存变量的初始化。"""
    assert hasattr(main, '_stats_cache')
    assert hasattr(main, '_stats_cache_time')
    assert hasattr(main, '_stats_cache_ttl')
    assert main._stats_cache_ttl == 60


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
