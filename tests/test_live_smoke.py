"""M9 的可选真实网络冒烟测试：只在显式要求时运行。

默认不进入 pytest 的选择集（``-m "not live"``），CI 与日常测试永远不会
联网；显式选择并设置环境变量后才运行::

    ME_LLM_LIVE_SMOKE=1 ME_LLM_MODEL=... ME_LLM_BASE_URL=... \\
    ME_LLM_API_KEY=... uv run pytest -m live -s

它验证两件事：端点确实能完成一次 chat completion；同样的 provider 能走完
一次 CJK 捕获并提交 journal。plan §9 要求 live 运行同时报告证据拒绝率
（R1 的调节数据），因此测试会把统计打印到 stdout——这只是一次观测，不对
模型是否提取出事实做断言，避免把一次连通性检查变成脆弱的行为测试。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zhiwo.llm import provider_from_env
from zhiwo.pipeline import capture
from zhiwo.store import bootstrap_dossier

pytestmark = pytest.mark.live

_CAPTURE_TEXT = "面试挂了，二面，反馈说项目深度不够。"


def test_live_provider_completes_and_captures(tmp_path: Path, git_identity: None) -> None:
    if os.environ.get("ME_LLM_LIVE_SMOKE") != "1":
        pytest.skip("set ME_LLM_LIVE_SMOKE=1 to run the live provider smoke test")

    provider = provider_from_env()
    if provider is None:
        pytest.skip("set ME_LLM_MODEL (and optionally ME_LLM_BASE_URL / ME_LLM_API_KEY)")

    answer = provider.complete("Reply with the single word: ok")
    assert answer.strip()

    root = tmp_path / "me"
    dossier = bootstrap_dossier(root)
    outcome = capture(dossier, _CAPTURE_TEXT, provider=provider)

    assert outcome.provider_error is None
    assert (dossier.root / outcome.capture.path).is_file()
    assert outcome.merge.commit

    evidence_rejections = [
        patch.reason
        for patch in outcome.merge.rejected
        if patch.reason is not None and "evidence" in patch.reason
    ]
    print(
        "\nlive capture: "
        f"applied={len(outcome.merge.applied)} "
        f"rejected={len(outcome.merge.rejected)} "
        f"evidence_rejections={len(evidence_rejections)} "
        f"extractor={provider.identity}"
    )
    for reason in evidence_rejections:
        print(f"  {reason}")
