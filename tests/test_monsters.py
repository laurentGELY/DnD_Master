"""Unit tests for monster detection and context building."""
import pytest
from main import detect_monsters_in_text, build_monsters_context, MONSTERS_DB


class TestDetectMonstersInText:
    def test_combat_tag_strategy_a(self):
        result = detect_monsters_in_text("[COMBAT: gobelin]")
        assert len(result) == 1
        assert result[0]["key"] == "goblin"

    def test_combat_tag_multiple(self):
        result = detect_monsters_in_text("[COMBAT: gobelin, loup géant]")
        keys = [m["key"] for m in result]
        assert "goblin" in keys
        assert "dire_wolf" in keys

    def test_combat_tag_case_insensitive(self):
        result = detect_monsters_in_text("[COMBAT: Gobelin]")
        assert len(result) == 1

    def test_unknown_name_returns_empty(self):
        result = detect_monsters_in_text("[COMBAT: dragonrouge]")
        assert result == []

    def test_strategy_b_name_in_prose(self):
        result = detect_monsters_in_text("Un gobelin surgit de l'ombre.")
        assert any(m["key"] == "goblin" for m in result)

    def test_strategy_b_skips_short_names(self):
        # Names < 4 chars are skipped to avoid false positives
        result = detect_monsters_in_text("Le rat est là.")
        assert all(len(m.get("fr", [""])[0]) >= 4 for m in result)

    def test_no_monster_returns_empty(self):
        result = detect_monsters_in_text("Il fait beau aujourd'hui.")
        assert result == []

    def test_strategy_a_takes_priority_over_b(self):
        # Tag present → strategy A fires, strategy B not run
        result = detect_monsters_in_text("[COMBAT: gobelin] Un gobelin attaque.")
        # Should not duplicate
        keys = [m["key"] for m in result]
        assert keys.count("goblin") == 1

    def test_deduplication(self):
        # Two occurrences of same monster name in prose
        result = detect_monsters_in_text("gobelin gobelin gobelin")
        assert sum(1 for m in result if m["key"] == "goblin") == 1


class TestBuildMonstersContext:
    def test_empty_list_returns_empty_string(self):
        assert build_monsters_context([]) == ""

    def test_single_monster_contains_key_stats(self):
        monsters = detect_monsters_in_text("[COMBAT: gobelin]")
        ctx = build_monsters_context(monsters)
        assert "HP:" in ctx
        assert "CA:" in ctx
        assert "ATK:" in ctx

    def test_monster_name_uppercased(self):
        monsters = detect_monsters_in_text("[COMBAT: gobelin]")
        ctx = build_monsters_context(monsters)
        # FR name of goblin should appear uppercased
        assert any(line.isupper() or line[:4].isupper() for line in ctx.splitlines())
