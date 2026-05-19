#!/usr/bin/env bash
# Manual smoke-test script for the D&D DM app.
# Run with: bash tests/curl_examples.sh
# Requires: app running on localhost:8000 and Ollama running.
#
# NOTE: curl -X POST combined with -L will re-POST on the 303 redirect.
# Never use -X POST -L together. Use --data or --data-urlencode to trigger
# POST without overriding the method on redirect.

BASE=http://127.0.0.1:8000
JAR=$(mktemp)
FIXTURES="$(dirname "$0")/fixtures"

sep() { echo; echo "──────── $* ────────"; }

# ── 1. Health ─────────────────────────────────────────────────────────────────
sep "GET /health"
curl -s "$BASE/health" | python3 -m json.tool

# ── 2. Voices ─────────────────────────────────────────────────────────────────
sep "GET /voices"
curl -s "$BASE/voices" | python3 -m json.tool

# ── 3. Create session ─────────────────────────────────────────────────────────
sep "GET / (creates session cookie)"
curl -s -c "$JAR" -b "$JAR" "$BASE/" -o /dev/null -w "Status: %{http_code}\n"

# ── 4. Load party ─────────────────────────────────────────────────────────────
sep "POST /send — load party from JSON (waits for Ollama ~60s)"
PARTY=$(cat "$FIXTURES/party_two_chars.json")
curl -s -c "$JAR" -b "$JAR" "$BASE/send" \
  --data-urlencode "user_input=$PARTY" \
  -w "Status: %{http_code}\n" -o /dev/null

echo "Waiting 90s for Ollama response..."
sleep 90

# ── 5. Check active character ─────────────────────────────────────────────────
sep "GET /party/active"
curl -s -c "$JAR" -b "$JAR" "$BASE/party/active" | python3 -m json.tool

# ── 6. Manually set active character ──────────────────────────────────────────
sep "POST /party/active — set index=0"
curl -s -c "$JAR" -b "$JAR" \
  -X POST "$BASE/party/active" \
  -H "Content-Type: application/json" \
  -d '{"index": 0}' | python3 -m json.tool

# ── 7. Update HP ──────────────────────────────────────────────────────────────
sep "POST /party/hp — Thorin takes 5 damage (idx 0, hp 30)"
curl -s -c "$JAR" -b "$JAR" \
  -X POST "$BASE/party/hp" \
  -H "Content-Type: application/json" \
  -d '{"index": 0, "hp": 30}' | python3 -m json.tool

# ── 8. Spell slot (requires a caster — use party_with_warlock.json instead) ──
# sep "POST /spells/use — Aria uses a level-1 slot"
# curl -s -c "$JAR" -b "$JAR" \
#   -X POST "$BASE/spells/use" \
#   -H "Content-Type: application/json" \
#   -d '{"char_name": "Aria", "slot_level": 1, "delta": 1}' | python3 -m json.tool

# ── 9. TTS ────────────────────────────────────────────────────────────────────
sep "GET /tts — synthesise a short phrase"
curl -s "$BASE/tts?text=Bienvenue+dans+la+taverne&voice=fr_FR-gilles-low.onnx" \
  -o /tmp/dnd_test.wav -w "Status: %{http_code}, Size: %{size_download} bytes\n"
file /tmp/dnd_test.wav 2>/dev/null

# ── 10. TTS security ──────────────────────────────────────────────────────────
sep "GET /tts — path traversal (expect 400)"
curl -s "$BASE/tts?text=test&voice=../../etc/passwd" -w "Status: %{http_code}\n" -o /dev/null

sep "GET /tts — wrong extension (expect 400)"
curl -s "$BASE/tts?text=test&voice=fr_FR-gilles-low.txt" -w "Status: %{http_code}\n" -o /dev/null

# ── 11. Reset ─────────────────────────────────────────────────────────────────
sep "POST /reset"
curl -s -c "$JAR" -b "$JAR" "$BASE/reset" --data "" \
  -w "Status: %{http_code}\n" -o /dev/null

# ── 12. Verify session is empty after reset ───────────────────────────────────
sep "GET /party/active (should be null after reset)"
curl -s -c "$JAR" -b "$JAR" "$BASE/party/active" | python3 -m json.tool

rm -f "$JAR"
echo
echo "Done."
