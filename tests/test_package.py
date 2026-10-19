"""包级冒烟测试：保证脚手架可导入、版本号可读。

M1 的第一条提交只有脚手架，这条测试让 CI 从第一天起就真的在验证东西，
而不是对一个空包跑绿灯。

刻意不钉死具体版本号：版本由 pyproject 决定，会在 M9 打 tag 时变；
把它写死只会制造一条"改版本就红"的假警报。
"""

import importlib.metadata

import zhiwo


def test_version_has_a_single_source_of_truth() -> None:
    """``__version__`` 必须就是安装元数据里的版本。

    版本只在 pyproject.toml 里写一次；再在代码里抄一份，两处迟早漂移，
    而 MCP/CLI 会把它当对外报告的版本号——报错了没人会发现。
    """
    assert zhiwo.__version__ == importlib.metadata.version("zhiwo")


def test_public_api_is_importable() -> None:
    """门面再导出可用：后续里程碑从 zhiwo 顶层取用，而非记住内部划分。"""
    assert callable(zhiwo.load_profile)
    assert callable(zhiwo.can_read)
    assert zhiwo.Profile is not None


def test_l0_api_is_re_exported() -> None:
    """M2 的 L0 面同样从顶层取用（M4/M7 不该 import zhiwo.store 的内部划分）。"""
    for name in (
        "Dossier",
        "JournalAppend",
        "FactDocument",
        "FactLine",
        "LineKind",
        "Marker",
        "atomic_write",
        "bootstrap_dossier",
        "git_init",
        "mentions_fact_id",
        "parse_marker",
        "render_fact_line",
        "render_marker",
    ):
        assert name in zhiwo.__all__, name
        assert getattr(zhiwo, name) is not None, name


def test_the_whole_error_vocabulary_is_re_exported() -> None:
    """异常是层与层之间的 API（errors.py 的模块 docstring）：一个都不能漏。"""
    for name in ("ZhiwoError", "SchemaError", "MergeRefused", "LockHeld", "ProviderError"):
        assert name in zhiwo.__all__, name
    # M2 补的三个失败模式
    for name in ("DossierNotFound", "GitError", "MarkerError"):
        assert name in zhiwo.__all__, name


def test_every_export_is_sorted_and_real() -> None:
    """``__all__`` 保持有序（ASCII 序）且每一项都真的存在。

    有序是给人看的（门面同时是 API 清单），而"每一项都存在"是给未来
    的改动看的：删掉一个再导出却忘了删 ``__all__`` 的条目，
    ``from zhiwo import *`` 会在运行时炸，而这条测试在 CI 里先炸。
    """
    assert zhiwo.__all__ == sorted(zhiwo.__all__)
    for name in zhiwo.__all__:
        assert hasattr(zhiwo, name), name
