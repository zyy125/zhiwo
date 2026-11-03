"""M8 的 review 写路径：confirm / edit / reject 与延迟 supersede。

这些测试钉住 SPEC §2.3 的延迟边语义、OQ#4 的"第一个确认获胜"、以及
D-011 的三个人工 op。它们大多直接调用 pipeline，避免把锁、commit 与
CLI 按键混在一条断言里；CLI 的脚本按键另有测试。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from zhiwo.errors import ZhiwoError
from zhiwo.llm import MockProvider
from zhiwo.merge import ReviewAction
from zhiwo.models import FactStatus, PatchOp
from zhiwo.pipeline import capture, pending_reviews, review_pending
from zhiwo.recall import logical_records
from zhiwo.reconcile import reconcile_dossier
from zhiwo.store import Dossier, bootstrap_dossier

FIXED = datetime(2026, 11, 3, 12, 0, 0)
CAPTURE = "想清楚了，先工作，不读博。"
DEFERRED_RESPONSE = json.dumps(
    [
        {
            "op": "add",
            "target": "about/career.md",
            "kind": "statement",
            "text": "决定直接工作，不读博",
            "evidence": CAPTURE,
        },
        {
            "op": "supersede",
            "target_id": "f_009",
            "kind": "inference",
            "text": "也许该直接工作",
            "evidence": "想清楚了，先工作",
        },
    ],
    ensure_ascii=False,
)


def _commit_count(root: Path) -> int:
    output = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(output)


def _deferred_dossier(tmp_path: Path) -> Dossier:
    """准备一个带延迟 supersede 的档案：f_011 pending → f_009 active。

    f_009 是手写在 values.md 里的事实（模拟黄金示例的既有事实）；
    一次捕获产生 statement f_010 与 inference supersede f_011。这样
    review 的两行改写有真实的落盘坐标与 changelog 创建事件。
    """
    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "about" / "values.md").write_text("- 想读博 <!-- f_009 -->\n", encoding="utf-8")
    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider(DEFERRED_RESPONSE, identity="mock-review"),
        now=FIXED,
    )
    assert [patch.id for patch in outcome.merge.applied] == ["f_010", "f_011"]
    assert outcome.pending_total == 1
    return dossier


def test_confirm_applies_the_deferred_supersede(tmp_path: Path, git_identity: None) -> None:
    """确认 deferred pair：新行去掉 pending，目标行划掉并加 superseded-by。"""
    dossier = _deferred_dossier(tmp_path)
    before = _commit_count(dossier.root)

    outcome = review_pending(dossier, "f_011", ReviewAction.CONFIRM, now=FIXED)

    assert outcome.merge is not None
    values = (dossier.root / "about" / "values.md").read_text(encoding="utf-8")
    assert "- ~~想读博~~ <!-- f_009 superseded-by:f_011 -->" in values
    assert "- 也许该直接工作 <!-- f_011 supersedes:f_009 -->" in values
    assert _commit_count(dossier.root) == before + 1

    entry = dossier.read_changelog()[-1]
    assert entry.input == "review"
    assert entry.extractor == "human"
    patch = entry.patches[-1]
    assert patch.op is PatchOp.CONFIRM
    assert patch.id == "f_011"
    assert patch.target_id == "f_009"

    by_id = {record.id: record for record in logical_records(dossier)}
    assert by_id["f_009"].status is FactStatus.SUPERSEDED
    assert by_id["f_011"].status is FactStatus.ACTIVE
    assert by_id["f_011"].supersedes == "f_009"


def test_confirm_without_an_edge_only_activates(tmp_path: Path, git_identity: None) -> None:
    """没有延迟边的 pending 推断确认后只是 active，不触碰任何目标。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    response = json.dumps(
        [
            {
                "op": "add",
                "target": "goals/current.md",
                "kind": "inference",
                "text": "需要补项目深度",
                "evidence": CAPTURE,
            }
        ],
        ensure_ascii=False,
    )
    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider(response, identity="mock-review"),
        now=FIXED,
    )
    new_id = outcome.merge.applied[0].id
    assert new_id is not None

    reviewed = review_pending(dossier, new_id, ReviewAction.CONFIRM, now=FIXED)

    assert reviewed.merge is not None
    current = (dossier.root / "goals" / "current.md").read_text(encoding="utf-8")
    assert f"- 需要补项目深度 <!-- {new_id} -->" in current
    patch = dossier.read_changelog()[-1].patches[-1]
    assert patch.op is PatchOp.CONFIRM
    assert patch.target_id is None


def test_target_change_blocks_the_edge_then_allows_standalone_confirm(
    tmp_path: Path, git_identity: None
) -> None:
    """OQ#4：目标被手锁后，confirm 不自动改写；standalone 才丢弃延迟边。"""
    dossier = _deferred_dossier(tmp_path)
    values_path = dossier.root / "about" / "values.md"
    values_path.write_text(
        values_path.read_text(encoding="utf-8").replace(
            "- 想读博 <!-- f_009 -->", "- 想读博 <!-- f_009 locked -->"
        ),
        encoding="utf-8",
    )
    changelog_before = {path.name for path in (dossier.root / "changelog").glob("*.yaml")}
    commits_before = _commit_count(dossier.root)

    blocked = review_pending(dossier, "f_011", ReviewAction.CONFIRM, now=FIXED)

    assert blocked.merge is None
    assert blocked.target_changed is True
    assert blocked.target_id == "f_009"
    assert {path.name for path in (dossier.root / "changelog").glob("*.yaml")} == changelog_before
    assert _commit_count(dossier.root) == commits_before
    values = values_path.read_text(encoding="utf-8")
    assert "- 想读博 <!-- f_009 locked -->" in values
    assert "- 也许该直接工作 <!-- f_011 supersedes:f_009 pending -->" in values

    standalone = review_pending(dossier, "f_011", ReviewAction.CONFIRM, standalone=True, now=FIXED)

    assert standalone.merge is not None
    values = values_path.read_text(encoding="utf-8")
    assert "- 想读博 <!-- f_009 locked -->" in values
    assert "- 也许该直接工作 <!-- f_011 -->" in values
    assert "supersedes" not in values.split("- 也许该直接工作")[1]
    patch = dossier.read_changelog()[-1].patches[-1]
    assert patch.op is PatchOp.CONFIRM
    assert patch.target_id is None
    assert patch.reason is not None and "standalone" in patch.reason

    by_id = {record.id: record for record in logical_records(dossier)}
    assert by_id["f_009"].status is FactStatus.ACTIVE
    assert by_id["f_011"].status is FactStatus.ACTIVE


def test_edit_locks_the_fact_and_drops_the_deferred_edge(
    tmp_path: Path, git_identity: None
) -> None:
    """edit 是人工新断言：locked、active、没有 supersedes，目标不被划掉。"""
    dossier = _deferred_dossier(tmp_path)

    outcome = review_pending(
        dossier,
        "f_011",
        ReviewAction.EDIT,
        text="先工作两年再考虑读博",
        now=FIXED,
    )

    assert outcome.merge is not None
    values = (dossier.root / "about" / "values.md").read_text(encoding="utf-8")
    assert "- 想读博 <!-- f_009 -->" in values
    assert "- 先工作两年再考虑读博 <!-- f_011 locked -->" in values
    assert "supersedes:f_009" not in values.split("- 先工作两年再考虑读博")[1]

    patch = dossier.read_changelog()[-1].patches[-1]
    assert patch.op is PatchOp.EDIT
    assert patch.text == "先工作两年再考虑读博"
    assert patch.reason is not None and "dropped" in patch.reason

    by_id = {record.id: record for record in logical_records(dossier)}
    assert by_id["f_011"].status is FactStatus.ACTIVE
    assert by_id["f_011"].locked is True
    assert by_id["f_011"].supersedes is None


def test_edit_rejects_text_that_breaks_the_line_grammar(tmp_path: Path, git_identity: None) -> None:
    dossier = _deferred_dossier(tmp_path)
    changelog_before = {path.name for path in (dossier.root / "changelog").glob("*.yaml")}
    commits_before = _commit_count(dossier.root)

    with pytest.raises(ZhiwoError, match="line grammar"):
        review_pending(dossier, "f_011", ReviewAction.EDIT, text="坏~~文本", now=FIXED)

    assert {path.name for path in (dossier.root / "changelog").glob("*.yaml")} == changelog_before
    assert _commit_count(dossier.root) == commits_before
    assert "- 也许该直接工作 <!-- f_011 supersedes:f_009 pending -->" in (
        dossier.root / "about" / "values.md"
    ).read_text(encoding="utf-8")


def test_reject_removes_the_line_and_archives_via_changelog(
    tmp_path: Path, git_identity: None
) -> None:
    """reject 移除行、目标不动；reject 事件就是 archived 的墓碑。"""
    dossier = _deferred_dossier(tmp_path)

    outcome = review_pending(dossier, "f_011", ReviewAction.REJECT, now=FIXED)

    assert outcome.merge is not None
    values = (dossier.root / "about" / "values.md").read_text(encoding="utf-8")
    assert "也许该直接工作" not in values
    assert "- 想读博 <!-- f_009 -->" in values

    patch = dossier.read_changelog()[-1].patches[-1]
    assert patch.op is PatchOp.REJECT
    assert patch.id == "f_011"
    assert patch.reason is not None and "rejected" in patch.reason

    by_id = {record.id: record for record in logical_records(dossier)}
    assert by_id["f_011"].status is FactStatus.ARCHIVED
    assert by_id["f_011"].text == "也许该直接工作"
    assert by_id["f_009"].status is FactStatus.ACTIVE

    scan = reconcile_dossier(dossier)
    assert all(item.fact_id != "f_011" for item in scan.report.retired)


def test_review_reconciles_hand_edits_in_the_same_merge(tmp_path: Path, git_identity: None) -> None:
    """review 也是 merge：锁内的 reconcile 会收编这次决定之前的手写行。"""
    dossier = _deferred_dossier(tmp_path)
    values_path = dossier.root / "about" / "values.md"
    values_path.write_text(
        values_path.read_text(encoding="utf-8") + "- 手写的新偏好\n",
        encoding="utf-8",
    )

    outcome = review_pending(dossier, "f_011", ReviewAction.CONFIRM, now=FIXED)

    assert outcome.merge is not None
    ops = [patch.op for patch in dossier.read_changelog()[-1].patches]
    assert PatchOp.ADOPT in ops
    assert PatchOp.CONFIRM in ops
    assert "<!-- f_012 locked -->" in values_path.read_text(encoding="utf-8")


def test_non_pending_facts_cannot_be_reviewed(tmp_path: Path, git_identity: None) -> None:
    dossier = _deferred_dossier(tmp_path)
    review_pending(dossier, "f_011", ReviewAction.CONFIRM, now=FIXED)

    with pytest.raises(ZhiwoError, match="not pending"):
        review_pending(dossier, "f_011", ReviewAction.CONFIRM, now=FIXED)


def test_first_deferred_confirmation_wins(tmp_path: Path, git_identity: None) -> None:
    """OQ#4：两条 pending 指向同一目标时，第二条必须重新走 target_changed。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "about" / "values.md").write_text("- 想读博 <!-- f_009 -->\n", encoding="utf-8")
    response = json.dumps(
        [
            {
                "op": "supersede",
                "target_id": "f_009",
                "kind": "inference",
                "text": "也许该直接工作",
                "evidence": CAPTURE,
            },
            {
                "op": "supersede",
                "target_id": "f_009",
                "kind": "inference",
                "text": "也许先工作两年",
                "evidence": CAPTURE,
            },
        ],
        ensure_ascii=False,
    )
    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider(response, identity="mock-review"),
        now=FIXED,
    )
    first_id = outcome.merge.applied[0].id
    second_id = outcome.merge.applied[1].id
    assert first_id is not None and second_id is not None

    first = review_pending(dossier, first_id, ReviewAction.CONFIRM, now=FIXED)
    second = review_pending(dossier, second_id, ReviewAction.CONFIRM, now=FIXED)

    assert first.merge is not None
    assert second.merge is None
    assert second.target_changed is True
    assert second.target_id == "f_009"

    standalone = review_pending(
        dossier, second_id, ReviewAction.CONFIRM, standalone=True, now=FIXED
    )
    assert standalone.merge is not None
    by_id = {record.id: record for record in logical_records(dossier)}
    assert by_id["f_009"].superseded_by == first_id
    assert by_id[second_id].status is FactStatus.ACTIVE
    assert by_id[second_id].supersedes is None


def test_pending_reviews_reports_a_changed_target(tmp_path: Path, git_identity: None) -> None:
    dossier = _deferred_dossier(tmp_path)
    values_path = dossier.root / "about" / "values.md"
    values_path.write_text(
        values_path.read_text(encoding="utf-8").replace(
            "- 想读博 <!-- f_009 -->", "- 想读博 <!-- f_009 locked -->"
        ),
        encoding="utf-8",
    )

    (item,) = pending_reviews(dossier)

    assert item.record.id == "f_011"
    assert item.target_id == "f_009"
    assert item.target_changed is True
    assert item.target is not None and item.target.locked is True
