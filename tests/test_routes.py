"""
Integration tests for all HTTP routes.

OLLAMA_MOCK=1 and DEBUG=1 must be set before importing main so that:
  - call_ollama returns a fixed reply (no live Ollama needed)
  - GET /debug/session is enabled

These env vars are set at module level before the import.
"""
import json
import os

os.environ["OLLAMA_MOCK"] = "1"
os.environ["DEBUG"] = "1"

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def session(client):
    """Returns a fresh-session wrapper per test.

    POST /reset deletes any stale session; the 303 → GET / chain creates a new
    one and updates the module-scoped client's cookie jar, so every test starts
    with an empty Session without per-request cookie hacks.
    """
    client.post("/reset")
    assert client.cookies.get("session_id"), "session_id cookie not set after reset"

    class _S:
        def __init__(self, c):
            self._c = c

        def get(self, url, **kw):
            return self._c.get(url, **kw)

        def post(self, url, **kw):
            return self._c.post(url, **kw)

        @property
        def debug(self):
            return self.get("/debug/session").json()

    return _S(client)


# ── Health / meta ──────────────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ollama_status"] == "OK"
    assert data["monsters_loaded"] > 0

def test_home_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "D&D" in r.text

def test_voices(client):
    r = client.get("/voices")
    assert r.status_code == 200
    assert len(r.json()["voices"]) > 0


# ── POST /party ────────────────────────────────────────────────────────────────

def test_load_party_array(session, party_two):
    r = session.post("/party", json=party_two)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["party"] == ["Thorin", "Aria"]
    assert data["count"] == 2

def test_load_party_empty_returns_400(session):
    r = session.post("/party", json=[])
    assert r.status_code == 400

def test_load_party_normalises_lowercase(session, party_aelar):
    r = session.post("/party", json=party_aelar)
    assert r.status_code == 200
    assert r.json()["party"] == ["Aelar"]
    dbg = session.debug
    assert dbg["party_names"] == ["Aelar"]


# ── GET /debug/session ─────────────────────────────────────────────────────────

def test_debug_session_requires_debug_flag(client):
    # Temporarily unset DEBUG, test returns 403, then restore
    del os.environ["DEBUG"]
    r = client.get("/debug/session")
    assert r.status_code == 403
    os.environ["DEBUG"] = "1"

def test_debug_session_shows_state(session, party_two):
    session.post("/party", json=party_two)
    dbg = session.debug
    assert dbg["party_names"] == ["Thorin", "Aria"]
    assert dbg["active_character"] == 0


# ── POST /send ─────────────────────────────────────────────────────────────────

def test_send_adds_two_messages(session, party_two):
    session.post("/party", json=party_two)
    r = session.post("/send", data={"user_input": "Bonjour"})
    assert r.status_code in (200, 303)
    assert session.debug["conversation_count"] == 2

def test_active_character_advances_after_send(session, party_two):
    session.post("/party", json=party_two)
    assert session.debug["active_character"] == 0
    session.post("/send", data={"user_input": "Tour 1"})
    assert session.debug["active_character"] == 1

def test_ollama_error_does_not_advance_turn(session, party_two):
    session.post("/party", json=party_two)
    os.environ["OLLAMA_MOCK_REPLY"] = "[ERREUR: test simulé]"
    before = session.debug["active_character"]
    session.post("/send", data={"user_input": "Tour erreur"})
    after = session.debug["active_character"]
    assert after == before
    del os.environ["OLLAMA_MOCK_REPLY"]


# ── POST /party/active ─────────────────────────────────────────────────────────

def test_set_active_character(session, party_two):
    session.post("/party", json=party_two)
    r = session.post("/party/active", json={"index": 1})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["name"] == "Aria"


# ── POST /party/hp ─────────────────────────────────────────────────────────────

def test_update_hp(session, party_two):
    session.post("/party", json=party_two)
    r = session.post("/party/hp", json={"index": 0, "hp": 20})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── POST /spells/use ───────────────────────────────────────────────────────────

def test_spell_slot_use(session, party_two):
    session.post("/party", json=party_two)
    r = session.post("/spells/use", json={"char_name": "Aria", "slot_level": 1, "delta": 1})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["used"] == 1
    assert data["available"] == data["max"] - 1

def test_short_rest_resets_warlock(session, party_warlock):
    session.post("/party", json=party_warlock)
    # Use a warlock slot
    session.post("/spells/use", json={"char_name": "Zara", "slot_level": 3, "delta": 1})
    # Short rest
    session.post("/send", data={"user_input": "!rest"})
    r = session.post("/spells/use", json={"char_name": "Zara", "slot_level": 3, "delta": 0})
    # After rest, used should be back to 0
    dbg = session.debug
    assert dbg["spell_slots_used"].get("Zara", [0]*9)[2] == 0


# ── GET /tts (security) ────────────────────────────────────────────────────────

def test_tts_path_traversal_blocked(client):
    r = client.get("/tts?text=test&voice=../../etc/passwd")
    assert r.status_code == 400

def test_tts_wrong_extension_blocked(client):
    r = client.get("/tts?text=test&voice=fr_FR-gilles-low.txt")
    assert r.status_code == 400


# ── POST /load-session ─────────────────────────────────────────────────────────

def test_load_session_full(session, session_save):
    r = session.post("/load-session", json=session_save)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["party"] == ["Aelar"]
    assert data["context_msgs"] == 2

def test_load_session_injects_context_messages(session, session_save):
    session.post("/load-session", json=session_save)
    dbg = session.debug
    assert dbg["conversation_count"] == 2

def test_load_session_missing_party_returns_400(session):
    r = session.post("/load-session", json={"world_state": {"city": "Valdris"}})
    assert r.status_code == 400

def test_load_session_no_optional_fields(session):
    minimal = {"party": [{"name": "X", "class": "Guerrier", "level": 1, "hp": 10, "ac": 10}]}
    r = session.post("/load-session", json=minimal)
    assert r.status_code == 200
    assert r.json()["context_msgs"] == 0


# ── POST /reset ────────────────────────────────────────────────────────────────

def test_reset_clears_session(session, party_two):
    session.post("/party", json=party_two)
    session.post("/send", data={"user_input": "Hello"})
    session.post("/reset")
    # POST /reset issues a 303 → GET / which creates a brand-new empty session.
    # The observable effect: conversation history is gone.
    assert session.debug["conversation_count"] == 0
