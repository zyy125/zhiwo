"""Zhiwo (知我) — 本地优先的个人档案，供被授权的 AI 作为上下文阅读。

本包的磁盘契约见仓库根目录 ``SPEC.md``；实现分层见 ``AGENTS.md``。
已落地：L1（schema，M1）、L0（store + factfile，M2）、reconcile
（SPEC §4 的五种观察 + id 空间，M3）、merge
（SPEC §3 的唯一写路径：验证 / 分配 id / 应用 / changelog / 提交，M4）、
extract（L2：prompt + 模型响应解析 + provider 协议/录制回放，M5）、
recall（L4：core/reader 视图、token 估算、journal 检索、逻辑记录与
provenance，M6）、pipeline/CLI（M7：捕获、init/show/why/diff 的编排与
渲染）以及 M8 的 review/export/ask。

公开 API 在这里再导出一次，作为层的门面：后续里程碑（CLI、MCP server）
从 ``zhiwo`` 顶层取用，而不是各自记住 ``zhiwo.models`` /
``zhiwo.schema`` / ``zhiwo.store`` / ``zhiwo.factfile`` / ``zhiwo.merge``
的内部划分。

分层纪律：``factfile`` 只认字符串，``store`` 是唯一碰磁盘的模块，
``models`` 零 I/O——换存储后端只需动 ``store.py``；``reconcile``
是纯扫描（D-006），读档案走 ``store``、改文档走内存、落盘归调用方；
``merge`` 的纯核心同样零 I/O，只有薄壳在调用方持锁时写盘与提交；
``pipeline`` 拥有步骤顺序与锁，``cli`` 只解析参数和渲染结果。
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
from zhiwo.extract import (
    ExtractResult,
    ParsedResponse,
    build_extract_prompt,
    extract,
    parse_extract_response,
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
from zhiwo.llm import MockProvider, Provider, RecordProvider, ReplayProvider
from zhiwo.merge import (
    BudgetEstimator,
    BudgetStatus,
    Capture,
    MalformedDraft,
    MergeConflict,
    MergePlan,
    MergeResult,
    ReviewAction,
    ReviewOutcome,
    ReviewPlan,
    apply_patches,
    apply_review,
    merge_dossier,
    review_dossier,
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
from zhiwo.pipeline import (
    AskContext,
    AskResult,
    CaptureOutcome,
    ConflictPreview,
    DiffResult,
    PendingReview,
    ShowSelection,
    WhyResult,
    answer_question,
    ask,
    build_ask_context,
    build_ask_prompt,
    capture,
    diff,
    explain,
    export_reader,
    init_dossier,
    pending_reviews,
    preview_conflicts,
    review_pending,
    select_records,
)
from zhiwo.recall import (
    FactRecord,
    JournalHit,
    ProvenanceEvent,
    SourceLine,
    estimate_reader_tokens,
    estimate_tokens,
    load_documents,
    logical_records,
    provenance,
    render_core_view,
    render_reader_view,
    search_journal,
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
    JournalEntry,
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
    "AskContext",
    "AskResult",
    "Axis",
    "BudgetEstimator",
    "BudgetStatus",
    "Capture",
    "CaptureOutcome",
    "ChangelogEntry",
    "ChangelogPatch",
    "Clearance",
    "Conflict",
    "ConflictPreview",
    "DiffResult",
    "Dossier",
    "DossierNotFound",
    "ExtractResult",
    "FactDocument",
    "FactKind",
    "FactLine",
    "FactRecord",
    "FactStatus",
    "GitError",
    "JournalAppend",
    "JournalEntry",
    "JournalHit",
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
    "MockProvider",
    "ParsedResponse",
    "PatchOp",
    "PatchResult",
    "PendingReview",
    "Profile",
    "ProvenanceEvent",
    "Provider",
    "ProviderError",
    "QuarantineItem",
    "ReconcileReport",
    "ReconcileScan",
    "RecordProvider",
    "ReplayProvider",
    "Retirement",
    "ReviewAction",
    "ReviewOutcome",
    "ReviewPlan",
    "SchemaError",
    "Section",
    "Sensitivity",
    "ShowSelection",
    "SourceLine",
    "WhyResult",
    "ZhiwoError",
    "__version__",
    "answer_question",
    "apply_patches",
    "apply_review",
    "ask",
    "atomic_write",
    "bootstrap_dossier",
    "build_ask_context",
    "build_ask_prompt",
    "build_extract_prompt",
    "can_read",
    "capture",
    "check_fact_text",
    "diff",
    "estimate_reader_tokens",
    "estimate_tokens",
    "explain",
    "export_reader",
    "extract",
    "git_init",
    "init_dossier",
    "load_documents",
    "load_profile",
    "logical_records",
    "mentioned_fact_ids",
    "mentions_fact_id",
    "merge_dossier",
    "next_fact_id",
    "parse_changelog_entry",
    "parse_extract_response",
    "parse_marker",
    "parse_profile",
    "parse_yaml",
    "pending_reviews",
    "preview_conflicts",
    "provenance",
    "reconcile_documents",
    "reconcile_dossier",
    "render_core_view",
    "render_fact_line",
    "render_marker",
    "render_reader_view",
    "review_dossier",
    "review_pending",
    "search_journal",
    "select_records",
    "template_text",
]
