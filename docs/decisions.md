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
