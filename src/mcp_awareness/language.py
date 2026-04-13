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

"""Language detection and resolution for multilingual hybrid retrieval.

Provides the language resolution chain used at write time and query time:

  1. Explicit override (caller-provided ISO 639-1 code)
  2. User preference (ISO 639-1 code from ``users.preferences``)
  3. Auto-detection via lingua-py on composed text
  4. Fall back to ``'simple'`` (word-boundary tokenization, always works)

ISO 639-1 codes are the API boundary format.  Postgres ``regconfig`` names
are the internal format.  Mapping happens at the boundary via
:func:`iso_to_regconfig` and :func:`regconfig_to_iso`.

This module is deliberately pure — no module-level store/db access — and
the lingua detector is lazily imported so tests that don't exercise
detection don't pay the import cost.

Part of Layer 1 of the hybrid retrieval design
(``docs/design/hybrid-retrieval-multilingual.md``).

Cost considerations
-------------------

Language support has two distinct cost dimensions that scale very
differently and need separate awareness when reading or modifying this
module.  Both are tracked as follow-ups for the wiring PR; both are
named here so a future reader gets the full picture from one place.

**Client-side (this Python process)** — the lingua detector loaded by
:func:`_get_detector` carries an n-gram model for every supported
language.  Per lingua's own documentation this is on the order of
several hundred MB resident set, with a multi-second build cost paid
lazily on the first call to :func:`detect_language`.  See the
:func:`_get_detector` docstring for the full analysis, including why
the "narrow to supported languages" alternative
(``LanguageDetectorBuilder.from_languages(*supported)``) was rejected
on correctness grounds — narrowing produces false positives on
out-of-set text rather than the correct ``None``-then-``simple``
fallback.  Latency mitigation (background warmup at server start when
detection will be enabled) is tracked as #247.

**Server-side (Postgres backend)** — the 28 regconfigs in
:data:`ISO_639_1_TO_REGCONFIG` are all stock snowball-based and
essentially free per backend: the Snowball stemmer is compiled into
the Postgres binary as C code, stop-word lists at
``$PGSHARE/tsearch_data/*.stop`` are roughly 2 KB each, and configs
are cached per-backend in ``TSCacheEntry`` after first use.
Order-of-magnitude estimate is ~10-50 KB per language per backend, so
all 28 in one backend ≈ 0.5-1.5 MB.  This estimate is based on
architectural reasoning about Postgres' FTS infrastructure and **has
not been measured against this codebase**; the verification plan is
tracked in #248.

**CJK and Hebrew are intentionally NOT in this mapping.** An earlier
version of this module included 4 pgroonga-listed entries (``ja`` →
``japanese``, ``zh`` → ``chinese_simplified``, ``ko`` → ``korean``,
``he`` → ``hebrew``) on the assumption that installing pgroonga
registers those names in ``pg_ts_config``.  Verification via context7
against pgroonga's official documentation (during PR #246's QA cycle)
showed that pgroonga's documented integration model is its own
PostgreSQL index access method (``USING pgroonga``) with per-index
tokenizer configuration (``WITH (tokenizer = 'TokenMecab')``) and
pgroonga-specific operators (``&@``, ``&@~``, ``&^``, ``%%``) — a
completely different mechanism from Postgres' standard FTS
infrastructure (``to_tsvector(regconfig, text)``, ``tsvector``,
``tsquery``, ``pg_ts_config``) that this mapping is built around.
None of pgroonga's documented examples involve the standard FTS path.

The 4 pgroonga entries were therefore removed pending #249's
verification of whether the regconfig path is actually available on
the target Postgres + pgroonga build.

For Chinese specifically, **zhparser** is a counter-example proving
the parser-extension approach is viable.  Verified via context7
against zhparser's official documentation
(``/amutu/zhparser``, during PR #246's QA cycle, same approach as
the pgroonga finding above): zhparser registers as a PostgreSQL
parser, and an operator builds a usable regconfig on top of it via
the standard ``CREATE TEXT SEARCH CONFIGURATION ... (PARSER = zhparser)``
mechanism.  The resulting regconfig works with
``to_tsvector(regconfig, text)``, generated ``tsvector`` columns,
GIN indexes, and the standard FTS query operators (``@@``,
``to_tsquery``, ``plainto_tsquery``, ``ts_rank``, ``ts_headline``).
zhparser proves the design pattern works for at least one
non-Western language with the right extension; pgroonga is just the
wrong extension for this specific pattern.

Japanese / Korean / Hebrew parser-extension equivalents have **not**
been confirmed.  The equivalent context7 query for Japanese parser
extensions (run during PR #246's design-doc-edit work, with library
search ``"textsearch_ja PostgreSQL Japanese"``) returned only
``/takuyaa/ja-law-parser`` — a Japanese law-text parser, not a
PostgreSQL FTS extension — so no high-confidence parallel to zhparser
was found in context7's index.  Korean and Hebrew were not
exhaustively searched.  This is a negative verification result
(query ran, returned nothing useful), not absence of verification.
The wiring PR will either add CJK + Hebrew back to this mapping after
identifying and verifying the appropriate extensions, use a separate
pgroonga code path with a branched query CTE, or defer non-Western
language support to a follow-up phase after Layer 1.  See #249 for
the verification plan and the three-option trilemma.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from lingua import LanguageDetector

logger = logging.getLogger(__name__)

#: The universal fallback regconfig.  Does word-boundary tokenization with
#: no stemming or stopwords.  Works for any language — never breaks a write
#: — but loses stem-based recall on in-language FTS queries.
SIMPLE: Final[str] = "simple"

#: ISO 639-1 (two-letter) → Postgres ``regconfig`` name.
#:
#: Currently covers the 28 stock snowball-based regconfigs built into
#: Postgres (``english``, ``french``, ``arabic``, ``hindi``, ``russian``,
#: ``turkish``, etc.).  Codes not in this map fall back to :data:`SIMPLE`.
#:
#: **CJK and Hebrew are intentionally deferred.**  An earlier version of
#: this mapping included 4 pgroonga-listed entries (``ja``, ``zh``,
#: ``ko``, ``he``) on the assumption that installing pgroonga registers
#: ``japanese`` / ``chinese_simplified`` / ``korean`` / ``hebrew`` as
#: standard Postgres regconfigs accessible via
#: ``to_tsvector(regconfig, text)``.  Verification via context7 against
#: pgroonga's official documentation (during PR #246's QA cycle) showed
#: that pgroonga's documented integration is its own PostgreSQL index
#: access method (``USING pgroonga``), not the standard regconfig
#: registry.  See the "Cost considerations" section of this module's
#: docstring for the full finding.  The 4 entries were removed rather
#: than shipped with a caveat because the mapping is the public API of
#: this module — downstream code (the wiring PR, tests, any consumer
#: that imports this dict) needs to be able to trust that values in the
#: mapping are real regconfigs, and the asymmetry of error costs favors
#: removing unverified data over shipping it with documentation.
#:
#: The wiring PR will either add CJK + Hebrew back after verifying the
#: appropriate extensions (zhparser is the verified counter-example for
#: Chinese — see the "Cost considerations" section of this module's
#: docstring for the citation; Japanese / Korean / Hebrew equivalents
#: need verification), use a separate pgroonga code path with a
#: branched query CTE, or defer non-Western language support to a
#: follow-up phase after Layer 1.  Verification plan and the
#: three-option trilemma are tracked in #249.  Memory-cost measurement
#: of whatever extensions are chosen is tracked in #248 (blocked on
#: #249).
ISO_639_1_TO_REGCONFIG: Final[dict[str, str]] = {
    # Built into stock Postgres (verified — 28 entries)
    "ar": "arabic",
    "hy": "armenian",
    "eu": "basque",
    "ca": "catalan",
    "da": "danish",
    "nl": "dutch",
    "en": "english",
    "fi": "finnish",
    "fr": "french",
    "de": "german",
    "el": "greek",
    "hi": "hindi",
    "hu": "hungarian",
    "id": "indonesian",
    "ga": "irish",
    "it": "italian",
    "lt": "lithuanian",
    "ne": "nepali",
    "no": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sr": "serbian",
    "es": "spanish",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
    "yi": "yiddish",
    # CJK and Hebrew support intentionally deferred from this foundation
    # PR.  The pgroonga-listed regconfigs (japanese, chinese_simplified,
    # korean, hebrew) assume pgroonga registers entries in pg_ts_config,
    # which is not part of pgroonga's documented integration model and
    # has not been verified.  Tracked as #249; the wiring PR will either
    # add these entries back after verification, use a separate pgroonga
    # code path with a branched query CTE, or defer non-Western language
    # support to a follow-up phase after Layer 1.  See the "Cost
    # considerations" section of this module's docstring for the full
    # finding.
}

#: Reverse lookup: ``regconfig`` name → ISO 639-1 code.  Does not include
#: ``simple`` (which has no corresponding ISO code).
REGCONFIG_TO_ISO_639_1: Final[dict[str, str]] = {v: k for k, v in ISO_639_1_TO_REGCONFIG.items()}

# Minimum text length before we attempt auto-detection.  Shorter text
# produces unreliable results (lingua happily classifies two-word inputs
# as obscure languages based on coincidental n-grams).  20 characters is
# the lower bound documented by lingua-py for acceptable accuracy.
_MIN_DETECTION_LENGTH: Final[int] = 20

# Lazy singleton state for the lingua detector.
# ``_detector_probed`` flips to True after the first load attempt so we
# don't re-probe on every call.  ``_detector`` is None either because we
# haven't probed yet or because lingua-py isn't installed — callers must
# check ``_detector_probed`` to distinguish.
_detector: LanguageDetector | None = None
_detector_probed: bool = False


def iso_to_regconfig(iso_code: str | None) -> str:
    """Map an ISO 639-1 code to a Postgres ``regconfig`` name.

    Returns :data:`SIMPLE` for ``None``, empty strings, whitespace-only
    strings, or codes not present in :data:`ISO_639_1_TO_REGCONFIG`.
    Case-insensitive: ``"EN"``, ``"en"``, and ``" En "`` all map to
    ``"english"``.
    """
    if not iso_code:
        return SIMPLE
    normalized = iso_code.strip().lower()
    return ISO_639_1_TO_REGCONFIG.get(normalized, SIMPLE)


def regconfig_to_iso(regconfig: str | None) -> str | None:
    """Map a Postgres ``regconfig`` name to an ISO 639-1 code.

    Returns ``None`` for :data:`SIMPLE`, empty strings, ``None``, or
    ``regconfig`` names not present in :data:`REGCONFIG_TO_ISO_639_1`.

    Case-sensitive on input — ``regconfig`` names are canonical.
    """
    if not regconfig or regconfig == SIMPLE:
        return None
    return REGCONFIG_TO_ISO_639_1.get(regconfig)


def _get_detector() -> LanguageDetector | None:
    """Lazy-load the lingua ``LanguageDetector``.

    Returns the detector on success, ``None`` if lingua-py is not
    installed.  Caches the result — subsequent calls are cheap.

    **Cost note.** ``LanguageDetectorBuilder.from_all_languages().build()``
    loads lingua's high-accuracy n-gram models for all ~75 supported
    languages.  Per lingua's own documentation this is on the order of
    several hundred MB resident set, and the build itself takes
    multiple seconds on a typical machine.  This cost is paid lazily
    on the first call rather than at module import, so the cost is
    invisible until the first detection is requested.

    The "narrow to supported languages" alternative
    (``from_languages(*supported)``) was deliberately rejected: lingua's
    cross-language disambiguation depends on having all candidate
    languages in scope, so narrowing produces false positives — text in
    an unsupported language (e.g. Tagalog) gets misclassified as the
    closest supported language with high confidence rather than
    correctly returning ``None`` so the caller can fall back to
    :data:`SIMPLE`.  The footprint cost is the price of correct
    fallback semantics.

    Latency mitigation (background warmup at server start when
    detection will be enabled) is tracked as #247, queued for the
    wiring PR.  For the parallel server-side cost (Postgres backend
    for the 28 stock regconfigs in :data:`ISO_639_1_TO_REGCONFIG`)
    and the deferral of CJK + Hebrew pending #249, see the "Cost
    considerations" section of this module's docstring.
    """
    global _detector, _detector_probed
    if _detector_probed:
        return _detector
    _detector_probed = True
    try:
        from lingua import LanguageDetectorBuilder
    except ImportError:
        logger.debug("lingua-py not installed; language detection disabled")
        return None
    _detector = LanguageDetectorBuilder.from_all_languages().build()
    return _detector


def detect_language(text: str) -> str | None:
    """Detect the language of text and return its Postgres ``regconfig``.

    Returns ``None`` — meaning "unknown, caller should fall back to
    :data:`SIMPLE`" — in any of these cases:

      - ``text`` is empty, whitespace-only, or shorter than
        :data:`_MIN_DETECTION_LENGTH` characters
      - lingua-py is not installed
      - lingua cannot classify the input confidently enough to commit
      - the detected language is not in :data:`ISO_639_1_TO_REGCONFIG`
        (e.g. lingua supports Latin and Maori, but neither has a
        Postgres regconfig)

    Callers should treat ``None`` as a signal to fall through the
    resolution chain, not as an error.

    Uses ``detect_language_of`` rather than
    ``compute_language_confidence_values`` because the former applies
    lingua's own internal decision logic (which accounts for vocabulary
    overlap between similar languages) and returns ``None`` when lingua
    is genuinely unsure.  The raw confidence values returned by
    ``compute_language_confidence_values`` are normalized across all
    languages and are not comparable across inputs.
    """
    if not text or len(text.strip()) < _MIN_DETECTION_LENGTH:
        return None
    detector = _get_detector()
    if detector is None:
        return None
    detected = detector.detect_language_of(text)
    if detected is None:
        return None
    iso = detected.iso_code_639_1.name.lower()
    return ISO_639_1_TO_REGCONFIG.get(iso)


def detect_language_iso(text: str) -> str | None:
    """Detect the language of text and return the raw ISO 639-1 code.

    Unlike :func:`detect_language`, this returns the ISO code even when
    it's not in :data:`ISO_639_1_TO_REGCONFIG`.  Returns ``None`` when
    detection fails or text is too short.  Used by the unsupported-language
    alert infrastructure to identify *which* unsupported language was detected.
    """
    if not text or len(text.strip()) < _MIN_DETECTION_LENGTH:
        return None
    detector = _get_detector()
    if detector is None:
        return None
    detected = detector.detect_language_of(text)
    if detected is None:
        return None
    return detected.iso_code_639_1.name.lower()


def resolve_language(
    explicit: str | None = None,
    user_preference: str | None = None,
    text_for_detection: str | None = None,
) -> str:
    """Resolve the effective Postgres ``regconfig`` for an entry.

    Precedence (first non-``SIMPLE`` hit wins):

      1. ``explicit`` — caller-supplied ISO 639-1 override
      2. ``user_preference`` — ISO 639-1 code from the writer's profile
      3. Auto-detection on ``text_for_detection`` via lingua-py
      4. :data:`SIMPLE`

    All arguments are optional; callers pass what they have.  Unknown ISO
    codes at levels 1 and 2 do not short-circuit the chain — they fall
    through to the next level.

    Always returns a non-empty, valid Postgres ``regconfig`` name.
    """
    if explicit:
        mapped = iso_to_regconfig(explicit)
        if mapped != SIMPLE:
            return mapped

    if user_preference:
        mapped = iso_to_regconfig(user_preference)
        if mapped != SIMPLE:
            return mapped

    if text_for_detection:
        detected = detect_language(text_for_detection)
        if detected:
            return detected

    return SIMPLE


def compose_detection_text(entry_type: str, data: Mapping[str, object]) -> str:
    """Build the text string used for language detection from entry fields.

    Each entry type uses the same field composition as its write tool:

    - ``pattern``: description + effect (matches ``learn_pattern``)
    - ``note``: description + content (matches ``remember``)
    - ``context``: description only (matches ``add_context``)
    - ``intention``: goal only (matches ``remind``)
    - all others: description only (safe default)

    This function is the single source of truth for detection text
    composition — used by both write tools and the backfill script.
    """
    desc = str(data.get("description") or "")
    entry_type_lower = entry_type.lower()

    if entry_type_lower == "pattern":
        effect = str(data.get("effect") or "")
        return f"{desc} {effect}".strip()
    elif entry_type_lower == "note":
        content = str(data.get("content") or "")
        return f"{desc} {content}".strip()
    elif entry_type_lower == "intention":
        goal = str(data.get("goal") or "")
        return goal or desc
    else:
        return desc
