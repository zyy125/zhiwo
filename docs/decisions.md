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

## D-007 — M4 merge core: draft border, id ceiling, append-only apply, and the budget seam

**Date:** 2026-10-23 · **Status:** accepted

**Context.** M4 implemented `merge.py` (plan §5 steps 6–11: validation, id
assignment, apply, changelog, commit, budget hook). SPEC defines patch
semantics but leaves several implementation choices open: how untrusted drafts
are shaped, what a rejection record looks like when the rejected patch is
itself malformed, where the id ceiling comes from after same-merge adoptions,
how multiple patches against one target interact, and where the budget check
sits. These positions were taken during implementation and review.

**Decision.**

1. **The draft border is M4, and drafts are raw mappings.** Extract (M5)
   returns the parsed patch array as `Sequence[object]`; merge validates every
   entry. `id`, `source`, `result`, and `reason` in a draft are ignored —
   identity and provenance are produced only by the merger (SPEC §3). A draft
   whose `op` cannot be identified becomes a `MalformedDraft` in the result,
   not a changelog record: the changelog's `op` is a closed vocabulary, so
   there is no patch shape to record. This is the one deliberate
   report-only path; the CLI must print it, and extract should never produce it.
2. **A rejection is a minimal changelog record.** `ChangelogPatch` now enforces
   the per-op required fields only for `applied` records (including the default
   `result`); `rejected` and `skipped` records require `reason` and may omit
   the applied shape. Otherwise the most important refusals — a missing
   `evidence`, an undeclared target — could never be recorded, contradicting
   plan §3 decision 10. Applied records keep hard rule 4 intact, and no new
   on-disk field is introduced. Invalid values are quoted in `reason`; only
   individually well-typed fields are salvaged into the record.
3. **The id ceiling is the post-adoption scan.** `ReconcileScan.known_ids` is
   the conservative set computed after reconcile has rewritten its in-memory
   documents: every comment-span mention in those files (damaged markers and
   prose mentions included, wikilinks stripped) union every changelog id.
   Calling `Dossier.known_fact_ids()` here would read the pre-adoption disk
   and let a same-merge adoption and a new add both take the same number. Only
   accepted `add`/`supersede` patches consume an id; rejected/skipped/link
   records do not. A collision with an existing fact raises `ZhiwoError`
   rather than silently renumbering.
4. **Validation and application are one sequential pass.** Each accepted draft
   changes the state the next draft is checked against. Consequently two
   statement supersedes of the same target resolve as "first applies, second is
   rejected (target no longer active)", while two inference supersedes coexist
   (the target stays active and both edges are inert until review). This is
   deterministic given draft order and avoids validating against one state and
   applying against another.
5. **New fact lines are appended, never inserted.** A supersede rewrites the
   target line in place (same line number) and appends the new line at the end
   of that file. Inserting would shift the line numbers of later facts and
   invalidate existing `path#L<n>` adopt sources (D-006). The cost — a
   supersede pair is not necessarily adjacent — is accepted.
6. **References resolve against parsed facts; absence means unknown or
   archived.** There is no separate archived set in merge: a retired id is
   simply not in any file. `supersede` additionally requires the target to be
   `active`; `link` requires both ends to exist and the source not to be
   locked. A locked target is rejected and flagged (`MergeConflict`), never
   auto-superseded, in either direction and for both statement and inference.
7. **The caller owns the lock; one merge is one commit.** `merge_dossier`
   assumes `Dossier.writer_lock()` is held across journal append → reconcile →
   extract → merge. It does not acquire a second lock. Write order is
   documents → changelog → one `git add -A` commit with subject
   `merge: <merge_id>`; the journal append rides that commit. On commit
   failure the files and changelog stay on disk and `GitError` is raised — no
   rollback, because rolling back would discard durable captures. Crash
   recovery is the selective `git checkout -- about/ goals/`, never against
   `journal/`. Patches are applied to a **deep copy** of the scan documents, so
   the scan remains the post-reconcile snapshot: a merge that fails before
   writing (a broken budget estimator, for example) cannot leave applied
   patches in the scan for a later retry to commit without their changelog
   records. This does not make a partial write or a commit failure
   transactional; those still recover through git.
8. **Evidence is read back from the durable journal.** The shell reads the
   capture range through `Dossier.read_journal_entries` and matches against
   those bytes, not against an in-memory copy held by the extractor. Matching
   is whitespace-collapsed containment (any Unicode whitespace → one space),
   first occurrence, per-line first and then across the range; a cross-line hit
   points at its starting line. Evidence outside the capture range is rejected.
   This keeps the `#L<n>` source pointer truthful.
9. **The budget hook is an injected pure estimator.** Merge receives
   `BudgetEstimator` (post-merge documents → high-clearance reader-view token
   count), compares it to `profile.core_budget_tokens`, and returns
   `BudgetStatus`; it runs before any disk write so a broken estimator cannot
   leave a half merge. Over-budget never blocks or defers (D-002) and adds no
   changelog field; M6 supplies the estimator and M7 renders the loud warning.
   When no estimator is injected the status is `None`, which makes the missing
   wiring visible in M7's smoke test.
10. **Review finalization remains M8.** Confirm/edit/reject share the private
    write/changelog/commit tail, but their semantics and the changelog op for a
    review rejection (reusing `retire` versus adding a new op) are M8's
    decision; `ReconcileScan`'s `known_ids` is already the right handoff for
    it. D-006's flag about `retired_ids` learning a new op remains open.

**Why.** The recurring question is *which artefact owns this fact*: the raw
draft is untrusted input, the merger owns identity and provenance, the
post-adoption files own the id ceiling, the append-only file owns line
addresses, the journal owns evidence, and the caller owns the write lock. Every
choice above names the owner explicitly instead of letting whichever code path
runs first decide. Rejections are data because the changelog is the audit of
the border; malformed drafts that cannot name an op are the one thing that
cannot be a patch record, and they are surfaced rather than hidden.

**Consequences.** M6 must implement the estimator against the post-merge
document list (pending and superseded excluded) and M7 must inject it; until
then over-budget is not detected. M8 can reuse `_write_merge` and
`Capture.from_journal_append` patterns but will need a capture-less input label
for review merges. `ChangelogPatch` readers now accept minimal rejection
records; no released reader exists to break. The contract (`SPEC.md`) and
`profile.yaml` are unchanged.

## D-008 — M5 extract: opaque core view, hash-keyed recordings, and malformed output as data

**Date:** 2026-11-02 · **Status:** accepted

**Context.** M5 lands the only model call site (plan §5 step 5). It has to hand
a post-reconcile `core_view` to the model, make the call swappable for tests,
and decide what happens when the model returns something that is not a patch
array. M5 and M6 are independently schedulable (plan §7), so the boundary
between "who renders the view" and "who consumes it" cannot be left implicit.

**Decision.**

1. **`core_view` is an opaque string supplied by the caller.** M6 owns both
   views and their rendering; extract only embeds the string in a
   `<core_view>` block. This keeps M5 and M6 parallel and prevents extract
   from growing a second renderer whose format can drift from the reader view.
   The capture is passed as raw lines plus an absolute `first_line`, so prompt
   line numbers use the same coordinates as `#L<n>`; extract does not read the
   journal itself.
2. **The provider protocol is one prompt in, one string out, plus an
   identity.** No chat-message, tool-call, or SDK types cross the boundary.
   The identity goes into the changelog's `extractor` field (SPEC §3), so it
   must be stable and non-empty. The OpenAI-compatible adapter is M9.
3. **Malformed model output is data, not an exception.** A response that
   cannot be parsed yields an empty draft tuple plus a diagnostic
   (`ExtractResult.error`); only transport/authentication failures raise
   `ProviderError`. This is what lets the caller keep the journal append
   committed (iron rule #2); M7 turns the diagnostic into a warning rather
   than failing capture. Parsing tolerates Markdown fences, surrounding prose,
   `{"patches": [...]}`, and a single patch object; non-mapping list entries
   are preserved for M4 to report as malformed instead of being silently
   dropped.
4. **Extract emits raw drafts only.** It does not assign ids, resolve
   `source`, check evidence, or protect locked facts. The prompt asks for the
   right behaviour, but validation remains M4's border (D-007): a model can be
   wrong without becoming an authority.
5. **Recordings are JSONL keyed by the prompt's SHA-256.** Each record carries
   `version`, `identity`, `prompt_sha256`, and `response`, in UTF-8 with CJK
   left readable. The full prompt is deliberately not stored: it contains the
   core view (personal context), and copying it into test fixtures would create
   a second dossier outside the dossier. A prompt change makes replay fail
   loudly instead of silently reusing an old response; the prompt snapshot test
   explains what changed. Replay never falls back to a live provider.
6. **The prompt is English and the source-language rule is pinned.** Fact text
   and evidence must stay in the source language; the prompt says so
   explicitly and a committed snapshot includes the sentence. The prompt
   contains no current time or random value, so snapshots are deterministic.
   Target sections come from `profile.yaml` (journal excluded) and are never
   hardcoded. Relative-date grounding is left for a later milestone because
   the capture input does not yet carry an observation date.

**Why.** The alternative — extract rendering the core view, storing full
prompts in recordings, or raising on bad model output — would each create a
second owner for something the contract already assigns elsewhere: M6 for
views, the dossier for personal context, and the journal write for durability.
Keeping those owners separate is what makes the model boundary replaceable
without changing on-disk semantics.

**Consequences.** M7 must catch `ProviderError`, pass an empty draft list to
merge, and print the diagnostic without blocking the journal commit. M6 must
supply a stable core-view string; if its rendering changes, the prompt
snapshot and recording hashes are expected to be re-recorded deliberately.
M9 adds the network client behind the same `Provider` protocol. The contract
(`SPEC.md`) and `profile.yaml` are unchanged.

## D-009 — M6 recall: one join, two views, and the reader boundary

**Date:** 2026-11-03 · **Status:** accepted

**Context.** M6 implemented `recall.py` (L4): core/reader views, the token
estimator consumed by M4's budget hook, journal keyword search, the §2.1
logical-record join, and provenance. SPEC defines those concepts but leaves
the rendering format, the join's fallback values, the tokenizer, and the
source-context lookup open. D-008 also assigned M6 ownership of `core_view`
and warned that a rendering change re-records M5's prompt fixtures.

**Decision.**

1. **Both views are pure renderers over `(Section, FactDocument)`; only the
   thin shells read disk.** `load_documents` delegates to `store`; rendering,
   token counting, join, search and provenance operate on in-memory data.
   The core view follows SPEC §2.3 literally: active + pending only, locked
   as an annotation, `supersedes` edges shown (including inert pending ones).
   Superseded and archived facts are excluded. The reader view keeps active
   facts only, filters sections through `can_read`, excludes the journal at
   **every** clearance (plan §6), omits empty sections, and carries no ids or
   markers.
2. **The budget estimator is the high-clearance reader view.** The heuristic
   stays plan §3 decision 5: `CJK chars × 1 + ceil(other / 4)`. M4 receives
   `estimate_reader_tokens` through its existing `BudgetEstimator` seam; the
   estimator counts the maximal (high-clearance) view, never the normal one.
3. **The logical record splits ownership exactly as §2.1 says.** Files own
   text, live status, `locked` and relation edges; applied changelog creation
   events own `kind` and `source`; `created` is the earliest applied event
   date and `updated` the latest. Dates come from the ISO date in the merge
   id, which tolerates the golden example's `m_` prefix (R5). Rejected and
   skipped events are provenance only — they never move status or dates.
   Missing history stays `None` rather than being inferred from pending flags
   or file mtimes. `archived` requires an applied `retire`, a creation event,
   and absence from every file (D-006's tombstone rule). Duplicate ids raise
   `ZhiwoError` naming both locations; read-side code never picks one.
4. **Journal search is a high-clearance capability over captures only.**
   `Dossier.journal_entries()` skips headings, blank lines and HTML comments
   while preserving absolute line numbers. Tokenization splits on Unicode
   P*/Z* (plus control characters), folds case, and generates CJK bigrams
   only inside contiguous CJK runs — never across punctuation. Ranking is the
   distinct intersection of query and line tokens; ties are newest-first.
   `top_n` defaults to 5 and must be positive; normal clearance returns an
   empty tuple.
5. **Provenance returns all events touching an id, with context read through
   L0.** Touching means `id`, `target_id`, `from`, `to` or `members`; wikilink
   mentions in text are not events. Context is the source line ±1, read by
   `Dossier.read_line_context` for any dossier-relative path, because `adopt`
   sources point at fact files (D-006), not journals. A broken source pointer
   becomes `context_error` data; it does not suppress the event or its
   evidence.
6. **M5's core-view fixture was regenerated deliberately.** The old fixture
   contained a superseded fact, which contradicts both SPEC §2.3 and the
   prompt's own rule 9. `core_view.txt` and `prompt.txt` lost that line, and
   `cjk.jsonl`'s prompt hash was updated; a recall test now asserts the
   fixture is exactly the production renderer's output, so the seam cannot
   drift silently (D-008's re-record path).
7. **The 5k join benchmark is disk-backed and generous.** Five thousand
   facts and five thousand changelog files join in about 4.4 s on the
   development machine; the test budget is 10 s to absorb CI variance. A real
   regression (an O(n²) join) will blow past it. If it ever goes red, pull
   `me index rebuild` forward — do not add an in-memory cache and call it an
   index. `.index/` remains disposable and unpersisted.

**Why.** Every choice names the artefact that owns the answer: the file owns
the live fact, the changelog owns what happened, the store owns disk access,
and the reader boundary owns what a clearance may see. Guessing a missing
`kind` from a marker, serving journal hits to normal readers, or letting a
broken provenance pointer hide evidence would each create a second authority
for something the contract already places elsewhere.

**Consequences.** M7 composes `load_documents` + `render_core_view` for
extract and injects `estimate_reader_tokens` into `merge_dossier`; M8 uses
`render_reader_view`, `search_journal` and `provenance` for export/ask/why.
No `SPEC.md` or `profile.yaml` semantics changed.
