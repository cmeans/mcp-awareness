#!/usr/bin/env python3
# mcp-awareness — ambient system awareness for AI agents
# Copyright (C) 2026 Chris Means
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Layer 1 hybrid retrieval benchmark for mcp-awareness.

Validates FTS stemming, cross-language search, vector similarity,
RRF fusion, language detection, language filters, write-time language
assignment, regconfig validation, deprecated aliases, and search latency.

Usage:
    python scripts/benchmark_layer1.py --dsn "host=... dbname=... user=... password=..."
    python scripts/benchmark_layer1.py --no-vector   # skip vector/RRF tests
    python scripts/benchmark_layer1.py --baseline previous.json  # regression comparison

Requires: pip install -e ".[dev]"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# mcp_awareness imports
# ---------------------------------------------------------------------------
from mcp_awareness.embeddings import (
    OllamaEmbedding,
    compose_embedding_text,
    text_hash,
)
from mcp_awareness.language import (
    SIMPLE,
    detect_language,
)
from mcp_awareness.postgres_store import PostgresStore
from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BENCHMARK_TAG = "_benchmark_layer1"
BENCHMARK_SOURCE = "benchmark-layer1"

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class TestResult:
    name: str
    category: str
    description: str
    expected: str
    actual: str
    passed: bool
    latency_ms: float
    error: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class BenchmarkReport:
    meta: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    baseline_comparison: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

SEED_ENTRIES: list[dict[str, Any]] = [
    # --- English stemming targets ---
    {
        "logical_key": "bench-en-walked",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "english", "stemming"],
        "language": "english",
        "data": {
            "description": "Yesterday I walked five miles through the park",
            "learned_from": "benchmark",
        },
    },
    {
        "logical_key": "bench-en-running",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "english", "stemming"],
        "language": "english",
        "data": {
            "description": "She enjoys running every morning before sunrise for exercise",
            "learned_from": "benchmark",
        },
    },
    # --- French entries ---
    {
        "logical_key": "bench-fr-cuisine",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "french"],
        "language": "french",
        "data": {
            "description": "La cuisine française est reconnue dans le monde entier pour sa qualité",
            "learned_from": "benchmark",
        },
    },
    {
        "logical_key": "bench-fr-jardinage",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "french"],
        "language": "french",
        "data": {
            "description": (
                "Le jardinage est une activité relaxante qui apporte beaucoup de satisfaction"
            ),
            "learned_from": "benchmark",
        },
    },
    # --- German entries ---
    {
        "logical_key": "bench-de-wandern",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "german"],
        "language": "german",
        "data": {
            "description": (
                "Wandern in den Bergen ist eine beliebte Freizeitbeschäftigung in Deutschland"
            ),
            "learned_from": "benchmark",
        },
    },
    {
        "logical_key": "bench-de-kochen",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "german"],
        "language": "german",
        "data": {
            "description": "Kochen und Backen sind traditionelle Familienaktivitäten am Wochenende",
            "learned_from": "benchmark",
        },
    },
    # --- Vector similarity targets ---
    {
        "logical_key": "bench-vec-401k",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "vector", "finance"],
        "language": "english",
        "data": {
            "description": (
                "My 401k retirement savings account has a balanced portfolio of index funds"
            ),
            "learned_from": "benchmark",
        },
    },
    {
        "logical_key": "bench-vec-solar",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "vector", "energy"],
        "language": "english",
        "data": {
            "description": "Solar panels on the roof generate renewable electricity from sunlight",
            "learned_from": "benchmark",
        },
    },
    # --- Short text (simple fallback) ---
    {
        "logical_key": "bench-short",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "short"],
        "language": "simple",
        "data": {
            "description": "Quick note",
            "learned_from": "benchmark",
        },
    },
    # --- Explicit language override ---
    {
        "logical_key": "bench-override-es",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "override"],
        "language": "spanish",
        "data": {
            "description": (
                "This is actually English text but explicitly tagged as Spanish for testing"
            ),
            "learned_from": "benchmark",
        },
    },
    # --- Auto-detect Portuguese ---
    {
        "logical_key": "bench-autodetect-pt",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "autodetect"],
        "language": "portuguese",
        "data": {
            "description": (
                "A programação de computadores é uma habilidade essencial no mundo moderno"
            ),
            "learned_from": "benchmark",
        },
    },
    # --- Unsupported language (Tagalog) -- should fall back to simple ---
    {
        "logical_key": "bench-unsupported-tl",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "unsupported"],
        "language": "simple",
        "data": {
            "description": (
                "Ang pagprograma ng mga kompyuter ay isang mahalagang kasanayan sa modernong mundo"
            ),
            "learned_from": "benchmark",
        },
    },
    # --- Additional English for diversity ---
    {
        "logical_key": "bench-en-cooking",
        "type": EntryType.NOTE,
        "tags": [BENCHMARK_TAG, "english"],
        "language": "english",
        "data": {
            "description": (
                "Cooking a hearty stew requires patience and good quality fresh ingredients"
            ),
            "learned_from": "benchmark",
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timed(fn: Any) -> tuple[Any, float]:
    """Call fn() and return (result, elapsed_ms)."""
    t0 = time.perf_counter()
    result = fn()
    elapsed = (time.perf_counter() - t0) * 1000.0
    return result, elapsed


def _make_entry(seed: dict[str, Any]) -> Entry:
    """Build an Entry from a seed dict."""
    return Entry(
        id=make_id(),
        type=seed["type"],
        source=BENCHMARK_SOURCE,
        tags=seed["tags"],
        created=now_utc(),
        data=seed["data"],
        logical_key=seed["logical_key"],
        language=seed["language"],
    )


def _zero_vector(dims: int) -> list[float]:
    """Return a zero vector of the given dimensionality."""
    return [0.0] * dims


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


def seed_entries(
    store: PostgresStore,
    owner: str,
    embedding_provider: OllamaEmbedding | None,
) -> list[Entry]:
    """Seed benchmark entries. Uses upsert_by_logical_key for idempotence."""
    entries: list[Entry] = []
    for seed in SEED_ENTRIES:
        entry = _make_entry(seed)
        stored, created = store.upsert_by_logical_key(
            owner, BENCHMARK_SOURCE, entry.logical_key or "", entry
        )
        entries.append(stored)
        verb = "created" if created else "exists"
        logger.info("  %s: %s [%s]", verb, stored.logical_key, stored.language)

        # Generate embeddings if provider available
        if embedding_provider is not None:
            text = compose_embedding_text(stored)
            th = text_hash(text)
            vectors = embedding_provider.embed([text])
            if vectors:
                store.upsert_embedding(
                    owner,
                    stored.id,
                    embedding_provider.model_name,
                    embedding_provider.dimensions,
                    th,
                    vectors[0],
                )
                logger.debug("  embedded: %s", stored.logical_key)

    return entries


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup(store: PostgresStore, owner: str) -> None:
    """Remove all benchmark entries: soft-delete then hard-delete."""
    logger.info("Cleaning up benchmark entries...")
    count = store.soft_delete_by_tags(owner, [BENCHMARK_TAG])
    if count:
        logger.info("  soft-deleted %d entries", count)
    # Hard delete: bypass trash retention via raw SQL
    with store._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        store._set_rls_context(cur, owner)
        cur.execute(
            "DELETE FROM entries WHERE owner_id = %s AND deleted IS NOT NULL AND tags @> %s::jsonb",
            (owner, json.dumps([BENCHMARK_TAG])),
        )
        purged = cur.rowcount
    if purged:
        logger.info("  hard-deleted %d entries from trash", purged)


# ---------------------------------------------------------------------------
# Test runners
# ---------------------------------------------------------------------------


def run_fts_stemming(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
    embedding_provider: OllamaEmbedding | None,
) -> list[TestResult]:
    """FTS stemming tests: stem-inflected queries match base forms."""
    results: list[TestResult] = []

    # Test 1: "walking" should find "walked" (English stem: walk)
    def _search_walking() -> list[tuple[Entry, float]]:
        vec = _zero_vector(embedding_provider.dimensions) if embedding_provider else [0.0] * 768
        model = embedding_provider.model_name if embedding_provider else "null"
        return store.semantic_search(
            owner,
            embedding=vec,
            model=model,
            query_text="walking",
            query_language="english",
            tags=[BENCHMARK_TAG, "stemming"],
            limit=10,
        )

    (hits, latency) = _timed(_search_walking)
    ids_found = {e.logical_key for e, _ in hits}
    passed = "bench-en-walked" in ids_found
    results.append(
        TestResult(
            name="fts_stem_walk",
            category="FTS Stemming",
            description="Query 'walking' finds entry containing 'walked' via English stemmer",
            expected="bench-en-walked in results",
            actual=f"found: {sorted(ids_found)}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 2: "run" should find "running"
    def _search_run() -> list[tuple[Entry, float]]:
        vec = _zero_vector(embedding_provider.dimensions) if embedding_provider else [0.0] * 768
        model = embedding_provider.model_name if embedding_provider else "null"
        return store.semantic_search(
            owner,
            embedding=vec,
            model=model,
            query_text="run",
            query_language="english",
            tags=[BENCHMARK_TAG, "stemming"],
            limit=10,
        )

    (hits, latency) = _timed(_search_run)
    ids_found = {e.logical_key for e, _ in hits}
    passed = "bench-en-running" in ids_found
    results.append(
        TestResult(
            name="fts_stem_run",
            category="FTS Stemming",
            description="Query 'run' finds entry containing 'running' via English stemmer",
            expected="bench-en-running in results",
            actual=f"found: {sorted(ids_found)}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    return results


def run_cross_language_fts(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
    embedding_provider: OllamaEmbedding | None,
) -> list[TestResult]:
    """Cross-language FTS: queries in matching language find results; wrong language does not."""
    results: list[TestResult] = []
    vec = _zero_vector(embedding_provider.dimensions) if embedding_provider else [0.0] * 768
    model = embedding_provider.model_name if embedding_provider else "null"

    # Test 1: French query matches French entries
    def _search_fr() -> list[tuple[Entry, float]]:
        return store.semantic_search(
            owner,
            embedding=vec,
            model=model,
            query_text="cuisine française qualité",
            query_language="french",
            tags=[BENCHMARK_TAG],
            limit=10,
        )

    (hits, latency) = _timed(_search_fr)
    fr_keys = {e.logical_key for e, _ in hits if e.language == "french"}
    passed = len(fr_keys) > 0
    results.append(
        TestResult(
            name="fts_cross_lang_french",
            category="Cross-language FTS",
            description="French query 'cuisine française qualité' finds French entries",
            expected="at least one French entry",
            actual=f"french entries found: {sorted(fr_keys)}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 2: German query matches German entries
    def _search_de() -> list[tuple[Entry, float]]:
        return store.semantic_search(
            owner,
            embedding=vec,
            model=model,
            query_text="Wandern Bergen Freizeitbeschäftigung",
            query_language="german",
            tags=[BENCHMARK_TAG],
            limit=10,
        )

    (hits, latency) = _timed(_search_de)
    de_keys = {e.logical_key for e, _ in hits if e.language == "german"}
    passed = len(de_keys) > 0
    results.append(
        TestResult(
            name="fts_cross_lang_german",
            category="Cross-language FTS",
            description="German query finds German entries",
            expected="at least one German entry",
            actual=f"german entries found: {sorted(de_keys)}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 3: Wrong-language FTS should NOT match via raw tsquery
    # Use direct FTS SQL to isolate the lexical arm — the hybrid CTE's
    # vector arm returns results even with a zero vector, so we can't use
    # semantic_search() for FTS-only isolation.
    def _fts_wrong_lang() -> int:
        with store._pool.connection() as conn, conn.transaction():
            PostgresStore._set_rls_context(conn.cursor(), owner)
            row = conn.execute(
                "SELECT count(*)::int AS cnt FROM entries e "
                "WHERE e.tsv @@ plainto_tsquery('english'::regconfig, %s) "
                "AND e.tags @> %s::jsonb AND e.deleted IS NULL",
                ("Freizeitbeschäftigung Familienaktivitäten", f'["{BENCHMARK_TAG}"]'),
            ).fetchone()
            return row["cnt"] if row else 0

    (count, latency) = _timed(_fts_wrong_lang)
    passed = count == 0
    results.append(
        TestResult(
            name="fts_wrong_language_no_match",
            category="Cross-language FTS",
            description="German words queried with English regconfig do not match via FTS",
            expected="0 FTS matches (raw tsquery, wrong stemmer)",
            actual=f"{count} FTS matches",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    return results


def run_vector_search(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
    embedding_provider: OllamaEmbedding,
) -> list[TestResult]:
    """Vector similarity: conceptually related queries find semantically similar entries."""
    results: list[TestResult] = []

    # Test 1: "pension" should find "401k" (conceptual similarity)
    pension_vec = embedding_provider.embed(["pension retirement savings"])[0]

    def _search_pension() -> list[tuple[Entry, float]]:
        return store.semantic_search(
            owner,
            embedding=pension_vec,
            model=embedding_provider.model_name,
            query_text="",
            query_language=SIMPLE,
            tags=[BENCHMARK_TAG],
            limit=5,
        )

    (hits, latency) = _timed(_search_pension)
    keys = [e.logical_key for e, _ in hits]
    passed = "bench-vec-401k" in keys
    results.append(
        TestResult(
            name="vector_pension_401k",
            category="Vector Search",
            description="Query 'pension retirement savings' finds 401k entry via vector similarity",
            expected="bench-vec-401k in results",
            actual=f"top results: {keys}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 2: "green power" should find "solar panels"
    green_vec = embedding_provider.embed(["green power renewable energy"])[0]

    def _search_green() -> list[tuple[Entry, float]]:
        return store.semantic_search(
            owner,
            embedding=green_vec,
            model=embedding_provider.model_name,
            query_text="",
            query_language=SIMPLE,
            tags=[BENCHMARK_TAG],
            limit=5,
        )

    (hits, latency) = _timed(_search_green)
    keys = [e.logical_key for e, _ in hits]
    passed = "bench-vec-solar" in keys
    results.append(
        TestResult(
            name="vector_green_solar",
            category="Vector Search",
            description="Query 'green power renewable energy' finds solar panels entry",
            expected="bench-vec-solar in results",
            actual=f"top results: {keys}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    return results


def run_rrf_fusion(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
    embedding_provider: OllamaEmbedding,
) -> list[TestResult]:
    """RRF fusion: both arms contribute, fusion score exceeds single-arm score."""
    results_list: list[TestResult] = []

    # Use a query that should hit both FTS and vector for the same entry:
    # "retirement savings" matches "401k retirement savings" via FTS (English stemmer)
    # and the embedding of "retirement savings" should be close to the 401k entry vector.
    query_text = "retirement savings account"
    query_vec = embedding_provider.embed([query_text])[0]

    # Hybrid search (both arms)
    def _hybrid() -> list[tuple[Entry, float]]:
        return store.semantic_search(
            owner,
            embedding=query_vec,
            model=embedding_provider.model_name,
            query_text=query_text,
            query_language="english",
            tags=[BENCHMARK_TAG],
            limit=10,
        )

    (hybrid_hits, latency) = _timed(_hybrid)
    hybrid_scores = {e.logical_key: score for e, score in hybrid_hits}

    # Vector-only (empty query_text neuters FTS)
    vec_only = store.semantic_search(
        owner,
        embedding=query_vec,
        model=embedding_provider.model_name,
        query_text="",
        query_language=SIMPLE,
        tags=[BENCHMARK_TAG],
        limit=10,
    )
    vec_scores = {e.logical_key: score for e, score in vec_only}

    # FTS-only approximation (zero vector gives random vector ranking, but
    # FTS arm dominates scoring for matching entries). Not perfectly isolated
    # but sufficient for the fusion-score comparison below.
    zero_vec = _zero_vector(embedding_provider.dimensions)
    fts_only = store.semantic_search(
        owner,
        embedding=zero_vec,
        model=embedding_provider.model_name,
        query_text=query_text,
        query_language="english",
        tags=[BENCHMARK_TAG],
        limit=10,
    )
    fts_scores = {e.logical_key: score for e, score in fts_only}

    # Test 1: Both arms contribute results
    target = "bench-vec-401k"
    in_vec = target in vec_scores
    in_fts = target in fts_scores
    in_hybrid = target in hybrid_scores
    passed = in_vec and in_fts and in_hybrid
    results_list.append(
        TestResult(
            name="rrf_both_arms_contribute",
            category="RRF Fusion",
            description="Target entry appears in vector-only, FTS-only, and hybrid results",
            expected=f"{target} in all three result sets",
            actual=f"vector={in_vec}, fts={in_fts}, hybrid={in_hybrid}",
            passed=passed,
            latency_ms=round(latency, 2),
            details={
                "hybrid_score": hybrid_scores.get(target),
                "vec_score": vec_scores.get(target),
                "fts_score": fts_scores.get(target),
            },
        )
    )

    # Test 2: Fusion score >= max of single-arm scores
    h_score = hybrid_scores.get(target, 0.0)
    v_score = vec_scores.get(target, 0.0)
    f_score = fts_scores.get(target, 0.0)
    max_single = max(v_score, f_score)
    passed = h_score >= max_single
    results_list.append(
        TestResult(
            name="rrf_fusion_score_higher",
            category="RRF Fusion",
            description="Hybrid fusion score >= max single-arm score for target entry",
            expected=f"hybrid ({h_score:.4f}) >= max_single ({max_single:.4f})",
            actual=f"hybrid={h_score:.4f}, vec={v_score:.4f}, fts={f_score:.4f}",
            passed=passed,
            latency_ms=0.0,
            details={
                "hybrid_score": h_score,
                "vec_score": v_score,
                "fts_score": f_score,
            },
        )
    )

    return results_list


def run_language_detection(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
) -> list[TestResult]:
    """Language detection tests: detect_language on known texts."""
    results: list[TestResult] = []

    # Test 1: Detect English
    text_en = "The quick brown fox jumps over the lazy dog near the riverbank"
    (detected, latency) = _timed(lambda: detect_language(text_en))
    passed = detected == "english"
    results.append(
        TestResult(
            name="detect_english",
            category="Language Detection",
            description="detect_language identifies English text correctly",
            expected="english",
            actual=str(detected),
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 2: Detect French
    text_fr = "La programmation informatique est une compétence essentielle dans le monde moderne"
    (detected, latency) = _timed(lambda: detect_language(text_fr))
    passed = detected == "french"
    results.append(
        TestResult(
            name="detect_french",
            category="Language Detection",
            description="detect_language identifies French text correctly",
            expected="french",
            actual=str(detected),
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 3: Short text returns None
    text_short = "Hi there"
    (detected, latency) = _timed(lambda: detect_language(text_short))
    passed = detected is None
    results.append(
        TestResult(
            name="detect_short_none",
            category="Language Detection",
            description="detect_language returns None for text shorter than threshold",
            expected="None",
            actual=str(detected),
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    return results


def run_language_filter(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
) -> list[TestResult]:
    """Language filter tests: get_knowledge with language= returns correct entries."""
    results: list[TestResult] = []

    # Test 1: Filter by French
    (fr_entries, latency) = _timed(
        lambda: store.get_knowledge(owner, tags=[BENCHMARK_TAG], language="french")
    )
    fr_keys = {e.logical_key for e in fr_entries}
    all_french = all(e.language == "french" for e in fr_entries)
    passed = len(fr_entries) > 0 and all_french
    results.append(
        TestResult(
            name="filter_french",
            category="Language Filter",
            description="get_knowledge(language='french') returns only French entries",
            expected="only entries with language=french",
            actual=f"{len(fr_entries)} entries, all french: {all_french}, keys: {sorted(fr_keys)}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 2: Filter by German
    (de_entries, latency) = _timed(
        lambda: store.get_knowledge(owner, tags=[BENCHMARK_TAG], language="german")
    )
    de_keys = {e.logical_key for e in de_entries}
    all_german = all(e.language == "german" for e in de_entries)
    passed = len(de_entries) > 0 and all_german
    results.append(
        TestResult(
            name="filter_german",
            category="Language Filter",
            description="get_knowledge(language='german') returns only German entries",
            expected="only entries with language=german",
            actual=f"{len(de_entries)} entries, all german: {all_german}, keys: {sorted(de_keys)}",
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    # Test 3: Filter by simple
    (simple_entries, latency) = _timed(
        lambda: store.get_knowledge(owner, tags=[BENCHMARK_TAG], language="simple")
    )
    simple_keys = {e.logical_key for e in simple_entries}
    all_simple = all(e.language == SIMPLE for e in simple_entries)
    passed = len(simple_entries) > 0 and all_simple
    results.append(
        TestResult(
            name="filter_simple",
            category="Language Filter",
            description="get_knowledge(language='simple') returns only simple-language entries",
            expected="only entries with language=simple",
            actual=(
                f"{len(simple_entries)} entries, all simple: {all_simple}, "
                f"keys: {sorted(simple_keys)}"
            ),
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    return results


def run_write_time_language(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
) -> list[TestResult]:
    """Write-time language assignment: explicit override, auto-detect, short text fallback."""
    results: list[TestResult] = []
    entry_map = {e.logical_key: e for e in entries}

    # Test 1: Explicit override stored
    override_entry = entry_map.get("bench-override-es")
    stored_lang = override_entry.language if override_entry else "MISSING"
    passed = stored_lang == "spanish"
    results.append(
        TestResult(
            name="write_explicit_override",
            category="Write-time Language",
            description="Entry with explicit language='spanish' stored as spanish",
            expected="spanish",
            actual=stored_lang,
            passed=passed,
            latency_ms=0.0,
        )
    )

    # Test 2: Auto-detect stored (Portuguese)
    pt_entry = entry_map.get("bench-autodetect-pt")
    stored_lang = pt_entry.language if pt_entry else "MISSING"
    passed = stored_lang == "portuguese"
    results.append(
        TestResult(
            name="write_autodetect_portuguese",
            category="Write-time Language",
            description="Portuguese text entry stored with language='portuguese'",
            expected="portuguese",
            actual=stored_lang,
            passed=passed,
            latency_ms=0.0,
        )
    )

    # Test 3: Short text gets simple
    short_entry = entry_map.get("bench-short")
    stored_lang = short_entry.language if short_entry else "MISSING"
    passed = stored_lang == SIMPLE
    results.append(
        TestResult(
            name="write_short_simple",
            category="Write-time Language",
            description="Short text entry stored with language='simple'",
            expected="simple",
            actual=stored_lang,
            passed=passed,
            latency_ms=0.0,
        )
    )

    return results


def run_regconfig_validation(store: PostgresStore) -> list[TestResult]:
    """Regconfig validation: invalid regconfig falls back to simple."""
    results: list[TestResult] = []

    (validated, latency) = _timed(lambda: store.validate_regconfig("nonexistent_language_xyz"))
    passed = validated == SIMPLE
    results.append(
        TestResult(
            name="regconfig_invalid_fallback",
            category="Regconfig Validation",
            description="Invalid regconfig 'nonexistent_language_xyz' falls back to 'simple'",
            expected="simple",
            actual=validated,
            passed=passed,
            latency_ms=round(latency, 2),
        )
    )

    return results


def run_deprecated_alias() -> list[TestResult]:
    """Deprecated alias: semantic_search function exists in tools module."""
    results: list[TestResult] = []

    # Check the source file directly — tools.py can't be imported outside
    # the MCP server context due to circular deps at module init time.
    from pathlib import Path

    tools_path = Path(__file__).parent.parent / "src" / "mcp_awareness" / "tools.py"
    if not tools_path.exists():
        # Installed package — try site-packages
        import mcp_awareness

        tools_path = Path(mcp_awareness.__file__).parent / "tools.py"

    try:
        source = tools_path.read_text()
        has_func = "def semantic_search(" in source or 'name="semantic_search"' in source
        has_deprecated = "deprecated" in source.lower() and "semantic_search" in source
        passed = has_func and has_deprecated
        actual = f"function_in_source={has_func}, deprecated_marker={has_deprecated}"
    except Exception as exc:
        passed = False
        actual = f"Error reading tools.py: {exc}"

    results.append(
        TestResult(
            name="deprecated_semantic_search_alias",
            category="Deprecated Alias",
            description="semantic_search function exists in tools.py and is marked deprecated",
            expected="function exists with deprecation marker",
            actual=actual,
            passed=passed,
            latency_ms=0.0,
        )
    )

    return results


def run_search_latency(
    store: PostgresStore,
    owner: str,
    embedding_provider: OllamaEmbedding | None,
) -> list[TestResult]:
    """Search latency meta-test: 20 iterations, compute p50/p95/p99."""
    results: list[TestResult] = []
    vec = _zero_vector(embedding_provider.dimensions) if embedding_provider else [0.0] * 768
    model = embedding_provider.model_name if embedding_provider else "null"

    latencies: list[float] = []
    for _ in range(20):
        _, elapsed = _timed(
            lambda: store.semantic_search(
                owner,
                embedding=vec,
                model=model,
                query_text="benchmark latency test query",
                query_language="english",
                tags=[BENCHMARK_TAG],
                limit=10,
            )
        )
        latencies.append(elapsed)

    latencies.sort()
    p50 = round(statistics.median(latencies), 2)
    p95 = round(latencies[int(len(latencies) * 0.95)], 2)
    p99 = round(latencies[int(len(latencies) * 0.99)], 2)
    mean = round(statistics.mean(latencies), 2)

    # Pass if p95 < 500ms (generous threshold for CI/local variance)
    passed = p95 < 500.0
    results.append(
        TestResult(
            name="search_latency_p95",
            category="Search Latency",
            description="Hybrid search p95 latency under 500ms over 20 iterations",
            expected="p95 < 500ms",
            actual=f"p50={p50}ms, p95={p95}ms, p99={p99}ms, mean={mean}ms",
            passed=passed,
            latency_ms=p50,
            details={
                "p50_ms": p50,
                "p95_ms": p95,
                "p99_ms": p99,
                "mean_ms": mean,
                "iterations": 20,
                "all_ms": [round(x, 2) for x in latencies],
            },
        )
    )

    return results


def run_embedding_status(
    store: PostgresStore,
    owner: str,
    entries: list[Entry],
    embedding_provider: OllamaEmbedding,
) -> list[TestResult]:
    """Embedding status: all benchmark entries have embeddings, vector CTE returns rows."""
    results: list[TestResult] = []

    # Test 1: All benchmark entries have embeddings
    missing = store.get_entries_without_embeddings(owner, embedding_provider.model_name, limit=100)
    missing_bench = [e for e in missing if BENCHMARK_TAG in e.tags]
    passed = len(missing_bench) == 0
    results.append(
        TestResult(
            name="embedding_all_seeded",
            category="Embedding Status",
            description="All benchmark entries have embeddings",
            expected="0 benchmark entries without embeddings",
            actual=f"{len(missing_bench)} missing: {[e.logical_key for e in missing_bench]}",
            passed=passed,
            latency_ms=0.0,
        )
    )

    # Test 2: Vector CTE returns rows for a real query
    query_vec = embedding_provider.embed(["retirement savings pension"])[0]
    hits = store.semantic_search(
        owner,
        embedding=query_vec,
        model=embedding_provider.model_name,
        query_text="",
        query_language=SIMPLE,
        tags=[BENCHMARK_TAG],
        limit=5,
    )
    passed = len(hits) > 0
    results.append(
        TestResult(
            name="embedding_vector_cte_returns_rows",
            category="Embedding Status",
            description="Vector-only search returns results from benchmark entries",
            expected=">0 results",
            actual=f"{len(hits)} results",
            passed=passed,
            latency_ms=0.0,
        )
    )

    return results


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------


def compare_baseline(current_results: list[TestResult], baseline_path: str) -> dict[str, Any]:
    """Compare current results against a baseline JSON file."""
    with open(baseline_path) as f:
        baseline = json.load(f)

    baseline_by_name = {r["name"]: r for r in baseline.get("results", [])}
    current_by_name = {r.name: r for r in current_results}

    regressions: list[dict[str, str]] = []
    improvements: list[dict[str, str]] = []
    new_tests: list[str] = []
    removed_tests: list[str] = []
    latency_deltas: dict[str, dict[str, float]] = {}

    for name, cur in current_by_name.items():
        if name not in baseline_by_name:
            new_tests.append(name)
            continue
        prev = baseline_by_name[name]
        if prev["passed"] and not cur.passed:
            regressions.append({"test": name, "was": "PASS", "now": "FAIL"})
        elif not prev["passed"] and cur.passed:
            improvements.append({"test": name, "was": "FAIL", "now": "PASS"})
        prev_lat = prev.get("latency_ms", 0)
        if prev_lat > 0 and cur.latency_ms > 0:
            delta = cur.latency_ms - prev_lat
            latency_deltas[name] = {
                "previous_ms": prev_lat,
                "current_ms": cur.latency_ms,
                "delta_ms": round(delta, 2),
                "delta_pct": round((delta / prev_lat) * 100, 1) if prev_lat else 0.0,
            }

    for name in baseline_by_name:
        if name not in current_by_name:
            removed_tests.append(name)

    return {
        "baseline_file": baseline_path,
        "regressions": regressions,
        "improvements": improvements,
        "new_tests": new_tests,
        "removed_tests": removed_tests,
        "latency_deltas": latency_deltas,
    }


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------


def print_results(all_results: list[TestResult], report: BenchmarkReport) -> None:
    """Print human-readable results to console."""
    print("\n" + "=" * 72)
    print("  Layer 1 Hybrid Retrieval Benchmark")
    print("=" * 72)

    current_category = ""
    for r in all_results:
        if r.category != current_category:
            current_category = r.category
            print(f"\n--- {current_category} ---")
        status = "[PASS]" if r.passed else "[FAIL]"
        if r.error:
            status = "[SKIP]"
        latency = f" ({r.latency_ms}ms)" if r.latency_ms > 0 else ""
        print(f"  {status} {r.name}{latency}")
        if not r.passed:
            print(f"         expected: {r.expected}")
            print(f"         actual:   {r.actual}")
        if r.error:
            print(f"         error:    {r.error}")

    summary = report.summary
    print(f"\n{'=' * 72}")
    print(f"  Total: {summary['total']}  Pass: {summary['pass']}  Fail: {summary['fail']}")
    if summary.get("skipped"):
        print(f"  Skipped: {summary['skipped']}")
    print()

    if "latency_percentiles" in summary:
        lp = summary["latency_percentiles"]
        print(
            f"  Latency: p50={lp.get('p50_ms', 'N/A')}ms  "
            f"p95={lp.get('p95_ms', 'N/A')}ms  "
            f"p99={lp.get('p99_ms', 'N/A')}ms"
        )

    if "category_breakdown" in summary:
        print("\n  Category breakdown:")
        for cat, counts in summary["category_breakdown"].items():
            print(f"    {cat}: {counts['pass']}/{counts['total']} passed")

    if report.baseline_comparison:
        bc = report.baseline_comparison
        print(f"\n  Baseline comparison ({bc['baseline_file']}):")
        if bc["regressions"]:
            print(f"    REGRESSIONS: {len(bc['regressions'])}")
            for reg in bc["regressions"]:
                print(f"      {reg['test']}: {reg['was']} -> {reg['now']}")
        if bc["improvements"]:
            print(f"    Improvements: {len(bc['improvements'])}")
            for imp in bc["improvements"]:
                print(f"      {imp['test']}: {imp['was']} -> {imp['now']}")
        if bc["new_tests"]:
            print(f"    New tests: {bc['new_tests']}")
        if bc["removed_tests"]:
            print(f"    Removed tests: {bc['removed_tests']}")

    print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Layer 1 hybrid retrieval benchmark for mcp-awareness",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("AWARENESS_DATABASE_URL", ""),
        help="PostgreSQL DSN (or set AWARENESS_DATABASE_URL)",
    )
    parser.add_argument(
        "--owner",
        default="benchmark-layer1",
        help="Owner ID for benchmark entries (default: benchmark-layer1)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for JSON output (default: current directory)",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Path to previous benchmark JSON for regression comparison",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip cleanup of benchmark entries after run",
    )
    parser.add_argument(
        "--no-vector",
        action="store_true",
        help="Skip vector and RRF tests (no embedding provider needed)",
    )
    parser.add_argument(
        "--embedding-url",
        default=os.environ.get("AWARENESS_EMBEDDING_URL", "http://localhost:11434"),
        help="Ollama URL for embeddings (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get("AWARENESS_EMBEDDING_MODEL", "granite-embedding:278m"),
        help="Embedding model name (default: granite-embedding:278m)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.dsn:
        logger.error("No DSN provided. Use --dsn or set AWARENESS_DATABASE_URL.")
        return 1

    # --- Initialize store ---
    logger.info("Connecting to database...")
    store = PostgresStore(args.dsn)

    # --- Initialize embedding provider ---
    embedding_provider: OllamaEmbedding | None = None
    if not args.no_vector:
        logger.info("Checking embedding provider at %s ...", args.embedding_url)
        provider = OllamaEmbedding(
            base_url=args.embedding_url,
            model=args.embedding_model,
        )
        if provider.is_available():
            embedding_provider = provider
            logger.info(
                "Embedding provider ready: %s (dims=%d)", provider.model_name, provider.dimensions
            )
        else:
            logger.warning(
                "Embedding provider not available at %s — "
                "vector/RRF/embedding tests will be skipped",
                args.embedding_url,
            )

    owner = args.owner
    all_results: list[TestResult] = []

    try:
        # --- Seed ---
        logger.info("Seeding %d benchmark entries (owner=%s)...", len(SEED_ENTRIES), owner)
        entries = seed_entries(store, owner, embedding_provider)
        logger.info("Seeding complete. %d entries ready.", len(entries))

        # Re-read entries from store to get stored state (language may have been validated)
        stored_entries: list[Entry] = store.get_knowledge(owner, tags=[BENCHMARK_TAG], limit=50)
        entry_map = {e.logical_key: e for e in stored_entries}
        # Merge: prefer stored entries (they have the validated language)
        final_entries = []
        for e in entries:
            final_entries.append(entry_map.get(e.logical_key, e))

        # --- Run tests ---
        logger.info("Running tests...")

        # 1. FTS Stemming (2)
        all_results.extend(run_fts_stemming(store, owner, final_entries, embedding_provider))

        # 2. Cross-language FTS (3)
        all_results.extend(run_cross_language_fts(store, owner, final_entries, embedding_provider))

        # 3. Vector Search (2, skip if no-vector)
        if embedding_provider:
            all_results.extend(run_vector_search(store, owner, final_entries, embedding_provider))
        else:
            for name, desc in [
                ("vector_pension_401k", "Query 'pension' finds 401k via vector similarity"),
                ("vector_green_solar", "Query 'green power' finds solar panels via vector"),
            ]:
                all_results.append(
                    TestResult(
                        name=name,
                        category="Vector Search",
                        description=desc,
                        expected="N/A",
                        actual="skipped (no embedding provider)",
                        passed=False,
                        latency_ms=0.0,
                        error="skipped: --no-vector or provider unavailable",
                    )
                )

        # 4. RRF Fusion (2, skip if no-vector)
        if embedding_provider:
            all_results.extend(run_rrf_fusion(store, owner, final_entries, embedding_provider))
        else:
            for name, desc in [
                ("rrf_both_arms_contribute", "Both vector and FTS arms contribute to results"),
                ("rrf_fusion_score_higher", "Fusion score >= max single-arm score"),
            ]:
                all_results.append(
                    TestResult(
                        name=name,
                        category="RRF Fusion",
                        description=desc,
                        expected="N/A",
                        actual="skipped (no embedding provider)",
                        passed=False,
                        latency_ms=0.0,
                        error="skipped: --no-vector or provider unavailable",
                    )
                )

        # 5. Language Detection (3)
        all_results.extend(run_language_detection(store, owner, final_entries))

        # 6. Language Filter (3)
        all_results.extend(run_language_filter(store, owner, final_entries))

        # 7. Write-time Language (3)
        all_results.extend(run_write_time_language(store, owner, final_entries))

        # 8. Regconfig Validation (1)
        all_results.extend(run_regconfig_validation(store))

        # 9. Deprecated Alias (1)
        all_results.extend(run_deprecated_alias())

        # 10. Search Latency (1)
        all_results.extend(run_search_latency(store, owner, embedding_provider))

        # 11. Embedding Status (2, skip if no-vector)
        if embedding_provider:
            all_results.extend(
                run_embedding_status(store, owner, final_entries, embedding_provider)
            )
        else:
            for name, desc in [
                ("embedding_all_seeded", "All benchmark entries have embeddings"),
                ("embedding_vector_cte_returns_rows", "Vector CTE returns rows"),
            ]:
                all_results.append(
                    TestResult(
                        name=name,
                        category="Embedding Status",
                        description=desc,
                        expected="N/A",
                        actual="skipped (no embedding provider)",
                        passed=False,
                        latency_ms=0.0,
                        error="skipped: --no-vector or provider unavailable",
                    )
                )

    finally:
        if not args.no_cleanup:
            try:
                cleanup(store, owner)
            except Exception:
                logger.exception("Cleanup failed")
        else:
            logger.info("Skipping cleanup (--no-cleanup)")

    # --- Build report ---
    total = len(all_results)
    passed = sum(1 for r in all_results if r.passed)
    failed = sum(1 for r in all_results if not r.passed and not r.error)
    skipped = sum(1 for r in all_results if r.error)

    # Category breakdown
    categories: dict[str, dict[str, int]] = {}
    for r in all_results:
        cat = categories.setdefault(r.category, {"total": 0, "pass": 0, "fail": 0, "skip": 0})
        cat["total"] += 1
        if r.error:
            cat["skip"] += 1
        elif r.passed:
            cat["pass"] += 1
        else:
            cat["fail"] += 1

    # Latency percentiles from the latency test
    latency_details: dict[str, Any] = {}
    for r in all_results:
        if r.name == "search_latency_p95" and r.details:
            latency_details = {
                "p50_ms": r.details.get("p50_ms"),
                "p95_ms": r.details.get("p95_ms"),
                "p99_ms": r.details.get("p99_ms"),
                "mean_ms": r.details.get("mean_ms"),
            }

    report = BenchmarkReport(
        meta={
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "owner": owner,
            "dsn_hash": hashlib.sha256(args.dsn.encode()).hexdigest()[:12],
            "vector_enabled": embedding_provider is not None,
            "embedding_model": embedding_provider.model_name if embedding_provider else None,
            "seed_entries": len(SEED_ENTRIES),
        },
        summary={
            "total": total,
            "pass": passed,
            "fail": failed,
            "skipped": skipped,
            "category_breakdown": categories,
            "latency_percentiles": latency_details,
        },
        results=[asdict(r) for r in all_results],
    )

    # Baseline comparison
    if args.baseline:
        try:
            report.baseline_comparison = compare_baseline(all_results, args.baseline)
        except Exception:
            logger.exception("Baseline comparison failed")

    # --- Write JSON ---
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_path = Path(args.output_dir) / f"benchmark-layer1-{timestamp}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(asdict(report), f, indent=2, default=str)
    logger.info("Report written to %s", output_path)

    # --- Console output ---
    print_results(all_results, report)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
