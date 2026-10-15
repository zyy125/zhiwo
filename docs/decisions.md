# Decisions

Dated records of decisions that are **not** part of the contract. Contract
changes belong in `SPEC.md`; this file records the reasoning around them, so
that a later reader can tell a deliberate choice from an accident.

## D-001 — Editor integration (Obsidian / Foam) is deferred; fact-id format unchanged

**Date:** 2026-10-13 · **Status:** accepted

**Context.** The survey in [`prior-art.md`](prior-art.md) (§6) verified that
fact-level `[[f_042]]` links do **not** resolve to fact lines in Obsidian or
Foam. Wikilinks target a file, a heading, or a block — never an arbitrary line;
a block reference requires a bare trailing `^id` on the target line; an HTML
comment is not a block id; and both tools restrict block-id characters to
letters, digits and dashes, which excludes the `_` in `f_042`. Both tools also
graph at file granularity, so block anchors would not produce fact-level nodes
anyway.

**Decision.** Do not pursue editor-native fact navigation now.

- The fact-id format (`f_017`) stays exactly as specified in SPEC §2.1.
- Fact files remain plain markdown, openable and editable in any editor. What is
  relinquished is fact-level navigation, fact-level backlinks, and a fact-level
  graph inside Obsidian.

**Why.** Fact-level navigation would cost, in order of severity:

1. a **contract-level change to the id format** — rule 7 territory; it touches
   every marker, every changelog entry, and every existing dossier;
2. a **second copy of the id on the same line** (comment + `^f-017` anchor),
   in tension with "a fact lives in exactly one place", plus a new reconcile
   rule for the case where a human edits one copy and not the other;
3. **coupling to a not-portable convention** — Obsidian's own documentation
   notes that links with block references do not work outside Obsidian, which is
   in tension with the no-vendor-lock-in guarantee;
4. one **undocumented behaviour requiring an experiment** — whether a trailing
   HTML comment interferes with block detection at all.

**Left open.** A future decision may revisit this if editor integration is
wanted badly enough to pay (1)–(4). Nothing in Zhiwo's correctness depends on
it: the plain-files guarantee is about *you* being able to read and edit the
dossier, not about any particular editor's link graph.

## D-002 — Over-budget is warn-only until distill exists

**Date:** 2026-10-14 · **Status:** accepted

**Context.** SPEC §6 defers `add`/`supersede` patches into a queue when the
core budget is exceeded; the queue is drained by distill. Distill is v0.5 —
the v0.2 MVP ships without it. A queue with no drain strands facts
indefinitely, and the SPEC does not define where the queue durably lives:
`.index/` is disposable, so putting it there would violate "nothing is ever
lost".

**Decision.** In the v0.2 MVP, an over-budget state never blocks and never
defers: patches apply, the merge prints a loud warning naming v0.5 distill,
and the event surfaces in the merge summary. SPEC §6 carries a
version-scoped carve-out note that lapses when the §6 mechanism ships. The
defer queue will be redesigned together with its drain.

**Why.** Of the guarantees in play, capture-never-blocks (iron rule #2) and
"nothing is ever lost" must survive the MVP; "budget is enforced by
deferral" cannot survive without distill in any case. Warn-only degrades the
budget from a hard gate to a visible signal — the honest MVP posture, and a
reversible one. Contract-facing carve-out approved by the owner 2026-10-14
(hard rule 7).

## D-003 — YAML: PyYAML with a duplicate-key-rejecting safe loader

**Date:** 2026-10-15 · **Status:** accepted

**Context.** `profile.yaml` and `changelog/*.yaml` need a parser (M1). No
prior record chose one; the language choice (pydantic v2) was locked in
[plan-v0.2.md](plan-v0.2.md) §2 but left the YAML library open.

**Decision.** PyYAML, used through three fixed rules:

1. always `yaml.safe_load` (never `load`/`full_load`) — no arbitrary object
   construction;
2. always parse a `str` obtained from `read_text(encoding="utf-8")`, never a
   file handle (PyYAML falls back to the locale codec on handles, which
   mangles CJK under a POSIX/C locale — the exact failure the encoding law
   in plan §4 exists to prevent);
3. the loader is a `SafeLoader` subclass that raises on duplicate mapping
   keys, because PyYAML's default is silent last-key-wins and a hand-edited
   profile with two `sections:` blocks would then lose one silently — a
   miniature "nothing is ever lost" (SPEC §7) violation.

**Why not ruamel.yaml.** Its distinguishing feature is round-trip,
comment-preserving *rewriting*. Nothing in the contract rewrites
`profile.yaml`: comments in it are human documentation, the bootstrap
template ships as static package data, and the changelog is machine-written
once and append-only. Paying for a capability the contract forbids needing
would be the wrong trade.

**Consequences.** If a future feature ever rewrites `profile.yaml` while
preserving its comments, revisit this. The comment-loss risk is otherwise
covered by the fact that `me init` copies the template verbatim instead of
serializing it.

## D-004 — L1 validation is stricter than the SPEC's literal text in four places

**Date:** 2026-10-15 · **Status:** accepted

**Context.** SPEC §1 and §5 describe the shape of `profile.yaml` but not how
strictly it should be validated. M1 had to decide four points, each stricter
than a literal reading of the SPEC.

**Decision.** L1 rejects, with `SchemaError`:

1. a non-journal section that omits `sensitivity` — the SPEC says every
   section *is* marked with a level; defaulting an omission to `normal`
   would make the quiet default the leak-prone one;
2. two sections sharing a `path` (in addition to the SPEC's implied unique
   `id`) — two sections over one file makes sensitivity filtering ambiguous,
   and §1 says ambiguity is what makes files drift;
3. an `axis` outside the closed set `about | goals | journal` — the axis
   drives the §5 journal rules, so a free-form string would be a bypass;
4. more than one `axis: journal` section — §1 defines the journal as *the*
   append-only time stream.

**Why.** Each follows from a rationale the SPEC already states (fail loud on
the profile, anti-ambiguity in the layout, journal-never-downgraded in §5).
None of them changes contract semantics, denies a document the SPEC allows,
or touches the on-disk format: they convert silent misconfiguration into a
loud error. This is not hard-rule-1 territory — the *layout* (which sections
exist) stays data; what is closed here is contract vocabulary, on the same
footing as `sensitivity`, `op`, and `kind`.

**Consequences.** A future section type that legitimately needs no
sensitivity must make that explicit (a named level, not an omission). If a
fourth axis is ever wanted, it is a contract change (rule 7): the enum
member and the SPEC edit land together.
