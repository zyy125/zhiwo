"""测试共享夹具。

M1 阶段所有测试都是**只读**的：直接指向仓库里的 ``examples/me/``，
不复制、不改写。plan §1 规定的"拷贝到临时目录再改"的夹具模式从 M2
开始生效——那时才第一次有测试需要写档案。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples" / "me"


@pytest.fixture
def examples_dir() -> Path:
    """黄金档案目录（``examples/me/``），只读。"""
    return EXAMPLES


@pytest.fixture
def profile_path(examples_dir: Path) -> Path:
    return examples_dir / "profile.yaml"


@pytest.fixture
def golden_changelog(examples_dir: Path) -> Path:
    """黄金 changelog 条目：SPEC §3 补丁形态的活样本（含 CJK evidence）。"""
    return examples_dir / "changelog" / "2026-10-11T2130.yaml"
