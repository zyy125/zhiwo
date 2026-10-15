"""L1 的端到端测试：加载黄金档案、解析黄金 changelog、拒绝坏 profile。

这是 M1 三条验收测试的落点（plan §7 M1 行的 Key tests 列）：
example profile loads / invalid profiles rejected / golden changelog parses。
"""

from pathlib import Path

import pytest

from zhiwo.errors import SchemaError
from zhiwo.models import (
    Axis,
    ChangelogEntry,
    Clearance,
    PatchOp,
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

# --- 黄金档案：能加载 --------------------------------------------------------


def test_example_profile_loads(profile_path: Path) -> None:
    """M1 验收 1：examples/me/profile.yaml 必须按声明原样加载。

    这个测试同时是"L1 不要求分区文件存在"的证明：示例里声明了 identity /
    health / relations / finance / long-term 等文件，但 examples/me/ 是部分树，
    这些文件并不存在，加载仍必须成功。
    """
    profile = load_profile(profile_path)

    assert profile.core_budget_tokens == 4000
    assert [s.id for s in profile.sections] == [
        "identity",
        "health",
        "relations",
        "finance",
        "values",
        "career",
        "long-term",
        "current",
        "journal",
    ]
    assert profile.sections[0].axis is Axis.ABOUT
    assert profile.sections[6].axis is Axis.GOALS

    journal = profile.sections[-1]
    assert journal.axis is Axis.JOURNAL
    assert journal.sensitivity is Sensitivity.HIGH
    assert journal.path == "journal/"


def test_example_profile_declares_files_that_do_not_exist(profile_path: Path) -> None:
    """显式钉住上一条的隐含前提：声明与磁盘状态解耦（reconcile 才创建文件）。"""
    profile = load_profile(profile_path)
    missing = [s.path for s in profile.sections if not (profile_path.parent / s.path).exists()]
    assert missing, "examples/me/ 应当是部分树，否则这条测试失去意义"


def test_template_loads_and_matches_defaults() -> None:
    """随包模板自身必须是一份合法 profile（M2 的 me init 要直接复制它）。

    刻意**不**断言模板与 examples/me/profile.yaml 逐字段相同：示例是冻结的
    契约说明数据，模板是活的引导默认值，两者允许各自演进（模板里也写明了
    这一点）。这里只钉"模板本身合法、且关键默认值正确"。
    """
    profile = parse_profile(parse_yaml(template_text(), source="template"))

    assert profile.core_budget_tokens == 4000
    assert len(profile.sections) == 9
    journal = profile.sections[-1]
    assert journal.axis is Axis.JOURNAL
    assert journal.sensitivity is Sensitivity.HIGH


# --- 黄金 changelog：能解析 --------------------------------------------------


def test_golden_changelog_parses(golden_changelog: Path) -> None:
    """M1 验收 3：黄金 changelog 必须原样解析（SPEC §3 的补丁形态）。

    evidence 以**字面中文**断言，钉死"UTF-8 读入不毁中文"这条编码法
    （plan §4）：把中文转义成 \\uXXXX 或读成乱码，plain-file transparency
    就没了。
    """
    text = golden_changelog.read_text(encoding="utf-8")
    entry = parse_changelog_entry(
        parse_yaml(text, source=str(golden_changelog)), source=str(golden_changelog)
    )

    assert isinstance(entry, ChangelogEntry)
    assert entry.merge == "m_2026-10-11T2130"
    assert entry.input == "journal/2026-10-11.md"
    assert entry.extractor == "mock-v0"
    assert entry.schema_version == 1  # 文件里没有该字段 → 视为 1（OQ#3）
    assert len(entry.patches) == 2

    statement, inference = entry.patches
    assert statement.op is PatchOp.ADD
    assert statement.id == "f_051"
    assert statement.target == "about/career.md"
    assert statement.text == "二面反馈：项目深度不够（2026-10-11）"
    assert statement.evidence == "面试挂了，二面，反馈说项目深度不够。"
    assert statement.source == "journal/2026-10-11.md#L7"

    assert inference.id == "f_043"
    assert inference.target == "goals/current.md"
    assert inference.text == "根据二面反馈补项目深度（见 [[f_051]]）"


def test_golden_profile_has_no_schema_version_to_migrate(golden_changelog: Path) -> None:
    """留住 R5 的现状：黄金条目的文件名是分钟精度且无 schema_version。

    写入侧（M2）按 SPEC §1 输出秒精度 + schema_version: 1；
    读取侧接受任意 *.yaml stem。示例是契约说明性数据，永不改动。
    """
    assert golden_changelog.name == "2026-10-11T2130.yaml"
    assert "schema_version" not in golden_changelog.read_text(encoding="utf-8")


# --- 坏 profile：一律拒绝 ----------------------------------------------------


def test_missing_profile_file_is_a_schema_error(tmp_path: Path) -> None:
    with pytest.raises(SchemaError, match="cannot read profile"):
        load_profile(tmp_path / "profile.yaml")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty-file"),
        pytest.param("just a string\n", id="root-is-scalar"),
        pytest.param("- a\n- b\n", id="root-is-list"),
        pytest.param("sections: [unclosed\n", id="yaml-syntax-error"),
        pytest.param(
            "core_budget_tokens: !!python/object:os.system\n",
            id="python-object-tag-refused",
        ),
        pytest.param(
            "core_budget_tokens: 4000\ncore_budget_tokens: 9000\n",
            id="duplicate-top-level-key",
        ),
        pytest.param("sectons: []\n", id="unknown-top-level-key"),
        pytest.param(
            "sections:\n"
            "  - id: journal\n    title: 日常流水\n    path: journal/\n"
            "    axis: journal\n    sensitivity: normal\n",
            id="journal-downgraded",
        ),
        pytest.param(
            "sections:\n"
            "  - id: values\n    title: 价值观\n    path: about/values.md\n"
            "    axis: about\n    sensitivity: normal\n"
            "  - id: values\n    title: 重复\n    path: about/other.md\n"
            "    axis: about\n    sensitivity: normal\n",
            id="duplicate-section-id",
        ),
        pytest.param(
            "sections:\n"
            "  - id: health\n    title: 身体\n    path: about/health.md\n    axis: about\n",
            id="missing-sensitivity-on-non-journal-section",
        ),
    ],
)
def test_invalid_profiles_are_rejected(text: str) -> None:
    """M1 验收 2：坏 profile 一律拒绝，且消息以来源开头、指认得出问题所在。"""
    with pytest.raises(SchemaError) as excinfo:
        parse_profile(parse_yaml(text, source="<test>"), source="<test>")
    assert str(excinfo.value).startswith("<test>:")


def test_non_utf8_profile_is_a_schema_error(tmp_path: Path) -> None:
    """损坏编码同样归一成 SchemaError（L1 的失败只有一种类型）。

    GB18030/Big5 的旧档案、编辑器写坏的字节都会走到这条路径；
    若让 UnicodeDecodeError 逃出去，上层就得认识第二种异常——
    与 schema.py 开头承诺的"fail loud 且只有一种失败"不符。
    """
    bad = tmp_path / "profile.yaml"
    bad.write_bytes("sections:\n  - id: 身体\n".encode("gb18030"))
    with pytest.raises(SchemaError, match="not valid UTF-8"):
        load_profile(bad)


def test_duplicate_key_error_names_the_line() -> None:
    """重复键的报错要能定位到行——否则手改 profile 的人只能靠数行。"""
    text = "core_budget_tokens: 4000\ncore_budget_tokens: 9000\n"
    with pytest.raises(SchemaError, match="duplicate mapping key 'core_budget_tokens' at line 2"):
        parse_yaml(text, source="<test>")


def test_yaml_merge_keys_are_not_false_positives() -> None:
    """``<<`` 合并键 + 显式覆盖是合法 YAML，不得误报重复键。

    PyYAML 会把合并进来的键前置，若不分青红皂白判重，一份用锚点收敛
    重复字段的 profile 会被拒，且错误文本（"拒绝静默丢弃前一段"）与事实
    相反——这里没有任何东西被丢弃，后写的显式键本来就该胜出。
    """
    text = "base: &b\n  title: 原标题\n  axis: about\nsec:\n  <<: *b\n  title: 覆盖后\n"
    parsed = parse_yaml(text, source="<test>")
    assert parsed["sec"] == {"title": "覆盖后", "axis": "about"}


def test_duplicate_inside_explicit_keys_still_rejected() -> None:
    """合并键放宽之后，真正写重复的键仍须拒绝（避免放宽过头）。"""
    text = "sec:\n  title: 甲\n  title: 乙\n"
    with pytest.raises(SchemaError, match="duplicate mapping key 'title' at line 3"):
        parse_yaml(text, source="<test>")


def test_schema_error_names_the_source_path(tmp_path: Path) -> None:
    """失败必须指认文件：多档案场景下用户要知道改哪一个 profile。"""
    bad = tmp_path / "profile.yaml"
    bad.write_text("sections: []\n", encoding="utf-8")
    with pytest.raises(SchemaError) as excinfo:
        load_profile(bad)
    assert str(bad) in str(excinfo.value)


def test_schema_error_keeps_cjk_values(tmp_path: Path) -> None:
    """报错里的中文标题/路径原样保留，不转义（可见性优于"整齐"）。"""
    bad = tmp_path / "profile.yaml"
    bad.write_text(
        "sections:\n"
        "  - id: 身体\n    title: 身体\n    path: about/health.md\n"
        "    axis: about\n    sensitivity: high\n",
        encoding="utf-8",
    )
    with pytest.raises(SchemaError) as excinfo:
        load_profile(bad)
    assert "身体" in str(excinfo.value)


# --- clearance 判定（SPEC §5）------------------------------------------------


def make_section(sensitivity: Sensitivity, axis: Axis = Axis.ABOUT) -> Section:
    return Section.model_validate(
        {
            "id": "journal" if axis is Axis.JOURNAL else "values",
            "title": "标题",
            "path": "journal/" if axis is Axis.JOURNAL else "about/values.md",
            "axis": axis,
            "sensitivity": sensitivity,
        }
    )


def test_normal_reader_cannot_read_high_section() -> None:
    assert not can_read(Clearance.NORMAL, make_section(Sensitivity.HIGH))


def test_high_reader_reads_everything() -> None:
    assert can_read(Clearance.HIGH, make_section(Sensitivity.HIGH))
    assert can_read(Clearance.HIGH, make_section(Sensitivity.NORMAL))


def test_normal_reader_can_read_normal_section() -> None:
    assert can_read(Clearance.NORMAL, make_section(Sensitivity.NORMAL))


def test_journal_is_unreadable_at_normal_clearance() -> None:
    """SPEC §5 的落点：normal 读者拿不到 journal（原始话语流是泄露源头）。"""
    journal = make_section(Sensitivity.HIGH, axis=Axis.JOURNAL)
    assert not can_read(Clearance.NORMAL, journal)
    # journal 回想是独立的高权限能力，因此 high 读者可以——"journal 不进
    # reader view"是 recall 层的视图规则，不是 can_read 的规则（plan §6）。
    assert can_read(Clearance.HIGH, journal)


def test_can_read_fails_closed_on_unvalidated_section() -> None:
    """判定偏向失败安全：绕过校验构造出的分区（sensitivity 缺失）也应读不到。

    这是防御性用例，写在测试层是为了把"默认拒绝"的意图固定下来：
    将来若有人把判定改回"是 high 才拦"，这条会立刻红。
    """
    bypassed = Section.model_construct(
        id="health",
        title="身体",
        path="about/health.md",
        axis=Axis.ABOUT,
        sensitivity=None,
    )
    assert not can_read(Clearance.NORMAL, bypassed)
    assert can_read(Clearance.HIGH, bypassed)


@pytest.mark.parametrize("as_raw_string", [False, True])
def test_can_read_accepts_clearance_as_raw_string(as_raw_string: bool) -> None:
    """裸字符串 clearance 必须与枚举成员等价，不得被静默降级。

    CLI（click 的 Choice）、MCP 参数、配置文件都把 clearance 当字符串递
    进来，而这个函数的参数类型注解拦不住运行时的裸字符串。若这里用 ``is``
    比较，``"high"`` 会静默落到 normal 分支——高权限请求变成"看不到高敏感
    分区"且无任何报错。Clearance 是 StrEnum，值比较对两种形态都成立。
    """
    high_section = make_section(Sensitivity.HIGH)
    clearance: Clearance | str = "high" if as_raw_string else Clearance.HIGH
    assert can_read(clearance, high_section)  # type: ignore[arg-type]

    normal_only: Clearance | str = "normal" if as_raw_string else Clearance.NORMAL
    assert not can_read(normal_only, high_section)  # type: ignore[arg-type]
