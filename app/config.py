"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "SuperBizAgent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）

    # 对话/推理模型的提供方：dashscope | deepseek
    # 两者都是 OpenAI 兼容端点，切换只需改这里 + 对应 base_url/api_key/model。
    # 实测结论：DashScope 的 qwen 系列在免费额度耗尽后不可用；
    # DeepSeek 官方 API 的模型默认开启 thinking 模式，会拒绝强制 tool_choice，
    # 必须关掉（见 llm_factory 的 extra_body）。
    llm_provider: str = "dashscope"
    deepseek_api_key: str = ""
    deepseek_api_base: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-flash"

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_top_k: int = 3
    rag_model: str = "qwen-max"  # 使用快速响应模型，不带扩展思考

    # API 输入校验
    input_max_length: int = 2000  # 用户输入（对话问题 / 排障任务描述）字符数上限

    # 可靠性：LLM 调用重试与单步超时（共用同一套封装，见 agent/aiops/failure.py）
    llm_max_retries: int = 1          # 瞬时类失败的额外重试次数
    step_timeout_seconds: float = 60.0  # 单步 LLM/工具调用超时（秒），<=0 表示不限制

    # 持久化：SQLite checkpointer + 任务注册表
    # 缺 langgraph-checkpoint-sqlite 或 aiosqlite 时自动降级 MemorySaver，
    # 不影响启动（保持"能力开关式接入"风格）。
    checkpoint_db_path: str = "./data/checkpoints.sqlite"
    task_db_path: str = "./data/tasks.sqlite"
    checkpoint_enabled: bool = True   # 显式关闭则退回内存态（进程内）
    task_ttl_seconds: int = 3600      # ejected 停滞超过此时长收敛为 expired

    # 长任务编排
    task_background_execution: bool = True     # 关 → 同步内联执行（降级路径）
    max_delivery_attempts: int = 3
    context_inject_enabled: bool = False       # 通道2 开关，默认关

    # 多模态版面解析（P1-4）：云端飞桨 OCR，开关式接入
    # 未配置 key / 关闭开关时，/upload 退回纯文本链路，不影响启动与既有功能。
    ocr_enabled: bool = False
    ocr_api_url: str = ""             # 云端版面解析端点
    ocr_api_key: str = ""
    ocr_request_timeout: float = 30.0
    ocr_max_retries: int = 2          # 云端解析的瞬时失败重试次数

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    # MCP 服务配置（transport: stdio | sse | streamable-http）
    # 腾讯云托管 MCP 的 URL 通常含 /sse/，需使用 sse；本地 FastMCP 使用 streamable-http
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"

    # Prometheus
    prometheus_base_url: str = "http://127.0.0.1:9090"
    prometheus_request_timeout: float = 10.0

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            }
        }


# 全局配置实例
config = Settings()
