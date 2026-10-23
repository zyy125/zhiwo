"""merge 薄壳的契约测试（M4：写盘 / changelog / 一次提交 / 预算钩子）。

纯核心的验证/应用在 ``test_merge_core.py``；这里只验证 plan §5 的第 8–11 步
与 M4 的关键列：exactly one commit per merge、rejections recorded、CJK
evidence 在盘上可读、reconcile 事件与补丁同条目、预算只警告不阻塞。

测试的编排刻意模仿 M7 的 pipeline：在同一个 ``writer_lock`` 内做
journal 追加 → reconcile → merge。薄壳假定调用方持锁，测试必须真的持锁，
否则测到的不是生产路径。
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from zhiwo.errors import GitError, ZhiwoError
from zhiwo.factfile import FactDocument
from zhiwo.merge import BudgetEstimator, BudgetStatus, Capture, MergeResult, merge_dossier
from zhiwo.models import PatchOp, PatchResult, Section
from zhiwo.reconcile import reconcile_dossier
from zhiwo.store import Dossier, bootstrap_dossier

FIXED = datetime(2026, 10, 23, 12, 0, 0)
DEFAULT_CAPTURE = "我厌恶风险，偏好确定性。"


def _git(root: Path, *args: str) -> str:
    """跑一条 git 命令；失败返回空串（断言里再区分 0 个提交与非零）。"""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def commit_count(dossier: Dossier) -> int:
    output = _git(dossier.root, "rev-list", "--count", "HEAD")
    return int(output) if output else 0


def add_draft(
    *,
    text: str = "厌恶风险",
    evidence: str = "厌恶风险，偏好确定性",
    target: str = "about/identity.md",
    kind: str = "statement",
) -> dict[str, object]:
    return {
        "op": "add",
        "target": target,
        "kind": kind,
        "text": text,
        "evidence": evidence,
    }


def merge_one(
    dossier: Dossier,
    drafts: list[object],
    *,
    text: str = DEFAULT_CAPTURE,
    now: datetime = FIXED,
    extractor: str = "mock-v0",
    budget: BudgetEstimator | None = None,
) -> MergeResult:
    """按 M7 的顺序跑一次捕获 + merge（调用方持锁）。"""
    with dossier.writer_lock():
        append = dossier.append_journal([text], now=now)
        scan = reconcile_dossier(dossier)
        capture = Capture.from_journal_append(dossier, append)
        return merge_dossier(
            dossier,
            scan=scan,
            drafts=drafts,
            capture=capture,
            extractor=extractor,
            now=now,
            budget=budget,
        )


# --- 落盘、changelog、提交 --------------------------------------------------


def test_merge_writes_fact_changelog_and_exactly_one_commit(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    result = merge_one(dossier, [add_draft()])

    assert (dossier.root / "about" / "identity.md").read_text(
        encoding="utf-8"
    ) == "- 厌恶风险 <!-- f_001 -->\n"

    entries = {entry.merge: entry for entry in dossier.read_changelog()}
    entry = entries[result.merge_id]
    assert result.changelog_path.stem == result.merge_id
    assert entry.input == "journal/2026-10-23.md"
    assert entry.extractor == "mock-v0"
    assert entry.schema_version == 1
    (patch,) = entry.patches
    assert patch.op is PatchOp.ADD
    assert patch.id == "f_001"
    assert patch.source == "journal/2026-10-23.md#L3"

    assert commit_count(dossier) == 1
    assert _git(dossier.root, "status", "--porcelain") == ""
    assert _git(dossier.root, "log", "-1", "--format=%s") == f"merge: {result.merge_id}"
    assert result.commit == _git(dossier.root, "rev-parse", "--short=12", "HEAD")
    names = _git(dossier.root, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "journal/2026-10-23.md" in names
    assert "about/identity.md" in names
    assert f"changelog/{result.merge_id}.yaml" in names
    assert not any(name.startswith(".index/") for name in names)


def test_second_merge_makes_a_second_commit(tmp_path: Path, git_identity: None) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    merge_one(dossier, [add_draft()])
    merge_one(dossier, [add_draft(text="偏好确定性", evidence="偏好确定性")])

    assert commit_count(dossier) == 2
    assert _git(dossier.root, "status", "--porcelain") == ""


def test_same_second_merges_get_collision_suffixes(tmp_path: Path, git_identity: None) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    first = merge_one(dossier, [add_draft()])
    second = merge_one(dossier, [add_draft(text="偏好确定性", evidence="偏好确定性")])

    assert first.merge_id == "2026-10-23T120000"
    assert second.merge_id == "2026-10-23T120000-2"
    assert first.changelog_path != second.changelog_path
    assert len(dossier.read_changelog()) == 2


def test_untouched_files_stay_byte_identical_and_missing_sections_are_created(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    career = dossier.root / "about" / "career.md"
    career.write_text("# 学业与职业\n\n- 已有事实 <!-- f_001 -->\n", encoding="utf-8")
    before = career.read_bytes()
    (dossier.root / "about" / "values.md").unlink()

    merge_one(dossier, [add_draft()])

    assert career.read_bytes() == before  # 未触碰的文件一个字节都不动
    assert (dossier.root / "about" / "values.md").read_text(encoding="utf-8") == ""


def test_add_into_a_declared_but_missing_section_creates_it(
    tmp_path: Path, git_identity: None
) -> None:
    """plan §5 步骤 4：reconcile 建空桩，merge 的 add 落在同一个桩文件上。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "about" / "values.md").unlink()

    result = merge_one(dossier, [add_draft(target="about/values.md")])

    assert result.applied[0].id == "f_001"
    assert (dossier.root / "about" / "values.md").read_text(
        encoding="utf-8"
    ) == "- 厌恶风险 <!-- f_001 -->\n"


def test_reconcile_events_come_before_merge_patches_and_do_not_collide(
    tmp_path: Path, git_identity: None
) -> None:
    """M4 的关键回归：同轮收编 f_001 后，add 必须是 f_002。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "about" / "identity.md").write_text("- 手写的一条事实\n", encoding="utf-8")

    with dossier.writer_lock():
        append = dossier.append_journal([DEFAULT_CAPTURE], now=FIXED)
        scan = reconcile_dossier(dossier)
        capture = Capture.from_journal_append(dossier, append)
        result = merge_dossier(
            dossier,
            scan=scan,
            drafts=[add_draft()],
            capture=capture,
            extractor="mock-v0",
            now=FIXED,
        )

    entry = next(entry for entry in dossier.read_changelog() if entry.merge == result.merge_id)
    assert [patch.op for patch in entry.patches] == [PatchOp.ADOPT, PatchOp.ADD]
    assert [patch.id for patch in entry.patches] == ["f_001", "f_002"]
    assert result.patches[0].id == "f_002"
    identity = (dossier.root / "about" / "identity.md").read_text(encoding="utf-8")
    assert "- 手写的一条事实 <!-- f_001 locked -->\n" in identity
    assert "- 厌恶风险 <!-- f_002 -->\n" in identity


def test_provider_failure_path_still_commits_the_journal(
    tmp_path: Path, git_identity: None
) -> None:
    """drafts=[] 是 extract 失败的路径：journal 仍须落进提交。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    result = merge_one(dossier, [])

    entry = next(entry for entry in dossier.read_changelog() if entry.merge == result.merge_id)
    assert entry.patches == []
    assert commit_count(dossier) == 1
    journal = (dossier.root / "journal" / "2026-10-23.md").read_text(encoding="utf-8")
    assert f"- {DEFAULT_CAPTURE}\n" in journal


def test_all_rejected_drafts_still_commit_and_are_recorded(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    result = merge_one(dossier, [add_draft(evidence="并不在输入里")])

    entry = next(entry for entry in dossier.read_changelog() if entry.merge == result.merge_id)
    (patch,) = entry.patches
    assert patch.result is PatchResult.REJECTED
    assert patch.reason is not None and "verbatim" in patch.reason
    assert (dossier.root / "about" / "identity.md").read_text(encoding="utf-8") == ""
    assert commit_count(dossier) == 1


def test_malformed_drafts_surface_but_do_not_block_the_commit(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    result = merge_one(dossier, [{"op": "bogus"}, "not a mapping"])

    assert [item.index for item in result.malformed] == [0, 1]
    entry = next(entry for entry in dossier.read_changelog() if entry.merge == result.merge_id)
    assert entry.patches == []
    assert commit_count(dossier) == 1


def test_result_exposes_the_cli_summary_counts(tmp_path: Path, git_identity: None) -> None:
    """plan §5 步骤 12 的摘要字段都能从结果里直接读，CLI 不必再解析 changelog。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    merge_one(dossier, [add_draft(text="第一条", evidence="第一条")], text="第一条")
    result = merge_one(
        dossier,
        [
            add_draft(text="也许换城市", evidence="也许换城市", kind="inference"),
            add_draft(text="坏补丁", evidence="不在输入里"),
        ],
        text="也许换城市",
    )

    assert len(result.applied) == 1
    assert len(result.rejected) == 1
    assert result.skipped == ()
    assert result.pending_count == 1


def test_cjk_evidence_stays_readable_utf8_on_disk(tmp_path: Path, git_identity: None) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    evidence = "面试挂了，二面，反馈说项目深度不够。"
    result = merge_one(
        dossier,
        [add_draft(text="二面反馈：项目深度不够", evidence=evidence)],
        text=evidence,
    )

    raw = result.changelog_path.read_bytes()
    assert evidence.encode("utf-8") in raw
    assert b"\\u" not in raw


# --- 预算钩子 ---------------------------------------------------------------


def test_budget_exactly_at_the_limit_is_not_over() -> None:
    """SPEC §6 的 "exceeded" 是严格大于；等于预算仍算可用。"""
    assert BudgetStatus(tokens=4000, limit=4000).over is False
    assert BudgetStatus(tokens=4001, limit=4000).over is True


def test_budget_hook_returns_status_without_blocking(tmp_path: Path, git_identity: None) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    seen: list[int] = []

    def estimator(documents: Sequence[tuple[Section, FactDocument]]) -> int:
        # 估算器看到的是 post-merge 文档：此时 identity 里已有新事实。
        seen.append(sum(len(document.facts()) for _, document in documents))
        return 4500

    result = merge_one(dossier, [add_draft()], budget=estimator)

    assert result.budget is not None
    assert (result.budget.tokens, result.budget.limit) == (4500, 4000)
    assert result.budget.over is True
    assert seen == [1]  # 看到了刚写入内存的新事实
    assert commit_count(dossier) == 1


def test_no_budget_hook_means_no_status(tmp_path: Path, git_identity: None) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    result = merge_one(dossier, [add_draft()])
    assert result.budget is None


def test_budget_hook_failure_writes_nothing(tmp_path: Path, git_identity: None) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")

    def broken(_documents: object) -> int:
        raise RuntimeError("estimator exploded")

    with pytest.raises(RuntimeError, match="exploded"):
        merge_one(dossier, [add_draft()], budget=broken)

    assert list(dossier.changelog_dir.glob("*.yaml")) == []
    assert commit_count(dossier) == 0
    assert (dossier.root / "about" / "identity.md").read_text(encoding="utf-8") == ""


def test_invalid_capture_range_writes_nothing(tmp_path: Path, git_identity: None) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")

    with dossier.writer_lock():
        append = dossier.append_journal([DEFAULT_CAPTURE], now=FIXED)
        scan = reconcile_dossier(dossier)
        bad = Capture(
            path=Capture.from_journal_append(dossier, append).path,
            first_line=append.first_line,
            last_line=append.last_line + 5,
        )
        with pytest.raises(ZhiwoError):
            merge_dossier(
                dossier,
                scan=scan,
                drafts=[add_draft()],
                capture=bad,
                extractor="mock-v0",
                now=FIXED,
            )

    assert list(dossier.changelog_dir.glob("*.yaml")) == []
    assert commit_count(dossier) == 0


# --- 失败语义 ---------------------------------------------------------------


def test_commit_failure_leaves_the_merge_on_disk(dossier: Dossier, git_identity: None) -> None:
    """没有 git 仓库：写盘后 GitError，不回滚——journal 与 changelog 留在盘上。

    git 是恢复后盾，不是事务。滚回去会把已经落盘的捕获与事实一起丢掉，
    与 "nothing is ever lost" 相反；正确做法是响亮失败，让下一次提交带上。
    """
    with pytest.raises(GitError, match="not a git repository"):
        merge_one(dossier, [add_draft(target="about/career.md")])

    assert "- 厌恶风险 <!-- f_052 -->\n" in (dossier.root / "about" / "career.md").read_text(
        encoding="utf-8"
    )
    assert len(list(dossier.changelog_dir.glob("*.yaml"))) == 2  # 黄金条目 + 新条目


def test_golden_dossier_merges_and_commits(git_dossier: Dossier) -> None:
    """黄金档案 + 真 git 仓库的端到端：既有 f_051、缺失分区、收编后新 id。"""
    with git_dossier.writer_lock():
        append = git_dossier.append_journal([DEFAULT_CAPTURE], now=FIXED)
        scan = reconcile_dossier(git_dossier)
        capture = Capture.from_journal_append(git_dossier, append)
        result = merge_dossier(
            git_dossier,
            scan=scan,
            drafts=[add_draft(target="about/career.md")],
            capture=capture,
            extractor="mock-v0",
            now=FIXED,
        )

    assert result.applied[0].id == "f_052"  # 黄金档案的天花板是 f_051
    career = (git_dossier.root / "about" / "career.md").read_text(encoding="utf-8")
    assert "- 厌恶风险 <!-- f_052 -->\n" in career
    assert commit_count(git_dossier) == 1
    assert _git(git_dossier.root, "status", "--porcelain") == ""
    assert len(list(git_dossier.changelog_dir.glob("*.yaml"))) == 2
