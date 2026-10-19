"""journal 追加的测试（plan §5 步骤 2：capture 的持久性锚点）。

plan §7 M2 的关键测试之一在这里：**CJK 证据在磁盘上仍是可读的 UTF-8**
——journal 是第一站，它先坏了，"证据逐字"就无从谈起。
"""

from datetime import datetime
from pathlib import Path

import pytest

from zhiwo.errors import ZhiwoError
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
