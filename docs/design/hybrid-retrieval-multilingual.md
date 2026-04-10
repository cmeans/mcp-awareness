<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Hybrid Retrieval + Multilingual Support

**Status:** Draft (amended 2026-04-10 after PR #241 round-1 QA review)
**Date:** 2026-04-10
**Owner:** @cmeans
**Related issues:** #238 (Layer 1), #239 (Layer 2), #240 (Layer 3). Supersedes #195.

## Context

A dogfooding finding on 2026-03-24 surfaced a fundamental problem with current search: a 5000-word vision doc lost to a 2-sentence calendar note for the query *"broader vision six domains knowledge fragmentation life silos"*. This is the *dilution bug* and Issue #195 originally proposed chunked storage as the fix.

The literal mechanism is worth naming precisely, because it changes how hybrid retrieval helps. At `src/mcp_awareness/embeddings.py:212-217`:

```python
_max_content_len = 500
if content := data.get("content"):
    content_str = str(content)
    if len(content_str) > _max_content_len:
        content_str = content_str[:_max_content_len] + "..."
```

**The bulk of any long document is never embedded at all.** Only the first ~500 characters of `data['content']` reach the embedding model. The "averaged dilution" framing I used originally was imprecise — the content isn't diluted in the vector, it's *absent from the vector*. The vision doc query lost because the vision doc's content was invisible to the vector branch from the start; cosine similarity was doing its job correctly on the impoverished text it was given.

This matters for the design in two ways:

1. **Hybrid retrieval is complementary at the data level, not just the algorithm level.** The FTS branch reads the full `data->>'content'` (no 500-char cap in the generated tsvector), while the vector branch sees only the first 500 chars. They observe different data, not just different signals on the same data. The lexical branch rescues long docs by matching terms buried deep in the content that the vector branch never saw.

2. **The dogfooding regression test has to assert the right mechanism.** When the vision doc surfaces in the amended test, it will be rescued by FTS, not by vector. Asserting the source of the rescue (inspecting which branch matched) prevents a future false "it works" signal from masking a regression in either branch.

A separate requirement surfaced 2026-04-10: **awareness should be multilingual, with cross-language search working at all times.** A user writing a note in English should be searchable by themselves or others using a Japanese query, and vice versa. This is a differentiator for bilingual users, multinational teams, expat families, and language learners — and it's hard to retrofit. This requirement is met by **Layer 2** (cross-lingual vector model), not by Layer 1 alone; see "Layer scoping and user-facing releases" below.

## Problem statements

1. **Long documents lose to short documents** — actually because their content is truncated at 500 chars before embedding, not because cosine dilutes them
2. **Exact-term queries are weakly supported** (identifiers, acronyms, rare words)
3. **Language is hardcoded** — `nomic-embed-text` is English-centric, no per-entry language metadata, no lexical retrieval
4. **Response sizes are large** — full entries returned even when one sentence would answer the query
5. **Data sovereignty is undefined.** The current design does not explicitly govern where user content can be sent for inference. As soon as cloud embedding providers (e.g. #111) or cloud extraction models are introduced, every entry's content may pass through a third party without a coherent framework for when this is acceptable. Layer 2 and Layer 3 both need this framework before any non-local inference path ships as a default.

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
The dilution bug is not a chunking problem — it's a "cosine similarity is the wrong signal alone" problem, compounded by the 500-char content truncation. Add Postgres FTS as a second retriever, fuse via Reciprocal Rank Fusion. Long docs are rescued by term matches on their full content; exact terms are found by FTS; semantic queries still use vector.

### F. Proposition extraction — **chosen for Layer 3**
Extract atomic claims via a small local LLM, embed each individually, return matching claims with backrefs to source entries. Semantic sub-document splits instead of structural ones. Follows Dense X Retrieval (Chen et al., 2023).

## Layer scoping and user-facing releases

The three layers have different *technical* scopes and different *user-facing* scopes, and conflating them is a mistake we are explicitly avoiding:

- **Layer 1 alone** delivers: dilution-bug fix + lexical cross-language precision + hybrid ranking. With `nomic-embed-text` as the vector model, vector retrieval remains English-centric. Layer 1 alone is **not** a user-facing multilingual feature.
- **Layer 1 + Layer 2 bundled** delivers: all of Layer 1 *plus* cross-lingual semantic retrieval via a multilingual embedding model. This is the first user-facing "multilingual" release.
- **Layer 3** is an experimental follow-on for sub-document semantic granularity.

**User-facing release framing:**

| Release | User-facing story | Internal contents |
|---|---|---|
| v1 (dilution fix) | "Search is smarter now" | Layer 1 alone; no multilingual marketing claim |
| v2 (multilingual) | "Cross-language unified memory" | Layer 1 + Layer 2, bundled |
| v3 (sub-document) | "Find the answer, not the document" | Layer 3, experimental |

Layer 1 alone can ship first if it provides value (it does — the dilution fix is meaningful on its own), but the project should not call it "multilingual" publicly until Layer 2 lands. Bilingual users who try a Japanese query against English vector-only content will get nothing useful and conclude the feature is broken — exactly the opposite of what we're trying to build.

## Data sovereignty policy

This section is a cross-cutting policy that governs **any** inference call awareness makes — embedding generation (Layer 2 and future embedding providers), proposition extraction (Layer 3), and any future LLM-using feature (summarization, tagging, classification, rewriting). Every code path that sends user content to an inference target, local or remote, must respect this policy.

### Trust anchors: `safe = (we-control-it) OR (contract-protects-it)`

The question "is it safe to send user data to this inference target?" is answered not by network location but by one of two **trust anchors**:

**Trust anchor B — Control.** We (operator, user, or administratively-owned infrastructure) control the machine the model runs on. The data never leaves infrastructure we own.

- Examples: Ollama on the awareness host itself; Ollama on a user's LAN NAS over Tailscale; Ollama on a user-owned GCE instance; an internal corporate LLM endpoint; a model running in the operator's VPC with no external egress.
- **Network location is irrelevant to this classification.** Ollama on a user-owned cloud VM is trust-anchor-B even though the machine is "in the cloud." Ollama running in a VPC alongside a cloud-deployed awareness instance is trust-anchor-B. What matters is administrative ownership, not network topology.
- **"Cloud deployment" ≠ "cloud inference".** Awareness deployed in AWS/GCP/Azure can absolutely use trust-anchor-B by pointing at Ollama (or any other controlled model) running in the same VPC, via private peering, or over a private network. This is a core nuance and the design must not assume otherwise. A managed cloud awareness offering that runs Ollama privately within its own infrastructure qualifies as trust-anchor-B, not trust-anchor-C.

**Trust anchor C — Contract.** A legal instrument binds the third party running the model: enterprise-tier API, zero-retention agreement, BAA, no-training-on-data clause. The third party sees the data in flight but is contractually bound not to retain it, train on it, or access it beyond serving the inference request.

- Examples: OpenAI Enterprise with zero-retention configured; Anthropic Enterprise; Google Workspace-tier Gemini with BAA; Cohere Enterprise.
- **Not examples:** OpenAI consumer API (default 30-day retention); Anthropic consumer tier; Google consumer Gemini; public inference endpoints without a signed contract; OpenAI-compatible proxies of unknown provenance.

**Safety rule.** An inference target is **safe** if it satisfies trust-anchor-B *or* trust-anchor-C. An inference target that satisfies neither is **unprotected**.

### Scope: "our deployments" vs "other operators"

The sovereignty policy has two enforcement levels depending on deployment context:

**Our deployments** — the awareness project's canonical hosted instances (`mcp.mcpawareness.com`, any managed cloud offering that ships in the future, internal instances operated by the awareness project itself). These deployments **must** use trust-anchor-B or trust-anchor-C exclusively. Unprotected providers are not a configuration option in our shipped defaults, full stop.

**Other operators' deployments** — self-hosted awareness instances run by anyone else. Operators are free to configure their instances however they want, including unprotected providers. The awareness server **does not hard-block** unprotected providers — an operator who knows what they're doing can use them. But the server **does emit a log warning** at startup and at each inference call to an unprotected provider, and surfaces the condition through the consent surface below.

The principle is **soft enforcement with visibility**, not hard block. Informed consent is the mechanism; the server's job is to make sure an operator can't *accidentally* leak data through an unprotected path, not to stop a determined operator from making an informed choice.

### Per-entry sensitivity routing (opt-in)

Even with trust-anchor-C (enterprise contract), some users want certain categories of content to never leave infrastructure they personally control. The per-entry sensitivity routing mechanism provides this:

**Reserved tag: `sensitive`.** Any entry with the `sensitive` tag must be routed to a trust-anchor-B inference target. Trust-anchor-C is *not* acceptable for sensitive entries — the contract doesn't matter here; the user's choice is that the data stays on infrastructure they own.

**Operator-configurable additional tags** via env var:
```
AWARENESS_LOCAL_ONLY_TAGS=sensitive,health,family,finance,therapy
```
Sets the deployment-level floor of tags that trigger local-only routing.

**User preference additions** via `users.preferences.local_only_tags`:
```json
{"local_only_tags": ["private", "estate-plan"]}
```
Users can *extend* the set of local-only tags with their own additions. They **cannot remove** tags from the operator's deployment-level floor — the operator's choices are always respected (additive, not subtractive).

**Routing rule at inference time.** For any given entry, compute the union of (env-var tags, user-preference tags). If any of the entry's tags intersect this set, all inference for that entry must use a trust-anchor-B target. If the deployment has no trust-anchor-B target configured at all, see "availability degradation" below.

### The three tradeoffs of sensitivity routing

Users opting into per-entry sensitivity routing are making an explicit choice to prioritize sovereignty over retrieval quality. The three specific costs are documented so users understand what they are giving up:

**1. Quality degradation.** If the deployment's trust-anchor-B model is smaller or weaker than its trust-anchor-C model, sensitive entries get lower-quality embeddings and extractions. In a hybrid deployment, sensitive entries receive a measurably different quality floor than non-sensitive entries on the same instance. See the Sovereignty benchmark requirement below — we are committed to publishing quantitative comparisons so users can make informed tradeoff decisions.

**2. Availability degradation in pure-cloud deployments.** In a deployment with **no trust-anchor-B option configured at all** — pure cloud, no VPC-internal model, no user-controlled LLM endpoint — sensitive entries **receive no vector embeddings and no propositions**. They fall back to Postgres full-text search, which is always local because it *is* Postgres itself. The entries are still stored, still searchable via term matches, and still appear in `search` results — they just don't benefit from Layer 2 semantic retrieval or Layer 3 proposition retrieval.

**This is silent graceful degradation, not a write failure.** The sensitive tag is a promise: "keep this local or don't process it." Overriding the promise to get better retrieval would be worse than degrading. The user sees the effect in their search results (sensitive entries rank lower on semantic queries) and can adjust.

**3. Search consistency drift.** In a hybrid deployment (both B and C available), a query that matches both a sensitive entry (B-only signals) and a non-sensitive entry (B + C signals) may rank the non-sensitive entry higher simply because it has more signals contributing to its score. Users' sensitive entries are systematically less discoverable by semantic queries than their non-sensitive entries on the same instance.

This is the correct behavior — sovereignty has a cost — but it is the subtlest of the three and worth surfacing in user-facing documentation so nobody is surprised by it later.

### Sovereignty benchmark (release criterion for cloud inference)

Before any cloud-inference code path ships as a supported option — cloud embedding providers (Layer 2 / #111), cloud extraction providers (Layer 3), or any future LLM integration — a **sovereignty benchmark** must be published comparing retrieval quality with sensitivity routing enabled versus disabled.

**Benchmark scope:**
- Representative query set covering semantic search, exact-term search, and mixed queries across multiple entry types
- Same entries, same queries, two configurations:
  - "Best available": `AWARENESS_LOCAL_ONLY_TAGS=` (empty) — all inference uses the best configured provider (B or C, whichever is stronger)
  - "Sovereignty mode": `AWARENESS_LOCAL_ONLY_TAGS=sensitive` applied to every entry — all inference uses only trust-anchor-B
- Metrics: recall @1, @5, @10; mean reciprocal rank (MRR); latency P50/P95; storage delta if embeddings differ by dimension
- Published in the deployment guide or README as a quantitative table users can reference when deciding whether the sovereignty tradeoff is worth it for their use case

**Purpose.** Users opting into sensitivity routing are making a quality/sovereignty tradeoff. We owe them the data to make that tradeoff **informed** rather than **superstitious**.

**Gating.** The sovereignty benchmark is a **hard release criterion** for any cloud-inference path shipped as an awareness default. Layer 1+2 with local-only defaults can ship without it — there is nothing cloud to compare against. The moment a trust-anchor-C path is supported as a shipped default, the benchmark must exist before the feature ships.

### Consent surface

Three complementary visibility mechanisms, each serving a different purpose:

**1. `get_info` tool exposes active inference providers (always on).** The `get_info` tool (issue #235) surfaces the current extraction and embedding provider configuration, each tagged with its trust-anchor classification:
- `B` — we-control-it (local/owned infrastructure)
- `C` — contract-protects-it (enterprise-tier with zero-retention guarantees)
- `U` — unprotected (neither B nor C detected)

Users and operators can check at any time. No proactive alerts. This is the "I'm curious, let me look" lane.

**2. First-time-seen briefing notice (one-shot, per provider configuration).** The first time a new inference provider configuration is observed — on first boot, after an operator swaps providers, after a version upgrade changes defaults — a one-line note appears in the briefing: *"inference providers updated: extraction=phi3.5 (B), embedding=multilingual-e5-large (B)."* The operator acknowledges it via `acted_on` and it goes silent. This is the "something changed, you should know" lane.

**3. Recurring briefing warning only when unprotected (conditional).** If the server detects an unprotected inference provider (neither B nor C), a persistent briefing warning appears on every briefing until the condition is resolved. All-protected state (everything is B or C) is **silent** after the first-time notice. This follows the same philosophy as every other awareness alert: silent on all-clear, speak only on warning. Briefings are not polluted with routine status.

This three-surface approach balances visibility against noise. The on-demand `get_info` gives anyone who cares the full picture. The first-time notice catches configuration changes. The briefing warning fires only when there's something that actively needs fixing.

## Design — three independent layers

### Layer 1 — Hybrid retrieval (dilution-bug fix + lexical cross-language)

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

**Note on FTS weights:** initial guess is description=A, content/goal=B, tags=C. Add a Phase 1 task to benchmark this weighting against awareness data before it calcifies. If `description`-as-A doesn't actually outperform `content`-as-A for our entry distribution, adjust before the default ships.

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

Both branches use their indexes (HNSW + GIN). Fusion is in-memory over ≤100 rows. No planner cleverness required. RRF k=60 is the published default and stays unchanged unless benchmarks demand tuning.

**Language resolution at write time:**

1. Explicit `entry.data.language` override (ISO 639-1)
2. Explicit `language` parameter on the write tool (new optional parameter, overrides everything below)
3. User preference (`users.preferences->>'language'`, ISO 639-1)
4. Auto-detection via `lingua-py` on composed text
5. Fall back to `'simple'`
6. **Validate the resolved regconfig exists before INSERT** (see below)

The write tools (`remember`, `add_context`, `learn_pattern`, etc.) get an optional `language` parameter so bilingual users can override per-entry without going through `entry.data.language`. Global preference doesn't fit the bilingual case: one user, multiple languages, depending on context.

**Language resolution at query time:**

1. Explicit `search(language=...)` parameter
2. Caller's user preference
3. Fall back to `'simple'`
4. **Vector branch ignores language entirely** — the embedding model's cross-lingual properties handle multi-language retrieval at the model level (see Layer 2)

**Write-time regconfig validation (Substantive finding — critical):**

If a write reaches INSERT with `language='japanese'` but the Japanese text search configuration doesn't exist (e.g. pgroonga isn't installed), the generated `tsv` column expression errors and **the entire INSERT fails**. The user loses their write. A retry hits the same failure.

The design mandates validation *before* the row reaches the database:

1. At server startup, query `SELECT cfgname FROM pg_ts_config` and cache the result as a set of available regconfigs
2. At write time, after language resolution (steps 1–5 above), verify the resolved regconfig is in the cached set
3. If missing:
   - Coerce to `'simple'` (do not fail the write)
   - Fire `report_alert` with `level='warning'`, `alert_id='missing-ts-config-{lang}'`, pointing at install documentation
   - Record the requested language in the alert so the operator can see exactly which config is needed
4. On alert miss (extension was just installed), refresh the cache on next write of that language

This is the difference between "alert + degrade" and "writes fail for any user whose preferred language isn't supported by the current deployment."

**Schema verification task (Substantive finding):**

Generated tsvector columns using a `regconfig` sourced from another column are an edge case. `to_tsvector(regconfig, text)` is declared IMMUTABLE on modern Postgres, but this exact pattern has been rejected at column-creation time on PG12–PG14, and has had subtle issues on some PG15 minor versions. Before Layer 1 migration code is written, prove the pattern works on the actual target version:

- [ ] Create a fresh PG17 database (matches the `pgvector/pgvector:pg17` base image used in all compose files)
- [ ] Run the schema migration on an empty table
- [ ] Insert sample rows with `language='english'`, `'spanish'`, `'japanese'` (assuming pgroonga configured)
- [ ] Verify `tsv` column is populated correctly for each
- [ ] Update an existing row's `language` column and verify `tsv` regenerates
- [ ] `EXPLAIN ANALYZE` a query with `@@` — confirm GIN scan is used
- [ ] Confirm the hybrid CTE plan uses both HNSW and GIN indexes

If the generated-column approach fails on PG17 for any reason, the fallback is a `BEFORE INSERT/UPDATE` trigger that computes the same expression and stores it in a non-generated column. Functionally equivalent; adds a small write-time cost; keeps the query plan the same. Documenting the fallback now so implementation isn't blocked if the verification fails.

**500-char content truncation — investigate lifting as part of Layer 1:**

The 500-char cap in `embeddings.py:212-217` predates the hybrid retrieval design and was introduced to "keep embeddings focused." In the hybrid-retrieval world, the cap is actively harmful: the FTS branch will find long-content matches regardless, but the vector branch will keep missing them. The cap was a workaround for a problem Layer 1 now solves at the architectural level.

**Add to Phase 1:** evaluate raising the cap to the embedding model's actual context limit (nomic-embed-text supports ~8192 tokens, roughly 30K chars of English prose), or removing it entirely. Re-embed wave via existing `backfill_embeddings` tool. This could be the single highest-leverage one-line change in the entire effort — it might substantially improve vector recall on long docs *before* Layer 2 even ships. Benchmark before committing: confirm the un-truncated content actually helps on the dogfooding query and doesn't degrade other cases.

### Layer 2 — Multilingual embedding model

**Goal:** replace `nomic-embed-text` (English-centric, Nomic/US) with a multilingual embedding model that provides a shared cross-lingual vector space, enabling the user-facing "cross-language unified memory" story.

**Data sovereignty constraint.** Any embedding provider must satisfy the [Data sovereignty policy](#data-sovereignty-policy) — trust-anchor-B (we-control-it) or trust-anchor-C (enterprise contract). The initial Layer 2 candidate list below is all trust-anchor-B (local Ollama models). Future cloud embedding providers (e.g. #111 OpenAI embedding provider) must be configured for enterprise-tier API access (zero-retention, BAA) to qualify as trust-anchor-C — consumer-tier configurations will trigger the log warning defined in the sovereignty policy. The sovereignty benchmark must be published before a trust-anchor-C embedding path ships as a default.

**Model sourcing constraint.** Independent of sovereignty, the default must not be a Chinese-sourced model. This rules out `bge-m3` (BAAI), `bge-large`, `bge-reranker`, `qwen-embed`, and similar, regardless of whether they run local or remote. The constraint applies only to shipped defaults — operators who explicitly opt into a Chinese model via `AWARENESS_EMBEDDING_MODEL` on their own instance are making their own call.

**Candidate models (all non-Chinese, all on Ollama or Hugging Face):**

| Model | Source | License | Languages | Dim | Schema migration | Notes |
|---|---|---|---|---|---|---|
| **`intfloat/multilingual-e5-large`** | Microsoft / intfloat | MIT | 100+ | 1024 | yes (768→1024) | Primary candidate. Closest replacement for bge-m3 in scope. |
| **`granite-embedding:278m`** | IBM | Apache 2.0 | ~12 | **768** | **no migration** | Lower-risk fallback. Covers EN, DE, ES, FR, JA, PT, AR, CS, IT, KO, NL, ZH. |
| **`mxbai-embed-large`** | Mixedbread AI (Germany) | Apache 2.0 | English-focused | 1024 | yes | Strong English quality; weak multilingual. Reference option. |
| **`jina-embeddings-v2-base-multilingual`** | Jina AI (Germany) | Apache 2.0 | ~30 | 768 | no migration | German provenance; narrower language coverage. |
| **`snowflake-arctic-embed:l`** | Snowflake (US) | Apache 2.0 | English-only | 1024 | yes | "Best English, no multilingual story" reference. |
| **`nomic-embed-text`** (current) | Nomic (US) | Apache 2.0 | English-centric | 768 | no migration | Do-nothing baseline. Kept as configurable fallback. |

**Primary candidate: `intfloat/multilingual-e5-large`.** Closest like-for-like replacement for what bge-m3 would have been. 100+ languages, contrastive training on parallel pairs, matches the 1024-dim target, MIT license, Microsoft/intfloat provenance.

**Fallback candidate: `granite-embedding:278m`.** If e5-large benchmarks only borderline against nomic on English, granite's 768-dim means **no schema migration** — the existing embeddings column stays. 12 major languages is a narrower story than 100+ but covers most bilingual user cohorts. Lower migration risk, simpler rollback.

**Critical implementation detail: e5 prefix convention.**

The e5 family requires prefixes on embedded text, or retrieval quality degrades significantly (published benchmarks show 5–10 point drops without them):

- **Write path**: embedding worker prefixes `"passage: "` to composed text before calling the model
- **Query path**: query handler prefixes `"query: "` to the user's query before embedding
- The two paths must stay in sync — asymmetric prefixes silently degrade retrieval with no error signal

This is an implementation tax bge-m3 wouldn't have had. The Layer 2 PR must include tests that assert both paths apply their prefixes, and a code-review checklist item to keep them in sync on any refactor. Cite the model card explicitly in the embedding worker's docstring.

**Does not apply to:** `granite-embedding`, `mxbai-embed-large`, `nomic-embed-text`. Only e5 family. If the final choice is not e5, drop the prefix logic.

**Schema changes (if e5-large or mxbai):**

- Alembic migration to widen `embeddings.embedding` from `VECTOR(768)` to `VECTOR(1024)`
- `AWARENESS_EMBEDDING_DIMENSIONS=1024` as new default
- `AWARENESS_EMBEDDING_MODEL=intfloat/multilingual-e5-large` (or the chosen model) as new default
- nomic remains a configurable fallback

**Schema changes (if granite or jina v2):**

- No migration. `VECTOR(768)` stays.
- `AWARENESS_EMBEDDING_MODEL` updated to the chosen model.
- Re-embed wave covers existing entries with the new model.

**Benchmark gate (blocks default swap, unchanged from original design plus cross-lingual additions):**

Before defaulting to any alternative model, run `benchmarks/semantic_search_bench.py` with the new model against current awareness data and:

1. **English-quality gate (existing):** new model must match or exceed `nomic-embed-text` on English content. Recall @1, @5, @10; similarity score distributions; P50/P95 embedding latency. If English regresses, **abort the swap.**
2. **Cross-lingual smoke tests (new):** prove cross-lingual retrieval actually works with the chosen model, not just that English doesn't regress.
   - At least one **English query → non-English entry** test passes: write a Spanish note about "planning de jubilación", query "retirement planning", assert the Spanish note is in top-10
   - At least one **non-English query → English entry** test passes: write an English note about "retirement planning", query "planning de jubilación", assert the English note is in top-10
   - At least one **same-language non-English** test passes: Spanish query → Spanish entry in top-10 (proves the model handles non-English at all, not just English-via-translation)
3. **Latency comparison:** document P50/P95 embedding latency delta. Larger model → longer embed latency is acceptable for the background worker, but flag if extreme.
4. **Storage comparison:** 1024-dim vectors are ~33% larger than 768-dim; document the delta for operators planning disk capacity.

**If the e5-large English benchmark comes in close to nomic but not clearly above**, switch to granite-embedding:278m as the default: narrower language coverage but no migration, simpler rollback, and the cross-lingual smoke tests still have to pass for it to be worth shipping.

**Migration:** re-embed wave via existing `backfill_embeddings` background worker, same mechanism as prior model swaps.

### Layer 3 — Proposition extraction (experimental, follow-on)

**Why:** sub-document semantic granularity without structural chunking. Propositions are self-contained, so no anchor-prefix problem, no HNSW aggregation problem. Naturally token-efficient — a proposition is 50–200 tokens vs. 5000+ for the source entry.

**Schema:** new `propositions` table mirroring the entries/embeddings design with `entry_id` backref (ON DELETE CASCADE), its own tsvector, its own HNSW index, `extractor_model` column for drift detection.

**Retrieval:** new `find` tool returns propositions with entry backrefs. Entry-level embeddings retained as recall fallback for short/non-propositionalizable entries. Feature-flagged by `AWARENESS_PROPOSITION_EXTRACTION=true`.

**Extraction pipeline:**

- Background worker (separate from embedding worker, same pattern)
- Pulls entries flagged as extraction-pending via a view
- Calls a **local** Ollama generation model (see candidate list below)
- Parses output as a JSON array of proposition strings
- For each proposition: compute `text_hash`, upsert by `(entry_id, text_hash)` — propositions surviving an edit are preserved
- Embed propositions via existing embedding worker
- Mark entry as extracted

**Data sovereignty constraint.** Same as Layer 2 — the extractor must satisfy the [Data sovereignty policy](#data-sovereignty-policy): trust-anchor-B (local/we-control-it) or trust-anchor-C (enterprise contract). The Layer 3 initial release ships with trust-anchor-B candidates only (local Ollama models) because provider-specific cloud extraction integrations (OpenAI, Anthropic, etc.) are real work that is out of scope for Layer 3's experimental release. When a trust-anchor-C extraction path is added in a future release, the sovereignty benchmark must be published alongside it.

**Model sourcing constraint.** Independent of sovereignty, the default extractor must not be a Chinese-sourced model. This rules out `qwen2.5`, `qwen3`, `deepseek`, and similar, regardless of whether they run local or remote.

**Candidate models (all non-Chinese, available on Ollama):**

| Model | Source | License | Size | Primary criterion | Use case |
|---|---|---|---|---|---|
| **`phi3.5`** | Microsoft | MIT | ~3.8B | **Reliable JSON output** | **Default.** Fast, good size/quality balance, handles strict JSON extraction reliably. |
| **`gemma2:2b`** | Google | Gemma License | ~2B | Smallest footprint | Low-resource alternative (Raspberry Pi, low-end VPS). JSON reliability is OK but not as strong as phi3.5 — needs schema enforcement at parse time. |
| **`mistral-nemo`** | Mistral AI (France) | Apache 2.0 | ~12B | Extraction quality | High-quality alternative for users who care more about accuracy than speed. Slower; acceptable for background worker, not write-path. |
| **`llama3.2:3b`** | Meta | Llama Community License | ~3B | — | **Not recommended as default.** License has a >700M MAU clause that's friendly but not as clean as MIT for redistribution. Listed for completeness as a third alternative. |

**Primary default: `phi3.5`.** Most permissive license of the candidates, good size/quality balance, and crucially — reliable at strict JSON output. This matters more than raw extraction quality: some 2–3B models are unreliable at JSON and silently output prose when given structured prompts. When that happens, the pipeline's JSON parser breaks, the entry is marked extraction-failed, and extraction quality drops to zero for that entry. "Reliable at schema" is the first criterion, not the second.

**Configurable from day one:** `AWARENESS_PROPOSITION_EXTRACTION_MODEL=phi3.5` as the default env var. Operators can swap without a code release.

**Entry type handling:**

- **Extract**: note, pattern, context (long-form knowledge)
- **Use as-is**: intention (goal is already atomic)
- **Skip**: alert, status, suppression, preference (structured or ephemeral)

**Risks (expanded from original):**

1. **Extraction quality is the recall ceiling** on the proposition path. Propositions missed by the extractor are invisible to proposition search. Mitigation: keep entry-level embeddings as a recall fallback, always. Dual-index means the worst case for proposition extraction is "no worse than Layer 1+2 search alone."

2. **Non-determinism of LLM extraction.** Two runs of the same entry against the same model produce different propositions. This is fundamental, not a bug:
   - **Tests against extraction quality are flaky by construction.** Regression tests must use fixed seeds *or* accept fuzzy matches against a gold standard.
   - **`extractor_model` drift detection cannot distinguish genuine drift from noise.** A `text_hash` mismatch might mean "model behavior changed" or just "same model, different sampling." Document that retries of the same entry produce different rows and the dedupe mechanism accepts that.
   - **Benchmarks must be averaged over multiple runs** or use temperature=0 to make extraction deterministic. Temperature=0 reduces quality variance but also reduces diversity in the extracted propositions.

3. **Backref staleness on entry edits.** When an entry's content is edited, its existing propositions become stale. Three options, each with downsides:
   - (a) Re-extract on every edit — LLM cost on every write, tight consistency
   - (b) Mark stale, re-extract on next background worker cycle — window of incorrect retrievals, lower write cost
   - (c) Accept indefinite staleness — propositions diverge from source
   
   **Design commitment: option (b).** Mark propositions stale synchronously on entry edit, rely on the background worker to re-extract asynchronously. Acceptable window of "proposition retrieval for this entry may return outdated claims for up to N minutes." Consistent with how embeddings are re-computed today. Entry-level embeddings stay current synchronously so Layer 1+2 search is always accurate even during the proposition staleness window.

4. **Cloud-hosted extractor is a data exfiltration risk if unprotected.** Every entry's content passes through the extractor's context window at write time. For unprotected providers (consumer-tier APIs, public endpoints), this is a privacy violation. For trust-anchor-C providers (enterprise-tier with zero-retention contracts), it is acceptable under the [Data sovereignty policy](#data-sovereignty-policy). For trust-anchor-B providers (local Ollama, user-controlled model endpoints), no third party sees the data.
   
   **Design commitment for Layer 3 initial release: candidates are all trust-anchor-B (local Ollama).** Trust-anchor-C extraction providers (enterprise-tier cloud APIs) are acceptable under the sovereignty policy *in principle* but require provider-specific integration work (client libraries, auth, retries, error handling, telemetry) that is out of scope for Layer 3's experimental release. When cloud extraction is added in a future release, the sovereignty benchmark (see policy section) must be published alongside it, and the consent surface must surface the provider's trust-anchor classification through `get_info`.
   
   Sensitivity routing (`sensitive` tag) applies to extraction the same way it applies to embedding: sensitive entries always route to trust-anchor-B regardless of the global extractor configuration. In a pure-cloud deployment with no trust-anchor-B extractor configured, sensitive entries get no propositions and fall back to entry-level search.

5. **Short/structured entries don't propositionalize well.** A status entry `cpu: 80%, mem: 60%` has no propositions to extract. Skip-list by entry type + content length. Documented above under "Entry type handling."

6. **Write-time LLM cost.** Free for local Ollama (CPU/GPU time), billable per-call if the operator opts into a cloud extractor. Backfill on first enable is the most expensive one-time operation.

7. **LLM drift across model updates.** When an operator upgrades phi3.5 (or swaps to a different model entirely), extraction behavior changes. Store `extractor_model` per row for drift detection; treat it like `text_hash` for stale detection. Operators see an alert in their briefing if extraction model changes and they need to re-backfill.

8. **Extraction prompt is production code.** The prompt we ship has to be maintained, versioned, tested, and occasionally updated as underlying models evolve. Document the prompt in the Layer 3 implementation PR and version it in the same file as the worker.

## Language support

### Built into Postgres (28)

arabic, armenian, basque, catalan, danish, dutch, english, finnish, french, german, greek, hindi, hungarian, indonesian, irish, italian, lithuanian, nepali, norwegian, portuguese, romanian, russian, serbian, spanish, swedish, tamil, turkish, yiddish, plus `simple`.

### Via pgroonga extension (CJK + improved Arabic/Hebrew)

japanese, chinese_simplified, chinese_traditional, korean, hebrew. Postgres base image swap to `groonga/pgroonga:latest-alpine-17`, one Alembic migration to `CREATE EXTENSION pgroonga`.

### Detection

`lingua-py` — high accuracy on short text, pure Python, MIT license, no model downloads beyond the wheel. Uses `detect_language_of()` which applies lingua's own decision logic across vocabulary overlap.

### Fallback chain (write time)

Explicit `entry.data.language` → explicit tool `language` parameter → user preference → auto-detection → `'simple'` → **regconfig validation against `pg_ts_config` cache**. Never breaks a write.

### Unsupported languages

Fall back to `'simple'` and fire a `report_alert` (`alert_id=missing-ts-config-{lang}`). Alert auto-clears once the config exists. This self-dogfoods the awareness alert system — the operator sees the missing-language problem in their own briefing.

### ISO 639-1 at boundaries

API accepts `'en'`, `'ja'`, `'es'`, etc. Server maps to `regconfig` at the boundary via the helpers in `src/mcp_awareness/language.py`. Unknown ISO codes fall back to `'simple'`.

## Migration plan

### Phase 1 — Layer 1: Hybrid retrieval + language column

1. **Phase 1.0 — Schema verification on PG17** (new, Substantive 2): prove the `to_tsvector(regconfig-from-other-column, text)` generated column pattern works on a fresh PG17 database before writing migration code. Trigger-based fallback documented if verification fails.
2. Alembic: add `language regconfig NOT NULL DEFAULT 'simple'` + `tsv` generated column + GIN index + partial index on language
3. Language resolution helpers in `src/mcp_awareness/language.py` *(already landed as foundation)*
4. `lingua-language-detector>=2.0` runtime dependency *(already landed as foundation)*
5. **Write-time regconfig validation** (new, Substantive 3): startup cache of `pg_ts_config`, pre-INSERT validation, fall-through to `'simple'` + alert, cache refresh on alert miss
6. Rewrite `semantic_search` SQL to hybrid CTE (vector + FTS + RRF)
7. Rename `semantic_search` tool → `search`; keep `semantic_search` as deprecated alias for one release
8. `search` tool gains optional `language` parameter; `get_knowledge` gains optional `language` filter
9. Write tools (`remember`, `add_context`, `learn_pattern`) gain optional `language` parameter
10. **Evaluate lifting the 500-char content truncation** in `embeddings.py:212-217` (new): benchmark with full content vs 500-char cap, ship whichever wins
11. Backfill migration: detect language on existing ~700 entries via lingua-py
12. Unsupported-language alert infrastructure
13. FTS weight validation benchmark: confirm A/B/C weighting is correct for awareness data
14. **Sovereignty framework scaffolding** (new):
    - Parse `AWARENESS_LOCAL_ONLY_TAGS` env var (default `"sensitive"`)
    - Read `users.preferences.local_only_tags` at inference call sites
    - Add helper `requires_trust_anchor_b(entry, user_prefs, env_tags) -> bool`
    - Add helper `classify_inference_target(url_or_provider) -> Literal["B", "C", "U"]` with allowlists for known trust-anchor-B (localhost, loopback, LAN ranges, well-known Ollama URLs) and trust-anchor-C (enterprise OpenAI with explicit zero-retention env var set, etc.)
    - Document the framework in user docs
    - **No behavior change in Phase 1** (all current providers are trust-anchor-B, so the routing helper is a no-op). The framework is in place, ready to activate when cloud providers are added.
15. Test coverage: vector branch, FTS branch, fusion, language resolution, regconfig validation, alert firing, sovereignty helpers
16. Dogfooding regression test: the vision doc query surfaces the vision doc *and asserts the FTS branch is what rescued it* (not a false-positive rescue from vector)

### Phase 2 — Layer 2: Multilingual embedding model

1. Run `benchmarks/semantic_search_bench.py` with `intfloat/multilingual-e5-large` against awareness data
2. **English benchmark gate:** match or exceed nomic on English; abort if regression
3. **Cross-lingual smoke tests:** EN→non-EN, non-EN→EN, non-EN→non-EN (all must pass before default swap)
4. If e5-large benchmarks close-but-borderline, switch to `granite-embedding:278m` (no migration path) and re-run gates
5. Alembic migration for vector dimension change (if e5-large or mxbai chosen — not needed for granite/jina/nomic)
6. Docker compose pulls the chosen model on startup
7. **e5 prefix convention** (if e5 chosen): embedding worker applies `"passage: "`, query path applies `"query: "`, tests assert both
8. `backfill_embeddings` mass re-embed wave
9. nomic remains a config fallback
10. README + deployment guide updates

### Phase 3 — pgroonga extension

1. Postgres base image swap: `groonga/pgroonga:latest-alpine-17`
2. Alembic: `CREATE EXTENSION pgroonga`
3. Create text search configurations for japanese, chinese_simplified, chinese_traditional, korean, hebrew
4. LXC install docs for non-Docker production deploys (install pgroonga from Groonga apt repo)
5. Test coverage with CJK sample content
6. **This phase can run in parallel with Phase 1 or Phase 2** — independent concern

### Phase 4 — Layer 3: Proposition extraction (experimental)

1. New `propositions` table + HNSW + GIN indexes + RLS policies
2. Extraction worker (background thread, separate from embedding worker)
3. Extractor prompt + phi3.5 model config + JSON schema enforcement
4. `find` tool returns propositions with backrefs + `AWARENESS_PROPOSITION_EXTRACTION` feature flag
5. `AWARENESS_PROPOSITION_EXTRACTION_MODEL` env var (default `phi3.5`)
6. Backfill on existing entries
7. Benchmark proposition retrieval vs hybrid retrieval alone on sub-document queries
8. Promote to default only after quality validation

### User-facing release packaging

- **v1 release** — Phase 1 + Phase 3 (hybrid retrieval + CJK support). User-facing story: "search is smarter, 35 languages supported lexically."
- **v2 release** — Phase 2 bundled on top of v1. User-facing story: "cross-language unified memory."
- **v3 release** — Phase 4 (experimental). User-facing story: "find the answer, not the document."

## Open questions (remaining after amendment)

1. **Proposition dedupe threshold (Layer 3)** — exact `text_hash` match, or near-duplicate detection via embedding similarity? Defer until Layer 3 implementation.
2. **Multilingual model final choice** — e5-large vs granite-278m depends on the English benchmark outcome. Run the benchmark before committing.

**Resolved in this amendment:**
- RRF k=60: stick with published default ✓
- FTS weights: initial guess + Phase 1 benchmark validation task ✓
- Tool rename: `search` + deprecated `semantic_search` alias ✓
- Per-entry language override surface: optional `language` parameter on write tools ✓
- Layer 3 extraction model: `phi3.5` default, env-configurable, local-runnable constraint ✓
- Sequencing: v1 ships Phase 1+3, v2 adds Phase 2, v3 adds Phase 4 (experimental) ✓
- bge-m3 sourcing concern: model replaced with e5-large or granite ✓

## Risks

### Layer 1 — Low-Medium (upgraded from Low after QA review)

- Generated-column + regconfig-from-another-column pattern needs PG17 verification (Phase 1.0 task). Trigger fallback documented if verification fails.
- Write-time regconfig validation is new code on the write path; must be covered by tests that exercise missing-language scenarios (explicitly).
- `ts_rank_cd` is not BM25 but is sufficient for personal-scale content. Bigger concern at web scale, not here.
- FTS behavior needs language-specific test coverage across at least 3 representative languages.
- lingua-py is a small pure-Python dep, already integrated in the foundation commit.
- **GIN + RLS at multi-tenant scale** (Substantive 5): common-term queries may see GIN matches on all owners' rows, then RLS filters. Correct but wasteful. Personal-scale (current state) doesn't have the problem; revisit if the threat model changes. Noted as future scaling concern.

### Layer 2 — Low-Medium

- Chosen model must match or exceed nomic on English — blocked on benchmark.
- Cross-lingual smoke tests must pass before default swap (new acceptance criterion).
- Re-embed wave briefly degrades production search recall during the migration.
- Larger model → longer embed latency (acceptable for background worker).
- 1024-dim vectors are ~33% larger storage (if e5-large or mxbai chosen; zero change for granite).
- **e5 prefix convention asymmetry** (if e5 chosen): test-enforced symmetry is mandatory. Missing a prefix is silent degradation.

### Layer 3 — Medium-High (upgraded after QA review)

- Extraction quality ceilings recall on that path; mitigation is dual-index with entry-level embeddings as recall fallback.
- **Non-determinism of LLM extraction** — fundamental, not fixable. Tests must use fixed seeds or fuzzy matching.
- **Backref staleness on edits** — committed to mark-stale + background re-extract; accept the staleness window.
- **Cloud extractor privacy hazard** — default must be local-runnable, cloud extractors not supported in default config.
- LLM drift requires per-row `extractor_model` tracking.
- Short/structured entries need skip rules.
- Write-time LLM cost is nonzero (free local, billable cloud).
- Backfill is the most expensive one-time operation.
- Extraction prompt is production code requiring maintenance.

### pgroonga — Low

~150MB image-size increase. New operational dependency for CJK support. Extension install on non-Docker deploys (LXC production) needs documentation and a Groonga apt repo source configured.

## Out of scope

- **#184 response size cap** — Layer 3 mitigates it for the search path, but other read tools still need their own cap. Tracked separately.
- **Federation across language instances** — long-term future work; current design is single-instance multilingual (Option A from the architecture discussion).
- **Custom BM25 implementation** — `ts_rank_cd` is good enough for personal-scale.
- **OpenAI embedding provider (#111)** — parallel work; tracked independently.
- **Cloud-hosted extraction models (Layer 3)** — explicitly out of scope for the initial Layer 3 release.

## Acceptance criteria

### Layer 1 (Phase 1)

- [ ] Schema verification task (Phase 1.0) completed and documented
- [ ] Dogfooding regression query returns the vision doc as top result, **with assertion that the FTS branch is what rescued it**
- [ ] Unsupported-language write fires a `report_alert` and falls back to `'simple'` without failing the INSERT
- [ ] No regression on pure-English recall vs the current nomic-based search
- [ ] `search` tool runs vector + FTS + RRF in a single CTE
- [ ] `language` parameter on `search` and `get_knowledge`; optional override on write tools
- [ ] `semantic_search` tool name kept as deprecated alias for one release
- [ ] FTS weight validation benchmark documents the A/B/C choice
- [ ] Backfill re-detects language on all existing entries without loss
- [ ] Test coverage across vector branch, FTS branch, fusion layer, language resolution, regconfig validation, alert firing
- [ ] 500-char content truncation investigation completed with a committed direction (lift or keep)
- [ ] Sovereignty framework scaffolding (env var + user preference parsing, routing helper, trust-anchor classification helper) implemented and unit-tested, even though all Layer 1+Phase 1 providers are trust-anchor-B (no-op at this stage)
- [ ] `get_info` tool (#235) exposes extraction and embedding provider trust-anchor classification
- [ ] First-time-seen briefing notice fires on provider configuration changes
- [ ] Unprotected provider detection fires a persistent briefing warning (no unprotected providers in Layer 1+Phase 1, but the detection code is tested with a test fixture)

### Layer 2 (Phase 2)

- [ ] Benchmark report: chosen model vs nomic-embed-text on English (recall@k, latency, storage)
- [ ] **Cross-lingual smoke tests pass**: EN→non-EN, non-EN→EN, non-EN→non-EN-same-lang
- [ ] Alembic migration for vector dimension change (if applicable to the chosen model)
- [ ] Docker compose pulls the chosen model on startup
- [ ] (If e5 family) embedding worker applies `"passage: "` prefix; query path applies `"query: "` prefix; tests assert both
- [ ] `backfill_embeddings` re-embeds all existing data with the new model
- [ ] nomic remains a valid config fallback
- [ ] Japanese query returns English entries on the same topic
- [ ] English query returns Japanese entries on the same topic
- [ ] Spanish query returns Spanish entries on the same topic
- [ ] Documentation update (README + deployment guide)
- [ ] If any trust-anchor-C embedding path ships as part of Phase 2: sovereignty benchmark published (sensitivity-on vs sensitivity-off quality comparison) before the feature ships; per-entry sensitivity routing honors the sensitive tag in the embedding worker; consent surface (get_info + first-time notice) reflects the new provider

### Layer 3 (Phase 4, experimental)

- [ ] `propositions` table + HNSW + GIN + RLS policies
- [ ] Background extraction worker
- [ ] Extractor prompt + `phi3.5` default + `AWARENESS_PROPOSITION_EXTRACTION_MODEL` env var
- [ ] `find` tool returns propositions with entry backrefs
- [ ] Dedupe by `text_hash` on edit; propositions surviving an edit are preserved
- [ ] Entry-level embeddings retained as recall fallback
- [ ] Feature flag (`AWARENESS_PROPOSITION_EXTRACTION=true`) gating
- [ ] Trust-anchor-B default extractor (phi3.5 or equivalent local Ollama model); no trust-anchor-C providers in the initial release candidate list
- [ ] Sensitivity routing honored by extraction worker: sensitive entries get no proposition extraction if the configured extractor is not trust-anchor-B
- [ ] Benchmark: proposition retrieval improves recall on sub-document queries vs Layer 1+2 alone
- [ ] Smoke test: "retire at 62" query returns the matching proposition, not the full retirement-planning entry
- [ ] If any trust-anchor-C extraction path ships in a later release: sovereignty benchmark published, consent surface updated, sensitivity routing verified

## Merge checklist (after amended #241 merges)

- [ ] Close #195 with a comment pointing to this design doc and issues #238/#239/#240
- [ ] Update issue #239 body: drop bge-m3, add e5-large/granite candidates + e5 prefix convention + cross-lingual smoke tests
- [ ] Update issue #240 body: drop qwen, add phi3.5 default + local-runnable constraint + non-determinism/staleness risks

## References

- Dogfooding finding — awareness entry `06f85fd0` (2026-03-24)
- 500-char truncation source — `src/mcp_awareness/embeddings.py:212-217`
- Cormack, Clarke, Büttcher, *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods* (2009)
- Chen et al., *Dense X Retrieval: What Retrieval Granularity Should We Use?* (2023) — https://arxiv.org/abs/2312.06648
- Wang et al., *Text Embeddings by Weakly-Supervised Contrastive Pre-training* (E5 paper, 2022) — https://arxiv.org/abs/2212.03533
- IBM Granite embedding — https://www.ibm.com/granite
- Postgres full-text search docs — https://www.postgresql.org/docs/current/textsearch.html
- Postgres text search configurations — https://www.postgresql.org/docs/current/textsearch-configuration.html
- pgroonga — https://pgroonga.github.io/
- lingua-py — https://github.com/pemistahl/lingua-py
