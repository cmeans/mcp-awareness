<!-- SPDX-License-Identifier: AGPL-3.0-or-later | Copyright (C) 2026 Chris Means -->
# Schema + Record guide

New in v0.18.0. This guide walks through what schemas and records are,
why you'd use them, and how to register one and start storing validated
data.

- Reference: entry-type schemas for `schema` and `record` live in the
  [Data Dictionary](data-dictionary.md#schema--json-schema-definitions).
- Design context:
  [2026-04-13 schema/record design](superpowers/specs/2026-04-13-schema-record-entry-types-design.md).

---

## Why typed data?

`remember`, `add_context`, `learn_pattern`, and `remind` let you store
free-form knowledge. That's the right tool for "the router is in the
basement closet" or "update the CLA bot whitelist weekly." Most things
an agent learns about you are notes — no shape required.

Some things *do* have a shape, though. A book you're reading has a
title, an author, and a status. A recipe has ingredients and steps. A
home-inventory item has a location and a purchase date. Without a
schema, one entry says `status: "reading"`, another says
`state: "in progress"`, and a third forgets status entirely. Three
months later nothing lines up and your agent can't answer "what am I
partway through?"

**Schemas fix that.** You define the shape once (as
[JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12)),
and records that conform to it are validated on write and re-validated
on update. Invalid data is rejected at the boundary, not discovered
later.

### The Tag Taxonomy tie-in

The [Tag Taxonomy v2 design](#) (Layer C) is built on top of
schema/record: a user-defined tag vocabulary is just a set of records
validated against a `tag-taxonomy` schema. That layer isn't wired in
yet, but it means the schema/record primitive in this release is doing
double duty — the feature you're reading about now is also the
foundation under something bigger. If you start using schema/record
for your own data today, you'll be using the same machinery that'll
later power shared tag vocabularies, edge provider manifests, and
more.

---

## Who it's for

**Personal use.** You're curating something that matters to you — your
music collection, reading list, recipe file, home inventory,
subscription tracker. You want the agent to validate what it stores so
fields stay consistent as the collection grows. When a future [edge
provider](superpowers/specs/2026-04-13-schema-record-entry-types-design.md)
syncs with an external service (Goodreads for books, Spotify for
music, a recipe API for your saved recipes), the schema is unchanged
— the data just starts arriving automatically instead of being typed
by hand.

**Team and integration use.** You're building an edge provider that
writes structured telemetry to an awareness instance, or you need
shared vocabulary across multiple agents. Schema/record gives you a
typed contract that both sides agree on, with immutability and
versioning so the contract can evolve without breaking consumers.

---

## Walk-through: a music collection

Imagine you want to keep an inventory of albums you own. You care
about the title, artist, year, rating, and a short note. You'd like
the agent to reject `year: "nineteen ninety seven"` and
`rating: 11` before they pollute the store.

### 1. Register an `album` schema

```
register_schema(
    family="album",
    version="1",
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["title", "artist", "year"],
        "properties": {
            "title":  {"type": "string", "minLength": 1},
            "artist": {"type": "string", "minLength": 1},
            "year":   {"type": "integer", "minimum": 1877},
            "rating": {"type": "integer", "minimum": 1, "maximum": 5},
            "notes":  {"type": "string"}
        },
        "additionalProperties": false
    },
    description="A single album in my music collection."
)
```

A few things are happening:

- `family` + `version` become the schema's `logical_key`
  (`album:1`). That's how records reference it.
- The schema body is a standard JSON Schema. Anything a JSON Schema
  Draft 2020-12 validator understands, mcp-awareness understands.
- **Schemas are immutable after registration.** If you need to change
  the shape, register `album:2`. The data dictionary explains why:
  immutability is what makes the schema safely referenceable by every
  record pinned to it.

### 2. Create a record

```
create_record(
    schema_ref="album",
    schema_version="1",
    content={
        "title":  "OK Computer",
        "artist": "Radiohead",
        "year":   1997,
        "rating": 5,
        "notes":  "Still the reference"
    },
    tags=["music", "album", "90s"]
)
```

The record is stored with its content validated against `album:1`.
Tags work as usual, so you can retrieve it the same way you retrieve
any other entry.

### 3. What a validation failure looks like

```
create_record(
    schema_ref="album",
    schema_version="1",
    content={"title": "Kid A", "artist": "Radiohead", "year": "2000"}
)
```

The write is rejected with a structured error:

```
{
  "error": "validation_failed",
  "schema_ref": "album:1",
  "validation_errors": [
    {"path": "/year", "message": "'2000' is not of type 'integer'"}
  ]
}
```

All failures are reported at once — you don't fix one and discover the
next on the retry.

### 4. Update with re-validation

```
update_entry(
    id="<the record id>",
    content={"title": "OK Computer", "artist": "Radiohead", "year": 1997, "rating": 4, "notes": "Downgraded after fresh listen"}
)
```

`update_entry` re-runs schema validation on record content. If the
update would produce an invalid record, the write is rejected and the
stored entry is unchanged. You can't accidentally corrupt a record by
editing one field into an invalid state.

### 5. Schemas with live records can't be deleted

If you try to `delete_entry` on `album:1` while any records still
reference it:

```
{
  "error": "schema_in_use",
  "schema_ref": "album:1",
  "referencing_records": 42
}
```

Deletion is blocked because records pin their exact schema version.
To retire a schema, migrate records to a newer version (`album:2`)
first. (A follow-up, [#293](https://github.com/cmeans/mcp-awareness/issues/293),
will add a migration helper; today you do it by creating new records
and deleting the old ones.)

---

## Extending the walk-through: tag taxonomy

Your music collection probably already uses tags — `["music", "rock", "90s"]` on
one album, `["music", "rock", "alternative"]` on another. What is
"alternative rock" exactly? Is it the same as "alt rock"? Without a
shared definition, different entries drift.

Register a `tag-definition` schema:

```
register_schema(
    family="tag-definition",
    version="1",
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["path", "description"],
        "properties": {
            "path":        {"type": "string", "pattern": "^[a-z0-9/_-]+$"},
            "description": {"type": "string", "minLength": 1},
            "synonyms":    {"type": "array", "items": {"type": "string"}},
            "display":     {"type": "string"}
        },
        "additionalProperties": false
    },
    description="A human-defined tag with description, synonyms, and display name."
)
```

Then seed a few records:

```
create_record(schema_ref="tag-definition", schema_version="1",
    content={"path": "music/genre/rock/alternative",
             "description": "Rock that deliberately departs from mainstream rock conventions; 90s onward.",
             "synonyms": ["alt rock", "alternative"],
             "display":  "Alternative Rock"},
    tags=["tag-definition", "music"])
```

Once the [Tag Taxonomy Layer C](#) work lands, the awareness server
will automatically consume these records to disambiguate cross-user
tags, provide display names in shared views, and power prefix-aware
tag searches. Today they serve as self-documenting tag definitions
that future tooling will leverage.

---

## More use cases

<details>
<summary><strong>Reading list</strong> — books you're reading, finished, or gave up on</summary>

```
register_schema(family="book", version="1", schema={
    "type": "object",
    "required": ["title", "author", "status"],
    "properties": {
        "title":  {"type": "string"},
        "author": {"type": "string"},
        "status": {"enum": ["to-read", "reading", "finished", "abandoned"]},
        "rating": {"type": "integer", "minimum": 1, "maximum": 5},
        "notes":  {"type": "string"}
    }
})
```

A status enum catches typos the way free text can't — you can't end
up with records split across `"in progress"`, `"reading"`, and
`"currently reading"` a year from now.

**Future collector:** an edge provider that syncs from Goodreads,
Kindle, or another reading service can add fields like `pages_read`,
`last_opened`, or `progress_pct` to an updated schema (`book:2`).
Until that lands, keep the manual schema lean — only fields you'd
actually type by hand.
</details>

<details>
<summary><strong>Recipes</strong> — a personal cookbook</summary>

```
register_schema(family="recipe", version="1", schema={
    "type": "object",
    "required": ["title", "ingredients", "steps"],
    "properties": {
        "title":       {"type": "string"},
        "servings":    {"type": "integer", "minimum": 1},
        "prep_min":    {"type": "integer", "minimum": 0},
        "cook_min":    {"type": "integer", "minimum": 0},
        "ingredients": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "steps":       {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "source_url":  {"type": "string", "format": "uri"}
    }
})
```

Arrays of strings keep it simple; a future version could tighten
`ingredients` into `{name, quantity, unit}` objects if you want
shopping-list generation. `source_url` preserves the link to wherever
you found the recipe.

**Future collector:** an edge provider could ingest recipes from
third-party services or from `application/ld+json` Recipe microdata
on web pages you bookmark.
</details>

<details>
<summary><strong>Home inventory</strong> — what you own and where it is</summary>

```
register_schema(family="inventory-item", version="1", schema={
    "type": "object",
    "required": ["name", "location"],
    "properties": {
        "name":              {"type": "string"},
        "location":          {"type": "string"},
        "purchase_date":     {"type": "string", "format": "date"},
        "purchase_price":    {"type": "number", "minimum": 0},
        "purchase_url":      {"type": "string", "format": "uri"},
        "receipt_url":       {"type": "string", "format": "uri"},
        "warranty_expires":  {"type": "string", "format": "date"},
        "serial_number":     {"type": "string"}
    }
})
```

The URL fields turn this into a durable audit trail: years later,
`warranty_expires` and `receipt_url` still resolve; `purchase_url`
takes you back to the original listing for replacement or
comparison-shopping.

**Future collector:** an edge provider could watch an email inbox for
shipping confirmations and pre-populate records; or parse receipts
from a file drop.
</details>

<details>
<summary><strong>Subscriptions</strong> — services you pay for</summary>

```
register_schema(family="subscription", version="1", schema={
    "type": "object",
    "required": ["service", "cost", "billing_cycle"],
    "properties": {
        "service":        {"type": "string"},
        "cost":           {"type": "number", "minimum": 0},
        "currency":       {"type": "string", "default": "USD"},
        "billing_cycle":  {"enum": ["monthly", "annual", "quarterly"]},
        "renewal_date":   {"type": "string", "format": "date"},
        "auto_renew":     {"type": "boolean"},
        "cancel_url":     {"type": "string", "format": "uri"}
    }
})
```

Enforcing `billing_cycle` as an enum prevents the "once a year vs
yearly vs annual" drift that makes cost totals hard to compute.

**Future collector:** an edge provider could read bank or card
statements and propose matching records.
</details>

<details>
<summary><strong>Edge provider manifests</strong> — the technical motivator</summary>

An edge provider is an external daemon that writes status, alerts, or
knowledge into your awareness instance. Each provider declares its
capabilities via a manifest record validated against an
`edge-manifest` schema. See the
[schema/record design doc](superpowers/specs/2026-04-13-schema-record-entry-types-design.md)
for the manifest shape; it's registered as a `_system` schema at
deploy time.
</details>

<details>
<summary><strong>Meeting notes and bug templates</strong> — team artifacts</summary>

If you run standups, retros, or incident reviews, a schema enforces
the fields you always want — `attendees`, `decisions`, `action_items`
for meetings; `title`, `repro_steps`, `environment`, `severity` for
bugs. Drops the "did I remember to capture X?" overhead.
</details>

---

## Schema immutability, versioning, and deletion protection

**Immutable.** Once a schema is registered, its body can't be changed.
Register a new version (`album:2`) instead. This guarantee is what
lets records safely pin an exact version at write time — the agent
validating `album:1` records five years from now is running against
the same rules as the day the first record was written.

**Versioned.** Records store both `schema_ref` (family) and
`schema_version` (exact version). A single family can have many
versions in flight simultaneously. Migrating records from one version
to another is a deliberate action, not an implicit one.

**Deletion-protected.** `delete_entry` on a schema blocks if any
records still reference it. This is why immutability and versioning
matter together: you can't accidentally orphan a record by deleting
its schema out from under it. To retire an old version, migrate its
records to a newer version first (or to a different shape), then
delete the schema.

**Known gap.** As of v0.18.0, bulk `delete_entry` paths (by tags, by
source) don't yet consult the referencing-record check. Single-id
deletion is protected. Tracked at
[#288](https://github.com/cmeans/mcp-awareness/issues/288).

---

## Operator-seeded `_system` schemas (CLI)

If you're running an awareness instance and want certain schemas
available to every user on the server (e.g., a standard
`edge-manifest` schema that all edge providers can reference), the
`mcp-awareness-register-schema` CLI registers schemas under the
shared `_system` owner namespace. Users looking up a schema fall back
to `_system` if they don't have a per-owner version of the same
family.

```
mcp-awareness-register-schema --system \
    --family edge-manifest --version 1 \
    --schema /etc/awareness/schemas/edge-manifest.v1.json \
    --description "Edge provider capability manifest"
```

Typical pattern:

- At deploy time, your Docker image or compose config runs this for
  each built-in schema.
- Agents and edge providers reference the schema as if it were
  owned by the current user; the store transparently falls back to
  `_system`.

This is how built-in schemas stay versioned and deletion-protected
just like user-registered ones, without being duplicated per tenant.

---

## What's next

A few threads already in motion that will make schema/record more
powerful:

- **REST API** (roadmap) — HTTP surface for writing and reading
  schemas and records, useful for non-MCP clients and web UIs.
- **Schema marketplace / one-click import** — once the REST API
  lands, we're planning a way to import community-contributed schemas
  (plus optional starter records) into your instance with a single
  click. Imagine "install the Music Collection pack" or "import this
  shared tag vocabulary." Tracked in awareness as
  `design-schema-marketplace-import`.
- **Tag Taxonomy Layer C** — wires user-defined tag records into the
  server's tag resolution. Design at `design-tag-taxonomy-v2`.
- **Cross-schema `$ref`** ([#291](https://github.com/cmeans/mcp-awareness/issues/291))
  — compose schemas from reusable fragments.
- **Validator caching** ([#290](https://github.com/cmeans/mcp-awareness/issues/290))
  — perf win at edge-manifest scale.
- **Generic `create_entry`** ([#292](https://github.com/cmeans/mcp-awareness/issues/292))
  — collapse type-specific write tools behind a single polymorphic
  tool.
- **Record-version migration** ([#293](https://github.com/cmeans/mcp-awareness/issues/293))
  — a first-class way to move records from `album:1` to `album:2`.

---

## Reference

- [Data Dictionary](data-dictionary.md) — entry schemas for `schema`
  and `record`, including all fields, indexes, and constraints.
- [Schema/Record design doc](superpowers/specs/2026-04-13-schema-record-entry-types-design.md)
  — the design this implementation shipped from.
- [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12)
  — the external spec schemas conform to.
