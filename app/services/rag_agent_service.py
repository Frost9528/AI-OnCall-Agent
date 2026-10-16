"""RAG Agent 服务 - 基于 LangGraph 的智能代理

模型统一经 core/llm_factory 构造（OpenAI 兼容端点），
支持真正的流式输出。
"""

from datetime import datetime
from typing import Annotated, Any, AsyncGenerator, Dict, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import before_model
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.runtime import Runtime
from loguru import logger
from typing_extensions import TypedDict

from app.config import config
from app.core.llm_factory import llm_factory
from app.models.task import TaskStatus
from app.services.task_delivery import task_delivery
from app.tools import DEFAULT_LOCAL_AGENT_TOOLS
from app.agent.aiops.failure import classify_failure, is_retryable
from app.agent.mcp_client import (
    get_mcp_client_with_retry,
    load_mcp_tools_safe,
    format_exception_chain,
    suggest_mcp_transport,
)

# 阿里千问大模型和langchain集成参考： https://docs.langchain.com/oss/python/integrations/chat/qwen
# 注意：需要配置环境变量 DASHSCOPE_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1 否则默认访问的是新加坡站点
# 同时也需要配置环境变量 DASHSCOPE_API_KEY=your_api_key


class AgentState(TypedDict):
    """Agent 状态"""
    messages: Annotated[Sequence[BaseMessage], add_messages]


@before_model
def trim_messages_middleware(
    state: AgentState,
    runtime: Runtime,
) -> dict[str, Any] | None:
    """
    修剪消息历史，只保留最近的几条消息以适应上下文窗口

    采用「硬截断」而非 LLM 摘要：不额外消耗模型调用，且能保住完整对话轮次。

    策略：
    - 保留系统消息（若存在）
    - 保留最近的 N 条消息（完整对话轮次）
    - 当消息少于等于 7 条时，不做修剪

    注意：只按位置取 messages[0] 并不安全——调用方若不传系统消息，位置 0
    就是最旧的一条用户消息，会被误当成系统提示永久保留，反而顶掉更新的上下文。

    Args:
        state: Agent 状态
        runtime: LangGraph 运行时（langchain 1.x middleware 协议要求此参数）

    Returns:
        包含修剪后消息的字典，如果无需修剪则返回 None
    """
    messages = state["messages"]

    # 如果消息数量较少，无需修剪
    if len(messages) <= 7:
        return None

    # 按类型定位系统消息，而不是假定它一定在位置 0
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]

    # 保留最近的 6 条消息（确保包含完整的对话轮次）
    recent_messages = messages[-6:] if len(messages) % 2 == 0 else messages[-7:]

    # 系统消息放最前；recent 中若已含系统消息则去重，避免重复
    new_messages = system_msgs + [m for m in recent_messages if not isinstance(m, SystemMessage)]

    logger.debug(f"修剪消息历史: {len(messages)} -> {len(new_messages)} 条")

    return {
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            *new_messages
        ]
    }


@before_model
async def inject_task_delivery_middleware(state, runtime):
    """通道2：把"已完成任务的轻量通知"注入本会话（spec §3.4）。

    只在**对话图自己的执行流内**注入，因此与用户消息写入顺序发生，
    不存在跨进程并发写 checkpoint 的竞态。

    默认关闭（config.context_inject_enabled=False）：实时性由通道1/3
    （任务表 + 前端查询）负责，本通道只解决"模型下次记得"。
    """
    if not config.context_inject_enabled:
        return None
    session_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
    if not session_id:
        return None
    try:
        pending = await task_delivery.consume_pending_for_session(session_id)
    except Exception as e:
        logger.warning(f"投递消费失败，跳过注入: {e}")
        return None
    if not pending:
        return None
    lines = ["[系统] 你此前派发的排障任务状态更新："]
    for t in pending:
        if t.status is TaskStatus.FAILED:
            lines.append(f"- {t.task_id}（排障失败），可用 get_task_report 查看详情")
        else:
            lines.append(f"- {t.task_id}（状态 {t.status.value}），可用 get_task_report 查看详情")
    return {"messages": [SystemMessage(content="\n".join(lines))]}


class RagAgentService:
    """RAG Agent 服务 - 使用 LangGraph + LangChain create_agent"""

    def __init__(self, streaming: bool = True):
        """初始化 RAG Agent 服务

        Args:
            streaming: 是否启用流式输出，默认为 True
        """
        self.model_name = config.rag_model
        self.streaming = streaming
        self.system_prompt = self._build_system_prompt()


        # 模型由 llm_factory 按 config.llm_provider 决定；这里不写死模型名
        self.model_name = config.rag_model
        self.model = llm_factory.create_chat_model(
            temperature=0.7,
            streaming=streaming,
        )

        # 定义基础工具（与 AIOps Planner/Executor 使用同一套默认本地工具）
        self.tools = list(DEFAULT_LOCAL_AGENT_TOOLS)

        # MCP 客户端（延迟初始化，使用全局管理）
        self.mcp_tools: list = []

        # 创建内存检查点（用于会话管理）
        self.checkpointer = MemorySaver()   # 默认内存态；lifespan 中由 attach_checkpointer 替换

        # Agent 初始化（会在异步方法中完成）
        self.agent = None
        self._agent_initialized = False

        logger.info(f"RAG Agent 服务初始化完成, model={self.model_name}, streaming={streaming}")

    async def _initialize_agent(self):
        """异步初始化 Agent（包括 MCP 工具）"""
        if self._agent_initialized:
            return

        for name, server in config.mcp_servers.items():
            hint = suggest_mcp_transport(
                str(server.get("url", "")),
                str(server.get("transport", "")),
            )
            if hint:
                logger.warning(f"MCP 配置 [{name}]: {hint}")

        mcp_client = await get_mcp_client_with_retry()
        mcp_tools, mcp_err = await load_mcp_tools_safe(mcp_client)
        if mcp_err:
            logger.warning(
                f"MCP 工具加载失败，将仅使用本地工具继续运行:\n{mcp_err}"
            )
            self.mcp_tools = []
        else:
            self.mcp_tools = mcp_tools
            logger.info(f"成功加载 {len(mcp_tools)} 个 MCP 工具")

        all_tools = self.tools + self.mcp_tools

        self.agent = create_agent(
            self.model,
            tools=all_tools,
            checkpointer=self.checkpointer,
            middleware=[trim_messages_middleware, inject_task_delivery_middleware],
        )

        self._agent_initialized = True


        if all_tools:
            tool_names = [tool.name if hasattr(tool, "name") else str(tool) for tool in all_tools]
            logger.info(f"可用工具列表: {', '.join(tool_names)}")

    def attach_checkpointer(self, checkpointer) -> None:
        """挂载持久化 checkpointer 并重建 agent。

        与 aiops_service.attach_checkpointer 同模式：图在编译时绑定 checkpointer，
        无法替换实例，故重建。

        注意：MCP 工具在 ``_initialize_agent`` 中懒加载。若此处直接置
        ``_agent_initialized = True``，lifespan 先于首次请求调用本方法，会导致
        ``_initialize_agent`` 早退、MCP 工具永不加载。故仅当 MCP 工具已加载
        （``mcp_tools`` 非空）时才标记 agent 初始化完成；否则保持 False，
        让首次请求仍走 ``_initialize_agent`` 加载 MCP 工具并重建 agent。
        """
        self.checkpointer = checkpointer
        self._agent_initialized = False          # 强制重建 agent
        self.agent = create_agent(
            self.model,
            tools=self.tools + self.mcp_tools,
            checkpointer=self.checkpointer,
            middleware=[trim_messages_middleware, inject_task_delivery_middleware],
        )
        # 仅当 MCP 工具已加载时才认为初始化完成，否则保留给 _initialize_agent 懒加载
        self._agent_initialized = bool(self.mcp_tools)
        logger.info(f"对话 Agent 已挂载 checkpointer: {type(checkpointer).__name__}（初始化={self._agent_initialized}）")

    def _build_system_prompt(self) -> str:
        """
        构建系统提示词

        注意：LangChain 框架会自动将工具信息传递给 LLM，
        因此系统提示词中无需列举具体的工具列表。

        Returns:
            str: 系统提示词
        """
        from textwrap import dedent

        return dedent("""
            你是一个专业的AI助手，能够使用多种工具来帮助用户解决问题。

            工作原则:
            1. 理解用户需求，选择合适的工具来完成任务
            2. 当需要获取实时信息或专业知识时，主动使用相关工具
            3. 基于工具返回的结果提供准确、专业的回答
            4. 如果工具无法提供足够信息，请诚实地告知用户

            回答要求:
            - 保持友好、专业的语气
            - 回答简洁明了，重点突出
            - 基于事实，不编造信息
            - 如有不确定的地方，明确说明

            请根据用户的问题，灵活使用可用工具，提供高质量的帮助。
        """).strip()

    async def query(
        self,
        question: str,
        session_id: str,
    ) -> str:
        """
        非流式处理用户问题（一次性返回完整答案）

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Returns:
            str: 完整答案
        """
        try:
            await self._initialize_agent()

            logger.info(f"[会话 {session_id}] RAG Agent 收到查询（非流式）: {question}")

            # 构建消息列表（系统提示 + 用户问题）
            messages = [
                SystemMessage(content=self.system_prompt),
                HumanMessage(content=question)
            ]

            # 构建 Agent 输入
            agent_input = {"messages": messages}

            # 配置 thread_id（用于会话持久化）
            config_dict = {
                "configurable": {
                    "thread_id": session_id
                }
            }

            result = await self.agent.ainvoke(
                input=agent_input,
                config=config_dict,
            )

            # 提取最终答案
            messages_result = result.get("messages", [])
            if messages_result:
                last_message = messages_result[-1]
                answer = last_message.content if hasattr(last_message, 'content') else str(last_message)

                # 记录工具调用
                if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                    tool_names = [tc.get("name", "unknown") for tc in last_message.tool_calls]
                    logger.info(f"[会话 {session_id}] Agent 调用了工具: {tool_names}")

                logger.info(f"[会话 {session_id}] RAG Agent 查询完成（非流式）")
                return answer

            logger.warning(f"[会话 {session_id}] Agent 返回结果为空")
            return ""

        except Exception as e:
            logger.error(
                f"[会话 {session_id}] RAG Agent 查询失败（非流式）: "
                f"{format_exception_chain(e)}"
            )
            raise

    async def query_stream(
        self,
        question: str,
        session_id: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式处理用户问题（逐步返回答案片段）

        **重试语义（P1-1）**：首 token 产出**之前**失败 → 整次重试；
        已经开始推流后再失败 → **不重试**（否则用户会看到前后两段矛盾内容），
        改为发出降级错误提示。

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Yields:
            Dict[str, Any]: 包含流式数据的字典
                - type: "content" | "tool_call" | "complete" | "error" | "degraded"
                - data: 具体内容
        """
        try:
            await self._initialize_agent()
        except Exception as e:
            detail = format_exception_chain(e)
            logger.error(f"[会话 {session_id}] Agent 初始化失败: {detail}")
            yield {"type": "error", "data": detail}
            return

        logger.info(f"[会话 {session_id}] RAG Agent 收到查询（流式）: {question}")

        config_dict = {"configurable": {"thread_id": session_id}}
        max_retries = max(0, config.llm_max_retries)
        last_error = ""

        # 首 token 前允许有界重试；一旦推过流就锁定，不再重试
        for attempt in range(max_retries + 1):
            produced_output = False
            try:
                messages = [
                    SystemMessage(content=self.system_prompt),
                    HumanMessage(content=question),
                ]

                async for token, metadata in self.agent.astream(
                    input={"messages": messages},
                    config=config_dict,
                    stream_mode="messages",
                ):
                    node_name = metadata.get('langgraph_node', 'unknown') if isinstance(metadata, dict) else 'unknown'
                    message_type = type(token).__name__

                    if message_type in ("AIMessage", "AIMessageChunk"):
                        content_blocks = getattr(token, 'content_blocks', None)

                        if content_blocks and isinstance(content_blocks, list):
                            for block in content_blocks:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    text_content = block.get('text', '')
                                    if text_content:
                                        produced_output = True
                                        yield {
                                            "type": "content",
                                            "data": text_content,
                                            "node": node_name
                                        }

                logger.info(f"[会话 {session_id}] RAG Agent 查询完成（流式）")
                yield {"type": "complete"}
                return

            except Exception as e:
                detail = format_exception_chain(e)
                last_error = detail

                if produced_output:
                    # 已推流：重试会让用户看到重复/矛盾内容，改为降级提示
                    logger.error(
                        f"[会话 {session_id}] 已推流后失败，不重试，发降级提示: {detail}"
                    )
                    yield {
                        "type": "degraded",
                        "data": "回答在生成过程中中断，已输出内容可能不完整。请重试或换个问法。",
                        "detail": detail,
                    }
                    return

                kind = classify_failure(e)
                if is_retryable(kind) and attempt < max_retries:
                    logger.warning(
                        f"[会话 {session_id}] 首 token 前失败（{kind.value}），"
                        f"第 {attempt + 1} 次重试: {detail}"
                    )
                    continue

                logger.error(
                    f"[会话 {session_id}] 流式查询失败（{kind.value}，不可重试或已达上限）: {detail}"
                )
                break

        yield {"type": "error", "data": last_error}

    async def get_session_history(self, session_id: str) -> list:
        """获取会话历史（从 checkpointer 读取；持久化后可跨重启）。"""
        try:
            cfg = {"configurable": {"thread_id": session_id}}
            tup = await self.checkpointer.aget_tuple(cfg)
            if not tup:
                return []
            messages = (tup.checkpoint or {}).get("channel_values", {}).get("messages", [])
            history = []
            for msg in messages:
                if isinstance(msg, SystemMessage):
                    continue
                role = "user" if isinstance(msg, HumanMessage) else "assistant"
                history.append({
                    "role": role,
                    "content": msg.content if hasattr(msg, "content") else str(msg),
                    "timestamp": getattr(msg, "timestamp", None) or datetime.now().isoformat(),
                })
            logger.info(f"获取会话历史: {session_id}, 消息数量: {len(history)}")
            return history
        except Exception as e:
            logger.error(f"获取会话历史失败: {session_id}, 错误: {e}")
            return []

    async def clear_session(self, session_id: str) -> bool:
        """清空会话历史。"""
        try:
            await self.checkpointer.adelete_thread(session_id)
            logger.info(f"已清除会话历史: {session_id}")
            return True
        except Exception as e:
            logger.error(f"清空会话历史失败: {session_id}, 错误: {e}")
            return False

    async def cleanup(self):
        """清理资源"""
        try:
            logger.info("清理 RAG Agent 服务资源...")
            # MCP 客户端由全局管理器统一管理，无需手动清理
            logger.info("RAG Agent 服务资源已清理")
        except Exception as e:
            logger.error(f"清理资源失败: {e}")


# 全局单例 - 启用流式输出
rag_agent_service = RagAgentService(streaming=True)
