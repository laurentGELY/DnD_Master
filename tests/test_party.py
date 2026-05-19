"""Unit tests for party parsing, context building, caster detection, and Pydantic normalisation."""
import json
import pytest
from main import (
    extract_party_from_input,
    build_party_context,
    _get_caster_type,
    _normalise_char,
    char_name,
)


class TestNormaliseChar:
    def test_uppercase_keys_pass_through(self):
        char = _normalise_char({"name": "Thorin", "class": "Guerrier", "level": 3,
                                 "HP": 35, "AC": 17})
        assert char["name"] == "Thorin"
        assert char["HP"] == 35
        assert char["AC"] == 17

    def test_lowercase_hp_ac_normalised(self):
        char = _normalise_char({"name": "Aelar", "class": "Magicien", "level": 1,
                                 "hp": 12, "ac": 5})
        assert char["HP"] == 12
        assert char["AC"] == 5
        assert "hp" not in char
        assert "ac" not in char

    def test_french_keys_normalised(self):
        char = _normalise_char({"nom": "Zara", "classe": "Occultiste", "niveau": 5,
                                 "hp": 32, "ac": 13})
        assert char["name"] == "Zara"
        assert char["class"] == "Occultiste"
        assert char["level"] == 5

    def test_hp_current_maps_to_HP(self):
        char = _normalise_char({"name": "X", "hp_current": 10, "ac": 5})
        assert char["HP"] == 10

    def test_extra_fields_preserved(self):
        char = _normalise_char({"name": "Aelar", "hp": 12, "ac": 5,
                                 "gold": 20, "inventory": ["épée"]})
        assert char["gold"] == 20
        assert char["inventory"] == ["épée"]

    def test_internal_app_fields_preserved(self):
        char = _normalise_char({"name": "X", "hp": 10, "ac": 5,
                                 "_slots_max": [4, 2, 0, 0, 0, 0, 0, 0, 0]})
        assert char["_slots_max"] == [4, 2, 0, 0, 0, 0, 0, 0, 0]

    def test_stat_block_normalised(self):
        char = _normalise_char({"name": "X", "hp": 10, "ac": 5,
                                 "str": 16, "dex": 14, "con": 13,
                                 "int": 10, "wis": 11, "cha": 8})
        assert char["STR"] == 16
        assert char["DEX"] == 14


class TestExtractPartyFromInput:
    def test_bare_array(self, party_two):
        result = extract_party_from_input(json.dumps(party_two))
        assert result is not None
        assert len(result) == 2
        assert result[0]["name"] == "Thorin"

    def test_json_embedded_in_prose(self, party_two):
        msg = f"Voici mon groupe : {json.dumps(party_two)} merci"
        result = extract_party_from_input(msg)
        assert result is not None
        assert len(result) == 2

    def test_party_wrapper_list(self, party_two):
        msg = json.dumps({"party": party_two, "meta": {"session": 1}})
        result = extract_party_from_input(msg)
        assert result is not None
        assert len(result) == 2

    def test_party_wrapper_single_dict(self, session_save):
        # "party" is a single dict in the session save file
        result = extract_party_from_input(json.dumps(session_save))
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "Aelar"

    def test_characters_wrapper(self, party_two):
        msg = json.dumps({"characters": party_two})
        result = extract_party_from_input(msg)
        assert result is not None

    def test_invalid_json_returns_none(self):
        assert extract_party_from_input("{ not valid json }") is None

    def test_no_json_returns_none(self):
        assert extract_party_from_input("J'attaque le gobelin") is None

    def test_non_party_json_returns_none(self):
        assert extract_party_from_input('{"foo": "bar"}') is None

    def test_result_is_normalised(self, party_two):
        result = extract_party_from_input(json.dumps(party_two))
        # After normalisation HP/AC are always uppercase
        assert "HP" in result[0]
        assert "AC" in result[0]

    def test_lowercase_keys_normalised(self, party_aelar):
        result = extract_party_from_input(json.dumps(party_aelar))
        assert result is not None
        assert result[0]["HP"] == 12
        assert result[0]["AC"] == 5


class TestBuildPartyContext:
    def test_contains_character_name(self, party_two):
        from main import _normalise_char
        party = [_normalise_char(c) for c in party_two]
        ctx = build_party_context(party)
        assert "Thorin" in ctx
        assert "Aria" in ctx

    def test_active_marker_shown(self, party_two):
        from main import _normalise_char
        party = [_normalise_char(c) for c in party_two]
        ctx = build_party_context(party, active_idx=0)
        assert "◄ ACTIF" in ctx
        assert "PERSONNAGE QUI AGIT MAINTENANT: Thorin" in ctx

    def test_stat_modifiers_computed(self):
        char = _normalise_char({"name": "X", "class": "Guerrier", "level": 1,
                                 "HP": 10, "AC": 14, "STR": 16})
        ctx = build_party_context([char])
        assert "STR:16(+3)" in ctx

    def test_negative_modifier(self):
        char = _normalise_char({"name": "X", "class": "Guerrier", "level": 1,
                                 "HP": 10, "AC": 14, "CHA": 8})
        ctx = build_party_context([char])
        assert "CHA:8(-1)" in ctx


class TestGetCasterType:
    def test_wizard_is_full(self):
        assert _get_caster_type("Magicien") == "full"

    def test_cleric_is_full(self):
        assert _get_caster_type("Clerc") == "full"

    def test_warlock_detected(self):
        assert _get_caster_type("Occultiste") == "warlock"

    def test_fighter_is_none(self):
        assert _get_caster_type("Guerrier") is None

    def test_empty_string_is_none(self):
        assert _get_caster_type("") is None

    def test_case_insensitive(self):
        assert _get_caster_type("MAGICIEN") == "full"
