"""M9 的真实模型客户端：任何 OpenAI-compatible 的 ``/chat/completions``。

plan §2 锁定用 ``httpx`` 直连，不引入厂商 SDK：OpenAI、Anthropic 代理、
ollama、vLLM 的差异只在 ``base_url`` 与是否需要 API key，把这段差异收在
一个适配器里，就能用同一个 ``Provider`` 协议覆盖它们。

三条边界（与 D-008 / D-012 对齐）：

- **只做传输。** prompt 被包成一条 user message；响应只取
  ``choices[0].message.content``。解析补丁、校验证据、决定是否 pending
  仍然全部发生在 :mod:`zhiwo.extract` 与 :mod:`zhiwo.merge`。
- **失败翻译成词汇表里的异常。** 网络错误、非 2xx、响应信封畸形都抛
  :class:`~zhiwo.errors.ProviderError`，上层因此能在不 import httpx 的
  情况下识别"这次提取没成"，并按铁律 2 继续提交 journal。
- **配置错误在构造时失败。** 缺 model、空 base_url、非法 timeout 抛
  :class:`~zhiwo.errors.ZhiwoError`：这是用户改环境变量就能修复的设置
  错误，不应伪装成一次可降级的网络故障。

``identity`` 取 ``openai-compatible:<model>``，进入 changelog 的
``extractor`` 字段（SPEC §3）。模型名通常自带版本号，这一层不再额外拼
时间戳或随机量——同一条事实的溯源必须可重复。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from zhiwo.errors import ProviderError, ZhiwoError
from zhiwo.llm.base import Provider

# 环境变量与 ME_HOME 同一前缀：属于本产品的配置，不与厂商 SDK 的
# OPENAI_* 变量互相污染（同一台机器上可能同时跑着别的工具）。
BASE_URL_ENV = "ME_LLM_BASE_URL"
API_KEY_ENV = "ME_LLM_API_KEY"
MODEL_ENV = "ME_LLM_MODEL"

# 不设置 base_url 时按官方地址；这是"OpenAI-compatible"这个名字最不
# 意外的默认值。本地端点（ollama / vLLM）必须显式覆盖。
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# httpx 的分阶段超时（connect/read/write/pool 各自 60 秒），不是一次
# 调用的墙钟总时长。capture 持有写锁，卡死比失败更糟：60 秒足够普通
# 推理服务返回一次补丁，又不会让任一阶段无限期占住锁。
DEFAULT_TIMEOUT = 60.0

_ERROR_BODY_LIMIT = 300


class OpenAICompatibleProvider:
    """把一个 chat-completions 端点包装成 :class:`~zhiwo.llm.base.Provider`。

    ``transport`` 只供测试注入 ``httpx.MockTransport``；生产路径永远走
    httpx 自己的网络传输。每次 ``complete`` 建一个短生命周期的
    ``httpx.Client``：CLI 一次调用只发一个请求，换取"不用管理连接池和
    关闭时机"的简单性，也让 provider 本身是无状态、可跨线程构建的。
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model.strip()
        if not self._model:
            raise ZhiwoError("LLM model must be a non-empty string")

        self._base_url = base_url.strip().rstrip("/")
        if not self._base_url:
            raise ZhiwoError("LLM base_url must be a non-empty string")
        parsed = urlsplit(self._base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ZhiwoError(
                f"LLM base_url must be an absolute http(s) URL, got {self._base_url!r}"
            )

        # 空字符串与未设置等价：本地端点通常不需要 key，shell 里的
        # ``ME_LLM_API_KEY=`` 不应被当成一个真的空 Bearer token 发出去。
        stripped_key = api_key.strip() if api_key is not None else ""
        self._api_key = stripped_key or None

        if timeout <= 0:
            raise ZhiwoError(f"LLM timeout must be > 0, got {timeout}")
        self._timeout = timeout
        self._transport = transport
        self._identity = f"openai-compatible:{self._model}"

    @property
    def identity(self) -> str:
        return self._identity

    def complete(self, prompt: str) -> str:
        """发送一次 chat completion；返回模型文本（不解析、不校验）。

        非 2xx 也走 ``ProviderError`` 而不是让 httpx 的
        ``raise_for_status`` 抛出：上层只认识 zhiwo 的异常词汇表。错误
        消息带状态码和截断后的响应体，足够定位鉴权/模型名错误；截断避免
        一个 HTML 错误页把终端刷满。API key 在消息里统一替换成 ``***``：
        端点或代理偶尔会把请求头回显进错误页，审计日志不应因此泄漏密钥。
        """
        url = f"{self._base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(url, headers=headers, json=payload)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ProviderError(_redact(f"cannot reach {url}: {exc}", self._api_key)) from exc

        if not 200 <= response.status_code < 300:
            detail = _error_detail(response)
            raise ProviderError(
                _redact(f"{url} returned HTTP {response.status_code}{detail}", self._api_key)
            )

        try:
            body: object = response.json()
        except ValueError as exc:
            raise ProviderError(
                _redact(
                    f"{url} returned HTTP {response.status_code} with invalid JSON: {exc}",
                    self._api_key,
                )
            ) from exc
        return _message_content(body, url=url)


def provider_from_env(environ: Mapping[str, str] | None = None) -> Provider | None:
    """按环境变量构造真实 provider；完全未配置时返回 ``None``。

    矩阵（D-012）：

    ==========================  =============================================
    环境变量                    结果
    ==========================  =============================================
    三个都空                    ``None``（capture 降级为 journal-only）
    只有 model                  官方 base_url、无鉴权（本地代理也能这样用）
    model + base_url            指定端点；是否带 key 由第三个变量决定
    model + base_url + key      Bearer 鉴权
    任何"部分配置"（如只有      ``ZhiwoError``：用户显然想配置，静默降级
    base_url 或 key）           会让一次 typo 永远不被发现
    ==========================  =============================================

    传 ``environ`` 让测试不依赖进程环境；生产调用使用 ``os.environ``。
    """
    values = os.environ if environ is None else environ
    model = values.get(MODEL_ENV, "").strip()
    base_url = values.get(BASE_URL_ENV, "").strip()
    api_key = values.get(API_KEY_ENV, "").strip()

    if not (model or base_url or api_key):
        return None
    if not model:
        raise ZhiwoError(
            f"{MODEL_ENV} is required when configuring the live LLM provider; "
            f"{BASE_URL_ENV} and {API_KEY_ENV} are optional"
        )
    return OpenAICompatibleProvider(
        model=model,
        base_url=base_url or DEFAULT_BASE_URL,
        api_key=api_key or None,
    )


def _redact(text: str, secret: str | None) -> str:
    """把 API key 从错误消息里抹掉（D-012 的 "never the key"）。

    正常路径不会把 key 拼进消息；这里防的是代理/端点把请求头回显进
    错误页这种意外，确保无论响应体写了什么，日志里都不会出现密钥。
    """
    return text.replace(secret, "***") if secret else text


def _error_detail(response: httpx.Response) -> str:
    """截断响应体成一行；空响应体不产生一个孤零零的冒号。"""
    text = " ".join(response.text.split())
    if not text:
        return ""
    if len(text) > _ERROR_BODY_LIMIT:
        text = text[:_ERROR_BODY_LIMIT] + "…"
    return f": {text}"


def _message_content(body: object, *, url: str) -> str:
    """从 chat-completions 信封里取出正文；结构不符即 ``ProviderError``。

    只接受字符串 content。``content: null``（拒答）或内容分片数组属于
    当前协议未覆盖的形态，明确报错比猜一个拼接规则更安全——模型没给出
    可解析文本时，调用方本来就要走"这次没有可提取内容"的降级路径。
    """
    if not isinstance(body, dict):
        raise ProviderError(f"{url} returned a JSON {type(body).__name__}, expected an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(f"{url} response has no non-empty choices array")
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError(f"{url} response choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderError(f"{url} response choice has no message object")
    content = message.get("content")
    if not isinstance(content, str):
        raise ProviderError(
            f"{url} response message content is {type(content).__name__}, expected str"
        )
    return content
