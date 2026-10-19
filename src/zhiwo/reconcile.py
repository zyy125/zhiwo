"""L3 前哨：reconcile——人类的手改与系统的认知在这里对账（SPEC §4）。

reconcile 是 merge 管线的第 4 步（plan §5），在写锁内、每次 merge 前运行：
人会在两次 merge 之间直接编辑 markdown——加一行、删一行、改坏一个 marker。
这一步的工作是把五种观察（SPEC §4 的表格）转成**数据**：收编、退休、
隔离报告、冲突旗标，外加 R9 的划线不一致警告。

两条纪律钉死整个模块的形状：

- **扫描，不是写者**（D-006）。纯核心 :func:`reconcile_documents` 只改
  内存里的 :class:`~zhiwo.factfile.FactDocument`（且只经
  :meth:`~zhiwo.factfile.FactLine.rewrite` 这一条改行入口）并产出
  changelog 补丁；**零 I/O、零模型调用**。落盘——收编后的文件、补建的
  分区文件、changelog 条目、git commit——全部归 M4 管线的后续步骤。
  这样"收编 + 它的 changelog 记录 + 它的 commit"保持原子：崩溃在半途，
  ``git checkout -- about/ goals/`` 丢弃半个合并（plan §5），没被收编的
  手写行原样留在盘上，下次 merge 再收编；绝不会出现"文件里有了 id、
  changelog 里却查无此案"的孤儿。round-trip 定律让 M4 可以全量写回全部
  文档而零代价——没被碰过的文件 render 出来逐字节不变。
- **两层 API**。纯核心 + :func:`reconcile_dossier` 薄壳（唯一碰磁盘的
  入口，读 profile / 事实文件 / changelog）。核心对着内存文档即可测试，
  M4 也可以只调薄壳。"LLM 调用点是唯一的裂缝"由此保持：本模块不 import
  任何会碰模型的东西，薄壳的 ``Dossier`` 参数是唯一的 I/O 面。
"""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import dataclass

from zhiwo.errors import MarkerError, MergeRefused

# 私有符号 ``_FACT_ID_RE`` 的跨模块引用是刻意的：id 形态的权威正则只有
# 一份（factfile），与 ``models.FactId`` 的一致性由 test_factfile 钉住。
# 这里再抄一份，"模型放宽了 id 而 reconcile 没跟上"这类漂移就多了一个入口。
from zhiwo.factfile import (
    _FACT_ID_RE,
    FactDocument,
    Marker,
    mentioned_fact_ids,
)
from zhiwo.models import (
    Axis,
    ChangelogPatch,
    FactKind,
    FactStatus,
    PatchOp,
    PatchResult,
    Section,
)
from zhiwo.store import Dossier

# retire 候选只认"确实创建过"的 id：add / supersede / adopt 且 result
# 为 applied（D-006）。supersede 会在目标上产生新事实身份（新 id），
# 所以也算创建；compress（v0.5）产出的是合并后的新 id，届时加进来即可。
_CREATION_OPS = frozenset({PatchOp.ADD, PatchOp.SUPERSEDE, PatchOp.ADOPT})


@dataclass(frozen=True, slots=True)
class Location:
    """一个契约面上的位置：分区 + 声明路径 + 1-based 行号。

    ``path`` 用 profile 声明的相对路径而不是绝对路径——报告是给人看的
    （quarantine、拒签消息），绝对路径里的 tmp 目录只会制造噪音。
    """

    section_id: str
    path: str
    line_no: int

    def render(self) -> str:
        """``about/career.md:L4`` 形式——与 journal 的 #L<n> 同一坐标系。"""
        return f"{self.path}:L{self.line_no}"


@dataclass(frozen=True, slots=True)
class QuarantineItem:
    """SPEC §4 第 4 案：一行被隔离的坏行（报告，不动，永不阻塞）。

    ``reason`` 是人类可读的原因：坏 marker 的解析错误原文，或手写行
    无法合法收编的文法错误。"never guess" 意味着系统既不修复它、
    也不绕过它——修好它是人的事。
    """

    location: Location
    reason: str


@dataclass(frozen=True, slots=True)
class Adoption:
    """SPEC §4 第 1 案：一条被收编的手写行。

    ``evidence`` 是**行本身**（去掉行终止符，含 ``- `` 项目符号）：
    SPEC §4 说 adopt 的证据就是 the line itself——人写下的就是原话。
    """

    location: Location
    fact_id: str
    text: str
    evidence: str
    patch: ChangelogPatch


@dataclass(frozen=True, slots=True)
class Retirement:
    """SPEC §4 第 2 案：一个被手删的事实 id。

    没有墓碑落在文件里（SPEC §2.1：archived never appears in a file）——
    ``patch``（op: retire）本身就是墓碑，``me why`` 从 changelog 读它。
    """

    fact_id: str
    patch: ChangelogPatch


@dataclass(frozen=True, slots=True)
class Conflict:
    """SPEC §4 第 5 案：收编的 locked 事实与既有 active 事实相撞。

    M3 只做**结构化切片**（D-006）：文本完全相同的撞车。语义矛盾
    （"这句话和那句矛盾"）是对意义的判断，按 SPEC §3 属于 extract
    （M5），M4 的 locked-fact protection 会接上。系统在两个方向上
    都**绝不自动 supersede**——只旗标，人裁决（me review）。
    """

    fact_id: str
    target_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class MarkerWarning:
    """plan §8 R9：划线装饰与 marker 不一致——只警告，永不改写。

    marker 是权威，划线是渲染；状态永远按 marker 算。人改坏了装饰
    不是系统替他改回去的理由（SPEC §4：human lines are never
    auto-rewritten）。
    """

    location: Location
    fact_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """纯核心的全部产出：五种观察的发现 + 待 M4 落盘的补丁。

    元组而不是列表：报告是一次扫描的不可变结果，调用方（M4/CLI）
    只消费不追加。``patches`` 的顺序是确定性的（adopt 按文档/行序、
    retire 按 id 序）——同样的输入永远产出同样的 changelog。
    """

    adopted: tuple[Adoption, ...]
    retired: tuple[Retirement, ...]
    quarantined: tuple[QuarantineItem, ...]
    conflicts: tuple[Conflict, ...]
    warnings: tuple[MarkerWarning, ...]
    patches: tuple[ChangelogPatch, ...]


@dataclass(frozen=True, slots=True)
class ReconcileScan:
    """薄壳的产出：报告 + 调用方（M4）要落盘的全部文档。

    ``documents`` 按 profile 分区序返回全部非 journal 事实文档
    （adopt 已就地改写）；M4 全量写回它们即可——round-trip 定律保证
    没被碰过的文件写出逐字节不变的原文。``created`` 是声明了但文件
    缺失的分区（plan §5 步骤 4 的"补建"）：桩文档在这里创建，文件
    本身由调用方写。
    """

    report: ReconcileReport
    documents: tuple[tuple[Section, FactDocument], ...]
    created: tuple[Section, ...]


def next_fact_id(known_ids: Set[str]) -> str:
    """下一个可用 id：``max + 1``，零填充到至少 3 位（plan §3 决策 4）。

    三条边界规则：

    - 空集 → ``f_001``（刚 bootstrap 的档案收编第一条手写事实）；
    - **不填洞**：``{f_001, f_003}`` → ``f_004``。洞是历史（被删的、
      被拒的、手写没落盘的），填洞 = 复用 = ``me why f_x`` 的历史指向
      两条不同的事实；
    - ``f_999`` → ``f_1000``：零填充是最小宽度，不是上限。

    输入本应全部合法（文件侧出自同一个正则，changelog 侧经 pydantic
    校验），过滤不合形态的 token 只是防御——真出现时宁可跳号。
    """
    return f"f_{_max_id_num(known_ids) + 1:03d}"


def _max_id_num(known_ids: Set[str]) -> int:
    """集合里最大的 id 数字部分；空集或全不合形态 → 0。"""
    nums = [int(token[2:]) for token in known_ids if _FACT_ID_RE.match(token)]
    return max(nums) if nums else 0


def reconcile_documents(
    documents: Sequence[tuple[Section, FactDocument]],
    *,
    changelog_ids: Set[str],
    retired_ids: Set[str],
    created_ids: Set[str],
) -> ReconcileReport:
    """纯核心：对一组（分区, 文档）跑完 SPEC §4 的五种观察。

    参数是三个 id 集合（薄壳从 changelog 推导，见
    :func:`reconcile_dossier`），核心因此不碰磁盘：

    - ``changelog_ids``：changelog 里**出现过的**全部 patch.id（含
      rejected/skipped）——id 天花板的 changelog 侧（SPEC §4
      "never resurrected"：退役的 id 不复用，分配过就不再派发）；
    - ``retired_ids``：已有 retire 事件的 id——防止每次 merge 把同一条
      被删事实退休一遍（changelog 只追加，重复记录毫无信息量）；
    - ``created_ids``：有 **applied 创建事件**（add/supersede/adopt）的
      id——只有"确实创建过"的 id 才有"被手删"可言；rejected 补丁的
      id 从未在盘上存在过，绝不能被判为手删（D-006）。

    顺序即语义：**先拒签，再报告，最后改写**。重复 id（第 3 案）在
    任何行被碰之前抛 :class:`~zhiwo.errors.MergeRefused`——被拒的
    merge 不留半点痕迹；retire 的存在性检查对着**原始**文档做，
    收编（会改写行、引入新 id）排在它之后。

    本函数不改磁盘：改写只发生在内存文档上，落盘是 M4 的事。
    """
    # --- 阶段 1：重复 id（§4 第 3 案）——在改写发生之前拒绝整次 merge ----
    locations_by_id: dict[str, list[Location]] = {}
    for section, document in documents:
        for line in document.facts():
            assert line.marker is not None  # facts() 的不变量，见 factfile
            locations_by_id.setdefault(line.marker.fact_id, []).append(
                Location(section.id, section.path, line.line_no)
            )
    duplicates = {fact_id: locs for fact_id, locs in locations_by_id.items() if len(locs) > 1}
    if duplicates:
        detail = "; ".join(
            f"{fact_id} appears in {len(locs)} places: " + ", ".join(loc.render() for loc in locs)
            for fact_id, locs in sorted(duplicates.items())
        )
        raise MergeRefused(
            "refusing the whole merge: duplicate fact ids (SPEC §4: never pick "
            f"one silently): {detail}"
        )

    # --- 阶段 2：隔离坏 marker（§4 第 4 案）——报告、不动、不阻塞 --------
    quarantined: list[QuarantineItem] = []
    for section, document in documents:
        for line in document.damaged():
            assert line.damage is not None  # damaged() 的不变量，见 factfile
            quarantined.append(
                QuarantineItem(
                    location=Location(section.id, section.path, line.line_no),
                    reason=line.damage,
                )
            )

    # --- 阶段 3：retire（§4 第 2 案，plan §8 R8/R12）----------------------
    # 存在性 = 注释 span 内的 id 集合（含坏 marker 与散文注释提及——
    # 检测存在，绝不猜语义；``[[…]]`` wikilink 已被剥掉）。此刻文档
    # 尚未被收编改写，检查对着的是人手改之后的原状。
    file_ids: set[str] = set()
    for _, document in documents:
        file_ids |= mentioned_fact_ids(document.render())
    # 交集是防御：按薄壳的推导 created_ids ⊆ changelog_ids，但核心不该
    # 信任调用方的推导——一个"创建了却从未记账"的 id 不该被退休。
    candidates = sorted((created_ids & changelog_ids) - file_ids - retired_ids)
    retired: list[Retirement] = []
    for fact_id in candidates:
        retired.append(
            Retirement(
                fact_id=fact_id,
                patch=ChangelogPatch(
                    op=PatchOp.RETIRE,
                    id=fact_id,
                    reason=(
                        "fact line missing from every fact file (hand-deleted; "
                        "SPEC §4: never resurrected)"
                    ),
                ),
            )
        )

    # --- 阶段 4：收编手写行（§4 第 1 案 + 第 5 案的结构化切片）------------
    # id 从 max(文件侧 ∪ changelog 侧) + 1 起步；同轮多条手写行按
    # 文档序（= profile 分区序，由调用方保证）+ 行序连号。
    next_num = _max_id_num(file_ids | changelog_ids) + 1
    # 冲突对照集：既有 active 事实的 (文本, id)。收编引入的新行不在
    # 对照集里——第 5 案说的是"新 locked 事实撞上**既有** active 事实"。
    active_facts: list[tuple[str, str]] = []
    for _, document in documents:
        for line in document.facts():
            assert line.marker is not None
            if line.marker.status == FactStatus.ACTIVE and line.text is not None:
                active_facts.append((line.text, line.marker.fact_id))
    adopted: list[Adoption] = []
    conflicts: list[Conflict] = []
    for section, document in documents:
        for line in document.hand_written():
            assert line.text is not None  # hand_written() 的不变量，见 factfile
            # 整体 ~~…~~ 包裹的手写行：人已经在文件里把它"划掉"了，
            # 收编时保留这层装饰（struck），文本取内部——与 _classify
            # 对 FACT 行的判定同一逻辑。
            text = line.text
            struck = len(text) >= 4 and text.startswith("~~") and text.endswith("~~")
            if struck:
                text = text[2:-2]
            # evidence 在改写之前取：收编后 raw 变成了事实行，
            # "行本身"指的是人写下的那一行。
            evidence = line.raw.rstrip("\r\n")
            fact_id = f"f_{next_num:03d}"
            try:
                # rewrite 先渲染再赋值：渲染抛 MarkerError 时行原封不动
                # （"never guess" 的延伸——绝不写出一行写不合法的行）。
                line.rewrite(text, Marker(fact_id=fact_id, locked=True), struck=struck)
            except MarkerError as exc:
                # 号码未消耗：计数只在改写成功后才 +1，而失败的行没改成、
                # 补丁没记、盘上无痕——id 从未存在过，下一条手写行接着用。
                # （宁可少分配，不可凭空记账：一个无人认领的 id 同样违反
                # "id 一旦出现就永不复用"。）
                quarantined.append(
                    QuarantineItem(
                        location=Location(section.id, section.path, line.line_no),
                        reason=(
                            "hand-written line cannot be adopted without breaking "
                            f"the line grammar (plan §8 R15): {exc}"
                        ),
                    )
                )
                continue
            next_num += 1
            patch = ChangelogPatch(
                op=PatchOp.ADOPT,
                id=fact_id,
                target=section.path,
                kind=FactKind.STATEMENT,
                text=text,
                evidence=evidence,
                source=f"{section.path}#L{line.line_no}",
            )
            adopted.append(
                Adoption(
                    location=Location(section.id, section.path, line.line_no),
                    fact_id=fact_id,
                    text=text,
                    evidence=evidence,
                    patch=patch,
                )
            )
            # 第 5 案的结构化切片：文本完全相同即旗标。不做归一化——
            # 纯文件透明性：多一个空格就是不同的文本，语义层面的
            # "其实是一个意思"是 extract 的判断（SPEC §3，D-006）。
            for active_text, active_id in active_facts:
                if text == active_text:
                    conflicts.append(
                        Conflict(
                            fact_id=fact_id,
                            target_id=active_id,
                            detail=(
                                f"adopted locked fact {fact_id} has the same text "
                                f"as active fact {active_id} (SPEC §4: flagged for "
                                "me review, never auto-superseded in either direction)"
                            ),
                        )
                    )

    # --- 阶段 5：R9 划线不一致——只警告 ------------------------------------
    warnings: list[MarkerWarning] = []
    for section, document in documents:
        for line in document.facts():
            assert line.marker is not None
            declared = line.marker.superseded_by is not None
            if line.struck == declared:
                continue
            if line.struck:
                detail = (
                    "line is struck through but the marker declares no "
                    "superseded-by — status follows the marker (plan §8 R9)"
                )
            else:
                detail = (
                    "marker declares superseded-by but the line is not struck "
                    "through — status is superseded either way (plan §8 R9)"
                )
            warnings.append(
                MarkerWarning(
                    location=Location(section.id, section.path, line.line_no),
                    fact_id=line.marker.fact_id,
                    detail=detail,
                )
            )

    patches = tuple([item.patch for item in adopted] + [item.patch for item in retired])
    return ReconcileReport(
        adopted=tuple(adopted),
        retired=tuple(retired),
        quarantined=tuple(quarantined),
        conflicts=tuple(conflicts),
        warnings=tuple(warnings),
        patches=patches,
    )


def reconcile_dossier(dossier: Dossier) -> ReconcileScan:
    """薄壳：读档案 → 跑纯核心。**只读不写**（D-006）。

    调用方（M4 的管线）应在 :meth:`~zhiwo.store.Dossier.writer_lock`
    内调用本函数：reconcile 的"读 → 判断"窗口与写盘同锁，晚到的并发
    写入才不会让判断基于过期快照。M3 自身不做任何落盘——补建的分区
    文件、收编后的文档、changelog，全部由调用方按报告写。

    journal 分区被跳过（与 ``known_fact_ids`` 同一规则）：journal 是
    只追加的原始文本，没有 marker（SPEC §1）；journal 里提到 ``f_051``
    不等于这条事实还活在文件里——否则删掉事实、留下日志，就永远退
    不掉它了。
    """
    profile = dossier.load_profile()

    documents: list[tuple[Section, FactDocument]] = []
    created: list[Section] = []
    for section in profile.sections:
        if section.axis is Axis.JOURNAL:
            continue
        document = dossier.read_fact_document(section)
        if document is None:
            # 声明了但文件缺失（plan §5 步骤 4 的"补建"）：给一个空桩，
            # 让收编/退休对它照常工作；文件由调用方把桩写出去时创建
            # （atomic_write 自带 parent.mkdir）。
            document = FactDocument(lines=[], source=str(dossier.section_path(section)))
            created.append(section)
        documents.append((section, document))

    # 一遍 read_changelog 推出三个集合（plan §3 决策 1：全量扫描只在
    # store 的读取函数里，这里不复扫）。retire 的语义集合从**全部**
    # changelog 条目累积：事件跨条目分布（一条创建于 m_1、退休记录于
    # m_7），只看最后一条会漏。
    changelog_ids: set[str] = set()
    retired_ids: set[str] = set()
    created_ids: set[str] = set()
    for entry in dossier.read_changelog():
        for patch in entry.patches:
            if patch.id is None:
                continue
            changelog_ids.add(patch.id)
            if patch.op is PatchOp.RETIRE:
                retired_ids.add(patch.id)
            elif patch.op in _CREATION_OPS and patch.result is PatchResult.APPLIED:
                created_ids.add(patch.id)

    report = reconcile_documents(
        documents,
        changelog_ids=changelog_ids,
        retired_ids=retired_ids,
        created_ids=created_ids,
    )
    return ReconcileScan(
        report=report,
        documents=tuple(documents),
        created=tuple(created),
    )
