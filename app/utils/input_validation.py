"""API 层输入校验

校验放在进入 Agent 之前：拒绝空/纯空白/超长输入，避免白白消耗 LLM 调用。
对话与 AIOps 两个 Agent 的接口共用本模块。
"""

from fastapi import HTTPException

from app.config import config


def validate_user_input(raw: str | None, field_name: str = "输入") -> str:
    """校验用户输入，返回去除首尾空白后的文本。

    规则：
    - None / 纯空白 → 400
    - 超过 ``config.input_max_length`` 字符 → 400

    Args:
        raw: 原始输入
        field_name: 用于错误信息展示的字段名

    Returns:
        str: 去除首尾空白后的输入

    Raises:
        HTTPException: 校验不通过时抛 400
    """
    if raw is None or not raw.strip():
        raise HTTPException(status_code=400, detail=f"{field_name}不能为空")

    text = raw.strip()
    max_length = config.input_max_length
    if len(text) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name}长度超过限制（最大 {max_length} 字符，当前 {len(text)} 字符）",
        )

    return text
