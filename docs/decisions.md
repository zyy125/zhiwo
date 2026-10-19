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

## D-005 — L0 store: durable-write recipe, byte-pinning scope, and bootstrap posture

**Date:** 2026-10-16 · **Status:** accepted

**Context.** M2 implemented L0 (`store.py`, `factfile.py`) and left several
implementation-level choices that the plan doc explicitly delegates to the
implementation PRs ("model fields, function signatures live in the
implementation PRs, not here"). They are recorded together because they share
one theme: *which artefact is authoritative for what*.

**Decision.**

1. **Atomic writes.** `atomic_write` = `mkstemp` in the target directory →
   write UTF-8 with `newline=""` → `flush` + `os.fsync(file)` → `os.replace`
   → best-effort `fsync(dir)`. The journal is the deliberate exception: it is
   opened `O_APPEND`, written, and fsynced in place, never rewritten. Its
   append is the durability anchor of plan §5 step 2 — from that moment a
   capture is safe, and nothing later may roll it back (iron rule #2).
2. **Byte-pinning scope.** Fact files (`.md` under `about/` and `goals/`) are
   byte-pinned: `parse → render` must be byte-identical, and a merge rewrites
   only lines it has a decision for (plan §4's round-trip law). Changelog
   entries are **not** byte-pinned — only parse-compatible. They are
   machine-written once and append-only, and the golden example carries
   hand-written YAML comments, so re-dumping it could never be byte-identical;
   pinning it would have been an assertion about PyYAML's formatting rather
   than about the contract.
3. **Merge id vs filename stem.** SPEC §1 requires them to be equal. The
   writer enforces it (`model_copy(update={"merge": stem})`), so no caller can
   get it wrong. The golden example predates this and carries an `m_` prefix
   (`merge: m_2026-10-11T2130` for `2026-10-11T2130.yaml`); per R5's posture
   the example stays untouched, and the reader accepts it, because the reader
   reads content, not naming conventions.
4. **Marker parsing never raises; writing always may.** A damaged marker is
   *expected input* (SPEC §4): parsing classifies it as `LineKind.DAMAGED`
   with a reason and leaves the raw line untouched, for reconcile to
   quarantine. The write side (`render_fact_line`) is a hard gate and rejects
   text that would break the line grammar (plan §8 R15) — newline, `<!--`,
   `-->`, leading/trailing whitespace, empty, and `~~` (which would collide
   with the strikethrough rendering). Consequence: the parser can never lose
   a fact to a hand-edit, and the writer can never produce one a hand-edit
   would have to repair.
5. **Bootstrap posture.** `profile.yaml` is copied verbatim from the package
   template (never re-serialized — D-003's consequence: comments and
   formatting belong to the human). Section files are created **empty**: the
   content is the human's, and pre-filling headings would be pretending to
   know what goes there. `git init` runs, but there is **no initial commit** —
   the first commit is the first merge's, so "every merge is one commit"
   (SPEC §7) holds from the very first one. A new daily journal file gets only
   a `# <date>` heading; the golden example's explanatory HTML comments are
   documentation for a reader of the example, not part of the format.
6. **Error vocabulary grows by three.** `plan §4` named five exception
   classes; L0 has three more failure modes with no home — `MarkerError`,
   `DossierNotFound`, `GitError`. Following M1's posture (the vocabulary is
   the cross-layer API, so names are pinned before they are raised), they are
   added to `errors.py` rather than left as bare `ZhiwoError` or
   `OSError`/`CalledProcessError` leakage.

**Why.** Each choice picks the artefact that can be the authority on a
question: the journal on durability, the fact file on text, the changelog on
provenance, the human on anything hand-edited. Where two artefacts could
claim authority (a re-dumped changelog, a merge id vs its filename, a
hand-written marker vs a parseable one), the decision names the winner
explicitly instead of letting whichever code path runs first decide.

**Consequences.** An index-backed `known_fact_ids` can replace the scan
without touching callers (plan §3 decision 1). A Windows port must replace
`fcntl` (POSIX-only, accepted post-MVP). If a changelog format v2 ever needs
byte-pinning or a migration, that is a new decision, not an amendment to this
one.

## D-006 — M3 reconcile: scan-not-write, the adopt/retire patch shapes, and the case-5 boundary

**Date:** 2026-10-19 · **Status:** accepted

**Context.** M3 implemented `reconcile.py` (SPEC §4's five observations +
the id space, plan §5 step 4). SPEC defines the *semantics* of adopt/retire
but not their field shapes, and leaves open how reconcile relates to the
pipeline's write steps. These positions were taken during implementation and
user review.

**Decision.**

1. **Reconcile is a scan, not a writer.** The pure core
   (`reconcile_documents`) mutates in-memory `FactDocument`s via
   `FactLine.rewrite` only and returns ready-made `ChangelogPatch`es; every
   disk write — adopted files, created section files, changelog, git commit
   — belongs to M4's pipeline steps 8/9/11. This keeps an adoption, its
   changelog entry, and its commit atomic under plan §5's crash semantics
   (`git checkout -- about/ goals/` discards a half merge; an un-adopted
   hand-written line stays adoptable next merge). The round-trip law makes
   "M4 writes every document back" free.
2. **Adopt patch shape.** `id / target / kind / text / evidence / source`:
   evidence is **the line itself** (sans terminator, bullet included — the
   human's own words are the verbatim quote), kind is always `statement`,
   `target` is the section's declared path (same semantics as add), `source`
   is `path#L<n>` in the same coordinate system as add's journal pointer.
   `result` stays at the `applied` default. A hand-written line whose
   adoption would break the write-side grammar (R15, e.g. text containing
   `~~`) is quarantined, never guessed into a file — "never guess" extends
   to "never write a line that cannot be written legally", and the failed
   attempt consumes no id.
3. **Retire patch shape.** `id / reason`, `result` at the `applied` default
   (archival takes effect when the observation is recorded). No evidence:
   the fact is already gone from disk, so the recordable thing is *why* the
   id was judged hand-deleted. **No tombstone anywhere** — the changelog
   entry is the tombstone (SPEC §2.1: archived never appears in a file);
   `retired_ids` (ids that already have a retire event) prevents repeat
   records on every subsequent merge.
4. **Two id sets, two questions.** The *ceiling* (never reuse) is every id
   anywhere: parsed markers ∪ all comment-span tokens (damaged markers and
   prose-comment mentions included — skipping a number is free, reuse is
   not) ∪ every changelog id including rejected ones (the
   `known_fact_ids` semantics). *Retire candidates* additionally require an
   **applied creation event** (op add/supersede/adopt, `result: applied`):
   a rejected patch's id never existed on disk and must never look
   hand-deleted. Assignment is `max + 1`, zero-padded to 3, no gap filling
   (plan §3 decision 4).
5. **Retire is conservative in both directions.** Presence is
   `mentioned_fact_ids` over comment spans with `[[…]]` wikilinks stripped
   (R8): a damaged marker still counts as present (unterminated `<!--`
   extends to EOF), and a **prose-comment mention counts too** — the golden
   `career.md` header mentioning `me why f_051` blocks f_051's retire.
   Chosen deliberately: a false retire is a one-way door
   ("never resurrected"), while a missed retire is re-judged next merge.
   Journal is excluded from the scan entirely (a journal mention is not a
   fact living in a file). Whole-file deletion and emptied files (R12) fall
   out as per-id retires with a per-id report; file-only ids (never in the
   changelog) are not retired — plan §5's literal "changelog-known" rule.
6. **Case 5 is the structural slice only.** M3 flags one contradiction
   signal visible without semantics: an adopted locked fact whose text
   *exactly* duplicates an active fact's text (no normalization —
   plain-file transparency). Nothing is superseded in either direction.
   Semantic contradiction is extract's judgment (SPEC §3) and lands with
   M4/M5's locked-fact protection.
7. **MergeRefused is raised by the scan, before any mutation.** Duplicate
   ids abort the whole merge naming every `path:L<n>` location; documents
   stay byte-identical (a refused merge leaves no trace). Quarantine
   findings from the same run are lost until the duplicate is fixed —
   acceptable: the user must edit files either way, and the next run
   re-reports. Quarantine, conflicts, and R9 struck-mismatch warnings are
   report data and never block; rendering them is the CLI's job (M7).

**Why.** The recurring theme is *who owns a moment of change*: reconcile
owns the judgment, M4 owns the write, the human owns anything their hand
touched. Every refusal to act (no tombstone, no auto-supersede, no
gap-filling, quarantine instead of repair) protects the same property —
the plain files and the changelog remain a complete, unambiguous account
of what happened and who decided it.

**Consequences.** M4 calls `reconcile_dossier` inside the writer lock,
writes back every returned document plus the created stubs, and appends
`report.patches` to its changelog entry (plan §5 step 9's "reconcile
events"). If review-reject archival (plan §5 step 8, "archived via
changelog") defines a new op rather than reusing `retire`, the
`retired_ids` derivation in the shell must learn it — flagged for M4.
`MergeRefused`'s docstring stays accurate: duplicates remain its only
source.
