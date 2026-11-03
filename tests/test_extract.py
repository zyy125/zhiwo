"""M5 的响应解析与提取编排。

两条边界分开测：

- ``parse_extract_response`` 只负责"文本里有没有 JSON、外层形状能不能
  认出来"，不做补丁字段校验；
- ``extract`` 只负责拼 prompt、调 provider、包装解析结果，不碰磁盘，
  也不把 provider 失败伪装成"没有新事实"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zhiwo.errors import ProviderError, ZhiwoError
from zhiwo.extract import extract, parse_extract_response
from zhiwo.llm import MockProvider
from zhiwo.schema import load_profile

FIXTURES = Path(__file__).parent / "fixtures" / "extract"
CORE_VIEW = (FIXTURES / "core_view.txt").read_text(encoding="utf-8")


# --- 响应解析 ---------------------------------------------------------------


def test_plain_json_array_is_returned_in_order() -> None:
    parsed = parse_extract_response('[{"op":"add"},{"op":"link"}]')

    assert parsed.error is None
    assert parsed.drafts == ({"op": "add"}, {"op": "link"})


def test_markdown_fence_and_surrounding_prose_are_tolerated() -> None:
    response = (
        "Here are the patches you asked for:\n"
        "```json\n"
        '[{"op":"add","text":"厌恶风险"}]\n'
        "```\n"
        "Let me know if you need anything else.\n"
    )

    parsed = parse_extract_response(response)

    assert parsed.error is None
    assert parsed.drafts == ({"op": "add", "text": "厌恶风险"},)


def test_patches_wrapper_and_single_patch_object_are_accepted() -> None:
    wrapped = parse_extract_response('{"patches":[{"op":"add"}]}')
    single = parse_extract_response('{"op":"add","text":"一条事实"}')

    assert wrapped.drafts == ({"op": "add"},)
    assert single.drafts == ({"op": "add", "text": "一条事实"},)


def test_empty_and_malformed_responses_become_diagnostics() -> None:
    empty = parse_extract_response("   \n")
    malformed = parse_extract_response("the model forgot the JSON")

    assert empty.drafts == ()
    assert empty.error is not None and "empty" in empty.error
    assert malformed.drafts == ()
    assert malformed.error is not None and "JSON array" in malformed.error


def test_unrecognised_json_shape_is_an_error() -> None:
    assert parse_extract_response('{"hello":"world"}').drafts == ()
    assert parse_extract_response("null").drafts == ()
    assert parse_extract_response("[1, 2]").error is None


def test_non_mapping_entries_are_preserved_for_merge_to_report() -> None:
    """extract 不替 merge 过滤；一条坏条目应在 M4 的 malformed 报告里出现。"""
    parsed = parse_extract_response('[1, "two", {"op":"add"}]')

    assert parsed.error is None
    assert parsed.drafts == (1, "two", {"op": "add"})


# --- 提取编排 ---------------------------------------------------------------


def test_extract_calls_provider_with_the_built_prompt(profile_path: Path) -> None:
    provider = MockProvider('[{"op":"add","target":"about/values.md"}]', identity="mock-v1")
    lines = ("面试挂了，二面。",)

    result = extract(
        provider=provider,
        profile=load_profile(profile_path),
        core_view=CORE_VIEW,
        lines=lines,
        first_line=7,
    )

    assert result.error is None
    assert result.extractor == "mock-v1"
    assert result.drafts == ({"op": "add", "target": "about/values.md"},)
    assert len(provider.prompts) == 1
    assert CORE_VIEW in provider.prompts[0]
    assert "[L7] 面试挂了，二面。" in provider.prompts[0]


def test_extract_lets_provider_failures_escape(profile_path: Path) -> None:
    class FailingProvider:
        identity = "failing"

        def complete(self, prompt: str) -> str:
            raise ProviderError("network down")

    with pytest.raises(ProviderError, match="network down"):
        extract(
            provider=FailingProvider(),
            profile=load_profile(profile_path),
            core_view="",
            lines=("一句",),
            first_line=1,
        )


def test_extract_rejects_a_non_string_provider_response(profile_path: Path) -> None:
    class BadProvider:
        identity = "bad"

        def complete(self, prompt: str) -> str:
            return 42  # type: ignore[return-value]

    with pytest.raises(ProviderError, match="expected str"):
        extract(
            provider=BadProvider(),
            profile=load_profile(profile_path),
            core_view="",
            lines=("一句",),
            first_line=1,
        )


def test_extract_requires_a_non_empty_identity(profile_path: Path) -> None:
    class AnonymousProvider:
        identity = ""

        def complete(self, prompt: str) -> str:
            return "[]"

    with pytest.raises(ZhiwoError, match="identity"):
        extract(
            provider=AnonymousProvider(),
            profile=load_profile(profile_path),
            core_view="",
            lines=("一句",),
            first_line=1,
        )


def test_cjk_replay_fixture_produces_the_expected_drafts(profile_path: Path) -> None:
    """录制的 prompt 与当前构建结果必须 hash 一致，响应里的 CJK 原样解析。"""
    from zhiwo.llm import ReplayProvider

    replay = ReplayProvider(Path(__file__).parent / "fixtures" / "llm" / "cjk.jsonl")
    result = extract(
        provider=replay,
        profile=load_profile(profile_path),
        core_view=CORE_VIEW,
        lines=(
            "昨晚只睡了六个小时，下午有个面试。",
            "面试挂了，二面，反馈说项目深度不够。",
        ),
        first_line=5,
    )

    assert result.error is None
    assert result.extractor == "mock-cjk"
    assert result.drafts == (
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
        {
            "op": "link",
            "from": "f_043",
            "to": "f_051",
        },
    )


def test_recorded_fixture_is_jsonl_and_keeps_cjk_readable() -> None:
    path = Path(__file__).parent / "fixtures" / "llm" / "cjk.jsonl"
    line = path.read_text(encoding="utf-8").strip()

    record = json.loads(line)
    assert record["identity"] == "mock-cjk"
    assert "二面失败" in record["response"]
