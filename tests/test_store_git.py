"""git 原语的测试（plan §5 步骤 11、SPEC §7 的 "Nothing is ever lost"）。

M2 只提供原语：``git init`` 与"把工作区提交成一个提交"。**每次 merge
一个提交**的编排属 M4；这里验证的是那个编排可以依赖的地基——
一次提交、工作区干净、作者身份来自用户（测试用环境变量解决）。
"""

import os
import subprocess
from pathlib import Path

import pytest

from zhiwo.errors import GitError
from zhiwo.store import Dossier, bootstrap_dossier


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


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_bootstrap_initialises_a_repository(tmp_path: Path, git_identity: None) -> None:
    """``me init`` 的库层：有 .git，且**还没有**提交（用户已定：不提交）。"""
    root = tmp_path / "me"
    bootstrap_dossier(root)

    assert (root / ".git").is_dir()
    with pytest.raises(subprocess.CalledProcessError):
        _git(root, "rev-parse", "HEAD")  # 还没有 HEAD —— 第一个提交属于第一次 merge


def test_git_commit_creates_exactly_one_commit_and_a_clean_tree(
    tmp_path: Path, git_identity: None
) -> None:
    """plan §7 M4 会依赖的性质：一次 ``git_commit`` = 一个提交 + 干净工作区。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    (dossier.root / "about" / "career.md").write_text(
        "- 二面反馈：项目深度不够 <!-- f_051 -->\n", encoding="utf-8"
    )

    short_sha = dossier.git_commit("merge: apply capture m_2026-10-12T213000")

    assert short_sha == _git(dossier.root, "rev-parse", "--short=12", "HEAD")
    assert _git(dossier.root, "rev-list", "--count", "HEAD") == "1"
    assert _git(dossier.root, "status", "--porcelain") == ""
    assert _git(dossier.root, "log", "-1", "--format=%s") == (
        "merge: apply capture m_2026-10-12T213000"
    )


def test_git_commit_picks_up_new_and_deleted_files(tmp_path: Path, git_identity: None) -> None:
    """``add -A``：新增、修改、删除一并入账——merge 的产物从来不止一种。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    dossier.git_commit("merge: first")

    (dossier.root / "goals" / "current.md").write_text(
        "- 新事实 <!-- f_001 -->\n", encoding="utf-8"
    )
    (dossier.root / "about" / "health.md").unlink()

    dossier.git_commit("merge: second")

    assert _git(dossier.root, "rev-list", "--count", "HEAD") == "2"
    changed = _git(dossier.root, "show", "--name-status", "--format=", "HEAD").splitlines()
    statuses = {line.split("\t")[0] for line in changed}
    assert statuses == {"M", "D"}


def test_git_commit_with_nothing_to_commit_raises(tmp_path: Path, git_identity: None) -> None:
    """空提交是失败的，不是静默的成功——合并层需要知道"这次什么都没发生"。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    dossier.git_commit("merge: first")
    with pytest.raises(GitError) as excinfo:
        dossier.git_commit("merge: 没有任何改动")
    assert "nothing to commit" in str(excinfo.value)


def test_git_error_carries_a_usable_message(tmp_path: Path) -> None:
    """没有身份又没有仓库存根时，错误来自 git 自己——消息里要能看见原因。"""
    root = tmp_path / "not-a-repo"
    root.mkdir()
    with pytest.raises(GitError) as excinfo:
        Dossier(root).git_commit("merge: 无处可提交")
    assert "not a git repository" in str(excinfo.value)
