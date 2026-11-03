"""journal 追加的测试（plan §5 步骤 2：capture 的持久性锚点）。

plan §7 M2 的关键测试之一在这里：**CJK 证据在磁盘上仍是可读的 UTF-8**
——journal 是第一站，它先坏了，"证据逐字"就无从谈起。
"""

from datetime import datetime
from pathlib import Path

import pytest

from zhiwo.errors import SchemaError, ZhiwoError
from zhiwo.store import Dossier, bootstrap_dossier

GOLDEN_DAY = datetime(2026, 10, 11, 22, 30, 0)


def test_append_extends_the_golden_journal_in_place(dossier: Dossier) -> None:
    """追加到已有文件：新行落在既有内容之后，既有字节一个都没动。

    ``first_line`` / ``last_line`` 是**落盘后**的绝对行号——黄金档案的
    ``#L7`` 指针与它是同一坐标系，M4 的证据校验就靠它划定"这次捕获的行范围"。
    """
    path = dossier.journal_dir() / "2026-10-11.md"
    before = path.read_bytes()

    result = dossier.append_journal(["晚上又面了一家。", "拿到了口头 offer。"], now=GOLDEN_DAY)

    assert result.path == path
    assert (result.first_line, result.last_line) == (9, 10)
    assert path.read_bytes().startswith(before)  # 只追加，历史未动（SPEC §1）
    assert path.read_text(encoding="utf-8").splitlines()[8] == "- 晚上又面了一家。"


def test_append_creates_the_daily_file_with_a_heading(dossier: Dossier) -> None:
    """新的一天：建文件、写标题、行号从 3 起（标题 + 空行占前两行）。"""
    result = dossier.append_journal(["今天开始记。", "第二行。"], now=datetime(2026, 12, 1, 9, 0))

    assert result.path == dossier.journal_dir() / "2026-12-01.md"
    assert (result.first_line, result.last_line) == (3, 4)
    assert result.path.read_text(encoding="utf-8") == "# 2026-12-01\n\n- 今天开始记。\n- 第二行。\n"


def test_daily_file_is_named_by_local_date(dossier: Dossier) -> None:
    """按本地日期分文件——跨日的两次捕获不会写进同一个文件。"""
    first = dossier.append_journal(["昨晚的一句"], now=datetime(2026, 10, 12, 23, 50))
    second = dossier.append_journal(["今早的一句"], now=datetime(2026, 10, 13, 0, 10))
    assert first.path != second.path
    assert first.path.name == "2026-10-12.md"
    assert second.path.name == "2026-10-13.md"


def test_append_repairs_a_missing_trailing_newline(dossier: Dossier) -> None:
    """手删掉文末换行后追加：补一个终止符，旧行内容一字不改。

    不补的话新行会黏在旧行尾巴上，产出一条**合并了两个捕获**的 journal 行——
    而"一行 = 一次捕获"是证据校验与 provenance 的共同前提。
    """
    path = dossier.journal_dir() / "2026-10-11.md"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.rstrip("\n"), encoding="utf-8")
    without_final_newline = path.read_text(encoding="utf-8")

    result = dossier.append_journal(["补上的一句"], now=GOLDEN_DAY)

    text = path.read_text(encoding="utf-8")
    old_lines = without_final_newline.splitlines()
    assert text.splitlines()[: len(old_lines)] == old_lines
    assert text.splitlines()[len(old_lines)] == "- 补上的一句"
    assert result.first_line == len(old_lines) + 1


def test_append_survives_an_empty_existing_file(dossier: Dossier) -> None:
    """空文件（比如手工 ``touch`` 出来的）按"新的一天"处理，不写进空气里。"""
    path = dossier.journal_dir() / "2026-10-11.md"
    path.write_text("", encoding="utf-8")
    result = dossier.append_journal(["重新开始"], now=GOLDEN_DAY)
    assert path.read_text(encoding="utf-8") == "# 2026-10-11\n\n- 重新开始\n"
    assert result.first_line == 3


def test_cjk_journal_lines_stay_readable_utf8(dossier: Dossier) -> None:
    """plan §7 M2 的关键测试：中文在磁盘上是可读的 UTF-8，不是转义序列。"""
    line = "面试挂了，二面，反馈说项目深度不够。"
    result = dossier.append_journal([line], now=datetime(2026, 10, 12, 8, 0))

    raw = result.path.read_bytes()
    assert line.encode("utf-8") in raw
    assert b"\\u" not in raw
    assert result.path.read_text(encoding="utf-8").endswith(f"- {line}\n")


@pytest.mark.parametrize("bad", ["", "   ", "\t", "两行\n一起", "带回车\r一起"])
def test_journal_lines_must_be_single_and_nonempty(dossier: Dossier, bad: str) -> None:
    """一次捕获一行：多行会让 ``#L<n>`` 与"一次捕获"的对应关系含糊。"""
    with pytest.raises(ZhiwoError):
        dossier.append_journal([bad], now=datetime(2026, 12, 5, 9, 0))
    assert not (dossier.journal_dir() / "2026-12-05.md").exists()


def test_a_rejected_append_leaves_the_existing_file_untouched(dossier: Dossier) -> None:
    """坏输入不该留下半次追加——校验发生在打开文件之前。"""
    path = dossier.journal_dir() / "2026-10-11.md"
    before = path.read_bytes()
    with pytest.raises(ZhiwoError):
        dossier.append_journal(["好的一行", "坏的一行\n其实有两行"], now=GOLDEN_DAY)
    assert path.read_bytes() == before


def test_append_strips_surrounding_whitespace(dossier: Dossier) -> None:
    """行的首尾空白属于输入时的噪音，不属于捕获内容。"""
    result = dossier.append_journal(["  留白的一句话  "], now=datetime(2026, 12, 6, 9, 0))
    assert result.path.read_text(encoding="utf-8").endswith("- 留白的一句话\n")


def test_append_needs_at_least_one_line(dossier: Dossier) -> None:
    """一次"什么都没记"的捕获是调用方的 bug：报错，而不是建一个空壳文件。"""
    with pytest.raises(ZhiwoError):
        dossier.append_journal([], now=datetime(2026, 12, 7, 9, 0))
    assert not (dossier.journal_dir() / "2026-12-07.md").exists()


def test_append_works_when_the_journal_directory_is_missing(tmp_path: Path) -> None:
    """journal 目录被手删了：追加时补建，而不是报错（capture 永不阻塞）。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    dossier.journal_dir().rmdir()  # 新建的 journal/ 是空的，直接删掉
    append = dossier.append_journal(["目录被删了也要能记"], now=datetime(2026, 10, 12, 9, 0))
    assert append.path.is_file()


# --- 读回捕获区间（M4 证据校验的盘上 haystack）----------------------------


def test_read_journal_entries_returns_capture_texts(dossier: Dossier) -> None:
    """读回的是**捕获文本**：项目符号被剥掉，行号与 ``#L<n>`` 同一坐标系。"""
    assert dossier.read_journal_entries("journal/2026-10-11.md", first_line=7, last_line=7) == (
        "面试挂了，二面，反馈说项目深度不够。",
    )


def test_read_journal_entries_keeps_cjk_and_order(dossier: Dossier) -> None:
    """多行区间按文件顺序返回，中文逐字保留（编码法）。"""
    assert dossier.read_journal_entries("journal/2026-10-11.md", first_line=6, last_line=8) == (
        "昨晚只睡了六个小时，下午有个面试。",
        "面试挂了，二面，反馈说项目深度不够。",
        "和家里通电话，聊到毕业后回不回老家。",
    )


def test_read_journal_entries_leaves_unbulleted_lines_alone(dossier: Dossier) -> None:
    """手改掉 ``- `` 前缀的行原样返回——证据仍有机会匹配，格式不代修。"""
    path = dossier.journal_dir() / "2026-10-11.md"
    path.write_text(path.read_text(encoding="utf-8") + "没有项目符号的一行\n", encoding="utf-8")
    assert dossier.read_journal_entries("journal/2026-10-11.md", first_line=9, last_line=9) == (
        "没有项目符号的一行",
    )


@pytest.mark.parametrize(
    ("first_line", "last_line"),
    [(0, 1), (7, 6), (99, 99)],
)
def test_read_journal_entries_rejects_bad_ranges(
    dossier: Dossier, first_line: int, last_line: int
) -> None:
    """越界/倒置不是"读回空"：证据校验对着空气通过比报错危险得多。"""
    with pytest.raises(ZhiwoError):
        dossier.read_journal_entries(
            "journal/2026-10-11.md", first_line=first_line, last_line=last_line
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "about/career.md",  # 事实文件不是 journal
        "journal/../about/career.md",  # 目录逃逸
        "./journal/2026-10-11.md",  # 非规范相对路径
        "journal\\2026-10-11.md",  # 反斜杠不是 POSIX 分隔符
        "/etc/passwd",  # 绝对路径
    ],
)
def test_read_journal_entries_rejects_paths_outside_the_journal(
    dossier: Dossier, relative_path: str
) -> None:
    """source 指针只能是 journal 里的行；路径逃逸在 L0 就拦住。"""
    with pytest.raises(ZhiwoError):
        dossier.read_journal_entries(relative_path, first_line=1, last_line=1)


def test_read_journal_entries_missing_file_is_a_schema_error(dossier: Dossier) -> None:
    """文件不存在属于"读不出来"，与 L1/L0 的其他读取失败同一姿态。"""
    with pytest.raises(SchemaError):
        dossier.read_journal_entries("journal/1999-01-01.md", first_line=1, last_line=1)


# --- 全量捕获扫描（M6 检索）---------------------------------------------


def test_journal_entries_skip_headings_and_comments_but_keep_line_numbers(
    dossier: Dossier,
) -> None:
    """标题/空行/HTML 注释不是捕获；它们仍占行号，指针不能重编号。"""
    entries = dossier.journal_entries()

    assert [(entry.path, entry.line_no, entry.text) for entry in entries] == [
        ("journal/2026-10-11.md", 6, "昨晚只睡了六个小时，下午有个面试。"),
        ("journal/2026-10-11.md", 7, "面试挂了，二面，反馈说项目深度不够。"),
        ("journal/2026-10-11.md", 8, "和家里通电话，聊到毕业后回不回老家。"),
    ]


def test_journal_entries_keep_a_hand_edited_line_without_a_bullet(dossier: Dossier) -> None:
    """手改掉 ``- `` 前缀的行仍是一条捕获：检索不该比证据校验更严格。"""
    path = dossier.journal_dir() / "2026-10-11.md"
    path.write_text(path.read_text(encoding="utf-8") + "没有项目符号的一行\n", encoding="utf-8")

    last = dossier.journal_entries()[-1]

    assert (last.line_no, last.text) == (9, "没有项目符号的一行")


def test_journal_entries_skip_a_multi_line_html_comment(dossier: Dossier) -> None:
    """跨行注释里的文字不能被当成捕获——否则搜索会命中说明文档。"""
    path = dossier.journal_dir() / "2026-10-11.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "<!-- 注释开始\n注释中间的捕获假象\n-->\n真实一行\n",
        encoding="utf-8",
    )

    entries = dossier.journal_entries()

    assert [entry.text for entry in entries][-1] == "真实一行"
    assert all("注释中间" not in entry.text for entry in entries)


# --- 任意文件的 ±1 行上下文（M6 provenance）------------------------------


def test_read_line_context_returns_the_source_line_plus_neighbors(dossier: Dossier) -> None:
    """journal 与事实文件共用同一坐标；上下文保留原始行（含项目符号）。"""
    assert dossier.read_line_context("journal/2026-10-11.md", 7) == (
        (6, "- 昨晚只睡了六个小时，下午有个面试。"),
        (7, "- 面试挂了，二面，反馈说项目深度不够。"),
        (8, "- 和家里通电话，聊到毕业后回不回老家。"),
    )
    assert dossier.read_line_context("about/values.md", 9) == (
        (8, "- 独处充电，不喜欢频繁社交 <!-- f_002 -->"),
        (9, "- 遇到大决定习惯列出所有选项再逐个排除 <!-- f_003 locked -->"),
        (10, "- ~~想读博~~ <!-- f_009 superseded-by:f_017 -->"),
    )


def test_read_line_context_clips_at_file_boundaries(dossier: Dossier) -> None:
    assert dossier.read_line_context("journal/2026-10-11.md", 1, radius=1) == (
        (1, "# 2026-10-11"),
        (2, ""),
    )
    assert dossier.read_line_context("journal/2026-10-11.md", 8, radius=1) == (
        (7, "- 面试挂了，二面，反馈说项目深度不够。"),
        (8, "- 和家里通电话，聊到毕业后回不回老家。"),
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../about/career.md",
        "./journal/2026-10-11.md",
        "journal\\2026-10-11.md",
        "/etc/passwd",
    ],
)
def test_read_line_context_rejects_paths_outside_the_dossier(
    dossier: Dossier, relative_path: str
) -> None:
    with pytest.raises(ZhiwoError):
        dossier.read_line_context(relative_path, 1)


def test_read_line_context_rejects_an_out_of_bounds_line(dossier: Dossier) -> None:
    with pytest.raises(ZhiwoError, match="out of bounds"):
        dossier.read_line_context("journal/2026-10-11.md", 99)
