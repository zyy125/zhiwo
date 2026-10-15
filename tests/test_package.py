"""包级冒烟测试：保证脚手架可导入、版本号可读。

M1 的第一条提交只有脚手架，这条测试让 CI 从第一天起就真的在验证东西，
而不是对一个空包跑绿灯。

刻意不钉死具体版本号：版本由 pyproject 决定，会在 M9 打 tag 时变；
把它写死只会制造一条"改版本就红"的假警报。
"""

import re

import zhiwo


def test_version_is_exposed() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", zhiwo.__version__), zhiwo.__version__


def test_public_api_is_importable() -> None:
    """门面再导出可用：后续里程碑从 zhiwo 顶层取用，而非记住内部划分。"""
    assert callable(zhiwo.load_profile)
    assert callable(zhiwo.can_read)
    assert zhiwo.Profile is not None
