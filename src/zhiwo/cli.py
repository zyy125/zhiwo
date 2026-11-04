"""L6：``me`` 命令行——只做参数解析与渲染，编排全部委托 ``pipeline``。

plan §4 对 CLI 的定位很窄：把 argv 变成 pipeline 调用，再把纯数据结果
变成人能读的文字。命令里出现的分支只应回答"打印什么"，不应回答"先写
哪个文件"或"锁该什么时候拿"——后者一旦散落到命令层，MCP server（v0.3）
就得再实现一遍，而且两套顺序迟早漂移。

M7 落地的五个入口：

- ``me "<text>"`` —— 无子命令时的捕获回退（click 的 ``resolve_command``
  扩展点，plan §2 锁定选择）；
- ``me init`` —— 按 SPEC §1 建档案；
- ``me show [f_id|section] [--conflicts] [--json]`` —— 逻辑记录与待处理项；
- ``me why <f_id>`` —— 经 changelog 的溯源；
- ``me diff [git-args]`` —— 工作区 vs HEAD。

M8 补齐另外三个入口：``me review``（逐条确认/编辑/拒绝 pending 推断）、
``me export``（reader view 导出）与 ``me ask``（把 reader view 注入模型
回答问题）。它们与捕获一样只做渲染；锁、reconcile、写盘与提交仍在
``pipeline`` / ``merge`` 里。

M9 不新增命令，只把 provider 解析链延长为"注入 > ``--replay`` >
ME_LLM_* 环境配置 > journal-only 降级"。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from wcwidth import wcswidth

from zhiwo import __version__
from zhiwo.errors import ZhiwoError
from zhiwo.llm import Provider, ReplayProvider, provider_from_env
from zhiwo.merge import MergeResult, ReviewAction, ReviewOutcome
from zhiwo.models import ChangelogPatch, Clearance, PatchOp
from zhiwo.pipeline import (
    CaptureOutcome,
    ConflictPreview,
    PendingReview,
    ShowSelection,
    WhyResult,
    answer_question,
    build_ask_context,
    capture,
    diff,
    explain,
    export_reader,
    init_dossier,
    pending_reviews,
    preview_conflicts,
    review_pending,
    select_records,
)
from zhiwo.recall import FactRecord, ProvenanceEvent
from zhiwo.store import Dossier, atomic_write, default_dossier_root


@dataclass(slots=True)
class AppContext:
    """一次 CLI 调用的共享状态（dossier 位置、provider、测试用时钟）。

    ``provider_explicit`` 区分"没有配置 provider"与"明确选择不提取"：
    前者应落到降级提示，后者（测试注入 ``None``）同样降级，但不再尝试
    ``--replay``。测试通过 ``CliRunner.invoke(..., obj=AppContext(...))``
    注入 provider，生产路径则按 ``--replay`` 或 ME_LLM_* 环境变量构造
    （M9）。
    """

    dossier: Path | None = None
    replay: Path | None = None
    provider: Provider | None = None
    provider_explicit: bool = False
    now: datetime | None = None


class ZhiwoGroup(click.Group):
    """带捕获回退的 click 组：未知的第一个位置参数按捕获文本处理。

    ``me "今天很累"`` 的核心不是语法糖，而是铁律 2 的交互面：记录不应
    要求用户先学会一个命令名。click 文档把 ``resolve_command`` 定为
    这个需求的正规扩展点（plan §2 因此选 click 而非 typer）。

    只改写"第一个参数不是选项、也不是已知命令"的情况：

    - ``--help`` / ``--version`` / ``--dossier`` 走正常解析；
    - ``show`` / ``review`` / ``export`` / ``ask`` 等已知命令走正常分发；
    - 其余整段 argv 交给隐藏的 ``capture`` 命令，多词自动拼成一句话。
    """

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            return "capture", self.commands["capture"], args
        return super().resolve_command(ctx, args)


@click.group(cls=ZhiwoGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--dossier",
    "dossier_path",
    type=click.Path(path_type=Path),
    default=None,
    metavar="PATH",
    help="Dossier directory (default: $ME_HOME or ~/.me).",
)
@click.option(
    "--replay",
    "replay_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    metavar="FILE",
    help="Replay recorded LLM responses from a JSONL file (offline provider).",
)
@click.version_option(version=__version__, prog_name="me")
@click.pass_context
def cli(
    ctx: click.Context,
    dossier_path: Path | None,
    replay_path: Path | None,
) -> None:
    """Zhiwo (知我) — a local-first personal dossier.

    Capture with `me "<text>"`; run `me <command> --help` for the rest.
    """
    if ctx.obj is None:
        ctx.obj = AppContext()
    if not isinstance(ctx.obj, AppContext):
        raise click.UsageError("CLI context must be an AppContext")
    if dossier_path is not None:
        ctx.obj.dossier = dossier_path
    if replay_path is not None:
        ctx.obj.replay = replay_path


@cli.command()
@click.pass_obj
def init(app: AppContext) -> None:
    """Bootstrap a dossier at --dossier, $ME_HOME, or ~/.me (SPEC §1)."""
    target = app.dossier if app.dossier is not None else default_dossier_root()
    try:
        dossier = init_dossier(target)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Initialised dossier at {dossier.root}")
    click.echo('Capture your first fact with: me "..."')


# 捕获命令的公开形态是 `me "<text>"`；子命令形式（`me capture ...`）只是
# 回退机制的落点，因此名字取 capture、对 help 隐藏。
@cli.command(name="capture", hidden=True)
@click.argument("text", nargs=-1, required=True)
@click.pass_obj
def capture_command(app: AppContext, text: Sequence[str]) -> None:
    """Capture free text into the journal and merge extracted facts."""
    joined = " ".join(text)
    try:
        dossier = _dossier(app)
        outcome = capture(
            dossier,
            joined,
            provider=_provider(app),
            now=app.now,
        )
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_capture(outcome)


@cli.command()
@click.argument("selector", required=False)
@click.option(
    "--conflicts",
    is_flag=True,
    help="Report reconcile conflicts, quarantined lines, and marker warnings.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_obj
def show(app: AppContext, selector: str | None, conflicts: bool, as_json: bool) -> None:
    """Show the owner's facts or the §2.1 logical record.

    SELECTOR is a fact id (`f_` prefix) or a section id. With --conflicts,
    the same read-only scan that runs before a merge reports what needs
    attention; nothing is written.
    """
    try:
        dossier = _dossier(app)
        if conflicts:
            preview = preview_conflicts(dossier, selector)
            _render_conflicts(preview, as_json=as_json)
            return
        selection = select_records(dossier, selector)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _echo_json([_record_payload(record) for record in selection.records])
    else:
        _render_records(selection)


@cli.command()
@click.argument("fact_id")
@click.pass_obj
def why(app: AppContext, fact_id: str) -> None:
    """Explain where a fact came from (evidence, journal line, op chain)."""
    try:
        result = explain(_dossier(app), fact_id)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_why(result)


@cli.command()
@click.pass_obj
def review(app: AppContext) -> None:
    """Confirm, edit, or reject pending facts (SPEC §2.3).

    One keystroke per fact: c=confirm, e=edit, r=reject, s=skip, q=quit.
    A deferred supersede is applied only if its target is still active and
    unlocked; otherwise you are offered a standalone confirmation.
    """
    try:
        dossier = _dossier(app)
        items = pending_reviews(dossier)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc

    if not items:
        click.echo("no pending facts")
        return

    _render_review_list(items)
    try:
        for item in items:
            try:
                if not _review_one(dossier, app, item):
                    break
            except ZhiwoError as exc:
                # 列表是快照；锁内重新读取后事实可能已不再 pending。报错并
                # 继续下一条，比让整次 review 崩掉更接近"人一条条裁决"的
                # 交互模型，也不会留下未解释的半状态。
                click.echo(f"warning: {exc}", err=True)
    except (click.Abort, EOFError):
        click.echo("\nreview stopped; remaining facts stay pending", err=True)

    remaining = pending_reviews(dossier)
    if remaining:
        click.echo(f"{len(remaining)} fact(s) still pending")
    else:
        click.echo("no pending facts left")


@cli.command(name="export")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    metavar="FILE",
    help="Write the reader view to FILE instead of stdout.",
)
@click.option(
    "--clearance",
    type=click.Choice([item.value for item in Clearance]),
    default=Clearance.NORMAL.value,
    show_default=True,
    help="Reader clearance; high includes high-sensitivity sections (never the journal).",
)
@click.pass_obj
def export_command(app: AppContext, output: Path | None, clearance: str) -> None:
    """Export the reader view as GFM Markdown (SPEC §5)."""
    try:
        text = export_reader(_dossier(app), clearance=Clearance(clearance))
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc

    if output is None:
        click.echo(text, nl=False)
        return
    try:
        atomic_write(output, text)
    except OSError as exc:
        raise click.ClickException(f"cannot write {output}: {exc}") from exc


@cli.command(name="ask")
@click.argument("question", nargs=-1, required=True)
@click.option(
    "--clearance",
    type=click.Choice([item.value for item in Clearance]),
    default=Clearance.NORMAL.value,
    show_default=True,
    help="Reader clearance; high also injects journal keyword hits.",
)
@click.option(
    "--show-context",
    is_flag=True,
    help="Print the exact context blocks injected into the model (stderr).",
)
@click.pass_obj
def ask_command(
    app: AppContext, question: Sequence[str], clearance: str, show_context: bool
) -> None:
    """Ask a question with the reader view injected (SPEC §5 / §6)."""
    try:
        dossier = _dossier(app)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc

    provider = _require_provider(app)
    try:
        context = build_ask_context(
            dossier,
            " ".join(question),
            clearance=Clearance(clearance),
        )
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc

    if show_context:
        click.echo(context.render(), err=True)
        click.echo("", err=True)

    try:
        result = answer_question(context, provider)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(result.answer)


@cli.command(
    name="diff",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.argument("git_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def diff_command(app: AppContext, git_args: Sequence[str]) -> None:
    """Show working-tree changes against HEAD (hand edits awaiting reconcile).

    GIT_ARGS are passed through to `git diff`, e.g. `me diff --stat`.
    """
    try:
        result = diff(_dossier(app), git_args)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc

    if not result.has_head:
        click.echo(
            "no commits yet — the first merge creates one; showing `git status --short`:",
            err=True,
        )
    if result.text:
        click.echo(result.text, nl=False)


# --- 上下文解析 -------------------------------------------------------------


def _dossier(app: AppContext) -> Dossier:
    """按 plan §3 决策 2 的发现链定位档案（``--dossier`` > ME_HOME > ~/.me）。"""
    return Dossier.discover(app.dossier)


def _provider(app: AppContext) -> Provider | None:
    """解析 provider：测试注入 > ``--replay`` > ME_LLM_* > 无降级。

    显式注入与回放优先于环境配置，保证测试和离线演示不被开发机上的
    真实端点意外接管。真实 provider 的构造错误（如只设了 base_url 却
    没有 model）是 ``ZhiwoError``：用户显然在尝试配置，静默当成"没配"
    会让一次环境变量 typo 永远不被发现。没有配置不是错误——capture 仍会
    提交 journal，并在摘要里明确告诉用户"这次没有提取"。
    """
    if app.provider_explicit:
        return app.provider
    if app.replay is not None:
        return ReplayProvider(app.replay)
    return provider_from_env()


def _require_provider(app: AppContext) -> Provider:
    """``me ask`` 必须有 provider：没有答案时不能伪装成成功。"""
    try:
        provider = _provider(app)
    except ZhiwoError as exc:
        raise click.ClickException(str(exc)) from exc
    if provider is None:
        raise click.ClickException(
            "no LLM provider configured; `me ask` needs a provider "
            "(set ME_LLM_MODEL for a live endpoint, or pass --replay FILE)"
        )
    return provider


# --- review（plan §3 决策 8 / SPEC §2.3）-----------------------------------


def _review_one(dossier: Dossier, app: AppContext, item: PendingReview) -> bool:
    """处理一条 pending 事实；返回 False 表示用户选择退出整个 review。"""
    _render_review_item(item)
    choice = _read_choice("  [c]onfirm [e]dit [r]eject [s]kip [q]uit > ", "cersq")
    if choice == "q":
        return False
    if choice == "s":
        click.echo("  skipped")
        return True

    if choice == "e":
        text = _prompt_edit_text(item.record.text)
        if text is None:
            return True
        outcome = review_pending(
            dossier,
            item.record.id,
            ReviewAction.EDIT,
            text=text,
            now=app.now,
        )
        _render_review_outcome(outcome)
        return True

    if choice == "r":
        outcome = review_pending(
            dossier,
            item.record.id,
            ReviewAction.REJECT,
            now=app.now,
        )
        _render_review_outcome(outcome)
        return True

    outcome = review_pending(
        dossier,
        item.record.id,
        ReviewAction.CONFIRM,
        now=app.now,
    )
    if outcome.target_changed:
        _render_target_changed(item, outcome.target_id)
        followup = _read_choice("  [s]tandalone [e]dit [r]eject [k]eep pending > ", "serk")
        if followup == "k":
            return True
        if followup == "e":
            text = _prompt_edit_text(item.record.text)
            if text is None:
                return True
            outcome = review_pending(
                dossier,
                item.record.id,
                ReviewAction.EDIT,
                text=text,
                now=app.now,
            )
        elif followup == "r":
            outcome = review_pending(
                dossier,
                item.record.id,
                ReviewAction.REJECT,
                now=app.now,
            )
        else:
            outcome = review_pending(
                dossier,
                item.record.id,
                ReviewAction.CONFIRM,
                standalone=True,
                now=app.now,
            )
    _render_review_outcome(outcome)
    return True


def _read_choice(prompt: str, allowed: str) -> str:
    """读一个单键决定；回车/换行被忽略，Ctrl-D 视为退出。

    用 ``click.getchar`` 而不是 ``click.prompt``：plan §1 的承诺是
    "one keystroke per fact"。输出端显式回显按键，因为 getchar 本身不 echo。
    """
    click.echo(prompt, nl=False)
    while True:
        char = _read_char()
        if char in ("\r", "\n"):
            continue
        if char == "" or char == "\x04":
            click.echo("")
            raise click.Abort()
        lowered = char.lower()
        if lowered in allowed:
            click.echo(lowered)
            return lowered
        click.echo(f"  choose one of: {', '.join(allowed)}", err=True)
        click.echo(prompt, nl=False)


def _read_char() -> str:
    """读一个按键；没有终端（管道/CI）时退回逐行读取。

    ``click.getchar`` 在真正的 TTY 上提供"一键"体验，但它会尝试打开
    ``/dev/tty``；子进程里没有控制终端时那是 ``OSError``，不是用户错误。
    回退到 stdin 的一行并取首个非空白字符，让 ``printf 'c\\n' | me review``
    这种脚本化输入仍然可用。
    """
    try:
        return click.getchar()
    except OSError:
        line = sys.stdin.readline()
        if not line:
            return ""
        stripped = line.strip()
        return stripped[0] if stripped else "\n"


def _prompt_edit_text(current: str) -> str | None:
    """读取编辑后的文本；空输入视为放弃，保持 pending。"""
    text = click.prompt("  new text", default=current, show_default=False)
    if not text.strip():
        click.echo("  empty text; fact stays pending", err=True)
        return None
    return text


def _render_review_list(items: Sequence[PendingReview]) -> None:
    """列出 pending 事实；用 wcwidth 对齐 CJK 双宽字符。"""
    click.echo(f"pending {len(items)}")
    id_width = max(_display_width(item.record.id) for item in items)
    location_width = max(_display_width(_review_location(item.record)) for item in items)
    text_width = max(_display_width(item.record.text) for item in items)
    for item in items:
        kind = item.record.kind.value if item.record.kind is not None else "unknown"
        location = _review_location(item.record)
        target = (
            f"supersedes {item.target_id} ({_target_state(item.target)})"
            if item.target_id is not None
            else "-"
        )
        # text 列之后还有 target 列，cjk 文本必须按显示宽度补齐；否则
        # 一条中文推断会把目标状态推到右边，表格看起来像坏了（wcwidth）。
        click.echo(
            f"  {_pad(item.record.id, id_width)}  "
            f"{_pad(location, location_width)}  "
            f"[pending, {kind}]  "
            f"{_pad(item.record.text, text_width)}  "
            f"{target}"
        )


def _render_review_item(item: PendingReview) -> None:
    """渲染单条事实的 review 头部（列表已给过位置，这里强调当前项）。"""
    click.echo("")
    click.echo(f"{item.record.id}  {_review_location(item.record)}")
    click.echo(f"  text: {item.record.text}")
    if item.target_id is not None:
        click.echo(f"  supersedes: {item.target_id} ({_target_state(item.target)})")


def _render_target_changed(item: PendingReview, target_id: str | None) -> None:
    """OQ#4：延迟边目标已前进/被锁/不可重写时的提示。"""
    target = item.target
    state = _target_state(target)
    click.echo(
        f"  warning: deferred target {target_id} is {state}; "
        "the edge will not be applied automatically",
        err=True,
    )


def _render_review_outcome(outcome: ReviewOutcome) -> None:
    """打印一次 review 决定的结果（成功时含 merge/commit）。"""
    verbs = {"confirm": "confirmed", "edit": "edited", "reject": "rejected"}
    verb = verbs.get(outcome.action.value, outcome.action.value)
    if outcome.merge is None:
        click.echo(f"  {verb} {outcome.fact_id}: no write")
        return
    merged = outcome.merge
    click.echo(f"  {verb} {outcome.fact_id}  merge {merged.merge_id}  commit {merged.commit}")
    _render_reconcile_counts(merged)
    if merged.budget is not None and merged.budget.over:
        click.echo(
            "warning: reader view is over budget; the review decision was applied "
            "anyway (D-002). Distill (v0.5) is the mechanism that will drain this queue.",
            err=True,
        )


def _review_location(record: FactRecord) -> str:
    path = record.path if record.path is not None else "(unknown)"
    line = f":L{record.line_no}" if record.line_no is not None else ""
    return f"{path}{line}"


def _target_state(record: FactRecord | None) -> str:
    if record is None:
        return "missing or archived"
    flags = record.status.value
    if record.locked:
        flags = f"{flags}, locked"
    return flags


def _display_width(text: str) -> int:
    """终端显示宽度；控制字符等不可测字符退回字符数（wcswidth 返回 -1）。"""
    width = wcswidth(text)
    return len(text) if width < 0 else width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


# --- 捕获摘要（plan §5 步骤 12）--------------------------------------------


def _render_capture(outcome: CaptureOutcome) -> None:
    """把一次 merge 的结果渲染成 plan §5 步骤 12 要求的摘要。"""
    merged = outcome.merge
    click.echo(
        f"journal  {outcome.capture.path}"
        f"#L{outcome.capture.first_line}-L{outcome.capture.last_line}"
    )

    if outcome.provider_error is not None:
        click.echo(f"warning: extraction unavailable: {outcome.provider_error}", err=True)
        click.echo("warning: the journal capture is committed; no facts were extracted", err=True)
    if outcome.extraction_error is not None:
        click.echo(f"warning: model response was not usable: {outcome.extraction_error}", err=True)

    click.echo(f"applied  {len(merged.applied)}")
    for patch in merged.applied:
        click.echo(f"  - {_patch_line(patch)}")

    if merged.rejected:
        click.echo(f"rejected {len(merged.rejected)}")
        for patch in merged.rejected:
            click.echo(f"  - {patch.op.value}: {patch.reason}")
    if merged.skipped:
        click.echo(f"skipped  {len(merged.skipped)}")
        for patch in merged.skipped:
            click.echo(f"  - {patch.op.value}: {patch.reason}")
    if merged.malformed:
        click.echo(f"malformed {len(merged.malformed)}")
        for item in merged.malformed:
            click.echo(f"  - draft {item.index}: {item.detail}")

    _render_reconcile_counts(merged)

    if merged.budget is not None:
        click.echo(f"budget   {merged.budget.tokens} / {merged.budget.limit} tokens")
        if merged.budget.over:
            click.echo(
                "warning: reader view is over budget; patches were applied anyway "
                "(D-002). Distill (v0.5) is the mechanism that will drain this queue.",
                err=True,
            )

    if outcome.pending_total:
        click.echo(f"{outcome.pending_total} fact(s) await `me review`")

    click.echo(f"commit   {merged.commit}")


def _render_reconcile_counts(merged: MergeResult) -> None:
    """渲染 reconcile 的五类观察（D-006：报告数据，永不阻塞）。"""
    report = merged.reconcile
    click.echo(
        "reconcile "
        f"adopted={len(report.adopted)} retired={len(report.retired)} "
        f"quarantined={len(report.quarantined)} "
        f"conflicts={len(report.conflicts)} warnings={len(report.warnings)}"
    )
    for adoption in report.adopted:
        click.echo(f"  adopted {adoption.location.render()} -> {adoption.fact_id}")
    for retirement in report.retired:
        click.echo(f"  retired {retirement.fact_id}")
    for item in report.quarantined:
        click.echo(f"  quarantined {item.location.render()}: {item.reason}")
    for conflict in report.conflicts:
        click.echo(f"  conflict {conflict.fact_id} vs {conflict.target_id}: {conflict.detail}")
    for warning in report.warnings:
        click.echo(f"  warning {warning.location.render()}: {warning.detail}")


def _patch_line(patch: ChangelogPatch) -> str:
    """一条已应用补丁的单行摘要（id、op、目标、kind）。"""
    if patch.op is PatchOp.LINK:
        return f"link {patch.from_fact} -> {patch.to}"
    target = patch.target if patch.op is PatchOp.ADD else patch.target_id
    pending = " (pending)" if patch.kind is not None and patch.kind.value == "inference" else ""
    return f"{patch.op.value} {patch.id} -> {target}{pending}"


# --- show 渲染 --------------------------------------------------------------


def _render_records(selection: ShowSelection) -> None:
    """人类可读的 owner 视图：分区分组、状态、关系边。"""
    if not selection.records:
        click.echo("(no facts)")
        return

    if selection.fact is not None:
        _render_record_detail(selection.fact)
        return

    section_path: str | None = None
    for record in selection.records:
        if record.path != section_path:
            section_path = record.path
            click.echo(f"\n{section_path or '(unknown section)'}")
        click.echo(f"  {_record_summary(record)}")


def _record_summary(record: FactRecord) -> str:
    """一行逻辑记录：id、状态（含 locked）、文本与关系边。"""
    flags = record.status.value
    if record.locked:
        flags = f"{flags}, locked"
    line = f"{record.id}  [{flags}]  {record.text}"
    if record.supersedes is not None:
        line += f" (supersedes {record.supersedes})"
    if record.superseded_by is not None:
        line += f" (superseded-by {record.superseded_by})"
    return line


def _render_record_detail(record: FactRecord) -> None:
    """单条事实的 §2.1 逻辑记录：文本、状态与全部已知元数据。"""
    click.echo(f"{record.id}  [{record.status.value}]")
    click.echo(f"  text:          {record.text}")
    click.echo(f"  kind:          {_or_unknown(record.kind.value if record.kind else None)}")
    click.echo(f"  section:       {record.section_id or '(unknown)'}")
    click.echo(f"  path:          {record.path or '(unknown)'}")
    click.echo(f"  line:          {record.line_no if record.line_no is not None else '(archived)'}")
    click.echo(f"  locked:        {'yes' if record.locked else 'no'}")
    click.echo(f"  supersedes:    {_or_unknown(record.supersedes)}")
    click.echo(f"  superseded-by: {_or_unknown(record.superseded_by)}")
    click.echo(f"  created:       {_or_unknown(record.created)}")
    click.echo(f"  updated:       {_or_unknown(record.updated)}")
    click.echo(f"  source:        {_or_unknown(record.source)}")


def _render_conflicts(preview: ConflictPreview, *, as_json: bool) -> None:
    """``me show --conflicts``：冲突、隔离与 R9 警告。"""
    if as_json:
        _echo_json(
            {
                "conflicts": [
                    {
                        "fact_id": conflict.fact_id,
                        "target_id": conflict.target_id,
                        "detail": conflict.detail,
                    }
                    for conflict in preview.conflicts
                ],
                "quarantined": [
                    {
                        "section_id": item.location.section_id,
                        "path": item.location.path,
                        "line_no": item.location.line_no,
                        "reason": item.reason,
                    }
                    for item in preview.quarantined
                ],
                "warnings": [
                    {
                        "section_id": warning.location.section_id,
                        "path": warning.location.path,
                        "line_no": warning.location.line_no,
                        "fact_id": warning.fact_id,
                        "detail": warning.detail,
                    }
                    for warning in preview.warnings
                ],
            }
        )
        return

    if not (preview.conflicts or preview.quarantined or preview.warnings):
        click.echo("no conflicts found")
        return

    if preview.conflicts:
        click.echo(f"conflicts {len(preview.conflicts)}")
        for conflict in preview.conflicts:
            click.echo(f"  - {conflict.fact_id} vs {conflict.target_id}: {conflict.detail}")
    if preview.quarantined:
        click.echo(f"quarantined {len(preview.quarantined)}")
        for item in preview.quarantined:
            click.echo(f"  - {item.location.render()}: {item.reason}")
    if preview.warnings:
        click.echo(f"warnings {len(preview.warnings)}")
        for warning in preview.warnings:
            click.echo(f"  - {warning.location.render()}: {warning.detail}")


# --- why 渲染 ---------------------------------------------------------------


def _render_why(result: WhyResult) -> None:
    """溯源视图：记录摘要 + 每个 changelog 事件及其上下文。"""
    if result.fact is not None:
        click.echo(_record_summary(result.fact))
        if result.fact.source is not None:
            click.echo(f"  source: {result.fact.source}")
    else:
        click.echo("(fact is not present in a file; see the changelog events below)")

    if not result.events:
        click.echo("no changelog events for this fact (it predates the audit trail)")
        return

    click.echo("")
    click.echo(f"events {len(result.events)}")
    for index, event in enumerate(result.events, start=1):
        click.echo(
            f"{index}. merge {event.merge} (extractor {event.extractor}, input {event.input})"
        )
        click.echo(f"   {_event_line(event)}")
        patch = event.patch
        if patch.evidence is not None:
            click.echo(f"   evidence: {patch.evidence}")
        if patch.source is not None:
            click.echo(f"   source:   {patch.source}")
        if patch.reason is not None:
            click.echo(f"   reason:   {patch.reason}")
        if event.context_error is not None:
            click.echo(f"   context unavailable: {event.context_error}")
        source_line = _source_line_no(patch.source)
        for line in event.context:
            marker = ">" if line.line_no == source_line else " "
            click.echo(f"   L{line.line_no:<4}{marker} {line.text}")


def _event_line(event: ProvenanceEvent) -> str:
    """一条事件的 op 摘要：identity、关系与目标。"""
    patch = event.patch
    details: list[str] = [patch.op.value]
    if patch.id is not None:
        details.append(patch.id)
    if patch.target is not None:
        details.append(f"-> {patch.target}")
    if patch.target_id is not None:
        details.append(f"target {patch.target_id}")
    if patch.from_fact is not None:
        details.append(f"from {patch.from_fact}")
    if patch.to is not None:
        details.append(f"to {patch.to}")
    if patch.members:
        details.append(f"members {', '.join(patch.members)}")
    if patch.result is not None and patch.result.value != "applied":
        details.append(f"result {patch.result.value}")
    return " ".join(details)


# --- 通用输出工具 -----------------------------------------------------------


def _record_payload(record: FactRecord) -> dict[str, Any]:
    """逻辑记录的 JSON 形态（枚举经 ``json`` 序列化成字符串）。"""
    return asdict(record)


def _echo_json(payload: object) -> None:
    """统一的 JSON 输出：UTF-8、不转义 CJK（plan §4 编码法）。"""
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _or_unknown(value: object) -> str:
    return "(unknown)" if value is None else str(value)


def _source_line_no(source: str | None) -> int | None:
    """从 ``path#L<n>`` 提取行号；格式不对返回 None（上下文照常显示）。"""
    if source is None or "#L" not in source:
        return None
    _, _, number = source.rpartition("#L")
    return int(number) if number.isdigit() else None


def main() -> None:
    """``[project.scripts] me`` 的入口。"""
    cli.main(prog_name="me")
