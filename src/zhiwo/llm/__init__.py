"""模型接入层（L2 的 ``llm/``，plan §4）。

对外只暴露四样东西：协议、假 provider、录制器、回放器。真实网络客户端
（OpenAI-compatible）按 plan 属 M9，M5 不引入 ``httpx`` 依赖。
"""

from zhiwo.llm.base import Provider
from zhiwo.llm.mock import MockProvider
from zhiwo.llm.recording import RecordProvider, ReplayProvider, prompt_sha256

__all__ = [
    "MockProvider",
    "Provider",
    "RecordProvider",
    "ReplayProvider",
    "prompt_sha256",
]
