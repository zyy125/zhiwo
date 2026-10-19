"""共享词汇表的 pydantic 模型——**纯数据，零 I/O**（plan §4 的 models.py 行）。

分层纪律：本模块不认识文件系统、不认识路径存在性、不认识 git。
所有磁盘来源的东西（读文件、解析 YAML）都在 ``schema.py``（M1）与
``store.py``（M2）里；模型只负责"这份数据在结构上是否说得通"。
这样 M2 的 store、M4 的 merge、M5 的 extract 都能直接复用同一套词汇，
也保证了任何构造路径（文件加载、程序内构造、未来的 MCP 工具）都绕不过
下面的校验。

模型的校验只覆盖**结构**与**契约明文的硬规则**；语义judgment 一律不在
这里（例如"evidence 是否逐字来自输入"属 M4 merge，SPEC §3）。
"""

import enum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Sensitivity(enum.StrEnum):
    """分区敏感度（SPEC §5）。

    只有两档：normal / high。"清除度"（clearance）用的是同一套取值，
    但两者是不同的概念——前者是数据的属性，后者是读者的权限，
    因此下面分成两个枚举，避免"把数据的标签当权限用"这类错误。
    """

    NORMAL = "normal"
    HIGH = "high"


class Axis(enum.StrEnum):
    """分区所属的轴（SPEC §1："one level, one axis"）。

    封闭枚举而非自由字符串：轴驱动 §5 的 journal 规则，若放开成任意
    字符串，``axis: diary`` 指向 journal 目录就能绕过"journal 永不降级"
    的保护。新增一个轴属于契约变更（硬规则 7），届时应同时改 SPEC——
    在枚举里加成员是"随契约一起改"，不是绕过。
    """

    ABOUT = "about"
    GOALS = "goals"
    JOURNAL = "journal"


class Clearance(enum.StrEnum):
    """读者的权限等级（SPEC §5）。

    clearance 在 reader 边界强制（MCP server / export / ``me ask``），
    绝不交给调用方模型自行裁量；``me ask`` 与导出默认 normal。
    """

    NORMAL = "normal"
    HIGH = "high"


# 事实 id 的形态：``f_`` + 至少 3 位数字，零填充（SPEC §2.1 的 f_017；
# plan §3 决策 4 定下"zero-padded to 3"）。用 Annotated 复用，
# 避免在 id / target_id / members 上重复写同一个 pattern。
FactId = Annotated[str, Field(pattern=r"^f_\d{3,}$")]

# "在场即非空"的字符串。用于补丁的 text / evidence / source / target / reason：
# 这些字段要么缺席（None），要么必须携带真实内容。空串是"看起来填了"的缺席，
# 而 evidence: "" 永远不可能是逐字引文——把关口留在结构层，好过让它
# 变成 M4 里一条语焉不详的拒绝记录。
NonEmptyStr = Annotated[str, Field(min_length=1)]


class Section(BaseModel):
    """``profile.yaml`` 中的一个分区声明（SPEC §1 / §5）。

    ``extra="forbid"`` 是刻意的：profile 是**人手工编辑**的文件，
    而硬规则 1 说它就是 schema 本身。拼错一个键（``sensitvity``）若被
    静默忽略，后果是某个分区悄悄失去敏感度标注——在"手滑"和"数据泄漏"
    之间只隔一个静默忽略。因此未知键一律报错。

    ``path`` 可以是文件（``about/identity.md``）也可以是目录
    （journal 的 ``journal/``）。这里**不校验存在性**：L1 校验的是"声明"
    本身，mkdir/touch 是 reconcile 的活（plan §5 步骤 4）；而且
    ``examples/me/`` 本就是部分树，声明 9 个分区却只放了 4 个文件。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")  # 允许 long-term 这类连字符 id
    title: str = Field(min_length=1)
    path: str = Field(min_length=1)
    axis: Axis
    sensitivity: Sensitivity | None = None

    @model_validator(mode="after")
    def _resolve_sensitivity(self) -> Self:
        """落实 SPEC §5 的"journal 默认 high，系统永不降级"。

        规则放在模型层而不是 ``load_profile`` 里，是为了让**任何**构造
        路径都无法表达一个被降级的 journal——这是 M1 最要紧的一条校验
        （也是 M1 的两个关键拒绝测试之一）。

        - ``axis: journal`` + 省略 sensitivity → 解析为 high（SPEC 原文
          "high by default"）；
        - ``axis: journal`` + 显式给 normal → 拒绝；
        - 其余分区省略 sensitivity → 拒绝：SPEC §5 要求"每个分区都标注
          敏感度"，而"省略即 normal"是朝向泄漏方向的默认值，必须 fail loud。
        """
        if self.axis is Axis.JOURNAL:
            if self.sensitivity is None:
                # 原地写入解析结果：校验通过后 sensitivity 不再是 None。
                self.sensitivity = Sensitivity.HIGH
            elif self.sensitivity is not Sensitivity.HIGH:
                raise ValueError(
                    f"journal section {self.id!r} may not be downgraded to "
                    f"{self.sensitivity.value!r}: journal is always 'high' (SPEC §5)"
                )
        elif self.sensitivity is None:
            raise ValueError(
                f"section {self.id!r} must declare 'sensitivity' explicitly "
                "(SPEC §5: every section is marked with a level)"
            )
        return self


class Profile(BaseModel):
    """``profile.yaml`` 的整体结构（SPEC §1 / §5 / §6）。

    这是"schema 是数据"的落点：分区布局、标题、路径、敏感度全部来自
    这个文件，代码里不存在分区清单。新增一个分区是数据变更。
    """

    model_config = ConfigDict(extra="forbid")

    # SPEC §6：reader view 的 token 预算，默认 4000，声明在 profile 里。
    # gt=0：0 或负数不是"更严格的预算"，是无意义值，早点拒绝。
    # 注意这里**只存值不做闸门**：MVP 阶段超预算只警告不拦截
    # （plan §2 锁定选择 + D-002），闸门属于 distill（v0.5）。
    core_budget_tokens: int = Field(default=4000, gt=0)
    sections: list[Section] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_layout(self) -> Self:
        """分区布局的三条结构约束（均为 M1 关键测试覆盖点）。

        1. **id 唯一**——重复 id 会让"事实属于哪个分区"失去唯一答案。
        2. **path 唯一**——两个分区指向同一文件时，分区过滤（SPEC §5 的
           敏感度边界）会产生歧义，而 SPEC §1 明说"ambiguity makes files
           drift"。两条都放宽会得到"同一行事实同时属于两个分区"的怪状态。
        3. **至多一个 journal 分区**——journal 是"那条"只追加时间流
           （SPEC §1），两条时间流没有意义。
        """
        ids = [s.id for s in self.sections]
        duplicated_ids = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated_ids:
            raise ValueError(f"duplicate section id(s): {', '.join(duplicated_ids)} (SPEC §1)")

        paths = [s.path for s in self.sections]
        duplicated_paths = sorted({p for p in paths if paths.count(p) > 1})
        if duplicated_paths:
            raise ValueError(f"duplicate section path(s): {', '.join(duplicated_paths)} (SPEC §1)")

        journals = [s.id for s in self.sections if s.axis is Axis.JOURNAL]
        if len(journals) > 1:
            raise ValueError(f"at most one journal section is allowed, got {journals} (SPEC §1)")
        return self


class PatchOp(enum.StrEnum):
    """changelog 里出现过的全部操作值。

    - SPEC §3 的四种补丁：add / supersede / link / compress。
      compress 由 distill 发射、v0.5 才实现，但枚举值现在就有——
      changelog 是长期格式，读取器必须认识它。
    - SPEC §4 的两种 reconcile 事件：adopt（收编手写行）/ retire（事实
      被手删）。它们由 M3 写入，这里同样先放进词汇表：理由与 compress
      一致——既然"读取器必须认识契约里的每个 op"，就没有理由只对
      §3 的 op 生效。必填字段见 _REQUIRED_BY_OP（M3，形态记在 D-006）。
    """

    ADD = "add"
    SUPERSEDE = "supersede"
    LINK = "link"
    COMPRESS = "compress"
    ADOPT = "adopt"  # SPEC §4：手写事实被收编，行本身即 evidence
    RETIRE = "retire"  # SPEC §4：事实被手删，状态转 archived


class FactKind(enum.StrEnum):
    """事实的认知来源（SPEC §2.3）：原话支撑 vs 提取器的推断。

    inference 一律以 pending 进入档案，确认前不进 reader view。
    """

    STATEMENT = "statement"
    INFERENCE = "inference"


class FactStatus(enum.StrEnum):
    """事实的存活状态（SPEC §2.1 的逻辑记录）。

    M1 只放词汇，不算状态：状态由行内 marker 推导
    （``superseded-by`` > ``pending`` > ``active``，archived 只存在于
    changelog），而 marker 文法属 M2 的 factfile。这里先定义是为了
    M2/M6 不必回头改词汇表。
    """

    ACTIVE = "active"
    PENDING = "pending"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class PatchResult(enum.StrEnum):
    """补丁在 changelog 里的处理结果（plan §3 决策 10）。

    契约只强制记录"已应用"的补丁；记录 rejected/skipped 是超集，
    也是"证据边界真的在拦"的审计痕迹。缺省为 applied：
    ``examples/me/`` 的黄金条目里已应用的补丁没有 result 字段。
    """

    APPLIED = "applied"
    REJECTED = "rejected"
    SKIPPED = "skipped"


# 每个 op 必须携带的字段（SPEC §3 的补丁形态）。
# 用表驱动而不是一长串 if：v0.5 新增 op 是往表里加一行，不是重构。
# 注意这里只查"字段在不在"，不查语义——evidence 是否逐字来自输入是
# M4 merge 的判断（SPEC §3 "No evidence, no write" 的边界控制）。
_REQUIRED_BY_OP: dict[PatchOp, tuple[str, ...]] = {
    PatchOp.ADD: ("id", "target", "kind", "text", "evidence", "source"),
    PatchOp.SUPERSEDE: ("id", "target_id", "kind", "text", "evidence", "source"),
    PatchOp.LINK: ("from_fact", "to"),
    # compress 的权威是成员集而非 evidence：压缩文本是重写，
    # 可回溯性由成员集合保证（SPEC §3）。
    PatchOp.COMPRESS: ("id", "members", "text"),
    # SPEC §4 的两种 reconcile 事件（M3，字段形态记在 D-006）：
    #
    # adopt 的 evidence 是**行本身**（去掉行终止符）——人写下的就是原话，
    # kind 恒为 statement（SPEC §2.3：原话支撑，非推断）；target 与 add
    # 同义（分区声明路径）；source 指向收编发生的那一行（``path#L<n>``，
    # 与 add 的 journal 指针同一坐标系）。
    PatchOp.ADOPT: ("id", "target", "kind", "text", "evidence", "source"),
    # retire 记录的是对人类动作的**观察**：事实已经不在盘上，没有可以
    # 逐字引用的 evidence——可引用的是"为什么判它被手删"，即 reason。
    # result 走默认的 applied：归档（状态转 archived）在记录写入时
    # 已经生效，事实本来就不在文件里（SPEC §2.1：archived 只存在于
    # changelog，文件里没有它的写法——retire 补丁本身就是墓碑）。
    PatchOp.RETIRE: ("id", "reason"),
}


class ChangelogPatch(BaseModel):
    """changelog 里的一条补丁记录——记录的是**已应用/已拒绝之后**的形态。

    SPEC §3：写进 changelog 的是 "the patch as applied"——带着 merger 分配的
    ``id``、解析后的 ``source`` 指针与状态转移。补丁**输入**本身没有 id
    （身份在 merger 分配后才存在），所以这个模型描述的是 changelog 侧。

    ``extra="ignore"``（与 Profile 的 forbid 相反）是有意的：SPEC 前言说
    数据要比实现活得久，changelog 尤其如此。未来版本给条目加字段不应
    让旧读取器崩掉；"缺 schema_version 视为 1"（plan §3 决策 6）已经是
    同一姿态。M3 落地后契约里的 op 已全部有 _REQUIRED_BY_OP 表行
    （含 §4 的 adopt/retire，D-006），但本模型对新 op 依旧宽容——
    "读取器必须认识每个 op"约束的是**写出侧**，不是读取侧。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    op: PatchOp
    id: FactId | None = None  # add/supersede 由 merger 分配（SPEC §3）
    target: NonEmptyStr | None = None  # add 的目标文件
    target_id: FactId | None = None  # supersede 的目标事实
    kind: FactKind | None = None
    text: NonEmptyStr | None = None
    evidence: NonEmptyStr | None = None  # 逐字原文；永久保存在 changelog（SPEC §3）
    source: NonEmptyStr | None = None  # 形如 journal/2026-10-11.md#L7
    members: list[FactId] | None = Field(default=None, min_length=1)  # compress：成员集合
    # ``from`` 是 Python 关键字，字段名改用 from_fact，YAML 侧仍是 from。
    from_fact: FactId | None = Field(default=None, alias="from")
    to: FactId | None = None
    result: PatchResult | None = PatchResult.APPLIED
    reason: NonEmptyStr | None = None  # result 为 rejected/skipped 时的人类可读理由

    @model_validator(mode="after")
    def _check_required_for_op(self) -> Self:
        """按 op 检查必填字段是否缺席（表驱动，SPEC §3）。

        缺 ``evidence`` 的 add/supersede 在这里就死掉——硬规则 4
        "没有逐字 evidence 的补丁一律拒绝"的第一道闸门。

        只查"缺席"（None）是不够的：空串同样是缺席的表现，而
        ``evidence: ""`` 永远不可能是逐字引文，``members: []`` 更是一个
        没有权威来源的压缩。因此这几个字段的类型都带 ``min_length=1``
        （见 NonEmptyStr 与 members 的 Field），把关口留在结构层。

        缺行兜底按"无必填字段"处理（``.get``）：契约里的 op 今天已全部
        有表行（含 §4 的 adopt/retire），但 v2 若新增 op，旧代码遇到新
        op 时应该继续可读——写侧闸门在新代码里，不在读取器里。
        """
        required = _REQUIRED_BY_OP.get(self.op, ())
        missing = [f for f in required if getattr(self, f) is None]
        if missing:
            raise ValueError(f"op {self.op.value!r} requires {', '.join(missing)} (SPEC §3)")
        return self


class ChangelogEntry(BaseModel):
    """``changelog/*.yaml`` 一个 merge 文件（SPEC §1 / §3）。

    changelog 是只追加的审计轨迹：每次 merge 一个文件，永不改写、
    永不删除；``merge`` id 就是文件名的 stem。

    **给 M2 写盘方的两条硬性要求**（写错就会产出非契约形态的 YAML）：

    1. ``by_alias=True`` 必须打开。``from_fact`` 这个字段名只是 Python
       侧的规避（``from`` 是关键字），契约键是 ``from``（SPEC §3）。
       实测：``model_dump(mode="json")`` 会写出 ``from_fact:``，
       只有加上 ``by_alias=True`` 才写出 ``from:``。
    2. ``mode="json"``（或对枚举取 ``.value``）。默认 dump 产出枚举成员，
       YAML 里会变成 ``!!python/object`` 之类的标签。

    合起来就是写盘形态：``model_dump(mode="json", by_alias=True)``，
    再用 ``allow_unicode=True`` 交给 PyYAML（编码法，plan §4）。
    """

    model_config = ConfigDict(extra="ignore")

    merge: str = Field(min_length=1)  # 与文件名 stem 一致（SPEC §1）
    input: str = Field(min_length=1)
    extractor: str = Field(min_length=1)
    # OQ#3 的 MVP 答案（plan §3 决策 6）：写入时恒为 1，读取时缺省视为 1，
    # 在 v2 出现之前不做迁移工具。因此这里是 default 而不是 required。
    schema_version: int = 1
    # 允许为空列表：一次没有提取出任何事实的捕获，仍然要留下
    # "日志已追加"的记录（plan §5 步骤 2：capture 自 journal 落盘起即持久）。
    patches: list[ChangelogPatch] = Field(default_factory=list)
