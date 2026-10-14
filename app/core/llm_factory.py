"""LLM 工厂类

统一从一处构造对话模型，便于切换模型/提供方。

**为什么用 OpenAI 兼容模式而非 ChatQwen**：
`langchain_qwq.ChatQwen` 走的是 DashScope 原生站点/鉴权路径，对部分模型与
API Key 会返回 401；而各家提供的 **OpenAI 兼容端点**对 `qwen-*` / `deepseek-*`
都稳定可用。因此本工厂统一走 ChatOpenAI + 兼容端点。

**提供方切换**（`config.llm_provider`）：
- `dashscope`：DashScope 兼容端点
- `deepseek` ：DeepSeek 官方 API

**DeepSeek 的 thinking 模式坑（实测）**：
DeepSeek 官方模型默认开启 thinking（先输出 reasoning_content）。开启时：
- 强制 `tool_choice` 会报 `400 Thinking mode does not support this tool_choice`，
  而 `with_structured_output` 正是靠强制 tool_choice 实现的 → **直接失败**；
- `response_format=json_schema` 报 `This response_format type is unavailable`。
关闭 thinking（`extra_body={"thinking": {"type": "disabled"}}`）后两条路都恢复，
故本工厂对 deepseek 提供方默认关闭 thinking。
"""

from langchain_openai import ChatOpenAI
from app.config import config
from loguru import logger


class LLMFactory:
    """LLM 工厂类 - 使用 OpenAI 兼容模式"""

    # 阿里云 DashScope OpenAI 兼容模式 URL
    DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # DeepSeek 官方 OpenAI 兼容端点
    DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

    @staticmethod
    def _resolve(
        model: str | None,
        base_url: str | None,
        api_key: str | None,
    ) -> tuple[str, str, str, dict]:
        """解析出 (model, base_url, api_key, extra_body)。"""
        provider = (config.llm_provider or "dashscope").lower()

        if provider == "deepseek":
            model = model or config.deepseek_model
            base_url = base_url or config.deepseek_api_base or LLMFactory.DEEPSEEK_BASE_URL
            api_key = api_key or config.deepseek_api_key
            # 关闭 thinking：否则强制 tool_choice 会被拒，结构化输出不可用
            extra_body = {"thinking": {"type": "disabled"}}
        else:
            model = model or config.dashscope_model
            base_url = base_url or LLMFactory.DASHSCOPE_BASE_URL
            api_key = api_key or config.dashscope_api_key
            extra_body = {}

        if not api_key:
            logger.warning(
                f"提供方 {provider} 的 API Key 为空，后续 LLM 调用将失败；"
                f"请在 .env 配置对应 key"
            )
        return model, base_url, api_key, extra_body

    @staticmethod
    def create_chat_model(
        model: str | None = None,
        temperature: float = 0.7,
        streaming: bool = False,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> ChatOpenAI:
        model, base_url, api_key, extra_body = LLMFactory._resolve(
            model, base_url, api_key
        )

        return ChatOpenAI(
            model=model,
            temperature=temperature,
            streaming=streaming,
            base_url=base_url,
            api_key=api_key,
            extra_body=extra_body or None,
        )

    @staticmethod
    def create_structured_model(
        schema,
        model: str | None = None,
        temperature: float = 0.0,
    ):
        """构造带结构化输出的模型。

        **统一用 ``method="function_calling"``**：两家提供方对
        ``response_format=json_schema`` 都会报错（DashScope: "unavailable now"；
        DeepSeek: 同），而函数调用路径稳定可用。

        集中在工厂里设置，避免三个调用点各写一遍、日后漏改。
        """
        llm = LLMFactory.create_chat_model(model=model, temperature=temperature)
        return llm.with_structured_output(schema, method="function_calling")


# 全局 LLM 工厂实例
llm_factory = LLMFactory()
