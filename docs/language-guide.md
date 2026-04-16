<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Language support guide

New in v0.17.0. This guide explains how mcp-awareness handles
multilingual content — per-entry language detection, language-specific
full-text search, and vector search across languages.

## Contents

### Adding and maintaining data
- [How it works](#how-it-works) — the language resolution chain
- [Supported FTS languages](#supported-fts-languages) — 28 Postgres regconfigs
- [Embedding model languages](#embedding-model-languages) — 12 natively trained + ~100 via XLM-RoBERTa
- [Writing in a specific language](#writing-in-a-specific-language) — explicit, auto-detected, override
- [Unsupported-language alerts](#unsupported-language-alerts) — demand signals for new languages

### Searching and retrieving data
- [Querying by language](#querying-by-language) — filter by language
- [Hybrid search across languages](#hybrid-search-across-languages) — how vector + FTS work together

### Operations
- [Deployment notes](#deployment-notes) — lingua install, backfill, regconfig cache
- [What's next](#whats-next) — Phases 2–3, data sovereignty
- [Reference](#reference)

---

## How it works

```mermaid
flowchart TD
    A["Entry written"] --> B{language\nparam?}
    B -- "Yes (e.g. 'fr')" --> C["Map ISO → regconfig\n(fr → french)"]
    B -- No --> D{lingua\ninstalled?}
    D -- Yes --> E["Auto-detect language"]
    E --> F{Regconfig\nexists?}
    F -- Yes --> C
    F -- No --> G["Use 'simple'\n+ fire unsupported-language alert"]
    D -- No --> H["Use 'simple'"]
    C --> I["Store entry with\nlanguage-specific FTS"]
    G --> I
    H --> I
```

Every entry has a `language` column that stores a Postgres
[regconfig](https://www.postgresql.org/docs/17/textsearch-configuration.html)
name (e.g., `english`, `french`, `german`). This regconfig controls
how full-text search (FTS) tokenizes and stems that entry's text.

The language is resolved at write time through this chain:

1. **Explicit parameter.** Write tools (`remember`, `add_context`,
   `learn_pattern`, `remind`, `register_schema`, `create_record`)
   accept an optional `language` parameter. Pass an ISO 639-1 code
   (e.g., `"fr"` for French) to force a specific language.

2. **Auto-detection.** If no language is provided,
   [lingua-py](https://github.com/pemistahl/lingua-py) analyzes the
   entry's text (description + content) and returns an ISO code. That
   code is mapped to a Postgres regconfig.

3. **Fallback.** If lingua is not installed, or the text is too short
   for reliable detection, or lingua detects a language without a
   Postgres regconfig, the entry is stored with `simple` — a
   language-agnostic config that tokenizes on whitespace without
   stemming.

This means entries are always searchable via FTS. The question is
whether they get language-specific stemming (better recall for
inflected forms) or the `simple` fallback (exact-token matching only).

---

## Supported FTS languages

28 languages have a Postgres snowball regconfig mapped in
mcp-awareness. These get **language-specific stemming** in full-text
search — inflected forms like "serveurs" match "serveur":

| ISO code | Regconfig | | ISO code | Regconfig |
|----------|------------|-|----------|-----------|
| `ar` | arabic | | `it` | italian |
| `ca` | catalan | | `lt` | lithuanian |
| `da` | danish | | `ne` | nepali |
| `de` | german | | `nl` | dutch |
| `el` | greek | | `no` | norwegian |
| `en` | english | | `pt` | portuguese |
| `es` | spanish | | `ro` | romanian |
| `eu` | basque | | `ru` | russian |
| `fi` | finnish | | `sr` | serbian |
| `fr` | french | | `sv` | swedish |
| `ga` | irish | | `ta` | tamil |
| `hi` | hindi | | `tr` | turkish |
| `hu` | hungarian | | `yi` | yiddish |
| `hy` | armenian | | | |
| `id` | indonesian | | | |

Languages not in this list (e.g., Chinese, Japanese, Korean, Hebrew)
fall back to `simple` for FTS. Phase 3 of the hybrid retrieval design
covers non-Western FTS support via Postgres extensions (`pgroonga`,
`zhparser`, etc.), but that hasn't shipped yet.

---

## Embedding model languages

The default embedding model
([granite-embedding:278m](https://huggingface.co/ibm-granite/granite-embedding-278m-multilingual))
is a multilingual model trained on **12 languages**:

| Language | Language | Language |
|----------|----------|----------|
| Arabic | English | Japanese |
| Chinese | French | Korean |
| Czech | German | Dutch |
| Italian | Portuguese | Spanish |

The model is built on XLM-RoBERTa, which covers approximately **100
languages** in its vocabulary. Languages outside the 12 training
languages still produce usable embeddings — cross-lingual retrieval
will work, just with lower recall than for the trained set.

**Why this matters even without FTS stemming.** Languages like
Japanese, Korean, and Chinese have no Postgres regconfig in our
mapping — FTS falls back to `simple` (whitespace tokenization, no
stemming). But the embedding model *was* trained on these languages,
so the **vector branch of hybrid search still works well for them**.
A Japanese query will find Japanese entries via vector similarity
even though FTS can't stem the text. This is why enabling the
embedding provider is especially valuable for multilingual use.

| Language | FTS stemming | Vector search |
|----------|:---:|:---:|
| English, French, German, ... (28 FTS languages) | ✓ | ✓ |
| Japanese, Korean, Chinese, Czech (in embedding model, no regconfig) | ✗ (simple fallback) | ✓ |
| Other XLM-RoBERTa languages (not in embedding training set) | ✗ (simple fallback) | partial |
| Languages outside XLM-RoBERTa vocabulary | ✗ (simple fallback) | ✗ |

---

## Writing in a specific language

### Explicit language

```
remember(
    description="Le serveur NAS est dans le placard du sous-sol.",
    source="personal",
    tags=["infra", "nas"],
    language="fr"
)
```

The entry is stored with `language = 'french'`. FTS will stem
French inflections correctly — a search for "serveurs" will match
"serveur".

### Auto-detected language

```
remember(
    description="Der NAS-Server steht im Kellerschrank.",
    source="personal",
    tags=["infra", "nas"]
)
```

With lingua installed, this auto-detects as German (`de`) → stored
as `german` regconfig. Without lingua, it falls back to `simple`.

### Override on update

```
update_entry(
    entry_id="<entry-id>",
    language="de"
)
```

If auto-detection guessed wrong (or the entry was written before
lingua was installed), you can update the language explicitly.

---

## Unsupported-language alerts

When you write an entry and lingua detects a language that has no
Postgres regconfig (e.g., Chinese, Japanese, Korean), mcp-awareness:

1. Stores the entry with `language = 'simple'` (FTS still works,
   just without stemming; vector search still works if the language
   is in the embedding model's training set).
2. Fires an **info-level structural alert** with the tag
   `unsupported-language-{iso}` (e.g., `unsupported-language-zh`).

These alerts are upserted per language — you'll see at most one
alert per unsupported language, not one per entry. They serve as a
demand signal: if `unsupported-language-ja` fires, the operator
knows users are writing in Japanese and should consider installing
Phase 3 language support when it ships.

You can find current unsupported-language alerts via:

```
search(query="unsupported language", entry_type="alert")
```

Or browse all active alerts with `get_alerts()` and look for alert IDs
starting with `unsupported-language-`.

---

## Querying by language

### Filter `get_knowledge` to a single language

```
get_knowledge(tags=["infra"], language="fr")
```

Returns only French-language entries matching the tag filter. The
`language` parameter accepts an ISO 639-1 code (`"fr"`) or the
special value `"simple"` (entries with no detected language).

---

## Hybrid search across languages

```mermaid
flowchart TD
    Q["search(query, ...)"] --> P["Resolve query language"]
    P --> V["Vector branch\n(HNSW index)"]
    P --> F["FTS branch\n(GIN index)"]

    V --> V1["Embed query text\n(granite-embedding)"]
    V1 --> V2["Cosine similarity\nagainst entry embeddings"]
    V2 --> V3["Top N by vector score"]

    F --> F1["Parse query as ts_query\n(using query's regconfig)"]
    F1 --> F2["Match against entry tsvector\n(per-entry language stemming)"]
    F2 --> F3["Rank by ts_rank_cd"]

    V3 --> RRF["Reciprocal Rank Fusion\n(k=60)"]
    F3 --> RRF
    RRF --> R["Merged results\n(best of both branches)"]

    style V fill:#e8f4e8
    style F fill:#e8e8f4
    style RRF fill:#f4e8e8
```

```
search(query="NAS server basement", tags=["infra"])
```

The `search` tool runs two branches in parallel:

- **Vector branch** (green above) — if an embedding provider is
  configured, embeds the query and compares it against stored entry
  embeddings via cosine similarity. This is **language-agnostic** —
  the multilingual embedding model handles cross-lingual matching
  internally. A French query can find English entries and vice versa.
- **FTS branch** (blue above) — parses the query as a Postgres
  `ts_query` using the query's resolved language for stemming, then
  matches against entries' `tsv` column (which uses each entry's own
  language for stemming). This is **language-specific** — French
  stemming matches French entries, English matches English.

Results from both branches are fused via **Reciprocal Rank Fusion**
(red above, k=60). RRF doesn't care about absolute scores — it
combines rankings, so an entry that ranks highly in *either* branch
surfaces in the final results.

### What this means in practice

| Scenario | Vector branch | FTS branch | Result |
|----------|:---:|:---:|--------|
| Same-language query (e.g., English → English) | ✓ strong | ✓ strong | Best recall — both branches contribute |
| Cross-language query (e.g., French → English) | ✓ strong | ✗ miss | Vector carries the match; still works |
| Rare identifier or exact term | ✗ weak | ✓ strong | FTS rescues the match |
| Long document (>500 chars) | ✗ partial (first 500 chars embedded) | ✓ full text indexed | FTS rescues buried terms |
| No embedding provider configured | ✗ skipped | ✓ only branch | FTS-only mode, still functional |

**Graceful degradation:** if no embedding provider is configured,
search runs FTS only. If an entry has no embedding (new entry,
backfill not yet run), it still participates in FTS. If the query
text is too short for meaningful FTS (stop words only), the vector
branch carries. Each branch compensates for the other's gaps.

---

## Deployment notes

### Installing lingua

lingua-py is an optional dependency. Without it, all entries get
`language = 'simple'` (still searchable, just without stemming).

```bash
pip install lingua-language-detector
```

Or, if using the Docker image, lingua is included by default.

### Language backfill on upgrade

When upgrading to v0.17.0+, two Alembic migrations run:

1. **Schema migration** — adds `language` and `tsv` columns (fast,
   DDL only).
2. **Language backfill** — runs lingua detection on all existing
   entries and updates the `language` column. This is a one-time data
   migration:
   - lingua's first call loads ~300 MB of n-gram models (multi-second
     startup cost)
   - Each existing entry is processed for language detection
   - If lingua is not installed, the backfill is skipped and entries
     remain as `simple`

After backfill, existing entries participate in language-specific FTS
immediately — no re-indexing needed (the `tsv` column is a generated
column that updates automatically when `language` changes).

### Regconfig validation

At startup, `PostgresStore` caches valid Postgres regconfig names from
`pg_ts_config`. If a write provides a regconfig that doesn't exist in
the server's Postgres (e.g., a third-party config was uninstalled),
the entry falls back to `simple` with a one-time cache refresh. This
prevents INSERT failures from invalid `language` values reaching the
generated `tsv` column.

---

## What's next

- **Phase 2: Cross-lingual vector model** — swap the embedding model
  to one with stronger cross-lingual properties (e.g., multilingual-e5
  or similar). Tracked at
  [#239](https://github.com/cmeans/mcp-awareness/issues/239).
- **Phase 3: Non-Western language support** — install Postgres
  extensions for CJK, Hebrew, and other languages that need
  non-snowball tokenizers. Driven by unsupported-language alerts.
- **Data sovereignty framework** — governs where content is sent for
  inference, required before cloud embedding providers ship as
  defaults.

---

## Reference

- [Hybrid Retrieval + Multilingual design](design/hybrid-retrieval-multilingual.md)
  — full design doc covering Layers 1–3, data sovereignty, and the
  dilution-bug root cause.
- [Data Dictionary](data-dictionary.md) — `language` and `tsv`
  column definitions.
- [Postgres text search configs](https://www.postgresql.org/docs/17/textsearch-configuration.html)
  — how regconfigs work.
- [lingua-py](https://github.com/pemistahl/lingua-py) — the
  language detection library.
- [granite-embedding:278m](https://huggingface.co/ibm-granite/granite-embedding-278m-multilingual)
  — the default embedding model (IBM, multilingual, 768 dimensions).
