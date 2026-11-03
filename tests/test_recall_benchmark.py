"""5k changelog join 基准（plan §3 决策 1 / §7 M6）。

MVP 的 ``.index/`` 不持久化，逻辑记录每次现算。这个基准是那个决定的
护栏：changelog 是只追加的，join 的输入随历史线性增长；如果 5k 文件
已经慢到不可用，就该把 ``me index rebuild`` 提前，而不是把单次 CLI
调用拖成秒级等待。

阈值刻意宽松（10 秒；本地基线约 4.4 秒），因为 CI 机器差异远大于这里
要拦的性能悬崖。真正的回归（例如每个事实重新扫一遍全部 changelog 的
O(n²)）会远远越过预算，而不是在 9.9 与 10.0 秒之间擦边。
"""

from __future__ import annotations

from pathlib import Path
from time import perf_counter

from zhiwo.factfile import FactDocument
from zhiwo.recall import logical_records
from zhiwo.store import bootstrap_dossier

_RECORDS = 5_000
_BUDGET_SECONDS = 10.0


def test_five_thousand_changelog_join_stays_within_budget(tmp_path: Path) -> None:
    dossier = bootstrap_dossier(tmp_path / "me")
    profile = dossier.load_profile()
    section = next(section for section in profile.sections if section.path == "about/values.md")

    facts = "".join(
        f"- 事实 {number} <!-- f_{number:04d} -->\n" for number in range(1, _RECORDS + 1)
    )
    dossier.write_fact_document(section, FactDocument.parse(facts))

    # 直接写 changelog 文件，而不是走 write_changelog：后者每次 fsync 目录，
    # 测的会是磁盘元数据延迟；这里要测的是 join 的解析与索引成本。
    for number in range(1, _RECORDS + 1):
        (dossier.changelog_dir / f"2026-01-01T000000-{number:05d}.yaml").write_text(
            "merge: 2026-01-01T000000\n"
            "input: journal/2026-01-01.md\n"
            "extractor: benchmark\n"
            "patches:\n"
            "  - op: add\n"
            f"    id: f_{number:04d}\n"
            "    target: about/values.md\n"
            "    kind: statement\n"
            f"    text: 事实 {number}\n"
            f"    evidence: 事实 {number}\n"
            "    source: journal/2026-01-01.md#L1\n",
            encoding="utf-8",
        )

    started = perf_counter()
    records = logical_records(dossier)
    elapsed = perf_counter() - started

    assert len(records) == _RECORDS
    assert elapsed < _BUDGET_SECONDS, (
        f"5k changelog join took {elapsed:.2f}s (budget {_BUDGET_SECONDS:.1f}s); "
        "pull me index rebuild forward instead of letting every invocation scan history"
    )
