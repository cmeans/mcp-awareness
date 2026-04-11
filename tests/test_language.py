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

"""Tests for the language resolution helpers."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from unittest.mock import patch

import pytest

import mcp_awareness.language as lang_mod
from mcp_awareness.language import (
    ISO_639_1_TO_REGCONFIG,
    REGCONFIG_TO_ISO_639_1,
    SIMPLE,
    detect_language,
    iso_to_regconfig,
    regconfig_to_iso,
    resolve_language,
)


class TestIsoToRegconfig:
    def test_known_code_maps(self) -> None:
        assert iso_to_regconfig("en") == "english"
        assert iso_to_regconfig("fr") == "french"
        assert iso_to_regconfig("ar") == "arabic"
        assert iso_to_regconfig("ru") == "russian"

    def test_none_returns_simple(self) -> None:
        assert iso_to_regconfig(None) == SIMPLE

    def test_empty_returns_simple(self) -> None:
        assert iso_to_regconfig("") == SIMPLE

    def test_whitespace_only_returns_simple(self) -> None:
        assert iso_to_regconfig("   ") == SIMPLE

    def test_unknown_code_returns_simple(self) -> None:
        assert iso_to_regconfig("xx") == SIMPLE
        assert iso_to_regconfig("klingon") == SIMPLE

    def test_case_insensitive(self) -> None:
        assert iso_to_regconfig("EN") == "english"
        assert iso_to_regconfig("En") == "english"
        assert iso_to_regconfig(" en ") == "english"


class TestRegconfigToIso:
    def test_known_regconfig_maps(self) -> None:
        assert regconfig_to_iso("english") == "en"
        assert regconfig_to_iso("russian") == "ru"
        assert regconfig_to_iso("arabic") == "ar"

    def test_simple_returns_none(self) -> None:
        assert regconfig_to_iso(SIMPLE) is None

    def test_none_returns_none(self) -> None:
        assert regconfig_to_iso(None) is None

    def test_empty_returns_none(self) -> None:
        assert regconfig_to_iso("") is None

    def test_unknown_regconfig_returns_none(self) -> None:
        assert regconfig_to_iso("elvish") is None


class TestDetectLanguage:
    def test_empty_text_returns_none(self) -> None:
        assert detect_language("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert detect_language("   \n\t  ") is None

    def test_very_short_text_returns_none(self) -> None:
        # Below the min length threshold, detection is unreliable
        assert detect_language("hi") is None
        assert detect_language("hello") is None

    def test_detects_english(self) -> None:
        text = "The quick brown fox jumps over the lazy dog and runs into the woods."
        assert detect_language(text) == "english"

    def test_detects_french(self) -> None:
        text = "Le renard brun rapide saute par-dessus le chien paresseux et court dans les bois."
        assert detect_language(text) == "french"

    def test_detects_spanish(self) -> None:
        text = (
            "El rápido zorro marrón salta sobre el perro perezoso y "
            "corre hacia el bosque sin parar."
        )
        assert detect_language(text) == "spanish"

    def test_detects_german(self) -> None:
        text = (
            "Der schnelle braune Fuchs springt über den faulen Hund und läuft in den Wald hinein."
        )
        assert detect_language(text) == "german"

    def test_japanese_returns_none_pending_pgroonga_verification(self) -> None:
        # lingua correctly identifies this as Japanese, but `ja` is not in
        # ISO_639_1_TO_REGCONFIG (the 4 pgroonga entries were removed pending
        # #249's verification of whether pgroonga registers regconfigs).  So
        # detect_language returns None and the caller falls back to SIMPLE.
        # When #249 resolves and CJK support is added back via the chosen
        # mechanism, this test should be updated to assert the actual
        # regconfig name.
        text = "今日は東京で友達と夕食を食べました。とても楽しい時間を過ごしました。"
        assert detect_language(text) is None

    def test_garbage_input_returns_none_or_simple_fallback(self) -> None:
        # Random symbols / numbers are not in any language — lingua should
        # either return None or a language not in our regconfig map, and in
        # both cases we return None so the caller falls back to SIMPLE.
        assert detect_language("!@#$%^&*()_+=-[]{}|;:',.<>/?") is None


class TestResolveLanguage:
    def test_explicit_override_wins(self) -> None:
        result = resolve_language(
            explicit="en",
            user_preference="fr",
            text_for_detection="Le renard brun rapide saute par-dessus.",
        )
        assert result == "english"

    def test_user_preference_when_no_explicit(self) -> None:
        result = resolve_language(
            explicit=None,
            user_preference="fr",
            text_for_detection="The quick brown fox jumps over the lazy dog.",
        )
        assert result == "french"

    def test_detection_when_no_explicit_or_preference(self) -> None:
        result = resolve_language(
            text_for_detection="The quick brown fox jumps over the lazy dog and runs.",
        )
        assert result == "english"

    def test_fallback_to_simple_when_all_empty(self) -> None:
        assert resolve_language() == SIMPLE
        assert resolve_language(None, None, None) == SIMPLE

    def test_fallback_to_simple_when_detection_short(self) -> None:
        assert resolve_language(text_for_detection="hi") == SIMPLE

    def test_unknown_explicit_falls_through_to_preference(self) -> None:
        # Unknown explicit code should not short-circuit — fall through to preference
        result = resolve_language(
            explicit="xx",
            user_preference="fr",
        )
        assert result == "french"

    def test_unknown_explicit_and_preference_falls_through_to_detection(self) -> None:
        result = resolve_language(
            explicit="xx",
            user_preference="yy",
            text_for_detection="The quick brown fox jumps over the lazy dog and runs.",
        )
        assert result == "english"

    def test_all_unknown_returns_simple(self) -> None:
        result = resolve_language(
            explicit="xx",
            user_preference="yy",
            text_for_detection="hi",
        )
        assert result == SIMPLE

    def test_empty_string_explicit_falls_through(self) -> None:
        result = resolve_language(
            explicit="",
            user_preference="en",
        )
        assert result == "english"

    def test_japanese_text_routes_to_simple_pending_pgroonga_verification(self) -> None:
        # User-facing consequence of the CJK + Hebrew deferral.  Full chain:
        # lingua correctly detects the input as Japanese, detect_language()
        # filters through ISO_639_1_TO_REGCONFIG.get('ja') which is now None
        # (the 4 pgroonga entries were removed pending #249), so resolve_language()
        # falls through to SIMPLE.  This is the user-facing-behavior layer of
        # the deferral, complementing test_pgroonga_codes_intentionally_absent
        # (data-shape layer) and test_japanese_returns_none_pending_pgroonga_verification
        # (unit-behavior layer).  Update when CJK is added back via #249.
        result = resolve_language(
            text_for_detection=(
                "今日は東京で友達と夕食を食べました。とても楽しい時間を過ごしました。"
            )
        )
        assert result == SIMPLE


class TestMappingCoverage:
    """Structural checks that guard the mapping tables."""

    def test_reverse_mapping_is_consistent(self) -> None:
        # Every forward entry must have a matching reverse entry
        for iso, regconfig in ISO_639_1_TO_REGCONFIG.items():
            assert REGCONFIG_TO_ISO_639_1[regconfig] == iso

    def test_iso_codes_are_lowercase_two_letters(self) -> None:
        for iso in ISO_639_1_TO_REGCONFIG:
            assert iso == iso.lower(), f"ISO code {iso!r} not lowercase"
            assert len(iso) == 2, f"ISO code {iso!r} not two letters"

    def test_expected_languages_present(self) -> None:
        # Core language coverage sanity check.  CJK (`ja`, `zh`, `ko`) and
        # Hebrew (`he`) are intentionally NOT in the expected set — they
        # were removed from ISO_639_1_TO_REGCONFIG pending #249's
        # verification of pgroonga's regconfig integration.  Restore them
        # here when the wiring PR adds CJK + Hebrew support back via the
        # chosen mechanism.
        expected = {"en", "fr", "de", "es", "ar", "ru", "pt", "it", "nl", "tr"}
        missing = expected - ISO_639_1_TO_REGCONFIG.keys()
        assert not missing, f"missing expected languages: {missing}"

    def test_pgroonga_codes_intentionally_absent(self) -> None:
        # The 4 pgroonga-listed codes were removed from the mapping pending
        # #249's verification of whether pgroonga registers regconfigs.
        # This test documents the deferral at the test level so a future
        # dev who tries to add them back without resolving #249 first sees
        # the assertion failure with a clear pointer.
        deferred = {"ja", "zh", "ko", "he"}
        present = deferred & ISO_639_1_TO_REGCONFIG.keys()
        assert not present, (
            f"CJK / Hebrew codes {present} are in ISO_639_1_TO_REGCONFIG "
            f"but should be deferred pending #249.  If #249 is resolved "
            f"and the pgroonga regconfigs are confirmed (or replaced with "
            f"a verified alternative), update this test along with the "
            f"new entries."
        )

    def test_simple_not_in_forward_mapping(self) -> None:
        # 'simple' should not have an ISO code mapping — it is the fallback
        assert SIMPLE not in ISO_639_1_TO_REGCONFIG.values()


class TestDetectorCaching:
    """Verify the lazy singleton pattern for the lingua detector."""

    @pytest.fixture(autouse=True)
    def _save_detector_state(self) -> Iterator[None]:
        # Snapshot module-level detector state and restore it after each
        # test in this class so mutations don't bleed across tests or
        # into the rest of the test session.
        saved_detector = lang_mod._detector
        saved_probed = lang_mod._detector_probed
        try:
            yield
        finally:
            lang_mod._detector = saved_detector
            lang_mod._detector_probed = saved_probed

    def test_detector_is_lazy_loaded_and_cached(self) -> None:
        # Reset the module-level cache to force a reload
        lang_mod._detector = None
        lang_mod._detector_probed = False
        detector1 = lang_mod._get_detector()
        detector2 = lang_mod._get_detector()
        # Same object returned on subsequent calls
        assert detector1 is detector2
        # Probed flag is set after first call
        assert lang_mod._detector_probed is True

    def test_get_detector_returns_none_when_lingua_not_installed(self) -> None:
        # Simulate lingua-py not being installed.  Setting the entry in
        # sys.modules to None causes `from lingua import ...` to raise
        # ImportError, exercising the fallback branch in _get_detector.
        lang_mod._detector = None
        lang_mod._detector_probed = False
        with patch.dict(sys.modules, {"lingua": None}):
            assert lang_mod._get_detector() is None
            # Second call returns the cached None without re-probing
            assert lang_mod._get_detector() is None
            assert lang_mod._detector_probed is True

    def test_detect_language_returns_none_when_detector_unavailable(self) -> None:
        # When _get_detector returns None (lingua not installed), detect_language
        # must short-circuit to None so callers fall back to SIMPLE.
        with patch.object(lang_mod, "_get_detector", return_value=None):
            text = "The quick brown fox jumps over the lazy dog and runs."
            assert lang_mod.detect_language(text) is None
