"""L4 的两套视图与 token 估算（SPEC §2.3 / §5 / §6）。

视图是**读者边界的实现**：core view 给提取器看（active + pending，含
locked 标注与惰性 supersede 边），reader view 给普通读者看（active，
按 clearance 过滤，journal 永不出现）。两者一旦渲染错误，前者会让模型
重复推导已知事实，后者会直接泄漏高敏感分区。

token 估算则被 M4 的预算钩子消费；这里钉住公式与“只数 reader view”
这两件事，避免预算悄悄把 pending / superseded 也算进去。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhiwo.factfile import FactDocument
from zhiwo.models import Axis, Clearance, Section, Sensitivity
from zhiwo.recall import (
    estimate_reader_tokens,
    estimate_tokens,
    load_documents,
    render_core_view,
    render_reader_view,
)
from zhiwo.store import Dossier

FIXTURES = Path(__file__).parent / "fixtures" / "extract"
CORE_VIEW = (FIXTURES / "core_view.txt").read_text(encoding="utf-8")


def _section(
    section_id: str,
    title: str,
    path: str,
    *,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> Section:
    return Section(
        id=section_id,
        title=title,
        path=path,
        axis=Axis.ABOUT,
        sensitivity=sensitivity,
    )


# --- core view（SPEC §2.3）--------------------------------------------------


def test_core_view_contains_active_locked_and_pending_but_not_superseded(
    dossier: Dossier,
) -> None:
    view = render_core_view(load_documents(dossier))

    assert "- f_001 [active] 厌恶风险，偏好确定性" in view
    assert "- f_003 [active, locked] 遇到大决定习惯列出所有选项再逐个排除" in view
    assert "- f_043 [pending] 根据二面反馈补项目深度（见 [[f_051]]）" in view
    assert "想读博" not in view
    assert "[superseded]" not in view


def test_core_view_keeps_the_inert_supersede_edge(dossier: Dossier) -> None:
    """pending 事实上的 supersedes 边确认前无效，但 extract 必须看得见。"""
    view = render_core_view(load_documents(dossier))

    assert "- f_017 [active] 决定直接工作，不读博 (supersedes f_009)" in view


def test_core_view_skips_empty_sections_and_comes_from_profile_order(
    dossier: Dossier,
) -> None:
    view = render_core_view(load_documents(dossier))
    blocks = [block for block in view.split("\n\n") if block]

    assert [block.splitlines()[0] for block in blocks] == [
        "about/values.md",
        "about/career.md",
        "goals/current.md",
    ]


# --- reader view（SPEC §5）--------------------------------------------------


def test_reader_view_excludes_pending_and_superseded(dossier: Dossier) -> None:
    view = render_reader_view(load_documents(dossier))

    assert "# 价值观与决策偏好" in view
    assert "- 厌恶风险，偏好确定性" in view
    assert "- 遇到大决定习惯列出所有选项再逐个排除" in view  # locked 仍是 active
    assert "根据二面反馈补项目深度" not in view  # pending
    assert "想读博" not in view  # superseded
    assert "f_001" not in view  # 读者视图不带系统 id


def test_reader_view_filters_sections_by_clearance() -> None:
    public = _section("public", "公开", "about/public.md")
    secret = _section(
        "secret",
        "私密",
        "about/secret.md",
        sensitivity=Sensitivity.HIGH,
    )
    documents = (
        (public, FactDocument.parse("- 可以公开 <!-- f_001 -->\n")),
        (secret, FactDocument.parse("- 不能公开 <!-- f_002 -->\n")),
    )

    normal = render_reader_view(documents, clearance=Clearance.NORMAL)
    high = render_reader_view(documents, clearance=Clearance.HIGH)

    assert "可以公开" in normal
    assert "不能公开" not in normal
    assert "可以公开" in high and "不能公开" in high


def test_reader_view_never_includes_the_journal_even_at_high_clearance(
    dossier: Dossier,
) -> None:
    view = render_reader_view(load_documents(dossier), clearance=Clearance.HIGH)

    assert "# 日常流水" not in view
    assert "面试挂了" not in view


def test_reader_view_omits_sections_with_no_active_facts() -> None:
    section = _section("empty", "空分区", "about/empty.md")
    document = FactDocument.parse(
        "# 标题\n\n"
        "- ~~旧事实~~ <!-- f_001 superseded-by:f_002 -->\n"
        "- 待确认 <!-- f_002 pending -->\n"
    )

    assert render_reader_view(((section, document),)) == ""


# --- token 估算（plan §3 决策 5 / D-007）-----------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("abcd", 1),
        ("abcde", 2),
        ("中文", 2),
        ("中abcd", 2),
        ("中文abcd", 3),
        ("中  a", 2),
        ("\U00020000", 1),  # CJK Extension B 也按 CJK 计 1
    ],
)
def test_token_estimate_uses_the_documented_heuristic(text: str, expected: int) -> None:
    assert estimate_tokens(text) == expected


def test_reader_token_estimate_counts_only_active_facts() -> None:
    section = _section("values", "价值观", "about/values.md")
    document = FactDocument.parse(
        "- 用 <!-- f_001 -->\n"
        "- 不用 <!-- f_002 pending -->\n"
        "- ~~旧~~ <!-- f_003 superseded-by:f_001 -->\n"
    )
    documents = ((section, document),)

    assert estimate_reader_tokens(documents) == estimate_tokens("# 价值观\n\n- 用\n")
    assert estimate_reader_tokens(documents) < estimate_tokens(
        "# 价值观\n\n- 用\n- 不用\n- ~~旧~~\n"
    )


def test_core_view_fixture_is_produced_by_the_recall_renderer(dossier: Dossier) -> None:
    """M5 的 prompt 夹具必须就是 M6 渲染器的输出。

    这条接缝决定录制文件的 prompt hash：如果夹具与生产渲染漂移，
    回放测试会绿着测一个不存在的输入形态。
    """
    assert render_core_view(load_documents(dossier)) == CORE_VIEW
