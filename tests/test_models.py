"""词汇表模型的校验矩阵（M1）。

这里覆盖的是"结构是否说得通"与"契约明文里最硬的那几条"：
journal 永不降级（SPEC §5）、分区 id 唯一（SPEC §1）、
按 op 分派必填字段（SPEC §3，硬规则 4 的第一道闸门）。

测试统一用 ``model_validate(dict)`` 而不是关键字构造：一来这正是
``schema.py`` 喂数据的方式（YAML 解析出来就是 dict），二来在 mypy strict
下不必为字典解包写类型体操。
"""

import pytest
from pydantic import ValidationError

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


def make_section(**overrides: object) -> dict[str, object]:
    """构造一个合法的 about 分区声明，便于各用例只改关心的字段。"""
    base: dict[str, object] = {
        "id": "values",
        "title": "价值观",
        "path": "about/values.md",
        "axis": "about",
        "sensitivity": "normal",
    }
    base.update(overrides)
    return base


def make_profile(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"sections": [make_section()]}
    base.update(overrides)
    return base


# --- Section：journal 敏感度规则（SPEC §5，M1 关键测试）---------------------


def test_journal_sensitivity_defaults_to_high() -> None:
    """SPEC §5"journal 是 high by default"：省略即 high。"""
    section = Section.model_validate(
        make_section(id="journal", path="journal/", axis="journal", sensitivity=None)
    )
    assert section.sensitivity is Sensitivity.HIGH


def test_journal_explicit_high_is_accepted() -> None:
    section = Section.model_validate(
        make_section(id="journal", path="journal/", axis="journal", sensitivity="high")
    )
    assert section.sensitivity is Sensitivity.HIGH


def test_journal_downgrade_is_rejected() -> None:
    """M1 关键测试：journal 被显式降级必须被拒绝（SPEC §5 永不降级）。"""
    with pytest.raises(ValidationError, match="may not be downgraded"):
        Section.model_validate(
            make_section(id="journal", path="journal/", axis="journal", sensitivity="normal")
        )


def test_non_journal_section_requires_explicit_sensitivity() -> None:
    """SPEC §5 要求每个分区都标注敏感度；省略不得静默落到 normal。"""
    with pytest.raises(ValidationError, match="must declare 'sensitivity'"):
        Section.model_validate(make_section(id="health", path="about/health.md", sensitivity=None))


@pytest.mark.parametrize("bad", ["secret", "HIGH", 1, True])
def test_unknown_sensitivity_is_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        Section.model_validate(make_section(sensitivity=bad))


@pytest.mark.parametrize("bad", ["diary", "About", ""])
def test_unknown_axis_is_rejected(bad: object) -> None:
    """轴是封闭枚举：否则 ``axis: diary`` 就能绕过 §5 的 journal 保护。"""
    with pytest.raises(ValidationError):
        Section.model_validate(make_section(axis=bad))


@pytest.mark.parametrize("bad_id", ["Values", "values_2", "-values", ""])
def test_section_id_pattern_is_enforced(bad_id: object) -> None:
    with pytest.raises(ValidationError):
        Section.model_validate(make_section(id=bad_id))


def test_unknown_section_key_is_rejected() -> None:
    """手滑拼错键必须炸，而不是静默丢掉一个分区的敏感度标注。"""
    with pytest.raises(ValidationError):
        Section.model_validate(make_section(sensitvity="high"))


def test_section_path_is_not_checked_for_existence() -> None:
    """L1 只校验声明，不碰磁盘（plan §5 步骤 4 才创建缺失的分区文件）。"""
    section = Section.model_validate(make_section(id="identity", path="about/identity.md"))
    assert section.path == "about/identity.md"


# --- Profile：预算与布局（SPEC §1 / §6）------------------------------------


def test_core_budget_defaults_to_4000() -> None:
    assert Profile.model_validate(make_profile()).core_budget_tokens == 4000


def test_core_budget_can_be_overridden() -> None:
    profile = Profile.model_validate(make_profile(core_budget_tokens=8000))
    assert profile.core_budget_tokens == 8000


@pytest.mark.parametrize("bad", [0, -1, 1.5, "lots"])
def test_invalid_core_budget_is_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(make_profile(core_budget_tokens=bad))


def test_duplicate_section_ids_are_rejected() -> None:
    """M1 关键测试：分区 id 重复必须被拒绝（SPEC §1）。"""
    duplicated = [make_section(), make_section(path="about/career.md")]
    with pytest.raises(ValidationError, match="duplicate section id"):
        Profile.model_validate(make_profile(sections=duplicated))


def test_duplicate_section_paths_are_rejected() -> None:
    """两个分区指向同一文件会让敏感度过滤产生歧义（SPEC §1）。"""
    collided = [make_section(), make_section(id="career")]
    with pytest.raises(ValidationError, match="duplicate section path"):
        Profile.model_validate(make_profile(sections=collided))


def test_two_journal_sections_are_rejected() -> None:
    two_journals = [
        make_section(id="journal", path="journal/", axis="journal", sensitivity="high"),
        make_section(id="diary", path="diary/", axis="journal", sensitivity="high"),
    ]
    with pytest.raises(ValidationError, match="at most one journal section"):
        Profile.model_validate(make_profile(sections=two_journals))


def test_empty_sections_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(make_profile(sections=[]))


def test_unknown_profile_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(make_profile(section="typo"))


def test_example_profile_shape_validates() -> None:
    """与 examples/me/profile.yaml 同构的最小档案：9 个分区的顺序与敏感度。"""
    sections = [
        make_section(id="identity", path="about/identity.md"),
        make_section(id="health", path="about/health.md", sensitivity="high"),
        make_section(id="values", path="about/values.md"),
        make_section(id="career", path="about/career.md"),
        make_section(id="current", path="goals/current.md", axis="goals"),
        make_section(id="journal", path="journal/", axis="journal", sensitivity="high"),
    ]
    profile = Profile.model_validate(make_profile(sections=sections))
    assert [s.id for s in profile.sections] == [
        "identity",
        "health",
        "values",
        "career",
        "current",
        "journal",
    ]
    assert profile.sections[1].sensitivity is Sensitivity.HIGH
    assert profile.sections[5].axis is Axis.JOURNAL


# --- ChangelogEntry / ChangelogPatch（SPEC §1 / §3，OQ#3）-------------------


def make_add_patch(**overrides: object) -> dict[str, object]:
    """黄金条目里那个 statement add 补丁的形态（examples/me/changelog）。"""
    base: dict[str, object] = {
        "op": "add",
        "id": "f_051",
        "target": "about/career.md",
        "kind": "statement",
        "text": "二面反馈：项目深度不够（2026-10-11）",
        "evidence": "面试挂了，二面，反馈说项目深度不够。",
        "source": "journal/2026-10-11.md#L7",
    }
    base.update(overrides)
    return base


def test_schema_version_defaults_to_1() -> None:
    """OQ#3 的 MVP 答案：缺字段视为 1（plan §3 决策 6）。"""
    entry = ChangelogEntry.model_validate(
        {"merge": "m_1", "input": "journal/2026-10-11.md", "extractor": "mock-v0"}
    )
    assert entry.schema_version == 1


def test_explicit_schema_version_is_kept() -> None:
    entry = ChangelogEntry.model_validate(
        {
            "merge": "m_1",
            "input": "journal/2026-10-11.md",
            "extractor": "mock-v0",
            "schema_version": 2,
        }
    )
    assert entry.schema_version == 2


def test_empty_patches_are_allowed() -> None:
    """没提取出事实的捕获仍留下记录：journal 已落盘即持久（plan §5 步骤 2）。"""
    entry = ChangelogEntry.model_validate(
        {
            "merge": "m_1",
            "input": "journal/2026-10-11.md",
            "extractor": "mock-v0",
            "patches": [],
        }
    )
    assert entry.patches == []


def test_unknown_changelog_keys_are_ignored() -> None:
    """前向容忍：数据要比实现活得久，未来加的字段不能崩掉旧读取器。"""
    entry = ChangelogEntry.model_validate(
        {
            "merge": "m_1",
            "input": "journal/2026-10-11.md",
            "extractor": "mock-v0",
            "future_field": {"reconcile": []},
        }
    )
    assert not hasattr(entry, "future_field")


def test_add_patch_parses_golden_shape() -> None:
    patch = ChangelogPatch.model_validate(make_add_patch())
    assert patch.op is PatchOp.ADD
    assert patch.id == "f_051"
    assert patch.kind is FactKind.STATEMENT
    assert patch.source == "journal/2026-10-11.md#L7"
    assert patch.result is PatchResult.APPLIED  # 缺省即已应用（决策 10）


def test_add_patch_without_evidence_is_rejected() -> None:
    """硬规则 4 的第一道闸门：缺 evidence 的 add 在结构层就死掉。"""
    with pytest.raises(ValidationError, match="requires"):
        ChangelogPatch.model_validate(make_add_patch(evidence=None))


def test_supersede_patch_requires_target_id() -> None:
    with pytest.raises(ValidationError, match="target_id"):
        ChangelogPatch.model_validate(make_add_patch(op="supersede", target=None))


def test_compress_patch_requires_members_not_evidence() -> None:
    """SPEC §3：compress 的权威是成员集（证据要求变成成员合法性）。"""
    with pytest.raises(ValidationError, match="members"):
        ChangelogPatch.model_validate({"op": "compress", "id": "f_070", "text": "压缩后的事实"})
    patch = ChangelogPatch.model_validate(
        {"op": "compress", "id": "f_070", "text": "压缩后的事实", "members": ["f_011"]}
    )
    assert patch.members == ["f_011"]


def test_link_patch_accepts_from_alias_and_field_name() -> None:
    """``from`` 是 Python 关键字：YAML 侧用 from，模型侧用 from_fact。"""
    patch = ChangelogPatch.model_validate({"op": "link", "from": "f_042", "to": "f_017"})
    assert patch.from_fact == "f_042"
    assert patch.to == "f_017"
    assert patch.model_dump(by_alias=True)["from"] == "f_042"


def test_link_patch_requires_both_ends() -> None:
    with pytest.raises(ValidationError, match="to"):
        ChangelogPatch.model_validate({"op": "link", "from": "f_042"})


@pytest.mark.parametrize("bad_id", ["f_01", "f_0099x", "009", "F_009"])
def test_fact_id_pattern_is_enforced(bad_id: object) -> None:
    """零填充至少 3 位（SPEC §2.1 的 f_017；plan §3 决策 4）。"""
    with pytest.raises(ValidationError):
        ChangelogPatch.model_validate(make_add_patch(id=bad_id))


def test_rejected_patch_records_reason() -> None:
    """plan §3 决策 10：被拒的补丁进 changelog，带 result 与理由。"""
    patch = ChangelogPatch.model_validate(
        make_add_patch(result="rejected", reason="evidence not found in input")
    )
    assert patch.result is PatchResult.REJECTED
    assert patch.reason == "evidence not found in input"


def test_unknown_patch_keys_are_ignored() -> None:
    patch = ChangelogPatch.model_validate(make_add_patch(confidence=0.9))
    assert not hasattr(patch, "confidence")


@pytest.mark.parametrize(
    ("field", "value"),
    [("evidence", ""), ("text", ""), ("source", ""), ("target", "")],
)
def test_empty_strings_are_not_accepted_as_present(field: str, value: str) -> None:
    """空串是"看起来填了"的缺席——尤其 evidence: "" 不可能是逐字引文。

    只查 None 会把这类输入放过去，然后在 M4 变成一条语焉不详的拒绝；
    硬规则 4 的第一道闸门应当在这里就明确地拦下来。
    """
    with pytest.raises(ValidationError):
        ChangelogPatch.model_validate(make_add_patch(**{field: value}))


def test_empty_members_list_is_rejected() -> None:
    """compress 的权威是成员集：空集合意味着压缩没有任何可回溯来源。"""
    with pytest.raises(ValidationError):
        ChangelogPatch.model_validate(
            {"op": "compress", "id": "f_070", "text": "压缩后的事实", "members": []}
        )


@pytest.mark.parametrize(
    ("op", "payload"),
    [
        (
            "adopt",
            {
                "id": "f_052",
                "target": "about/career.md",
                "kind": "statement",
                "text": "手写的一条事实",
                "evidence": "- 手写的一条事实",
                "source": "about/career.md#L4",
            },
        ),
        ("retire", {"id": "f_051", "reason": "fact line missing from every fact file"}),
    ],
)
def test_reconcile_ops_are_readable(op: str, payload: dict[str, object]) -> None:
    """SPEC §4 的 adopt / retire 属于契约词汇，读取器今天就得认识。

    与 compress（v0.5 才实现）同理：changelog 是长期格式，"读取器必须
    认识契约里的每个 op"不该只对 §3 的 op 生效。M3 起这两个 op 也有
    必填字段（D-006）：adopt 形态见本参数化，retire 只需 id + reason。
    """
    patch = ChangelogPatch.model_validate({"op": op, **payload})
    assert patch.op.value == op
    assert patch.result is PatchResult.APPLIED  # 归档/收编在记录时已生效


def test_adopt_patch_without_evidence_is_rejected() -> None:
    """硬规则 4 不因"这是人写的行"而豁免：adopt 的 evidence 就是行本身。"""
    with pytest.raises(ValidationError, match="evidence"):
        ChangelogPatch.model_validate(
            {
                "op": "adopt",
                "id": "f_052",
                "target": "about/career.md",
                "kind": "statement",
                "text": "手写的一条事实",
                "source": "about/career.md#L4",
            }
        )


def test_retire_patch_requires_reason() -> None:
    """retire 没有 evidence 可引用（事实已不在盘上），可引用的是 reason。

    一个既无 evidence 又无 reason 的 retire 记录什么也没说——`me why`
    的整条链路会在这里断掉。
    """
    with pytest.raises(ValidationError, match="reason"):
        ChangelogPatch.model_validate({"op": "retire", "id": "f_051"})


def test_writer_recipe_emits_contract_keys() -> None:
    """钉住给 M2 的写盘配方：``mode="json", by_alias=True``。

    漏掉 by_alias 会写出 ``from_fact:``，而契约键是 ``from``（SPEC §3）；
    ChangelogEntry 的 docstring 因此把这条写成硬性要求。
    """
    patch = ChangelogPatch.model_validate({"op": "link", "from": "f_042", "to": "f_017"})
    dumped = patch.model_dump(mode="json", by_alias=True)
    assert dumped["from"] == "f_042"
    assert "from_fact" not in dumped
    assert dumped["op"] == "link"  # mode="json" 下枚举已降为字符串
    assert dumped["result"] == "applied"


def test_clearance_and_fact_status_vocabulary() -> None:
    """词汇完整性：读者权限与事实状态的取值集合不因 M2 重构而变。"""
    assert {c.value for c in Clearance} == {"normal", "high"}
    assert {s.value for s in FactStatus} == {
        "active",
        "pending",
        "superseded",
        "archived",
    }
