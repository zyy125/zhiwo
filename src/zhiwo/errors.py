"""zhiwo 的异常词汇表（plan §4 的 errors.py 行）。

设计约定：

- **消息一律英文**，与标识符同一语言（CLAUDE.md 语言约定）；但消息里
  内嵌的值（事实 id、路径、CJK 原文）原样保留，不转义、不音译——
  错误输出也是"plain-file transparency"的一部分：用户要能一眼认出
  是哪一条事实、哪一行出了问题。
- **层级平铺**，全部挂在 ``ZhiwoError`` 之下。五个类在 M1 就全部定义，
  即使其中三个要到 M2/M4/M5 才被 raise：词汇表就是层与层之间的 API，
  先钉住名字，好过让三个里程碑各自长出临时异常再回头收拢。
- 只带 message，不引入 ``context`` 字典之类的机器，等有调用方真的需要
  结构化字段再说（YAGNI；每个字段都是要长期维护的契约面）。
"""


class ZhiwoError(Exception):
    """所有 zhiwo 异常的基类。

    调用方若要"捕获一切 zhiwo 自己的失败"，捕获这一个即可；
    ``OSError``、``pydantic.ValidationError`` 等外部异常在层边界处
    被转换成下面这些具体类型，不让实现细节泄漏到上层。
    """


class SchemaError(ZhiwoError):
    """L1 校验失败：profile.yaml / changelog 读取、解析或结构不合法。

    对应 SPEC §1"schema 是数据"的失败面：分区声明读不出来，系统就不该
    继续往下走——fail loud（plan §5 步骤 3 的"profile.yaml（fail loud）"）。
    消息必须带上出错的路径与违规值。
    """


class MergeRefused(ZhiwoError):
    """L3 拒绝整次 merge。

    目前唯一来源是 SPEC §4 的"同一 id 出现在两处"：绝不静默挑一个，
    而是拒绝整次 merge 并报告两处位置——半合并的状态比不合并更危险。
    """


class LockHeld(ZhiwoError):
    """L0 拿不到单写者锁：``fcntl.flock`` 竞争失败。

    plan §3 决策 3：fail fast，不做排队重试。并发写入会让
    reconcile/merge 的读-改-写窗口重叠，宁可让第二次调用立刻失败。
    """


class ProviderError(ZhiwoError):
    """L2/llm 调用失败（网络、鉴权、响应不可解析等）。

    注意语义边界：provider 失败**不能**阻断日志路径——SPEC 铁律 2 与
    plan §7 M5 的关键测试都要求"provider 失败时 journal 仍然提交".
    这个异常只用来告诉上层"这次提取没成"，不是让整个 capture 回滚。
    """
