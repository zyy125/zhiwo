"""reconcile 的契约矩阵（M3：SPEC §4 的五种观察 + id 空间）。

三类断言（plan §7 M3 的验收列）：

- **每个表格行一个测试**：收编 / 退休 / 拒签 / 隔离 / 冲突旗标；
- **三个关键边界**：坏 marker 不误退役（R8）、退休的 id 不复用
  （plan §3 决策 4）、``[[f_id]]`` 提及不阻退休（R8）；
- **薄壳的读侧契约**：journal 永不被扫描、缺失分区补桩、文档按
  profile 序交还（M4 的交接面）。

纯核心用内存文档直测（byte 断言不碰 tmp 目录）；薄壳用黄金档案的
可写副本（conftest 的 ``dossier``）。落盘一律由测试自己调
``write_fact_document``——reconcile 是扫描不是写者（D-006），
"没有写盘"本身也是被测行为。
"""

from datetime import datetime
from pathlib import Path

import pytest

from zhiwo.errors import MergeRefused
from zhiwo.factfile import FactDocument
from zhiwo.models import (
    ChangelogEntry,
    ChangelogPatch,
    FactKind,
    PatchOp,
    PatchResult,
    Section,
)
from zhiwo.reconcile import (
    ReconcileReport,
    ReconcileScan,
    next_fact_id,
    reconcile_documents,
    reconcile_dossier,
)
from zhiwo.store import Dossier, bootstrap_dossier

# 黄金档案的行号（测试要对着它们断言 location / source 指针）：
# values.md  L7=f_001；career.md  L6=f_051；current.md  L6=f_042 L7=f_044 L8=f_043。
VALUES_F001_LINE = 7
CAREER_F051_LINE = 6


def make_section(**overrides: object) -> Section:
    """构造一个合法的 about 分区声明（与黄金 profile 的 values 同形）。"""
    base: dict[str, object] = {
        "id": "values",
        "title": "价值观与决策偏好",
        "path": "about/values.md",
        "axis": "about",
        "sensitivity": "normal",
    }
    base.update(overrides)
    return Section.model_validate(base)


VALUES = make_section()
CAREER = make_section(id="career", title="学业与职业", path="about/career.md")


def core_report(
    documents: list[tuple[Section, FactDocument]],
    *,
    changelog_ids: frozenset[str] | set[str] = frozenset(),
    retired_ids: frozenset[str] | set[str] = frozenset(),
    created_ids: frozenset[str] | set[str] = frozenset(),
) -> ReconcileReport:
    """纯核心的便捷入口：空集合是 reconcile 的合法常态（新档案）。"""
    return reconcile_documents(
        documents,
        changelog_ids=set(changelog_ids),
        retired_ids=set(retired_ids),
        created_ids=set(created_ids),
    )


def append_line(copy_dossier: Path, relative: str, line: str) -> None:
    """往黄金副本的一个事实文件末尾追加一行（模拟人的手写编辑）。"""
    path = copy_dossier / relative
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + line, encoding="utf-8")


def write_back(dossier: Dossier, scan: ReconcileScan) -> None:
    """模拟 M4 的步骤 8：全量写回薄壳交还的文档。"""
    for section, document in scan.documents:
        dossier.write_fact_document(section, document)


def rewrite_minimal_career(copy_dossier: Path) -> None:
    """把 career.md 重写成无说明注释的最小形态（构造"干净的删除场景"）。

    黄金 career.md 的头部注释里写着 ``me why f_051``——按 R8 的保守
    存在性检查，**散文注释里的提及也算在场**（见
    :func:`test_prose_comment_mention_blocks_retire`，那是刻意的安全
    方向：宁可不退，绝不误退）。需要"删掉 f_051 就真的删干净"的
    测试，先重写成最小文件。
    """
    (copy_dossier / "about" / "career.md").write_text(
        "# 学业与职业\n\n- 二面反馈：项目深度不够（2026-10-11） <!-- f_051 -->\n",
        encoding="utf-8",
    )


def delete_f051_line(copy_dossier: Path) -> None:
    """从（最小形态的）career.md 里删掉 f_051 的行——模拟人的手删。"""
    career = copy_dossier / "about" / "career.md"
    career.write_text(
        career.read_text(encoding="utf-8").replace(
            "- 二面反馈：项目深度不够（2026-10-11） <!-- f_051 -->\n", ""
        ),
        encoding="utf-8",
    )


def make_add_patch(fact_id: str, target: str) -> ChangelogPatch:
    """构造一条合法的 add 补丁（给"伪造历史"的测试用）。"""
    return ChangelogPatch(
        op=PatchOp.ADD,
        id=fact_id,
        target=target,
        kind=FactKind.STATEMENT,
        text=f"事实 {fact_id}",
        evidence=f"证据 {fact_id}",
        source="journal/2026-10-11.md#L7",
    )


# --- §4 第 1 案：收编手写行 ------------------------------------------------


def test_adopt_hand_written_line(copy_dossier: Path, dossier: Dossier) -> None:
    """手写行拿到新 id 与 locked，changelog 记 op: adopt，行本身即证据。"""
    append_line(copy_dossier, "about/career.md", "- 手写的一条新事实\n")
    scan = reconcile_dossier(dossier)

    (adoption,) = scan.report.adopted
    assert adoption.fact_id == "f_052"  # 天花板 f_051（文件 ∪ changelog）
    assert adoption.evidence == "- 手写的一条新事实"
    assert adoption.patch.op is PatchOp.ADOPT
    assert adoption.patch.id == "f_052"
    assert adoption.patch.target == "about/career.md"
    assert adoption.patch.kind is FactKind.STATEMENT  # 人写下的就是原话
    assert adoption.patch.text == "手写的一条新事实"
    assert adoption.patch.source == "about/career.md#L7"
    assert scan.report.patches == (adoption.patch,)

    # 改写后的文档：只有新行变了，其余行 byte 不变（round-trip 定律）。
    career = next(doc for section, doc in scan.documents if section.id == "career")
    assert career.render() == (
        (copy_dossier / "about/career.md")
        .read_text(encoding="utf-8")
        .removesuffix("- 手写的一条新事实\n")
        + "- 手写的一条新事实 <!-- f_052 locked -->\n"
    )
    # reconcile 是扫描不是写者（D-006）：盘上仍是人写的原行。
    assert "- 手写的一条新事实\n" in (copy_dossier / "about/career.md").read_text(encoding="utf-8")


def test_adopts_get_sequential_ids_in_profile_then_line_order(
    copy_dossier: Path, dossier: Dossier
) -> None:
    """同轮多条手写行按 profile 分区序 + 行序连号（plan §5 步骤 7）。"""
    # 先写进 career（profile 序里 values 在前）——顺序由 profile 决定，
    # 不由写入顺序决定。
    append_line(copy_dossier, "about/career.md", "- 后写的行\n")
    append_line(copy_dossier, "about/values.md", "- 先收编的行\n")
    append_line(copy_dossier, "about/values.md", "- 再收编的行\n")
    scan = reconcile_dossier(dossier)

    assert [item.fact_id for item in scan.report.adopted] == [
        "f_052",  # values 第一条手写行
        "f_053",  # values 第二条
        "f_054",  # career 的行
    ]
    values = next(doc for section, doc in scan.documents if section.id == "values")
    assert "- 先收编的行 <!-- f_052 locked -->\n" in values.render()
    assert "- 再收编的行 <!-- f_053 locked -->\n" in values.render()


def test_unadoptable_hand_written_line_is_quarantined() -> None:
    """含 ``~~`` 的手写行写不成合法事实行 → 隔离，不崩溃、不改写。

    "never guess" 的延伸（D-006）：系统可以不收编一行，但绝不能写出一行
    写侧文法不合法的东西（R15）——那等于亲手制造下一个坏 marker。
    """
    document = FactDocument.parse("- a~~b~~c\n", source="about/values.md")
    report = core_report([(VALUES, document)])

    assert not report.adopted
    (item,) = report.quarantined
    assert item.location.path == "about/values.md"
    assert item.location.line_no == 1
    assert "line grammar" in item.reason
    # 行原封不动——既没改成事实，也没被删掉。
    assert document.render() == "- a~~b~~c\n"
    # 失败的尝试不消耗 id：号没记账（无补丁、无落盘），下一条手写行
    # 接着用 f_001。
    document2 = FactDocument.parse("- a~~b~~c\n- 正常的行\n", source="a.md")
    report2 = core_report([(VALUES, document2)])
    assert [item.fact_id for item in report2.adopted] == ["f_001"]


def test_struck_hand_written_line_keeps_its_decoration() -> None:
    """人已经划掉的手写行：收编保留 struck 装饰（与 FACT 行同一判定）。"""
    document = FactDocument.parse("- ~~想放弃秋招~~\n", source="about/values.md")
    report = core_report([(VALUES, document)])

    (adoption,) = report.adopted
    assert adoption.text == "想放弃秋招"
    assert document.render() == "- ~~想放弃秋招~~ <!-- f_001 locked -->\n"


def test_empty_dossier_first_id_is_f_001(tmp_path: Path) -> None:
    """刚 bootstrap 的档案（changelog 空、文件空）收编第一条 → f_001。"""
    dossier = bootstrap_dossier(tmp_path / "fresh")
    append_line(tmp_path / "fresh", "about/identity.md", "- 第一条手写事实\n")
    scan = reconcile_dossier(dossier)

    (adoption,) = scan.report.adopted
    assert adoption.fact_id == "f_001"
    assert adoption.patch.id == "f_001"


# --- §4 第 2 案：退休（changelog 已知的 id 从所有文件消失）------------------


def test_retire_changelog_known_id_missing(copy_dossier: Path, dossier: Dossier) -> None:
    """被手删的 changelog 已知 id → op: retire，文件一个字都不写。"""
    rewrite_minimal_career(copy_dossier)
    delete_f051_line(copy_dossier)
    career = copy_dossier / "about/career.md"
    deleted = career.read_text(encoding="utf-8")
    scan = reconcile_dossier(dossier)

    (retirement,) = scan.report.retired
    assert retirement.fact_id == "f_051"
    assert retirement.patch.op is PatchOp.RETIRE
    assert retirement.patch.id == "f_051"
    assert retirement.patch.reason is not None
    assert "hand-deleted" in retirement.patch.reason
    assert retirement.patch.result is not None  # 归档在记录时已生效
    # 扫描不写者：盘上的文件保持我们删完之后的样子。
    assert career.read_text(encoding="utf-8") == deleted
    assert not scan.report.adopted


def test_wikilink_mention_does_not_block_retire(copy_dossier: Path, dossier: Dossier) -> None:
    """R8：``[[f_051]]`` 是引用不是副本——留着链接照样退休。"""
    rewrite_minimal_career(copy_dossier)
    delete_f051_line(copy_dossier)
    # current.md 的 f_043 行文本里带着 [[f_051]]（黄金档案本来就有）。
    scan = reconcile_dossier(dossier)
    assert [item.fact_id for item in scan.report.retired] == ["f_051"]


def test_prose_comment_mention_blocks_retire(copy_dossier: Path, dossier: Dossier) -> None:
    """保守方向的另一半（D-006）：散文注释里的提及也算"在场"。

    存在性检查认注释 span、不认语义——黄金 career.md 的头部注释写着
    ``me why f_051``，删掉事实行后它仍在。**宁可不退，绝不误退**：
    误退休是单向门（never resurrected），而"该退没退"下一轮还会
    再判一次。人想让它退，就把注释里的提及也删掉。
    """
    career = copy_dossier / "about/career.md"
    career.write_text(
        career.read_text(encoding="utf-8").replace(
            "- 二面反馈：项目深度不够（2026-10-11） <!-- f_051 -->\n", ""
        ),
        encoding="utf-8",
    )
    # 头部说明注释（``me why f_051``）还在 → f_051 仍算在场。
    scan = reconcile_dossier(dossier)
    assert not scan.report.retired


def test_no_false_retire_on_damaged_marker(copy_dossier: Path, dossier: Dossier) -> None:
    """R8 关键测试：id 只活在坏 marker 里（未闭合 ``<!--``）→ 不退休。"""
    rewrite_minimal_career(copy_dossier)
    delete_f051_line(copy_dossier)
    append_line(copy_dossier, "about/career.md", "- 二面反馈：项目深度不够 <!-- f_051\n")
    scan = reconcile_dossier(dossier)

    assert not scan.report.retired  # 坏 marker 里的 id 仍然"在场"
    assert scan.report.quarantined  # 但那行确实坏——报告它


def test_no_id_reuse_after_retire(copy_dossier: Path, dossier: Dossier) -> None:
    """退休的 id 留在天花板里（changelog 记账）——新手写行拿下一个号。"""
    rewrite_minimal_career(copy_dossier)
    delete_f051_line(copy_dossier)
    append_line(copy_dossier, "about/career.md", "- 顶替不了的一条新事实\n")
    scan = reconcile_dossier(dossier)

    assert [item.fact_id for item in scan.report.retired] == ["f_051"]
    (adoption,) = scan.report.adopted
    assert adoption.fact_id == "f_052"  # 不是 f_051——never resurrected


def test_retire_not_repeated_on_second_run(copy_dossier: Path, dossier: Dossier) -> None:
    """退休记录落进 changelog 后，下一轮不再重复退休（retired_ids 守门）。"""
    rewrite_minimal_career(copy_dossier)
    delete_f051_line(copy_dossier)
    first = reconcile_dossier(dossier)
    assert first.report.retired

    # 模拟 M4 步骤 9：把 reconcile 事件写进 changelog。
    entry = ChangelogEntry(
        merge="m_2026-10-19T120000",
        input="journal/2026-10-19.md",
        extractor="reconcile",
        patches=list(first.report.patches),
    )
    dossier.write_changelog(entry, now=datetime(2026, 10, 19, 12, 0, 0))

    second = reconcile_dossier(dossier)
    assert not second.report.retired


def test_whole_file_deletion_mass_retires_per_id(copy_dossier: Path, dossier: Dossier) -> None:
    """R12：整文件手删 → 逐 id 退休、逐 id 报告，不特殊处理。"""
    # 伪造历史：两条 add（f_060/f_061）都指向 values.md。
    dossier.write_changelog(
        ChangelogEntry(
            merge="m_2026-10-18T090000",
            input="journal/2026-10-18.md",
            extractor="mock-v0",
            patches=[
                make_add_patch("f_060", "about/values.md"),
                make_add_patch("f_061", "about/values.md"),
            ],
        ),
        now=datetime(2026, 10, 18, 9, 0, 0),
    )
    (copy_dossier / "about" / "values.md").unlink()

    scan = reconcile_dossier(dossier)
    assert [item.fact_id for item in scan.report.retired] == ["f_060", "f_061"]
    # 每条退休一个补丁——"响亮的逐 id 报告"。
    assert len(scan.report.patches) == 2
    # 文件独有 id（f_001 等，changelog 不知）不退休：plan §5 的字面规则。
    assert all(
        item.fact_id not in {"f_001", "f_002", "f_003", "f_009", "f_017"}
        for item in scan.report.retired
    )
    # values 分区进了补建名单。
    assert "values" in {section.id for section in scan.created}


def test_emptied_file_behaves_like_deleted(copy_dossier: Path, dossier: Dossier) -> None:
    """文件被清空（只剩标题）与被删除同判：里面的 id 全部缺席。"""
    current = copy_dossier / "goals" / "current.md"
    current.write_text("# 当前在办\n", encoding="utf-8")
    scan = reconcile_dossier(dossier)

    # f_043 有 changelog 记账 → 退休；f_042/f_044 仅存在于文件 → 不退。
    assert [item.fact_id for item in scan.report.retired] == ["f_043"]
    assert "current" not in {section.id for section in scan.created}  # 文件还在


def test_rejected_patch_id_is_never_retired(copy_dossier: Path, dossier: Dossier) -> None:
    """rejected 的补丁从未把 id 写上盘——绝不能被判"被手删"（D-006）。"""
    rejected = make_add_patch("f_070", "about/values.md").model_copy(
        update={"result": PatchResult.REJECTED, "reason": "evidence not found"}
    )
    dossier.write_changelog(
        ChangelogEntry(
            merge="m_2026-10-18T080000",
            input="journal/2026-10-18.md",
            extractor="mock-v0",
            patches=[rejected],
        ),
        now=datetime(2026, 10, 18, 8, 0, 0),
    )
    scan = reconcile_dossier(dossier)
    assert not scan.report.retired


# --- §4 第 3 案：重复 id → 拒绝整次 merge ------------------------------------


def test_duplicate_id_across_files_refuses_naming_both_locations(
    copy_dossier: Path, dossier: Dossier
) -> None:
    """跨文件复制粘贴（把 f_001 抄进 career.md）→ MergeRefused 报两处。"""
    append_line(copy_dossier, "about/career.md", "- 厌恶风险，偏好确定性 <!-- f_001 -->\n")
    with pytest.raises(MergeRefused) as excinfo:
        reconcile_dossier(dossier)

    message = str(excinfo.value)
    assert f"about/values.md:L{VALUES_F001_LINE}" in message
    assert "about/career.md:L7" in message
    assert "f_001" in message


def test_duplicate_id_within_one_file_refuses() -> None:
    """同一文件里出现两次（更隐蔽的手滑）同一规则、同一消息。"""
    text = "- 甲 <!-- f_051 -->\n- 乙 <!-- f_051 -->\n"
    document = FactDocument.parse(text, source="about/career.md")
    with pytest.raises(MergeRefused, match=r"about/career\.md:L1.*about/career\.md:L2"):
        core_report([(CAREER, document)])


def test_refusal_leaves_documents_byte_unchanged() -> None:
    """拒签发生在任何改写之前——被拒的 merge 不留半点痕迹。

    连同同轮已可见的收编候选一起作废：人必须先解决重复，下一轮
    merge 再从头来（半合并比不合并危险，SPEC §4）。
    """
    text = "- 手写行\n- 甲 <!-- f_051 -->\n- 乙 <!-- f_051 -->\n"
    document = FactDocument.parse(text, source="about/career.md")
    with pytest.raises(MergeRefused):
        core_report([(CAREER, document)])
    assert document.render() == text  # 手写行也原样——没有被"顺手"收编


# --- §4 第 4 案：隔离坏 marker ----------------------------------------------


def test_damaged_marker_quarantined_untouched(copy_dossier: Path, dossier: Dossier) -> None:
    """坏 marker → 报告位置与原因、行原样、不阻塞同轮的收编。"""
    append_line(copy_dossier, "about/career.md", "- 一条被改坏的事实 <!-- f_9 bad -->\n")
    append_line(copy_dossier, "about/career.md", "- 一条正常的手写行\n")
    scan = reconcile_dossier(dossier)

    (item,) = scan.report.quarantined
    assert item.location.path == "about/career.md"
    assert item.location.line_no == 7
    assert "invalid fact id" in item.reason
    # 坏行不阻塞：同轮的手写行照常收编。
    assert [item.fact_id for item in scan.report.adopted] == ["f_052"]
    # 坏行原封不动地留在 render 里。
    career = next(doc for section, doc in scan.documents if section.id == "career")
    assert "- 一条被改坏的事实 <!-- f_9 bad -->\n" in career.render()


# --- §4 第 5 案：冲突旗标（结构化切片，D-006）--------------------------------


def test_locked_fact_conflicting_with_active_fact_is_flagged_not_superseded(
    copy_dossier: Path, dossier: Dossier
) -> None:
    """收编的 locked 事实与 active 事实文本相同 → 旗标，绝不自动 supersede。"""
    append_line(copy_dossier, "about/career.md", "- 厌恶风险，偏好确定性\n")
    scan = reconcile_dossier(dossier)

    (adoption,) = scan.report.adopted
    assert adoption.fact_id == "f_052"
    (conflict,) = scan.report.conflicts
    assert conflict.fact_id == "f_052"
    assert conflict.target_id == "f_001"
    # 既有事实的行一个 byte 都不动：两个方向都不自动 supersede（§4）。
    values = next(doc for section, doc in scan.documents if section.id == "values")
    assert values.render() == (copy_dossier / "about" / "values.md").read_text(encoding="utf-8")


def test_differing_text_is_not_a_conflict(copy_dossier: Path, dossier: Dossier) -> None:
    """M3 的边界：文本不同就不是冲突——语义矛盾是 extract 的判断（SPEC §3）。"""
    append_line(copy_dossier, "about/career.md", "- 厌恶风险，偶尔也赌一把\n")
    scan = reconcile_dossier(dossier)
    assert not scan.report.conflicts


# --- R9：划线与 marker 不一致 → 警告，永不改写 -------------------------------


def test_struck_mismatch_is_a_warning_never_a_rewrite() -> None:
    """R9：marker 是权威，划线是渲染——不一致只警告，人改不改是人的事。"""
    text = (
        "# 标题\n"
        "- ~~被划掉但没有继任者~~ <!-- f_002 -->\n"
        "- 有继任者却没划掉 <!-- f_003 superseded-by:f_002 -->\n"
        "- ~~划掉且有继任者~~ <!-- f_009 superseded-by:f_017 -->\n"
    )
    document = FactDocument.parse(text, source="about/values.md")
    report = core_report([(VALUES, document)])

    assert [item.fact_id for item in report.warnings] == ["f_002", "f_003"]
    assert all("R9" in item.detail for item in report.warnings)
    # 一行都不改写（render 逐字节等于输入——round-trip 定律）。
    assert document.render() == text


# --- 薄壳：读侧契约（M4 的交接面）-------------------------------------------


def test_journal_is_never_scanned(copy_dossier: Path, dossier: Dossier) -> None:
    """journal 是原始话语流不是事实文件：不进文档集、不阻退休。

    journal 里写着 marker 形态的行也不能算"这条事实还活着"——否则
    删掉一条事实、留下一句日志引用，它就永远退不掉（SPEC §1）。
    """
    journal = copy_dossier / "journal" / "2026-10-11.md"
    journal.write_text(
        journal.read_text(encoding="utf-8") + "- 日志里提到 <!-- f_051 -->\n",
        encoding="utf-8",
    )
    rewrite_minimal_career(copy_dossier)
    delete_f051_line(copy_dossier)
    scan = reconcile_dossier(dossier)

    assert "journal" not in {section.id for section, _ in scan.documents}
    assert [item.fact_id for item in scan.report.retired] == ["f_051"]
    assert not scan.report.adopted  # journal 的行不会被收编


def test_missing_section_file_is_created_and_its_changelog_ids_retire(
    copy_dossier: Path, dossier: Dossier
) -> None:
    """声明了但文件缺失（plan §5 步骤 4）：补桩 + 该退休的退休。"""
    (copy_dossier / "goals" / "current.md").unlink()
    scan = reconcile_dossier(dossier)

    # 黄金副本本就缺 5 个分区文件（identity/health/…），current 现在
    # 也进了补建名单。
    assert "current" in {section.id for section in scan.created}
    # f_043 有 changelog 记账 → 退休；f_042/f_044 仅在文件 → 不退休。
    assert [item.fact_id for item in scan.report.retired] == ["f_043"]

    # 调用方写回全部文档（M4 步骤 8）→ 缺失文件以空文件补建出来。
    write_back(dossier, scan)
    assert (copy_dossier / "goals" / "current.md").read_text(encoding="utf-8") == ""


def test_scan_exposes_documents_in_profile_order_and_writes_back_cleanly(
    copy_dossier: Path, dossier: Dossier
) -> None:
    """交接契约：文档按 profile 序全量交还，写回后只有真改动可见。"""
    append_line(copy_dossier, "about/career.md", "- 手写的一条新事实\n")
    originals = {
        relative: (copy_dossier / relative).read_text(encoding="utf-8")
        for relative in (
            "about/values.md",
            "about/career.md",
            "goals/current.md",
        )
    }
    scan = reconcile_dossier(dossier)

    section_ids = [section.id for section, _ in scan.documents]
    assert section_ids == [
        "identity",
        "health",
        "relations",
        "finance",
        "values",
        "career",
        "long-term",
        "current",
    ]  # journal 不在其中
    write_back(dossier, scan)
    # 未被触碰的文件写回后逐字节不变——`me diff` 里只有收编的那一行。
    assert (copy_dossier / "about" / "values.md").read_text(encoding="utf-8") == (
        originals["about/values.md"]
    )
    assert (copy_dossier / "goals" / "current.md").read_text(encoding="utf-8") == (
        originals["goals/current.md"]
    )
    assert (copy_dossier / "about" / "career.md").read_text(encoding="utf-8") == (
        originals["about/career.md"].removesuffix("- 手写的一条新事实\n")
        + "- 手写的一条新事实 <!-- f_052 locked -->\n"
    )


def test_golden_dossier_reconciles_to_a_noop(dossier: Dossier) -> None:
    """黄金档案本身 reconciles 成 no-op：五种观察全部落空。

    这是最便宜的"契约没被自己写坏"的回归：黄金样本是 SPEC 引用的
    活例（SPEC §1/§2.1），它若触发任何 retire/conflict/warning，
    说明实现或样本有一边出了问题。
    """
    scan = reconcile_dossier(dossier)

    assert scan.report == ReconcileReport(
        adopted=(),
        retired=(),
        quarantined=(),
        conflicts=(),
        warnings=(),
        patches=(),
    )
    # 黄金副本只带 3 个事实文件：其余声明的分区进补建名单（正常状态，
    # plan §5 步骤 4——不是错误，是"还没建"）。
    assert {section.id for section in scan.created} == {
        "identity",
        "health",
        "relations",
        "finance",
        "long-term",
    }
    # 三个在盘文件 render 逐字节等于原文（round-trip 定律的现场证明）。
    for relative in ("about/values.md", "about/career.md", "goals/current.md"):
        document = next(doc for section, doc in scan.documents if section.path == relative)
        assert document.render() == (dossier.root / relative).read_text(encoding="utf-8")


# --- id 空间（plan §3 决策 4）-----------------------------------------------


@pytest.mark.parametrize(
    ("known_ids", "expected"),
    [
        (frozenset(), "f_001"),  # 空集：刚 bootstrap 的档案
        ({"f_001", "f_003"}, "f_004"),  # 不填洞——洞是历史
        ({"f_999"}, "f_1000"),  # 零填充是最小宽度，不是上限
        ({"f_051"}, "f_052"),  # 退休的 id 仍在集合里（changelog 记账）
    ],
)
def test_next_fact_id_rules(known_ids: frozenset[str], expected: str) -> None:
    """max + 1、零填充到 3 位、不填洞（plan §3 决策 4 的全部条款）。"""
    assert next_fact_id(known_ids) == expected


def test_next_fact_id_ignores_malformed_tokens() -> None:
    """防御：不合形态的 token 不进 max 计算（输入本应全部合法）。"""
    assert next_fact_id({"f_003", "not-an-id", "f_1"}) == "f_004"
