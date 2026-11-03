"""可编程的假 provider：M5/M7 的离线测试入口。

它替代的不是"模型"这个黑盒，而是"一次确定的模型响应"：给固定字符串，
或用 callable 按 prompt 生成。所有收到的 prompt 都留在 ``prompts`` 里，
测试因此可以断言"prompt 真的包含 core view / 行号 / 目标分区"，而不是
只断言最终解析结果。

``identity`` 默认 ``mock-v0``，与 M4 测试里沿用的 ``extractor`` 值一致；
它让测试产出的 changelog 看起来像一条正常的审计记录，而不是一个特例。
"""

from __future__ import annotations

from collections.abc import Callable

from zhiwo.errors import ProviderError, ZhiwoError

ResponseSource = str | Callable[[str], str]


class MockProvider:
    """固定响应或按 prompt 计算的响应；同时记录每次调用。"""

    def __init__(self, response: ResponseSource, *, identity: str = "mock-v0") -> None:
        if not identity:
            raise ZhiwoError("provider identity must be a non-empty string")
        self._response = response
        self._identity = identity
        self._prompts: list[str] = []

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def prompts(self) -> tuple[str, ...]:
        """按调用顺序返回收到的 prompt（只读快照，避免测试改内部状态）。"""
        return tuple(self._prompts)

    def complete(self, prompt: str) -> str:
        self._prompts.append(prompt)
        response = self._response(prompt) if callable(self._response) else self._response
        if not isinstance(response, str):
            # 协议写的是 str；实现返回别的类型是 provider 自己的 bug。
            # 在这里转成词汇表里的失败，避免 extract 把非字符串当成
            # "模型给了个奇怪响应"继续往下走。
            raise ProviderError(
                f"provider {self._identity!r} returned {type(response).__name__}, expected str"
            )
        return response
