# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# The venv lives one level above the project root (shared venv pattern)
source ../.venv/bin/activate
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Access at `http://localhost:8000`. Diagnostics at `http://localhost:8000/health`.

**External dependencies that must be running:**
- **Ollama** (`ollama serve`) with the `dnd-dm-magistral` model loaded (Magistral 24B, `num_ctx 40000`, defined in `Modelfile_Magistral:latest.txt`)
- Override model: `OLLAMA_MODEL=other-model uvicorn main:app ...`

**Verify Piper TTS manually:**
```bash
LD_LIBRARY_PATH=bin/piper_amd64 \
ESPEAK_DATA_PATH=bin/piper_amd64/espeak-ng-data \
bin/piper --model voices/fr_FR-gilles-low.onnx --output_file /tmp/test.wav <<< "Test"
```

## Architecture

Single-file FastAPI backend (`main.py`) — no database, no auth, no package structure.

**In-RAM state** (lost on restart, session-keyed by UUID cookie):
- `CONVERSATIONS` — chat history sent to Ollama
- `PARTY_STATES` — character sheets parsed from player-submitted JSON
- `ACTIVE_CHARACTER` — index of whose turn it is (round-robin)
- `SPELL_SLOTS_USED` — tracking per caster per session

**Request flow:** POST /send → process → `303 Redirect` → GET / (PRG pattern to prevent F5 resubmission)

**System prompt** (`prompts/system_prompt.txt`) is re-read on every Ollama call — edit it and changes take effect immediately without restart.

**Data files loaded at startup** (`data/monsters.json`, `data/spells.json`):
- `MONSTERS_DB` — keyed by English name, each entry has `fr: [...]` aliases
- `MONSTER_FR_INDEX` — inverted FR-name → key index for `detect_monsters_in_text()`
- `SPELLS_DB` — spell slot tables by caster type and level

## Key design patterns

**Monster injection:** When the DM response contains a `[COMBAT: gobelin, loup géant]` tag, `detect_monsters_in_text()` extracts it, looks up official SRD stats, and injects them as a system message on the *next* Ollama call via `build_monsters_context()`. The tag is stripped from the text displayed to the player.

**Message construction** (`build_messages()`): system messages are always prepended in this order — DM system prompt, party state + active character, spell slots, combat monster stats — followed by the sliding history window (`MAX_HISTORY_TURNS = 15` turns × 2 messages).

**Markdown stripping** (`strip_markdown()`): applied to every Ollama reply before storage and display, so both the UI and Piper TTS receive clean text.

**TTS voice security:** `GET /tts?text=&voice=` validates that the resolved `voice` path stays within `voices/` and has `.onnx` extension before invoking the bundled `bin/piper` binary.

**Voice discovery:** any `.onnx` file dropped into `voices/` is automatically listed by `GET /voices` and appears in the UI — no restart needed.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | `dnd-dm-8b` | Model name |

`MAX_HISTORY_TURNS` and `MAX_USER_INPUT_LEN` are constants in `main.py`.

## Spell slot system

`init_spell_slots_for_party()` detects caster types (`full`, `half`, `warlock`, `third`) from `spells.json` class lists and pre-populates `_slots_max` on each character dict. The frontend calls `POST /spells/use` (delta ±1) to track usage; `build_slots_context()` injects available slots into Ollama context so the DM can enforce limits.

Warlocks recover slots on short rest (`!rest`); all casters recover on long rest (`!longrest`).
