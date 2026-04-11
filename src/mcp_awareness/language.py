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
    detection will be enabled) is tracked as a separate follow-up — see
    the wiring PR for details.
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
