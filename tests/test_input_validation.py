"""API 层输入校验测试（P0-4）

覆盖：空串 / 纯空白 / 超长 / 正常输入，以及边界值恰好等于上限。
校验实现为同步函数，因此无需 pytest-asyncio。
"""

import pytest
from fastapi import HTTPException

from app.config import config
from app.utils.input_validation import validate_user_input


def test_empty_string_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_user_input("", "问题")
    assert exc.value.status_code == 400


def test_whitespace_only_rejected():
    """纯空白（空格/制表/换行）应被拒绝，不能靠 strip 后长度判断漏过。"""
    with pytest.raises(HTTPException) as exc:
        validate_user_input("   \t\n  ", "问题")
    assert exc.value.status_code == 400


def test_none_rejected():
    with pytest.raises(HTTPException) as exc:
        validate_user_input(None, "问题")
    assert exc.value.status_code == 400


def test_over_length_rejected():
    too_long = "a" * (config.input_max_length + 1)
    with pytest.raises(HTTPException) as exc:
        validate_user_input(too_long, "任务描述")
    assert exc.value.status_code == 400
    assert "任务描述" in exc.value.detail


def test_exact_max_length_accepted():
    """边界：恰好等于上限应通过。"""
    text = "a" * config.input_max_length
    assert validate_user_input(text) == text


def test_normal_input_returns_stripped():
    assert validate_user_input("  什么是向量数据库？  ") == "什么是向量数据库？"


def test_whitespace_does_not_count_toward_length():
    """首尾空白不占用长度额度：strip 后恰好等于上限应通过。"""
    text = " " * 10 + "a" * config.input_max_length + " " * 10
    assert validate_user_input(text) == "a" * config.input_max_length
