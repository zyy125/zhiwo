"""M9 的 OpenAI-compatible provider：请求形状、错误翻译与配置矩阵。

这里不联网：``httpx.MockTransport`` 把每个请求变成一条断言，覆盖
plan §7 M9 要钉死的三件事：

- chat-completions 的 URL/鉴权/消息体被正确构造，CJK 原样传输；
- 网络、非 2xx、畸形信封全部翻译成 ``ProviderError``，让 capture 能走
  journal-first 降级；
- 环境变量矩阵把"完全没配"和"配错了"区分开，typo 不会被静默当成离线。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from zhiwo.errors import ProviderError, ZhiwoError
from zhiwo.llm import OpenAICompatibleProvider, provider_from_env
from zhiwo.llm.openai import API_KEY_ENV, BASE_URL_ENV, DEFAULT_BASE_URL, MODEL_ENV

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(
    handler: Handler,
    *,
    base_url: str = "https://llm.example/v1",
    api_key: str | None = "secret",
) -> OpenAICompatibleProvider:
    """带 MockTransport 的 provider；``handler`` 是 httpx 的请求回调。"""
    return OpenAICompatibleProvider(
        model="test-model",
        base_url=base_url,
        api_key=api_key,
        transport=httpx.MockTransport(handler),
    )


def _static_response(body: object) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return handler


def test_complete_posts_a_chat_completion_and_returns_content() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("Content-Type")
        captured["auth"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "你好，世界"}}]})

    provider = _provider(handler)

    assert provider.complete("用中文回答") == "你好，世界"
    assert provider.identity == "openai-compatible:test-model"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["content_type"] == "application/json"
    assert captured["auth"] == "Bearer secret"
    assert captured["payload"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "用中文回答"}],
    }


def test_default_base_url_and_optional_api_key_support_local_endpoints() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    default_provider = OpenAICompatibleProvider(model="m", transport=httpx.MockTransport(handler))
    assert default_provider.complete("p") == "ok"
    assert captured["url"] == f"{DEFAULT_BASE_URL}/chat/completions"
    assert captured["auth"] is None

    local_provider = _provider(
        handler,
        base_url="http://localhost:11434/v1/",
        api_key="",
    )
    assert local_provider.complete("p") == "ok"
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["auth"] is None


def test_http_error_becomes_provider_error_with_status_and_body() -> None:
    provider = _provider(lambda _request: httpx.Response(401, text="invalid api key\n"))

    with pytest.raises(ProviderError, match=r"HTTP 401: invalid api key"):
        provider.complete("prompt")


def test_redirects_count_as_non_2xx_and_are_not_parsed_as_success() -> None:
    provider = _provider(lambda _request: httpx.Response(302, headers={"Location": "/elsewhere"}))

    with pytest.raises(ProviderError, match=r"HTTP 302"):
        provider.complete("prompt")


def test_api_key_never_appears_in_error_messages() -> None:
    secret = "super-secret-token"

    def failing_request(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom for {secret}", request=request)

    providers = [
        (
            _provider(
                lambda _request: httpx.Response(401, text=f"denied for {secret}"), api_key=secret
            ),
            True,
        ),
        (
            _provider(
                lambda _request: httpx.Response(200, text=f"not json {secret}"), api_key=secret
            ),
            False,
        ),
        (_provider(failing_request, api_key=secret), True),
    ]
    for provider, echoed_in_message in providers:
        with pytest.raises(ProviderError) as excinfo:
            provider.complete("prompt")
        message = str(excinfo.value)
        assert secret not in message
        assert ("***" in message) is echoed_in_message


def test_network_error_becomes_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderError, match=r"cannot reach .*connection refused"):
        _provider(handler).complete("prompt")


def test_invalid_json_and_malformed_envelopes_become_provider_errors() -> None:
    invalid_json = _provider(lambda _request: httpx.Response(200, text="not json"))
    with pytest.raises(ProviderError, match=r"HTTP 200 with invalid JSON"):
        invalid_json.complete("prompt")

    envelopes = [
        {},
        {"choices": []},
        {"choices": ["not-an-object"]},
        {"choices": [{}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]},
    ]
    for envelope in envelopes:
        provider = _provider(_static_response(envelope))
        with pytest.raises(ProviderError, match=r"response|choices|message content"):
            provider.complete("prompt")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"model": "  "}, "model"),
        ({"model": "m", "base_url": "  "}, "base_url"),
        ({"model": "m", "base_url": "localhost:11434/v1"}, "absolute http"),
        ({"model": "m", "base_url": "ftp://host/v1"}, "absolute http"),
        ({"model": "m", "base_url": "http:///v1"}, "absolute http"),
        ({"model": "m", "timeout": 0}, "timeout"),
        ({"model": "m", "timeout": -1.0}, "timeout"),
    ],
)
def test_constructor_rejects_invalid_configuration(
    kwargs: Mapping[str, object], message: str
) -> None:
    with pytest.raises(ZhiwoError, match=message):
        OpenAICompatibleProvider(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("environ", "identity"),
    [
        ({"ME_LLM_MODEL": "gpt-test"}, "openai-compatible:gpt-test"),
        (
            {"ME_LLM_MODEL": "  local-model  ", "ME_LLM_BASE_URL": "http://host/v1"},
            "openai-compatible:local-model",
        ),
        (
            {
                "ME_LLM_MODEL": "m",
                "ME_LLM_BASE_URL": "http://host/v1",
                "ME_LLM_API_KEY": "k",
            },
            "openai-compatible:m",
        ),
    ],
)
def test_provider_from_env_builds_a_live_provider(
    environ: Mapping[str, str], identity: str
) -> None:
    provider = provider_from_env(environ)

    assert provider is not None
    assert provider.identity == identity


def test_provider_from_env_returns_none_only_when_everything_is_blank() -> None:
    assert provider_from_env({}) is None
    assert provider_from_env({MODEL_ENV: " ", BASE_URL_ENV: "", API_KEY_ENV: "\t"}) is None


@pytest.mark.parametrize(
    "environ",
    [
        {BASE_URL_ENV: "http://host/v1"},
        {API_KEY_ENV: "secret"},
        {BASE_URL_ENV: "http://host/v1", API_KEY_ENV: "secret"},
    ],
)
def test_partial_environment_configuration_fails_loudly(environ: Mapping[str, str]) -> None:
    with pytest.raises(ZhiwoError, match=MODEL_ENV):
        provider_from_env(environ)


def test_provider_from_env_rejects_a_base_url_without_an_http_scheme() -> None:
    with pytest.raises(ZhiwoError, match="absolute http"):
        provider_from_env({MODEL_ENV: "m", BASE_URL_ENV: "localhost:11434/v1"})
