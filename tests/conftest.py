"""测试共享夹具。

M1 阶段的测试都是**只读**的：直接指向仓库里的 ``examples/me/``，不复制、
不改写。plan §1 规定的"拷贝到临时目录再改"的夹具模式从 M2 起生效——
那时才第一次有测试需要写档案（:func:`copy_dossier` 就是它）。
"""

import shutil
from pathlib import Path

import pytest

from zhiwo.store import Dossier

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


@pytest.fixture
def copy_dossier(tmp_path: Path) -> Path:
    """黄金档案的**可写副本**，放在临时目录里。

    plan §1：测试永不改动 ``examples/me/`` 原件。那份目录是契约的说明性
    数据（SPEC §1/§2.1 引用的活样本），被测试改写之后，后续所有关于
    "示例能不能解析"的断言就都失去了意义——它们会对着被改过的样本断言。
    """
    destination = tmp_path / "me"
    shutil.copytree(EXAMPLES, destination)
    return destination


@pytest.fixture
def dossier(copy_dossier: Path) -> Dossier:
    """指向临时副本的 :class:`~zhiwo.store.Dossier`——M2 起测试的默认写靶子。"""
    return Dossier(copy_dossier)
