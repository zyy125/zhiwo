"""录制与回放：把一次 provider 响应钉成可提交、可离线重放的 JSONL。

这是 plan §7 M5 的"mock/record/replay first"落地。录制的目的不是备份
模型输出，而是让测试**离线、确定、可审计**：CI 不联网，也不依赖某个
模型今天是否愿意输出合法 JSON。

格式（每行一个 JSON 对象，UTF-8，CJK 原文不转义）::

    {"version":1,"identity":"mock-v0","prompt_sha256":"…","response":"…"}

两个刻意选择：

1. **按 prompt 的 SHA-256 匹配，而不是按调用顺序。** 顺序匹配会在
   prompt 改动后静默复用旧响应——测试仍然绿，但测的已经不是当前 prompt。
   hash 不匹配会大声失败，迫使作者重新录制；这是 prompt 快照测试的另一半。
2. **默认不保存完整 prompt。** prompt 里带着 core view（个人上下文），
   把它复制进 ``tests/fixtures`` 等于在仓库里造了第二份档案。hash 足够
   定位"是不是同一段输入"，而要调试 prompt 本身有专门的快照文件。

回放器永不联网、永不 fallback 到真实模型：缺失就是失败，避免"本地绿、
CI 红"这种最难查的漂移。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from zhiwo.errors import ProviderError, ZhiwoError
from zhiwo.llm.base import Provider

_RECORD_VERSION = 1


def prompt_sha256(prompt: str) -> str:
    """返回 prompt 的 SHA-256 十六进制摘要（录制与回放共用的键）。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class RecordProvider:
    """包装一个真实/假 provider，把每次成功响应追加到 JSONL。

    只记录**成功返回**的调用：provider 抛 ``ProviderError`` 时没有可回放
    的响应，写一条空记录只会让回放误以为当时"模型说了空话"。录制失败
    （磁盘错误）会转成 ``ProviderError``——调用方已经拿到了模型响应，但
    这次录制不完整，不应假装成功。
    """

    def __init__(self, inner: Provider, path: Path) -> None:
        self._inner = inner
        self._path = path

    @property
    def identity(self) -> str:
        return self._inner.identity

    def complete(self, prompt: str) -> str:
        response = self._inner.complete(prompt)
        record = {
            "version": _RECORD_VERSION,
            "identity": self._inner.identity,
            "prompt_sha256": prompt_sha256(prompt),
            "response": response,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise ProviderError(f"cannot write recording {self._path}: {exc}") from exc
        return response


class ReplayProvider:
    """从 JSONL 录制文件回放响应；按 prompt hash 精确匹配。

    ``identity`` 的优先级：构造参数 > 文件里唯一出现的 identity >
    ``replay:<文件名>``。录制文件里若混入多个 identity 而又没显式指定，
    说明这份 fixture 来源不唯一，回放出的 changelog 会说不清提取器是谁；
    此时大声失败，而不是随便挑一个。
    """

    def __init__(self, path: Path, *, identity: str | None = None) -> None:
        self._path = path
        responses, identities = _load_records(path)
        if identity is not None:
            if not identity:
                raise ZhiwoError("provider identity must be a non-empty string")
            self._identity = identity
        elif len(identities) == 1:
            self._identity = next(iter(identities))
        elif not identities:
            raise ProviderError(f"recording has no records: {path}")
        else:
            raise ProviderError(
                f"recording {path} mixes identities {sorted(identities)}; "
                "pass identity= to choose the changelog extractor label"
            )
        self._responses = responses

    @property
    def identity(self) -> str:
        return self._identity

    def complete(self, prompt: str) -> str:
        digest = prompt_sha256(prompt)
        try:
            return self._responses[digest]
        except KeyError as exc:
            raise ProviderError(
                f"no recorded response for prompt SHA-256 {digest} in {self._path}; "
                "re-record the fixture after changing the prompt"
            ) from exc


def _load_records(path: Path) -> tuple[dict[str, str], set[str]]:
    """读取并校验 JSONL；返回 hash→response 与出现过的 identity 集合。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProviderError(f"cannot read recording {path}: {exc}") from exc

    responses: dict[str, str] = {}
    identities: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            # 尾部空行/手工分组空行不是记录；忽略比报错更符合编辑体验。
            continue
        record = _parse_line(line, path=path, lineno=lineno)
        digest = cast(str, record["prompt_sha256"])
        response = cast(str, record["response"])
        previous = responses.get(digest)
        if previous is not None and previous != response:
            raise ProviderError(
                f"{path}:{lineno}: conflicting responses for prompt SHA-256 {digest}"
            )
        responses[digest] = response
        identities.add(cast(str, record["identity"]))
    if not responses:
        raise ProviderError(f"recording has no records: {path}")
    return responses, identities


def _parse_line(line: str, *, path: Path, lineno: int) -> dict[str, object]:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProviderError(f"{path}:{lineno}: record must be a JSON object")

    record = cast(dict[str, object], raw)
    version = record.get("version", _RECORD_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != _RECORD_VERSION:
        raise ProviderError(
            f"{path}:{lineno}: unsupported record version {version!r} (expected {_RECORD_VERSION})"
        )
    for key in ("identity", "prompt_sha256", "response"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise ProviderError(f"{path}:{lineno}: {key} must be a non-empty string")
    return record
