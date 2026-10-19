"""marker 文法的测试（plan §7 M2 的"标记文法"面）。

M2 的关键测试清单里与本文件相关的两条：
"round-trip byte-identical" 与 "damaged/absent marker classes"。
"""

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from zhiwo import factfile, models
from zhiwo.errors import MarkerError
from zhiwo.factfile import (
    FactDocument,
    FactLine,
    LineKind,
    Marker,
    mentioned_fact_ids,
    mentions_fact_id,
    parse_marker,
    render_fact_line,
    render_marker,
)
from zhiwo.models import FactStatus


class _IdBox(BaseModel):
    """只用来问"模型层认不认这个 id"的最小容器（测试用，无契约含义）。"""

    id: models.FactId


@pytest.mark.parametrize(
    "value",
    ["f_001", "f_017", "f_0001", "f_12345", "f_12", "x_001", "F_001", "f_", "f_1a2", "f_001 "],
)
def test_fact_id_language_agrees_with_model_contract(value: str) -> None:
    """``factfile`` 的 id 正则必须与 ``models.FactId`` 判得一模一样。

    id 的形态是契约面（SPEC §2.1），但在两处出现：模型用于校验 changelog，
    factfile 用于解析文件行。若模型放宽了 id 而这里没跟上，一个 changelog
    里合法的 id 会变成文件里"损坏的 marker"——两边都必须动，且必须一起动。
    这里比的是**行为**而不是两串正则文本：文本相同不等于判定相同
    （锚点、标志位、Unicode 类都可能带来差异）。
    """
    model_accepts = True
    try:
        _IdBox(id=value)
    except ValidationError:
        model_accepts = False
    assert (factfile._FACT_ID_RE.match(value) is not None) == model_accepts


def test_parse_minimal_marker() -> None:
    """只有一个 id 的 marker：active、无关系、无标志（SPEC §2.1 的最小形态）。"""
    marker = parse_marker("f_001")
    assert marker == Marker("f_001")
    assert marker.status is FactStatus.ACTIVE


def test_parse_full_marker() -> None:
    marker = parse_marker("f_017 supersedes:f_009 locked pending")
    assert marker.fact_id == "f_017"
    assert marker.supersedes == "f_009"
    assert marker.superseded_by is None
    assert marker.locked is True
    assert marker.pending is True


def test_parse_is_order_liberal() -> None:
    """标志顺序自由：手写的 ``pending locked`` 与规范顺序是同一个 marker。

    规范顺序只约束我们**写出**什么，不约束人**写进来**什么——判一条手写的
    事实"损坏"，只因为它把两个标志写反了，是把手滑当成了数据损坏。
    """
    assert parse_marker("f_001 pending locked") == parse_marker("f_001 locked pending")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # superseded-by 是终局：压过同一行上的 pending。
        ("f_009 superseded-by:f_017", FactStatus.SUPERSEDED),
        ("f_009 superseded-by:f_017 pending", FactStatus.SUPERSEDED),
        # pending 压过 active；supersedes 边在 pending 事实上是惰性的，
        # 不影响状态（SPEC §2.3 + plan §8 R9）。
        ("f_060 pending", FactStatus.PENDING),
        ("f_060 supersedes:f_009 pending", FactStatus.PENDING),
        # 只有 supersedes 边、没有 pending：仍然是 active——
        # 被取代的是目标，不是它自己。
        ("f_017 supersedes:f_009", FactStatus.ACTIVE),
        ("f_001 locked", FactStatus.ACTIVE),
    ],
)
def test_status_precedence(content: str, expected: FactStatus) -> None:
    assert parse_marker(content).status is expected


def test_supersedes_and_superseded_by_may_coexist() -> None:
    """两个关系同现是合法文法（compress 会让成员既取代又被取代，SPEC §3）。

    系统自己从不产出它（v0.5 的 compress 才可能），但读取器必须认识
    契约里的每种形态——这是 changelog"比实现活得久"姿态在文件侧的对应物。
    """
    marker = parse_marker("f_011 supersedes:f_012 superseded-by:f_020")
    assert marker.supersedes == "f_012"
    assert marker.superseded_by == "f_020"
    assert marker.status is FactStatus.SUPERSEDED


@pytest.mark.parametrize(
    "content",
    [
        "",  # 空的 marker：<!-- --> 里什么都没有
        "   ",  # 全空白同理
        "f_12",  # 数字不足 3 位（plan §3 决策 4 的零填充）
        "x_001",  # 前缀不对
        "F_001",  # 大小写敏感
        "locked",  # 首 token 必须是 id，标志不能打头
        "f_001 bogus",  # 不认识的标志
        "f_001 --->",  # 花括号残渣
        "f_001 locked locked",  # 重复标志
        "f_001 pending pending",  # 重复标志
        "f_001 supersedes:",  # 关系缺目标
        "f_001 supersedes:f_12",  # 关系目标形状不对
        "f_001 superseded-by:",  # 同上
        "f_001 superseded-by:x_001",  # 同上
        "f_001 supersedes:f_001 supersedes:f_002",  # 同种关系出现两次
        "f_001 superseded-by:f_001 superseded-by:f_002",  # 同上
    ],
)
def test_parse_rejects_broken_markers(content: str) -> None:
    with pytest.raises(MarkerError):
        parse_marker(content, source="about/values.md")


def test_parse_error_names_source_and_token() -> None:
    """报错要能指认到文件与违规 token（错误输出也是 plain-file transparency）。"""
    with pytest.raises(MarkerError) as excinfo:
        parse_marker("f_001 bogus", source="goals/current.md")
    message = str(excinfo.value)
    assert "goals/current.md" in message
    assert "bogus" in message


def test_render_is_canonical_order() -> None:
    """渲染顺序恒为 SPEC §2.1 写的那个顺序，与构造顺序无关。"""
    marker = Marker(
        fact_id="f_017",
        supersedes="f_009",
        superseded_by="f_020",
        locked=True,
        pending=True,
    )
    assert render_marker(marker) == "f_017 supersedes:f_009 superseded-by:f_020 locked pending"


@pytest.mark.parametrize(
    "marker",
    [
        Marker("f_001"),
        Marker("f_001", locked=True),
        Marker("f_043", pending=True),
        Marker("f_009", superseded_by="f_017"),
        Marker("f_017", supersedes="f_009"),
        Marker("f_060", supersedes="f_009", pending=True),
        Marker("f_011", supersedes="f_012", superseded_by="f_020", locked=True, pending=True),
    ],
)
def test_render_then_parse_is_identity(marker: Marker) -> None:
    """渲染出来的文本必然能被解析回同一个 marker（写读闭环）。"""
    assert parse_marker(render_marker(marker)) == marker


@pytest.mark.parametrize(
    "marker",
    [
        Marker("x_001"),  # 坏 id
        Marker("f_12"),  # 位数不足
        Marker("f_001", supersedes="nope"),  # 坏关系目标
        Marker("f_001", superseded_by="f_1"),  # 坏关系目标
    ],
)
def test_render_rejects_broken_markers(marker: Marker) -> None:
    """写侧闸门：宁可在这里失败，也不产出下次 reconcile 只能 quarantine 的行。"""
    with pytest.raises(MarkerError):
        render_marker(marker)


# --- 文档层：分类、round-trip、改写 ---------------------------------------

# 黄金档案里的四份 markdown。plan §9 的"黄金不变量"要求 CI 对它们逐个
# round-trip——契约自己的示例永远不能解析不了。
GOLDEN_FACT_FILES = [
    "about/values.md",
    "about/career.md",
    "goals/current.md",
    "journal/2026-10-11.md",
]


@pytest.mark.parametrize("relative", GOLDEN_FACT_FILES)
def test_round_trip_byte_identical(examples_dir: Path, relative: str) -> None:
    """plan §7 M2 的第一条关键测试：parse → render 逐字节相同。"""
    path = examples_dir / relative
    original = path.read_bytes()
    document = FactDocument.parse(original.decode("utf-8"), source=str(path))
    assert document.render().encode("utf-8") == original


@pytest.mark.parametrize("relative", GOLDEN_FACT_FILES)
def test_golden_files_parse_without_damage(examples_dir: Path, relative: str) -> None:
    """契约自带的示例永远不该被自己的解析器判成损坏。"""
    document = FactDocument.parse((examples_dir / relative).read_text(encoding="utf-8"))
    assert document.damaged() == []


def _facts_by_id(document: FactDocument) -> dict[str, FactLine]:
    by_id: dict[str, FactLine] = {}
    for line in document.facts():
        assert line.marker is not None
        by_id[line.marker.fact_id] = line
    return by_id


def test_parse_values_markers(examples_dir: Path) -> None:
    """``about/values.md`` 是 marker 文法四种形态的活样本（SPEC §2.1）。"""
    document = FactDocument.parse((examples_dir / "about/values.md").read_text(encoding="utf-8"))
    facts = _facts_by_id(document)
    assert set(facts) == {"f_001", "f_002", "f_003", "f_009", "f_017"}

    # 无标志的普通事实。
    assert facts["f_001"].text == "厌恶风险，偏好确定性"
    assert facts["f_001"].struck is False

    # locked：人手改过的行，系统永不覆盖（SPEC §2.1）。
    assert facts["f_003"].marker is not None
    assert facts["f_003"].marker.locked is True

    # 被取代的一对：文本划线 + superseded-by，取代者带 supersedes（SPEC §2.1）。
    superseded = facts["f_009"]
    assert superseded.struck is True
    assert superseded.text == "想读博"
    assert superseded.marker is not None
    assert superseded.marker.superseded_by == "f_017"
    assert superseded.marker.status is FactStatus.SUPERSEDED

    successor = facts["f_017"]
    assert successor.marker is not None
    assert successor.marker.supersedes == "f_009"
    assert successor.marker.status is FactStatus.ACTIVE


def test_pending_fact_keeps_wikilink_in_its_text(examples_dir: Path) -> None:
    """pending 事实与文本里的 ``[[f_051]]`` 链接：marker 解析绝不碰链接（SPEC §2.4）。"""
    document = FactDocument.parse((examples_dir / "goals/current.md").read_text(encoding="utf-8"))
    line = _facts_by_id(document)["f_043"]
    assert line.text == "根据二面反馈补项目深度（见 [[f_051]]）"
    assert line.marker is not None
    assert line.marker.pending is True
    assert line.marker.status is FactStatus.PENDING


def test_journal_lines_are_handwritten_bullets_not_facts(examples_dir: Path) -> None:
    """journal 的行是**无 marker 的列表项**，因此分类为 HANDWRITTEN。

    给 M3 的警示不要误读：SPEC §4 的 adopt 只扫事实文件（about/ 与
    goals/），journal 是人的只追加文本——把这些行当"待收编的手写事实"
    收编进档案，等于把整本日记变成事实。
    """
    document = FactDocument.parse(
        (examples_dir / "journal/2026-10-11.md").read_text(encoding="utf-8")
    )
    assert document.facts() == []
    assert len(document.hand_written()) == 3


def test_line_without_marker_is_handwritten() -> None:
    line = FactDocument.parse("- 手写的一条事实\n").lines[0]
    assert line.kind is LineKind.HANDWRITTEN
    assert line.text == "手写的一条事实"
    assert line.marker is None


@pytest.mark.parametrize(
    "raw",
    [
        "# 标题\n",
        "\n",
        "普通散文，不带项目符号。\n",
        "- \n",  # 空的项目符号不是一条主张
        "-    \n",
        "  - 缩进的项目符号 <!-- f_001 -->\n",  # 缩进即散文（见 _classify 的取舍）
        "<!-- 纯说明性注释 -->\n",
    ],
)
def test_prose_lines_are_never_touched(raw: str) -> None:
    document = FactDocument.parse(raw)
    assert document.lines[0].kind is LineKind.PROSE
    assert document.render() == raw


@pytest.mark.parametrize(
    ("raw", "reason_fragment"),
    [
        # 未闭合的注释：手滑删掉 "-->"。
        ("- 事实 <!-- f_001", "unterminated"),
        # marker 之后还有别的东西：marker 必须收在行尾。
        ("- 事实 <!-- f_001 --> 尾巴", "not a complete comment"),
        # 一行里两个 marker：注释结构坏了。
        ("- 事实 <!-- f_001 <!-- f_002 -->", "delimiter"),
        # 文本里混进了定界符。
        ("- 事实 --> <!-- f_001 -->", "delimiter"),
        ("- 文本 <!-- 中间 --> 尾巴 <!-- f_001 -->", "delimiter"),
        # 没有文本的事实。
        ("- <!-- f_001 -->", "empty fact text"),
        ("- 事实 <!-- -->", "empty marker"),
        # id 形状不对。
        ("- 事实 <!-- locked -->", "invalid fact id"),
        ("- 事实 <!-- f_12 -->", "invalid fact id"),
        # 不认识的标志。
        ("- 事实 <!-- f_001 bogus -->", "unknown marker token"),
        # 重复标志。
        ("- 事实 <!-- f_001 locked locked -->", "duplicate"),
        # 坏的关系 token。
        ("- 事实 <!-- f_001 supersedes: -->", "malformed relation"),
        ("- 事实 <!-- f_001 supersedes:f_12 -->", "malformed relation"),
        ("- 事实 <!-- f_001 superseded-by:x_001 -->", "malformed relation"),
    ],
)
def test_damaged_marker_classes(raw: str, reason_fragment: str) -> None:
    """plan §7 M2 的第二条关键测试：损坏 marker 的各类形态。

    SPEC §4 把"marker 被人手改坏"定为**预期输入**：解析的职责是分类成
    数据（连同原因），而不是拒绝整份文件。这里逐类钉住"归 DAMAGED、
    原因说得清、原文一个字节都不动"。
    """
    document = FactDocument.parse(raw, source="about/values.md")
    assert len(document.lines) == 1
    line = document.lines[0]
    assert line.kind is LineKind.DAMAGED
    assert line.damage is not None
    assert reason_fragment in line.damage
    assert line.marker is None
    assert line.raw == raw  # quarantine 的前提：原文原样留着
    assert document.render() == raw


def test_damage_reason_names_the_source_line() -> None:
    """损坏原因要指认到行号——用户拿着"第几行"才能去编辑器里找。"""
    document = FactDocument.parse("- 好的 <!-- f_001 -->\n- 坏的 <!-- f_12 -->\n", source="a.md")
    damage = document.damaged()
    assert len(damage) == 1
    assert damage[0].line_no == 2
    assert damage[0].damage is not None
    assert "a.md:2" in damage[0].damage


def test_status_marker_beats_strikethrough() -> None:
    """plan §8 R9：marker 是权威，划线只是渲染。

    一条被划掉、但 marker 里没有任何取代关系的行——状态仍然是 active
    （人只是把它划掉了，没告诉系统为什么），reconcile 会在报告里提一句。
    系统绝不据此改写人的行。
    """
    line = FactDocument.parse("- ~~划掉了但 marker 没说~~ <!-- f_001 -->\n").lines[0]
    assert line.kind is LineKind.FACT
    assert line.struck is True
    assert line.text == "划掉了但 marker 没说"
    assert line.marker is not None
    assert line.marker.status is FactStatus.ACTIVE


def test_single_unpaired_strikethrough_is_not_damage() -> None:
    """不成对的 ``~~`` 不是损坏，只是文本里的两个波浪号。

    判它损坏等于替人猜意图；R9 的原则是**只记录能确定的东西**——
    成对包裹才认作划线，其余原样留在文本里。
    """
    line = FactDocument.parse("- ~~只有开头 <!-- f_001 -->\n").lines[0]
    assert line.kind is LineKind.FACT
    assert line.struck is False
    assert line.text == "~~只有开头"


# --- 渲染与改写 -----------------------------------------------------------


def test_render_fact_line_shape() -> None:
    """渲染出的形态：``- text <!-- marker -->`` + 换行（SPEC §2.1）。"""
    assert (
        render_fact_line("想读博", Marker("f_009", superseded_by="f_017"), struck=True)
        == "- ~~想读博~~ <!-- f_009 superseded-by:f_017 -->\n"
    )
    assert render_fact_line("厌恶风险", Marker("f_001")) == "- 厌恶风险 <!-- f_001 -->\n"


@pytest.mark.parametrize(
    "text",
    [
        "",  # 空
        "   ",  # 全空白
        "带\n换行",
        "带\r回车",
        "含 <!-- 定界符",
        "含 --> 定界符",
        "含 ~~ 划线",  # R15：与"划线表示被取代"的渲染撞车
        "前导空格 ",
        " 尾随空格",
    ],
)
def test_render_fact_line_rejects_grammar_breaks(text: str) -> None:
    """plan §8 R15：会让行文法不可逆的文本，在写出前就拒绝。"""
    with pytest.raises(MarkerError):
        render_fact_line(text, Marker("f_001"))


@pytest.mark.parametrize("struck", [False, True])
def test_rendered_line_parses_back(struck: bool) -> None:
    """写读闭环：我们渲染的每一行，自己都解析得回来。"""
    marker = Marker("f_043", pending=True)
    line = FactDocument.parse(render_fact_line("一条事实", marker, struck=struck)).lines[0]
    assert line.kind is LineKind.FACT
    assert line.marker == marker
    assert line.text == "一条事实"
    assert line.struck is struck


def test_rewrite_touches_only_the_decided_line(examples_dir: Path) -> None:
    """round-trip 定律的另一半："merge 只改写它有决定的行"（plan §4）。"""
    path = examples_dir / "about/values.md"
    original = path.read_text(encoding="utf-8")
    document = FactDocument.parse(original, source=str(path))

    target = _facts_by_id(document)["f_017"]
    target.rewrite("决定先工作两年，再考虑读博", Marker("f_017", supersedes="f_009"))

    before = original.splitlines(keepends=True)
    after = document.render().splitlines(keepends=True)
    assert len(before) == len(after)
    changed = [
        index for index, (old, new) in enumerate(zip(before, after, strict=True)) if old != new
    ]
    assert changed == [target.line_no - 1]
    assert after[changed[0]] == "- 决定先工作两年，再考虑读博 <!-- f_017 supersedes:f_009 -->\n"


def test_rewrite_keeps_crlf_and_line_number() -> None:
    """CRLF 档案里改一行，改完仍是 CRLF，且行号不变。"""
    document = FactDocument.parse("- 旧事实 <!-- f_001 -->\r\n- 另一条 <!-- f_002 -->\r\n")
    assert document.newline == "\r\n"
    line = document.lines[1]
    line.rewrite("新事实", Marker("f_002"))
    assert line.line_no == 2
    assert document.render() == "- 旧事实 <!-- f_001 -->\r\n- 新事实 <!-- f_002 -->\r\n"


def test_rewrite_refuses_prose_and_damaged() -> None:
    """散文不是事实，损坏的 marker 必须原样留着等 quarantine（SPEC §4 never guess）。"""
    prose = FactDocument.parse("普通一行\n").lines[0]
    with pytest.raises(MarkerError):
        prose.rewrite("改成事实", Marker("f_001"))

    damaged = FactDocument.parse("- 坏的 <!-- f_12 -->\n").lines[0]
    with pytest.raises(MarkerError):
        damaged.rewrite("想修好它", Marker("f_012"))


def test_rewrite_adopts_a_handwritten_line() -> None:
    """SPEC §4 的 adopt：手写行被收编时分配 id 并加 locked（M3 会这么用）。"""
    document = FactDocument.parse("- 手写的一条\n")
    line = document.lines[0]
    line.rewrite("手写的一条", Marker("f_060", locked=True))
    assert line.kind is LineKind.FACT
    assert document.render() == "- 手写的一条 <!-- f_060 locked -->\n"


def test_missing_final_newline_survives_round_trip_and_append() -> None:
    """手删掉文末换行：round-trip 保住它；追加时补一个终止符，不改旧行内容。"""
    document = FactDocument.parse("- 末尾无换行 <!-- f_001 -->")
    assert document.render() == "- 末尾无换行 <!-- f_001 -->"

    document.append_fact("追加的一条", Marker("f_002"))
    assert document.render() == "- 末尾无换行 <!-- f_001 -->\n- 追加的一条 <!-- f_002 -->\n"
    assert document.lines[1].line_no == 2


def test_append_fact_uses_dominant_newline() -> None:
    """新行跟随文件的多数派终止符：不制造新的混排。"""
    document = FactDocument.parse("- 一 <!-- f_001 -->\r\n- 二 <!-- f_002 -->\r\n")
    appended = document.append_fact("三", Marker("f_003"))
    assert appended.raw == "- 三 <!-- f_003 -->\r\n"


# --- mentions_fact_id（R8 的存在性检查，M3 的 retire 靠它）----------------


@pytest.mark.parametrize(
    "text",
    [
        "- 一条事实 <!-- f_051 -->\n",
        # 未闭合：手滑删掉 "-->" 不能变成"这条事实没了"——否则一次手滑
        # 就能让一条活着的事实被退役（plan §8 R8）。
        "- 一条事实 <!-- f_051\n",
        "- 一条事实 <!-- f_051 --->\n",
        "- 一条事实 <!-- f_051 locked locked -->\n",
        "- 一条事实 <!-- f_051 supersedes:f_052 -->\n",
    ],
)
def test_mentions_finds_presence_even_in_damaged_markers(text: str) -> None:
    assert mentions_fact_id(text, "f_051")


@pytest.mark.parametrize(
    "text",
    [
        # 链接是引用，不是副本（SPEC §2.4）：留着指向它的链接不能阻止退役。
        "- 见 [[f_051]] 那条\n",
        # 注释里的链接同样是链接——黄金 values.md 里就有这种说明性注释。
        "<!-- 使用 [[f_051]] 这样的写法 -->\n",
        "- 别的 id <!-- f_052 -->\n",
        "- 不完整的 id <!-- f_05 -->\n",
    ],
)
def test_mentions_ignores_links_and_other_ids(text: str) -> None:
    assert not mentions_fact_id(text, "f_051")


def test_mentions_matches_whole_token_only() -> None:
    """``f_051`` 不能匹配到 ``f_0510``：整词边界，否则退役判定会误判。"""
    assert not mentions_fact_id("- 一条 <!-- f_0510 -->\n", "f_051")
    assert mentions_fact_id("- 一条 <!-- f_0510 -->\n", "f_0510")


def test_every_golden_fact_id_is_mentioned(examples_dir: Path) -> None:
    """黄金档案里出现的每个 id，存在性检查都认——M3 退役判定的正面样本。"""
    text = (examples_dir / "about/values.md").read_text(encoding="utf-8")
    for fact_id in ("f_001", "f_002", "f_003", "f_009", "f_017"):
        assert mentions_fact_id(text, fact_id)


# --- mentioned_fact_ids（集合版：M3 reconcile 的文件侧 id 宇宙）-------------


def test_mentioned_ids_collects_every_id_in_comment_spans() -> None:
    """一次扫描收齐全部：好 marker、坏 marker、散文注释里的提及。

    M3 的 reconcile 对每个文件只调一次本函数，产物同时服务
    retire 存在性与 id 天花板——少收一个 id 就是误退役或多发一个号。
    """
    text = (
        "<!-- 说明性注释：见 f_001 -->\n"
        "- 一条事实 <!-- f_002 -->\n"
        "- 坏 marker <!-- f_051\n"
        "- 另一条 <!-- f_003 superseded-by:f_017 -->\n"
        "- 最后一条 <!-- f_017 -->\n"
    )
    assert mentioned_fact_ids(text) == frozenset({"f_001", "f_002", "f_051", "f_003", "f_017"})


def test_mentioned_ids_excludes_wikilinks_and_outside_comments() -> None:
    """链接（SPEC §2.4）与注释 span 之外的文本不进集合。

    注释内嵌链接（黄金 values.md 的说明性注释就是这种写法）也要剥——
    链接是引用，不能算"这条事实还在文件里"。
    """
    text = "# 标题里写 f_001 不算\n- 见 [[f_051]] <!-- f_002 -->\n<!-- 用 [[f_051]] 的写法 -->\n"
    assert mentioned_fact_ids(text) == frozenset({"f_002"})


def test_mentioned_ids_unterminated_span_reaches_eof() -> None:
    """未闭合 ``<!--`` 延伸到文末：坏 marker 之后的行也在 span 里。

    这是"损坏 marker 不造成误退役"的实现所在（plan §8 R8）——
    集合版必须与单 id 版共享该行为，否则两套语义迟早漂移。
    """
    text = "- 开头 <!-- f_051\n- 后面这行的 f_052 也在未闭合 span 里\n"
    assert mentioned_fact_ids(text) == frozenset({"f_051", "f_052"})


def test_mentioned_ids_matches_whole_tokens_only() -> None:
    """``f_0512`` 是它自己的 id，不是 ``f_051`` 的在场，也不会拆出 f_051。"""
    assert mentioned_fact_ids("- 一条 <!-- f_0510 -->\n") == frozenset({"f_0510"})
    assert mentioned_fact_ids("- x <!-- xf_051 -->\n") == frozenset()


def test_single_id_check_is_membership_in_the_collected_set() -> None:
    """钉死"单 id 检查 = 集合成员测试"：两者必须是同一实现。

    mentions_fact_id 被现有测试逐条钉住，mentioned_fact_ids 是它的
    集合化——若有人改了其中一边的边界规则，这条测试会先红。
    """
    texts = [
        "- 一条 <!-- f_051 -->\n",
        "- 坏 <!-- f_051\n",
        "- 见 [[f_051]]\n",
        "- 别的 <!-- f_052 supersedes:f_051 -->\n",
    ]
    for text in texts:
        collected = mentioned_fact_ids(text)
        for fact_id in ("f_051", "f_052"):
            assert mentions_fact_id(text, fact_id) == (fact_id in collected)
