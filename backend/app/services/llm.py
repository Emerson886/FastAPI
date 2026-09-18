"""
==============================================================================
 大模型服务（LLM）
==============================================================================

本模块负责创建 LangChain 的 ChatModel 实例。

【★ 配置位置：backend/.env ★】
    LLM_BASE_URL  大模型服务地址（OpenAI 兼容协议）
    LLM_API_KEY   你的 API Key
    LLM_MODEL     模型名称
    LLM_TEMPERATURE / LLM_MAX_TOKENS / LLM_TIMEOUT / LLM_MAX_RETRIES

【为什么用 OpenAI 兼容协议？】
    DeepSeek、通义千问、Kimi、智谱、SiliconFlow、本地 vLLM/Ollama
    都提供 OpenAI 兼容端点，所以只要改 3 个配置项就能切换服务商，代码零改动。

【常用服务商配置示例】
    DeepSeek   base_url=https://api.deepseek.com/v1              model=deepseek-chat
    通义千问   base_url=https://dashscope.aliyuncs.com/compatible-mode/v1  model=qwen-plus
    Kimi       base_url=https://api.moonshot.cn/v1               model=moonshot-v1-8k
    智谱 GLM   base_url=https://open.bigmodel.cn/api/paas/v4     model=glm-4-plus
    本地 vLLM  base_url=http://127.0.0.1:8001/v1                 model=你的模型名
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import settings
from app.core.logging_config import logger


@lru_cache(maxsize=8)
def get_llm(
    model_name: str | None = None,
    temperature: float | None = None,
    streaming: bool | None = None,
) -> ChatOpenAI:
    """
    创建（并缓存）大模型客户端。

    :param model_name:  覆盖默认模型名（会话级配置，不同会话可用不同模型）
    :param temperature: 覆盖默认温度（知识库问答建议 0~0.4，越低越严谨）
    :param streaming:   是否流式输出

    缓存说明：lru_cache 以参数为 key，所以「同一个模型 + 同一温度」只创建一次，
             避免每次请求都新建客户端（会浪费连接池资源）。

    ⚠ 注意：返回的对象被缓存后请勿修改其属性（会污染其他调用方）。
    """
    model = model_name or settings.LLM_MODEL
    temp = settings.LLM_TEMPERATURE if temperature is None else temperature
    stream = settings.LLM_STREAMING if streaming is None else streaming

    logger.debug("创建 LLM 客户端 | model={} | temperature={} | streaming={}", model, temp, stream)

    return ChatOpenAI(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,  # type: ignore[arg-type]
        model=model,
        temperature=temp,
        max_tokens=settings.LLM_MAX_TOKENS,
        timeout=settings.LLM_TIMEOUT,
        max_retries=settings.LLM_MAX_RETRIES,
        streaming=stream,
    )


def get_current_model_name(model_name: str | None = None) -> str:
    """返回实际生效的模型名（写入消息记录，便于审计用了哪个模型）。"""
    return model_name or settings.LLM_MODEL


def check_llm_available() -> dict[str, object]:
    """
    大模型连通性自检（健康检查接口 /health 会调用）。

    注意：这一步会真实发起一次很小的请求，有成本（极低），
         所以健康检查默认不开启，只在 /health?deep=true 时调用。
    """
    info: dict[str, object] = {
        "base_url": settings.LLM_BASE_URL,
        "model": settings.LLM_MODEL,
        "available": False,
    }
    try:
        llm = get_llm(streaming=False)
        response = llm.invoke("ping")
        info["available"] = True
        info["reply"] = str(getattr(response, "content", ""))[:50]
    except Exception as exc:
        info["error"] = str(exc)
    return info
