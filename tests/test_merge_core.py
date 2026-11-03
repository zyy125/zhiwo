"""merge 纯核心的契约测试（M4：验证 / 分配 id / 应用 / 证据边界）。

这些测试只用内存文档 + 原始草稿映射，不碰磁盘、不碰 git——纯核心的
承诺正是"零 I/O、零模型调用、确定性"（plan §5 步骤 6–8）。薄壳的写盘/
提交在 ``test_merge_shell.py`` 里单独验证。

重点钉住 plan §7 M4 的关键列：evidence matrix、deferred-supersede 字节
精确、拒绝被记录、locked 永不改写，外加同轮"收编 + 新补丁"的 id 天花板。
"""

from __future__ import annotations

import pytest

from zhiwo.errors import MergeRefused, ZhiwoError
from zhiwo.factfile import FactDocument
from zhiwo.merge import Capture, MergePlan, apply_patches
from zhiwo.models import (
    ChangelogPatch,
    FactKind,
    PatchOp,
    PatchResult,
    Section,
)

DEFAULT_PATH = "journal/2026-10-11.md"


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
GOALS = make_section(id="current", title="当前在办", path="goals/current.md", axis="goals")


def document(text: str, *, source: str = "about/values.md") -> FactDocument:
    return FactDocument.parse(text, source=source)


def run(
    documents: list[tuple[Section, FactDocument]],
    drafts: list[object],
    *,
    lines: tuple[str, ...] = ("原始输入",),
    known_ids: set[str] | None = None,
    capture: Capture | None = None,
) -> MergePlan:
    """纯核心的便捷入口：默认捕获区间恰好覆盖 ``lines``。"""
    if capture is None:
        capture = Capture(path=DEFAULT_PATH, first_line=1, last_line=len(lines))
    return apply_patches(
        documents,
        drafts,
        capture=capture,
        journal_lines=list(lines),
        known_ids=set(known_ids or ()),
    )


def add_draft(
    *,
    text: str = "厌恶风险",
    evidence: str = "厌恶风险",
    target: str = "about/values.md",
    kind: str = "statement",
) -> dict[str, object]:
    return {
        "op": "add",
        "target": target,
        "kind": kind,
        "text": text,
        "evidence": evidence,
    }


def supersede_draft(
    target_id: str,
    *,
    text: str = "决定先工作",
    evidence: str = "决定先工作",
    kind: str = "statement",
) -> dict[str, object]:
    return {
        "op": "supersede",
        "target_id": target_id,
        "kind": kind,
        "text": text,
        "evidence": evidence,
    }


def link_draft(from_id: str, to_id: str) -> dict[str, object]:
    return {"op": "link", "from": from_id, "to": to_id}


def only(plan: MergePlan) -> ChangelogPatch:
    (patch,) = plan.patches
    return patch


# --- add：追加与 pending ----------------------------------------------------


def test_add_statement_appends_an_active_fact() -> None:
    doc = document("# 价值观\n")
    plan = run(
        [(VALUES, doc)],
        [add_draft(evidence="我厌恶风险")],
        lines=("我厌恶风险，偏好确定性。",),
    )

    assert doc.render() == "# 价值观\n- 厌恶风险 <!-- f_001 -->\n"
    patch = only(plan)
    assert patch.op is PatchOp.ADD
    assert patch.id == "f_001"
    assert patch.target == "about/values.md"
    assert patch.kind is FactKind.STATEMENT
    assert patch.source == f"{DEFAULT_PATH}#L1"
    assert patch.result is PatchResult.APPLIED
    assert plan.assigned_ids == ("f_001",)


def test_add_inference_enters_pending() -> None:
    doc = document("# 价值观\n")
    plan = run(
        [(VALUES, doc)],
        [add_draft(kind="inference", text="也许该换城市", evidence="也许该换城市")],
        lines=("也许该换城市。",),
    )

    assert doc.render() == "# 价值观\n- 也许该换城市 <!-- f_001 pending -->\n"
    assert only(plan).kind is FactKind.INFERENCE


def test_add_id_continues_from_known_ids() -> None:
    doc = document("# 价值观\n")
    plan = run(
        [(VALUES, doc)],
        [add_draft()],
        lines=("厌恶风险",),
        known_ids={"f_051"},
    )
    assert only(plan).id == "f_052"


def test_adopted_ids_are_part_of_the_ceiling_before_an_add() -> None:
    """同轮收编 f_052 后，add 必须拿 f_053——不是 f_052（SPEC §4 重复 id）。"""
    doc = document("# 价值观\n- 手写的行 <!-- f_052 locked -->\n")
    plan = run(
        [(VALUES, doc)],
        [add_draft()],
        lines=("厌恶风险",),
        known_ids={"f_001", "f_052"},
    )
    assert only(plan).id == "f_053"
    assert "f_053" in doc.render()


# --- 拒绝是数据，不是异常 ---------------------------------------------------


def test_rejected_draft_does_not_consume_an_id() -> None:
    doc = document("# 价值观\n")
    plan = run(
        [(VALUES, doc)],
        [add_draft(evidence="并不存在于输入"), add_draft()],
        lines=("厌恶风险",),
    )

    rejected, applied = plan.patches
    assert rejected.result is PatchResult.REJECTED
    assert rejected.id is None
    assert rejected.reason is not None
    assert "verbatim" in rejected.reason
    assert applied.id == "f_001"  # 被拒的不占号
    assert plan.assigned_ids == ("f_001",)


def test_rejected_add_records_whatever_fields_are_well_typed() -> None:
    """缺 evidence 的 add 仍能进 changelog：拒绝记录的权威是 reason。"""
    doc = document("# 价值观\n")
    draft = add_draft()
    del draft["evidence"]
    plan = run([(VALUES, doc)], [draft], lines=("厌恶风险",))

    patch = only(plan)
    assert patch.result is PatchResult.REJECTED
    assert patch.target == "about/values.md"
    assert patch.text == "厌恶风险"
    assert patch.id is None
    assert patch.reason is not None and "evidence" in patch.reason


def test_draft_id_and_source_are_ignored() -> None:
    """草稿里的 id/source 一律忽略：身份与 provenance 只能由 merger 产生。"""
    doc = document("# 价值观\n")
    draft = add_draft() | {"id": "f_999", "source": "journal/fake.md#L999"}
    plan = run([(VALUES, doc)], [draft], lines=("厌恶风险",))

    patch = only(plan)
    assert patch.id == "f_001"
    assert patch.source == f"{DEFAULT_PATH}#L1"


# --- evidence matrix --------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "first_line", "evidence", "expected_source"),
    [
        # 精确 CJK 子串；定位到第一行
        (("我厌恶风险，偏好确定性。",), 6, "厌恶风险，偏好确定性", f"{DEFAULT_PATH}#L6"),
        # 空白折叠：多空格、全角空格折叠成一个普通空格
        (("我  厌恶\u3000风险",), 6, "我 厌恶 风险", f"{DEFAULT_PATH}#L6"),
        # 跨行：逐行没命中，拼接后命中，归到起始行
        (("第一行", "第二行"), 9, "第一行 第二行", f"{DEFAULT_PATH}#L9"),
        # 首次出现优先
        (("重复 内容", "重复 内容"), 6, "重复 内容", f"{DEFAULT_PATH}#L6"),
        # 证据两端空白属于噪音
        (("我厌恶风险",), 6, "  厌恶风险  ", f"{DEFAULT_PATH}#L6"),
        # 大小写敏感（不做 casefold）
        (("Offer 来了",), 6, "offer 来了", None),
        # 纯空白/空证据
        (("有内容",), 6, "   ", None),
        # 同义改写不是逐字证据
        (("我厌恶风险",), 6, "我不喜欢风险", None),
        # 证据必须落在本次捕获区间内
        (("第二行",), 2, "第一行", None),
    ],
)
def test_evidence_matrix(
    lines: tuple[str, ...],
    first_line: int,
    evidence: str,
    expected_source: str | None,
) -> None:
    doc = document("# 价值观\n")
    capture = Capture(
        path=DEFAULT_PATH,
        first_line=first_line,
        last_line=first_line + len(lines) - 1,
    )
    plan = run(
        [(VALUES, doc)],
        [add_draft(text="一条事实", evidence=evidence)],
        lines=lines,
        capture=capture,
    )

    patch = only(plan)
    if expected_source is None:
        assert patch.result is PatchResult.REJECTED
        assert patch.id is None
        assert doc.render() == "# 价值观\n"  # 被拒不写盘
    else:
        assert patch.result is PatchResult.APPLIED
        assert patch.source == expected_source


def test_evidence_source_uses_absolute_line_numbers() -> None:
    """捕获从 L9 起，第二条输入的证据必须落到 L#10，而不是相对行号。"""
    doc = document("# 价值观\n")
    capture = Capture(path=DEFAULT_PATH, first_line=9, last_line=10)
    plan = run(
        [(VALUES, doc)],
        [add_draft(text="第二条", evidence="第二条")],
        lines=("第一条", "第二条"),
        capture=capture,
    )
    assert only(plan).source == f"{DEFAULT_PATH}#L10"


# --- supersede：立即应用 vs 延迟 --------------------------------------------


def test_supersede_statement_rewrites_target_and_appends() -> None:
    original = "# 当前\n- 想读博 <!-- f_009 -->\n"
    doc = document(original, source="goals/current.md")
    plan = run(
        [(GOALS, doc)],
        [supersede_draft("f_009", text="决定直接工作", evidence="决定直接工作")],
        lines=("决定直接工作。",),
        known_ids={"f_009"},
    )

    assert doc.render() == (
        "# 当前\n"
        "- ~~想读博~~ <!-- f_009 superseded-by:f_010 -->\n"
        "- 决定直接工作 <!-- f_010 supersedes:f_009 -->\n"
    )
    patch = only(plan)
    assert patch.op is PatchOp.SUPERSEDE
    assert patch.id == "f_010"
    assert patch.target_id == "f_009"


def test_supersede_inference_keeps_the_target_byte_identical() -> None:
    """M4 关键测试：pending supersede 只写新行，目标一个字节都不动。"""
    original = "# 当前\n- 想读博 <!-- f_009 -->\n"
    doc = document(original, source="goals/current.md")
    plan = run(
        [(GOALS, doc)],
        [
            supersede_draft(
                "f_009", kind="inference", text="也许该直接工作", evidence="也许该直接工作"
            )
        ],
        lines=("也许该直接工作。",),
        known_ids={"f_009"},
    )

    assert doc.render() == (original + "- 也许该直接工作 <!-- f_010 supersedes:f_009 pending -->\n")
    assert doc.render().startswith(original)
    assert only(plan).kind is FactKind.INFERENCE


def test_supersede_preserves_an_existing_supersedes_edge() -> None:
    """A supersedes B 之后 C 再取代 A：B 的边不能丢。"""
    doc = document(
        "# 当前\n- 新事实 <!-- f_010 supersedes:f_009 -->\n",
        source="goals/current.md",
    )
    run(
        [(GOALS, doc)],
        [supersede_draft("f_010", text="更新的决定", evidence="更新的决定")],
        lines=("更新的决定。",),
        known_ids={"f_009", "f_010"},
    )
    assert "- ~~新事实~~ <!-- f_010 supersedes:f_009 superseded-by:f_011 -->\n" in doc.render()


@pytest.mark.parametrize(
    ("marker", "status"),
    [
        ("f_002 pending", "pending"),
        ("f_003 superseded-by:f_004", "superseded"),
    ],
)
def test_supersede_requires_an_active_target(marker: str, status: str) -> None:
    doc = document(f"# 当前\n- 旧事实 <!-- {marker} -->\n", source="goals/current.md")
    target_id = marker.split()[0]
    plan = run(
        [(GOALS, doc)],
        [supersede_draft(target_id, text="新事实", evidence="新事实")],
        lines=("新事实",),
        known_ids={target_id},
    )

    patch = only(plan)
    assert patch.result is PatchResult.REJECTED
    assert patch.reason is not None and status in patch.reason
    assert doc.render() == f"# 当前\n- 旧事实 <!-- {marker} -->\n"


def test_supersede_missing_target_is_rejected() -> None:
    doc = document("# 当前\n")
    plan = run(
        [(GOALS, doc)],
        [supersede_draft("f_099", text="新事实", evidence="新事实")],
        lines=("新事实",),
    )
    patch = only(plan)
    assert patch.result is PatchResult.REJECTED
    assert patch.reason is not None and "does not exist" in patch.reason


def test_supersede_locked_target_is_rejected_and_flagged() -> None:
    """硬规则 5：locked 事实绝不改写；冲突只旗标。"""
    original = "# 当前\n- 厌恶风险 <!-- f_009 locked -->\n"
    doc = document(original, source="goals/current.md")
    for kind in ("statement", "inference"):
        plan = run(
            [(GOALS, doc)],
            [supersede_draft("f_009", kind=kind, text="新事实", evidence="新事实")],
            lines=("新事实",),
            known_ids={"f_009"},
        )
        patch = only(plan)
        assert patch.result is PatchResult.REJECTED
        assert patch.reason is not None and "locked" in patch.reason
        (conflict,) = plan.conflicts
        assert conflict.fact_id == "f_009"
        assert doc.render() == original  # 两种 kind 都逐字节不变


def test_two_statement_supersedes_of_one_target_first_wins() -> None:
    doc = document("# 当前\n- 想读博 <!-- f_009 -->\n", source="goals/current.md")
    plan = run(
        [(GOALS, doc)],
        [
            supersede_draft("f_009", text="第一条", evidence="第一条"),
            supersede_draft("f_009", text="第二条", evidence="第二条"),
        ],
        lines=("第一条", "第二条"),
        known_ids={"f_009"},
    )

    applied, rejected = plan.patches
    assert applied.result is PatchResult.APPLIED
    assert rejected.result is PatchResult.REJECTED
    assert rejected.reason is not None and "active" in rejected.reason
    assert doc.render().count("第一条") == 1
    assert "第二条" not in doc.render()


def test_two_inference_supersedes_of_one_target_both_stay_pending() -> None:
    """OQ#4：并发的 pending supersede 允许共存；确认时的先后是 M8 的事。"""
    doc = document("# 当前\n- 想读博 <!-- f_009 -->\n", source="goals/current.md")
    plan = run(
        [(GOALS, doc)],
        [
            supersede_draft("f_009", kind="inference", text="第一条", evidence="第一条"),
            supersede_draft("f_009", kind="inference", text="第二条", evidence="第二条"),
        ],
        lines=("第一条", "第二条"),
        known_ids={"f_009"},
    )

    assert all(patch.result is PatchResult.APPLIED for patch in plan.patches)
    assert "supersedes:f_009 pending" in doc.render()
    assert doc.render().count("supersedes:f_009 pending") == 2
    assert "- 想读博 <!-- f_009 -->\n" in doc.render()  # 目标仍 active


# --- link -------------------------------------------------------------------


def test_link_appends_a_wikilink_and_preserves_the_marker() -> None:
    doc = document("# 当前\n- 事实甲 <!-- f_001 -->\n- 事实乙 <!-- f_002 -->\n")
    plan = run([(VALUES, doc)], [link_draft("f_001", "f_002")], lines=("noop",))

    assert "- 事实甲 [[f_002]] <!-- f_001 -->\n" in doc.render()
    patch = only(plan)
    assert patch.op is PatchOp.LINK
    assert patch.from_fact == "f_001"
    assert patch.to == "f_002"


def test_duplicate_link_is_skipped_not_reapplied() -> None:
    doc = document("# 当前\n- 事实甲 <!-- f_001 -->\n- 事实乙 <!-- f_002 -->\n")
    plan = run(
        [(VALUES, doc)],
        [link_draft("f_001", "f_002"), link_draft("f_001", "f_002")],
        lines=("noop",),
    )

    applied, skipped = plan.patches
    assert applied.result is PatchResult.APPLIED
    assert skipped.result is PatchResult.SKIPPED
    assert skipped.reason is not None and "already present" in skipped.reason
    assert doc.render().count("[[f_002]]") == 1


def test_link_from_locked_is_rejected_and_flagged() -> None:
    original = "# 当前\n- 锁定 <!-- f_001 locked -->\n- 另一条 <!-- f_002 -->\n"
    doc = document(original)
    plan = run([(VALUES, doc)], [link_draft("f_001", "f_002")], lines=("noop",))

    patch = only(plan)
    assert patch.result is PatchResult.REJECTED
    (conflict,) = plan.conflicts
    assert conflict.fact_id == "f_001"
    assert doc.render() == original


def test_link_to_locked_is_allowed_but_never_touches_it() -> None:
    original = "# 当前\n- 锁定 <!-- f_001 locked -->\n- 另一条 <!-- f_002 -->\n"
    doc = document(original)
    plan = run([(VALUES, doc)], [link_draft("f_002", "f_001")], lines=("noop",))

    assert only(plan).result is PatchResult.APPLIED
    assert "- 锁定 <!-- f_001 locked -->\n" in doc.render()  # 锁定行未动
    assert "- 另一条 [[f_001]] <!-- f_002 -->\n" in doc.render()


def test_link_self_is_rejected() -> None:
    doc = document("# 当前\n- 事实甲 <!-- f_001 -->\n")
    plan = run([(VALUES, doc)], [link_draft("f_001", "f_001")], lines=("noop",))
    assert only(plan).result is PatchResult.REJECTED
    assert "differ" in (only(plan).reason or "")


@pytest.mark.parametrize(
    "draft",
    [
        {"op": "link", "from": "f_099", "to": "f_001"},
        {"op": "link", "from": "f_001", "to": "f_099"},
        {"op": "link", "from": "f_001"},
        {"op": "link", "to": "f_001"},
    ],
)
def test_link_missing_or_unknown_endpoint_is_rejected(draft: dict[str, object]) -> None:
    doc = document("# 当前\n- 事实甲 <!-- f_001 -->\n")
    plan = run([(VALUES, doc)], [draft], lines=("noop",))
    assert only(plan).result is PatchResult.REJECTED
    assert doc.render() == "# 当前\n- 事实甲 <!-- f_001 -->\n"


# --- 目标、文本、操作集合 ---------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["journal/2026-10-11.md", "about/nope.md", "about/values.md/", "", None],
)
def test_add_rejects_undeclared_or_journal_targets(target: str | None) -> None:
    doc = document("# 价值观\n")
    draft = add_draft()
    if target is None:
        del draft["target"]
    else:
        draft["target"] = target
    plan = run([(VALUES, doc)], [draft], lines=("厌恶风险",))
    assert only(plan).result is PatchResult.REJECTED
    assert doc.render() == "# 价值观\n"


@pytest.mark.parametrize(
    "text",
    ["带~~划线~~", "带\n换行", "带 <!-- 注释", " 前导空格", "尾随空格 ", ""],
)
def test_add_rejects_text_that_breaks_the_line_grammar(text: str) -> None:
    doc = document("# 价值观\n")
    plan = run([(VALUES, doc)], [add_draft(text=text, evidence="厌恶风险")], lines=("厌恶风险",))
    patch = only(plan)
    assert patch.result is PatchResult.REJECTED
    assert patch.id is None
    assert doc.render() == "# 价值观\n"


@pytest.mark.parametrize("op", ["compress", "adopt", "retire", "confirm", "edit", "reject"])
def test_unsupported_ops_are_rejected_not_applied(op: str) -> None:
    doc = document("# 价值观\n")
    plan = run(
        [(VALUES, doc)],
        [{"op": op, "id": "f_050", "members": ["f_001"], "text": "压缩"}],
        lines=("noop",),
    )
    patch = only(plan)
    assert patch.op is PatchOp(op)
    assert patch.result is PatchResult.REJECTED
    assert patch.id is None
    assert patch.reason is not None


@pytest.mark.parametrize("draft", [{"op": "bogus"}, {}, "not a mapping"])
def test_unidentifiable_drafts_are_malformed_not_recorded(draft: object) -> None:
    """连 op 都认不出就没有可记录的补丁形态——只能报告给 CLI。"""
    doc = document("# 价值观\n")
    plan = run([(VALUES, doc)], [draft], lines=("noop",))
    assert plan.patches == ()
    (item,) = plan.malformed
    assert item.index == 0
    assert item.detail
    assert doc.render() == "# 价值观\n"


# --- 语法/前置条件/防御 -----------------------------------------------------


def test_supersede_target_with_inner_strikethrough_is_rejected() -> None:
    """解析得出来但渲染不回去的目标文本：拒绝，别留下半改写。"""
    doc = document("# 当前\n- a~~b~~c <!-- f_009 -->\n", source="goals/current.md")
    plan = run(
        [(GOALS, doc)],
        [supersede_draft("f_009", text="新事实", evidence="新事实")],
        lines=("新事实",),
        known_ids={"f_009"},
    )
    assert only(plan).result is PatchResult.REJECTED
    assert doc.render() == "# 当前\n- a~~b~~c <!-- f_009 -->\n"


def test_link_from_unrenderable_text_is_rejected() -> None:
    doc = document("# 当前\n- a~~b~~c <!-- f_001 -->\n- 正常 <!-- f_002 -->\n")
    plan = run([(VALUES, doc)], [link_draft("f_001", "f_002")], lines=("noop",))
    assert only(plan).result is PatchResult.REJECTED
    assert doc.render() == "# 当前\n- a~~b~~c <!-- f_001 -->\n- 正常 <!-- f_002 -->\n"


def test_duplicate_ids_are_refused_before_any_mutation() -> None:
    doc = document("# 当前\n- 甲 <!-- f_001 -->\n- 乙 <!-- f_001 -->\n")
    with pytest.raises(MergeRefused, match="f_001"):
        run([(VALUES, doc)], [add_draft(evidence="厌恶风险")], lines=("厌恶风险",))


def test_journal_line_count_must_match_the_capture_range() -> None:
    doc = document("# 价值观\n")
    capture = Capture(path=DEFAULT_PATH, first_line=1, last_line=2)
    with pytest.raises(ZhiwoError, match="line"):
        apply_patches(
            [(VALUES, doc)],
            [],
            capture=capture,
            journal_lines=["only one line"],
            known_ids=set(),
        )


def test_capture_rejects_bad_paths_and_ranges() -> None:
    with pytest.raises(ZhiwoError):
        Capture(path="../escape.md", first_line=1, last_line=1)
    with pytest.raises(ZhiwoError):
        Capture(path=DEFAULT_PATH, first_line=0, last_line=1)
    with pytest.raises(ZhiwoError):
        Capture(path=DEFAULT_PATH, first_line=2, last_line=1)


def test_crlf_files_stay_crlf() -> None:
    doc = FactDocument.parse("# 价值观\r\n- 旧事实 <!-- f_009 -->\r\n", source="about/values.md")
    run(
        [(VALUES, doc)],
        [add_draft(text="新事实", evidence="新事实")],
        lines=("新事实",),
        known_ids={"f_009"},
    )
    assert doc.render().endswith("- 新事实 <!-- f_010 -->\r\n")
    assert "\n" not in doc.render().replace("\r\n", "")  # 没有混入裸 LF
