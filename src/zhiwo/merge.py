"""L3：merge——补丁从"模型说的话"变成"档案里的事实"的唯一写入口。

本模块实现 plan §5 的第 6–11 步：逐条验证 extract 的补丁草稿、分配 id、
改写内存文档、写 changelog、做一次 git 提交，并把预算检查留成可注入的钩子。
它不调用模型、不渲染 CLI；"LLM 调用点是唯一的裂缝"在这里体现为：
输入是纯数据（草稿映射 + 捕获区间），输出也是纯数据（报告与补丁记录）。

两层 API（与 reconcile 同构）：

- **纯核心** :func:`apply_patches`：零磁盘 I/O、零模型调用、确定性。它只
  通过 :meth:`~zhiwo.factfile.FactLine.rewrite` /
  :meth:`~zhiwo.factfile.FactDocument.append_fact` 改写调用方交来的内存文档，
  一次验证 + 应用一遍完成。被拒的草稿不留痕迹；被接受的草稿立即改变
  后续草稿看到的状态（两条 statement supersede 同一目标时，第二条会被
  正确拒绝）。
- **薄壳** :func:`merge_dossier`：读回 journal 区间（证据的 haystack 必须是
  盘上字节，而不是 extractor 手里的副本）、跑纯核心、写回全部文档、写一条
  changelog、提交。它**假定调用方已持有** :meth:`~zhiwo.store.Dossier.writer_lock`
  ——plan §5 的第 1–5 步（锁、journal 追加、reconcile、extract）与这里必须在
  同一个锁窗口内完成，因此薄壳不能再嵌套获取锁。

写序与崩溃语义（plan §5）：没有事务。写序固定为"文档 → changelog → commit"。
提交失败时文件与 changelog 已在盘上，抛 :class:`~zhiwo.errors.GitError`，
**不回滚**——journal 永不被回滚，下一次提交会带上这些改动；半合并的恢复是
选择性的 ``git checkout -- about/ goals/``。

M8 的 review 三条分支（confirm/edit/reject）也在这里：纯核心
:func:`apply_review` 负责语义，薄壳 :func:`review_dossier` 复用
:func:`_write_merge` 的写盘/提交尾段。add/supersede/link 之外的操作
（compress/adopt/retire 以及 review-only op）若由 extractor 草稿发出，
一律拒绝并记录——模型不能替人做 review 决定。
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Mapping, Sequence, Set
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import cast

from zhiwo.errors import MarkerError, MergeRefused, ZhiwoError
from zhiwo.factfile import (
    _FACT_ID_RE,
    FactDocument,
    FactLine,
    Marker,
    _line_ending,
    check_fact_text,
    render_fact_line,
)
from zhiwo.models import (
    ChangelogEntry,
    ChangelogPatch,
    FactKind,
    FactStatus,
    PatchOp,
    PatchResult,
    Section,
)
from zhiwo.reconcile import ReconcileReport, ReconcileScan, next_fact_id
from zhiwo.store import Dossier, JournalAppend

# 接受的写操作（v0.2）。compress 属 distill（v0.5）；adopt/retire 属
# reconcile（M3），extract 不得发射——它们会被拒绝并记录，而不是崩溃。
_APPLICABLE_OPS = frozenset({PatchOp.ADD, PatchOp.SUPERSEDE, PatchOp.LINK})

# review 合并没有捕获输入，也没有模型参与。changelog 的 input/extractor
# 字段仍必须如实、非空（SPEC §3），因此用稳定的字面量说明"这是人工裁决"，
# 而不是伪造一个 journal 路径或 extractor 身份（D-011）。
_REVIEW_INPUT = "review"
_REVIEW_EXTRACTOR = "human"

# 证据匹配的空白折叠：Python 的 \s 覆盖 CJK 全角空格（U+3000）等 Unicode
# 空白；折叠后两端再 strip。R1 说严格度是实现旋钮，这里钉住"空白折叠
# 包含"，由 evidence 矩阵测试固定行为，M9 用真实拒绝率再调。
_WHITESPACE_RE = re.compile(r"\s+")

# 拒绝原因里引用违规值时的截断长度：日志是给人读的，不是模型输出的
# 第二份副本；超长值只保留开头。
_REASON_VALUE_LIMIT = 120


@dataclass(frozen=True, slots=True)
class Capture:
    """一次捕获在 journal 里的位置（plan §5 步骤 2 的产物）。

    ``path`` 是**相对档案根目录的 POSIX 路径**（如
    ``journal/2026-10-11.md``），与 changelog 的 ``input`` / ``source``
    同一写法；``first_line``/``last_line`` 是落盘后的 1-based 绝对行号，
    与 ``#L<n>`` 同一坐标系。三个值都由 :class:`~zhiwo.store.JournalAppend`
    而来，手写构造只应出现在测试里。
    """

    path: str
    first_line: int
    last_line: int

    def __post_init__(self) -> None:
        candidate = PurePosixPath(self.path)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in candidate.parts)
            # PurePosixPath 会静默规整 "./"、多余斜杠；source 指针是可读的
            # 契约文本，要求逐字规范，避免两个写法指向同一行却看起来不同。
            or candidate.as_posix() != self.path
        ):
            raise ZhiwoError(
                f"capture path must be a canonical relative POSIX path inside "
                f"the journal directory: {self.path!r}"
            )
        if self.first_line < 1 or self.last_line < self.first_line:
            raise ZhiwoError(
                f"invalid capture range L{self.first_line}-L{self.last_line} "
                "(expected 1 <= first <= last)"
            )

    @classmethod
    def from_journal_append(cls, dossier: Dossier, append: JournalAppend) -> Capture:
        """从 store 的追加结果构造捕获区间。

        ``JournalAppend`` 给的是绝对路径；changelog 需要相对路径。路径在
        档案根目录之外是调用方 bug（可能是另一个档案的 journal），必须
        响亮失败，而不是写出一条指向别处的 source。
        """
        root = dossier.root.resolve()
        try:
            relative = append.path.resolve().relative_to(root)
        except ValueError as exc:
            raise ZhiwoError(
                f"journal append path is outside the dossier: {append.path} (dossier: {root})"
            ) from exc
        return cls(
            path=relative.as_posix(),
            first_line=append.first_line,
            last_line=append.last_line,
        )


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """一次 merge 后的核心档案预算状态（plan §3 决策 5 / SPEC §6）。

    ``tokens`` 是**高清除度 reader view** 的估算值（最大视图；pending、
    superseded、archived 不计）。比较是严格的 ``>``：等于预算不算超。
    MVP 阶段它只是数据——不阻塞、不延迟，CLI 负责把 ``over`` 渲染成
    D-002 的响亮警告。
    """

    tokens: int
    limit: int

    @property
    def over(self) -> bool:
        return self.tokens > self.limit


# 预算估算器：post-merge 文档 → 高清除度 reader view 的 token 数。
# M6 实现真正的估算（CJK ×1 + ceil(other/4) 的启发式）；M4 只消费。
BudgetEstimator = Callable[[Sequence[tuple[Section, FactDocument]]], int]


@dataclass(frozen=True, slots=True)
class MalformedDraft:
    """一条连 op 都认不出的草稿（只能报告，不能进 changelog）。

    changelog 的 ``op`` 是封闭词汇（SPEC §3/§4）；没有 op 就没有可记录的
    补丁形态。这类输入不静默丢弃：它进 ``MergeResult.malformed``，CLI
    必须打出来（M5 本就不该产出它）。
    """

    index: int  # 草稿在输入列表里的 0-based 下标
    detail: str


@dataclass(frozen=True, slots=True)
class MergeConflict:
    """锁定事实的冲突旗标（SPEC §4 第 5 案 / 硬规则 5）。

    系统绝不自动改写 locked 事实，也绝不自动 supersede。这里只把"哪条
    草稿撞了哪条锁定事实"交给 ``me review``；拒绝本身已进 changelog。
    """

    draft_index: int
    fact_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class MergePlan:
    """纯核心的全部产出：改写后的文档 + 可记录的补丁 + 报告数据。

    ``documents`` 引用的是调用方交来的、已被就地改写的内存文档；这里不再
    复制它们（复制反而会让"写回的到底是不是被验证过的那份"变得含糊）。
    ``patches`` 只含草稿来源的记录（reconcile 的 adopt/retire 由调用方
    拼在它们前面），顺序即输入顺序。
    """

    documents: tuple[tuple[Section, FactDocument], ...]
    patches: tuple[ChangelogPatch, ...]
    malformed: tuple[MalformedDraft, ...]
    conflicts: tuple[MergeConflict, ...]
    assigned_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """薄壳的产出：把"这次 merge 发生了什么"完整交给 CLI（M7）。

    字段与 plan §5 步骤 12 的摘要一一对应：applied / awaiting review /
    rejected with reasons / conflicts / budget。``patches`` 只含草稿来源；
    reconcile 的收编/退休在 ``reconcile`` 里。changelog 是完整账本，
    这里只是本次调用的快照。
    """

    merge_id: str
    changelog_path: Path
    commit: str
    patches: tuple[ChangelogPatch, ...]
    reconcile: ReconcileReport
    malformed: tuple[MalformedDraft, ...]
    conflicts: tuple[MergeConflict, ...]
    budget: BudgetStatus | None

    @property
    def applied(self) -> tuple[ChangelogPatch, ...]:
        return tuple(patch for patch in self.patches if _is_applied(patch))

    @property
    def rejected(self) -> tuple[ChangelogPatch, ...]:
        return tuple(patch for patch in self.patches if patch.result is PatchResult.REJECTED)

    @property
    def skipped(self) -> tuple[ChangelogPatch, ...]:
        return tuple(patch for patch in self.patches if patch.result is PatchResult.SKIPPED)

    @property
    def pending_count(self) -> int:
        """本次 merge 新增的待确认事实数（CLI 的 review 提示用）。"""
        return sum(
            1
            for patch in self.applied
            if patch.kind is FactKind.INFERENCE and patch.op in (PatchOp.ADD, PatchOp.SUPERSEDE)
        )


class ReviewAction(enum.StrEnum):
    """人对 pending 事实的三种裁决（M8 / SPEC §2.3）。

    取值与 changelog op 同名，让"用户按了什么"与"账本写了什么"之间
    没有翻译层；D-011 记录了这一词汇扩展。
    """

    CONFIRM = "confirm"
    EDIT = "edit"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """纯核心的 review 结果：改写后的文档 + 一条待记录的事件。

    ``patch`` 为 ``None`` 表示这次没有做出可写入的决定，只有一种情况：
    延迟 supersede 的目标已经变化，需要用户重新选择（OQ#4）。此时
    ``target_changed`` 为真，``target_id`` 指向那个已经不能应用的目标；
    ``documents`` 保持调用方交来的快照（深拷贝后未改动）。
    """

    documents: tuple[tuple[Section, FactDocument], ...]
    patch: ChangelogPatch | None
    target_changed: bool = False
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """薄壳的 review 结果：写入时带完整 ``MergeResult``，否则说明原因。

    与 :class:`MergeResult` 分开，是因为"目标已变化、需要再问一次"不是
    一次 merge，也不应产生 changelog 或 commit；把它伪装成一次空 merge
    会让调用方误以为磁盘上已经发生过一次决定。
    """

    fact_id: str
    action: ReviewAction
    merge: MergeResult | None
    target_changed: bool = False
    target_id: str | None = None


@dataclass(slots=True)
class _LocatedFact:
    """事实索引里的一项：它属于哪个分区/文档，以及它的行对象。

    ``line`` 持有的是**同一个** ``FactLine`` 引用；后续 statement supersede
    或 link 就地改写它之后，索引不需要刷新（状态与文本都从行对象现读）。
    """

    section: Section
    document: FactDocument
    line: FactLine


# --- 纯核心：验证 + 应用 ----------------------------------------------------


def apply_patches(
    documents: Sequence[tuple[Section, FactDocument]],
    drafts: Sequence[object],
    *,
    capture: Capture,
    journal_lines: Sequence[str],
    known_ids: Set[str],
) -> MergePlan:
    """逐条验证并应用补丁草稿，返回内存态的计划（不碰磁盘）。

    ``documents`` 必须是 **reconcile 之后**的文档（收编已就地改写）；本函数
    会再做一次重复 id 检查作为防御——被拒的整次 merge 不留任何痕迹。

    ``drafts`` 是 extract 的原始映射列表（未校验的模型输出）。草稿里的
    ``id`` / ``source`` / ``result`` / ``reason`` 一律忽略：身份与 provenance
    只能由这里产生（SPEC §3）。

    验证与应用是**同一次顺序遍历**：被接受的草稿立即改变后续草稿看到的
    状态。这样"两条 statement supersede 同一目标"时第二条会对上新状态被
    拒，而不是两条都基于旧快照通过、再写出互相矛盾的 marker。

    ``known_ids`` 来自 :attr:`~zhiwo.reconcile.ReconcileScan.known_ids`
    （收编后的保守天花板）；本函数复制它，并在接受补丁时追加新 id。
    """
    _require_journal_lines(capture, journal_lines)

    document_list = list(documents)
    sections_by_path = {section.path: (section, document) for section, document in document_list}
    facts_by_id = _index_facts(document_list)

    known = set(known_ids)
    patches: list[ChangelogPatch] = []
    malformed: list[MalformedDraft] = []
    conflicts: list[MergeConflict] = []
    assigned: list[str] = []

    for index, raw in enumerate(drafts):
        draft = _as_draft_mapping(raw)
        if draft is None:
            malformed.append(MalformedDraft(index, "draft is not a mapping"))
            continue
        op = _parse_op(draft.get("op"))
        if op is None:
            malformed.append(MalformedDraft(index, f"unrecognised op {_brief(draft.get('op'))}"))
            continue
        if op not in _APPLICABLE_OPS:
            patches.append(_rejected_patch(index, op, draft, _unsupported_reason(op)))
            continue

        if op is PatchOp.ADD:
            patch = _apply_add(
                index,
                draft,
                sections_by_path=sections_by_path,
                facts_by_id=facts_by_id,
                capture=capture,
                journal_lines=journal_lines,
                known=known,
                assigned=assigned,
            )
        elif op is PatchOp.SUPERSEDE:
            patch = _apply_supersede(
                index,
                draft,
                facts_by_id=facts_by_id,
                conflicts=conflicts,
                capture=capture,
                journal_lines=journal_lines,
                known=known,
                assigned=assigned,
            )
        else:  # LINK
            patch = _apply_link(
                index,
                draft,
                facts_by_id=facts_by_id,
                conflicts=conflicts,
            )
        patches.append(patch)

    return MergePlan(
        documents=tuple(document_list),
        patches=tuple(patches),
        malformed=tuple(malformed),
        conflicts=tuple(conflicts),
        assigned_ids=tuple(assigned),
    )


def apply_review(
    documents: Sequence[tuple[Section, FactDocument]],
    *,
    fact_id: str,
    action: ReviewAction,
    text: str | None = None,
    standalone: bool = False,
) -> ReviewPlan:
    """在内存文档上执行一个人工 review 决定（纯核心，零 I/O）。

    语义（SPEC §2.3 / plan §3 决策 9 / D-011）：

    - ``confirm``：pending → active。若事实带延迟 ``supersedes`` 边且目标
      仍是 active、未 locked、可合法重写，则同时把目标划掉并写入
      ``superseded-by``。目标变化时返回 ``target_changed``，绝不把边
      应用到一条已经前进或被人锁定的事实上（OQ#4：first confirmation wins）。
    - ``edit``：人工改写是新的断言，因此事实转为 ``locked`` 并丢弃延迟边；
      目标事实绝不因一次措辞修改而被自动取代。
    - ``reject``：行从文件移除，事件由 changelog 记录（墓碑）；目标不动。

    所有改行都先预渲染再落地：一次失败的 review 不应留下"新行写了一半、
    目标已经被划掉"的半状态。文档同样是调用方交来的快照（薄壳负责深拷贝），
    纯核心本身只经 :meth:`~zhiwo.factfile.FactLine.rewrite` 改行。
    """
    document_list = list(documents)
    facts_by_id = _index_facts(document_list)
    located = facts_by_id.get(fact_id)
    if located is None:
        raise ZhiwoError(f"no fact {fact_id!r} in any fact file (unknown or archived)")

    marker = located.line.marker
    assert marker is not None  # _index_facts 只收 FACT 行
    if marker.status is not FactStatus.PENDING:
        raise ZhiwoError(
            f"fact {fact_id} is {marker.status.value}, not pending; "
            "only pending facts can be reviewed"
        )

    fact_text = located.line.text
    assert fact_text is not None  # FACT 行不变量

    if action is ReviewAction.CONFIRM:
        return _confirm_review(
            document_list,
            facts_by_id=facts_by_id,
            located=located,
            fact_text=fact_text,
            fact_id=fact_id,
            marker=marker,
            standalone=standalone,
        )
    if action is ReviewAction.EDIT:
        return _edit_review(
            document_list,
            located,
            fact_id=fact_id,
            marker=marker,
            text=text,
        )
    return _reject_review(document_list, located, fact_text=fact_text, fact_id=fact_id)


def _confirm_review(
    documents: list[tuple[Section, FactDocument]],
    *,
    facts_by_id: Mapping[str, _LocatedFact],
    located: _LocatedFact,
    fact_text: str,
    fact_id: str,
    marker: Marker,
    standalone: bool,
) -> ReviewPlan:
    """确认一条 pending 事实；延迟边是否应用由目标当前状态决定。

    ``standalone`` 只在目标已变化时使用：用户选择"仅确认新事实"后，
    延迟边被丢弃。目标仍然 active 时，这个参数没有意义——那正是延迟
    supersede 应当生效的时刻（SPEC §2.3 的 deferred supersede）。
    """
    target_id = marker.supersedes
    target = facts_by_id.get(target_id) if target_id is not None else None
    edge_blocked = target_id is not None and not _edge_applicable(target)

    if edge_blocked and not standalone:
        return ReviewPlan(
            documents=tuple(documents),
            patch=None,
            target_changed=True,
            target_id=target_id,
        )

    reason: str | None = None
    new_marker = replace(marker, pending=False)
    if edge_blocked:
        # 目标已经前进/被锁/不可重写：standalone confirm 丢弃惰性边，
        # 避免盘上出现一条指向"已经不该被取代"的目标的 active supersedes。
        new_marker = replace(marker, pending=False, supersedes=None)
        reason = (
            f"target {target_id} was no longer active or writable; "
            "confirmed standalone without applying the deferred edge"
        )
    elif target is not None and target_id is not None:
        target_marker = target.line.marker
        target_text = target.line.text
        assert target_marker is not None and target_text is not None
        # 两行都先渲染成功，再动任何一行（与 _apply_supersede 同一顺序）。
        render_fact_line(
            target_text,
            replace(target_marker, superseded_by=fact_id),
            struck=True,
            newline=_line_ending(target.line.raw),
        )
        render_fact_line(fact_text, new_marker, newline=located.document.newline)
        target.line.rewrite(
            target_text,
            replace(target_marker, superseded_by=fact_id),
            struck=True,
        )

    located.line.rewrite(fact_text, new_marker, struck=False)
    return ReviewPlan(
        documents=tuple(documents),
        patch=ChangelogPatch(
            op=PatchOp.CONFIRM,
            id=fact_id,
            target_id=target_id if edge_blocked is False and target_id is not None else None,
            reason=reason,
        ),
    )


def _edit_review(
    documents: list[tuple[Section, FactDocument]],
    located: _LocatedFact,
    *,
    fact_id: str,
    marker: Marker,
    text: str | None,
) -> ReviewPlan:
    """把 pending 事实改写为人工锁定事实，并丢弃延迟边（D-011）。"""
    if text is None or not text:
        raise ZhiwoError(f"editing {fact_id} needs non-empty replacement text")
    try:
        check_fact_text(text)
    except MarkerError as exc:
        raise ZhiwoError(f"edited text would break the line grammar: {_clip(str(exc))}") from exc

    new_marker = replace(marker, pending=False, locked=True, supersedes=None)
    # 先渲染再落地：非法文本已经由 check_fact_text 拦住，这里仍保留
    # 同一防御顺序，让"改写失败"不可能留下半行。
    render_fact_line(text, new_marker, newline=_line_ending(located.line.raw))
    located.line.rewrite(text, new_marker, struck=False)

    reason: str | None = None
    if marker.supersedes is not None:
        reason = (
            f"deferred edge to {marker.supersedes} dropped: an edited fact is a new "
            "human assertion, not a confirmation of the old inference"
        )
    return ReviewPlan(
        documents=tuple(documents),
        patch=ChangelogPatch(
            op=PatchOp.EDIT,
            id=fact_id,
            text=text,
            reason=reason,
        ),
    )


def _reject_review(
    documents: list[tuple[Section, FactDocument]],
    located: _LocatedFact,
    *,
    fact_text: str,
    fact_id: str,
) -> ReviewPlan:
    """拒绝 pending 事实：移除行，changelog 记录墓碑，目标不动。"""
    located.document.lines.remove(located.line)
    return ReviewPlan(
        documents=tuple(documents),
        patch=ChangelogPatch(
            op=PatchOp.REJECT,
            id=fact_id,
            text=fact_text,
            reason=f"review: rejected pending fact {fact_id}",
        ),
    )


def _edge_applicable(target: _LocatedFact | None) -> bool:
    """延迟 supersede 的目标当前是否仍可应用。

    四个条件都是"不应用"的边界：目标不存在（已归档/手删）、不是 active
    （已被别的确认抢先后又前进）、被 locked（硬规则 5：永不改写人工事实）、
    或目标文本不可合法重渲染（改写会产出一行坏文法）。任何一条成立都
    交给用户重新选择，绝不猜。
    """
    if target is None:
        return False
    marker = target.line.marker
    if marker is None or marker.status is not FactStatus.ACTIVE or marker.locked:
        return False
    text = target.line.text
    if text is None:
        return False
    try:
        check_fact_text(text)
    except MarkerError:
        return False
    return True


def _apply_add(
    index: int,
    draft: Mapping[str, object],
    *,
    sections_by_path: Mapping[str, tuple[Section, FactDocument]],
    facts_by_id: dict[str, _LocatedFact],
    capture: Capture,
    journal_lines: Sequence[str],
    known: set[str],
    assigned: list[str],
) -> ChangelogPatch:
    """``add``：向声明的分区文件末尾追加一条新事实。"""
    target = _string(draft, "target")
    if target is None:
        return _rejected_patch(index, PatchOp.ADD, draft, "target is missing or empty")
    target_pair = sections_by_path.get(target)
    if target_pair is None:
        return _rejected_patch(
            index,
            PatchOp.ADD,
            draft,
            f"target {target!r} is not a declared about/goals section path",
        )

    kind = _parse_kind(draft.get("kind"))
    if kind is None:
        return _rejected_patch(index, PatchOp.ADD, draft, "kind must be 'statement' or 'inference'")
    text = _string(draft, "text")
    if text is None:
        return _rejected_patch(index, PatchOp.ADD, draft, "text is missing or empty")
    try:
        check_fact_text(text)
    except MarkerError as exc:
        return _rejected_patch(
            index, PatchOp.ADD, draft, f"text would break the line grammar: {_clip(str(exc))}"
        )

    evidence = _string(draft, "evidence")
    if evidence is None:
        return _rejected_patch(index, PatchOp.ADD, draft, "evidence is missing or empty")
    source = _match_evidence(evidence, capture, journal_lines)
    if source is None:
        return _rejected_patch(index, PatchOp.ADD, draft, _EVIDENCE_MISMATCH)

    new_id = _next_id(known, facts_by_id)
    section, document = target_pair
    marker = Marker(fact_id=new_id, pending=kind is FactKind.INFERENCE)
    line = document.append_fact(text, marker)
    facts_by_id[new_id] = _LocatedFact(section=section, document=document, line=line)
    known.add(new_id)
    assigned.append(new_id)
    return ChangelogPatch(
        op=PatchOp.ADD,
        id=new_id,
        target=target,
        kind=kind,
        text=text,
        evidence=evidence,
        source=source,
    )


def _apply_supersede(
    index: int,
    draft: Mapping[str, object],
    *,
    facts_by_id: dict[str, _LocatedFact],
    conflicts: list[MergeConflict],
    capture: Capture,
    journal_lines: Sequence[str],
    known: set[str],
    assigned: list[str],
) -> ChangelogPatch:
    """``supersede``：statement 立即改写目标并追加新事实；inference 延迟。"""
    target_id = _fact_id(draft.get("target_id"))
    if target_id is None:
        return _rejected_patch(
            index,
            PatchOp.SUPERSEDE,
            draft,
            "target_id is missing or not a valid fact id",
        )
    target = facts_by_id.get(target_id)
    if target is None:
        return _rejected_patch(
            index,
            PatchOp.SUPERSEDE,
            draft,
            f"target fact {target_id} does not exist in any fact file (unknown or archived)",
        )

    marker = target.line.marker
    assert marker is not None  # facts() 的不变量
    if marker.status is not FactStatus.ACTIVE:
        return _rejected_patch(
            index,
            PatchOp.SUPERSEDE,
            draft,
            f"target fact {target_id} is {marker.status.value}; supersede requires active",
        )
    if marker.locked:
        conflicts.append(
            MergeConflict(
                draft_index=index,
                fact_id=target_id,
                detail=(
                    f"draft {index} tried to supersede locked fact {target_id}; "
                    "the system never rewrites a locked fact (SPEC §4/§7)"
                ),
            )
        )
        return _rejected_patch(
            index,
            PatchOp.SUPERSEDE,
            draft,
            f"target fact {target_id} is locked; locked facts are never rewritten",
        )

    kind = _parse_kind(draft.get("kind"))
    if kind is None:
        return _rejected_patch(
            index, PatchOp.SUPERSEDE, draft, "kind must be 'statement' or 'inference'"
        )
    text = _string(draft, "text")
    if text is None:
        return _rejected_patch(index, PatchOp.SUPERSEDE, draft, "text is missing or empty")
    try:
        check_fact_text(text)
    except MarkerError as exc:
        return _rejected_patch(
            index,
            PatchOp.SUPERSEDE,
            draft,
            f"text would break the line grammar: {_clip(str(exc))}",
        )

    target_text = target.line.text
    assert target_text is not None  # FACT 行不变量
    try:
        # 目标文本自身必须可重渲染：一条解析得出来、却渲染不回去的行
        # （例如文本内部含 ~~）无法被合法 supersede；在分配 id 之前拒绝，
        # 避免"目标改了一半、新行写不出"的半状态。
        check_fact_text(target_text)
    except MarkerError as exc:
        return _rejected_patch(
            index,
            PatchOp.SUPERSEDE,
            draft,
            f"target fact {target_id} cannot be rewritten without breaking the line grammar: "
            f"{_clip(str(exc))}",
        )

    evidence = _string(draft, "evidence")
    if evidence is None:
        return _rejected_patch(index, PatchOp.SUPERSEDE, draft, "evidence is missing or empty")
    source = _match_evidence(evidence, capture, journal_lines)
    if source is None:
        return _rejected_patch(index, PatchOp.SUPERSEDE, draft, _EVIDENCE_MISMATCH)

    new_id = _next_id(known, facts_by_id)
    new_marker = Marker(
        fact_id=new_id,
        supersedes=target_id,
        pending=kind is FactKind.INFERENCE,
    )
    # 先把两行都渲染一遍再动手：虽然 check_fact_text 已经覆盖了文本，
    # 这里仍确保"新行渲染成功"与"目标改写渲染成功"都在任何内存改写之前，
    # 避免 append 失败留下一个已被划掉的目标行。
    try:
        render_fact_line(text, new_marker, newline=target.document.newline)
        if kind is FactKind.STATEMENT:
            render_fact_line(
                target_text,
                replace(marker, superseded_by=new_id),
                struck=True,
                newline=_line_ending(target.line.raw),
            )
    except MarkerError as exc:  # pragma: no cover - check_fact_text 已挡住
        return _rejected_patch(
            index, PatchOp.SUPERSEDE, draft, f"cannot render the supersede: {_clip(str(exc))}"
        )

    if kind is FactKind.STATEMENT:
        # 目标行就地改写：行号不变，只把 marker 变成 superseded-by 并划线。
        # 既有的 supersedes 边（A supersedes B 之后又被 C 取代）必须保留。
        target.line.rewrite(
            target_text,
            replace(marker, superseded_by=new_id),
            struck=True,
        )
    new_line = target.document.append_fact(text, new_marker)
    facts_by_id[new_id] = _LocatedFact(
        section=target.section, document=target.document, line=new_line
    )
    known.add(new_id)
    assigned.append(new_id)
    return ChangelogPatch(
        op=PatchOp.SUPERSEDE,
        id=new_id,
        target_id=target_id,
        kind=kind,
        text=text,
        evidence=evidence,
        source=source,
    )


def _apply_link(
    index: int,
    draft: Mapping[str, object],
    *,
    facts_by_id: dict[str, _LocatedFact],
    conflicts: list[MergeConflict],
) -> ChangelogPatch:
    """``link``：把 ``[[to]]`` 追加到 from 事实的文本末尾（不复制事实）。"""
    from_id = _fact_id(_from_value(draft))
    to_id = _fact_id(draft.get("to"))
    if from_id is None or to_id is None:
        return _rejected_patch(
            index,
            PatchOp.LINK,
            draft,
            "link requires both 'from' and 'to' as valid fact ids",
        )
    if from_id == to_id:
        return _rejected_patch(index, PatchOp.LINK, draft, "link endpoints must differ")

    source_fact = facts_by_id.get(from_id)
    if source_fact is None:
        return _rejected_patch(
            index,
            PatchOp.LINK,
            draft,
            f"from fact {from_id} does not exist in any fact file (unknown or archived)",
        )
    if to_id not in facts_by_id:
        return _rejected_patch(
            index,
            PatchOp.LINK,
            draft,
            f"to fact {to_id} does not exist in any fact file (unknown or archived)",
        )

    marker = source_fact.line.marker
    assert marker is not None
    if marker.locked:
        conflicts.append(
            MergeConflict(
                draft_index=index,
                fact_id=from_id,
                detail=(
                    f"draft {index} tried to link from locked fact {from_id}; "
                    "appending text would rewrite a locked fact (SPEC §4/§7)"
                ),
            )
        )
        return _rejected_patch(
            index,
            PatchOp.LINK,
            draft,
            f"from fact {from_id} is locked; appending a link would rewrite a locked fact",
        )

    source_text = source_fact.line.text
    assert source_text is not None
    link_text = f"[[{to_id}]]"
    if link_text in source_text:
        return _skipped_patch(
            index,
            PatchOp.LINK,
            draft,
            f"{link_text} is already present in the from fact",
        )

    new_text = f"{source_text} {link_text}"
    try:
        check_fact_text(new_text)
    except MarkerError as exc:
        return _rejected_patch(
            index,
            PatchOp.LINK,
            draft,
            f"the linked text would break the line grammar: {_clip(str(exc))}",
        )
    source_fact.line.rewrite(new_text, marker, struck=source_fact.line.struck)
    return ChangelogPatch(op=PatchOp.LINK, from_fact=from_id, to=to_id)


# --- 薄壳：写盘、changelog、提交、预算 -------------------------------------


def _clone_documents(
    documents: Sequence[tuple[Section, FactDocument]],
) -> tuple[tuple[Section, FactDocument], ...]:
    """深拷贝 reconcile 交来的文档，让薄壳失败时不污染调用方的 scan。

    :func:`apply_patches` 按设计**就地改写**传入的文档（与 reconcile 的纯
    核心同一姿态），而 ``merge_dossier`` 的调用方（M7）可能把 scan 保留到
    错误处理之后。若直接传 ``scan.documents``，一次在预算/条目校验处失败
    的合并会在 scan 里留下已应用、却从未写进 changelog 的补丁；调用方若
    按 provider 失败路径复用同一个 scan 再 merge，就会把事实写进盘而
    changelog 只记 ``patches: []``——provenance 断裂。

    ``deepcopy`` 而不是 ``parse(render())``：保留 ``FactDocument.newline``
    等解析时确定的字段，逐字节一致由深拷贝直接保证。
    """
    return tuple((section, deepcopy(document)) for section, document in documents)


def merge_dossier(
    dossier: Dossier,
    *,
    scan: ReconcileScan,
    drafts: Sequence[object],
    capture: Capture,
    extractor: str,
    now: datetime | None = None,
    budget: BudgetEstimator | None = None,
) -> MergeResult:
    """在调用方已持锁的前提下，把一次捕获完整落盘并提交。

    调用顺序（M7 的 pipeline 照此编排）::

        with dossier.writer_lock():
            append = dossier.append_journal([text])
            scan = reconcile_dossier(dossier)
            drafts = extract(...)          # M5；失败时传 []
            result = merge_dossier(..., capture=Capture.from_journal_append(dossier, append))

    **不自己获取锁**：plan §5 要求 journal 追加、reconcile、extract 与 merge
    在同一个锁窗口内完成；同进程第二次 ``flock`` 会直接失败，嵌套锁不可行。
    忘记持锁的调用方会失去单写者保证——这是 docstring 明确写出的前置条件。

    写序：读 journal 区间 → 应用 → 预算 → 写文档 → 写 changelog → 提交。
    任何一步失败，journal 都已在盘上（capture 永不阻塞）；已经写出的文件
    由 git 作为恢复后盾，不回滚 journal。

    补丁应用在 ``scan.documents`` 的**深拷贝**上（见 :func:`_clone_documents`）：
    scan 保持 reconcile 之后的内存快照，失败（例如预算估算器抛错）不会在
    其中留下"已应用但没有 changelog 记录"的补丁。注意这不把失败变成事务
    ——写盘到一半或提交失败后的恢复仍以 git 为准。
    """
    journal_lines = dossier.read_journal_entries(
        capture.path,
        first_line=capture.first_line,
        last_line=capture.last_line,
    )
    plan = apply_patches(
        _clone_documents(scan.documents),
        drafts,
        capture=capture,
        journal_lines=journal_lines,
        known_ids=scan.known_ids,
    )
    return _write_merge(
        dossier,
        plan.documents,
        patches=plan.patches,
        reconcile=scan.report,
        malformed=plan.malformed,
        conflicts=plan.conflicts,
        input_label=capture.path,
        extractor=extractor,
        now=now,
        budget=budget,
    )


def review_dossier(
    dossier: Dossier,
    *,
    scan: ReconcileScan,
    fact_id: str,
    action: ReviewAction,
    text: str | None = None,
    standalone: bool = False,
    now: datetime | None = None,
    budget: BudgetEstimator | None = None,
) -> ReviewOutcome:
    """在调用方已持锁的前提下落盘一个人工 review 决定。

    与 :func:`merge_dossier` 共用 ``_write_merge`` 的写盘/changelog/commit
    尾段，因此"一次决定 = 一条 changelog = 一个提交"在两条写路径上只有
    一份实现。``scan`` 必须是本次锁窗口内刚跑过的 reconcile 快照：人工
    决定必须基于手改之后的事实状态，而不是 CLI 列表看到时的旧快照。

    ``target_changed=True`` 时不写任何东西——那只是把 OQ#4 的"目标已变化"
    交回给调用方，让用户重新选择，不是一次空 merge。
    """
    plan = apply_review(
        _clone_documents(scan.documents),
        fact_id=fact_id,
        action=action,
        text=text,
        standalone=standalone,
    )
    if plan.target_changed:
        return ReviewOutcome(
            fact_id=fact_id,
            action=action,
            merge=None,
            target_changed=True,
            target_id=plan.target_id,
        )

    assert plan.patch is not None  # target_changed 分支之外，纯核心总会产出事件
    result = _write_merge(
        dossier,
        plan.documents,
        patches=(plan.patch,),
        reconcile=scan.report,
        malformed=(),
        conflicts=(),
        input_label=_REVIEW_INPUT,
        extractor=_REVIEW_EXTRACTOR,
        now=now,
        budget=budget,
    )
    return ReviewOutcome(fact_id=fact_id, action=action, merge=result)


def _write_merge(
    dossier: Dossier,
    documents: Sequence[tuple[Section, FactDocument]],
    *,
    patches: Sequence[ChangelogPatch],
    reconcile: ReconcileReport,
    malformed: Sequence[MalformedDraft],
    conflicts: Sequence[MergeConflict],
    input_label: str,
    extractor: str,
    now: datetime | None,
    budget: BudgetEstimator | None,
) -> MergeResult:
    """写盘/提交尾段——M8 的 review 合并也可以复用（见模块 docstring）。

    预算估算与 changelog 条目构造都放在第一次写盘之前：估算器是纯函数，
    条目构造会做字段校验；两者失败时磁盘上不应留下半次合并。
    """
    budget_status: BudgetStatus | None = None
    if budget is not None:
        tokens = budget(documents)
        budget_status = BudgetStatus(tokens=tokens, limit=dossier.load_profile().core_budget_tokens)

    # 先构造条目再写盘：空 extractor 之类的可预见校验失败不应发生在
    # 文档已经被改写之后。merge id 由 write_changelog 按文件名 stem 回填。
    entry = ChangelogEntry(
        merge="placeholder",
        input=input_label,
        extractor=extractor,
        patches=[*reconcile.patches, *patches],
    )

    for section, document in documents:
        dossier.write_fact_document(section, document)

    changelog_path = dossier.write_changelog(entry, now=now)
    commit = dossier.git_commit(f"merge: {changelog_path.stem}")
    return MergeResult(
        merge_id=changelog_path.stem,
        changelog_path=changelog_path,
        commit=commit,
        patches=tuple(patches),
        reconcile=reconcile,
        malformed=tuple(malformed),
        conflicts=tuple(conflicts),
        budget=budget_status,
    )


# --- 内部工具 ---------------------------------------------------------------


_EVIDENCE_MISMATCH = "evidence is not a verbatim (whitespace-collapsed) span of this capture range"


def _index_facts(
    documents: Sequence[tuple[Section, FactDocument]],
) -> dict[str, _LocatedFact]:
    """建 id → 事实位置的索引，并拒绝重复 id（SPEC §4 第 3 案）。

    reconcile 已经拒过一次；纯核心是公开入口，再做一次防御，避免任何人
    拿着未经扫描的文档调进来时，索引静默"取最后一条"——那正是 §4 说
    绝不能做的事。
    """
    facts_by_id: dict[str, _LocatedFact] = {}
    for section, document in documents:
        for line in document.facts():
            marker = line.marker
            assert marker is not None  # facts() 的不变量
            previous = facts_by_id.get(marker.fact_id)
            if previous is not None:
                raise MergeRefused(
                    "refusing the merge: duplicate fact id "
                    f"{marker.fact_id} at {previous.section.path}:L{previous.line.line_no} "
                    f"and {section.path}:L{line.line_no} (SPEC §4: never pick one silently)"
                )
            facts_by_id[marker.fact_id] = _LocatedFact(
                section=section, document=document, line=line
            )
    return facts_by_id


def _next_id(known: set[str], facts_by_id: Mapping[str, _LocatedFact]) -> str:
    """下一个可用 id；与现有事实碰撞是调用方 bug，不是可以静默跳过的状态。"""
    new_id = next_fact_id(known)
    if new_id in facts_by_id:
        raise ZhiwoError(
            f"id assignment collided on {new_id}: the reconcile scan is stale or "
            "its known_ids ceiling is missing a fact (refusing to silently renumber)"
        )
    return new_id


def _require_journal_lines(capture: Capture, journal_lines: Sequence[str]) -> None:
    """纯核心的前置条件：区间长度与捕获文本必须一一对应。"""
    expected = capture.last_line - capture.first_line + 1
    if len(journal_lines) != expected:
        raise ZhiwoError(
            f"capture range L{capture.first_line}-L{capture.last_line} has "
            f"{expected} line(s), got {len(journal_lines)} journal line(s)"
        )
    for line in journal_lines:
        if "\n" in line or "\r" in line:
            raise ZhiwoError(
                f"journal_lines must be one entry per line (no embedded newlines): {line!r}"
            )


def _match_evidence(evidence: str, capture: Capture, journal_lines: Sequence[str]) -> str | None:
    """把证据解析成 ``journal/...md#L<n>``；不匹配返回 None。

    规则（R1 的实现旋钮，测试钉住）：

    1. 证据与原文都做空白折叠（任意 Unicode 空白 → 单个空格 → strip）。
    2. **先逐行找**：命中即定位该行。这保证"证据就在某一行里"时不会因为
       跨行拼接而漂到别的行。
    3. 逐行未命中再在**整个区间的折叠拼接**里找：跨行引用是合法的逐字
       片段，定位到匹配起点所在的行（分隔空格归前一行）。
    4. 首次出现优先；证据只允许落在本次捕获的行区间内——引用更早的
       journal 行会被拒（"这次捕获"是证据边界）。
    """
    needle = _collapse(evidence)
    if not needle:
        return None

    normalized = [_collapse(line) for line in journal_lines]
    for offset, line in enumerate(normalized):
        if needle in line:
            return _source(capture, offset)

    # 逐行未命中：拼成"行 + 空格"的整段再找。starts 记录每行在拼接串里
    # 的起始偏移，用于把命中位置映射回行号。
    starts: list[int] = []
    pieces: list[str] = []
    position = 0
    for line in normalized:
        starts.append(position)
        pieces.append(line)
        position += len(line) + 1
    combined = " ".join(pieces)
    hit = combined.find(needle)
    if hit == -1:
        return None

    offset = 0
    for index, start in enumerate(starts):
        if start <= hit:
            offset = index
        else:
            break
    return _source(capture, offset)


def _source(capture: Capture, offset: int) -> str:
    return f"{capture.path}#L{capture.first_line + offset}"


def _collapse(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _as_draft_mapping(raw: object) -> Mapping[str, object] | None:
    if not isinstance(raw, Mapping):
        return None
    return cast("Mapping[str, object]", raw)


def _parse_op(value: object) -> PatchOp | None:
    if not isinstance(value, str):
        return None
    try:
        return PatchOp(value)
    except ValueError:
        return None


def _parse_kind(value: object) -> FactKind | None:
    if not isinstance(value, str):
        return None
    try:
        return FactKind(value)
    except ValueError:
        return None


def _fact_id(value: object) -> str | None:
    if isinstance(value, str) and _FACT_ID_RE.match(value):
        return value
    return None


def _string(draft: Mapping[str, object], key: str) -> str | None:
    value = draft.get(key)
    return value if isinstance(value, str) and value else None


def _from_value(draft: Mapping[str, object]) -> object:
    """读 link 的 from；契约键是 ``from``，也接受 Python 侧的 ``from_fact``。"""
    if "from" in draft:
        return draft["from"]
    return draft.get("from_fact")


def _unsupported_reason(op: PatchOp) -> str:
    if op is PatchOp.COMPRESS:
        return "compress is distill-only (v0.5); the v0.2 merge cannot apply it"
    if op is PatchOp.ADOPT:
        return "adopt is a reconcile-only op; extractor patches may not emit it"
    if op in (PatchOp.CONFIRM, PatchOp.EDIT, PatchOp.REJECT):
        return f"{op.value} is a review-only op; extractor patches may not emit it"
    return "retire is a reconcile-only op; extractor patches may not emit it"


def _rejected_patch(
    index: int, op: PatchOp, draft: Mapping[str, object], detail: str
) -> ChangelogPatch:
    """构造一条 rejected 记录；只打捞类型合法的字段，非法值只进 reason。"""
    payload: dict[str, object] = {
        "op": op,
        "result": PatchResult.REJECTED,
        "reason": f"draft {index}: {detail}",
    }
    _salvage_fields(draft, payload)
    return ChangelogPatch.model_validate(payload)


def _skipped_patch(
    index: int, op: PatchOp, draft: Mapping[str, object], detail: str
) -> ChangelogPatch:
    payload: dict[str, object] = {
        "op": op,
        "result": PatchResult.SKIPPED,
        "reason": f"draft {index}: {detail}",
    }
    _salvage_fields(draft, payload)
    return ChangelogPatch.model_validate(payload)


def _salvage_fields(draft: Mapping[str, object], payload: dict[str, object]) -> None:
    """把草稿里**类型合法**的字段带进拒绝记录，其余只留在 reason 里。

    拒绝记录的权威是 reason；字段只是审计的辅助。把非法值硬塞进
    ``target_id`` / ``members`` 之类的类型字段会触发 pydantic 校验失败，
    反而让"拒绝"本身写不进 changelog。
    """
    target = _string(draft, "target")
    if target is not None:
        payload["target"] = target
    target_id = _fact_id(draft.get("target_id"))
    if target_id is not None:
        payload["target_id"] = target_id
    kind = _parse_kind(draft.get("kind"))
    if kind is not None:
        payload["kind"] = kind
    text = _string(draft, "text")
    if text is not None:
        payload["text"] = text
    evidence = _string(draft, "evidence")
    if evidence is not None:
        payload["evidence"] = evidence
    from_id = _fact_id(_from_value(draft))
    if from_id is not None:
        payload["from_fact"] = from_id
    to_id = _fact_id(draft.get("to"))
    if to_id is not None:
        payload["to"] = to_id
    members = _members(draft.get("members"))
    if members is not None:
        payload["members"] = members


def _members(value: object) -> list[str] | None:
    """只接受非空且全部合法的成员集；否则整体缺席（不伪造成员）。"""
    if not isinstance(value, list) or not value:
        return None
    members: list[str] = []
    for item in value:
        fact_id = _fact_id(item)
        if fact_id is None:
            return None
        members.append(fact_id)
    return members


def _brief(value: object, limit: int = _REASON_VALUE_LIMIT) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _clip(text: str, limit: int = _REASON_VALUE_LIMIT) -> str:
    """截断一段人类可读的错误原文，避免一条坏草稿把 changelog 撑爆。"""
    return text if len(text) <= limit else f"{text[:limit]}..."


def _is_applied(patch: ChangelogPatch) -> bool:
    """缺省 result 视为 applied（决策 10 / 黄金条目的形态）。"""
    return patch.result in (None, PatchResult.APPLIED)
