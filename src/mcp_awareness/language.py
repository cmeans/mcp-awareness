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
detection will be enabled) is tracked as
`#247 <https://github.com/cmeans/mcp-awareness/issues/247>`_.

**Server-side (Postgres backend)** — the regconfigs in
:data:`ISO_639_1_TO_REGCONFIG` carry very different server-side cost
depending on type.  The 28 stock snowball-based configs are
essentially free: the Snowball stemmer is compiled into the Postgres
binary as C code, stop-word lists at ``$PGSHARE/tsearch_data/*.stop``
are roughly 2 KB each, and configs are cached per-backend in
``TSCacheEntry`` after first use.  Order-of-magnitude estimate is
~10-50 KB per language per backend, so all 28 in one backend ≈
0.5-1.5 MB.  This estimate is based on architectural reasoning about
Postgres' FTS infrastructure and **has not been measured against this
codebase.**

**The 4 pgroonga-listed entries (japanese, chinese_simplified,
korean, hebrew) are doubly unverified — both whether they exist as
Postgres regconfigs at all, and what their cost would be if they
do.**  pgroonga's documented integration model is its own
PostgreSQL index access method (``USING pgroonga``), with tokenizers
configured per-index via ``WITH (tokenizer = 'TokenMecab')`` and
queries written with pgroonga-specific operators (``&@``, ``&@~``,
``&^``, ``%%``).  This is a different mechanism from Postgres'
built-in FTS infrastructure (``to_tsvector(regconfig, text)``,
``tsvector``, ``tsquery``, the standard ``regconfig`` registry) that
this module's mapping is built around.  Whether installing pgroonga
also registers ``japanese`` / ``chinese_simplified`` / ``korean`` /
``hebrew`` as entries in ``pg_ts_config`` — so that
``to_tsvector('japanese', text)`` works through Postgres' FTS path
rather than only through pgroonga's index access method — has not
been verified against this codebase or against pgroonga's actual
behavior on the target Postgres version.  If they are not registered
as standard regconfigs, the 4 pgroonga entries in this mapping are
incorrect data and the wiring PR will need a different mechanism for
CJK and Hebrew support (either a separate pgroonga-based code path,
a Groonga-bridging extension, or removal of CJK from Layer 1
entirely).  This deeper finding is tracked as #249.  The original
memory-cost question (assuming the regconfigs do exist) is tracked
as #248, with #249 as a prerequisite.

The two cost dimensions are independent: documentation in this module
treats them in parallel rather than treating client-side as load-
bearing and server-side as a footnote.
"""

from __future__ import annotations

import logging
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
#: Coverage:
#:   - 28 configurations built into stock Postgres (``english``, ``french``,
#:     ``arabic``, ``hindi``, ``russian``, ``turkish``, etc.)
#:   - 4 additional configurations via the ``pgroonga`` extension
#:     (``japanese``, ``chinese_simplified``, ``korean``, ``hebrew``)
#:
#: Codes not in this map fall back to :data:`SIMPLE`.
#:
#: **pgroonga entries are unverified.** The 4 pgroonga-listed entries
#: (``ja``, ``zh``, ``ko``, ``he``) assume that installing pgroonga
#: registers ``japanese`` / ``chinese_simplified`` / ``korean`` /
#: ``hebrew`` as standard Postgres regconfigs accessible via
#: ``to_tsvector(regconfig, text)``.  pgroonga's documented integration
#: model is its own PostgreSQL index access method (``USING pgroonga``)
#: with per-index tokenizer configuration, which is a different
#: mechanism from Postgres' built-in FTS infrastructure that this
#: mapping is built around.  Whether the regconfig path is also
#: available has not been verified.  If it is not, these 4 entries are
#: incorrect data and need to be removed (or replaced with a different
#: CJK / Hebrew support mechanism in the wiring PR).  Tracked as #249.
#:
#: **Server-side cost.** The 28 stock snowball-based regconfigs are
#: essentially free per backend (estimate ~10-50 KB each, unmeasured).
#: The 4 pgroonga-listed entries carry an unverified cost on top of an
#: unverified existence question (see above).  See the "Cost
#: considerations" section of this module's docstring for the full
#: analysis; the memory-cost verification plan is tracked as #248
#: (blocked on #249).
#:
#: **Chinese caveat.** ISO 639-1 ``zh`` is the macro code for Chinese and
#: does not distinguish Simplified from Traditional script — that
#: distinction requires ISO 15924 suffixes (``zh-Hans`` / ``zh-Hant``)
#: which are not part of ISO 639-1.  Postgres has no
#: ``chinese_traditional`` regconfig either; pgroonga only ships
#: ``chinese_simplified``.  Both Simplified and Traditional Chinese text
#: therefore route to ``chinese_simplified``.  Whether pgroonga's
#: ``chinese_simplified`` analyzer handles Traditional script consistently
#: depends on which Groonga tokenizer it wraps and how that tokenizer
#: treats Han variants — neither has been verified against this codebase.
#: End-to-end verification against real Traditional Chinese text is
#: tracked as a QA item for the wiring PR.
ISO_639_1_TO_REGCONFIG: Final[dict[str, str]] = {
    # Built into stock Postgres
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
    # Provided by pgroonga extension (installed separately)
    "ja": "japanese",
    "zh": "chinese_simplified",
    "ko": "korean",
    "he": "hebrew",
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
    for the regconfigs in :data:`ISO_639_1_TO_REGCONFIG`, plus the
    deeper unverified question of whether the 4 pgroonga-listed
    entries are real regconfigs at all), see the "Cost considerations"
    section of this module's docstring.
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
