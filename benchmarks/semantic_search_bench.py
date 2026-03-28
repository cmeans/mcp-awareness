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

"""Semantic search benchmark for mcp-awareness.

Generates synthetic entries at various scales, embeds them via Ollama,
and measures query latency across different filter combinations and
concurrency levels. Uses a separate database to avoid polluting production.

Usage (from repo root):
    docker compose exec mcp-awareness python benchmarks/semantic_search_bench.py

Or via a temporary container on the same network:
    docker run --rm --network mcp-awareness_default \
      -v $(pwd):/app -w /app \
      python:3.12-slim \
      bash -c "pip install -e '.[dev]' && python benchmarks/semantic_search_bench.py"
"""

from __future__ import annotations

import json
import random
import statistics
import string
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from mcp_awareness.embeddings import (
    OllamaEmbedding,
    compose_embedding_text,
    text_hash,
)
from mcp_awareness.postgres_store import PostgresStore
from mcp_awareness.schema import Entry, EntryType, make_id

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POSTGRES_DSN = "postgresql://awareness:awareness-dev@postgres:5432/awareness"
BENCH_DB = "awareness_bench"
OLLAMA_URL = "http://ollama:11434"
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768

SCALE_TIERS = [500, 1_000, 5_000, 10_000]
# Tiers above this threshold use synthetic vectors (skip Ollama embedding)
REAL_EMBED_THRESHOLD = 1_000
QUERY_ITERATIONS = 50  # queries per scenario per tier
CONCURRENCY_LEVELS = [1, 3, 5, 10]

# Synthetic data variety
SOURCES = [
    "synology-nas", "home-assistant", "garmin", "google-calendar",
    "github-ci", "personal", "work-project", "family", "health",
    "finance", "travel", "infra-monitoring",
]
TAG_POOL = [
    "infra", "personal", "project", "health", "family", "finance",
    "travel", "nas", "homeassistant", "garmin", "engineering",
    "decision", "preference", "alert", "status", "reminder",
]
ENTRY_TYPES = [EntryType.NOTE, EntryType.CONTEXT, EntryType.PATTERN]

# Corpus of realistic descriptions for embedding variety
DESCRIPTIONS = [
    "NAS backup completed successfully on all shared folders",
    "Home Assistant detected motion in the garage at 2am",
    "Garmin watch sync shows resting heart rate trending down",
    "Google Calendar meeting with dentist next Tuesday",
    "GitHub CI pipeline failed on branch feature/auth-refactor",
    "Retirement savings rebalanced to 60/40 allocation",
    "Family dinner planned for Saturday evening at 6pm",
    "Synology disk health warning on drive 3 SMART status",
    "Blood pressure reading 128/82 slightly elevated",
    "Proxmox VM migration completed from node1 to node2",
    "Electric bill was higher than usual this month",
    "Travel itinerary updated for Denver trip in April",
    "New firmware available for Synology DSM 7.3",
    "Kids school calendar shows spring break next week",
    "Weight tracking shows 3lb loss over past month",
    "Home Assistant thermostat set to away mode automatically",
    "Garmin sleep score averaged 78 this week below target",
    "Project deadline moved to end of quarter per manager",
    "Ollama model pull completed for nomic-embed-text",
    "Docker compose deployment updated with new secrets",
    "Network latency spike detected on WAN interface",
    "Calendar conflict detected between two meetings Friday",
    "Investment portfolio quarterly review shows 8% growth",
    "Home security camera offline for 3 hours overnight",
    "Medication reminder for daily vitamin D supplement",
    "Server CPU usage exceeded 90% threshold for 5 minutes",
    "New pull request opened for database migration refactor",
    "Grocery list synced from shared family notes",
    "Garmin VO2 max estimate improved by 2 points",
    "Backup verification failed for offsite replication job",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rand_tags(n: int = 3) -> list[str]:
    return random.sample(TAG_POOL, min(n, len(TAG_POOL)))


def _rand_description() -> str:
    base = random.choice(DESCRIPTIONS)
    # Add some variation so embeddings aren't identical
    suffix = "".join(random.choices(string.ascii_lowercase + " ", k=random.randint(10, 40)))
    return f"{base}. {suffix.strip()}"


def generate_entries(count: int) -> list[Entry]:
    """Generate synthetic entries with realistic variety."""
    entries: list[Entry] = []
    base_time = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(count):
        etype = random.choice(ENTRY_TYPES)
        source = random.choice(SOURCES)
        tags = _rand_tags(random.randint(1, 4))
        created = base_time + timedelta(minutes=i * 2)

        data: dict = {"description": _rand_description()}
        if etype == EntryType.PATTERN:
            data["effect"] = f"When {source} reports, expect {random.choice(TAG_POOL)} activity"
        if etype == EntryType.CONTEXT:
            data["expires"] = (created + timedelta(days=7)).isoformat()

        expires = None
        if etype == EntryType.CONTEXT:
            # Ensure expiry is always in the future to avoid cleanup deletion
            expires = datetime.now(timezone.utc) + timedelta(days=7)

        entry = Entry(
            id=make_id(),
            type=etype,
            source=source,
            tags=tags,
            data=data,
            created=created,
            updated=created,
            expires=expires,
        )
        entries.append(entry)
    return entries


@dataclass
class QueryScenario:
    name: str
    kwargs: dict = field(default_factory=dict)


QUERY_SCENARIOS = [
    QueryScenario("unfiltered"),
    QueryScenario("filter_by_source", {"source": "synology-nas"}),
    QueryScenario("filter_by_type", {"entry_type": EntryType.NOTE}),
    QueryScenario("filter_by_tag", {"tags": ["infra"]}),
    QueryScenario("filter_combined", {"source": "personal", "tags": ["family"]}),
    QueryScenario("limit_5", {"limit": 5}),
    QueryScenario("limit_20", {"limit": 20}),
]

QUERY_TEXTS = [
    "NAS backup status and disk health",
    "heart rate and sleep quality trends",
    "home security camera alerts",
    "retirement savings portfolio performance",
    "server CPU memory usage monitoring",
]


@dataclass
class BenchResult:
    tier: int
    scenario: str
    latencies_ms: list[float]

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies_ms)

    @property
    def p95(self) -> float:
        s = sorted(self.latencies_ms)
        idx = int(len(s) * 0.95)
        return s[min(idx, len(s) - 1)]

    @property
    def p99(self) -> float:
        s = sorted(self.latencies_ms)
        idx = int(len(s) * 0.99)
        return s[min(idx, len(s) - 1)]

    @property
    def mean(self) -> float:
        return statistics.mean(self.latencies_ms)

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.latencies_ms) if len(self.latencies_ms) > 1 else 0.0


# ---------------------------------------------------------------------------
# Database setup / teardown
# ---------------------------------------------------------------------------


def create_bench_db(dsn: str, db_name: str) -> str:
    """Create isolated benchmark database; return its DSN."""
    # Connect to default DB to create benchmark DB
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{db_name}"')
                print(f"  Created database: {db_name}")
            else:
                print(f"  Database exists: {db_name}")

    parts = dsn.rsplit("/", 1)
    return f"{parts[0]}/{db_name}"


def drop_bench_db(dsn: str, db_name: str) -> None:
    """Drop the benchmark database."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Terminate active connections
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    print(f"  Dropped database: {db_name}")


# ---------------------------------------------------------------------------
# Benchmark phases
# ---------------------------------------------------------------------------


def phase_insert(store: PostgresStore, entries: list[Entry]) -> float:
    """Insert entries, return total time in seconds."""
    t0 = time.perf_counter()
    for entry in entries:
        store.add(entry)
    return time.perf_counter() - t0


def phase_embed_real(
    store: PostgresStore,
    entries: list[Entry],
    provider: OllamaEmbedding,
    batch_size: int = 50,
) -> tuple[float, int]:
    """Embed all entries via Ollama, return (total_time, count)."""
    t0 = time.perf_counter()
    embedded = 0
    for i in range(0, len(entries), batch_size):
        batch = entries[i : i + batch_size]
        texts = [compose_embedding_text(e) for e in batch]
        hashes = [text_hash(t) for t in texts]
        try:
            vectors = provider.embed(texts)
        except Exception as exc:
            print(f"    Embedding batch {i} failed: {exc}")
            continue
        for entry, h, vec in zip(batch, hashes, vectors):
            store.upsert_embedding(
                entry.id, provider.model_name, provider.dimensions, h, vec
            )
            embedded += 1
        if (i + batch_size) % 500 == 0 or i + batch_size >= len(entries):
            print(f"    Embedded {min(i + batch_size, len(entries))}/{len(entries)}")
    elapsed = time.perf_counter() - t0
    return elapsed, embedded


def phase_embed_synthetic(
    store: PostgresStore,
    entries: list[Entry],
    model_name: str,
    dimensions: int,
    batch_size: int = 200,
) -> tuple[float, int]:
    """Insert random vectors directly into DB (skip Ollama). Fast for large tiers."""
    t0 = time.perf_counter()
    embedded = 0
    for i in range(0, len(entries), batch_size):
        batch = entries[i : i + batch_size]
        for entry in batch:
            text = compose_embedding_text(entry)
            h = text_hash(text)
            # Random unit-ish vector — not semantically meaningful but exercises
            # the HNSW index and cosine distance operator realistically
            vec = [random.gauss(0, 1) for _ in range(dimensions)]
            norm = sum(v * v for v in vec) ** 0.5
            vec = [v / norm for v in vec]
            store.upsert_embedding(entry.id, model_name, dimensions, h, vec)
            embedded += 1
        if (i + batch_size) % 1000 == 0 or i + batch_size >= len(entries):
            print(f"    Inserted {min(i + batch_size, len(entries))}/{len(entries)} synthetic vectors")
    elapsed = time.perf_counter() - t0
    return elapsed, embedded


def phase_query(
    store: PostgresStore,
    provider: OllamaEmbedding,
    scenarios: list[QueryScenario],
    iterations: int,
) -> list[BenchResult]:
    """Run query scenarios, return latency results."""
    # Pre-embed query texts
    query_vectors = provider.embed(QUERY_TEXTS)

    results: list[BenchResult] = []
    for scenario in scenarios:
        latencies: list[float] = []
        for _ in range(iterations):
            qvec = random.choice(query_vectors)
            kwargs = dict(scenario.kwargs)
            kwargs.setdefault("limit", 10)
            t0 = time.perf_counter()
            store.semantic_search(
                embedding=qvec,
                model=provider.model_name,
                **kwargs,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)
        results.append(BenchResult(
            tier=0,  # filled in by caller
            scenario=scenario.name,
            latencies_ms=latencies,
        ))
    return results


def phase_concurrent(
    store: PostgresStore,
    provider: OllamaEmbedding,
    concurrency_levels: list[int],
    queries_per_level: int = 30,
) -> list[BenchResult]:
    """Measure query latency under concurrent load."""
    query_vectors = provider.embed(QUERY_TEXTS)
    results: list[BenchResult] = []

    for n_workers in concurrency_levels:

        def run_query(_: int) -> float:
            qvec = random.choice(query_vectors)
            t0 = time.perf_counter()
            store.semantic_search(
                embedding=qvec,
                model=provider.model_name,
                limit=10,
            )
            return (time.perf_counter() - t0) * 1000

        latencies: list[float] = []
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = [pool.submit(run_query, i) for i in range(queries_per_level)]
            for f in as_completed(futs):
                latencies.append(f.result())

        results.append(BenchResult(
            tier=0,
            scenario=f"concurrent_{n_workers}",
            latencies_ms=latencies,
        ))
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_table(title: str, results: list[BenchResult]) -> None:
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}")
    header = f"{'Scenario':<25} {'Count':>6} {'Mean':>8} {'P50':>8} {'P95':>8} {'P99':>8} {'StDev':>8}"
    print(header)
    print("-" * 80)
    for r in results:
        print(
            f"{r.scenario:<25} {len(r.latencies_ms):>6} "
            f"{r.mean:>7.2f}ms {r.p50:>7.2f}ms {r.p95:>7.2f}ms "
            f"{r.p99:>7.2f}ms {r.stdev:>7.2f}ms"
        )


def print_summary(all_results: dict[int, dict[str, list[BenchResult]]]) -> None:
    """Print a cross-tier comparison table."""
    print(f"\n{'=' * 80}")
    print("  CROSS-TIER COMPARISON (P50 latency in ms)")
    print(f"{'=' * 80}")

    # Collect all scenario names
    scenarios = set()
    for phases in all_results.values():
        for phase_results in phases.values():
            for r in phase_results:
                scenarios.add(r.scenario)
    scenarios_sorted = sorted(scenarios)

    tiers = sorted(all_results.keys())
    header = f"{'Scenario':<25}" + "".join(f"{t:>10}" for t in tiers)
    print(header)
    print("-" * (25 + 10 * len(tiers)))

    for s in scenarios_sorted:
        row = f"{s:<25}"
        for t in tiers:
            found = False
            for phase_results in all_results[t].values():
                for r in phase_results:
                    if r.scenario == s:
                        row += f"{r.p50:>9.2f}ms" if r.p50 < 1000 else f"{r.p50/1000:>8.2f}s "
                        found = True
                        break
                if found:
                    break
            if not found:
                row += f"{'—':>10}"
        print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("Semantic Search Benchmark")
    print("=" * 50)

    # Check Ollama
    provider = OllamaEmbedding(base_url=OLLAMA_URL, model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIM)
    if not provider.is_available():
        print(f"ERROR: Ollama not available at {OLLAMA_URL} with model {EMBEDDING_MODEL}")
        sys.exit(1)
    print(f"Ollama: {OLLAMA_URL} with {EMBEDDING_MODEL} ({EMBEDDING_DIM}d)")

    # Create benchmark database
    print("\nDatabase setup:")
    bench_dsn = create_bench_db(POSTGRES_DSN, BENCH_DB)

    all_results: dict[int, dict[str, list[BenchResult]]] = {}

    try:
        for tier in SCALE_TIERS:
            print(f"\n{'#' * 80}")
            print(f"  TIER: {tier:,} entries")
            print(f"{'#' * 80}")

            # Fresh store for this tier
            store = PostgresStore(bench_dsn, min_pool=2, max_pool=10)

            # Generate
            print(f"\n  Generating {tier:,} synthetic entries...")
            entries = generate_entries(tier)
            print(f"    Sources: {len(set(e.source for e in entries))}")
            print(f"    Types: {dict((t.value, sum(1 for e in entries if e.type == t)) for t in ENTRY_TYPES)}")

            # Insert
            print(f"\n  Inserting entries...")
            insert_time = phase_insert(store, entries)
            print(f"    Inserted {tier:,} in {insert_time:.2f}s ({tier/insert_time:.0f} entries/s)")

            # Embed
            use_real = tier <= REAL_EMBED_THRESHOLD
            if use_real:
                print(f"\n  Embedding entries via Ollama (batch_size=50)...")
                embed_time, embed_count = phase_embed_real(store, entries, provider, batch_size=50)
            else:
                print(f"\n  Inserting synthetic vectors (skipping Ollama for speed)...")
                embed_time, embed_count = phase_embed_synthetic(
                    store, entries, provider.model_name, provider.dimensions
                )
            print(f"    Embedded {embed_count:,} in {embed_time:.2f}s ({embed_count/embed_time:.0f} embeddings/s)")

            # Verify
            with psycopg.connect(bench_dsn, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) AS c FROM entries")
                    entry_count = cur.fetchone()["c"]
                    cur.execute("SELECT count(*) AS c FROM embeddings")
                    emb_count = cur.fetchone()["c"]
            print(f"    Verified: {entry_count:,} entries, {emb_count:,} embeddings")

            # Query benchmarks
            print(f"\n  Running query benchmarks ({QUERY_ITERATIONS} iterations each)...")
            query_results = phase_query(store, provider, QUERY_SCENARIOS, QUERY_ITERATIONS)
            for r in query_results:
                r.tier = tier
            print_table(f"Query Latency @ {tier:,} entries", query_results)

            # Concurrency benchmarks
            print(f"\n  Running concurrency benchmarks...")
            conc_results = phase_concurrent(store, provider, CONCURRENCY_LEVELS)
            for r in conc_results:
                r.tier = tier
            print_table(f"Concurrent Query Latency @ {tier:,} entries", conc_results)

            all_results[tier] = {
                "query": query_results,
                "concurrent": conc_results,
            }

            # Clean up for next tier
            store.clear()
            # Close pool connections
            store._pool.close()

        # Cross-tier summary
        print_summary(all_results)

    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        # Ensure all pools are closed before dropping the DB
        print("\n\nCleanup:")
        try:
            store._pool.close()  # type: ignore[union-attr]
        except Exception:
            pass
        drop_bench_db(POSTGRES_DSN, BENCH_DB)

    print("\nBenchmark complete.")


if __name__ == "__main__":
    main()
