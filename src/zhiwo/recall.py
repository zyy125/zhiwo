"""L4：recall——把档案读成视图、检索与溯源（SPEC §2.1 / §5 / §6）。

本模块是读者与提取器之间的**唯一视图层**。它只做两类事：

- **纯核心**：给定内存里的 ``(Section, FactDocument)`` 列表，渲染 core view /
  reader view、估算 token、检索与 join。不碰文件、不碰模型，因此可以对着
  内存夹具测试。
- **薄壳**：``Dossier`` 参数只出现在 ``load_documents`` / ``logical_records`` /
  ``search_journal`` / ``provenance`` 这类入口；读盘仍全部委托 L0
  （``store.py``），本模块不自己拼档案路径。

边界纪律（与 D-007 / D-008 对齐）：

- **core view 是 extract 的输入**（M5 只把它当不透明字符串），只含
  active + pending 事实；locked 是标注而不是状态（SPEC §2.3）。
  superseded / archived 不进入——契约明确写的是 "active + locked + pending"。
- **reader view 是读者边界**（SPEC §5）：只含 active 事实，按 clearance
  过滤分区；journal 在任何 clearance 下都不进入 reader view（plan §6）。
- **逻辑记录是文件与 changelog 的 join**（SPEC §2.1）：文件对文本与
  存活状态负责，changelog 对 kind / source / 时间负责。缺哪一半就留
  ``None``，绝不猜。
- **journal 检索是 high clearance 能力**（SPEC §5 / 决议 4）：normal
  读者拿不到任何命中。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from zhiwo.errors import ZhiwoError
from zhiwo.factfile import FactDocument, FactLine
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
)
from zhiwo.schema import can_read
from zhiwo.store import Dossier

# 契约里的日期只到天（SPEC §2.1 的逻辑记录）。changelog 的 merge id 可能是
# 写入方的 ``YYYY-MM-DDThhmmss``，也可能是黄金示例的 ``m_YYYY-MM-DDThhmm``
# （R5：读取器比写入者宽容）；两种形态里的 ISO 日期是唯一共有的时间信息。
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# changelog 里 ``source: journal/2026-10-11.md#L7`` 的坐标。路径用贪婪
# ``.+``，允许档案内的路径本身含 ``#``；行号必须为正整数。
_SOURCE_RE = re.compile(r"^(?P<path>.+)#L(?P<line>[1-9][0-9]*)$")

# 真正“创建了一条事实”的 op（与 reconcile 的 _CREATION_OPS 同一口径）。
# compress 由 distill 在 v0.5 发射；读取器现在就要认识它，否则未来的
# changelog 会让旧代码把压缩事实当成“无来源”。
_CREATION_OPS = frozenset({PatchOp.ADD, PatchOp.SUPERSEDE, PatchOp.ADOPT, PatchOp.COMPRESS})

# 让事实进入 archived 的事件：SPEC §4 的 retire 与 M8 review 的 reject。
# 两者都以 changelog 记录为墓碑（D-011）；读取器必须用同一口径判断
# "文件里没有 + 历史里归档过"，否则被 review 拒绝的事实会看起来像
# 一条凭空消失、无法解释的记录。
_ARCHIVAL_OPS = frozenset({PatchOp.RETIRE, PatchOp.REJECT})

# CJK 码位范围。用宽区间而不是逐个 Unicode 区块：未分配码位出现在
# 事实文本里时，多算一个 token 比漏算安全（预算只是警告，SPEC §6 / D-002）。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # Extension A
    (0x4E00, 0x9FFF),  # Unified Ideographs
    (0xF900, 0xFAFF),  # Compatibility Ideographs
    (0x20000, 0x323AF),  # Extensions B–H（宽区间，含未分配空隙）
)

# journal 检索默认返回的命中数。plan §6 只写“top-N”，5 是 MVP 的默认值；
# 调用方（M8 的 me ask）可以显式覆盖。
_DEFAULT_TOP_N = 5


@dataclass(frozen=True, slots=True)
class FactRecord:
    """SPEC §2.1 的**逻辑记录**：文件事实与 changelog 元数据的 join。

    文件是文本、存活状态、locked 与关系边的唯一权威；changelog 只补
    kind / source / created / updated。因此没有 changelog 的旧事实
    （黄金示例里的大量既有行）在这些字段上是 ``None``——这是诚实的
    “不知道”，不是可以拿文件状态倒推的猜测。

    ``status`` 取 :class:`~zhiwo.models.FactStatus`；``archived`` 只可能
    来自一条 applied ``retire`` 或 M8 的 ``reject`` 事件（SPEC §2.1：文件里
    没有 archived 写法；两类事件都以 changelog 为墓碑，D-011）。
    ``line_no`` 对 archived 事实为 ``None``（它已经不在文件里）。
    """

    id: str
    text: str
    kind: FactKind | None
    status: FactStatus
    locked: bool
    supersedes: str | None
    superseded_by: str | None
    created: str | None
    updated: str | None
    source: str | None
    section_id: str | None
    path: str | None
    line_no: int | None


@dataclass(frozen=True, slots=True)
class JournalHit:
    """一次 journal 关键词命中的一条捕获。

    ``source`` 与 changelog 的 ``source`` 指针同一坐标系：``#L<n>`` 是
    盘上 journal 文件的 1-based 绝对行号。``score`` 是命中的查询 token
    数（去重后的交集大小），供调用方排序或调试。
    """

    path: str
    line_no: int
    text: str
    score: int

    @property
    def source(self) -> str:
        return f"{self.path}#L{self.line_no}"


@dataclass(frozen=True, slots=True)
class SourceLine:
    """provenance 上下文里的一行：原始行号 + 原文（保留 journal 的项目符号）。"""

    line_no: int
    text: str


@dataclass(frozen=True, slots=True)
class ProvenanceEvent:
    """一个触及某条事实的 changelog 事件（含 rejected / skipped）。

    ``patch`` 保留 changelog 里的完整补丁记录（evidence / reason /
    target_id / from / to / members 都在里面）。``context`` 是 source
    指针指向那一行的 ±1 行原文；source 缺失就不读上下文，source 坏了
    则把原因放进 ``context_error``——一条坏指针不该让整个 ``me why``
    无法回答其它事件。
    """

    merge: str
    input: str
    extractor: str
    patch: ChangelogPatch
    context: tuple[SourceLine, ...] = ()
    context_error: str | None = None


@dataclass(frozen=True, slots=True)
class _FileFact:
    """文件里的一条事实及其分区/行对象（join 的输入侧）。"""

    section: Section
    line: FactLine


# --- 纯核心：视图与预算 -----------------------------------------------------


def load_documents(dossier: Dossier) -> tuple[tuple[Section, FactDocument], ...]:
    """读取全部非 journal 事实文件（按 profile 声明顺序）。

    缺失文件被跳过而不是报错：声明了分区但文件还没建是正常状态
    （SPEC §1 的 examples 也是部分树），补建属于 merge 前的 reconcile。
    """
    profile = dossier.load_profile()
    return _load_documents(dossier, profile)


def render_core_view(documents: Sequence[tuple[Section, FactDocument]]) -> str:
    """渲染 extract 的 owner 侧视图（SPEC §2.3）。

    格式与 M5 的 prompt 快照配套，故意保持稳定：

    ``<path>`` 分区头（空分区省略），随后每个事实一行
    ``- f_001 [active] 文本``；locked 追加在方括号内，``supersedes`` 边
    以 ``(supersedes f_009)`` 形式附在行尾。pending 事实的 supersede 边
    是惰性的，但 extract 必须看见它，才能避免重复推导同一结论
    （SPEC §2.3：deferred set 从文件即可重建）。

    只含 active + pending。superseded 留在文件里给人看，但不属于
    extract 的输入——契约原文是 "active + locked + pending facts"。
    """
    blocks: list[str] = []
    for section, document in documents:
        if section.axis is Axis.JOURNAL:
            continue
        lines: list[str] = []
        for fact in document.facts():
            marker = fact.marker
            assert marker is not None  # facts() 的不变量
            if marker.status not in (FactStatus.ACTIVE, FactStatus.PENDING):
                continue
            assert fact.text is not None
            flags = marker.status.value
            if marker.locked:
                flags = f"{flags}, locked"
            line = f"- {marker.fact_id} [{flags}] {fact.text}"
            if marker.supersedes is not None:
                line = f"{line} (supersedes {marker.supersedes})"
            lines.append(line)
        if lines:
            blocks.append("\n".join([section.path, *lines]))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_reader_view(
    documents: Sequence[tuple[Section, FactDocument]],
    *,
    clearance: Clearance = Clearance.NORMAL,
) -> str:
    """渲染读者视图（SPEC §5）：active 事实 + clearance 覆盖的分区。

    规则：

    - ``can_read`` 做分区过滤；normal 读者只拿到显式 normal 的分区。
    - **journal 永不进入 reader view**，即使 reader 有 high clearance
      （plan §6）。journal 回想是独立能力，走 :func:`search_journal`。
    - pending / superseded / archived 不计入；空分区连标题都不输出，
      免得读者视图被一堆空标题撑大预算。

    输出不带 id / marker：读者要读的是事实本身，而不是系统内部身份。
    """
    blocks: list[str] = []
    for section, document in documents:
        if section.axis is Axis.JOURNAL or not can_read(clearance, section):
            continue
        lines: list[str] = []
        for fact in document.facts():
            marker = fact.marker
            assert marker is not None  # facts() 的不变量
            if marker.status is not FactStatus.ACTIVE:
                continue
            assert fact.text is not None
            lines.append(f"- {fact.text}")
        if lines:
            blocks.append("\n".join([f"# {section.title}", "", *lines]))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def estimate_tokens(text: str) -> int:
    """启发式 token 估算（plan §3 决策 5）：``CJK×1 + ceil(其他/4)``。

    这是刻意乐观的估算：CJK 每字 1 token 对部分 tokenizer 偏低，警告可能
    来得晚；MVP 超预算只警告（D-002），不需要精确到某一家的分词器。
    整个估算只在此函数里，未来换成 provider 精确计数时调用方不动。
    """
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def estimate_reader_tokens(documents: Sequence[tuple[Section, FactDocument]]) -> int:
    """M4 预算钩子的实现：high-clearance reader view 的 token 数。

    D-007 把估算器定义为 post-merge documents → token 数；预算量的是
    **最大视图**（high clearance），所以这里显式用 HIGH，和读者实际
    拿到的 normal 视图无关。pending / superseded 已在 render 里排除。
    """
    return estimate_tokens(render_reader_view(documents, clearance=Clearance.HIGH))


# --- journal 检索（SPEC §5 / 决议 4）---------------------------------------


def search_journal(
    dossier: Dossier,
    query: str,
    *,
    clearance: Clearance,
    top_n: int = _DEFAULT_TOP_N,
) -> tuple[JournalHit, ...]:
    """在 journal 捕获行里做关键词检索，返回得分最高的 ``top_n`` 条。

    这是 SPEC §5 的 **high-clearance 能力**：journal 是原始话语流，里面
    躺着高敏感事实的 evidence 原文。normal 读者拿到空元组，而不是
    “过滤后的命中”——journal 的过滤粒度是“有没有 high clearance”，
    不是逐分区裁剪。

    检索是 resolved decision #4 的 keyword-first：

    - 查询与每行都按 :func:`_tokenize` 切 token（P*/Z* 分隔 + CJK bigram）；
    - 得分 = 查询 token 与行 token 的**去重交集大小**——重复出现不刷分，
      避免一条复读的行压过真正覆盖多个查询词的行；
    - 同分按“最新优先”排序（文件路径降序、行号降序），因为问当前状态
      时最近的记录通常更有用；
    - ``top_n`` 必须是正数；0 或负数不是“返回空”，而是调用方 bug。

    空查询（或只有标点的查询）返回空元组，不做“全量返回”。
    """
    if top_n < 1:
        raise ZhiwoError(f"top_n must be a positive integer, got {top_n}")

    profile = dossier.load_profile()
    journal = next((section for section in profile.sections if section.axis is Axis.JOURNAL), None)
    if journal is None or not can_read(clearance, journal):
        return ()

    query_tokens = _tokenize(query)
    if not query_tokens:
        return ()

    hits: list[JournalHit] = []
    for entry in dossier.journal_entries():
        score = len(query_tokens & _tokenize(entry.text))
        if score:
            hits.append(
                JournalHit(
                    path=entry.path,
                    line_no=entry.line_no,
                    text=entry.text,
                    score=score,
                )
            )

    # 两次稳定排序：先建立“最新优先”的底序，再按得分降序。Python 的
    # 稳定排序会把同分行保持在新→旧的顺序里，不需要手写复合 key。
    hits.sort(key=lambda hit: (hit.path, hit.line_no), reverse=True)
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return tuple(hits[:top_n])


# --- 逻辑记录与溯源（SPEC §2.1 / §2.4）------------------------------------


def logical_records(dossier: Dossier) -> tuple[FactRecord, ...]:
    """把文件事实与 changelog 事件 join 成 SPEC §2.1 的逻辑记录。

    权威划分严格按契约：

    - 文件 → ``text / status / locked / supersedes / superseded_by``
      与 ``section / path / line_no``；文件里没有的事实不会被凭空造出来。
    - changelog（仅 applied 事件）→ ``kind`` 取自创建事件，``source``
      取自创建事件，``created`` 取最早触及该 id 的事件日期，``updated``
      取最晚一次 applied 事件的日期。rejected / skipped 只是审计，
      不改变事实状态，因此不参与 created/updated。
    - ``archived`` 只在“创建过 + 有 applied retire/reject + 文件里已不存在”
      时出现（D-006：tombstone 就是 changelog 记录；M8 的 reject 同构，
      D-011）。文本回落到创建事件，因为那时文件里的唯一副本已被移除。

    没有 changelog 的既有事实（黄金示例里大量存在）在 kind/source/时间上
    是 ``None``。这是诚实的未知：拿 pending 标记倒推 inference、拿文件
    mtime 猜创建时间，都会制造第二个权威。

    重复 id 不静默取最后一个（SPEC §4）：直接抛 :class:`ZhiwoError` 并
    列出两处 ``path:L<n>``。这虽然通常先被 reconcile 拦住，但 ``me show``
    可能在任何一次 merge 之前被调用。
    """
    profile = dossier.load_profile()
    documents = _load_documents(dossier, profile)
    located = _index_facts(documents)

    creation: dict[str, ChangelogPatch] = {}
    created: dict[str, str | None] = {}
    updated: dict[str, str | None] = {}
    retired: set[str] = set()

    for entry in dossier.read_changelog():
        date = _entry_date(entry)
        for patch in entry.patches:
            if patch.result is not PatchResult.APPLIED:
                continue
            if patch.op in _ARCHIVAL_OPS and patch.id is not None:
                retired.add(patch.id)
            if patch.op in _CREATION_OPS and patch.id is not None:
                creation.setdefault(patch.id, patch)
                created.setdefault(patch.id, date)
            if date is not None:
                for fact_id in _patch_ids(patch):
                    updated[fact_id] = date

    records: list[FactRecord] = []
    for fact_id, located_fact in located.items():
        marker = located_fact.line.marker
        assert marker is not None  # _index_facts 只收 FACT 行
        assert located_fact.line.text is not None
        creation_patch = creation.get(fact_id)
        records.append(
            FactRecord(
                id=fact_id,
                text=located_fact.line.text,
                kind=creation_patch.kind if creation_patch is not None else None,
                status=marker.status,
                locked=marker.locked,
                supersedes=marker.supersedes,
                superseded_by=marker.superseded_by,
                created=created.get(fact_id),
                updated=updated.get(fact_id),
                source=creation_patch.source if creation_patch is not None else None,
                section_id=located_fact.section.id,
                path=located_fact.section.path,
                line_no=located_fact.line.line_no,
            )
        )

    sections_by_path = {section.path: section for section in profile.sections}
    for fact_id in sorted(fact_id for fact_id in creation if fact_id not in located):
        if fact_id not in retired:
            continue
        creation_patch = creation[fact_id]
        assert creation_patch.text is not None  # applied 创建事件必有 text（SPEC §3）
        target = creation_patch.target
        section = sections_by_path.get(target) if target is not None else None
        records.append(
            FactRecord(
                id=fact_id,
                text=creation_patch.text,
                kind=creation_patch.kind,
                status=FactStatus.ARCHIVED,
                locked=False,
                supersedes=None,
                superseded_by=None,
                created=created.get(fact_id),
                updated=updated.get(fact_id),
                source=creation_patch.source,
                section_id=section.id if section is not None else None,
                path=target,
                line_no=None,
            )
        )
    return tuple(records)


def provenance(dossier: Dossier, fact_id: str) -> tuple[ProvenanceEvent, ...]:
    """返回所有**触及**该 id 的 changelog 事件，按时间序（SPEC §2.4）。

    “触及”包括所有身份字段：``id``（创建/退休）、``target_id``（被取代）、
    ``from`` / ``to``（链接两端）、``members``（未来的 compress）。只看
    正文里的 ``[[f_x]]`` 提及不算——那是文本内容，不是事件主体，否则
    一条链接会被误当成很多条事实的溯源。

    rejected / skipped 记录也返回：它们是“边界拦截过什么”的审计，
    但不会影响 :func:`logical_records` 的状态/时间。每条事件的 source
    都会读成 ±1 行上下文；source 坏了只填 ``context_error``，不影响
    evidence 与事件本身。
    """
    events: list[ProvenanceEvent] = []
    for entry in dossier.read_changelog():
        for patch in entry.patches:
            if fact_id not in _patch_ids(patch):
                continue
            context, context_error = _source_context(dossier, patch.source)
            events.append(
                ProvenanceEvent(
                    merge=entry.merge,
                    input=entry.input,
                    extractor=entry.extractor,
                    patch=patch,
                    context=context,
                    context_error=context_error,
                )
            )
    return tuple(events)


# --- 内部工具：共享的视图/检索基础 ------------------------------------------


def _load_documents(dossier: Dossier, profile: Profile) -> tuple[tuple[Section, FactDocument], ...]:
    """``load_documents`` 的共用实现；``profile`` 已由调用方读过。

    保留此函数是为了让 ``logical_records`` 复用同一份 profile，避免在一次
    join 里读两次 ``profile.yaml``。journal 分区不是事实文件，直接跳过。
    """
    documents: list[tuple[Section, FactDocument]] = []
    for section in profile.sections:
        if section.axis is Axis.JOURNAL:
            continue
        document = dossier.read_fact_document(section)
        if document is not None:
            documents.append((section, document))
    return tuple(documents)


def _tokenize(text: str) -> frozenset[str]:
    """按 resolved decision #4 切检索 token。

    规则：

    - 以 Unicode **P***（标点）与 **Z***（分隔符）为断点；控制字符
      也算断点，避免换行/零宽字符把两个词黏成一个 token。
    - 大小写折叠（``casefold``）让英文查询不区分大小写。
    - 每个断点之间的一段是一个 token；其中**连续 CJK 段**额外生成
      bigram，且只在段内生成——``面试挂了，二面`` 不会产出跨标点的
      ``了二``，那正是“只在连续 CJK 段内”要防的假阳性。
    - 单字 CJK 段没有 bigram，但整段 token 就是那个字，仍可命中。

    返回 ``frozenset``：检索只关心“哪些 token 出现”，重复不改变得分。
    """
    tokens: set[str] = set()
    run: list[str] = []
    for char in text.casefold():
        if _is_token_separator(char):
            _flush_token_run(run, tokens)
            run.clear()
        else:
            run.append(char)
    _flush_token_run(run, tokens)
    return frozenset(tokens)


def _flush_token_run(run: list[str], tokens: set[str]) -> None:
    """把一段非分隔符文本写成 token，并为其中的连续 CJK 段补 bigram。"""
    if not run:
        return
    word = "".join(run)
    tokens.add(word)

    index = 0
    while index < len(word):
        if not _is_cjk(word[index]):
            index += 1
            continue
        end = index
        while end < len(word) and _is_cjk(word[end]):
            end += 1
        for offset in range(index, end - 1):
            tokens.add(word[offset : offset + 2])
        index = end


def _is_token_separator(char: str) -> bool:
    """P*/Z* 为断点；Cc/Cf 等控制字符同样是断点（防御脏输入）。"""
    category = unicodedata.category(char)
    return category[0] in {"P", "Z", "C"} or char.isspace()


def _is_cjk(char: str) -> bool:
    """字符是否属于 CJK 统一表意文字（预算与 bigram 共用同一判定）。"""
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def _index_facts(
    documents: Sequence[tuple[Section, FactDocument]],
) -> dict[str, _FileFact]:
    """建立 id → 文件事实索引；重复 id 响亮失败（SPEC §4）。

    ``dict`` 保持插入顺序，而插入按文档顺序、行顺序进行，因此
    :func:`logical_records` 可以直接用它产生确定的记录顺序。
    """
    facts: dict[str, _FileFact] = {}
    for section, document in documents:
        for line in document.facts():
            marker = line.marker
            assert marker is not None  # facts() 的不变量
            previous = facts.get(marker.fact_id)
            if previous is not None:
                raise ZhiwoError(
                    "duplicate fact id "
                    f"{marker.fact_id} at {previous.section.path}:L{previous.line.line_no} "
                    f"and {section.path}:L{line.line_no} (SPEC §4: never pick one silently)"
                )
            facts[marker.fact_id] = _FileFact(section=section, line=line)
    return facts


def _patch_ids(patch: ChangelogPatch) -> set[str]:
    """一条 changelog 记录触及的全部事实 id（身份、关系、成员）。"""
    ids = {
        fact_id
        for fact_id in (patch.id, patch.target_id, patch.from_fact, patch.to)
        if fact_id is not None
    }
    if patch.members:
        ids.update(patch.members)
    return ids


def _entry_date(entry: ChangelogEntry) -> str | None:
    """从 merge id 里取 ISO 日期（SPEC §2.1 的 created/updated 只到天）。

    写入方保证 merge id 是 ``YYYY-MM-DDThhmmss`` 形式的文件名 stem；黄金
    示例保留了 ``m_`` 前缀（R5：读取器比写入者宽容）。两者都含日期，
    匹配不到时返回 ``None``，不要拿文件 mtime 猜。
    """
    match = _ISO_DATE_RE.search(entry.merge)
    return match.group(0) if match is not None else None


def _source_context(
    dossier: Dossier, source: str | None
) -> tuple[tuple[SourceLine, ...], str | None]:
    """把 source 指针读成 ±1 行上下文；失败作为数据返回。"""
    if source is None:
        return (), None
    match = _SOURCE_RE.match(source)
    if match is None:
        return (), f"malformed source pointer: {source!r}"
    try:
        lines = dossier.read_line_context(match.group("path"), int(match.group("line")))
    except ZhiwoError as exc:
        return (), str(exc)
    return tuple(SourceLine(line_no=number, text=text) for number, text in lines), None
