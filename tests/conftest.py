"""测试共享夹具。

M1 阶段的测试都是**只读**的：直接指向仓库里的 ``examples/me/``，不复制、
不改写。plan §1 规定的"拷贝到临时目录再改"的夹具模式从 M2 起生效——
那时才第一次有测试需要写档案（:func:`copy_dossier` 就是它）。

M4 起多一个 ``git_dossier``：黄金副本 + 它自己的 git 仓库。黄金
``examples/me/`` 本身永远不改；它不带 ``.gitignore``（那是 ``me init`` 的
产物，不是契约示例的一部分），所以测试要补上契约唯一的那一条，否则
``writer_lock`` 创建的 ``.index/lock`` 会被 ``git add -A`` 提交。
"""

import os
import shutil
from pathlib import Path

import pytest

from zhiwo.store import Dossier, git_init

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


@pytest.fixture
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """给测试一套自足的 git 身份，**不让测试依赖开发机的全局配置**。

    ``GIT_CONFIG_GLOBAL`` 指向空设备 + ``GIT_CONFIG_NOSYSTEM``：这台机器
    的 ``~/.gitconfig`` 里可能有 ``commit.gpgsign = true`` 之类的设置，
    那会让测试因为"没配 GPG 密钥"而红——红得毫无信息量。生产代码不碰
    身份（那是用户的），测试自己把环境铺好。
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Zhiwo Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Zhiwo Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


@pytest.fixture
def git_dossier(dossier: Dossier, git_identity: None) -> Dossier:
    """黄金副本 + 自己的 git 仓库（带契约要求的 ``.gitignore``）。

    ``git_identity`` 是依赖而非 autouse：只有真的要提交的测试才需要它。
    """
    (dossier.root / ".gitignore").write_text(".index/\n", encoding="utf-8")
    git_init(dossier.root)
    return dossier
