"""单写者锁的测试（plan §3 决策 3、SPEC 决议 3）。

plan §7 M2 的关键测试之一是 "flock contention"：竞争必须**立刻失败**
（:class:`~zhiwo.errors.LockHeld`），而不是排队等锁。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zhiwo.errors import LockHeld
from zhiwo.store import Dossier

# 子进程只干一件事：按 flock 的语义占住锁，然后等待被杀。
# 刻意**不 import zhiwo**——这条测试要验证的是"两个进程之间的锁语义"，
# 越少依赖越好。
_CHILD_HOLDS_LOCK = """
import fcntl, os, sys, time

root = sys.argv[1]
ready = sys.argv[2]
os.makedirs(os.path.join(root, ".index"), exist_ok=True)
fd = os.open(os.path.join(root, ".index", "lock"), os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
open(ready, "w").close()
time.sleep(60)
"""


def test_second_writer_fails_immediately(dossier: Dossier) -> None:
    """同一进程里再开一个 fd 也是真竞争：flock 是按打开文件描述生效的。"""
    with dossier.writer_lock():
        started = time.monotonic()
        with pytest.raises(LockHeld), dossier.writer_lock():
            pass  # pragma: no cover - 不该进得来
        assert time.monotonic() - started < 1.0, "锁失败必须是立刻的，不排队（决策 3）"


def test_lock_error_names_the_lock_file(dossier: Dossier) -> None:
    with dossier.writer_lock(), pytest.raises(LockHeld) as excinfo, dossier.writer_lock():
        pass  # pragma: no cover
    assert str(dossier.lock_path) in str(excinfo.value)


def test_lock_is_released_when_the_context_exits(dossier: Dossier) -> None:
    with dossier.writer_lock():
        pass
    with dossier.writer_lock():  # 不抛就说明放掉了
        pass


def test_lock_is_released_even_after_an_exception(dossier: Dossier) -> None:
    """异常路径同样要放锁——否则一次失败会把档案永久锁死。"""
    with pytest.raises(RuntimeError), dossier.writer_lock():
        raise RuntimeError("模拟 merge 中途炸了")
    with dossier.writer_lock():
        pass


def test_lock_creates_the_index_directory_lazily(dossier: Dossier) -> None:
    """``.index/`` 只为放锁而存在（plan §3 决策 1）：用到时才建。"""
    assert not dossier.index_dir.exists()
    with dossier.writer_lock():
        assert dossier.lock_path.is_file()
    assert dossier.index_dir.is_dir()


def test_lock_contention_across_processes(dossier: Dossier, tmp_path: Path) -> None:
    """真正的跨进程竞争：另一个进程持锁时，我们立刻拿到 LockHeld。

    同进程的双 fd 测试已经能验证语义（flock 按 OFD），但"另一个进程"是
    这条契约实际要对付的场景（CLI 与 MCP server 同时写），所以值得真跑一次。
    子进程先把 ready 文件写出来再睡，父进程轮询等它——不用 readline，
    以免子进程启动失败时整个测试卡死。
    """
    ready = tmp_path / "child-ready"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLDS_LOCK, str(dossier.root), str(ready)]
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "子进程没能拿到锁（脚本路径或权限有问题）"

        with pytest.raises(LockHeld), dossier.writer_lock():
            pass  # pragma: no cover
    finally:
        child.terminate()
        child.wait(timeout=15)


def test_a_stale_lock_file_does_not_block_anyone(dossier: Dossier) -> None:
    """锁文件留在磁盘上不等于锁还在：flock 随进程/描述符消失而释放。

    这条区分很重要——否则用户会以为"上次崩了，档案被锁住了"，
    而去手删 ``.index/``。
    """
    dossier.index_dir.mkdir(parents=True, exist_ok=True)
    dossier.lock_path.touch()
    with dossier.writer_lock():
        pass


def test_lock_lives_in_the_disposable_index_directory(tmp_path: Path) -> None:
    """锁是 ``.index/`` 里唯一的常驻物（plan §3 决策 1），且它是空文件。

    锁的"内容"没有意义——被持有的东西是那块 inode（fd 上的 flock），
    不是文件里的字节。所以它不必、也不该往磁盘上写任何状态。
    """
    from zhiwo.store import bootstrap_dossier

    dossier = bootstrap_dossier(tmp_path / "me")
    with dossier.writer_lock():
        assert dossier.lock_path.parent == dossier.index_dir
        assert os.stat(dossier.lock_path).st_size == 0
    # 退出锁之后锁文件仍在（留着无妨：flock 已随 fd 关闭释放）
    assert dossier.lock_path.is_file()
