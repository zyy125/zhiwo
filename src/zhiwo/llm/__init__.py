"""模型接入层（L2 的 ``llm/``，plan §4）。

对外暴露协议、两个离线替身（mock / replay）、录制器，以及 M9 的真实
网络客户端。分层边界不变：真实客户端只把 prompt 变成文本，所有补丁语义
仍归 :mod:`zhiwo.extract` 与 :mod:`zhiwo.merge`。
"""

from zhiwo.llm.base import Provider
from zhiwo.llm.mock import MockProvider
from zhiwo.llm.openai import OpenAICompatibleProvider, provider_from_env
from zhiwo.llm.recording import RecordProvider, ReplayProvider, prompt_sha256

__all__ = [
    "MockProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "RecordProvider",
    "ReplayProvider",
    "prompt_sha256",
    "provider_from_env",
]
