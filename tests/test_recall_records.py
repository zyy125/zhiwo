"""逻辑记录 join 与 provenance（SPEC §2.1 / §2.4）。

这些测试钉住“谁拥有哪一半事实”：

- 文件拥有文本、存活状态、locked 与关系边；
- changelog 拥有 kind、source、创建/更新时间；
- 缺一半时留 ``None``，不拿另一边猜；
- archived 只能由 applied ``retire`` 产生。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from zhiwo.errors import ZhiwoError
from zhiwo.factfile import Marker
from zhiwo.models import (
    ChangelogEntry,
    ChangelogPatch,
    FactKind,
    FactStatus,
    PatchOp,
    PatchResult,
)
from zhiwo.recall import FactRecord, logical_records, provenance
from zhiwo.store import Dossier


def _records(dossier: Dossier) -> dict[str, FactRecord]:
    return {record.id: record for record in logical_records(dossier)}


def test_logical_record_for_f_051_joins_file_and_changelog(dossier: Dossier) -> None:
    """plan §7 M6 的关键测试：一条事实的完整 §2.1 记录。"""
    record = _records(dossier)["f_051"]

    assert record.id == "f_051"
    assert record.text == "二面反馈：项目深度不够（2026-10-11）"
    assert record.kind is FactKind.STATEMENT
    assert record.status is FactStatus.ACTIVE
    assert record.locked is False
    assert record.supersedes is None
    assert record.superseded_by is None
    assert record.created == "2026-10-11"
    assert record.updated == "2026-10-11"
    assert record.source == "journal/2026-10-11.md#L7"
    assert record.section_id == "career"
    assert record.path == "about/career.md"
    assert record.line_no == 6


def test_logical_record_keeps_pending_and_superseded_status_from_files(
    dossier: Dossier,
) -> None:
    records = _records(dossier)

    pending = records["f_043"]
    assert pending.kind is FactKind.INFERENCE
    assert pending.status is FactStatus.PENDING
    assert pending.section_id == "current"

    superseded = records["f_009"]
    assert superseded.status is FactStatus.SUPERSEDED
    assert superseded.superseded_by == "f_017"

    replacement = records["f_017"]
    assert replacement.supersedes == "f_009"


def test_facts_without_changelog_have_unknown_provenance_not_guesses(
    dossier: Dossier,
) -> None:
    record = _records(dossier)["f_001"]

    assert record.kind is None
    assert record.created is None
    assert record.updated is None
    assert record.source is None


def test_archived_record_comes_from_a_creation_and_a_retire_event(
    dossier: Dossier,
) -> None:
    add = ChangelogEntry(
        merge="placeholder",
        input="journal/2026-12-01.md",
        extractor="mock-v0",
        patches=[
            ChangelogPatch(
                op=PatchOp.ADD,
                id="f_900",
                target="about/career.md",
                kind=FactKind.STATEMENT,
                text="一条后来被手删的事实",
                evidence="一条后来被手删的事实",
                source="journal/2026-12-01.md#L3",
            )
        ],
    )
    retire = ChangelogEntry(
        merge="placeholder",
        input="journal/2026-12-02.md",
        extractor="mock-v0",
        patches=[
            ChangelogPatch(
                op=PatchOp.RETIRE,
                id="f_900",
                reason="id is absent from every fact file",
            )
        ],
    )
    dossier.write_changelog(add, now=datetime(2026, 12, 1, 9, 0))
    dossier.write_changelog(retire, now=datetime(2026, 12, 2, 9, 0))

    record = _records(dossier)["f_900"]

    assert record.status is FactStatus.ARCHIVED
    assert record.text == "一条后来被手删的事实"
    assert record.kind is FactKind.STATEMENT
    assert record.created == "2026-12-01"
    assert record.updated == "2026-12-02"
    assert record.source == "journal/2026-12-01.md#L3"
    assert record.section_id == "career"
    assert record.path == "about/career.md"
    assert record.line_no is None


def test_duplicate_ids_fail_loud_and_name_both_locations(dossier: Dossier) -> None:
    section = next(
        section for section in dossier.load_profile().sections if section.path == "about/career.md"
    )
    document = dossier.read_fact_document(section)
    assert document is not None
    document.append_fact("重复的身份", Marker("f_051"))
    dossier.write_fact_document(section, document)

    with pytest.raises(ZhiwoError, match="duplicate fact id f_051"):
        logical_records(dossier)


# --- provenance（SPEC §2.4）------------------------------------------------


def test_provenance_reports_evidence_and_one_line_of_context(dossier: Dossier) -> None:
    events = provenance(dossier, "f_051")

    assert len(events) == 1
    event = events[0]
    assert event.merge == "m_2026-10-11T2130"
    assert event.input == "journal/2026-10-11.md"
    assert event.extractor == "mock-v0"
    assert event.patch.op is PatchOp.ADD
    assert event.patch.kind is FactKind.STATEMENT
    assert event.patch.evidence == "面试挂了，二面，反馈说项目深度不够。"
    assert event.patch.source == "journal/2026-10-11.md#L7"
    assert [line.line_no for line in event.context] == [6, 7, 8]
    assert event.context[1].text == "- 面试挂了，二面，反馈说项目深度不够。"
    assert event.context_error is None


def test_provenance_includes_rejected_events_without_changing_records(
    dossier: Dossier,
) -> None:
    rejected = ChangelogEntry(
        merge="placeholder",
        input="journal/2026-12-03.md",
        extractor="mock-v0",
        patches=[
            ChangelogPatch(
                op=PatchOp.SUPERSEDE,
                target_id="f_051",
                result=PatchResult.REJECTED,
                reason="draft 0: target is locked (SPEC §4)",
            )
        ],
    )
    dossier.write_changelog(rejected, now=datetime(2026, 12, 3, 9, 0))

    events = provenance(dossier, "f_051")
    record = _records(dossier)["f_051"]

    assert [event.patch.result for event in events] == [
        PatchResult.APPLIED,
        PatchResult.REJECTED,
    ]
    assert record.updated == "2026-10-11"  # 拒绝不改变事实状态/时间


def test_provenance_reports_a_broken_source_pointer_as_data(dossier: Dossier) -> None:
    broken = ChangelogEntry(
        merge="placeholder",
        input="journal/2026-12-04.md",
        extractor="mock-v0",
        patches=[
            ChangelogPatch(
                op=PatchOp.ADD,
                id="f_998",
                target="about/career.md",
                kind=FactKind.STATEMENT,
                text="来源坏掉的事实",
                evidence="来源坏掉的事实",
                source="not-a-pointer",
            )
        ],
    )
    dossier.write_changelog(broken, now=datetime(2026, 12, 4, 9, 0))

    event = provenance(dossier, "f_998")[0]

    assert event.context == ()
    assert event.context_error is not None
    assert "malformed source pointer" in event.context_error
