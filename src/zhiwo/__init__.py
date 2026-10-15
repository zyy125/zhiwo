"""Zhiwo (知我) — 本地优先的个人档案，供被授权的 AI 作为上下文阅读。

本包的磁盘契约见仓库根目录 ``SPEC.md``；实现分层见 ``CLAUDE.md``。
M1 阶段只落地 L1（schema）：profile 的加载与校验。

公开 API 在这里再导出一次，作为层的门面：后续里程碑（CLI、MCP server）
从 ``zhiwo`` 顶层取用，而不是各自记住 ``zhiwo.models`` /
``zhiwo.schema`` 的内部划分。
"""

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

__version__ = "0.2.0.dev0"

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
