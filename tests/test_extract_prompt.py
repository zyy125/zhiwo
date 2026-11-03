"""M5 的 prompt 构建：快照、数据来源与行号坐标。

prompt 是 extract 的产品行为，不是可以随手改的实现细节：evidence 边界、
源语言、pending/locked 规则都靠它先挡住。因此这里比较完整快照，并单独
断言几条最容易被重构误删的规则。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhiwo.errors import ZhiwoError
from zhiwo.extract import build_extract_prompt
from zhiwo.schema import load_profile, parse_profile

FIXTURES = Path(__file__).parent / "fixtures" / "extract"
CORE_VIEW = (FIXTURES / "core_view.txt").read_text(encoding="utf-8")
LINES = (
    "昨晚只睡了六个小时，下午有个面试。",
    "面试挂了，二面，反馈说项目深度不够。",
)
FIRST_LINE = 5


def test_prompt_matches_the_committed_snapshot(profile_path: Path) -> None:
    prompt = build_extract_prompt(
        profile=load_profile(profile_path),
        core_view=CORE_VIEW,
        lines=LINES,
        first_line=FIRST_LINE,
    )

    assert prompt == (FIXTURES / "prompt.txt").read_text(encoding="utf-8")


def test_prompt_keeps_the_source_language_rule_and_capture_coordinates(
    profile_path: Path,
) -> None:
    prompt = build_extract_prompt(
        profile=load_profile(profile_path),
        core_view=CORE_VIEW,
        lines=LINES,
        first_line=FIRST_LINE,
    )

    assert "Never translate." in prompt
    assert "[L5] 昨晚只睡了六个小时，下午有个面试。" in prompt
    assert "[L6] 面试挂了，二面，反馈说项目深度不够。" in prompt
    assert CORE_VIEW in prompt


def test_targets_come_from_profile_and_exclude_journal(profile_path: Path) -> None:
    prompt = build_extract_prompt(
        profile=load_profile(profile_path),
        core_view="",
        lines=("一句",),
        first_line=1,
    )

    assert "- about/identity.md — 基本情况 — about" in prompt
    assert "- goals/current.md — 当前在办 — goals" in prompt
    assert "\n- journal/" not in prompt


def test_prompt_does_not_hardcode_section_layout() -> None:
    profile = parse_profile(
        {
            "sections": [
                {
                    "id": "elsewhere",
                    "title": "别处",
                    "path": "custom/place.md",
                    "axis": "goals",
                    "sensitivity": "normal",
                }
            ]
        },
        source="<test>",
    )

    prompt = build_extract_prompt(
        profile=profile,
        core_view="",
        lines=("一句",),
        first_line=1,
    )

    assert "- custom/place.md — 别处 — goals" in prompt
    assert "about/identity.md" not in prompt


def test_empty_or_multiline_capture_is_rejected(profile_path: Path) -> None:
    profile = load_profile(profile_path)

    with pytest.raises(ZhiwoError, match="at least one capture line"):
        build_extract_prompt(profile=profile, core_view="", lines=(), first_line=1)
    with pytest.raises(ZhiwoError, match="line terminators"):
        build_extract_prompt(profile=profile, core_view="", lines=("a\nb",), first_line=1)
    with pytest.raises(ZhiwoError, match="first_line"):
        build_extract_prompt(profile=profile, core_view="", lines=("a",), first_line=0)
