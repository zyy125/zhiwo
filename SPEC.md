# Zhiwo Data Contract — SPEC v0.2-draft

> **Status: draft, pending review.** Resolves the four v0.1 open questions and
> fills five gaps: canonical storage of fact metadata (§2.1), journal
> sensitivity leak (§5), extraction judgment — statement vs inference (§2.3),
> the distill write path (§3), and reconciliation of human edits (§4).
> No semantic direction changes: the fact model, the single patch channel,
> and the layering are as in v0.1. A summary of changes is at the bottom.

This document defines the on-disk contract between three parties:
**you** (who speaks and edits),
**the system** (which understands and files), and
**any AI** (which reads, with your permission).

Everything else — CLI, MCP server, importers — is an implementation of this contract.
The contract is versioned; implementations may vary; the data must outlive them all.

---

## 1. Location and layout

All data lives in one directory (default `~/.me/`):

```
~/.me/
├── profile.yaml     # declares sections and sensitivity — the schema is data, not code
├── about/           # axis 1: who I am (slow-changing)
│   ├── identity.md
│   ├── health.md
│   ├── relations.md
│   ├── finance.md
│   ├── values.md
│   └── career.md
├── goals/           # axis 2: where I'm headed (intent)
│   ├── long-term.md
│   └── current.md
├── journal/         # axis 3: what happened (append-only time stream)
│   └── 2026-10-12.md
├── changelog/       # append-only audit trail — one file per merge (§3)
│   └── 2026-10-12T0830.yaml
└── .index/          # derived cache — rebuildable, never canonical, gitignored
```

Rules:

- **One level, one axis.** `about/` and `goals/` are divided by theme and by
  time-horizon respectively. Never mix the two in one level — mixed axes make
  classification ambiguous, and ambiguity makes files drift.
- **`journal/` is append-only.** The system never rewrites history. Revision
  of understanding happens in `about/` and `goals/`, never in the journal.
- **`changelog/` is append-only.** Merge, reconcile, and distill only add
  files here; entries are never edited or removed. This is where evidence and
  provenance live permanently (§3). Filenames carry seconds
  (`YYYY-MM-DDThhmmss.yaml`), because two merges can complete inside one
  minute — a capture followed by a confirmation, for instance. If a name
  already exists, a numeric suffix is appended (`…-2.yaml`): a merge never
  overwrites a changelog entry. The merge id inside the file is the filename
  stem.
- **`.index/` is derived.** It is fully rebuildable from the markdown files
  plus the changelog (`me index rebuild`). No code may treat it as a source
  of truth; losing it loses nothing. A real dossier's `.gitignore` contains
  exactly one entry: `.index/`.
- **The tree is declared in `profile.yaml`, never hardcoded in code.**
  Adding a section (say, `family/`) is a data change, not a code change.
  This is what keeps the schema evolvable and the project portable.

## 2. The fact model

Every statement the system believes about you is a **fact** — the atomic unit
of the dossier.

### 2.1 Canonical storage (resolves v0.1 OQ1)

A fact's record is split across three places, with exactly one canonical
owner per part:

| Part | Canonical home | Property |
|---|---|---|
| text, id, live state | the markdown line + inline marker | human-readable, human-editable |
| provenance (evidence, source, op, extractor, time) | the `changelog/` entry that created it | append-only, permanent |
| resolved metadata (status, kind, created, section, line no.) | `.index/` | derived cache, rebuildable |

There is no second copy of anything. The markdown file is the only place a
fact's text and id exist; the changelog is the only place its evidence
exists; the index is a join of the two and is always disposable.

The **logical record** that tools expose (`me show`, L4 recall) is that join:

```yaml
- id: f_017
  text: 决定直接工作，不读博
  kind: statement        # statement | inference (§2.3)
  status: active         # active | pending | superseded | archived
  locked: false          # orthogonal flag, not a status (changed from v0.1)
  supersedes: f_009
  created: 2026-03-01    # from changelog
  updated: 2026-09-14    # from changelog
  source: journal/2026-03-01.md#L5   # from changelog
```

In markdown, a fact is a list item with an inline marker. The marker carries
identity, relations, and flags — everything else is derived:

```markdown
- 决定直接工作，不读博 <!-- f_017 supersedes:f_009 -->
- ~~想读博~~ <!-- f_009 superseded-by:f_017 -->
- 厌恶风险，偏好确定性 <!-- f_001 locked -->
```

Marker grammar: `<!-- <id> [supersedes:<id>] [superseded-by:<id>] [locked] [pending] -->`

- Status is computed from the marker: `superseded-by` → superseded;
  `pending` → pending; otherwise active. `pending` takes precedence — a
  `supersedes` edge on a pending fact is inert until confirmation (§2.3).
  `archived` never appears in a file — archived facts exist only in the
  changelog and index (§4, §3-compress).
- **Superseded facts stay in the file**, struck through, with a
  `superseded-by` marker. "放弃读博" is meaningless without "曾经想读博" —
  the trail of how you changed is visible where you read it, not hidden in a
  database.
- **`locked` is set automatically when a human edits or hand-writes a fact**
  (§4). The system may flag conflicts on locked facts but must never
  overwrite them. Human edits are authoritative; a system that reverts your
  corrections has broken rule #1.

### 2.2 Granularity

**One fact = one claim that can independently become false.**

"秋招进行中，目标后端岗" is two facts: the target role can change while the
hunt continues, and the hunt can end while the target role stays true for the
next one. Coarse facts make supersede lossy — replacing the target role
should not drag the hunt's status down with it. The extractor splits on this
rule; the merger needs no judgment about it.

### 2.3 Statement vs inference (border control for *understanding*)

Evidence-verbatim patches stop a model from recording words you never said.
They do not stop a model from recording a *conclusion* you never drew —
quoted correctly, understood wrongly, and carrying the false authority of
"your own words" into every future answer. Facts therefore carry a `kind`:

- **`statement`** — directly grounded in what you literally said.
  Evidence must be a verbatim span of the input. Enters as `active`.
- **`inference`** — a conclusion the extractor drew (a plan, a preference, a
  relation between events). Evidence must still be a verbatim span, **and**
  the fact enters as `pending`: excluded from the reader view, excluded from
  the budget, unable to supersede — until you confirm or edit it
  (`me review`, one keystroke per fact). Confirming makes it active;
  editing makes it locked; rejecting archives it via the changelog.
- **Deferred supersede.** An inference that contradicts an active fact is
  recorded as a pending supersede: the target fact stays active, the new
  fact stays pending, and the supersede applies only on confirmation.
  Rejection leaves the original untouched. The file states this without
  ambiguity — the *new* fact carries the edge; the target carries nothing
  until the supersede actually applies:

  ```markdown
  - 想读博 <!-- f_009 -->                                   # still active
  - 也许该直接工作 <!-- f_060 supersedes:f_009 pending -->   # pending; edge inert
  ```

  (`f_009` exists in `examples/me/`; `f_060` is illustrative.)

  A pending fact reads as pending (§2.1), so the edge changes nothing until
  confirmation. On confirmation, one merge rewrites f_060's marker to
  `supersedes:f_009` and marks the target
  `~~…~~ <!-- f_009 superseded-by:f_060 -->`. On rejection, f_060 is archived
  through the changelog and the target was never touched. The deferred set is
  therefore reconstructible from the files alone — which is what lets extract
  see it in `core_view` rather than re-deriving it.

Extract sees the full `core_view` (active + locked + pending facts of
`about/` + `goals/`) so it does not re-derive inferences already pending.
Readers never see pending facts (§5).

### 2.4 Standing rules

- **A fact lives in exactly one place.** Cross-domain references use links
  (`[[f_042]]`), never copies. Copies drift apart; drifting copies contradict
  each other; contradiction is poison for every downstream answer.
- **Every fact's provenance is answerable.** `me why f_017` resolves through
  the changelog to the evidence quote and the journal line. The journal is
  append-only, so `#L<n>` references never drift.

## 3. Patches — the only write path

The system writes facts exclusively through patches emitted by the
extractor or the distiller:

```yaml
- op: add                    # add | supersede | link | compress
  target: goals/current.md
  kind: statement
  text: 目标岗位：后端
  evidence: "正在准备秋招，目标是后端"
- op: supersede
  target_id: f_009
  kind: statement
  text: 决定直接工作，不读博
  evidence: "想清楚了，先工作"
- op: compress               # distill only — see below
  members: [f_011, f_012, f_015]
  text: <compressed fact>
- op: link
  from: f_042
  to: f_017
```

Rules:

- **No evidence, no write.** The merge layer rejects any `add`/`supersede`
  patch whose `evidence` is not a verbatim quote from the input. A model
  recording something you never said is the worst failure this system can
  have, and it is killed at the border, not patched afterwards.
- **Every applied patch is written to `changelog/` permanently** — evidence,
  source pointer, op, kind, extractor identity, timestamp. Fact files stay
  clean; the audit trail lives in exactly one append-only place.
  (Resolves v0.1 OQ2: evidence is retained — in the changelog, never
  duplicated onto the fact.) What is recorded is the patch **as applied**: it
  carries the `id` the merger assigned, the resolved `source` pointer, and
  the resulting status transition. The patch *input* carries no `id` —
  identity does not exist until the merger assigns it (§2.1), which is part
  of why merge is the only writer. The changelog is a log of events, not a
  second copy of state: `f_017` appears there as the subject of a past event,
  while the markdown line remains the only authority on whether that fact is
  live today.
- **Extract sees `core_view` when deciding whether new text supersedes an
  old fact.** Semantic conflict judgment lives in the extractor — it is a
  judgment about meaning, and meaning needs a model.
- **The merger is pure and structural**: ID assignment and addressing, schema
  validation, marker writing, status transitions, changelog append, git
  commit. It never calls a model. This keeps the only nondeterministic
  components swappable, mockable, and testable.
- **Distill is the second LLM and it writes only through `compress`
  patches.** For `compress`, the evidence requirement becomes: every member
  id must exist, be active, and be neither locked nor pending. The
  compressed text is a rewrite, so its authority is the member set —
  recursively traceable, through the changelog, back to statements.
  Compression may not introduce new topics: members are already in the core
  profile, and `me why` on a compressed fact shows its members. Compressed
  members become superseded (marked `superseded-by:<new id>` in place) —
  never deleted. One writer, one guarantee — that applies to models too.
- **Importers never write the dossier.** They emit standardized raw notes
  into the pipeline and are processed by the same extract/merge path as
  everything else.

## 4. Reconcile — human edits meet the system

The promise "fix it with a text editor" means hand-edited markdown is
authoritative *input*, not corrupted state. Before every merge, the system
scans the fact files and reconciles:

| Observation | Action |
|---|---|
| Fact line with no valid marker (hand-written) | Assign a new id, mark `locked`, record `op: adopt` in the changelog with the line itself as evidence |
| Known id missing from all files (hand-deleted) | Record `op: retire`; status archived. Never resurrected, never re-added by extract |
| Same id in two places (copy-paste) | **Refuse the merge**; report both locations. Never pick one silently |
| Marker present but unparseable (damaged by hand) | Quarantine: report to the user, leave the line untouched, never guess |
| New locked fact contradicting an active fact | Flag the conflict for `me review`; the system never auto-supersedes in either direction |

Inline markers *will* be damaged by hand-editing — that is an expected
input, not an exceptional path. The system self-heals where it is safe
(adopt, retire) and stops loudly where it is not (duplicates, corruption).

## 5. Sensitivity

`profile.yaml` marks each section with a sensitivity level:

```yaml
- id: health
  title: Health
  path: about/health.md
  axis: about
  sensitivity: high
```

- **The journal is `high` by default and the system never downgrades it.**
  Journal lines are the raw utterances that high-sensitivity facts were
  extracted from; serving the journal to a normal-clearance reader would
  leak exactly what section filtering exists to prevent. The trade-off is
  deliberate: readers without high clearance get the reader view of the
  core profile and no journal recall. Journal recall is a high-clearance
  capability.
- **Two views, one budget-free boundary.** The *reader view* — what MCP
  clients, exports, and `me ask` receive — is active + locked facts of
  sections the reader's clearance covers. Pending facts are never in it.
- Clearance is enforced at the reader boundary (MCP server, export,
  `me ask`), never left to the calling model's discretion.
- **Known limitation: sensitivity crosses sections.** A career fact can
  carry finance-grade content ("需要签字费，因为有助学贷款"). v0.2
  mitigation: extract routes each fact by the topic of the claim and the
  residual leak is accepted and documented. Fact-level sensitivity tags are
  a candidate fix (open question #2).
- **Threat model note.** Section filtering defends against low-clearance
  readers. It cannot defend against a prompt-injected agent that already
  holds high clearance — such an agent can request and exfiltrate
  everything its clearance covers. Therefore high clearance is opt-in per
  reader/session, never a default; `me ask` and exports default to normal.

## 6. Budget

The reader view of the core profile (`about/` + `goals/`) carries a token
budget (initial: 4,000 tokens, declared in `profile.yaml`). Pending,
superseded, and archived facts cost nothing — the trail of how you changed
never pressures the budget.

When the budget is exceeded, distill (compress patches) must run before
further core promotions. **Capture never blocks**: journal append always
succeeds; an over-budget state only defers `add`/`supersede` patches into a
queue applied after distill. Confirming a pending fact that would exceed
the budget likewise triggers distill first. Zero-friction capture (iron
rule #2) survives budget enforcement.

The failure mode this prevents: the dossier quietly becomes another pile of
text that no AI can actually finish reading — at which point the product is
indistinguishable from a notes folder.

## 7. Guarantees

| Guarantee | Mechanism |
|---|---|
| You can always see what it knows | plain files, `me diff`, `me why` (resolved via changelog) |
| You can always fix what it got wrong | text editor; reconcile auto-locks, never overwrites, never resurrects |
| Nothing is ever lost | git; every merge is a commit; superseded facts stay visible in place |
| Nothing is recorded you never said | evidence-verified patches; inferences quarantined as pending |
| Nothing is *believed* about you that you haven't seen | pending facts stay out of the reader view until confirmed |
| Raw sensitive data never reaches low-clearance readers | journal is high; clearance enforced at the reader boundary |
| No vendor lock-in is possible | this on-disk contract |

## Resolved decisions (were v0.1 open questions)

1. **Marker syntax** → HTML comments stay. Reliability comes not from
   trusting markers to survive hand-editing but from reconcile (§4):
   damaged markers are an expected input with a defined handling path.
2. **Evidence retention** → retained permanently, in the changelog only.
   Facts carry no evidence copy; `me why` resolves through the changelog to
   the journal line, which is stable because the journal is append-only.
3. **Concurrency** → a single-writer file lock around
   reconcile + merge + distill is sufficient for one user running CLI and
   MCP server simultaneously. Git is the recovery backstop. No further
   machinery.
4. **Journal retrieval order** → keyword-first. Embeddings, when justified,
   live behind the L4 interface and in derived stores only (like `.index/`,
   disposable and rebuildable). Not premature austerity — appropriate
   restraint.

## Open questions (v0.2)

1. **Pending lifecycle.** Do unreviewed inferences expire after N days,
   nag, or accumulate silently? Accumulation is free (no budget cost) but
   may bury `me review`.
2. **Fact-level sensitivity tags** to close the cross-section leak (§5),
   without breaking "one fact, one place".
3. **Changelog schema versioning** and migration tooling — the changelog
   outlives every implementation, so its format needs its own version
   field and upgrade path.
4. **Concurrent pending supersedes.** Two pending inferences targeting the
   same fact: first confirmation wins and the second is re-reviewed against
   the new state, or both are surfaced together?

## Changes from v0.1 (review guide)

- **§1** Added `changelog/` (append-only audit trail) and `.index/`
  (derived, gitignored) to the layout.
- **§2.1** New: canonical storage split — markdown line (text/id/state) +
  changelog (provenance) + index (derived). Superseded facts now stay
  visible in their file with `superseded-by` markers; v0.1's example had
  them vanishing into an inline annotation. `locked` becomes an orthogonal
  flag rather than a status.
- **§2.2** New: granularity rule (one fact = one independently falsifiable
  claim).
- **§2.3** New: `kind: statement | inference`; inferences enter `pending`
  and are excluded from the reader view until confirmed; deferred
  supersede.
- **§3** Patch schema gains `kind` and `op: compress`; changelog write is
  now part of the merge contract; distill writes exclusively through
  verified compress patches.
- **§4** New: reconcile — defined behavior for hand-written, hand-deleted,
  duplicated, and damaged facts.
- **§5** Journal sensitivity defaults to `high`; reader view defined;
  cross-section leak documented as a known limitation; threat-model note on
  injected high-clearance agents.
- **§6** Budget counts the reader view only; capture never blocks —
  over-budget defers promotions instead; confirmation can trigger distill.
- **§7** Two new guarantee rows (pending quarantine, journal clearance).
- **§1 / §2.1 / §2.3 / §3** — three review-pass refinements: changelog
  filenames carry seconds and never overwrite; a deferred supersede is written
  as `supersedes:<target> pending` on the *new* fact, the target staying
  untouched until confirmation; a changelog entry records the patch as
  applied (with the merger-assigned `id`), making explicit that the changelog
  is an event log rather than a second copy of state.
- v0.1's four open questions resolved; four new ones recorded.
