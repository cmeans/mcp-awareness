<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Hybrid Retrieval + Multilingual Support

**Status:** Draft
**Date:** 2026-04-10
**Owner:** @cmeans
**Related issues:** TBD (linked after creation)

## Context

A dogfooding finding on 2026-03-24 surfaced a fundamental problem with current search: a 5000-word vision doc lost to a 2-sentence calendar note for the query *"broader vision six domains knowledge fragmentation life silos"*. The long doc's single averaged embedding vector was weakly similar to everything; the short note's focused embedding won on cosine distance alone.

This is the *dilution bug* and it's a known failure mode of dense retrieval with long documents. Issue #195 originally proposed chunked storage as the fix.

A separate requirement surfaced 2026-04-10: **awareness should be multilingual, with cross-language search working at all times.** A user writing a note in English should be searchable by themselves or others using a Japanese query, and vice versa. This is a differentiator for bilingual users, multinational teams, expat families, and language learners — and it's hard to retrofit.

## Problem statements

1. **Long documents lose to short documents on pure vector similarity** (dilution bug)
2. **Exact-term queries are weakly supported** (identifiers, acronyms, rare words)
3. **Language is hardcoded** — `nomic-embed-text` is English-centric, no per-entry language metadata, no lexical retrieval
4. **Response sizes are large** — full entries returned even when one sentence would answer the query

## Alternatives considered

### A. Parent/child entries with chunked storage (original #195)
Split long entries into child `entries` rows linked via `related_ids`. **Rejected:** pollutes entries table with fragment rows, breaks pagination / soft-delete / briefing semantics, every read tool has to filter chunks.

### B. Chunks in embeddings table only
Multiple rows in `embeddings` with `chunk_index`, entries untouched. **Rejected after honest critique:** introduces HNSW + aggregation problem (top-N entries vs top-N chunks), complicates `text_hash`, creates partial-embedding failure states, anchor prefix dilution risk. Eleven real cons.

### C. LlamaIndex small-to-big with auto-merging
Parent embedding + child chunks + merge-up threshold. **Rejected:** most complex, threshold tuning, still has HNSW aggregation issues.

### D. Defer to clients
Document a convention. **Rejected:** violates the standing token-efficiency directive; clients will chunk inconsistently.

### E. Hybrid retrieval (vector + FTS + RRF) — **chosen for Layer 1**
The dilution bug is not a chunking problem — it's a "cosine similarity is the wrong signal alone" problem. Add Postgres FTS as a second retriever, fuse via Reciprocal Rank Fusion. Long docs are rescued by term matches; exact terms are found by FTS; semantic queries still use vector.

### F. Proposition extraction — **chosen for Layer 3**
Extract atomic claims via a small LLM, embed each individually, return matching claims with backrefs to source entries. Semantic sub-document splits instead of structural ones. Follows Dense X Retrieval (Chen et al., 2023).

## Design — three independent layers

Each layer ships on its own and provides standalone value. Layers 1 and 2 can bundle; Layer 3 is follow-on work.

### Layer 1 — Multilingual hybrid retrieval

**Schema:**
```sql
ALTER TABLE entries ADD COLUMN language regconfig NOT NULL DEFAULT 'simple';

ALTER TABLE entries ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
  setweight(to_tsvector(language, coalesce(data->>'description', '')), 'A') ||
  setweight(to_tsvector(language, coalesce(data->>'content', '')), 'B') ||
  setweight(to_tsvector(language, coalesce(data->>'goal', '')), 'B') ||
  setweight(to_tsvector(language, array_to_string(tags, ' ')), 'C')
) STORED;

CREATE INDEX idx_entries_tsv ON entries USING GIN (tsv);
CREATE INDEX idx_entries_language ON entries(language) WHERE language != 'simple';
```

**Query (single CTE):**
```sql
WITH vector_hits AS (
  SELECT e.id, ROW_NUMBER() OVER (ORDER BY emb.embedding <=> %s::vector) AS rnk
  FROM entries e
  JOIN embeddings emb ON e.id = emb.entry_id AND emb.model = %s
  WHERE {where}
  ORDER BY emb.embedding <=> %s::vector
  LIMIT 50
),
lexical_hits AS (
  SELECT e.id, ROW_NUMBER() OVER (ORDER BY ts_rank_cd(e.tsv, q) DESC) AS rnk
  FROM entries e, plainto_tsquery(%s::regconfig, %s) q
  WHERE e.tsv @@ q AND {where}
  ORDER BY ts_rank_cd(e.tsv, q) DESC
  LIMIT 50
),
fused AS (
  SELECT id, SUM(1.0 / (60 + rnk)) AS score
  FROM (
    SELECT id, rnk FROM vector_hits
    UNION ALL
    SELECT id, rnk FROM lexical_hits
  ) r
  GROUP BY id
)
SELECT e.*, f.score
FROM fused f JOIN entries e ON e.id = f.id
ORDER BY f.score DESC
LIMIT %s;
```

Both branches use their indexes (HNSW + GIN). Fusion is in-memory over ≤100 rows. No planner cleverness required.

**Language resolution at write time:**
1. Explicit `entry.data.language` override
2. User preference (`users.preferences->>'language'`, ISO 639-1)
3. Auto-detection via `lingua-py` on composed text
4. Fall back to `'simple'` if detection is unsure

**Language resolution at query time:**
1. Explicit `search(language=...)` parameter
2. Caller's user preference
3. Fall back to `'simple'`
4. **Vector branch ignores language entirely** — bge-m3 handles cross-lingual retrieval at the model level

### Layer 2 — bge-m3 embedding model swap

**Why:** multilingual by design (100+ languages in one shared vector space), drop-in Ollama replacement, long context (8192 tokens, incidentally helps long-doc dilution), produces dense + sparse + multi-vector representations in one model.

**Schema:** widen `embeddings.embedding` from `VECTOR(768)` to `VECTOR(1024)` via Alembic.

**Prerequisite:** benchmark bge-m3 vs `nomic-embed-text` on awareness data using `benchmarks/semantic_search_bench.py`. **Abort the default swap if English content regresses.** Keep nomic as a config fallback (`AWARENESS_EMBEDDING_MODEL=nomic-embed-text` remains valid).

**Migration:** re-embed wave via existing `backfill_embeddings` background worker.

### Layer 3 — Proposition extraction (experimental, follow-on)

**Why:** sub-document semantic granularity without structural chunking. Propositions are self-contained, so no anchor-prefix problem, no HNSW aggregation problem. Naturally token-efficient — a proposition is 50–200 tokens vs. 5000+ for the source entry.

**Schema:** new `propositions` table mirroring the entries/embeddings design with `entry_id` backref (ON DELETE CASCADE), its own tsvector, its own HNSW index, `extractor_model` column for drift detection.

**Pipeline:** background worker → Ollama generation model (candidates: `qwen2.5:3b`, `phi3.5`) → JSON-parsed claim list → dedupe by text_hash → embed → index.

**Retrieval:** new `find` tool returns propositions with entry backrefs. Entry-level embeddings retained as recall fallback. Feature-flagged by `AWARENESS_PROPOSITION_EXTRACTION=true`.

**Risks:** extraction quality is the recall ceiling on that path; LLM drift requires `extractor_model` tracking; write-time LLM cost (free local, billable cloud); backfill is the most expensive one-time operation; some entry types don't propositionalize (status, alert, suppression, preference) — skip-list.

## Language support

### Built into Postgres (28)
arabic, armenian, basque, catalan, danish, dutch, english, finnish, french, german, greek, hindi, hungarian, indonesian, irish, italian, lithuanian, nepali, norwegian, portuguese, romanian, russian, serbian, spanish, swedish, tamil, turkish, yiddish, plus `simple`.

### Via pgroonga extension (CJK + improved Arabic/Hebrew)
japanese, chinese_simplified, chinese_traditional, korean, hebrew. Postgres base image swap to `groonga/pgroonga:latest-alpine-17`, one Alembic migration to `CREATE EXTENSION pgroonga`.

### Detection
`lingua-py` — high accuracy on short text, pure Python, MIT license, no model downloads beyond the wheel.

### Fallback chain
Explicit override → user preference → auto-detection → `'simple'`. Never breaks a write.

### Unsupported languages
Fall back to `'simple'` (word-boundary tokenization, no stemming — works universally but loses stem-based recall). Server fires a `report_alert` with `alert_id=missing-ts-config-{lang}` so the operator sees it in the briefing and can install the extension. Alert auto-clears once the config exists.

### ISO 639-1 at boundaries
API accepts `'en'`, `'ja'`, `'es'`, etc. Server maps to `regconfig` at the boundary. Unknown ISO codes fall back to `'simple'`.

## Migration plan

### Phase 1 — Hybrid retrieval + language column
1. Alembic: add `language` + `tsv` columns + GIN index
2. Language resolution helpers in `schema.py`
3. `lingua-py` as runtime dependency
4. Rewrite `semantic_search` SQL to hybrid CTE
5. `search` tool gains `language` parameter; `get_knowledge` gains optional `language` filter
6. Backfill migration detects language on existing ~700 entries
7. Unsupported-language alert infra
8. Test coverage across vector/FTS/fusion branches + language resolution + alert firing
9. Dogfooding regression test: the vision doc query surfaces the vision doc

### Phase 2 — pgroonga extension
1. Postgres base image swap: `groonga/pgroonga:latest-alpine-17`
2. Alembic: `CREATE EXTENSION pgroonga`
3. Create text search configurations for japanese, chinese_simplified, chinese_traditional, korean, hebrew
4. LXC install docs for non-Docker production deploys
5. Test coverage with CJK sample content

### Phase 3 — bge-m3 swap (behind env var)
1. Benchmark bge-m3 vs nomic-embed-text on awareness data
2. If benchmarks pass: Alembic migration `VECTOR(768) → VECTOR(1024)`
3. Docker compose pulls bge-m3 on startup
4. `backfill_embeddings` mass re-embed wave
5. nomic remains a config fallback
6. README + deployment guide updates

### Phase 4 — Proposition extraction (experimental)
1. New `propositions` table + indexes
2. Extraction worker + prompt + model config
3. `find` tool + feature flag
4. Backfill on existing entries
5. Benchmark proposition vs hybrid retrieval on sub-document queries
6. Promote to default only after quality validation

## Open questions

1. **RRF k parameter** — k=60 is the published default; does awareness data warrant tuning?
2. **FTS weights** — initial guess is description=A, content/goal=B, tags=C. Empirical validation TBD.
3. **Extraction model (Layer 3)** — qwen2.5:3b vs phi3.5 vs larger. Size × quality × latency tradeoff.
4. **Proposition dedupe threshold (Layer 3)** — exact `text_hash`, or near-duplicate via embedding similarity?
5. **Tool rename** — `semantic_search` → `search`? Cleaner name; add alias for compat.
6. **Per-entry language override surface** — expose in write tools, or implicit via preference + detection only?

## Risks

### Layer 1 — Low
Generated column write cost is negligible. `ts_rank_cd` is not BM25 but is sufficient for personal-scale content. lingua-py is a small pure-Python dep. FTS behavior needs language-specific test coverage.

### Layer 2 — Low-Medium
bge-m3 quality on English content must match or exceed nomic — blocked on benchmark. Re-embed wave briefly degrades production search recall. Larger model → longer embed latency (acceptable for background worker). 1024-dim vectors are ~33% larger storage.

### Layer 3 — Medium
Extraction quality ceilings recall on that path. LLM drift requires per-row `extractor_model` tracking. Short/structured entries need skip rules. Write-time LLM cost is nonzero. Backfill is the most expensive one-time operation.

### pgroonga — Low
~150MB image-size increase. New operational dependency for CJK support. Extension install on non-Docker deploys needs documentation.

## Out of scope

- **#184 response size cap** — Layer 3 mitigates it for the search path, but other read tools still need their own cap. Tracked separately.
- **Federation across language instances** — long-term future work; current design is single-instance multilingual via Option A.
- **Custom BM25 implementation** — `ts_rank_cd` is good enough for personal-scale.
- **OpenAI embedding provider (#111)** — parallel work; bge-m3 is the Ollama-side default.

## Acceptance criteria (high level)

- [ ] Dogfooding regression query returns the vision doc as top result
- [ ] Japanese query returns English entries on the same topic (bge-m3 cross-lingual)
- [ ] English query returns Japanese entries on the same topic
- [ ] Unsupported language write fires an alert and falls back to `'simple'`
- [ ] No regression on pure-English recall vs current nomic-based search
- [ ] Backfill re-detects language on all existing entries without loss
- [ ] CHANGELOG + README updated to document multilingual support
- [ ] Test coverage across all retrieval branches and the fusion layer

## References

- Dogfooding finding — awareness entry `06f85fd0` (2026-03-24)
- Cormack, Clarke, Büttcher, *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods* (2009)
- Chen et al., *Dense X Retrieval: What Retrieval Granularity Should We Use?* (2023) — https://arxiv.org/abs/2312.06648
- Chen et al., *BGE M3-Embedding: Multi-Lingual, Multi-Functionality, Multi-Granularity Text Embeddings Through Self-Knowledge Distillation* (2024) — https://arxiv.org/abs/2402.03216
- Postgres full-text search docs — https://www.postgresql.org/docs/current/textsearch.html
- pgroonga — https://pgroonga.github.io/
- lingua-py — https://github.com/pemistahl/lingua-py
