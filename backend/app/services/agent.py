"""
==============================================================================
 基于 LangChain 的 Agent（智能体）服务  —— 项目核心
==============================================================================

【架构说明：为什么用 Agent 而不是简单的「检索 + 拼接 + 提问」？】

    简单 RAG（Retrieval-Augmented Generation）流程是固定的：
        用户问题 → 检索 → 拼进 Prompt → 大模型回答
    问题是：不管问什么都去查知识库，浪费成本；追问时还会重复检索。

    Agent（智能体）模式让大模型自己决定：
        - 这个问题需不需要查知识库？（寒暄就不查）
        - 要不要换个关键词再查一次？（第一次没查到时）
        - 检索到什么程度可以回答了？
    这就是 ReAct（Reasoning + Acting）范式：思考 → 行动 → 观察 → 再思考。

【技术选型】
    LangChain 1.x 的 create_agent（底层是 LangGraph 的 StateGraph）
    - 内置 ReAct 循环、工具调用、消息历史管理
    - 支持 astream_events 做细粒度流式输出（token 级打字机效果）
    - 未来要加「人在回路（human-in-the-loop）」「多智能体协作」可直接扩展

【本模块的方法】
    create_chat_agent()     创建 Agent 实例
    stream_chat()           流式问答（SSE 用），产出 (事件类型, 数据)
    invoke_chat()           非流式问答（一次性拿到完整回答）
    retrieve_knowledge()    供 Agent 调用的知识库检索工具（内部函数）
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging_config import logger
from app.schemas.message import Citation
from app.services.llm import get_llm
from app.services.rag.prompts import (
    NO_CONTEXT_PLACEHOLDER,
    RETRIEVER_TOOL_DESCRIPTION,
    SYSTEM_PROMPT,
)
from app.services.rag.vector_store import similarity_search_with_score

# 公司名称（会填进系统提示词，可按需修改或移到 .env）
COMPANY_NAME = "示例科技"

# Agent 单次对话最多迭代的轮数（防止模型陷入无限工具调用循环）
AGENT_RECURSION_LIMIT = 12


# =============================================================================
# 1. 知识库检索工具
# =============================================================================
def make_knowledge_base_tool(
    user_id: int,
    citations_sink: list[Citation],
    document_ids: list[int] | None = None,
):
    """
    构造「知识库检索」工具。

    【为什么用闭包（函数内定义函数）而不是全局工具？】
        RAG 检索必须带 user_id 做权限过滤，而 user_id 是「每个请求不同」的。
        LangChain 的工具签名只允许模型传 query 参数，
        如果把 user_id 做成工具参数，模型有可能被诱导传入别人的 ID（越权风险）。
        用闭包把 user_id「焊死」在工具内部，模型无法篡改 —— 这是安全最佳实践。

    :param user_id:        当前登录用户 ID（权限过滤依据）
    :param citations_sink: 检索结果收集容器，用于把引用来源返回给前端展示
    :param document_ids:   限定检索范围（可选）
    """

    @tool("search_knowledge_base", description=RETRIEVER_TOOL_DESCRIPTION)
    def search_knowledge_base(query: str) -> str:
        """
        在公司知识库中检索与 query 相关的资料。

        Args:
            query: 检索关键词或问题，例如「年假天数规定」

        Returns:
            格式化的检索结果文本，供大模型阅读理解。
        """
        logger.info("Agent 调用知识库检索 | user_id={} | query={}", user_id, query)

        results = similarity_search_with_score(
            query=query,
            top_k=settings.RAG_TOP_K,
            user_id=user_id,                 # ← 权限过滤（只检索用户可见的文档）
            document_ids=document_ids,
        )

        if not results:
            return "知识库中没有检索到与该问题相关的内容。"

        # ---- 收集引用来源（去重后放入 sink，供前端展示「参考来源」）----
        seen_keys: set[str] = set()
        formatted_parts: list[str] = []

        for index, (doc, score) in enumerate(results, start=1):
            meta = doc.metadata or {}
            doc_id = meta.get("document_id")
            chunk_index = meta.get("chunk_index")
            key = f"{doc_id}_{chunk_index}"
            filename = meta.get("filename") or meta.get("title") or "未知文档"

            if key not in seen_keys:
                seen_keys.add(key)
                citations_sink.append(
                    Citation(
                        doc_id=int(doc_id) if doc_id is not None else None,
                        filename=str(filename),
                        title=str(meta.get("title") or filename),
                        chunk_index=int(chunk_index) if chunk_index is not None else None,
                        content=doc.page_content[:500],  # 截断，避免前端展示过长
                        score=round(float(score), 4),
                    )
                )

            # ---- 拼给大模型看的文本（带来源编号，方便模型标注出处）----
            formatted_parts.append(
                f"【资料 {index}】来源：{filename}（相关度：{score:.2f}）\n{doc.page_content}"
            )

        return "\n\n---\n\n".join(formatted_parts)

    return search_knowledge_base


# =============================================================================
# 2. 创建 Agent 实例
# =============================================================================
def create_chat_agent(
    user_id: int,
    citations_sink: list[Citation],
    model_name: str | None = None,
    use_rag: bool = True,
    document_ids: list[int] | None = None,
    context_text: str | None = None,
    streaming: bool = True,
):
    """
    创建一个带知识库工具的 ReAct Agent。

    :param user_id:       当前用户（用于检索权限过滤）
    :param citations_sink: 引用来源收集列表（调用方传入并在之后读取）
    :param model_name:    指定模型（会话级配置）
    :param use_rag:       是否挂载知识库工具（False 时退化为纯聊天机器人）
    :param document_ids:  限定检索的文档范围
    :param context_text:  本次请求检索到的知识文本（若调用方已预检索）
    :param streaming:     是否流式

    返回：编译好的 LangGraph Agent（可用 astream_events / ainvoke 调用）
    """
    tools = []
    if use_rag:
        tools.append(
            make_knowledge_base_tool(
                user_id=user_id,
                citations_sink=citations_sink,
                document_ids=document_ids,
            )
        )

    # 系统提示词：填入公司名与「知识库上下文」
    system_prompt = SYSTEM_PROMPT.format(
        company_name=COMPANY_NAME,
        context=context_text or NO_CONTEXT_PLACEHOLDER,
    )

    agent = create_agent(
        model=get_llm(model_name=model_name, streaming=streaming),
        tools=tools,
        system_prompt=system_prompt,
        name="kb_assistant",
    )
    return agent


# =============================================================================
# 3. 构建消息列表（把 MySQL 里的历史记录转成 LangChain 消息）
# =============================================================================
def build_messages(
    question: str,
    history: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """
    把「历史对话 + 当前提问」转成 LangChain 的消息对象列表。

    :param history: [{"role": "user"|"assistant", "content": "..."}]
                    由 crud_message.get_history_for_llm() 提供
    """
    messages: list[Any] = []

    for item in history or []:
        role = item.get("role")
        content = item.get("content") or ""
        if not content.strip():
            continue
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
        elif role == "system":
            messages.append(SystemMessage(content=content))

    # 当前提问放在最后
    messages.append(HumanMessage(content=question))
    return messages


# =============================================================================
# 4. 流式问答（SSE 核心）
# =============================================================================
async def stream_chat(
    question: str,
    history: list[dict[str, Any]] | None = None,
    user_id: int = 0,
    model_name: str | None = None,
    use_rag: bool = True,
    document_ids: list[int] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    流式问答生成器。产出的事件字典与 schemas.message.ChatStreamChunk 对应：

        {"type": "citations", "citations": [...]}   检索到的引用来源（可能多次）
        {"type": "delta",     "content": "增量文本"}  逐字输出
        {"type": "done",      "content": "完整回答",
                              "latency_ms": 1234,
                              "prompt_tokens": 100,
                              "completion_tokens": 200,
                              "model_name": "deepseek-chat",
                              "citations": [...]}

    调用方（chat 接口）把这些事件包装成 SSE 帧推给前端。

    【astream_events 的事件类型说明】
        on_chat_model_stream  模型每产出一个 token 触发一次 → 用于打字机效果
        on_tool_start          Agent 开始调用工具
        on_tool_end            Agent 工具调用结束 → 此时引用来源已经收集好
        on_chain_end          整个 Agent 执行结束 → 取最终完整回答
    """
    started = time.perf_counter()
    citations_sink: list[Citation] = []
    full_text_parts: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    final_text: str | None = None
    emitted_citation_count = 0

    try:
        agent = create_chat_agent(
            user_id=user_id,
            citations_sink=citations_sink,
            model_name=model_name,
            use_rag=use_rag,
            document_ids=document_ids,
            streaming=True,
        )
        messages = build_messages(question, history)

        # ---------------------------------------------------------------
        # 使用 astream_events 逐事件消费
        # version="v2" 是当前稳定版本的事件协议
        # ---------------------------------------------------------------
        async for event in agent.astream_events(
            {"messages": messages},
            version="v2",
            config={"recursion_limit": AGENT_RECURSION_LIMIT},
        ):
            event_type = event.get("event")

            # ---- A. 模型输出的 token ----
            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk is None:
                    continue

                # 有些模型会同时返回 reasoning_content（思维链），这里只取正文
                text = ""
                content = getattr(chunk, "content", None)
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    # 部分服务返回结构化 content（如 [{"type":"text","text":"..."}]）
                    text = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )

                if text:
                    full_text_parts.append(text)
                    yield {"type": "delta", "content": text}

                # ---- 顺带取 token 用量（通常只有最后一个 chunk 带 usage_metadata）----
                usage = getattr(chunk, "usage_metadata", None)
                if usage:
                    prompt_tokens = usage.get("input_tokens", prompt_tokens)
                    completion_tokens = usage.get("output_tokens", completion_tokens)

            # ---- B. 工具调用结束：把新收集到的引用来源推给前端 ----
            elif event_type == "on_tool_end":
                if len(citations_sink) > emitted_citation_count:
                    new_citations = citations_sink[emitted_citation_count:]
                    emitted_citation_count = len(citations_sink)
                    yield {
                        "type": "citations",
                        "citations": [c.model_dump() for c in new_citations],
                    }

            # ---- C. 整个 Agent 结束：取最终回答文本 ----
            elif event_type == "on_chain_end" and event.get("name") == "kb_assistant":
                output = event.get("data", {}).get("output")
                final_text = _extract_final_text(output, full_text_parts)

    except Exception as exc:
        logger.exception("Agent 流式问答失败 | user_id={} | question={}", user_id, question[:50])
        yield {
            "type": "error",
            "error": _friendly_error_message(exc),
        }
        return

    # ------------------------------------------------------------------
    # 收尾：输出 done 事件
    # ------------------------------------------------------------------
    content = final_text if final_text else "".join(full_text_parts)
    latency_ms = int((time.perf_counter() - started) * 1000)

    logger.info(
        "Agent 回答完成 | user_id={} | 模型={} | 耗时={}ms | 字数={} | 引用={} 条",
        user_id, model_name or settings.LLM_MODEL, latency_ms, len(content), len(citations_sink),
    )

    yield {
        "type": "done",
        "content": content,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "model_name": model_name or settings.LLM_MODEL,
        "citations": [c.model_dump() for c in citations_sink],
    }


# =============================================================================
# 5. 非流式问答（一次性返回，适合接口调试或非实时场景）
# =============================================================================
async def invoke_chat(
    question: str,
    history: list[dict[str, Any]] | None = None,
    user_id: int = 0,
    model_name: str | None = None,
    use_rag: bool = True,
    document_ids: list[int] | None = None,
) -> dict[str, Any]:
    """
    非流式问答：等模型全部生成完再返回。

    返回：
        {"content": "...", "citations": [...], "latency_ms": 1234,
         "model_name": "...", "prompt_tokens": ..., "completion_tokens": ...}
    """
    started = time.perf_counter()
    citations_sink: list[Citation] = []

    agent = create_chat_agent(
        user_id=user_id,
        citations_sink=citations_sink,
        model_name=model_name,
        use_rag=use_rag,
        document_ids=document_ids,
        streaming=False,
    )
    messages = build_messages(question, history)

    result = await agent.ainvoke(
        {"messages": messages},
        config={"recursion_limit": AGENT_RECURSION_LIMIT},
    )

    content = _extract_final_text(result, [])
    latency_ms = int((time.perf_counter() - started) * 1000)

    return {
        "content": content,
        "citations": [c.model_dump() for c in citations_sink],
        "latency_ms": latency_ms,
        "model_name": model_name or settings.LLM_MODEL,
        "prompt_tokens": None,
        "completion_tokens": None,
    }


# =============================================================================
# 6. 内部辅助函数
# =============================================================================
def _extract_final_text(output: Any, fallback_parts: list[str]) -> str:
    """
    从 Agent 的返回值里提取最终回答文本。

    注意：Agent 返回的是完整消息列表（包含工具调用消息、中间思考等），
         我们要取【最后一条 AI 消息】的内容作为最终回答，
         而不是把所有消息拼起来（那样会把工具调用过程也带给用户）。
    """
    try:
        if isinstance(output, dict) and "messages" in output:
            messages = output["messages"]
            for message in reversed(messages):
                # 只取 AI 的最终回答：有 content 且不是工具调用请求
                if isinstance(message, AIMessage) or getattr(message, "type", "") == "ai":
                    content = getattr(message, "content", "")
                    if isinstance(content, list):
                        content = "".join(
                            part.get("text", "") if isinstance(part, dict) else str(part)
                            for part in content
                        )
                    if content and str(content).strip():
                        return str(content)
    except Exception:  # pragma: no cover
        logger.warning("解析 Agent 输出失败，回退到流式累积内容")

    return "".join(fallback_parts)


def _friendly_error_message(exc: Exception) -> str:
    """
    把技术异常转成用户能看懂的提示（避免把堆栈/SQL/密钥暴露给前端）。

    企业项目里这一步很重要：错误信息既要有助于排查，又不能泄露内部实现。
    """
    text = str(exc).lower()

    if "api key" in text or "unauthorized" in text or "401" in text:
        return "大模型服务认证失败，请检查 backend/.env 中的 LLM_API_KEY 是否正确"
    if "timeout" in text or "timed out" in text:
        return "大模型响应超时，请稍后重试或缩短问题长度"
    if "rate limit" in text or "429" in text:
        return "大模型服务调用频率超限，请稍后重试"
    if "connection" in text or "connect" in text:
        return "无法连接大模型服务，请检查网络与 LLM_BASE_URL 配置"
    if "embedding" in text:
        return "向量化服务异常，请检查 backend/.env 中的 EMBEDDING_* 配置"
    if "insufficient" in text or "balance" in text or "quota" in text:
        return "大模型账户余额或配额不足，请充值后重试"

    return "生成回答时出现异常，请稍后重试。如持续失败请查看后端日志（backend/logs/）"
