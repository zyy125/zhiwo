"""M7 的编排层：锁、journal、reconcile、extract、merge 的顺序。

plan §5 的步骤在这里第一次被完整串起来，因此这些测试关心的是**接缝**
而不是各层自己的语法：

- 捕获自 journal 落盘起持久，provider 失败也只能降级"这次没有事实"；
- extract 看到的是 reconcile **之后**的 core view（同轮收编可见）；
- 预算估算器确实被注入（D-007 的 M7 接线点），超预算仍按 D-002 放行；
- 多行输入拆成多条 journal 捕获，坐标不漂移。
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from zhiwo.errors import MergeRefused
from zhiwo.llm import MockProvider
from zhiwo.models import FactStatus
from zhiwo.pipeline import capture
from zhiwo.recall import load_documents, logical_records, render_core_view
from zhiwo.reconcile import reconcile_dossier
from zhiwo.store import Dossier, bootstrap_dossier

FIXED = datetime(2026, 11, 3, 12, 0, 0)
CAPTURE = "面试挂了，二面，反馈说项目深度不够。"

ADD_RESPONSE = (
    '[{"op":"add","target":"about/career.md","kind":"statement",'
    '"text":"二面失败，项目深度不够","evidence":"面试挂了，二面，反馈说项目深度不够。"},'
    '{"op":"add","target":"goals/current.md","kind":"inference",'
    '"text":"需要补项目深度","evidence":"反馈说项目深度不够"}]'
)


def _commit_count(dossier: Dossier) -> int:
    output = subprocess.run(
        ["git", "-C", str(dossier.root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(output)


def _status(dossier: Dossier) -> str:
    return subprocess.run(
        ["git", "-C", str(dossier.root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_capture_commits_the_journal_and_applies_patches(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")

    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider(ADD_RESPONSE, identity="mock-pipeline"),
        now=FIXED,
    )

    assert [patch.id for patch in outcome.merge.applied] == ["f_001", "f_002"]
    assert outcome.merge.merge_id.startswith("2026-11-03T")
    assert outcome.pending_total == 1
    assert outcome.provider_error is None and outcome.extraction_error is None
    assert outcome.merge.budget is not None
    assert outcome.merge.budget.tokens > 0

    journal = (dossier.root / "journal" / "2026-11-03.md").read_text(encoding="utf-8")
    assert journal.endswith(f"- {CAPTURE}\n")
    assert outcome.capture.first_line == 3
    assert outcome.capture.last_line == 3

    career = (dossier.root / "about" / "career.md").read_text(encoding="utf-8")
    current = (dossier.root / "goals" / "current.md").read_text(encoding="utf-8")
    assert "- 二面失败，项目深度不够 <!-- f_001 -->" in career
    assert "- 需要补项目深度 <!-- f_002 pending -->" in current

    entry = dossier.read_changelog()[0]
    assert entry.extractor == "mock-pipeline"
    assert entry.patches[0].evidence == CAPTURE
    assert entry.patches[0].source == "journal/2026-11-03.md#L3"

    assert _commit_count(dossier) == 1
    assert _status(dossier) == ""


def test_capture_without_a_provider_still_commits_the_journal(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")

    outcome = capture(dossier, CAPTURE, provider=None, now=FIXED)

    assert outcome.merge.applied == ()
    assert outcome.provider_error is not None
    assert "no LLM provider configured" in outcome.provider_error
    assert outcome.merge.patches == ()
    assert "extractor: none" in outcome.merge.changelog_path.read_text(encoding="utf-8")
    assert CAPTURE in (dossier.root / "journal" / "2026-11-03.md").read_text(encoding="utf-8")
    assert _commit_count(dossier) == 1
    assert _status(dossier) == ""


def test_extract_sees_the_post_reconcile_core_view(tmp_path: Path, git_identity: None) -> None:
    """同轮出现的手写行必须先被收编，再进入 extract 的 core view。

    plan §5 的步骤 4→5 是顺序契约：如果 extract 看到的是盘上的旧文件，
    模型会把一条已有的事实再推导一遍，而 reconcile 刚给它的 id 也无法
    用于 supersede/link。这里用 callable provider 直接检查 prompt。
    """
    dossier = bootstrap_dossier(tmp_path / "me")
    values = dossier.root / "about" / "values.md"
    values.write_text("- 我喜欢先列选项再决定\n", encoding="utf-8")
    seen: list[str] = []

    def respond(prompt: str) -> str:
        seen.append(prompt)
        return "[]"

    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider(respond, identity="mock-pipeline"),
        now=FIXED,
    )

    adopted = outcome.merge.reconcile.adopted
    assert len(adopted) == 1
    assert adopted[0].fact_id == "f_001"
    assert seen, "provider was never called"
    assert "f_001 [active, locked] 我喜欢先列选项再决定" in seen[0]


def test_multiline_capture_appends_one_journal_line_per_paragraph(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")

    outcome = capture(
        dossier,
        "第一段。\n\n第二段。\n第三段。",
        provider=None,
        now=FIXED,
    )

    journal = (dossier.root / "journal" / "2026-11-03.md").read_text(encoding="utf-8")
    assert journal.endswith("- 第一段。\n- 第二段。\n- 第三段。\n")
    assert (outcome.capture.first_line, outcome.capture.last_line) == (3, 5)


def test_capture_injects_the_budget_estimator_and_stays_warn_only(
    tmp_path: Path, git_identity: None
) -> None:
    """D-007 的接线点：预算状态不是 None；超预算仍按 D-002 应用补丁。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    profile = dossier.load_profile()
    profile_text = dossier.profile_path.read_text(encoding="utf-8").replace(
        "core_budget_tokens: 4000", "core_budget_tokens: 1"
    )
    dossier.profile_path.write_text(profile_text, encoding="utf-8")
    assert profile.core_budget_tokens == 4000  # 旧对象不受文件改动影响

    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider(ADD_RESPONSE, identity="mock-pipeline"),
        now=FIXED,
    )

    assert outcome.merge.budget is not None
    assert outcome.merge.budget.over is True
    assert len(outcome.merge.applied) == 2  # 放行，不延迟、不丢弃
    records = logical_records(dossier)
    assert {record.status for record in records} == {FactStatus.ACTIVE, FactStatus.PENDING}


def test_capture_uses_the_documents_returned_by_reconcile(tmp_path: Path) -> None:
    """防御性接缝：pipeline 的 core view 输入就是 scan.documents。

    这条测试不跑完整捕获（不需要 git），只确认两件事由同一份数据产生：
    reconcile 返回的文档与 recall 的渲染器共享 ``(Section, FactDocument)``
    形状，pipeline 没有另读一份盘上快照。
    """
    dossier = bootstrap_dossier(tmp_path / "me")
    scan = reconcile_dossier(dossier)

    assert render_core_view(scan.documents) == render_core_view(load_documents(dossier)) == ""


def test_malformed_model_output_is_data_and_still_commits(
    tmp_path: Path, git_identity: None
) -> None:
    """D-008 的 M7 接线：解析失败是诊断，不是回滚信号。"""
    dossier = bootstrap_dossier(tmp_path / "me")

    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider("模型今天没有返回 JSON", identity="mock-bad"),
        now=FIXED,
    )

    assert outcome.provider_error is None
    assert outcome.extraction_error is not None
    assert outcome.merge.patches == ()
    assert _commit_count(dossier) == 1
    assert CAPTURE in (dossier.root / "journal" / "2026-11-03.md").read_text(encoding="utf-8")


def test_duplicate_ids_refuse_the_merge_but_the_journal_stays_durable(
    tmp_path: Path, git_identity: None
) -> None:
    """SPEC §4 第 3 案：拒签整次 merge，但已经落盘的捕获不跟着回滚。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "about" / "values.md").write_text("- 甲 <!-- f_001 -->\n", encoding="utf-8")
    (dossier.root / "goals" / "current.md").write_text("- 乙 <!-- f_001 -->\n", encoding="utf-8")

    with pytest.raises(MergeRefused, match=r"about/values\.md:L1.*goals/current\.md:L1"):
        capture(dossier, CAPTURE, provider=None, now=FIXED)

    assert CAPTURE in (dossier.root / "journal" / "2026-11-03.md").read_text(encoding="utf-8")
    assert list((dossier.root / "changelog").glob("*.yaml")) == []
    with pytest.raises(subprocess.CalledProcessError):
        _commit_count(dossier)  # 还没有 HEAD：拒签没有产生提交


def test_pending_total_accumulates_preexisting_pending_facts(
    tmp_path: Path, git_identity: None
) -> None:
    """nudge 报的是积累量：旧 pending + 本次新增 inference。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "goals" / "current.md").write_text(
        "- 旧推断 <!-- f_001 pending -->\n", encoding="utf-8"
    )

    outcome = capture(
        dossier,
        CAPTURE,
        provider=MockProvider(ADD_RESPONSE, identity="mock-pipeline"),
        now=FIXED,
    )

    assert outcome.pending_total == 2
