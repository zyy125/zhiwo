# v0.2 MVP — Development Plan

**Date:** 2026-10-14 · **Revised:** 2026-10-15 · **Status:** accepted ·
**Implements:** [SPEC.md](../SPEC.md) v0.2-draft

The implementation plan for the v0.2 milestone: a working `me` CLI that runs
the full capture → extract → merge → recall loop end to end. This document
records the scope, the locked decisions, and the milestone breakdown — the
*what* and *in which order*. It is not a second contract: where this document
and SPEC.md disagree, SPEC.md wins, and the disagreement is a bug here.
Layer-level design detail (model fields, function signatures) lives in the
implementation PRs, not here.

## 1. Scope

**Layers in:** L0 store, L1 schema, L2 extract, L3 merge, L4 recall, L6 CLI.
**Layers out:** L5 distill (v0.5), MCP server (v0.3), importers (v0.4).

**CLI surface (8 commands):**

| Command | Role |
|---|---|
| `me "<text>"` | capture: journal append → extract → merge, one commit |
| `me init` | bootstrap a dossier (SPEC §1 tree, git init) |
| `me show [f_id\|section] [--conflicts] [--json]` | owner's view of facts and the §2.1 logical record |
| `me why <f_id>` | provenance: evidence quote, journal line, op chain, via changelog |
| `me review` | confirm / edit / reject pending facts, one keystroke each (§2.3) |
| `me diff [git-args]` | working tree vs HEAD — hand-edits awaiting reconcile |
| `me export [-o FILE] [--clearance]` | reader view as markdown |
| `me ask "<question>" [--clearance] [--show-context]` | answer with the reader view injected |

`me show` disambiguates its positional by shape: an `f_` prefix resolves as
a fact id, anything else as a section id.

Reconcile (SPEC §4) is **not** a command: it is the pre-merge scan step, run
inside the lock before every merge.

`examples/me/` is the golden test fixture: tests copy it to a temp directory
and never mutate the original.

## 2. Locked choices

| Choice | Decision | Rationale |
|---|---|---|
| Language | Python ≥3.11, uv, pydantic v2, pytest, ruff, mypy strict | pydantic fits "schema is data" validation; uv pins the toolchain |
| CLI framework | **click**, not typer | `me "<free text>"` needs "unknown first arg → capture" fallback dispatch; `click.Group.resolve_command` is a documented extension point for exactly this |
| LLM backend | Provider protocol + mock/record/replay first; first real backend **OpenAI-compatible** via plain `httpx` (`{base_url}/chat/completions`) | One client covers OpenAI / Anthropic proxies / ollama / vLLM — the most direct expression of model-agnostic; no SDK dependency |
| Over-budget behavior | **Warn-only** in MVP: patches apply, a loud warning prints, D-002 records it | SPEC §6's defer queue is drained by distill, which is v0.5; a queue with no drain strands facts indefinitely, and the SPEC does not define where the queue durably lives (`.index/` is disposable, so putting it there would violate "nothing is ever lost"). SPEC §6 now carries a version-scoped carve-out note (rule-7 approval 2026-10-14), so "SPEC wins" stays true |

## 3. Design decisions

1. **`.index/` is not persisted in the MVP.** The file ↔ changelog join is
   computed in memory per invocation (milliseconds at single-user scale),
   deleting the whole cache-invalidation surface. `.index/` exists in the MVP
   only to hold the lock file, keeping `.gitignore` at exactly one entry (§1).
   `me index rebuild` is deferred — with guardrails, because the changelog is
   append-only and the join is therefore O(all history): every full scan
   lives behind one store-level function (an index-backed implementation can
   replace it without touching callers), M6 benchmarks the join at 5k
   changelog files, and a red benchmark pulls `me index rebuild` forward
   instead of waiting for v0.5.
2. **Dossier discovery:** `--dossier` flag > `ME_HOME` env > `~/.me/`. The env
   override makes tests hermetic.
3. **Single-writer lock:** `fcntl.flock(LOCK_EX|LOCK_NB)` on `.index/lock`,
   fail fast on contention (resolved decision #3: lock + git backstop, no
   further machinery). POSIX-only in the MVP; a Windows port is a post-MVP issue.
4. **ID assignment:** `max(all ids in files ∪ all ids ever in changelog) + 1`,
   zero-padded to 3. No counter file — no second copy of state. Scanning the
   changelog guarantees retired/archived ids are never reused (§4: never resurrected).
5. **Token counting:** heuristic estimator `CJK chars × 1 + ceil(other/4)`,
   behind one function so a provider-exact counter can replace it later. The
   budget is measured against the high-clearance reader view (the maximal one).
   Known bias: ×1 per CJK char is optimistic for some tokenizers, so warnings
   can arrive late — accepted under warn-only rather than papered over with a
   fudge factor.
6. **Changelog versioning (OQ#3):** every entry is written with
   `schema_version: 1`; readers treat a missing field as 1. No migration
   tooling until a v2 exists.
7. **Pending lifecycle (OQ#1):** silent accumulation + a one-line count nudge
   after each capture ("N facts await `me review`"). No expiry — expiry would
   violate "nothing is ever lost".
8. **Review granularity:** one review decision = one merge = one commit. §1
   explicitly anticipates multiple merges within a minute (hence seconds in
   filenames and `-2` collision suffixes); per-decision merges keep the audit
   trail atomic. Batch review (N decisions, one merge) stays a later
   optimization if the decision-#1 benchmark shows per-decision full-history
   scans dominate.
9. **Concurrent pending supersedes (OQ#4):** first confirmation wins. If the
   target is no longer active at confirm time, `me review` surfaces "target
   changed" and offers confirm-standalone / edit / reject — the deferred edge
   is never applied to a moved-on target.
10. **Rejected patches are recorded in the changelog** with
    `result: rejected|skipped` and a reason. The contract mandates recording
    applied patches; recording rejections is a superset — the audit of the
    evidence border working — and changes no semantics.

## 4. Repository layout

```
pyproject.toml              # the only config file: deps, scripts, ruff, mypy, pytest
uv.lock  .python-version  .github/workflows/ci.yml
src/zhiwo/
  errors.py                 # ZhiwoError, SchemaError, MergeRefused, LockHeld, ProviderError
  models.py                 # all pydantic models — shared vocabulary, no I/O
  store.py                  # L0: the only module that knows where data lives;
                            #     atomic writes, journal, changelog, lock, git
  schema.py                 # L1: profile.yaml load/validate, section & clearance resolution
  factfile.py               # marker grammar, line parse/render, byte-exact round-trip
  reconcile.py              # SPEC §4 five cases
  merge.py                  # L3: validate, assign ids, apply, changelog, commit — never a model;
                            #     op dispatch table, so v0.5 `compress` is an addition, not a refactor
  extract.py                # L2: prompt build + response parse → patch drafts (pure data across)
  recall.py                 # L4: core/reader views, journal keyword search, budget, provenance
  pipeline.py               # §5 orchestration: pure data in/out, no rendering — cli.py today and
                            #     the MCP server (v0.3) are both thin shells over it
  cli.py                    # L6: click group with capture fallback + the 8 commands; argument
                            #     parsing and rendering only, never orchestration
  llm/                      # Provider protocol; mock/record/replay; OpenAI-compatible client
  templates/profile.yaml    # bootstrap default — the schema ships as package data
tests/                      # per-module unit tests + CLI E2E + LLM replay fixtures
```

**The round-trip law** (factfile's contract): parse → re-emit untouched lines
is byte-identical; a merge rewrites only lines it has a decision for. Pinned
by golden tests against every file in `examples/me/`.

**The encoding law**: every on-disk write is UTF-8; YAML dumps with
`allow_unicode`, JSON output with `ensure_ascii=False`. Chinese evidence
escaped to `\uXXXX` would break plain-file transparency as surely as a lossy
merge. Pinned by an M2 golden changelog containing CJK evidence.

## 5. Merge pipeline

The heart of the contract, in order (capture path; a review merge runs the
same skeleton minus steps 3–5):

1. **Lock** — flock; everything below happens inside the lock.
2. **Journal append** — create/append `journal/YYYY-MM-DD.md`, return path and
   line range. From this moment the capture is durable: any later failure
   still produces a journal-only commit. Capture never blocks (iron rule #2).
3. **Load state** — profile.yaml (fail loud) → parse all about/goals files →
   fact registry.
4. **Reconcile (§4)** — adopt hand-written lines (new id + `locked`); retire
   changelog-known ids missing from all files (presence check is a
   boundary-aware regex confined to HTML-comment spans and excluding `[[…]]`
   wikilinks: a damaged marker never causes a false retire, and a link
   mention never blocks a true one);
   **refuse the whole merge** on duplicate ids, reporting both locations;
   quarantine damaged markers (report, untouched, excluded — does not block);
   create declared-but-missing section files.
5. **Extract** — the only model call: post-reconcile core view (including
   pending facts and their inert supersede edges) + the new journal lines with
   absolute line numbers → patch drafts. The prompt requires fact text and
   evidence to stay in the source language — never translated.
6. **Validate each draft (border control)** — failures become
   `rejected` records, the rest proceed: schema; declared target; referenced
   ids exist and are not archived; **evidence verbatim** — whitespace-collapsed
   containment within this capture's journal line range, resolving to
   `source: journal/…#L<n>`; locked-fact protection (reject + conflict flag);
   supersede target must be active; text must not break the line grammar.
7. **Assign ids** — sequential, in patch order.
8. **Apply to files** — add appends (inference → `pending` marker);
   supersede/statement strikes the target in place and writes the new line;
   supersede/inference writes the pending edge on the new line only, target
   untouched (§2.3); link appends `[[f_id]]` to the from-fact's text; review
   ops confirm (applying a deferred edge now), edit (→ locked), or reject
   (line removed, archived via changelog).
9. **Append changelog** — `changelog/YYYY-MM-DDThhmmss.yaml` (`-2` suffix on
   collision; merge id = stem), containing applied, rejected, and reconcile
   events.
10. **Budget check** — over budget → loud warning naming v0.5 distill.
    Never blocks, never defers (decision #2-locked above; D-002).
11. **Git commit** — one commit per merge; the journal append rides in it.
12. **Unlock, print summary** — applied / awaiting review / rejected with
    reasons / conflicts / budget.

Crash semantics: no transaction machinery. A crash mid-merge leaves
uncommitted changes; recovery is **selective** — discard the half-applied
merge in `about/`/`goals/` (`git checkout -- about/ goals/`) but never roll
back `journal/`: the step-2 append is durable from the moment it hits disk
and rides the next merge's commit. A blanket `git checkout .` would erase
the capture step 2 promises to keep (iron rule #2). Git remains the backstop
(resolved decision #3).

## 6. Views & recall

- **Core view** (extract's input, owner-side): about + goals facts with status
  active or pending, all sensitivities, rendered with ids and `(pending)` /
  `(locked)` annotations, including inert supersede edges (§2.3: extract sees
  the deferred set rather than re-deriving it).
- **Reader view** (§5, the reader boundary): active facts only; sections
  filtered by clearance; pending never included; the journal never included at
  any clearance — journal recall is a separate high-clearance capability.
- **Journal search:** keyword-first (resolved decision #4) — tokens split on
  Unicode P*/Z* categories (so `，。！、` are separators, not just ASCII
  punctuation), plus CJK bigrams generated only within contiguous CJK runs,
  never across punctuation; scored, top-N. Normal clearance gets nothing.
- **Provenance (`me why`):** every changelog event touching an id — evidence
  quote, journal line ±1 context, op chain, extractor, merge id.
- **`me ask`:** reader view (clearance default **normal**, per the §5 threat
  model) + journal hits only at high clearance → provider → answer;
  `--show-context` prints exactly what was injected.

## 7. Milestones

One branch, one PR, N small commits each. Dependency order M1 → M9
(M5 and M6 are mutually independent and can be parallelized after M4).
Each milestone appends its decision records to [decisions.md](decisions.md).

| M# | Name | Deliverable | SPEC refs | Key tests |
|---|---|---|---|---|
| M1 | Scaffold + L1 schema | uv project, CI, errors/models/schema, profile template | §1, §5, rule 1, OQ#3 | example profile loads; invalid profiles rejected (dup ids, journal downgraded); golden changelog parses |
| M2 | L0 store + factfile | discovery, bootstrap, atomic IO, journal, changelog, lock, git; marker grammar | §1, §2.1, §2.4, resolved #3 | round-trip byte-identical; damaged/absent marker classes; bootstrap tree; flock contention; collision suffix; CJK evidence stays readable UTF-8 on disk |
| M3 | Reconcile + id space | the five §4 cases; changelog-known ids; MergeRefused | §4, §2.1 | one test per table row; no false retire on damaged markers; no id reuse after retire; a `[[f_id]]` link mention does not block retire |
| M4 | Merge core | validation, id assignment, apply, changelog, commit, budget hook | §2.2, §2.3, §3, §6, §7 | evidence matrix; deferred-supersede file states byte-exact; rejections recorded; exactly one commit per merge; locked never rewritten |
| M5 | Extract + providers | Provider protocol, mock/record/replay, prompt | §2.2, §2.3, §3 | replay → expected drafts; malformed JSON tolerated (journal still commits); prompt snapshot incl. the source-language rule; CJK capture replay |
| M6 | Recall (L4) | both views, journal search, token estimate, logical-record join, provenance | §2.1, §5, §6, resolved #4 | clearance filtering; pending excluded; keyword hit resolves to `#L7`; joined record for f_051; 5k-changelog join benchmark |
| M7 | CLI: capture/show/why/diff/init | click group with capture fallback; end-to-end orchestration | §7, iron rules 1–2 | CliRunner E2E with replay; provider failure still commits journal; unknown-word dispatch |
| M8 | CLI: review/export/ask | keystroke review UI, export, ask | §2.3, §5, §6, OQ#1/#4 | scripted keys: confirm rewrites both lines of a deferred pair; edit → locked; reject → line removed + archived; review UI aligns double-width CJK (wcwidth); export renders `~~…~~` via a GFM-capable renderer |
| M9 | OpenAI-compatible + polish | httpx client, README quickstart, decision records, v0.2.0 tag | §5 | opt-in live smoke test; provider-from-env matrix; replay tests still green |

## 8. Positions on SPEC ambiguities

MVP stances on points where the contract is silent or the example diverges.
None of these change contract semantics; the ones worth a permanent record
become D-00x entries as they land.

| # | Issue | MVP stance |
|---|---|---|
| R1 | What "verbatim evidence" matching allows | Whitespace-collapsed containment, full stop. Strictness is an implementation knob behind one function, tuned by live rejection data (M9 reports it) — deliberately **not** a contract-level stance, so no decision record. Pinned by tests incl. CJK |
| R5 | Example changelog filename is minute-precision; §1 mandates seconds | Writer emits seconds + collision suffix; reader accepts any `*.yaml` stem. The example is contract-illustrating data and stays untouched |
| R8 | A hand-damaged marker could make a live id look missing → false retire | Retire's presence check is a boundary-aware regex over HTML-comment spans, not a parse: detect presence, never guess semantics. `[[…]]` wikilinks are excluded, so a link mention never blocks a true retire |
| R9 | Marker/strikethrough inconsistencies (e.g. `superseded-by` without `~~…~~`) | The marker is canonical; strikethrough is rendering. Status follows fixed precedence (superseded-by > pending > active) + a warning in the reconcile report; human lines are never auto-rewritten |
| R11 | Whether rejected patches belong in the changelog | Yes, with `result` + `reason` — a superset of the contract and the audit of the border working |
| R12 | Whole fact file hand-deleted → mass retire | Behaves per §4 with a loud per-id report; git recovers unintended deletions. Not special-cased |
| R15 | Fact text containing `<!--`, `~~`, or newlines would break the line grammar | Rejected at validation; the extract prompt forbids them |

## 9. Verification

1. **Every milestone:** `uv run ruff check . && uv run mypy && uv run pytest`
   green locally and in CI.
2. **From M7, manual smoke** (temp dossier, mock provider): `me init` tree
   matches §1 with a one-line `.gitignore`; a capture produces journal line +
   fact + pending inference + changelog + exactly one commit; a hand-written
   line is adopted as locked on the next merge; a duplicated id refuses the
   merge naming both locations; `me why f_051` prints the evidence quote and
   journal line; `me review` walks confirm/edit/reject; `me export --clearance
   normal` contains no high sections, no pending, no journal; `me ask
   --show-context` shows the injected reader view.
3. **M9, live:** point the env config at any OpenAI-compatible endpoint and
   re-run capture; a patch whose evidence was paraphrased must be rejected
   with the rejection recorded. The live run also reports the
   evidence-rejection rate on CJK captures — the data R1's knob is tuned by.
4. **Golden invariant:** CI round-trips every file in `examples/me/`, so the
   contract's own example can never stop parsing.

## 10. Decision records to land

- **D-002** — over-budget is warn-only until distill exists (v0.5); the defer
  queue is redesigned together with its drain. **Landed** in
  [decisions.md](decisions.md); SPEC §6 carries the carve-out note.
- Further D-00x entries are appended as milestones surface them (e.g. the
  POSIX-only lock, the click capture-fallback dispatch, the selective
  crash-recovery rule). Evidence-matching strictness stays an implementation
  knob (R1), not a decision record.
