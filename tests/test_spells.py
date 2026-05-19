"""Unit tests for spell slot init, rest mechanics, and turn-order advancement."""
import pytest
from main import (
    Session,
    SESSIONS,
    init_spell_slots_for_party,
    apply_rest,
    advance_active_character,
    _normalise_char,
    char_name,
)


def _make_session(sid: str, party_raw: list) -> Session:
    sess = Session()
    sess.party = [_normalise_char(c) for c in party_raw]
    SESSIONS[sid] = sess
    return sess


class TestInitSpellSlots:
    def test_wizard_gets_slots(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        init_spell_slots_for_party(fresh_session_id)
        aria = next(c for c in sess.party if c["name"] == "Aria")
        assert "_slots_max" in aria
        assert len(aria["_slots_max"]) == 9
        assert aria["_slots_max"][0] > 0   # level-1 slots for wizard 3

    def test_fighter_gets_no_slots(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        init_spell_slots_for_party(fresh_session_id)
        thorin = next(c for c in sess.party if c["name"] == "Thorin")
        assert "_slots_max" not in thorin

    def test_warlock_slots_at_correct_level(self, fresh_session_id, party_warlock):
        sess = _make_session(fresh_session_id, party_warlock)
        init_spell_slots_for_party(fresh_session_id)
        zara = next(c for c in sess.party if c["name"] == "Zara")
        assert "_caster_type" in zara
        assert zara["_caster_type"] == "warlock"

    def test_existing_slots_not_overwritten(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        init_spell_slots_for_party(fresh_session_id)
        # Mark one slot used
        sess.spell_slots_used["Aria"] = [1, 0, 0, 0, 0, 0, 0, 0, 0]
        # Re-init should not reset used slots
        init_spell_slots_for_party(fresh_session_id)
        assert sess.spell_slots_used["Aria"][0] == 1


class TestApplyRest:
    def test_long_rest_restores_hp(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        init_spell_slots_for_party(fresh_session_id)
        thorin = next(c for c in sess.party if c["name"] == "Thorin")
        thorin["HP"] = 10   # simulate damage
        apply_rest(fresh_session_id, "long")
        assert thorin["HP"] == thorin.get("HP_max", thorin["HP"])

    def test_long_rest_resets_spell_slots(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        init_spell_slots_for_party(fresh_session_id)
        sess.spell_slots_used["Aria"] = [2, 1, 0, 0, 0, 0, 0, 0, 0]
        apply_rest(fresh_session_id, "long")
        assert sess.spell_slots_used["Aria"] == [0] * 9

    def test_short_rest_resets_warlock_slots(self, fresh_session_id, party_warlock):
        sess = _make_session(fresh_session_id, party_warlock)
        init_spell_slots_for_party(fresh_session_id)
        sess.spell_slots_used["Zara"] = [0, 0, 2, 0, 0, 0, 0, 0, 0]
        apply_rest(fresh_session_id, "short")
        assert sess.spell_slots_used["Zara"] == [0] * 9

    def test_short_rest_does_not_reset_wizard_slots(self, fresh_session_id, party_warlock):
        sess = _make_session(fresh_session_id, party_warlock)
        init_spell_slots_for_party(fresh_session_id)
        sess.spell_slots_used["Frère Aldric"] = [1, 0, 0, 0, 0, 0, 0, 0, 0]
        apply_rest(fresh_session_id, "short")
        assert sess.spell_slots_used["Frère Aldric"][0] == 1


class TestAdvanceActiveCharacter:
    def test_none_becomes_zero(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        sess.active_character = None
        advance_active_character(fresh_session_id)
        assert sess.active_character == 0

    def test_advances_by_one(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        sess.active_character = 0
        advance_active_character(fresh_session_id)
        assert sess.active_character == 1

    def test_wraps_around(self, fresh_session_id, party_two):
        sess = _make_session(fresh_session_id, party_two)
        sess.active_character = 1   # last index for 2-char party
        advance_active_character(fresh_session_id)
        assert sess.active_character == 0

    def test_empty_party_no_crash(self, fresh_session_id):
        sess = Session()
        SESSIONS[fresh_session_id] = sess
        advance_active_character(fresh_session_id)   # should not raise
