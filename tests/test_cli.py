"""M7/M8 的 CLI：click 回退分发、端到端捕获、review、export 与 ask。

plan §7 M7 的三条关键测试在这里各有落点：

- **CliRunner E2E with replay**：用 JSONL 回放驱动一次完整捕获；
- **provider failure still commits journal**：注入失败 provider，journal、
  changelog、提交仍然发生；
- **unknown-word dispatch**：未知的第一个词被当成捕获文本，而不是报
  "No such command"。

只读命令（show/why/diff/init）的断言偏向**输出里必须有可读的事实**，
而不是钉死整段排版：排版会随 M8 的 review UI / export 演进，而"打印了
证据与 journal 行号"是契约里的承诺。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from wcwidth import wcswidth

from zhiwo.cli import AppContext, cli
from zhiwo.errors import ProviderError
from zhiwo.extract import build_extract_prompt
from zhiwo.llm import MockProvider, prompt_sha256
from zhiwo.pipeline import capture, pending_reviews
from zhiwo.recall import load_documents, render_core_view
from zhiwo.store import Dossier, bootstrap_dossier

FIXED = datetime(2026, 11, 3, 12, 0, 0)
CAPTURE = "面试挂了，二面，反馈说项目深度不够。"

REPLAY_RESPONSE = json.dumps(
    [
        {
            "op": "add",
            "target": "about/career.md",
            "kind": "statement",
            "text": "二面失败，项目深度不够",
            "evidence": "面试挂了，二面，反馈说项目深度不够。",
        },
        {
            "op": "add",
            "target": "goals/current.md",
            "kind": "inference",
            "text": "需要补项目深度",
            "evidence": "反馈说项目深度不够",
        },
    ],
    ensure_ascii=False,
)


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
            "evidence": "面试挂了",
        },
    ],
    ensure_ascii=False,
)

TWO_PENDING_RESPONSE = json.dumps(
    [
        {
            "op": "supersede",
            "target_id": "f_009",
            "kind": "inference",
            "text": "也许该直接工作",
            "evidence": "面试挂了",
        },
        {
            "op": "supersede",
            "target_id": "f_010",
            "kind": "inference",
            "text": "也许应该先工作两年再考虑读博",
            "evidence": "面试挂了",
        },
    ],
    ensure_ascii=False,
)


def _prepare_deferred_dossier(root: Path, response: str = DEFERRED_RESPONSE) -> Dossier:
    """在临时档案里制造 pending 推断；供脚本按键与 review UI 测试使用。"""
    dossier = Dossier(root)
    (root / "about" / "values.md").write_text(
        "- 想读博 <!-- f_009 -->\n- 想留北京 <!-- f_010 -->\n", encoding="utf-8"
    )
    capture(
        dossier,
        CAPTURE,
        provider=MockProvider(response, identity="mock-cli"),
        now=FIXED,
    )
    return dossier


def _record_replay(path: Path, dossier_root: Path, *, first_line: int = 3) -> Path:
    """为一次全新档案的捕获写一条 JSONL 回放记录。

    prompt 的 hash 是回放的键（D-008），因此测试必须用生产代码构建同一段
    prompt，而不是手写一个 hash——手写会让测试在 prompt 变化时依然绿。
    """
    dossier = Dossier(dossier_root)
    prompt = build_extract_prompt(
        profile=dossier.load_profile(),
        core_view=render_core_view(load_documents(dossier)),
        lines=(CAPTURE,),
        first_line=first_line,
    )
    record = {
        "version": 1,
        "identity": "mock-cli",
        "prompt_sha256": prompt_sha256(prompt),
        "response": REPLAY_RESPONSE,
    }
    path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


def _commit_count(root: Path) -> int:
    output = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(output)


class _FailingProvider:
    identity = "failing"

    def complete(self, prompt: str) -> str:
        raise ProviderError("network down")


def test_unknown_text_dispatches_to_capture(tmp_path: Path, git_identity: None) -> None:
    """未知的第一个位置参数 → 捕获，而不是 click 的 "No such command"。"""
    root = bootstrap_dossier(tmp_path / "me").root
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["--dossier", str(root), "今天很累，想早点睡"],
        obj=AppContext(provider=MockProvider("[]"), provider_explicit=True, now=FIXED),
    )

    assert result.exit_code == 0, result.output
    assert "No such command" not in result.output
    journal = (root / "journal" / "2026-11-03.md").read_text(encoding="utf-8")
    assert journal.endswith("- 今天很累，想早点睡\n")


def test_cli_e2e_with_replay(tmp_path: Path, git_identity: None) -> None:
    """回放 provider 驱动完整捕获：journal + 两条事实 + changelog + 一个提交。"""
    root = bootstrap_dossier(tmp_path / "me").root
    replay = _record_replay(tmp_path / "replay.jsonl", root)

    result = CliRunner().invoke(
        cli,
        ["--dossier", str(root), "--replay", str(replay), CAPTURE],
    )

    assert result.exit_code == 0, result.output
    assert "applied  2" in result.output
    assert "1 fact(s) await `me review`" in result.output
    assert "二面失败，项目深度不够 <!-- f_001 -->" in (root / "about" / "career.md").read_text(
        encoding="utf-8"
    )
    assert "需要补项目深度 <!-- f_002 pending -->" in (root / "goals" / "current.md").read_text(
        encoding="utf-8"
    )
    assert list((root / "changelog").glob("*.yaml"))
    assert _commit_count(root) == 1


def test_provider_failure_still_commits_the_journal(tmp_path: Path, git_identity: None) -> None:
    """provider 失败不是回滚信号：journal 已持久，必须继续走完 merge。"""
    root = bootstrap_dossier(tmp_path / "me").root

    result = CliRunner().invoke(
        cli,
        ["--dossier", str(root), CAPTURE],
        obj=AppContext(provider=_FailingProvider(), provider_explicit=True, now=FIXED),
    )

    assert result.exit_code == 0, result.output
    assert "network down" in result.output
    assert "journal capture is committed" in result.output
    assert CAPTURE in (root / "journal" / "2026-11-03.md").read_text(encoding="utf-8")

    entries = list((root / "changelog").glob("*.yaml"))
    assert len(entries) == 1
    assert "patches: []" in entries[0].read_text(encoding="utf-8")
    assert "extractor: failing" in entries[0].read_text(encoding="utf-8")
    assert _commit_count(root) == 1


def test_review_is_a_registered_command_not_capture(
    tmp_path: Path, git_identity: None
) -> None:
    """``me review`` 不能再被"未知词 → 捕获"吞掉。"""
    root = bootstrap_dossier(tmp_path / "me").root

    result = CliRunner().invoke(cli, ["--dossier", str(root), "review"])

    assert result.exit_code == 0, result.output
    assert "no pending facts" in result.output
    assert list((root / "journal").glob("*.md")) == []


def test_review_confirm_with_a_scripted_key(tmp_path: Path, git_identity: None) -> None:
    """``c`` 一键确认 deferred pair：两行都改写，且产生一次 review 提交。"""
    root = bootstrap_dossier(tmp_path / "me").root
    dossier = _prepare_deferred_dossier(root)
    (item,) = pending_reviews(dossier)
    pending_id = item.record.id
    commits_before = _commit_count(root)

    result = CliRunner().invoke(
        cli,
        ["--dossier", str(root), "review"],
        input="c",
        obj=AppContext(now=FIXED),
    )

    assert result.exit_code == 0, result.output
    assert f"confirmed {pending_id}" in result.output
    values = (root / "about" / "values.md").read_text(encoding="utf-8")
    assert f"- ~~想读博~~ <!-- f_009 superseded-by:{pending_id} -->" in values
    assert f"- 也许该直接工作 <!-- {pending_id} supersedes:f_009 -->" in values
    assert _commit_count(root) == commits_before + 1


def test_review_edit_and_reject_with_scripted_keys(tmp_path: Path, git_identity: None) -> None:
    """``e<text>`` 后 ``r``：第一条 locked，第二条从文件移除。"""
    root = bootstrap_dossier(tmp_path / "me").root
    dossier = _prepare_deferred_dossier(root, TWO_PENDING_RESPONSE)
    first, second = pending_reviews(dossier)

    result = CliRunner().invoke(
        cli,
        ["--dossier", str(root), "review"],
        input="e新文本\nr",
        obj=AppContext(now=FIXED),
    )

    assert result.exit_code == 0, result.output
    values = (root / "about" / "values.md").read_text(encoding="utf-8")
    assert f"- 新文本 <!-- {first.record.id} locked -->" in values
    assert "也许应该先工作两年再考虑读博" not in values
    assert second.record.id not in values
    assert "f_009 superseded-by" not in values
    assert "f_010 superseded-by" not in values


def test_review_falls_back_to_line_input_without_a_terminal(
    dossier: Dossier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 /dev/tty 时按键读取退回 stdin 行首字符，而不是抛 OSError。"""
    def no_tty(echo: bool = False) -> str:
        raise OSError("no controlling terminal")

    monkeypatch.setattr("zhiwo.cli.click.getchar", no_tty)

    result = CliRunner().invoke(
        cli,
        ["--dossier", str(dossier.root), "review"],
        input="q\n",
    )

    assert result.exit_code == 0, result.output
    assert "1 fact(s) still pending" in result.output


def test_review_list_aligns_wide_cjk_columns(dossier: Dossier) -> None:
    """CJK 文本列的显示宽度由 wcwidth 补齐，目标列起点相同。"""
    values = dossier.root / "about" / "values.md"
    values.write_text(
        values.read_text(encoding="utf-8")
        + "- 也许该直接工作 <!-- f_060 supersedes:f_001 pending -->\n"
        + "- 也许应该先工作两年再认真考虑读博 <!-- f_061 supersedes:f_002 pending -->\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["--dossier", str(dossier.root), "review"], input="q")

    assert result.exit_code == 0, result.output
    rows = [
        line
        for line in result.output.splitlines()
        if line.startswith("  f_0") and "supersedes" in line
    ]
    assert len(rows) == 2
    assert wcswidth(rows[0][: rows[0].index("supersedes")]) == wcswidth(
        rows[1][: rows[1].index("supersedes")]
    )


def test_export_normal_reader_view(dossier: Dossier) -> None:
    """默认 normal：无 high 分区、无 pending、无 superseded、无 journal。"""
    result = CliRunner().invoke(cli, ["--dossier", str(dossier.root), "export"])

    assert result.exit_code == 0, result.output
    assert "厌恶风险，偏好确定性" in result.output
    assert "二面反馈：项目深度不够" in result.output
    assert "根据二面反馈补项目深度" not in result.output
    assert "想读博" not in result.output
    assert "昨晚只睡了" not in result.output
    assert "<!--" not in result.output
    assert "~~" not in result.output


def test_export_high_clearance_and_output_file(dossier: Dossier, tmp_path: Path) -> None:
    health = dossier.root / "about" / "health.md"
    health.write_text(
        "- 对花生过敏 <!-- f_090 -->\n- 偏好 **确定** 的方案 <!-- f_091 -->\n",
        encoding="utf-8",
    )
    output = tmp_path / "reader.md"

    result = CliRunner().invoke(
        cli,
        ["--dossier", str(dossier.root), "export", "--clearance", "high", "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    text = output.read_text(encoding="utf-8")
    assert "对花生过敏" in text
    assert "**确定**" in text
    assert "journal/2026-10-11.md" not in text
    assert "昨晚只睡了" not in text

    normal = CliRunner().invoke(
        cli, ["--dossier", str(dossier.root), "export", "--clearance", "normal"]
    )
    assert "对花生过敏" not in normal.output


def test_ask_normal_injects_only_the_reader_view(dossier: Dossier) -> None:
    provider = MockProvider("先补项目深度。", identity="mock-ask")
    result = CliRunner().invoke(
        cli,
        ["--dossier", str(dossier.root), "ask", "我该准备什么？", "--show-context"],
        obj=AppContext(provider=provider, provider_explicit=True),
    )

    assert result.exit_code == 0, result.output
    assert "先补项目深度。" in result.output
    assert "<reader_view>" in result.output
    assert "<journal_hits>" not in result.output
    prompt = provider.prompts[0]
    assert "二面反馈：项目深度不够" in prompt
    assert "根据二面反馈补项目深度" not in prompt
    assert "昨晚只睡了" not in prompt


def test_ask_high_clearance_adds_high_sections_and_journal_hits(dossier: Dossier) -> None:
    (dossier.root / "about" / "health.md").write_text(
        "- 对花生过敏 <!-- f_090 -->\n", encoding="utf-8"
    )
    provider = MockProvider("面试反馈说明需要补项目深度。", identity="mock-ask")

    result = CliRunner().invoke(
        cli,
        ["--dossier", str(dossier.root), "ask", "面试", "--clearance", "high", "--show-context"],
        obj=AppContext(provider=provider, provider_explicit=True),
    )

    assert result.exit_code == 0, result.output
    assert "<journal_hits>" in result.output
    prompt = provider.prompts[0]
    assert "对花生过敏" in prompt
    assert "journal/2026-10-11.md#L7" in prompt
    assert "面试挂了" in prompt


def test_ask_requires_a_provider(dossier: Dossier) -> None:
    result = CliRunner().invoke(cli, ["--dossier", str(dossier.root), "ask", "我该做什么？"])

    assert result.exit_code != 0
    assert "no LLM provider configured" in result.output


def test_ask_reports_provider_failure(dossier: Dossier) -> None:
    result = CliRunner().invoke(
        cli,
        ["--dossier", str(dossier.root), "ask", "我该做什么？"],
        obj=AppContext(provider=_FailingProvider(), provider_explicit=True),
    )

    assert result.exit_code != 0
    assert "network down" in result.output


def test_init_bootstraps_the_contract_tree(tmp_path: Path) -> None:
    root = tmp_path / "me"

    result = CliRunner().invoke(cli, ["--dossier", str(root), "init"])

    assert result.exit_code == 0, result.output
    assert (root / "profile.yaml").is_file()
    assert (root / "journal").is_dir()
    assert (root / "changelog").is_dir()
    assert (root / ".index").is_dir()
    assert (root / "about" / "values.md").is_file()
    assert (root / "goals" / "current.md").is_file()
    assert (root / ".gitignore").read_text(encoding="utf-8") == ".index/\n"
    assert (root / ".git").is_dir()

    again = CliRunner().invoke(cli, ["--dossier", str(root), "init"])
    assert again.exit_code != 0
    assert "non-empty" in again.output


def test_show_lists_logical_records_and_filters(dossier: Dossier) -> None:
    root = dossier.root
    runner = CliRunner()

    everything = runner.invoke(cli, ["--dossier", str(root), "show"])
    assert everything.exit_code == 0, everything.output
    assert "f_051  [active]" in everything.output
    assert "f_009  [superseded]" in everything.output
    assert "f_043  [pending]" in everything.output

    section = runner.invoke(cli, ["--dossier", str(root), "show", "current"])
    assert section.exit_code == 0, section.output
    assert "f_042" in section.output and "f_043" in section.output
    assert "f_001" not in section.output

    fact = runner.invoke(cli, ["--dossier", str(root), "show", "f_051"])
    assert fact.exit_code == 0, fact.output
    assert "source:        journal/2026-10-11.md#L7" in fact.output

    machine = runner.invoke(cli, ["--dossier", str(root), "show", "--json"])
    assert machine.exit_code == 0, machine.output
    records = json.loads(machine.output)
    by_id = {record["id"]: record for record in records}
    assert by_id["f_051"]["kind"] == "statement"
    assert by_id["f_009"]["status"] == "superseded"

    unknown = runner.invoke(cli, ["--dossier", str(root), "show", "no-such-section"])
    assert unknown.exit_code != 0
    assert "unknown section" in unknown.output


def test_show_conflicts_is_a_read_only_preview(dossier: Dossier) -> None:
    """``--conflicts`` 只预览：手写行保持原样，不写 changelog、不分配 id。"""
    root = dossier.root
    values = root / "about" / "values.md"
    before = values.read_text(encoding="utf-8")
    values.write_text(before + "- 厌恶风险，偏好确定性\n", encoding="utf-8")
    changelog_before = set((root / "changelog").glob("*.yaml"))

    result = CliRunner().invoke(cli, ["--dossier", str(root), "show", "--conflicts"])

    assert result.exit_code == 0, result.output
    assert "conflicts 1" in result.output
    assert "f_001" in result.output
    assert values.read_text(encoding="utf-8") == before + "- 厌恶风险，偏好确定性\n"
    assert set((root / "changelog").glob("*.yaml")) == changelog_before

    machine = CliRunner().invoke(cli, ["--dossier", str(root), "show", "--conflicts", "--json"])
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)
    assert payload["conflicts"][0]["target_id"] == "f_001"


def test_why_renders_evidence_and_journal_context(dossier: Dossier) -> None:
    root = dossier.root
    runner = CliRunner()

    result = runner.invoke(cli, ["--dossier", str(root), "why", "f_051"])
    assert result.exit_code == 0, result.output
    assert "evidence: 面试挂了，二面，反馈说项目深度不够。" in result.output
    assert "journal/2026-10-11.md#L7" in result.output
    assert "L7   >" in result.output

    no_history = runner.invoke(cli, ["--dossier", str(root), "why", "f_001"])
    assert no_history.exit_code == 0, no_history.output
    assert "no changelog events" in no_history.output

    unknown = runner.invoke(cli, ["--dossier", str(root), "why", "f_999"])
    assert unknown.exit_code != 0
    assert "no fact or changelog entry" in unknown.output


def test_diff_reports_no_head_then_passes_git_args(git_dossier: Dossier) -> None:
    dossier = git_dossier
    root = dossier.root
    runner = CliRunner()

    never_committed = runner.invoke(cli, ["--dossier", str(root), "diff"])
    assert never_committed.exit_code == 0, never_committed.output
    assert "no commits yet" in never_committed.output
    assert "?? profile.yaml" in never_committed.output

    dossier.git_commit("initial")
    values = root / "about" / "values.md"
    values.write_text(values.read_text(encoding="utf-8") + "- 手写新事实\n", encoding="utf-8")

    changed = runner.invoke(cli, ["--dossier", str(root), "diff", "--stat"])
    assert changed.exit_code == 0, changed.output
    assert "about/values.md" in changed.output
    assert "1 insertion" in changed.output


def test_diff_rejects_a_non_repository(copy_dossier: Path) -> None:
    result = CliRunner().invoke(cli, ["--dossier", str(copy_dossier), "diff"])

    assert result.exit_code != 0
    assert "not a git repository" in result.output


def test_capture_warns_loudly_when_over_budget(tmp_path: Path, git_identity: None) -> None:
    root = bootstrap_dossier(tmp_path / "me").root
    profile = (root / "profile.yaml").read_text(encoding="utf-8")
    (root / "profile.yaml").write_text(
        profile.replace("core_budget_tokens: 4000", "core_budget_tokens: 1"),
        encoding="utf-8",
    )
    replay = _record_replay(tmp_path / "replay.jsonl", root)

    result = CliRunner().invoke(cli, ["--dossier", str(root), "--replay", str(replay), CAPTURE])

    assert result.exit_code == 0, result.output
    assert "over budget" in result.output
    assert "D-002" in result.output
    assert "applied  2" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["--help"],
        ["show", "--help"],
        ["diff", "--help"],
        ["review", "--help"],
        ["export", "--help"],
        ["ask", "--help"],
    ],
)
def test_help_is_available(command: list[str]) -> None:
    result = CliRunner().invoke(cli, command)

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_init_uses_me_home_when_no_dossier_flag_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "custom-me"
    monkeypatch.setenv("ME_HOME", str(target))

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert (target / "profile.yaml").is_file()
