# Prior art — a survey for Zhiwo

**Status:** research notes. This document is not part of the contract and changes
nothing in `SPEC.md`. Findings are inputs to design decisions; where a finding
would touch the contract, it is flagged as needing explicit approval (rule 7).

**Method.** Each repository was cloned shallow and read only; nothing was
installed or executed. Third-party content was treated as untrusted data. Every
claim carries a file path; inferences are marked. **"No prior art found" means
"not found in the projects surveyed here"** — it is not a claim about the world.

Versions read:

| Project | Version / ref |
|---|---|
| `basicmachines-co/basic-memory` | default branch, fetched 2026-10-12 |
| `mem0ai/mem0` | v2.2.1, commit `d26ba5e` |
| `shmsw25/FActScore` | v0.2.0, commit `f28272d` |
| `letta-ai/letta` | `archive` branch (`main` is docs-only as of 2026-10) |
| `langchain-ai/langmem` | default branch |
| `567-labs/instructor` | v2 core |
| `hypothesis/h` | client, anchoring module |

## TL;DR

| Question | Finding |
|---|---|
| Is anyone else doing "markdown files are the authority, index is derived"? | **Yes** — Basic Memory, 4.2k★, actively developed. The philosophy is validated by a live project. |
| Does anyone attach verbatim, per-line evidence to each fact? | **No.** Basic Memory: absent. mem0 / FActScore: no evidence field at all. |
| Does anyone keep superseded facts visible in place, recursively traceable? | **No.** Letta keeps evicted text in a separate searchable DB (no marker, no chain); langmem deletes outright. |
| Does anyone enforce a *hard* token budget on a core store? | **No.** Letta and langmem both treat limits as advisory hints to the model. |
| Does anyone defer over-budget writes instead of evicting old content? | **No.** Every surveyed system evicts old to fit new. |
| Is there prior art for validation-retry at the model boundary? | **Yes** — instructor. Directly liftable. |
| Will `[[f_042]]` resolve to a fact line in Obsidian / Foam? | **No.** See §6 — this corrects an assumption made earlier in discussion. |

---

## 1. Basic Memory — the closest sibling

`basicmachines-co/basic-memory`: local-first AI memory stored as plain markdown,
exposed over MCP. 4.2k★, active.

**Architecture.** Layered `Client → HTTP API → Service → Repository`
(`docs/ARCHITECTURE.md`); MCP tools are thin adapters over typed HTTP clients.
Package under `src/basic_memory/`: `markdown/` (parsing only), `models/`
(SQLAlchemy ORM), `repository/` (SQLite/Postgres + vector indexes),
`index/` (watch service), `services/`, `mcp/`, `cli/`, `api/`.

**Canonical vs derived.** `docs/DOMAIN_MODEL.md`: *"The Markdown file is the
durable authority."* The index (FTS rows, embeddings, chunks, observation and
relation rows) is explicitly rebuildable from markdown (`bm reindex`). External
edits are detected by a `watchfiles` watcher using mtime/size etags and
reconciled by re-parsing and upserting while preserving `external_id`
(`index/watch_service.py`, `docs/DOMAIN_MODEL.md`). This is structurally the
same stance as SPEC §1.

**Note format.** `NOTE-FORMAT.md` is an explicit grammar: frontmatter (`title`,
`type`, `tags`, `permalink`, `schema`, `locked`); observations as
`- [category] content #tag (context)`; relations as
`- relation_type [[Target]] (context)` (multi-word types quoted); temporal
qualifiers `@observed[...]` / `@effective[...]`. Bare wikilinks in prose become
implicit `links_to` relations. Parsing rules: `markdown/plugins.py`.

**Identity.** `Entity.external_id` is a UUID stable across file moves; a human-
readable `permalink` is generated from title/path. Observations get synthetic
permalinks (`models/knowledge.py`).

**MCP tool surface** (verified from `@mcp.tool` decorators): `write_note`,
`read_note`, `edit_note` (with section-scoped operations: append, prepend,
find_replace, replace_section, insert_before/after_section),
`search_notes` (text | title | permalink | vector | hybrid),
`build_context` (depth, timeframe, max_related, compact), `list_directory`,
`delete_note`, `move_note`, `recent_activity`, `schema_validate`,
`schema_infer`, `schema_diff`, plus a POSIX-style alias set (`cat`, `grep`,
`ls`, `find`, `tail`). `memory://` resources and several prompts.

**Gaps (verified).**

| Zhiwo mechanism | Basic Memory |
|---|---|
| Evidence → exact source line | **Absent.** `Observation` has no line/offset field. `NoteSection` tracks body-relative offsets for section editing only. PDF ingestion attaches a page-level locator — ingestion-only, page granularity. |
| Human edits auto-locked | **Partial, manual, API-only.** `locked: true` frontmatter; API writes against a locked note return HTTP 423; the lock survives moves. But it is opt-in, makes no human-vs-agent distinction, and **direct filesystem edits bypass it entirely**. |
| Superseded facts visible in place | **Absent.** No supersede field, no per-fact history. Reconciliation rewrites derived rows; it preserves no trail. |
| Token-budgeted core with a distiller | **Partial.** `build_context(compact=…)`, pagination and `max_related` bound the output, but the source comments say compact is *"Not a guaranteed token budget."* No compressor. |

**Adoptable:** the observation/relation grammar and the denormalized
`Relation(from, to, type)` model; the `locked` mechanics (canonical-markdown
flag, refusal code, lock survives moves); `build_context`'s bounding primitives
and section-scoped editing; the MCP tool vocabulary as a naming reference for L6.

**Conflicting:** its lock is opt-in and API-only — our §4 makes filesystem edits
a first-class input; its reconciliation rewrites derived rows rather than
preserving superseded ones.

---

## 2. mem0 + FActScore — extraction and update decisions

### mem0

**Pipeline** (`mem0/memory/main.py`, `infer=True`): retrieve related existing
memories from the vector store → one LLM extraction call → embed → per-memory
MD5 dedup → persist → entity extraction/linking. Prompts live in
`mem0/configs/prompts.py`.

**Key finding — the famous decision prompt is dead code on the live path.**
`DEFAULT_UPDATE_MEMORY_PROMPT` (ADD / UPDATE / DELETE / NONE, with `old_memory`
on UPDATE) and its assembler `get_update_memory_messages` are referenced only
from `configs/prompts.py` and its tests — never from `memory/main.py`. The live
path uses `ADDITIVE_EXTRACTION_PROMPT`: **add-only extraction plus linking**.
Contradiction against an existing memory is a *linking* trigger, not an update.
(In this snapshot the `linked_memory_ids` output is not persisted on the OSS add
path at all — the field is read/written only for the entity store. Inference:
the linking output is aspirational here.)

**Legacy conflict semantics** (still the canonical artifact a reader will find):
`_update_memory` overwrites the payload **in place under the same vector id**;
`_delete_memory` removes the vector and keeps only a side-table history row.
Exact duplicates are hash-skipped silently; semantic duplicates are dropped by
the prompt with no record.

**Why this matters to us:** mem0's evolution is *toward* add-only + linking —
structurally closer to our supersede model than its own documented ops. But
UPDATE and DELETE as written cannot be represented in our contract: the old fact
would be overwritten or removed, and our supersede keeps it visible with a
marker and a changelog record.

### FActScore

**Decomposition** (`factscore/atomic_facts.py`): the prompt is a single line —
`Please breakdown the following sentence into independent facts: {sentence}` —
with BM25-selected few-shot demonstrations. **No source span is retained.**
Output is parsed by splitting on `"- "`.

**Support verification** (`factscore/factscorer.py`): `Answer the question about
{topic} based on the given context. … Input: {atom} True or False?` — decided by
logit comparison or string parsing; with no model configured it returns
`is_supported = True` unconditionally.

**Mapping:** "supported" for FActScore means *entailed by retrieved Wikipedia
passages*, not *verbatim in the original input*. Its atomicity *goal* matches
our §2.2 granularity rule; its verification method does **not** match our §3
evidence requirement and cannot be reused for it.

### What is liftable from these two

- mem0's transition rule wording: when the user describes *changing, switching,
  replacing, stopping*, the extraction "MUST capture … what the new state is AND
  what it replaces." This is a ready-made statement of the supersede rationale
  for the L2 prompt.
- `No Fabrication: Every detail must trace to the inputs` and the
  **no-detail-contamination** rule (do not fold details of existing memories
  into a new patch) — the discipline our evidence contract already enforces
  structurally; good prompt language to keep models aligned with it.
- Temporal grounding: resolve relative dates against the **observation date**,
  never the current date (`prompts.py`). Portable as a patch-field convention.
- **Not liftable:** `"When in doubt, extract"` — this is the opposite of an
  evidence-gated contract, and mem0 has no `evidence` field to gate on.

---

## 3. Letta (MemGPT) + langmem — bounded context and consolidation

### Letta

**Core memory** is a set of named blocks, each with `label`, `description`,
`value`, `limit`, `read_only` (`letta/schemas/block.py`), rendered into the
system prompt inside `<memory_blocks>`, each block annotated with
`chars_current` / `chars_limit` (`letta/schemas/memory.py`). This is a direct
analogue of `profile.yaml` sections + budget — **except that the budget is
never enforced**: the archived tool executor checks `read_only` and string
uniqueness only, never `len(value) > limit`
(`letta/services/tool_executor/core_tool_executor.py`). Over-limit is purely a
numeric hint the model is asked to respect.

**Pressure messaging.** `MESSAGE_SUMMARY_WARNING_STR`
(`letta/constants.py`): *"The conversation history will soon reach its maximum
length and be trimmed. … you should save that information immediately by calling
function core_memory_append, core_memory_replace, or archival_memory_insert."*
Fires at `memory_warning_threshold * context_window` — but only when
`send_memory_warning_message=True`, which **defaults to False**.

**Eviction.** Full context → forced summarize + clear. The summarizer prompt
(`letta/prompts/summarizer_prompt.py`) has seven sections and caps output at
"under 500 words", and explicitly folds a previous summary forward → recursive.
The summary replaces evicted messages **in context**; the originals stay in the
database and remain searchable via recall. Robust fallbacks: clamp tool returns,
middle-truncate transcripts keeping head/tail.

**Archival memory** is an unbounded vector store behind
`archival_memory_insert` / `archival_memory_search` (top-k default 10);
results are re-injected as a tool return carrying content, timestamp, and
relevance score.

### langmem

The semantic / episodic / procedural taxonomy exists **only in prompt text**,
not as classes (`langchain-ai/langmem`, `knowledge/extraction.py`). Two paths:
hot-path tools the agent calls mid-conversation, and background managers for
post-hoc extraction and consolidation ("Deduplicate, consolidate, and enrich").
`ReflectionExecutor` runs consolidation on a scheduled queue; `LocalReflectionExecutor`
uses a `thread_id`-keyed debounce where a new submit **cancels and reschedules**
the pending task. **Deletes happen**: `enable_deletes=True` drops memories.

### Mapping to Zhiwo

| Zhiwo mechanism | Prior art |
|---|---|
| Core store bounded by a hard budget **declared in data** | **Not found.** Both systems treat limits as advisory. |
| Supersede never deletes; old fact stays in place and is traceable | **Not found.** Letta's recall DB is the nearest cousin but has no marker and no chain; langmem deletes. |
| Capture never blocks; over-budget writes are deferred into a queue | **Not found.** All surveyed systems evict old to fit new. langmem's debounce delays *when consolidation runs*, not *whether a write lands*. |

**A design rationale worth recording.** The inference from Letta's soft-limit
design is that hard rejection without a queue can **deadlock an agent turn**:
the model wants to store something, is refused, and must keep going. A deferral
queue (reject + enqueue) is the structural answer to that failure mode — which
is exactly the shape SPEC §6 already prescribes. State it that way in the docs;
it is a stronger justification than an aesthetic one.

**Adoptable:** the `chars_current` / `chars_limit` annotation pattern (tell the
model how full its budget is rather than only enforcing it silently); the
warning/flush threshold ratios; the cancel-and-reschedule debounce as prior art
for the pending-lifecycle question (open question #1 in SPEC).

---

## 4. instructor — validation-retry at the model boundary

`567-labs/instructor` (v2 core). Default `max_retries=3`, so **4 total
attempts** (`stop_after_attempt(max(max_retries, 0) + 1)` in
`instructor/v2/core/retry.py`). Retryable errors are exactly
`(ValidationError, json.JSONDecodeError, AsyncValidationError, ResponseParsingError)`.

On failure the loop appends two messages before retrying:

1. the **failed assistant message itself** (including its tool call), and
2. one tool/`user` message per call carrying the **raw exception text** —
   `Validation Error found:\n{exception}\nRecall the function correctly, fix the errors`.

It does not feed back the schema (already present as the forced tool definition)
nor a traceback. On exhaustion it raises `InstructorRetryException` carrying
`failed_attempts`, `last_completion`, `n_attempts`.

**Why this fits us:** the retry lives at the model boundary and nowhere else —
extraction may re-ask, `merge` stays pure and simply rejects. A ~20-line
reimplementation (see the sketch in the survey notes) is enough; no dependency
is required.

---

## 5. Anchoring a quote so it survives edits

**W3C Web Annotation Data Model, TextQuoteSelector** (`exact`, `prefix`,
`suffix`; normalized text; selections in unicode code points). The spec defines
**no matching algorithm** and **no no-match behavior**; it only says multiple
matches SHOULD be treated as matching all of them.

**Hypothesis** (`hypothesis/h`, `src/annotator/anchoring/`) supplies the practical
algorithm: store `exact` plus a fixed 32-character prefix/suffix context; search
for exact occurrences first; if none, fall back to approximate matching
(`maxErrors = min(256, len(quote) / 2)`); score candidates by
`1 - errors/len(...)` weighted 50 (quote) / 20 (prefix) / 20 (suffix) / 2
(position hint); pick the single best. **No match → error. Ambiguity is never
rejected** — one winner is chosen deterministically, so duplicated text can
select the wrong copy.

**Relevance to Zhiwo:** the journal is append-only, so `#L<n>` remains stable and
this machinery is unnecessary there. It becomes relevant in exactly two places:
(a) importer raw notes, which are not append-only; (b) if an evidence span must
ever be re-located in a file that humans edit. Neither is needed for the MVP —
this is a documented fallback, not a design change.

---

## 6. Editor compatibility — Obsidian / Foam (corrects an earlier assumption)

**Claim made earlier in discussion:** that fact files with `[[f_042]]` links are
"directly compatible with Obsidian, and the editor ecosystem comes for free."

**Verified status: that claim is wrong at the fact level.** What actually holds:

- Wikilink targets are **file, heading, or block** — never an arbitrary line.
  `[[f_042]]` with no `#` can only ever mean *the file `f_042.md`*. With no such
  file it renders as an unresolved link (Obsidian) or a placeholder (Foam).
- A block reference is `[[file#^blockid]]` (same file: `[[#^blockid]]`), and the
  target line must carry a **bare trailing** `^blockid` — for simple paragraphs
  a space plus `^id` at end of line; for lists/quotes/tables the id goes on its
  own line.
- **An HTML comment is not a block id.** Neither `<!-- f_042 -->` nor
  `<!-- ^f_042 -->` satisfies the documented grammar.
- **Character set:** Obsidian documents block ids as *"Latin letters, numbers,
  and dashes"*; Foam's parser is explicit —
  `BLOCK_ANCHOR_REGEX = /(\s)\^([a-zA-Z0-9-]+)$/`. **Both exclude `_`, so
  `^f_042` is not a valid block id in either tool.** (Third-party sources claim
  Obsidian tolerates underscores; the official docs do not say so. For Foam it
  is definitive.)
- **Graph view is note-granularity in both tools.** Block references do not
  create fact-level nodes or edges. Even if every fact line carried `^f-017`,
  the graph would still show files, not facts.
- Obsidian's own docs note: *"Links containing block references won't work
  outside of Obsidian."*

**So, what is actually free:** markdown renders fine; the marker comments are
invisible in reading view; files are editable in any editor. **What is not
free:** fact-level navigation, fact-level backlinks, and a fact-level graph.

**What it would take to get fact-level navigation** — three coupled changes:

1. ids renamed off the underscore (`f_017` → `f-017`),
2. a trailing block anchor mirrored onto each fact line
   (`… <!-- f-017 superseded-by:f-019 --> ^f-017`),
3. links rewritten as `[[<file>#^f-017]]`.

**Three cautions before anyone does this:**

- (1) is a **contract-level change to the id format** — rule 7 territory; it
  needs explicit approval, and it touches every marker, every changelog entry,
  and every existing dossier.
- (2) makes the id exist in **two places on the same line** (comment + anchor),
  which contradicts "a fact lives in exactly one place" in spirit and needs a
  reconcile rule: *if a human edits one and not the other, which wins?*
- (3) introduces a **mild vendor coupling** (Obsidian's block-ref convention is
  not portable), in tension with the no-vendor-lock-in guarantee. The trailing
  anchor is at least plain text that any editor ignores.
- Also **undocumented and untested:** whether a trailing `<!-- … -->` before
  `^id` interferes with block detection. This would need an experiment before
  anyone relies on it.

**Recommendation:** treat editor integration as a *later, optional* feature with
its own decision record — not as a free win. Nothing in Zhiwo's correctness
depends on it.

**Decided (2026-10-13): deferred. Fact-id format unchanged — see
[`decisions.md`](decisions.md) D-001.**

---

## 7. Synthesis

**Adopt (ideas and mechanics, no dependencies assumed):**

| From | What |
|---|---|
| Basic Memory | observation/relation markdown grammar; `locked` mechanics (canonical flag, refusal, survives moves); section-scoped edits; MCP tool vocabulary for L6 |
| mem0 | transition wording for supersede; no-fabrication / no-contamination prompt rules; observation-date temporal grounding |
| Letta | `chars_current` / `chars_limit` budget surfacing; warn/flush/evict threshold ratios |
| langmem | cancel-and-reschedule debounce as prior art for the pending lifecycle |
| instructor | validation-retry at the model boundary, with the raw validation error fed back |
| FActScore | the atomicity discipline of §2.2 (decomposition into independently falsifiable claims) |
| W3C / Hypothesis | quote anchoring, held in reserve for importer notes |

**Conflict — do not import:** mem0's UPDATE (in-place overwrite) and DELETE;
silent duplicate drops; `"When in doubt, extract"`; langmem's delete-on-
consolidation; Basic Memory's API-only, manual lock; Letta's unenforced limits.

**Appears novel in this survey** (state these as differentiators, with the
scope caveat):

1. A hard token budget **declared in the data** and enforced in code.
2. Supersede that **never deletes**, keeps the old fact visible in place, and
   stays recursively traceable through an append-only changelog.
3. Over-budget **defers new writes into a queue** while capture never blocks.

## 8. Numbers worth stealing (as starting heuristics, not rules)

- Context pressure: warn at **70%**, flush at **100%**, evict ~**50%**
  (MemGPT paper, arXiv 2310.08560). Letta's landed values:
  `memory_warning_threshold = 0.75`, `desired_memory_token_pressure = 0.3`.
- Summary budgets: **≤500 words** (full), **300** (sliding).
- Tool-return truncation: **5000** chars; function-return cap **50000**.
- langmem: search `limit = 10`, `query_limit = 5`, consolidation debounce
  **30–60 min**.
- Basic Memory: section-scoped edits and pagination defaults
  (`page_size`, `max_related`) — a reminder that *bounding output* is a separate
  lever from *bounding storage*.

## 9. Actionable per layer

- **L2 extract** — build the retry loop first (instructor pattern: keep the
  failed attempt, feed back the raw validation error). Borrow mem0's transition
  wording and FActScore's atomicity discipline for the prompt. Do not borrow any
  support-checking method from FActScore.
- **L3 merge** — nothing to lift. No surveyed project has this shape; it is
  ours to design. `examples/me` remains the fixture.
- **L4 recall** — keyword-first is confirmed as the sane default (Basic Memory
  ships text/title/permalink search alongside vectors). Keep quote anchoring in
  reserve for importer notes.
- **L5 distill** — borrow Letta's *"tell the model how full it is"* pattern for
  budget reporting, and its thresholds as initial heuristics.
- **L6 interface** — Basic Memory's MCP tool naming is the best reference table
  available; match its vocabulary where our semantics allow, and diverge
  deliberately where they do not (e.g. refuse-to-overwrite semantics).
- **Docs** — the four "no prior art found" results are legitimate
  differentiators for the README. State them with the survey scope attached.

## Licensing notes

This survey is about ideas. The table exists because "borrow the idea" and
"copy the code" carry different obligations. **Zhiwo is MIT (see `LICENSE`).
Nothing in this document authorizes copying third-party code into this repo.**

| Project | License | Note |
|---|---|---|
| basic-memory | **AGPL-3.0** | Copyleft with a network clause. Ideas are free to use; **copying its code into an MIT project is not.** This is the project we are most tempted to read closely — read it as a specification, not as a source. |
| mem0 | Apache-2.0 | Permissive. Prompts would need rewriting for our schema regardless. |
| letta | Apache-2.0 | Permissive. |
| langmem | MIT | Permissive. |
| instructor | MIT | Permissive — but the loop is ~20 lines; own it rather than depend on it. |
| hypothesis/h | BSD-2-Clause | Permissive; the anchoring module is TypeScript and would need a port. |
| litellm | NOASSERTION | GitHub cannot classify the license (the repo splits off an enterprise directory). Verify before depending on it. |
| fastmcp | Apache-2.0 | Permissive. |
| foam | NOASSERTION | Check before reusing code; cited here for a regex only. |
| promptfoo | MIT | Dev-time tool, not a runtime dependency. |

Prompt *text* is covered by the same discipline: prompts pasted from mem0 or
FActScore carry their project's license. Both are permissive, but the mapping in
§2 requires rewriting prompts for our schema anyway — rewrite, don't paste.

Two source-sweep notes: this document was reviewed for factual errors after
compilation (the Hypothesis repo path was corrected from `hypothes/h` to
`hypothesis/h`); licenses were read from the GitHub API on 2026-10-13, and a
`NOASSERTION` value means GitHub could not determine one — not that none exists.

## Sources

- Basic Memory — https://github.com/basicmachines-co/basic-memory
- mem0 — https://github.com/mem0ai/mem0
- FActScore — https://github.com/shmsw25/FActScore · https://arxiv.org/abs/2305.14251
- Letta (MemGPT) — https://github.com/letta-ai/letta · https://arxiv.org/abs/2310.08560
- langmem — https://github.com/langchain-ai/langmem
- instructor — https://github.com/567-labs/instructor
- Hypothesis — https://github.com/hypothesis/h
- W3C Web Annotation Data Model — https://www.w3.org/TR/annotation-model/#text-quote-selectors
- Obsidian, Internal links — https://obsidian.md/help/Linking+notes+and+files/Internal+links
- Foam — https://github.com/foambubble/foam
- Chain-of-Verification — https://arxiv.org/abs/2309.11495