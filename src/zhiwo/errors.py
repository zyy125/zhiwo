"""zhiwo 的异常词汇表（plan §4 的 errors.py 行）。

设计约定：

- **消息一律英文**，与标识符同一语言（AGENTS.md 语言约定）；但消息里
  内嵌的值（事实 id、路径、CJK 原文）原样保留，不转义、不音译——
  错误输出也是"plain-file transparency"的一部分：用户要能一眼认出
  是哪一条事实、哪一行出了问题。
- **层级平铺**，全部挂在 ``ZhiwoError`` 之下。M1 定义的五个类一开始就
  全部写好，即使其中三个要到 M2/M4/M5 才被 raise：词汇表就是层与层之间
  的 API，先钉住名字，好过让三个里程碑各自长出临时异常再回头收拢。
  M2 按同一理由补了三个（``MarkerError`` / ``DossierNotFound`` /
  ``GitError``）：L0 的三个失败模式在 plan §4 的五个名字里没有家。
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


class DossierNotFound(ZhiwoError):
    """L0 找不到档案目录（M2）。

    plan §3 决策 2 的发现链 ``--dossier`` flag > ``ME_HOME`` env > ``~/.me/``
    全部穷尽，或显式给出的路径本身不存在时抛出。显式路径失败**不回退**到
    环境变量或 home：用户明确指了这里，静默换一个档案去写，比直接报错危险得多
    （可能把事实写进另一个人的档案）。消息里列出试过的全部候选路径。
    """


class GitError(ZhiwoError):
    """L0 的 git 子进程非零退出（M2）。

    ``git init`` / ``git add`` / ``git commit`` 失败（含未配置 committer
    identity 这类环境问题）。store 只交原语，编排（每次 merge 一个提交）
    属 M4；这里保证的是：调用方拿到的是一个可辨认的失败，而不是
    ``CalledProcessError`` 或一串裸 stderr。消息带 stderr 原文，因为
    "为什么没提交上"的答案几乎总在那里面。
    """


class MarkerError(ZhiwoError):
    """marker 文法在**写入**侧被违反（M2，plan §8 R15）。

    触发条件：要写出的文本为空、含换行、含 ``<!--`` / ``-->``、或首尾带空白——
    这些都会让 ``- text <!-- id -->`` 这一行的文法不再可逆，必须在这里拦住。

    **与解析侧刻意不对称**：``factfile.parse_*`` 永不 raise 这个异常。
    SPEC §4 把"marker 被人手改坏"定为*预期输入*，解析的职责是把它分类成
    数据（``LineKind.DAMAGED``）交给 reconcile 去 quarantine，而不是让一次
    手滑编辑把整次 merge 炸掉。损坏的 marker 是数据；要写的坏行是 bug。
    """


class ProviderError(ZhiwoError):
    """L2/llm 调用失败（网络、鉴权、响应不可解析等）。

    注意语义边界：provider 失败**不能**阻断日志路径——SPEC 铁律 2 与
    plan §7 M5 的关键测试都要求"provider 失败时 journal 仍然提交".
    这个异常只用来告诉上层"这次提取没成"，不是让整个 capture 回滚。
    """
