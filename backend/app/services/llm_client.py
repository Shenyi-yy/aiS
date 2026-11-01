"""LLM 客户端 —— 全后端唯一直连大模型的模块 (Phase 9)。

协议选型: OpenAI 兼容的 POST {base_url}/chat/completions。
DeepSeek、Moonshot(kimi)、通义 compatible-mode、智谱 GLM、自建 Ollama/vLLM
都提供这套接口 —— "可以调用任何模型" 的实现方式就是:
换供应商只改 /opt/zops/.env 里 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三个值,
本文件与业务代码一行不动。

项目约定 (与 zabbix_client 同款):
- 凭据只来自 Settings (Ubuntu 侧 .env → compose 注入容器环境变量),
  API Key 永不出现在代码、日志、HTTP 响应体中。
- 同步 httpx.Client: FastAPI sync 路由自动线程池执行, 全链路不阻塞事件循环。
- 所有失败 (网络 / 认证 / 供应商报错 / 响应形状异常) 归一成 LLMError,
  上层决定怎么呈现, 本模块不做任何"降级假回答"。
"""

import logging
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger("zops.services.llm_client")


class LLMError(RuntimeError):
    """LLM 调用失败 (网络 / 鉴权 / 供应商错误统一归一到这里)。"""


class LLMClient:
    """OpenAI 兼容 chat.completions 客户端 (无状态, 线程安全交给 httpx)。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        # 允许用户填带/不带结尾斜杠, 统一剥掉
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._http = httpx.Client(timeout=timeout)

    @property
    def model(self) -> str:
        return self._model

    def configured(self) -> bool:
        """三要素齐备才认为可用 (缺任何一个, 调用都会抛 LLMError)。"""
        return bool(self._base and self._api_key and self._model)

    # ---------- 底层 ----------

    def chat_raw(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """一次补全, 返回供应商完整 payload (choices/message/tool_calls 原样)。

        Phase 10 的 agent 循环需要看 tool_calls 是否存在, 所以必须拿到原样
        响应; messages 里可以包含 assistant/tool 角色 (OpenAI 标准形状)。
        """
        if not self.configured():
            raise LLMError(
                "LLM 未配置: 请在 Ubuntu 侧编辑 /opt/zops/.env 填入 "
                "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后重启 backend "
                "(Key 不要贴到聊天、Git 或任何脚本里)"
            )
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            resp = self._http.post(
                f"{self._base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 网络失败 ({self._base}): {exc}") from exc
        if resp.status_code >= 400:
            # 供应商错误体一般是原因 (余额/模型名/鉴权), 截 300 字符防日志爆炸
            raise LLMError(
                f"LLM 返回 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            payload = resp.json()
            if not payload["choices"]:
                raise KeyError("choices")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应形状异常: {str(exc)}") from exc
        logger.info(
            "LLM 调用成功 model=%s prompt_tokens=%s completion_tokens=%s",
            self._model,
            (payload.get("usage") or {}).get("prompt_tokens"),
            (payload.get("usage") or {}).get("completion_tokens"),
        )
        return payload

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """一次对话补全, 返回 (回复正文, usage 字典)。

        temperature 默认 0.2: 故障分析要稳定复现, 不要文学创作。
        """
        payload = self.chat_raw(messages, temperature=temperature, max_tokens=max_tokens)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应形状异常: {str(exc)}") from exc
        return content, payload.get("usage") or {}

    # ---------- 语义方法 (路由/业务层只调这些) ----------

    def ping(self) -> int:
        """真实最小调用验证连通性 (供 /api/v1/ai/check), 返回耗时毫秒。

        不给假 OK: 任何网络/鉴权/配额问题都会原样抛 LLMError。
        """
        start = time.monotonic()
        self.chat(
            [{"role": "user", "content": "只回复两个字母: ok"}],
            temperature=0.0,
            max_tokens=8,
        )
        return int((time.monotonic() - start) * 1000)


# ---------- 模块级单例 (与 zabbix_client 同模式) ----------

_default_client: LLMClient | None = None
_client_lock = threading.Lock()


def get_default_llm_client() -> LLMClient:
    """按当前 Settings 构造的全局客户端 (惰性单例, 线程安全)。"""
    global _default_client
    with _client_lock:
        if _default_client is None:
            from app.config import get_settings

            settings = get_settings()
            _default_client = LLMClient(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout=float(settings.llm_timeout),
            )
        return _default_client
