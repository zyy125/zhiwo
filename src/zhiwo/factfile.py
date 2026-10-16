"""L0 的文本层：事实行的 marker 文法与**字节级 round-trip**（plan §4）。

本模块是纯字符串层——零 I/O、不认识路径、不认识 profile。磁盘访问全部
在 ``store.py``；这里的输入输出都是 ``str``，因此可以对着 ``examples/me/``
里的黄金文本做逐字节的断言。

两条契约在这个模块里落地：

**round-trip 定律**（plan §4）：``parse`` → ``render`` 对未触碰的行
**字节一致**——不是"接近"，是相同。实现方式是保留每行的 ``raw``
（含行终止符）原样，``render`` 只是把它们接回去；改写只发生在
``FactLine.rewrite`` 与 ``FactDocument.append_fact`` 这两条显式路径上
（"merge 只改写它有决定的行"）。因此 CRLF、无尾换行、注释里的古怪空格
都不会被悄悄规整掉——手改过的档案经系统之手后仍然是它本来的样子。

**编码法**（plan §4）：本模块只处理 ``str``；"一律 UTF-8"由 store 的
读写边界保证。中文事实文本在这里是普通字符串，任何转义都发生在更外层，
且被明令禁止。
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from zhiwo.errors import MarkerError
from zhiwo.models import FactStatus

# 事实 id 的形态。与 ``models.FactId`` 是同一个约定（``f_`` + 至少 3 位数字），
# 但模型层那边是 ``Annotated`` 类型，这里需要可编译的正则。
# 两处必须一致：``tests/test_factfile.py`` 有一条测试直接比对二者，
# 防止"模型放宽了 id 而 factfile 没跟上"这类静默漂移。
_FACT_ID_RE = re.compile(r"^f_\d{3,}$")

# ``supersedes:`` / ``superseded-by:`` 前缀。带冒号是为了在报错时能区分
# "这是个坏的关系 token"和"这是个根本不认识的 token"——手改坏 marker 时，
# 前者才是那个更有用的提示。
_SUPERSEDES_PREFIX = "supersedes:"
_SUPERSEDED_BY_PREFIX = "superseded-by:"

_FLAGS = ("locked", "pending")


@dataclass(frozen=True, slots=True)
class Marker:
    """一条事实的 inline marker（SPEC §2.1）。

    marker 携带**身份、关系、标志**——其余一切（状态、kind、created、
    section、行号）都是推导出来的：状态由 :attr:`status` 从 marker 现算，
    kind 与 provenance 在 changelog 里（SPEC §2.1 的"三处一权"表）。

    这是 ``frozen`` 的：改写一条事实的 marker 不是"就地改标志"，
    而是产出一个新的 ``Marker`` 再去重渲染那一行——marker 是不可变的
    事实身份，可变的是它在文件里的呈现。
    """

    fact_id: str
    supersedes: str | None = None
    superseded_by: str | None = None
    locked: bool = False
    pending: bool = False

    @property
    def status(self) -> FactStatus:
        """从 marker 推导存活状态（SPEC §2.1，plan §8 R9 钉死优先级）。

        优先级 ``superseded-by`` > ``pending`` > ``active``：

        - ``superseded-by`` 一出现即为 superseded——被取代是终局，
          不该被同一行上的别的标志拉回中间态；
        - ``pending`` 表示"推断出的、待人确认的"（SPEC §2.3），
          它照样能带 ``supersedes`` 边，但那条件边在确认前是**惰性**的：
          所以这里既不看 ``supersedes`` 也不让它影响状态。
          注意 SPEC §2.1 那句"pending takes precedence"说的是
          *supersedes 边*在 pending 事实上失效，不是指 pending 压过
          superseded-by——被澄清在 plan §8 R9 里；
        - ``archived`` 永远不会从这里出现：它只存在于 changelog
          与 index（SPEC §2.1），文件里没有它的写法。
        """
        if self.superseded_by is not None:
            return FactStatus.SUPERSEDED
        if self.pending:
            return FactStatus.PENDING
        return FactStatus.ACTIVE


def parse_marker(content: str, *, source: str = "<marker>") -> Marker:
    """解析 ``<!--`` 与 ``-->`` **之间**的内容（不含定界符）。

    语法（SPEC §2.1）：``<id> [supersedes:<id>] [superseded-by:<id>]
    [locked] [pending]``。

    两个刻意的选择：

    1. **解析宽松，渲染规范**。任何标志顺序都能解析——手写或手工编辑
       出来的 ``<!-- f_001 pending locked -->`` 是一条完全可读的事实，
       没有理由判它损坏。规范顺序只约束我们**写出**的形态（见
       :func:`render_marker`），不约束读进来的形态。
    2. **``supersedes`` 与 ``superseded-by`` 同时出现是合法的**。
       它是文法的超集：``compress`` 会让一个成员既取代又被取代
       （SPEC §3，v0.5 才落地），而"读取器必须认识契约里的每种形态"
       是这个项目的长期姿态。系统自己从不产出这种 marker。

    失败一律 :class:`~zhiwo.errors.MarkerError`，消息带 ``source`` 与
    出错 token 原文。**注意**：本函数只在解析路径上抛错，而调用它的
    文档层会把错误**降级成数据**（``LineKind.DAMAGED``）——SPEC §4 说
    手改坏的 marker 是预期输入。直接调用本函数的调用方要自己决定
    "这是损坏数据"还是"这是应该在更高层拦住的输入"。
    """
    tokens = content.split()
    if not tokens:
        raise MarkerError(f"{source}: empty marker")

    fact_id = tokens[0]
    if not _FACT_ID_RE.match(fact_id):
        raise MarkerError(f"{source}: invalid fact id {fact_id!r} (expected f_NNN)")

    supersedes: str | None = None
    superseded_by: str | None = None
    locked = False
    pending = False

    for token in tokens[1:]:
        if token.startswith(_SUPERSEDES_PREFIX) or token.startswith(_SUPERSEDED_BY_PREFIX):
            prefix = (
                _SUPERSEDES_PREFIX
                if token.startswith(_SUPERSEDES_PREFIX)
                else _SUPERSEDED_BY_PREFIX
            )
            target = token[len(prefix) :]
            if not _FACT_ID_RE.match(target):
                raise MarkerError(
                    f"{source}: malformed relation {token!r} (expected {prefix}f_NNN)"
                )
            if prefix == _SUPERSEDES_PREFIX:
                if supersedes is not None:
                    raise MarkerError(f"{source}: duplicate {prefix} token")
                supersedes = target
            else:
                if superseded_by is not None:
                    raise MarkerError(f"{source}: duplicate {prefix} token")
                superseded_by = target
        elif token in _FLAGS:
            if token == "locked":
                if locked:
                    raise MarkerError(f"{source}: duplicate 'locked' token")
                locked = True
            else:
                if pending:
                    raise MarkerError(f"{source}: duplicate 'pending' token")
                pending = True
        else:
            raise MarkerError(f"{source}: unknown marker token {token!r}")

    return Marker(
        fact_id=fact_id,
        supersedes=supersedes,
        superseded_by=superseded_by,
        locked=locked,
        pending=pending,
    )


def render_marker(marker: Marker) -> str:
    """把 marker 渲染成 ``<!--`` 与 ``-->`` 之间的规范内容（SPEC §2.1 顺序）。

    规范顺序：``<id> [supersedes:<id>] [superseded-by:<id>] [locked]
    [pending]``，单空格分隔——与 SPEC §2.1 写的那行文法逐字一致，
    这样"契约的原文"与"我们写出的东西"可以直接对照。

    这是**写侧闸门**：id 或关系目标的形状不对就抛
    :class:`~zhiwo.errors.MarkerError`。写坏一个 marker 会让整行变得
    不可解析，而不可解析的行在下次 reconcile 时只能被 quarantine——
    人类看到的是"我的一条事实忽然变成了一行看不懂的注释"。宁可在
    这里失败。
    """
    if not _FACT_ID_RE.match(marker.fact_id):
        raise MarkerError(f"invalid fact id {marker.fact_id!r} (expected f_NNN)")

    parts = [marker.fact_id]
    if marker.supersedes is not None:
        _require_fact_id(marker.supersedes, "supersedes")
        parts.append(f"{_SUPERSEDES_PREFIX}{marker.supersedes}")
    if marker.superseded_by is not None:
        _require_fact_id(marker.superseded_by, "superseded-by")
        parts.append(f"{_SUPERSEDED_BY_PREFIX}{marker.superseded_by}")
    if marker.locked:
        parts.append("locked")
    if marker.pending:
        parts.append("pending")
    return " ".join(parts)


def _require_fact_id(value: str, field: str) -> None:
    """关系目标必须是合法 id——错误消息里点明是哪个字段。"""
    if not _FACT_ID_RE.match(value):
        raise MarkerError(f"invalid {field} target {value!r} (expected f_NNN)")


class LineKind(enum.StrEnum):
    """一行文本在契约里的身份（决定 merge/reconcile 能对它做什么）。

    四种身份对应 SPEC §4 的五种观察里除"重复 id"之外的全部输入面——
    重复 id 是**跨行**的观察，属 M3 的 reconcile，不是单行的分类。
    """

    PROSE = "prose"  # 标题、散文、纯注释、空行——永不被改写
    FACT = "fact"  # 带合法 marker 的事实行（SPEC §2.1）
    HANDWRITTEN = "handwritten"  # 列表项但无 marker——SPEC §4 的 adopt 候选（M3）
    DAMAGED = "damaged"  # marker 在场但解析不了——SPEC §4 的 quarantine 候选（M3）


@dataclass
class FactLine:
    """文档里的一行，连同它的分类。

    ``raw`` 是**整行原文、含行终止符**，并且是 round-trip 保真的唯一来源：
    :meth:`FactDocument.render` 就是把这些 ``raw`` 接起来。任何"顺手规整
    一下空白"的冲动都要在这里被压住——一行的空格是人的，不是我们的。
    """

    raw: str
    kind: LineKind
    line_no: int  # 1-based 绝对行号；与 journal 的 #L<n> 同一坐标系
    text: str | None = None  # FACT/HANDWRITTEN 的事实文本（已去掉 marker 与 ~~）
    struck: bool = False  # 文本原样被 ~~ 包裹（渲染装饰，SPEC §2.1）
    marker: Marker | None = None  # 仅 FACT 非 None
    damage: str | None = None  # 仅 DAMAGED：人类可读的原因，内嵌原文

    def rewrite(self, text: str, marker: Marker, *, struck: bool = False) -> None:
        """把这一行改写成一个事实行（merge/确认/adopt 的唯一改行入口）。

        允许作用于 ``FACT``（改写 marker 或文本）与 ``HANDWRITTEN``
        （SPEC §4 的 adopt：给手写行分配 id 并加 ``locked``）。作用于
        ``PROSE`` / ``DAMAGED`` 是调用方 bug：散文不是事实，而损坏的
        marker 按 §4 必须**原样留在文件里**等人处理，绝不能被系统
        "顺手修好"——那正是 §4 "never guess" 要防的事。

        **保留本行原有的行终止符**：在 CRLF 的档案里改一行，改完仍是
        CRLF。以"统一成 LF"为副作用的 merge 会让 `git diff` 出现一片
        无关的噪音，而 `me diff` 的全部价值就是让真正被改的行显眼。
        """
        if self.kind not in (LineKind.FACT, LineKind.HANDWRITTEN):
            raise MarkerError(
                f"cannot rewrite a {self.kind.value} line (SPEC §4: damaged markers "
                "stay untouched for quarantine, prose is never rewritten)"
            )
        self.raw = render_fact_line(text, marker, struck=struck, newline=_line_ending(self.raw))
        self.kind = LineKind.FACT
        self.text = text
        self.struck = struck
        self.marker = marker
        self.damage = None


@dataclass
class FactDocument:
    """一个事实文件（``about/`` / ``goals/`` 下的一份 markdown）的行模型。

    这是整份文件在内存里的样子：``lines`` 保序且逐行保真，``render``
    按原样接回去。M2 只在这里提供读写与分类；"发现重复 id""补建缺失文件"
    这类跨行判断是 M3 的 reconcile。
    """

    lines: list[FactLine]
    source: str = "<string>"
    # 追加新行时用哪种终止符：取文件里的多数派，空文件默认 LF。
    # 只用**新**行；既有行的终止符各自保留（见 FactLine.rewrite）。
    newline: str = "\n"

    @classmethod
    def parse(cls, text: str, *, source: str = "<string>") -> FactDocument:
        """把文件文本切成行并逐行分类（**永不抛 MarkerError**）。

        这一点是刻意的：SPEC §4 把"marker 被人手改坏"定为预期输入，
        所以解析路径的职责是**分类**而不是拒绝——坏行变成
        ``LineKind.DAMAGED`` 的数据，连同原因一起交给 reconcile 去报告
        与 quarantine。一次手滑编辑不该让整次 merge 炸掉。

        行号用绝对 1-based（与 journal 的 ``#L<n>`` 同一坐标系）：契约里
        的指针（SPEC §2.4 "``#L<n>`` references never drift"）用的就是它。

        实现细节：``splitlines(keepends=True)`` 会把 ``\\v``、``\\u2028``
        等也算作行边界——对合法档案无影响；即便真的出现，``raw`` 仍带着
        那个边界字符，``render`` 接回去依旧逐字节相同，round-trip 不破。
        """
        lines = [
            _classify(raw, line_no, source)
            for line_no, raw in enumerate(text.splitlines(keepends=True), start=1)
        ]
        return cls(lines=lines, source=source, newline=_dominant_newline(lines))

    def render(self) -> str:
        """按原样接回全部行——未触碰的文件**逐字节**等于输入（round-trip 定律）。

        这不是"尽力而为"：``raw`` 从头到尾没被拆开过，所以一致是由构造
        保证的，不依赖任何规整逻辑的正确性。
        """
        return "".join(line.raw for line in self.lines)

    def facts(self) -> list[FactLine]:
        return [line for line in self.lines if line.kind is LineKind.FACT]

    def hand_written(self) -> list[FactLine]:
        return [line for line in self.lines if line.kind is LineKind.HANDWRITTEN]

    def damaged(self) -> list[FactLine]:
        return [line for line in self.lines if line.kind is LineKind.DAMAGED]

    def append_fact(self, text: str, marker: Marker, *, struck: bool = False) -> FactLine:
        """在文件末尾追加一个事实行，返回它（新增行号对调用方有用）。

        若原文最后一行没有行终止符（手删掉了），先把它补上——否则新行会
        黏在旧行尾巴上，产出两行连成一行的事实。补的只是终止符，那一行的
        内容一个字符都没动。
        """
        newline = self.newline
        if self.lines and not _line_ending(self.lines[-1].raw):
            self.lines[-1].raw += newline
        line = FactLine(
            raw=render_fact_line(text, marker, struck=struck, newline=newline),
            kind=LineKind.FACT,
            line_no=len(self.lines) + 1,
            text=text,
            struck=struck,
            marker=marker,
        )
        self.lines.append(line)
        return line


def render_fact_line(
    text: str, marker: Marker, *, struck: bool = False, newline: str = "\n"
) -> str:
    """渲染一行事实：``- [~~]text[~~] <!-- <marker> -->``（SPEC §2.1）。

    **写侧文法闸门**（plan §8 R15）。文本里出现下列任何东西都会让这一行
    不可逆地损坏，因此在写出前就抛 :class:`~zhiwo.errors.MarkerError`：

    - 空文本（一条没有内容的事实没有意义）；
    - ``\\n`` / ``\\r``（事实是**一个**列表项，换行会把 marker 甩到别处）；
    - ``<!--`` / ``-->``（会伪造或截断 marker 本身）；
    - ``~~``（与"划线表示被取代"的渲染撞车，一行里分不清哪对是哪对）；
    - 首尾空白（会让 ``- text <!-- … -->`` 的解析结果与写出的文本不再一致）。

    ``struck`` 只影响渲染：被取代的事实留在文件里划线展示（SPEC §2.1），
    但**状态永远看 marker**，划线是给人看的装饰（plan §8 R9）。
    """
    _check_fact_text(text)
    body = f"~~{text}~~" if struck else text
    return f"- {body} <!-- {render_marker(marker)} -->{newline}"


def mentions_fact_id(text: str, fact_id: str) -> bool:
    """整份文件文本里是否**存在**对这个 id 的提及（plan §8 R8 的存在性检查）。

    用途是 M3 的 retire：一个 changelog 里的 id 在所有文件里都找不到时，
    "这条事实被手删了"的判定必须只在**确实不存在**时成立——误判会
    把一条还活着的事实退役掉，而退役是"never resurrected"（SPEC §4）。

    因此这是**存在性检测，不是解析**，两条设计由此而来：

    - 只扫 ``<!-- … -->`` 之间的内容。``[[f_051]]`` 这样的 wikilink 出现在
      span 之外，永不被当作"这条事实还在"（SPEC §2.4：链接是引用，不是副本；
      否则删掉一条事实却留着指向它的链接，就永远退不掉它了）。
    - 未闭合的 ``<!--`` 延伸到文件末尾，照样算一个 span：marker 被手改坏
      （``<!-- f_051`` 少了 ``-->``）时，这一条**仍然是存在的**——检测
      绝不因为"解析不了"而谎报缺席。
    """
    pattern = re.compile(rf"(?<![0-9A-Za-z_]){re.escape(fact_id)}(?![0-9A-Za-z_])")
    return any(pattern.search(_WIKILINK_RE.sub("", span)) for span in _comment_spans(text))


# 行终止符。用 \Z 而不是 $：$ 还会在"文末换行之前"匹配，那会把
# "abc\n" 的终止符判成空串，append_fact 的补终止符逻辑就会失效。
_LINE_ENDING_RE = re.compile(r"(\r\n|\r|\n)\Z")

# 行尾的 marker。``.*?`` 非贪婪 + ``\s*$`` 锚定，保证匹配的是**最后**一个
# 完整注释块；它前面的部分即事实文本。行内更早的 ``<!--`` 会被下面的
# 定界符检查判成 DAMAGED——一行里有两个 marker 不是"取最后一个"的问题，
# 是这行确实坏了。
_TRAILING_MARKER_RE = re.compile(r"<!--\s*(?P<inner>.*?)\s*-->\s*\Z")

# wikilink（SPEC §2.4 的 [[f_042]]）。在 mentions_fact_id 里被剥掉：
# 链接是引用，不能当作"这条事实还在文件里"的证据。
_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")

# 列表项前缀。**要求行首无缩进**：缩进的 ``  - xxx`` 归为散文。
# 取舍写在 _classify 的 docstring 里。
_BULLET = "- "


def _line_ending(raw: str) -> str:
    """取出一行末尾的行终止符（``\\n`` / ``\\r\\n`` / ``\\r``），没有则空串。"""
    match = _LINE_ENDING_RE.search(raw)
    return match.group(1) if match else ""


def _check_fact_text(text: str) -> None:
    """写侧文本闸门（plan §8 R15 的全部条款，理由见 render_fact_line）。"""
    if not text:
        raise MarkerError("fact text must not be empty")
    for banned in ("\n", "\r", "<!--", "-->", "~~"):
        if banned in text:
            raise MarkerError(
                f"fact text must not contain {banned!r} — it would break the line "
                f"grammar (plan §8 R15): {text!r}"
            )
    if text != text.strip():
        raise MarkerError(f"fact text must not start or end with whitespace: {text!r}")


def _dominant_newline(lines: Sequence[FactLine]) -> str:
    """文件里的多数派终止符——只用来决定**新**行怎么写。

    混排文件（手改出来的）不报错：既有行各自保留自己的终止符，
    我们只保证自己写出的新行与多数派一致，不制造新的混排。
    """
    crlf = sum(1 for line in lines if line.raw.endswith("\r\n"))
    lf = sum(1 for line in lines if line.raw.endswith("\n") and not line.raw.endswith("\r\n"))
    return "\r\n" if crlf > lf else "\n"


def _damaged(raw: str, line_no: int, reason: str) -> FactLine:
    """把一行标成 DAMAGED。``raw`` 原样带着——§4 的 quarantine 就是"别碰它"。"""
    return FactLine(raw=raw, kind=LineKind.DAMAGED, line_no=line_no, damage=reason)


def _classify(raw: str, line_no: int, source: str) -> FactLine:
    """把一行分到四种身份之一（SPEC §2.1 的文法 + §4 的输入面）。

    判定顺序与理由：

    1. **不以 ``- `` 开头 → 散文**。标题、说明性 HTML 注释、空行都在这里。
       缩进的列表项（``  - xxx``）也归散文：把嵌套的项目符号当事实收编，
       会把人在文件里写的**结构**变成事实，那比漏读一条缩进事实危险得多
       （漏读也不会误退役——:func:`mentions_fact_id` 不看行的形状）。
    2. **没有 ``<!--`` → 手写行**（SPEC §4 的第一种观察：无 marker 的事实行，
       下次 merge 会被 adopt）。
    3. **有 ``<!--`` 但行尾不是完整 marker → 损坏**，连"文本部分含定界符"
       一起算（一行里塞了两个注释块）。
    4. **marker 解析失败 → 损坏**：原因来自 :func:`parse_marker`，带上行号。
    5. 全过 → **事实行**；文本被 ``~~…~~`` 成对包裹则记 ``struck``
       （R9：只记录，状态永远看 marker）。

    ``text`` 一律 ``rstrip()``：``- 文本 <!-- id -->`` 里那个分隔空格属于
    格式，不属于文本。
    """
    ending = _line_ending(raw)
    body = raw[: len(raw) - len(ending)] if ending else raw

    if not body.startswith(_BULLET):
        return FactLine(raw=raw, kind=LineKind.PROSE, line_no=line_no)

    body = body[len(_BULLET) :]
    if not body.strip():
        # 只有一个空项目符号：不是一条主张，也不是待收编的手写行。
        return FactLine(raw=raw, kind=LineKind.PROSE, line_no=line_no)

    if "<!--" not in body:
        return FactLine(raw=raw, kind=LineKind.HANDWRITTEN, line_no=line_no, text=body.rstrip())

    match = _TRAILING_MARKER_RE.search(body)
    if match is None:
        if "-->" not in body[body.rindex("<!--") :]:
            reason = "unterminated marker comment (no closing '-->')"
        else:
            reason = "marker is not a complete comment at the end of the line"
        return _damaged(raw, line_no, reason)

    candidate = body[: match.start()]
    if "<!--" in candidate or "-->" in candidate:
        return _damaged(raw, line_no, "text contains an HTML comment delimiter")

    inner = match.group("inner")
    # 一行里有两个 ``<!--`` 时，非贪婪匹配会把第二个吞进 inner；那不是
    # "marker 里有个怪 token"，而是这行的注释结构本身就坏了——报原因要
    # 报得准，否则用户拿着 "unknown marker token '<!--'" 不知道该删哪一个。
    if "<!--" in inner or "-->" in inner:
        return _damaged(raw, line_no, "marker content contains an HTML comment delimiter")

    text = candidate.rstrip()
    if not text:
        return _damaged(raw, line_no, "empty fact text")

    try:
        marker = parse_marker(inner, source=f"{source}:{line_no}")
    except MarkerError as exc:
        return _damaged(raw, line_no, str(exc))

    struck = False
    if len(text) >= 4 and text.startswith("~~") and text.endswith("~~"):
        struck = True
        text = text[2:-2]
    return FactLine(
        raw=raw,
        kind=LineKind.FACT,
        line_no=line_no,
        text=text,
        struck=struck,
        marker=marker,
    )


def _comment_spans(text: str) -> Iterator[str]:
    """逐个产出 ``<!--`` 与 ``-->`` **之间**的内容（不含定界符）。

    未闭合的 ``<!--`` 产出"到文末为止"的剩余内容——这是 :func:`mentions_fact_id`
    "损坏 marker 仍算存在"的实现所在。
    """
    pos = 0
    while True:
        start = text.find("<!--", pos)
        if start == -1:
            return
        end = text.find("-->", start + 4)
        if end == -1:
            yield text[start + 4 :]
            return
        yield text[start + 4 : end]
        pos = end + 3
