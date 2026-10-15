"""L1：``profile.yaml`` 的加载与校验、敏感度边界判定（plan §4 的 schema.py 行）。

本模块是 L1 的**唯一**磁盘入口：模型（``models.py``）保持零 I/O，
YAML 解析、UTF-8 读取、异常归一都集中在这里。

两条贯穿全项目的约定在这里落地：

1. **编码法**（plan §4）：一律 ``read_text(encoding="utf-8")`` 之后再把
   ``str`` 交给 ``yaml.safe_load``。绝不把文件句柄喂给 PyYAML——
   它对句柄会退回 locale 编码，在 POSIX/C locale 的 CI 上中文会直接
    ``UnicodeDecodeError`` 或变成乱码。中文档案是本项目的常态，
   这条不是"以防万一"。
2. **fail loud**（plan §5 步骤 3）：任何读取/解析/校验失败都归一成
   ``SchemaError``，且消息里带上文件路径与违规值。上层不必区分
   "文件不存在"、"YAML 语法错"、"结构不合法"——它们都意味着同一件事：
   档案结构读不出来，不能继续往下走。
"""

from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from zhiwo.errors import SchemaError
from zhiwo.models import ChangelogEntry, Clearance, Profile, Section, Sensitivity


class _UniqueKeyLoader(yaml.SafeLoader):
    """``SafeLoader`` 的变体：映射里出现重复键时报错，而不是静默取后者。

    PyYAML 的默认行为是 last-key-wins：一个手改过的 profile 若出现两段
    ``sections:``，第一段会被悄悄丢掉——那是"nothing is ever lost"
    （SPEC §7）的微缩版违约，且极难排查。safe 子类 + 手工构造映射，
    保持 safe_load 的安全语义不变（仍然不构造任意 Python 对象）。
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> Any:
        # flatten_mapping 处理 YAML 的合并键（<<），与基类行为保持一致，
        # 否则带 << 的文件会出现"行为不一致但没人知道为什么"。
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                already_present = key in mapping
            except TypeError:  # 列表等不可哈希的键
                raise SchemaError(
                    f"unhashable mapping key at line {key_node.start_mark.line + 1}"
                ) from None
            if already_present:
                raise SchemaError(
                    f"duplicate mapping key {key!r} at line {key_node.start_mark.line + 1}"
                    " — refusing to silently drop an earlier block"
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def parse_yaml(text: str, *, source: str = "<string>") -> Mapping[str, Any]:
    """把一段 YAML 文本解析成顶层映射，失败即 ``SchemaError``。

    只接受顶层是映射的文档：profile 与 changelog 条目都是映射，
    顶层出现标量或列表说明文件已经坏了，与其让后续代码在
    ``data["sections"]`` 处抛出难以理解的 ``TypeError``，不如在这里说清楚。
    """
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except SchemaError as exc:
        # 重复键由 _UniqueKeyLoader 直接抛出；补上来源前缀，
        # 使所有 L1 失败在消息形态上一致（都以 "来源: 原因" 开头）。
        raise SchemaError(f"{source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SchemaError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        # 空文件解析出 None，同样落进这里：空 profile 是坏 profile。
        raise SchemaError(
            f"{source}: expected a YAML mapping at the top level, got {type(data).__name__}"
        )
    return data


_MISSING: Any = object()


def _format_field_error(err: Mapping[str, Any]) -> str:
    """渲染单条 pydantic 错误：``字段路径: 原因 (got 违规值)``。

    pydantic 自己不会回显违规值（例如 pattern 不匹配时只说"should match
    pattern"），可用户拿着一条 profile 报错最需要知道的恰恰是"我写的是什么"。
    因此标量输入一律附上；复杂输入（整个映射）不加，避免把档案内容
    整段喷进错误消息。
    """
    loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
    # 去掉 pydantic 给自定义校验器统一加的 "Value error, " 前缀：
    # 它对定位问题没有贡献，只让消息变长。
    reason = str(err.get("msg", "")).removeprefix("Value error, ")
    message = f"{loc}: {reason}"
    value = err.get("input", _MISSING)
    if value is not _MISSING and isinstance(value, str | int | float | bool):
        message += f" (got {value!r})"
    return message


def _as_schema_error(exc: ValidationError, source: str) -> SchemaError:
    """把 pydantic 的校验错误翻译成 SchemaError，并保留可读细节。

    pydantic 的错误文本已经是分条、带字段路径的；这里做两件事：
    前置数据来源（哪个文件），并把违规标量值带进消息。
    """
    details = "; ".join(_format_field_error(err) for err in exc.errors())
    return SchemaError(f"{source}: {details}")


def parse_profile(data: Mapping[str, Any], *, source: str = "<mapping>") -> Profile:
    """校验一个已解析的映射是不是合法 profile（纯函数，不碰磁盘）。

    ``source`` 只用于错误消息——L1 的失败必须能指认到具体文件，
    否则多档案场景下用户不知道该去改哪个 profile。
    """
    try:
        return Profile.model_validate(dict(data))
    except ValidationError as exc:
        raise _as_schema_error(exc, source) from exc


def parse_changelog_entry(data: Mapping[str, Any], *, source: str = "<mapping>") -> ChangelogEntry:
    """校验一个已解析的映射是不是合法 changelog 条目（纯函数）。

    与 profile 分开，是因为两者的宽容度刻意不同：profile 拒绝未知键
    （人手工编辑，拼错要炸），changelog 忽略未知键（长期格式，
    未来版本加字段不能让旧读取器崩掉）。
    """
    try:
        return ChangelogEntry.model_validate(dict(data))
    except ValidationError as exc:
        raise _as_schema_error(exc, source) from exc


def load_profile(path: Path) -> Profile:
    """从磁盘加载并校验 profile.yaml（L1 的主入口）。

    注意这里**不检查分区文件是否存在**：L1 校验的是声明本身，
    声明与磁盘状态的对账是 reconcile 的活（plan §5 步骤 4），
    而且 ``examples/me/`` 本就只放了部分分区文件。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot read profile {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError 不是 OSError 的子类，必须单独接住：
        # 非 UTF-8 的档案（旧编码、编辑器写坏的字节）同样属于"结构读不出来"，
        # 让上层只需要认识 SchemaError 一种失败。
        raise SchemaError(f"{path}: not valid UTF-8: {exc}") from exc
    return parse_profile(parse_yaml(text, source=str(path)), source=str(path))


def template_text() -> str:
    """读取随包分发的引导模板（``me init``，M2，会把它复制进新档案）。

    用 ``importlib.resources`` 而非 ``__file__`` 拼接路径：
    editable 安装（``uv run`` 造的）与正式 wheel 两种布局下都成立。
    """
    return (
        resources.files("zhiwo").joinpath("templates", "profile.yaml").read_text(encoding="utf-8")
    )


def can_read(clearance: Clearance, section: Section) -> bool:
    """reader 边界的分区可见性判定（SPEC §5）。

    high 分区需要 high clearance；normal 分区任何人可读。
    journal 分区因模型规则恒为 high，因此 ``can_read(NORMAL, journal)``
    为 False——§5 的"原始话语流不外泄"从这个模型规则自然落出来。

    一个容易混淆的边界，写在这里以免后续里程碑各自理解：
    **"journal 在任何 clearance 下都不进 reader view"**（plan §6）是
    recall 层的视图规则，不是本函数的规则——``can_read(HIGH, journal)``
    为 True，因为 journal 回想本来就是一项独立的、需要高权限的能力。
    """
    if clearance is Clearance.HIGH:
        return True
    # 默认拒绝：normal 读者只拿得到**显式标注为 normal** 的分区。
    # 写成"非 normal 即需高权限"而不是"是 high 才需高权限"，是为了让
    # 判定偏向失败安全：万一有构造路径绕过了模型校验（sensitivity 为
    # None），结果落在"读不到"一侧，而不是落在泄漏一侧。
    return section.sensitivity is Sensitivity.NORMAL
