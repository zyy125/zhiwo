"""store 的发现、原子写、事实文件 IO、bootstrap 与 id 扫描测试。

对应 plan §7 M2 的 "bootstrap tree" 关键测试，以及 plan §3 决策 2/4 的
发现顺序与 id 空间。
"""

from datetime import datetime
from pathlib import Path

import pytest

from zhiwo import store
from zhiwo.errors import DossierNotFound, SchemaError, ZhiwoError
from zhiwo.models import (
    Axis,
    ChangelogEntry,
    ChangelogPatch,
    FactKind,
    PatchOp,
    PatchResult,
    Section,
)
from zhiwo.schema import template_text
from zhiwo.store import Dossier, atomic_write, bootstrap_dossier

# --- 发现（plan §3 决策 2：--dossier > ME_HOME > ~/.me/）------------------


def test_explicit_path_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    env = tmp_path / "env"
    env.mkdir()
    monkeypatch.setenv("ME_HOME", str(env))
    assert Dossier.discover(explicit).root == explicit


def test_env_beats_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "env"
    env.mkdir()
    home = tmp_path / "home"
    (home / ".me").mkdir(parents=True)
    monkeypatch.setenv("ME_HOME", str(env))
    monkeypatch.setenv("HOME", str(home))
    assert Dossier.discover().root == env


def test_home_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没给 flag、没给 env 时落到 ``~/.me/``。"""
    home = tmp_path / "home"
    (home / ".me").mkdir(parents=True)
    monkeypatch.delenv("ME_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert Dossier.discover().root == home / ".me"


def test_explicit_missing_path_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式路径指错时**不**回退到环境变量指向的档案。

    静默回退的后果是把事实写进另一个人的档案——比报错危险得多。
    """
    env = tmp_path / "env"
    env.mkdir()
    monkeypatch.setenv("ME_HOME", str(env))
    with pytest.raises(DossierNotFound) as excinfo:
        Dossier.discover(tmp_path / "nope")
    message = str(excinfo.value)
    assert "no fallback" in message
    assert str(env) not in message  # 没有偷偷告诉调用方"其实还有一个备选"


def test_discovery_lists_every_candidate_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全都找不到时，错误消息要列清楚试过哪些位置。"""
    missing = tmp_path / "missing-env"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ME_HOME", str(missing))
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(DossierNotFound) as excinfo:
        Dossier.discover()
    message = str(excinfo.value)
    assert "ME_HOME" in message
    assert str(missing) in message
    assert "~/.me" in message


def test_empty_me_home_is_not_a_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ME_HOME=""``（比如 shell 里 ``export ME_HOME=``）等同于没设置。"""
    home = tmp_path / "home"
    (home / ".me").mkdir(parents=True)
    monkeypatch.setenv("ME_HOME", "   ")
    monkeypatch.setenv("HOME", str(home))
    assert Dossier.discover().root == home / ".me"


def test_constructing_over_a_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(DossierNotFound):
        Dossier(tmp_path / "nope")


# --- 原子写（plan §4 编码法 + plan §5 崩溃语义）---------------------------


def test_atomic_write_creates_parents_and_cleans_up(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "file.md"
    atomic_write(target, "内容\n")
    assert target.read_text(encoding="utf-8") == "内容\n"
    # 临时文件不能留在目录里（plan §1：档案里只该有契约说的东西）
    assert list(target.parent.glob(".*.tmp")) == []


def test_atomic_write_preserves_bytes_exactly(tmp_path: Path) -> None:
    """写出去的字节就是我给的那些：CRLF 与无尾换行都不被"规整"。"""
    target = tmp_path / "facts.md"
    payload = "- 一 <!-- f_001 -->\r\n- 二 <!-- f_002 -->"
    atomic_write(target, payload)
    assert target.read_bytes() == payload.encode("utf-8")


def test_atomic_write_keeps_cjk_readable(tmp_path: Path) -> None:
    """编码法（plan §4）：中文不许变成 ``\\uXXXX``。"""
    target = tmp_path / "facts.md"
    atomic_write(target, "面试挂了，二面，反馈说项目深度不够。\n")
    raw = target.read_bytes()
    assert "面试挂了，二面，反馈说项目深度不够。".encode() in raw
    assert b"\\u" not in raw


def test_atomic_write_replaces_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "facts.md"
    target.write_text("旧内容\n", encoding="utf-8")
    atomic_write(target, "新内容\n")
    assert target.read_text(encoding="utf-8") == "新内容\n"


# --- 事实文件 IO ----------------------------------------------------------


def _section(dossier: Dossier, section_id: str) -> Section:
    return next(section for section in dossier.load_profile().sections if section.id == section_id)


def test_missing_section_file_reads_as_none(dossier: Dossier) -> None:
    """声明了分区但文件还没建：返回 None，交给 M3 去补建（plan §5 步骤 4）。"""
    assert dossier.read_fact_document(_section(dossier, "health")) is None


def test_declared_and_missing_are_distinguishable_from_empty(dossier: Dossier) -> None:
    """空文件与"还没建"是两种状态，调用方必须能分辨。"""
    section = _section(dossier, "values")
    document = dossier.read_fact_document(section)
    assert document is not None
    assert document.facts() != []

    empty_section = _section(dossier, "long-term")
    assert dossier.read_fact_document(empty_section) is None
    (dossier.root / "goals" / "long-term.md").touch()
    blank = dossier.read_fact_document(empty_section)
    assert blank is not None and blank.facts() == []


def test_write_back_untouched_document_is_byte_identical(dossier: Dossier) -> None:
    """读进来、不做任何修改、写回去 —— 一个字节都不该变（round-trip 定律）。"""
    section = _section(dossier, "values")
    path = dossier.root / "about" / "values.md"
    before = path.read_bytes()

    document = dossier.read_fact_document(section)
    assert document is not None
    dossier.write_fact_document(section, document)

    assert path.read_bytes() == before


def test_section_paths_come_from_profile(dossier: Dossier) -> None:
    """硬规则 1：路径来自 profile，不是代码里的常量。"""
    section = _section(dossier, "current")
    assert dossier.section_path(section).name == "current.md"


def test_journal_dir_comes_from_profile(dossier: Dossier) -> None:
    assert dossier.journal_dir() == dossier.root / "journal"


def test_profile_without_journal_section_raises(tmp_path: Path) -> None:
    """没有时间流的档案上"追加一条捕获"没有意义（SPEC §1）。"""
    root = tmp_path / "me"
    root.mkdir()
    (root / "profile.yaml").write_text(
        "sections:\n"
        "  - id: identity\n"
        "    title: 基本情况\n"
        "    path: about/identity.md\n"
        "    axis: about\n"
        "    sensitivity: normal\n",
        encoding="utf-8",
    )
    with pytest.raises(ZhiwoError):
        Dossier(root).journal_dir()


def test_non_utf8_section_file_is_a_schema_error(dossier: Dossier) -> None:
    """用别的编码存过的档案 = 读不出来 = SchemaError（与 L1 同一姿态）。"""
    (dossier.root / "about" / "values.md").write_bytes("中文".encode("gb18030"))
    with pytest.raises(SchemaError):
        dossier.read_fact_document(_section(dossier, "values"))


# --- id 空间（plan §3 决策 4 的使能者）-----------------------------------


def test_known_fact_ids_is_files_union_changelog(dossier: Dossier) -> None:
    """黄金档案的全部 id：文件里的 ∪ changelog 里出现过的。

    ``f_043`` / ``f_051`` 在两处都有；这正是"changelog 也是 id 来源"的样本——
    一条事实被手删之后，它的 id 还留在 changelog 里，永远不会被派给别人
    （SPEC §4：never resurrected）。
    """
    assert dossier.known_fact_ids() == {
        "f_001",
        "f_002",
        "f_003",
        "f_009",
        "f_017",
        "f_042",
        "f_043",
        "f_044",
        "f_051",
    }


def test_rejected_patch_ids_are_still_known(dossier: Dossier) -> None:
    """被拒绝的补丁同样占住 id：分配过就不再复用。"""
    dossier.write_changelog(
        ChangelogEntry(
            merge="placeholder",
            input="journal/2026-10-11.md",
            extractor="mock-v0",
            patches=[
                ChangelogPatch(
                    op=PatchOp.ADD,
                    id="f_900",
                    target="about/values.md",
                    kind=FactKind.STATEMENT,
                    text="一条被拒绝的事实",
                    evidence="没有逐字对应",
                    source="journal/2026-10-11.md#L7",
                    result=PatchResult.REJECTED,
                    reason="evidence is not verbatim",
                )
            ],
        ),
        now=datetime(2026, 10, 12, 9, 0),
    )
    assert "f_900" in dossier.known_fact_ids()


def test_a_wikilink_is_not_a_marker(dossier: Dossier) -> None:
    """``[[f_051]]`` 是引用不是副本（SPEC §2.4）——它不算"这条事实还在文件里"。

    这里把 career.md 里那条事实的 marker 换成纯链接文本，模拟"人删掉了事实
    却留着指向它的链接"：
    - 文件侧不再贡献 f_051（解析不出 marker）；
    - 但 id 仍然**已知**，因为 changelog 记得它（SPEC §4：退役的 id 永不复用）。
      两个来源各司其职，正是 :meth:`Dossier.known_fact_ids` 要同时扫两处的理由。
    """
    career = dossier.root / "about" / "career.md"
    text = career.read_text(encoding="utf-8")
    career.write_text(text.replace("<!-- f_051 -->", "[[f_051]]"), encoding="utf-8")

    section = _section(dossier, "career")
    document = dossier.read_fact_document(section)
    assert document is not None
    assert document.facts() == []  # 链接没有被当 marker 解析
    assert "f_051" in dossier.known_fact_ids()  # 但 id 依旧占用（changelog 来源）


# --- bootstrap（plan §7 M2 的 "bootstrap tree"）---------------------------


def test_bootstrap_builds_the_spec_section_1_tree(tmp_path: Path) -> None:
    root = tmp_path / "new-me"
    dossier = bootstrap_dossier(root)

    assert dossier.root == root
    for directory in ("about", "goals", "journal", "changelog", ".index"):
        assert (root / directory).is_dir(), directory

    # profile.yaml 是模板的逐字复制（不重新序列化：注释与格式属于人）
    assert (root / "profile.yaml").read_text(encoding="utf-8") == template_text()

    # 每个声明的分区都在：journal 是目录，其余是空文件
    for section in dossier.load_profile().sections:
        path = root / section.path
        if section.axis is Axis.JOURNAL:
            assert path.is_dir()
            assert list(path.iterdir()) == []
        else:
            assert path.is_file()
            assert path.stat().st_size == 0

    # SPEC §1："exactly one entry"
    assert (root / ".gitignore").read_text(encoding="utf-8") == ".index/\n"
    assert (root / ".git").is_dir()


def test_bootstrap_refuses_a_nonempty_directory(tmp_path: Path) -> None:
    """在别人的数据上"初始化"等于拿模板盖过去（SPEC §7 nothing is ever lost）。"""
    root = tmp_path / "me"
    root.mkdir()
    (root / "existing.md").write_text("已有内容\n", encoding="utf-8")
    with pytest.raises(ZhiwoError):
        bootstrap_dossier(root)
    assert (root / "existing.md").read_text(encoding="utf-8") == "已有内容\n"


def test_bootstrap_refuses_a_path_that_is_a_file(tmp_path: Path) -> None:
    """目标是个文件时同样不能"初始化"——报 ZhiwoError，而不是让 mkdir/iterdir 炸。"""
    target = tmp_path / "already-a-file"
    target.write_text("我不是目录\n", encoding="utf-8")
    with pytest.raises(ZhiwoError):
        bootstrap_dossier(target)
    assert target.read_text(encoding="utf-8") == "我不是目录\n"


def test_bootstrap_accepts_an_existing_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "me"
    root.mkdir()
    assert bootstrap_dossier(root).root == root
    assert (root / "profile.yaml").is_file()


def test_bootstrap_validates_the_template_before_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏模板必须在**动手之前**失败——半成品档案比报错难收拾得多。"""
    monkeypatch.setattr(store, "template_text", lambda: "sections: [\n")
    root = tmp_path / "me"
    with pytest.raises(SchemaError):
        bootstrap_dossier(root)
    assert not root.exists()


def test_bootstrapped_dossier_is_usable_immediately(tmp_path: Path) -> None:
    """建完就能用：能读 profile、能追加 journal、能写 changelog。"""
    dossier = bootstrap_dossier(tmp_path / "me")
    assert dossier.load_profile().sections != []
    assert dossier.known_fact_ids() == set()
    append = dossier.append_journal(["第一条"], now=datetime(2026, 10, 12, 10, 0))
    assert append.first_line == 3
    assert dossier.read_changelog() == []
