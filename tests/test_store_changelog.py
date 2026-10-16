"""changelog 读写的测试（plan §3 决策 6/8、§4 编码法、§7 M2 的冲突后缀与 CJK）。

changelog 是唯一永久保存 evidence 的地方（SPEC §2.1 的三处一权表），
所以这里的两条关键断言是：**文件名永不覆盖**（一次 merge 一个文件）
与**中文 evidence 在磁盘上仍是可读的 UTF-8**。
"""

from datetime import datetime
from pathlib import Path

import pytest

from zhiwo.errors import SchemaError
from zhiwo.models import ChangelogEntry, ChangelogPatch, FactKind, PatchOp
from zhiwo.store import Dossier

FIXED = datetime(2026, 10, 12, 21, 30, 0)
CJK_EVIDENCE = "面试挂了，二面，反馈说项目深度不够。"


def _entry(evidence: str = "一条逐字证据", merge: str = "placeholder") -> ChangelogEntry:
    return ChangelogEntry(
        merge=merge,
        input="journal/2026-10-12.md",
        extractor="mock-v0",
        patches=[
            ChangelogPatch(
                op=PatchOp.ADD,
                id="f_100",
                target="about/career.md",
                kind=FactKind.STATEMENT,
                text="二面反馈：项目深度不够",
                evidence=evidence,
                source="journal/2026-10-12.md#L3",
            )
        ],
    )


# --- 文件名（SPEC §1：带秒 + 绝不覆盖）-----------------------------------


def test_filenames_carry_seconds_and_never_overwrite(dossier: Dossier) -> None:
    """plan §7 M2 的关键测试：同一秒内的多次 merge 靠 ``-2`` 后缀共存。"""
    first = dossier.write_changelog(_entry(), now=FIXED)
    second = dossier.write_changelog(_entry(), now=FIXED)
    third = dossier.write_changelog(_entry(), now=FIXED)

    assert first.name == "2026-10-12T213000.yaml"
    assert second.name == "2026-10-12T213000-2.yaml"
    assert third.name == "2026-10-12T213000-3.yaml"
    assert len({first, second, third}) == 3
    assert all(path.is_file() for path in (first, second, third))


def test_the_merge_id_is_the_filename_stem(dossier: Dossier) -> None:
    """SPEC §1：条目里的 merge 与文件名 stem 一致——由写入方保证，不靠调用方。"""
    path = dossier.write_changelog(_entry(merge="我瞎填的"), now=FIXED)
    assert path.stem == "2026-10-12T213000"
    entries = [entry for entry in dossier.read_changelog() if entry.merge == path.stem]
    assert len(entries) == 1


def test_a_different_second_gets_a_fresh_name(dossier: Dossier) -> None:
    first = dossier.write_changelog(_entry(), now=FIXED)
    later = dossier.write_changelog(_entry(), now=datetime(2026, 10, 12, 21, 30, 1))
    assert first.name == "2026-10-12T213000.yaml"
    assert later.name == "2026-10-12T213001.yaml"


# --- 写盘形态（plan §3 决策 6、models 钉死的 recipe）---------------------


def test_schema_version_is_written_as_one(dossier: Dossier) -> None:
    """OQ#3 的 MVP 答案（plan §3 决策 6）：写出的条目恒带 ``schema_version: 1``。"""
    path = dossier.write_changelog(_entry(), now=FIXED)
    assert "schema_version: 1" in path.read_text(encoding="utf-8")


def test_yaml_uses_the_contract_key_from_not_from_fact(dossier: Dossier) -> None:
    """``by_alias=True`` 的硬性要求（models.ChangelogEntry 的 docstring）。

    漏掉它，写出的键是 Python 侧规避关键字用的 ``from_fact``——那是一份
    读起来像契约、其实不是契约的数据，比直接报错更糟。
    """
    entry = ChangelogEntry(
        merge="placeholder",
        input="journal/2026-10-12.md",
        extractor="mock-v0",
        patches=[ChangelogPatch(op=PatchOp.LINK, from_fact="f_042", to="f_017")],
    )
    path = dossier.write_changelog(entry, now=FIXED)
    text = path.read_text(encoding="utf-8")
    assert "from: f_042" in text
    assert "from_fact" not in text
    # 枚举写的是值，不是 !!python/object 标签。
    assert "op: link" in text
    assert "!!python" not in text


def test_cjk_evidence_stays_readable_utf8_on_disk(dossier: Dossier) -> None:
    """plan §7 M2 的关键测试 + plan §4 编码法。

    直接看**原始字节**：转义成 ``\\uXXXX`` 的"中文"在编辑器里是一堆
    看不懂的符号——plain-file transparency 就此失效，而这一条正是
    changelog 存在的意义所在（唯一永久保存 evidence 的地方）。
    """
    path = dossier.write_changelog(_entry(evidence=CJK_EVIDENCE), now=FIXED)
    raw = path.read_bytes()

    assert CJK_EVIDENCE.encode("utf-8") in raw
    assert b"\\u" not in raw
    assert raw.decode("utf-8").count(CJK_EVIDENCE) == 1


def test_written_entry_round_trips_through_the_reader(dossier: Dossier) -> None:
    dossier.write_changelog(_entry(evidence=CJK_EVIDENCE), now=FIXED)
    written = [entry for entry in dossier.read_changelog() if entry.merge == "2026-10-12T213000"]
    assert len(written) == 1
    patch = written[0].patches[0]
    assert patch.evidence == CJK_EVIDENCE
    assert patch.source == "journal/2026-10-12.md#L3"
    assert written[0].schema_version == 1


# --- 读取（plan §8 R5：读者比写者宽容）----------------------------------


def test_reader_accepts_the_minute_precision_golden_entry(dossier: Dossier) -> None:
    """黄金条目是分钟精度，写入是秒精度；读者的宽容度不该小于写入者（R5）。

    注意黄金示例里 ``merge`` 帶着 ``m_`` 前缀，与文件名 stem 并不一致
    （SPEC §1 要求二者一致）——示例是**说明性数据**，按 R5 的姿态保持不动；
    我们的写入方则严格保证一致（见 test_the_merge_id_is_the_filename_stem）。
    读取器对两者都接受：它读的是内容，不是命名规约。
    """
    entries = dossier.read_changelog()
    golden = [entry for entry in entries if entry.merge == "m_2026-10-11T2130"]
    assert len(golden) == 1
    assert golden[0].extractor == "mock-v0"
    assert golden[0].input == "journal/2026-10-11.md"
    assert {patch.id for patch in golden[0].patches} == {"f_051", "f_043"}


def test_reader_sorts_by_filename_not_by_merge_field(dossier: Dossier) -> None:
    """排序按**文件名**（= 时间序，SPEC §1），不按内容里的 merge 字段。

    两者的差别是真实的：黄金条目的 merge 是 ``m_2026-10-11T2130``。若按
    merge 排序，``m_`` 前缀会把它拍到所有数字开头的名字之后——顺序就错了。
    """
    dossier.write_changelog(_entry(), now=FIXED)
    dossier.write_changelog(_entry(), now=datetime(2026, 10, 13, 9, 0))
    names = [path.name for path in sorted(dossier.changelog_dir.glob("*.yaml"))]
    merges = [entry.merge for entry in dossier.read_changelog()]
    assert names == ["2026-10-11T2130.yaml", "2026-10-12T213000.yaml", "2026-10-13T090000.yaml"]
    assert merges == ["m_2026-10-11T2130", "2026-10-12T213000", "2026-10-13T090000"]


def test_missing_changelog_directory_reads_as_empty(tmp_path: Path) -> None:
    """还没发生过 merge 的档案是合法的。"""
    from zhiwo.store import bootstrap_dossier

    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "changelog").rmdir()
    assert dossier.read_changelog() == []


def test_a_broken_entry_names_the_file(dossier: Dossier) -> None:
    """坏条目要指认到文件：用户得知道去改哪一个。"""
    (dossier.changelog_dir / "2026-10-12T220000.yaml").write_text(
        "merge: 只有一个字段\n", encoding="utf-8"
    )
    with pytest.raises(SchemaError) as excinfo:
        dossier.read_changelog()
    assert "2026-10-12T220000.yaml" in str(excinfo.value)


def test_non_utf8_entry_is_a_schema_error(dossier: Dossier) -> None:
    (dossier.changelog_dir / "2026-10-12T220000.yaml").write_bytes("中文".encode("gb18030"))
    with pytest.raises(SchemaError):
        dossier.read_changelog()


def test_ignored_keys_do_not_break_the_reader(dossier: Dossier) -> None:
    """changelog 是长期格式：未来版本加的字段不能让旧读取器崩掉（SPEC 前言）。"""
    (dossier.changelog_dir / "2026-10-12T220000.yaml").write_text(
        "merge: 2026-10-12T220000\n"
        "input: journal/2026-10-12.md\n"
        "extractor: mock-v0\n"
        "future_field: 未来版本才有的东西\n"
        "patches: []\n",
        encoding="utf-8",
    )
    entries = [entry for entry in dossier.read_changelog() if entry.merge == "2026-10-12T220000"]
    assert len(entries) == 1
    assert entries[0].patches == []
