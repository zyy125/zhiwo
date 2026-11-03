"""M5 与 M4 的接缝：提取失败时，journal 仍然必须留下并提交。

plan §7 M5 的关键测试是 "malformed JSON tolerated (journal still commits)"。
M5 本身不拥有 pipeline，所以这里按 M7 的顺序模拟：同一个 ``writer_lock``
内 journal 追加 → reconcile → extract（返回空 drafts + 诊断）→ merge。
它证明失败被限制在"这次没有事实"这一层，而不是把整个捕获回滚掉
（SPEC 铁律 2：capture 永不阻塞）。
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from zhiwo.extract import extract
from zhiwo.llm import MockProvider
from zhiwo.merge import Capture, merge_dossier
from zhiwo.reconcile import reconcile_dossier
from zhiwo.store import Dossier, bootstrap_dossier

FIXED = datetime(2026, 10, 23, 12, 0, 0)
CAPTURE = "面试挂了，二面，反馈说项目深度不够。"


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


def test_malformed_model_response_still_commits_the_journal(
    tmp_path: Path, git_identity: None
) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    profile = dossier.load_profile()

    with dossier.writer_lock():
        append = dossier.append_journal([CAPTURE], now=FIXED)
        scan = reconcile_dossier(dossier)
        capture = Capture.from_journal_append(dossier, append)
        lines = dossier.read_journal_entries(
            capture.path,
            first_line=capture.first_line,
            last_line=capture.last_line,
        )
        result = extract(
            provider=MockProvider("这不是 JSON，模型今天不太配合。"),
            profile=profile,
            core_view="",
            lines=lines,
            first_line=capture.first_line,
        )
        merged = merge_dossier(
            dossier,
            scan=scan,
            drafts=result.drafts,
            capture=capture,
            extractor=result.extractor,
            now=FIXED,
        )

    assert result.drafts == ()
    assert result.error is not None
    assert merged.patches == ()
    assert merged.malformed == ()

    entries = {entry.merge: entry for entry in dossier.read_changelog()}
    entry = entries[merged.merge_id]
    assert entry.input == "journal/2026-10-23.md"
    assert entry.extractor == "mock-v0"
    assert entry.patches == []

    journal = (dossier.root / "journal" / "2026-10-23.md").read_text(encoding="utf-8")
    assert CAPTURE in journal
    assert _commit_count(dossier) == 1
    assert _status(dossier) == ""
