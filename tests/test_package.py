"""包级冒烟测试：保证脚手架可导入、版本号可读。

M1 的第一条提交只有脚手架，这条测试让 CI 从第一天起就真的在验证东西，
而不是对一个空包跑绿灯。
"""

import zhiwo


def test_version_is_exposed() -> None:
    assert zhiwo.__version__ == "0.2.0.dev0"
