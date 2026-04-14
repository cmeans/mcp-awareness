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

"""CLI for registering _system-owned schema entries.

Bypasses MCP entirely — operator tool, run once per built-in schema at
deploy/bootstrap time. No MCP auth, no middleware, direct PostgresStore
access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a _system-owned schema entry (operator bootstrap only).",
    )
    parser.add_argument(
        "--system",
        action="store_true",
        required=True,
        help="Required. Confirms the caller intends to write to the _system owner.",
    )
    parser.add_argument(
        "--family",
        required=True,
        help="Schema family (e.g., schema:edge-manifest)",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Schema version (e.g., 1.0.0)",
    )
    parser.add_argument(
        "--schema-file",
        required=True,
        type=Path,
        help="Path to JSON file containing the Draft 2020-12 schema body",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Source field for the entry",
    )
    parser.add_argument(
        "--tags",
        default="",
        help="Comma-separated tags (empty string for none)",
    )
    parser.add_argument(
        "--description",
        required=True,
        help="Entry description",
    )
    args = parser.parse_args()

    # Read + parse schema file
    if not args.schema_file.exists():
        print(
            json.dumps({"error": {"code": "file_not_found", "message": str(args.schema_file)}}),
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        schema_body = json.loads(args.schema_file.read_text())
    except json.JSONDecodeError as e:
        print(
            json.dumps({"error": {"code": "invalid_json", "message": str(e)}}),
            file=sys.stderr,
        )
        sys.exit(1)

    # Meta-schema validation
    from jsonschema import exceptions as jse

    from mcp_awareness.validation import compose_schema_logical_key, validate_schema_body

    try:
        validate_schema_body(schema_body)
    except jse.SchemaError as e:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "invalid_schema",
                        "message": str(e.message),
                        "schema_error_path": "/" + "/".join(str(p) for p in e.absolute_path),
                    }
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    # DB connection
    database_url = os.environ.get("AWARENESS_DATABASE_URL", "")
    if not database_url:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "missing_env",
                        "message": "AWARENESS_DATABASE_URL required",
                    }
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    from mcp_awareness.language import resolve_language
    from mcp_awareness.postgres_store import PostgresStore
    from mcp_awareness.schema import Entry, EntryType, make_id, now_utc

    store = PostgresStore(database_url)
    logical_key = compose_schema_logical_key(args.family, args.version)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    # Match the MCP path: run the description through the standard
    # language-resolution chain (lingua auto-detection, SIMPLE fallback)
    # instead of pinning every CLI-seeded schema to english.
    resolved_lang = resolve_language(text_for_detection=args.description)

    entry = Entry(
        id=make_id(),
        type=EntryType.SCHEMA,
        source=args.source,
        tags=tags,
        created=now_utc(),
        expires=None,
        data={
            "family": args.family,
            "version": args.version,
            "schema": schema_body,
            "description": args.description,
            "learned_from": "cli-bootstrap",
        },
        logical_key=logical_key,
        language=resolved_lang,
    )

    try:
        store.add("_system", entry)
    except Exception as e:
        print(
            json.dumps({"error": {"code": "store_error", "message": str(e)}}),
            file=sys.stderr,
        )
        sys.exit(1)

    print(json.dumps({"status": "ok", "id": entry.id, "logical_key": logical_key}))


if __name__ == "__main__":
    main()
