"""M5 的模型接入层：协议替身与录制/回放。

这里不测真实网络。目标是把 plan §7 M5 的两条关键性质钉死：

- provider 是可替换的纯数据边界（mock 能拿到 prompt，响应原样返回）；
- 录制按 prompt hash 匹配，prompt 一改就大声失败，而不是静默复用旧响应。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zhiwo.errors import ProviderError, ZhiwoError
from zhiwo.llm import MockProvider, RecordProvider, ReplayProvider, prompt_sha256

# --- MockProvider -----------------------------------------------------------


def test_mock_provider_returns_response_and_records_prompts() -> None:
    provider = MockProvider("[]", identity="mock-v1")

    assert provider.complete("first prompt") == "[]"
    assert provider.complete("second prompt") == "[]"
    assert provider.identity == "mock-v1"
    assert provider.prompts == ("first prompt", "second prompt")


def test_mock_provider_accepts_a_callable() -> None:
    provider = MockProvider(lambda prompt: f"echo:{prompt}")

    assert provider.complete("你好") == "echo:你好"
    assert provider.prompts == ("你好",)


def test_mock_provider_rejects_a_non_string_response() -> None:
    provider = MockProvider(lambda _prompt: 42)  # type: ignore[arg-type,return-value]

    with pytest.raises(ProviderError, match="expected str"):
        provider.complete("prompt")


def test_mock_provider_requires_an_identity() -> None:
    with pytest.raises(ZhiwoError, match="identity"):
        MockProvider("[]", identity="")


# --- RecordProvider ---------------------------------------------------------


def test_record_writes_utf8_jsonl_without_the_prompt(tmp_path: Path) -> None:
    path = tmp_path / "recording.jsonl"
    inner = MockProvider('{"op":"add","text":"厌恶风险"}', identity="mock-cjk")
    provider = RecordProvider(inner, path)

    assert provider.complete("这是不应被保存的 prompt") == '{"op":"add","text":"厌恶风险"}'

    raw = path.read_text(encoding="utf-8")
    assert "这是不应被保存的 prompt" not in raw
    assert "厌恶风险" in raw  # CJK 原文可读，不是 \\u 转义
    record = json.loads(raw)
    assert record == {
        "identity": "mock-cjk",
        "prompt_sha256": prompt_sha256("这是不应被保存的 prompt"),
        "response": '{"op":"add","text":"厌恶风险"}',
        "version": 1,
    }


def test_record_does_not_write_when_the_inner_provider_fails(tmp_path: Path) -> None:
    class Failing:
        identity = "failing"

        def complete(self, prompt: str) -> str:
            raise ProviderError("boom")

    path = tmp_path / "recording.jsonl"
    provider = RecordProvider(Failing(), path)

    with pytest.raises(ProviderError, match="boom"):
        provider.complete("prompt")
    assert not path.exists()


# --- ReplayProvider ---------------------------------------------------------


def test_record_then_replay_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "recording.jsonl"
    recorded = RecordProvider(MockProvider("response", identity="mock-v0"), path)
    recorded.complete("prompt one")
    recorded.complete("prompt two")

    replay = ReplayProvider(path)
    assert replay.identity == "mock-v0"
    assert replay.complete("prompt two") == "response"
    assert replay.complete("prompt one") == "response"


def test_replay_matches_prompt_hash_not_call_order(tmp_path: Path) -> None:
    path = tmp_path / "recording.jsonl"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "identity": "mock-v0",
                "prompt_sha256": prompt_sha256("first"),
                "response": "A",
            }
        )
        + "\n"
        + json.dumps(
            {
                "version": 1,
                "identity": "mock-v0",
                "prompt_sha256": prompt_sha256("second"),
                "response": "B",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    replay = ReplayProvider(path)
    assert replay.complete("second") == "B"
    assert replay.complete("first") == "A"


def test_replay_missing_prompt_raises_and_names_the_hash(tmp_path: Path) -> None:
    path = tmp_path / "recording.jsonl"
    RecordProvider(MockProvider("response"), path).complete("recorded")

    replay = ReplayProvider(path)
    with pytest.raises(ProviderError, match=prompt_sha256("missing")):
        replay.complete("missing")


def test_replay_rejects_conflicting_duplicate_hashes(tmp_path: Path) -> None:
    path = tmp_path / "recording.jsonl"
    line = json.dumps(
        {
            "version": 1,
            "identity": "mock-v0",
            "prompt_sha256": prompt_sha256("same"),
            "response": "A",
        }
    )
    path.write_text(line + "\n" + line.replace('"A"', '"B"') + "\n", encoding="utf-8")

    with pytest.raises(ProviderError, match="conflicting responses"):
        ReplayProvider(path)


def test_replay_rejects_invalid_json_and_version(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.jsonl"
    bad_json.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ProviderError, match="invalid JSON"):
        ReplayProvider(bad_json)

    bad_version = tmp_path / "version.jsonl"
    bad_version.write_text(
        json.dumps(
            {
                "version": 99,
                "identity": "mock-v0",
                "prompt_sha256": prompt_sha256("p"),
                "response": "r",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ProviderError, match="unsupported record version"):
        ReplayProvider(bad_version)


def test_replay_requires_an_identity_when_the_file_mixes_providers(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    records = [
        {
            "version": 1,
            "identity": identity,
            "prompt_sha256": prompt_sha256(prompt),
            "response": "r",
        }
        for identity, prompt in (("mock-a", "a"), ("mock-b", "b"))
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    with pytest.raises(ProviderError, match="mixes identities"):
        ReplayProvider(path)

    assert ReplayProvider(path, identity="chosen").identity == "chosen"


def test_replay_rejects_missing_or_empty_recordings(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="cannot read recording"):
        ReplayProvider(tmp_path / "missing.jsonl")

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ProviderError, match="no records"):
        ReplayProvider(empty)
