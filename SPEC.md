# Zhiwo Data Contract — SPEC v0.1

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
└── journal/         # axis 3: what happened (append-only time stream)
    └── 2026-10-12.md
```

Rules:

- **One level, one axis.** `about/` and `goals/` are divided by theme and by
  time-horizon respectively. Never mix the two in one level — mixed axes make
  classification ambiguous, and ambiguity makes files drift.
- **`journal/` is append-only.** The system never rewrites history. Revision
  of understanding happens in `about/` and `goals/`, never in the journal.
- **The tree is declared in `profile.yaml`, never hardcoded in code.**
  Adding a section (say, `family/`) is a data change, not a code change.
  This is what keeps the schema evolvable and the project portable.

## 2. The fact model

Every statement the system believes about you is a **fact** — the atomic unit
of the dossier. A fact lives in exactly one file and carries:

```yaml
- id: f_017              # stable, system-assigned, never reused
  text: 想读博
  status: active         # active | superseded | archived | locked
  supersedes: f_003      # set when this fact replaces another
  created: 2026-03-01    # when first recorded
  updated: 2026-09-14
  source: journal/2026-03-01.md#L5   # provenance
```

In markdown, a fact is a list item with an inline marker:

```markdown
- 想读博 <!-- f_017 -->
```

Rules:

- **A fact lives in exactly one place.** Cross-domain references use links
  (`[[f_042]]`), never copies. Copies drift apart; drifting copies contradict
  each other; contradiction is poison for every downstream answer.
- **`locked` is set automatically when a human edits a fact by hand.**
  The system may flag conflicts on locked facts but must never overwrite
  them. Human edits are authoritative; a system that reverts your
  corrections has broken rule #1.
- **Superseded facts are never deleted.** They are the trail of how you
  changed — "放弃读博" is meaningless without "曾经想读博".
- **Every fact carries `source` provenance.** "Why do you believe this about
  me?" must always be answerable (`me why "..."`).

## 3. Patches — the only write path

The system writes facts exclusively through patches emitted by the extractor:

```yaml
- op: add
  target: goals/current.md
  text: 秋招目标：后端岗
  evidence: "正在准备秋招，目标是后端"
- op: supersede
  target_id: f_003
  text: 决定直接工作，不读博
  evidence: "想清楚了，先工作"
- op: link
  from: f_042
  to: f_017
```

Rules:

- **No evidence, no write.** The merge layer rejects any patch whose
  `evidence` is not a verbatim quote from the input. A model recording
  something you never said is the worst failure this system can have, and
  it is killed at the border, not patched afterwards.
- **Extract sees `core_view` (the whole core profile) when deciding whether
  new text supersedes an old fact.** Semantic conflict judgment lives in the
  extractor — it is a judgment about meaning, and meaning needs a model.
- **The merger is pure and structural**: ID addressing, schema validation,
  status transitions, changelog. It never calls a model. This keeps the
  only nondeterministic component swappable, mockable, and testable.
- **Importers never write the dossier.** They emit standardized raw notes
  into the pipeline and are processed by the same extract/merge path as
  everything else. One writer, one guarantee.

## 4. Sensitivity

`profile.yaml` marks each section with a sensitivity level:

```yaml
- id: health
  title: Health
  sensitivity: high
- id: career
  title: Career
  sensitivity: normal
```

Readers (MCP clients, exports, `me ask`) receive only sections their
clearance covers. **Open to any AI does not mean open to everything** —
prompt injection is a fact of life, and `health/` and `finance/` must not
be exfiltratable through a random MCP client.

## 5. Budget

The core profile (`about/` + `goals/`) carries a token budget
(initial: 4,000 tokens). When exceeded, distillation must compress before
anything else may be added.

The failure mode this prevents: the dossier quietly becomes another pile of
text that no AI can actually finish reading — at which point the product is
indistinguishable from a notes folder.

## 6. Guarantees

| Guarantee | Mechanism |
|---|---|
| You can always see what it knows | plain files, `me diff`, `me why` |
| You can always fix what it got wrong | text editor; manual edits auto-lock |
| Nothing is ever lost | git; every merge is a commit |
| Nothing is recorded you never said | evidence-verified patches only |
| No vendor lock-in is possible | this on-disk contract |

## Open questions (v0.1)

1. **Marker syntax for fact IDs.** HTML comments are invisible in rendered
   markdown and survive most editors, but are easy to damage by hand-editing.
   Alternatives: trailing tags, frontmatter-per-fact, sidecar index?
2. **Evidence retention.** Should `evidence` quotes be stored on the fact
   permanently (better auditability) or discarded after merge (less clutter)?
3. **Concurrency.** Is a single-writer file lock around merge sufficient for
   a single user running CLI + MCP server simultaneously?
4. **Journal retrieval order.** Keyword-first, embeddings later — right
   sequencing, or premature austerity?
