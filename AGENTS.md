# AGENTS.md — project context for coding agents

## What this is

Zhiwo (知我, "knows me"): a local-first personal dossier that any authorized
AI reads as context. Currently **pre-MVP**: this repo holds the philosophy
(READMEs) and the data contract (SPEC.md). Code comes after the contract.

**Language conventions:** code identifiers, issues, commit messages —
English. Comments — **Chinese, and detailed**: explain intent, cite the
contract (SPEC §), and cover edge cases; never just restate the code.
READMEs — bilingual (README.md English, README.zh.md Chinese).

## Answer style for code (temporary)

Assume the reader may review outcomes without knowing the implementation. In
prose about code — summaries, reviews, explanations, PR notes:

- Lead with behavior, effect, and trade-offs. Do not make internal identifiers
  (function/class/variable names, private helpers, internal module names) the
  subject of a sentence.
- When an exact name, path, acronym, or contract term is necessary — to locate
  a file, quote an error, or name a public command — add a short plain-language
  gloss immediately after it: what it is, what it does, why it matters.
  Example: `reconcile_dossier`（合并前扫描档案、收编手写行的函数）.
- Link a file path by its role when the reader may want to open it; do not make
  them decode the path.
- Keep code blocks, diffs, quoted errors, and commit messages exact; this rule
  is about prose, not artefacts.
- Gloss once per answer, and do not gloss terms the reader already uses (git,
  commit, test); over-glossing makes the answer harder to scan.

## Planned architecture (7 layers)

| Layer | Module | Role | LLM |
|---|---|---|---|
| L0 | `store` | file I/O + git; the only code that knows where data lives | no |
| L1 | `schema` | reads profile.yaml, validates structure | no |
| L2 | `extract` | free text + core_view → Patch[] | **yes** |
| L3 | `merge` | Patch[] × dossier → new dossier + changelog; pure & structural | no |
| L4 | `recall` | question → context to inject | optional |
| L5 | `distill` | journal → core profile, under token budget | **yes** |
| L6 | `iface` | CLI / MCP server / export | no |

Key property: **LLM call sites are the only cracks.** Anything touching a
model takes and returns pure data (patches, JSON) — mockable, recordable,
swappable. That is what "model-agnostic" means in code.

## Hard rules (violating any of these is a bug, not a style issue)

1. **Schema is data, not code.** Section layout lives in `profile.yaml`.
   Never hardcode it.
2. **A fact lives in exactly one file.** Cross-references are links
   (`[[f_id]]`), never copies.
3. **Merge never calls a model.** Supersede decisions (semantic conflict
   judgment) live in extract, which always receives core_view.
4. **Patches without verbatim `evidence` are rejected.** No exceptions.
5. **Never rewrite `locked` (human-edited) facts.** Flag conflicts instead.
6. **Importers only emit raw notes** for the extract/merge pipeline; they
   never write the dossier directly.
7. **Agents must not change the semantics of `profile.yaml` or SPEC.md**
   without explicit user approval — the contract is the product.

## Guarantees the code must preserve

Plain-file transparency (`me diff`, `me why`), human-edit authority
(auto-lock), history via git (every merge = one commit), hallucination
border control (evidence), section-level sensitivity filtering for readers.

## Development workflow

### Git discipline
- One logical change per commit; commit messages in English, imperative
  subject ≤72 chars.
- Never end a session with a dirty working tree.
- Branch per task; open and manage PRs with `gh` (gh pr create / view / checks).
- Never force-push shared branches; rebase before opening a PR.
- Commit and push only when the user asks; merging to main always requires
  approval.

### Token economy
- Search with Grep/Glob (narrow patterns) before Read; Read only the line
  range needed, never whole large files.
- Delegate broad multi-file searches to the Explore agent; keep main context
  for conclusions, not file dumps.
- Don't re-read files already in context; use offset/limit on big files.
- CodeGraph MCP (structural queries instead of grep chains): adopt once the
  codebase is large enough that multi-hop grep/Read chains dominate —
  roughly 200+ source files; below that the index costs more context than
  it saves.

## Current phase

Phase 0 — contract first. Next: SPEC review → v0.2 MVP
(L0/L1/L2/L3-append-first/L4-inject-core/L6-CLI).
