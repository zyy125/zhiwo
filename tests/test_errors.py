"""异常词汇表的回归测试（M1）。

这里只钉两件事，但都是长期约束：
1. 五个类都在 ``ZhiwoError`` 之下——上层"捕获一切 zhiwo 失败"的写法成立；
2. 消息里的 CJK 值原样穿过 ``str()``，不被转义或损坏（SPEC §7 的
   plain-file transparency 在错误路径上同样要成立）。
"""

import pytest

from zhiwo.errors import (
    LockHeld,
    MergeRefused,
    ProviderError,
    SchemaError,
    ZhiwoError,
)


@pytest.mark.parametrize(
    "exc_type",
    [SchemaError, MergeRefused, LockHeld, ProviderError],
)
def test_every_error_inherits_zhiwo_error(exc_type: type[ZhiwoError]) -> None:
    assert issubclass(exc_type, ZhiwoError)


def test_cjk_payload_survives_str() -> None:
    """错误消息里的中文 id / 原文不得被损坏（不转义、不音译）。"""
    exc = SchemaError("duplicate id f_009 in about/values.md 与 goals/current.md")
    assert "f_009" in str(exc)
    assert "与" in str(exc)
