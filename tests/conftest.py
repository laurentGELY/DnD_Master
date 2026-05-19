import json
import uuid
import pytest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def party_two():
    return json.loads((FIXTURES / "party_two_chars.json").read_text())


@pytest.fixture
def party_warlock():
    return json.loads((FIXTURES / "party_with_warlock.json").read_text())


@pytest.fixture
def party_single():
    return json.loads((FIXTURES / "party_single_fighter.json").read_text())


@pytest.fixture
def party_aelar():
    return json.loads((FIXTURES / "party_wizard_aelar.json").read_text())


@pytest.fixture
def session_save():
    return json.loads((FIXTURES / "session_save_aelar.json").read_text())


@pytest.fixture
def fresh_session_id():
    """Returns a unique session ID and removes it from SESSIONS after the test."""
    import main
    sid = str(uuid.uuid4())
    yield sid
    main.SESSIONS.pop(sid, None)
