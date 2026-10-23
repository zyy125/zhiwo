"""Zhiwo (知我) — 本地优先的个人档案，供被授权的 AI 作为上下文阅读。

本包的磁盘契约见仓库根目录 ``SPEC.md``；实现分层见 ``AGENTS.md``。
已落地：L1（schema，M1）、L0（store + factfile，M2）、reconcile
（SPEC §4 的五种观察 + id 空间，M3）与 merge
（SPEC §3 的唯一写路径：验证 / 分配 id / 应用 / changelog / 提交，M4）。

公开 API 在这里再导出一次，作为层的门面：后续里程碑（CLI、MCP server）
从 ``zhiwo`` 顶层取用，而不是各自记住 ``zhiwo.models`` /
``zhiwo.schema`` / ``zhiwo.store`` / ``zhiwo.factfile`` / ``zhiwo.merge``
的内部划分。

分层纪律：``factfile`` 只认字符串，``store`` 是唯一碰磁盘的模块，
``models`` 零 I/O——换存储后端只需动 ``store.py``；``reconcile``
是纯扫描（D-006），读档案走 ``store``、改文档走内存、落盘归调用方；
``merge`` 的纯核心同样零 I/O，只有薄壳在调用方持锁时写盘与提交。
"""

import importlib.metadata

from zhiwo.errors import (
    DossierNotFound,
    GitError,
    LockHeld,
    MarkerError,
    MergeRefused,
    ProviderError,
    SchemaError,
    ZhiwoError,
)
from zhiwo.factfile import (
    FactDocument,
    FactLine,
    LineKind,
    Marker,
    check_fact_text,
    mentioned_fact_ids,
    mentions_fact_id,
    parse_marker,
    render_fact_line,
    render_marker,
)
from zhiwo.merge import (
    BudgetEstimator,
    BudgetStatus,
    Capture,
    MalformedDraft,
    MergeConflict,
    MergePlan,
    MergeResult,
    apply_patches,
    merge_dossier,
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
from zhiwo.reconcile import (
    Adoption,
    Conflict,
    Location,
    MarkerWarning,
    QuarantineItem,
    ReconcileReport,
    ReconcileScan,
    Retirement,
    next_fact_id,
    reconcile_documents,
    reconcile_dossier,
)
from zhiwo.schema import (
    can_read,
    load_profile,
    parse_changelog_entry,
    parse_profile,
    parse_yaml,
    template_text,
)
from zhiwo.store import (
    Dossier,
    JournalAppend,
    atomic_write,
    bootstrap_dossier,
    git_init,
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
    "Adoption",
    "Axis",
    "BudgetEstimator",
    "BudgetStatus",
    "Capture",
    "ChangelogEntry",
    "ChangelogPatch",
    "Clearance",
    "Conflict",
    "Dossier",
    "DossierNotFound",
    "FactDocument",
    "FactKind",
    "FactLine",
    "FactStatus",
    "GitError",
    "JournalAppend",
    "LineKind",
    "Location",
    "LockHeld",
    "MalformedDraft",
    "Marker",
    "MarkerError",
    "MarkerWarning",
    "MergeConflict",
    "MergePlan",
    "MergeRefused",
    "MergeResult",
    "PatchOp",
    "PatchResult",
    "Profile",
    "ProviderError",
    "QuarantineItem",
    "ReconcileReport",
    "ReconcileScan",
    "Retirement",
    "SchemaError",
    "Section",
    "Sensitivity",
    "ZhiwoError",
    "__version__",
    "apply_patches",
    "atomic_write",
    "bootstrap_dossier",
    "can_read",
    "check_fact_text",
    "git_init",
    "load_profile",
    "mentioned_fact_ids",
    "mentions_fact_id",
    "merge_dossier",
    "next_fact_id",
    "parse_changelog_entry",
    "parse_marker",
    "parse_profile",
    "parse_yaml",
    "reconcile_documents",
    "reconcile_dossier",
    "render_fact_line",
    "render_marker",
    "template_text",
]
