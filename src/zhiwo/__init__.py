"""Zhiwo (知我) — 本地优先的个人档案，供被授权的 AI 作为上下文阅读。

本包的磁盘契约见仓库根目录 ``SPEC.md``；实现分层见 ``CLAUDE.md``。
M1 阶段只落地 L1（schema）：profile 的加载与校验。

公开 API 在这里再导出一次，作为层的门面：后续里程碑（CLI、MCP server）
从 ``zhiwo`` 顶层取用，而不是各自记住 ``zhiwo.models`` /
``zhiwo.schema`` 的内部划分。
"""

import importlib.metadata

from zhiwo.errors import (
    LockHeld,
    MergeRefused,
    ProviderError,
    SchemaError,
    ZhiwoError,
)
from zhiwo.models import (
    Axis,
    ChangelogEntry,
    ChangelogPatch,
    Clearance,
    FactKind,
    FactStatus,
    PatchOp,
    PatchResult,
    Profile,
    Section,
    Sensitivity,
)
from zhiwo.schema import (
    can_read,
    load_profile,
    parse_changelog_entry,
    parse_profile,
    parse_yaml,
    template_text,
)

# 版本号只有一个来源：pyproject.toml（经安装元数据读出）。
# 刻意不在这里再写一个字面量——两处版本迟早会漂移，而 MCP/CLI 会把它
# 当成对外报告的版本，报错了没人会发现。未安装（直接跑源码树）时给出
# 一个明显非发布版的占位值，而不是假装知道版本。
try:
    __version__ = importlib.metadata.version("zhiwo")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"

__all__ = [
    "Axis",
    "ChangelogEntry",
    "ChangelogPatch",
    "Clearance",
    "FactKind",
    "FactStatus",
    "LockHeld",
    "MergeRefused",
    "PatchOp",
    "PatchResult",
    "Profile",
    "ProviderError",
    "SchemaError",
    "Section",
    "Sensitivity",
    "ZhiwoError",
    "__version__",
    "can_read",
    "load_profile",
    "parse_changelog_entry",
    "parse_profile",
    "parse_yaml",
    "template_text",
]
