"""journal 关键词检索（resolved decision #4 / SPEC §5）。

这一层同时受两条规则约束：

- **权限**：journal 是 high by default，normal 读者拿不到任何命中；
- **分词**：按 Unicode P*/Z* 切分，CJK bigram 只在连续段内生成。
  跨标点的 bigram（``了，二`` 里的 ``了二``）是典型假阳性，必须钉住。
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

import pytest

from zhiwo.errors import ZhiwoError
from zhiwo.models import Clearance
from zhiwo.recall import search_journal
from zhiwo.store import Dossier


def test_high_clearance_keyword_search_resolves_the_golden_line(dossier: Dossier) -> None:
    hits = search_journal(dossier, "项目深度", clearance=Clearance.HIGH)

    assert hits
    assert hits[0].source == "journal/2026-10-11.md#L7"
    assert hits[0].text == "面试挂了，二面，反馈说项目深度不够。"
    assert hits[0].score == 3  # 项目 / 目深 / 深度


def test_normal_clearance_gets_no_journal_hits(dossier: Dossier) -> None:
    assert search_journal(dossier, "项目深度", clearance=Clearance.NORMAL) == ()
    assert search_journal(dossier, "项目深度", clearance=cast(Clearance, "normal")) == ()


def test_cjk_bigrams_never_cross_punctuation(dossier: Dossier) -> None:
    """``面试挂了，二面`` 不含 ``了二``：标点把连续段切开了。"""
    assert search_journal(dossier, "了二", clearance=Clearance.HIGH) == ()
    assert search_journal(dossier, "深度不够", clearance=Clearance.HIGH)


def test_latin_tokens_are_casefolded_and_matched_as_tokens(dossier: Dossier) -> None:
    path = dossier.append_journal(["AI 面试反馈"], now=datetime(2026, 12, 1, 9, 0))

    hits = search_journal(dossier, "ai", clearance=Clearance.HIGH)

    assert hits
    assert hits[0].path == "journal/2026-12-01.md"
    assert hits[0].line_no == 3
    assert path.path.exists()


def test_ties_are_broken_by_newest_first(dossier: Dossier) -> None:
    dossier.append_journal(["项目复盘"], now=datetime(2026, 12, 1, 9, 0))

    hits = search_journal(dossier, "项目", clearance=Clearance.HIGH, top_n=1)

    assert hits[0].path == "journal/2026-12-01.md"
    assert hits[0].line_no == 3


def test_top_n_and_empty_queries_are_handled_explicitly(dossier: Dossier) -> None:
    assert len(search_journal(dossier, "面试", clearance=Clearance.HIGH, top_n=1)) == 1
    assert search_journal(dossier, "，。！", clearance=Clearance.HIGH) == ()

    with pytest.raises(ZhiwoError, match="top_n"):
        search_journal(dossier, "面试", clearance=Clearance.HIGH, top_n=0)
