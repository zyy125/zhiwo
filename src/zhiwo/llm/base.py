"""L2 的模型接入面：一个 provider 只做两件事——报出身份、把 prompt 变成文本。

plan §4 把 ``llm/`` 定为模型调用的唯一边界。这里刻意只定义**一个字符串
进、一个字符串出**的协议，不引入 chat messages、工具调用或 SDK 类型：

- OpenAI / Anthropic 代理 / ollama / vLLM 最终都能映射成"一段 prompt →
  一段文本"，协议越窄，替身与录制器越容易实现；
- extract 的输入输出必须是纯数据（AGENTS.md 的硬规则），provider 不碰
  dossier、不碰文件、不决定补丁语义；
- M9 的 OpenAI-compatible 客户端只需实现 ``complete``，把 prompt 包成
  ``messages`` 是适配器内部的事。

``identity`` 不是装饰：它会原样进入 changelog 的 ``extractor`` 字段
（SPEC §3），是 ``me why`` 回答"这条事实是谁提取的"的唯一来源。因此
身份必须稳定、可区分模型与版本，不能是随机值或空串。
"""

from __future__ import annotations

from typing import Protocol


class Provider(Protocol):
    """模型调用的最小契约（plan §4 / SPEC §3）。

    实现方只需提供：

    - ``identity``：写入 changelog 的提取器标识；
    - ``complete``：同步调用，返回模型生成的原始文本。失败时抛
      :class:`~zhiwo.errors.ProviderError`，不要抛底层网络库的异常——
      上层要能在不 import httpx/openai 的情况下识别"这次提取没成"。

    ``complete`` 的返回值**不做任何信任假设**：它可能是畸形 JSON、带
    Markdown fence、甚至空串。解析是 :mod:`zhiwo.extract` 的职责，provider
    只负责传输。
    """

    @property
    def identity(self) -> str: ...

    def complete(self, prompt: str) -> str: ...
