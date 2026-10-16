"""L0：存储层——**唯一知道数据住在哪**的模块（plan §4 的 store.py 行）。

分层的意义在这里最具体：M2 之后所有碰磁盘的代码都从这里走，
``factfile`` 只认字符串，``models`` 连路径都不认识。换一个存储后端
（比如给档案加一层加密容器）只会动这一个文件。

指针一律**自 profile 而来**（硬规则 1：schema 是数据）。分区清单、分区
文件路径、journal 目录位置全部读 ``profile.yaml``；store 只认死契约里
写死的三样东西——``profile.yaml`` 这个文件名、``changelog/`` 与
``.index/`` 两个目录名（SPEC §1 的布局），以及 ``.index/`` 那把锁。

崩溃语义（plan §5）：**没有事务机制**。原子性靠"写临时文件 + rename"，
持久性靠 fsync，恢复靠 git。特别地，journal 的追加自落盘那一刻起就是
持久的（plan §5 步骤 2 的"durable from the moment it hits disk"）——
后面的任何失败都不该回滚它，这正是"capture 永不阻塞"（铁律 2）的
实现基础，也是为什么这里对它用 O_APPEND + fsync，而不是整文件重写。

平台：单写者锁用 ``fcntl.flock``，**仅 POSIX**（plan §3 决策 3）。
Windows 端口是 post-MVP 议题；在那之前，import 失败本身就是最好的
"这个平台还不支持"。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from zhiwo.errors import DossierNotFound, GitError, LockHeld, SchemaError, ZhiwoError
from zhiwo.factfile import FactDocument
from zhiwo.models import Axis, ChangelogEntry, Profile, Section
from zhiwo.schema import (
    load_profile,
    parse_changelog_entry,
    parse_profile,
    parse_yaml,
    template_text,
)

# 契约里写死的名字（SPEC §1 的布局图）。分区相关的一切仍然来自 profile；
# 这里只认"档案根目录下必须叫这个名字"的那几样。
DIR_NAME = ".me"
ME_HOME_ENV = "ME_HOME"
PROFILE_FILENAME = "profile.yaml"
CHANGELOG_DIR = "changelog"
INDEX_DIR = ".index"
LOCK_FILENAME = "lock"
GITIGNORE = ".gitignore"


@dataclass(frozen=True)
class JournalAppend:
    """一次 journal 追加的结果（plan §5 步骤 2）。

    ``first_line`` / ``last_line`` 是**追加后**文件里的绝对 1-based 行号，
    与 ``#L<n>`` 指针同一坐标系（SPEC §2.3 的
    ``source: journal/2026-10-11.md#L7``）。M4 拿它去限定"证据必须落在
    这次捕获的行范围内"，M6 拿它做 provenance 回指——两者都必须用的是
    "落盘之后"的行号，所以由 store 算好交出去，而不是让调用方自己数。
    """

    path: Path
    first_line: int
    last_line: int


class Dossier:
    """一个档案目录（默认 ``~/.me/``）及其上的全部文件操作。

    实例只持有 ``root``；profile 每次现读（``load_profile``）而不缓存。
    这是刻意的：CLI 的每次调用都是短命进程，缓存没有收益；而 MCP server
    是长命进程，一旦缓存，用户用编辑器改了 profile 之后，进程里的分区
    布局就会与磁盘不一致——"schema 是数据"要求数据改了立刻生效。
    """

    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise DossierNotFound(f"dossier directory does not exist: {root}")
        self.root = root

    @classmethod
    def discover(cls, explicit: Path | None = None) -> Dossier:
        """按 ``--dossier`` > ``ME_HOME`` > ``~/.me/`` 的顺序定位档案（plan §3 决策 2）。

        两条规则：

        - **显式路径失败时绝不回退**。用户明确指了一个目录，那里没有档案，
          正确反应是报错；悄悄换到 ``~/.me/`` 去写，可能把事实写进另一个
          人的档案——那是"数据去哪了"的最坏版本。
        - ``ME_HOME`` 的存在不只是方便：它让测试完全自足（每个测试指向
          自己的临时目录），也让"一个机器上多个档案"成为可能。

        ``Path.home()`` 在**调用时**求值，不在 import 时——测试用
        ``monkeypatch.setenv("HOME", …)`` 换掉家目录，模块级的常量会
        在 import 那一刻就被冻住，测不到。
        """
        if explicit is not None:
            if not explicit.is_dir():
                raise DossierNotFound(
                    f"--dossier path does not exist: {explicit} (no fallback: "
                    "an explicit target that is missing is an error, not a hint)"
                )
            return cls(explicit)

        candidates: list[tuple[str, Path]] = []
        env_home = os.environ.get(ME_HOME_ENV, "").strip()
        if env_home:
            candidates.append((ME_HOME_ENV, Path(env_home).expanduser()))
        candidates.append(("~/.me", Path.home() / DIR_NAME))

        for _label, path in candidates:
            if path.is_dir():
                return cls(path)
        tried = ", ".join(f"{label} ({path})" for label, path in candidates)
        raise DossierNotFound(f"no dossier found; tried: {tried}")

    # --- 路径：一切自负 profile 而来 ------------------------------------

    @property
    def profile_path(self) -> Path:
        return self.root / PROFILE_FILENAME

    @property
    def changelog_dir(self) -> Path:
        return self.root / CHANGELOG_DIR

    @property
    def index_dir(self) -> Path:
        return self.root / INDEX_DIR

    @property
    def lock_path(self) -> Path:
        return self.index_dir / LOCK_FILENAME

    def section_path(self, section: Section) -> Path:
        """分区在磁盘上的位置。**永远从 profile 里取**（硬规则 1）。

        ``path`` 可以是文件也可以是目录（journal 的 ``journal/``，SPEC §1）；
        这里不做区分，也不校验存在性——前者是调用方的事，后者是 M3
        reconcile 的事。
        """
        return self.root / section.path

    def load_profile(self) -> Profile:
        """读并校验 profile.yaml。fail loud（plan §5 步骤 3）：坏了就停。"""
        return load_profile(self.profile_path)

    def journal_dir(self) -> Path:
        """journal 分区对应的目录（SPEC §1 的轴 3）。

        profile 里没有 journal 分区时抛错：契约的布局里 journal 是存在的，
        而在一个没有时间流的档案上"追加一条捕获"没有意义。
        """
        for section in self.load_profile().sections:
            if section.axis is Axis.JOURNAL:
                return self.section_path(section)
        raise ZhiwoError("profile declares no journal section (SPEC §1)")

    # --- 事实文件 -------------------------------------------------------

    def read_fact_document(self, section: Section) -> FactDocument | None:
        """读一个分区文件；**文件不存在时返回 None**。

        返回 None 而不是抛错，是因为"声明了分区但文件还没建"是正常状态：
        ``examples/me/`` 自己就是部分树，而补建是 merge 前 reconcile 的活
        （plan §5 步骤 4）。返回 None 让调用方（M3）能区分"空文件"与
        "还没建"，两者的处理不同。

        读取失败归一成 ``SchemaError``，与 ``schema.load_profile`` 同一姿态：
        上层只需要认识一种"档案读不出来"。
        """
        path = self.section_path(section)
        if not path.is_file():
            return None
        return FactDocument.parse(_read_text(path), source=str(path))

    def write_fact_document(self, section: Section, document: FactDocument) -> None:
        """整份写回（原子）。merge 改的是内存里的 ``FactDocument``，
        落盘一律走这里——没有"就地改一行"的写入路径，round-trip 定律
        （plan §4）才有一个可审计的出口。"""
        atomic_write(self.section_path(section), document.render())

    # --- journal --------------------------------------------------------

    def append_journal(self, lines: Sequence[str], *, now: datetime | None = None) -> JournalAppend:
        """把若干行追加到当日 journal 文件（plan §5 步骤 2）。

        契约要点：

        - **只追加，永不改写**（SPEC §1）。因此用 ``O_APPEND`` 打开、
          只写新字节；既有的字节一个都不动，历史行号也不会漂。
        - **自落盘起持久**：立刻 fsync。这是"capture 永不阻塞"（铁律 2）
          与崩溃恢复（plan §5："never roll back journal/"）共同的支点——
          后半程提取失败、merge 失败都无所谓，这一行已经在了。
        - 文件按 **本地日期** 命名（``journal/YYYY-MM-DD.md``），日期取
          ``now`` 的日期部分；``now`` 可注入，测试才不必跟真实时钟较劲。
        - 新建的当日文件带一行 ``# YYYY-MM-DD`` 标题（与黄金档案的形态一致，
          但**不写**那些说明性 HTML 注释——那是给人看的文档，不是格式）。

        空调用是调用方的 bug：一次"什么都没记"的捕获没有任何意义，
        而不报错的话它会给你建一个只有标题的当日文件。
        """
        if not lines:
            raise ZhiwoError("append_journal needs at least one line")

        moment = now if now is not None else datetime.now().astimezone()
        day = moment.date().isoformat()
        path = self.journal_dir() / f"{day}.md"

        body = "".join(f"- {_journal_line(line)}\n" for line in lines)

        if path.exists() and path.stat().st_size > 0:
            existing = _read_text(path)
            if not existing.endswith("\n"):
                # 手删掉了文末换行：补一个终止符，否则新行会黏在旧行尾巴上。
                # 补的只是"有没有换行"，旧行内容一字未动。
                body = "\n" + body
            first_line = len(existing.splitlines()) + 1
        else:
            header = f"# {day}\n\n"
            body = header + body
            first_line = len(header.splitlines()) + 1

        path.parent.mkdir(parents=True, exist_ok=True)
        # 直接写字节而不是套一层文本文件对象：这条路径的契约就是"只追加、
        # 立刻持久"，用 os.write + fsync 是它最短、最没有歧义的表达
        # （也顺手把编码握在自己手里，见 plan §4 编码法）。
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

        return JournalAppend(
            path=path,
            first_line=first_line,
            last_line=first_line + len(lines) - 1,
        )

    # --- changelog ------------------------------------------------------

    def read_changelog(self) -> list[ChangelogEntry]:
        """读全部 changelog 条目，按文件名排序（= 时间序，SPEC §1）。

        读取器**接受任意 stem 精度**（plan §8 R5）：写入用秒，但黄金示例是
        分钟精度，读者的宽容度不该小于写入者。缺目录 → 空列表（一个还没
        发生过 merge 的档案是合法的）。
        """
        if not self.changelog_dir.is_dir():
            return []
        entries: list[ChangelogEntry] = []
        for path in sorted(self.changelog_dir.glob("*.yaml")):
            source = str(path)
            entries.append(
                parse_changelog_entry(parse_yaml(_read_text(path), source=source), source=source)
            )
        return entries

    def write_changelog(self, entry: ChangelogEntry, *, now: datetime | None = None) -> Path:
        """写一个 changelog 条目，返回落盘路径（plan §5 步骤 9）。

        文件名 ``YYYY-MM-DDThhmmss.yaml``，重名则加 ``-2``、``-3``…
        （SPEC §1："a merge never overwrites a changelog entry"）。
        秒精度来自 plan §3 决策 8：一分钟内可以完成多次 merge（一次捕获
        紧跟一次确认），分钟精度会撞名——而撞名的处置必须是**加后缀**，
        不是覆盖。

        条目里的 ``merge`` 字段被改写成文件名 stem：SPEC §1 规定二者一致，
        这比"要求调用方自己填对"可靠。

        写盘形态由 ``models.ChangelogEntry`` 的 docstring 钉死：
        ``model_dump(mode="json", by_alias=True)`` + ``allow_unicode=True``
        （编码法，plan §4）。``sort_keys=False`` 让字段顺序跟随模型定义，
        diff 才稳定可读；``schema_version`` 由模型默认值写出（决策 6）。

        调用方应先持 :meth:`writer_lock`：重名扫描 + 写入之间有一个窗口，
        锁外并发会让两个进程选中同一个文件名。
        """
        moment = now if now is not None else datetime.now().astimezone()
        path = self._free_changelog_path(moment.strftime("%Y-%m-%dT%H%M%S"))
        stamped = entry.model_copy(update={"merge": path.stem})
        payload = stamped.model_dump(mode="json", by_alias=True)
        atomic_write(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
        return path

    def _free_changelog_path(self, stem: str) -> Path:
        candidate = self.changelog_dir / f"{stem}.yaml"
        suffix = 2
        while candidate.exists():
            candidate = self.changelog_dir / f"{stem}-{suffix}.yaml"
            suffix += 1
        return candidate

    def known_fact_ids(self) -> set[str]:
        """档案里"已经用过"的全部事实 id（plan §3 决策 4 的使能者）。

        = 事实文件里现存的 id ∪ changelog 里**出现过的**全部 id
        （含 ``rejected`` / ``skipped`` 记录）。两个来源缺一不可：

        - 只有文件里的 id 不够：一条被退役的事实已经不在文件里，
          但它的 id 不能派给别人——SPEC §4 的"never resurrected"。
        - 被拒绝的补丁同样记账：id 是 merger 分配的，一旦分配过就不再复用，
          否则 `me why f_051` 的历史会指向两条不同的事实。

        **这是唯一一处全量扫描**（plan §3 决策 1）。将来的索引实现要替换的
        就是这一个函数体，调用方不必知道自己拿的是实时扫描还是缓存。

        journal 分区被跳过：journal 是只追加的原始文本，里面没有 marker
        （SPEC §1），扫描它只是浪费 I/O。
        """
        ids: set[str] = set()
        for section in self.load_profile().sections:
            if section.axis is Axis.JOURNAL:
                continue
            document = self.read_fact_document(section)
            if document is None:
                continue
            for line in document.facts():
                assert line.marker is not None  # facts() 的不变量，见 factfile
                ids.add(line.marker.fact_id)
        for entry in self.read_changelog():
            for patch in entry.patches:
                if patch.id is not None:
                    ids.add(patch.id)
        return ids

    # --- 单写者锁（plan §3 决策 3）--------------------------------------

    @contextlib.contextmanager
    def writer_lock(self) -> Iterator[None]:
        """整个"读-改-写"窗口的单写者锁：``flock(LOCK_EX|LOCK_NB)``。

        plan §3 决策 3 / SPEC 决议 3：一个人的机器上，CLI 与 MCP server
        同时跑是全部并发来源，一把文件锁足够；**失败即报错，不排队不重试**
        （:class:`~zhiwo.errors.LockHeld`）。理由：并发写入会让 reconcile/
        merge 的"读 → 判断 → 写"窗口重叠，晚到的那个是基于过期快照做的
        判断——排队执行一个过期判断，比让调用方立刻失败重来更危险。

        flock 是**按打开文件描述**（OFD）生效的，所以同一进程里开两个 fd
        也会真的互斥——这正是测试能在一个进程内验证竞争的原因。

        锁文件放在 ``.index/`` 里（SPEC §1）：那是档案里唯一的派生物，
        丢了不损失任何东西，也不会污染 git（``.gitignore`` 只有它一条）。
        """
        self.index_dir.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockHeld(
                    f"another zhiwo writer is working in {self.root} "
                    f"(lock: {self.lock_path}); refusing to queue (plan §3 decision 3)"
                ) from exc
            yield
        finally:
            # 关掉 fd 就会释放锁；显式 LOCK_UN 在异常路径上反而可能掩盖
            # 真正的错误，所以只关不开。
            os.close(handle)

    # --- git ------------------------------------------------------------

    def git_commit(self, message: str) -> str:
        """把工作区全部改动提交为一个提交，返回短 sha（plan §5 步骤 11）。

        "每次 merge 一个提交"（SPEC §7：Nothing is ever lost）由 M4 编排，
        这里只提供可靠的原语：``add -A`` + ``commit``，失败抛
        :class:`~zhiwo.errors.GitError` 并带上 stderr。

        不伪造作者身份：git 的身份配置是用户的，测试自己用
        ``GIT_AUTHOR_*`` 环境变量解决，生产代码不越界去猜。
        """
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", message)
        return _git(self.root, "rev-parse", "--short=12", "HEAD").stdout.strip()


def atomic_write(path: Path, text: str) -> None:
    """**原子地**把 ``text`` 写到 ``path``（UTF-8）。

    做法是 POSIX 的老配方：同目录建临时文件 → 写 → fsync → ``os.replace``
    （同一文件系统内的 rename，原子）→ fsync 父目录。

    为什么非要这么啰嗦：

    - 同目录：``os.replace`` 跨文件系统会退化成分步复制，那就不原子了。
    - fsync 文件：否则崩溃时可能留下一个"名字对了但内容是空的"文件——
      rename 只保证目录项原子，不保证数据落盘。
    - fsync 父目录：否则 rename 本身可能还在缓存里。这一条在若干文件系统
      上不被支持，所以是 best-effort 而不是硬失败。

    ``newline=""`` 不是花样：文本模式的默认换行转换会在写出时改字节，
    而这里的契约是"写出去的字节就是我给的那些"（编码法 + CRLF 保真）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    _fsync_directory(path.parent)


def git_init(root: Path) -> None:
    """在 ``root`` 里 ``git init``（plan §5 的版本控制底座）。

    刻意**不做初始提交**：第一个提交由 M4 的第一次 merge 产生，
    这样"每次 merge 一个提交"从第一个提交起就成立，历史里也不会多出一个
    "脚手架"提交去解释。"me init 之后 me diff 没有 HEAD"由 CLI（M7）
    决定怎么提示。
    """
    _git(root, "init", "-q")


def bootstrap_dossier(root: Path) -> Dossier:
    """按 SPEC §1 的布局建一个新档案，返回它的 :class:`Dossier`。

    顺序是刻意的，**先验证后动手**：

    1. 目标目录必须不存在或为空。在别人的数据上"初始化"等于拿一个模板
       覆盖过去——SPEC §7 的 nothing-is-ever-lost 不允许这件事有任何
       灰色地带。
    2. **先**把模板解析成一个合法 ``Profile``。模板是随包分发的数据，
       但它同样是会被改的（加分区是数据变更）——坏模板必须在创建
       任何东西**之前**失败，否则用户得到的是一个半成品档案，
       而且报错发生在写到一半的时候。
    3. ``profile.yaml`` 是模板的**逐字复制**，不是"解析后重新序列化"
       （D-003 的后果）：重新序列化会重排注释、丢失格式，而这份文件是
       人天天要看要改的。
    4. 分区文件按轴分两种：journal 是**目录**（SPEC §1 的
       ``journal/``），about/goals 是**空文件**——内容永远是人的，
       我们不预置标题去假装知道该写什么。
    5. ``changelog/``、``.index/``、``.gitignore``（内容恰为
       ``.index/``，SPEC §1："exactly one entry"）是契约固定的布局，
       不是 schema 数据，因此 store 可以直接知道它们。
    """
    if root.exists() and not root.is_dir():
        raise ZhiwoError(f"bootstrap target exists and is not a directory: {root}")
    if root.exists() and any(root.iterdir()):
        raise ZhiwoError(f"refusing to bootstrap over a non-empty directory: {root}")

    template = template_text()
    profile = parse_profile(parse_yaml(template, source="<template>"), source="<template>")

    root.mkdir(parents=True, exist_ok=True)
    (root / PROFILE_FILENAME).write_text(template, encoding="utf-8", newline="")

    for section in profile.sections:
        path = root / section.path
        if section.axis is Axis.JOURNAL:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    (root / CHANGELOG_DIR).mkdir(parents=True, exist_ok=True)
    (root / INDEX_DIR).mkdir(parents=True, exist_ok=True)
    (root / GITIGNORE).write_text(f"{INDEX_DIR}/\n", encoding="utf-8", newline="")

    git_init(root)
    return Dossier(root)


# --- 内部工具 -------------------------------------------------------------


def _read_text(path: Path) -> str:
    """读 UTF-8 文本，失败归一成 ``SchemaError``（与 L1 同一姿态）。

    ``UnicodeDecodeError`` 单独接住：它不是 ``OSError`` 的子类，
    而"用别的编码存过的档案"和"文件读不到"对上层是同一件事——
    都意味着这份数据现在读不出来。
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{path}: not valid UTF-8: {exc}") from exc


def _journal_line(line: str) -> str:
    """校验并规整一条 journal 行。

    journal 的一行 = 一次捕获（plan §5 步骤 2），所以必须是单行非空：
    多行会让 ``#L<n>`` 指针与"一次捕获"的对应关系变得含糊，而证据校验
    （M4）与 provenance（M6）都建立在这个对应关系上。调用方若要写多行，
    拆成多次调用——那是它的判断，不是这里替它猜的。
    """
    stripped = line.strip()
    if not stripped:
        raise ZhiwoError("journal line must not be empty")
    if "\n" in line or "\r" in line:
        raise ZhiwoError(f"journal line must be a single line: {line!r}")
    return stripped


def _fsync_directory(directory: Path) -> None:
    """尽力 fsync 目录项（best-effort：部分文件系统不支持对目录 fsync）。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """跑一条 git 子进程命令；非零退出抛 ``GitError``（带 stderr）。

    统一在这里做三件事：``-C`` 指定档案目录（不依赖进程 cwd）、
    捕获输出、把 stderr 原文带进异常——"为什么没提交上"的答案几乎总在
    那里面（缺 committer identity、文件被锁、hook 失败）。
    """
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        # git 的"为什么失败"并不总在 stderr：``commit`` 在无事可做时把
        # "nothing to commit" 写在 **stdout** 上。两手都要，取有的那个——
        # 一条只有退出码的 GitError 对调用方等于没有信息。
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitError(
            f"git {' '.join(args)} failed in {root} (exit {result.returncode}): {detail}"
        )
    return result
