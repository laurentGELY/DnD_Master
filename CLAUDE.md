# CLAUDE.md — D&D 5e AI Dungeon Master · v1.0

> Règles permanentes d'exécution du dépôt pour Claude Code.
> Ne modifier que si une contrainte est récurrente sur plusieurs sessions.
> Référence technique (architecture, config, patterns) en bas de ce fichier.
> Standards de log, failure modes, perf traces, DoD → voir **`STANDARDS.md`**.

---

## Règles absolues

**Ne jamais :**
- écrire du code avant l'aval explicite de l'utilisateur
- conclure sans preuves observables (logs, `perf_traces.jsonl`, `/health`)
- modifier `Config.yml` sans noter la valeur précédente
- lancer un refactor hors périmètre sans validation explicite
- modifier le system prompt sans tester l'impact sur une session réelle

**Si l'aval n'est pas donné → s'arrêter après l'analyse et attendre.**

---

## Rôle

Claude est l'équipe technique complète de l'app DM.
L'utilisateur est propriétaire du produit et maître du jeu côté design.
Périmètre Claude : architecture, dev, tuning (Config.yml + system prompt), debug, perf, doc, git.

**Sources de vérité :** dépôt git (code) · `Config.yml` (paramètres) · `perf_traces.jsonl` (perf observée).
Restaurer depuis git avant tout patch.

**Limites :** impossible d'interagir directement avec l'interface Ollama ou le navigateur.
Pour toute action manuelle requise : fournir la commande exacte prête à copier.

---

## Démarrage de session

```bash
# 1. Sync et état dépôt
git pull origin main && git status && git log --oneline -5

# 2. État des dépendances (si app en cours d'exécution)
curl -s http://localhost:8000/health | jq

# 3. Dernières traces de perf (si fichier présent)
tail -3 perf_traces.jsonl 2>/dev/null | jq '{ts,wall_s,response_t,gen_tps,capped}'
```

Signaler toute anomalie avant de proposer un patch.

**Classifier le travail :**

| Type | Déclencheur | Flux |
|------|-------------|------|
| **Feature** | Nouvelle mécanique de jeu, nouvel endpoint | Analyse → Aval → Code → Test A → Wrap-up |
| **Fix** | Régression, comportement inattendu | Preuve (log/trace) → Fix ciblé → Test A → Wrap-up |
| **Perf** | Latence, TTFT, tokens/s | Mesure avant (`perf_traces`) → Patch → Mesure après → Wrap-up |
| **Tuning** | `Config.yml`, seuils de session | Valeur avant → Patch → Session test → Valeur après → Wrap-up |
| **Prompt** | `prompts/system_prompt.txt` uniquement | Session test avant + après — zéro code Python |
| **Doc** | `CLAUDE.md`, `STANDARDS.md`, README | Zéro code Python |

---

## Analyse (obligatoire sauf Fix évident < 5 lignes)

Toujours partir du code réel (`main.py`), pas d'une hypothèse mémoire.

```
## Compréhension
- Objectif :
- Comportement actuel → cible :
- Périmètre inclus / exclu :

## Fichiers touchés
- main.py — fonctions concernées :
- Config.yml — clés concernées (valeur avant) :
- prompts/system_prompt.txt :
- templates/ ou static/ :

## Impact chemin de requête
- Touche /send_stream : oui/non → si oui, test B obligatoire
- Touche Config.yml : valeur avant / valeur cible
- Touche system_prompt.txt : session test qualitatif requis

## Plan d'exécution
1.
2.

## Plan de test
- A — Ciblé : <comportement à observer dans le navigateur>
- B — Perf : grep [PERF] app.log + jq perf_traces.jsonl [si chemin requête touché]
- C — Session complète : groupe + combat + TTS + sorts [si risque élevé]

## Demande d'aval
Résumé 3 lignes · fichiers à modifier · tests prévus
→ J'attends ton aval explicite avant de coder.
```

---

## Code

Une fois l'aval obtenu :
- modifications minimales et chirurgicales — pas de refactor hors périmètre
- `Config.yml` = source de vérité, aucun paramètre hardcodé dans `main.py`
- réutiliser les patterns existants (`[PERF]`/`[CONTEXT]` dans les logs, `_write_trace`, etc.)
- valider incrémentalement : tester chaque composant modifié avant de passer au suivant
- si un test intermédiaire échoue → diagnostiquer avant de continuer

**Commentaires — documenter le POURQUOI, jamais le QUOI :**
- justifier les valeurs de seuil avec leur contexte (`# 400t ≈ 21s à 19 t/s — mesure mai 2026`)
- documenter les workarounds et comportements contre-intuitifs

**Suppression de code :** après suppression d'une fonction, vérifier les usages orphelins :
```bash
grep -n "nom_fonction" main.py
```

Si plusieurs approches : expliciter solution retenue · alternative écartée · dette créée.

**Standards observabilité :** voir `STANDARDS.md §Observabilité` — préfixes obligatoires, failure modes.

---

## Test

| Niveau | Quand | Forme | Critère OK |
|--------|-------|-------|------------|
| **A — Ciblé** | Toujours | Tester le comportement modifié dans le navigateur | Comportement attendu, aucun `[ERREUR:]` dans les logs |
| **B — Perf** | Si `/send_stream` ou Ollama touché | `grep "[PERF]" app.log` + `jq` sur `perf_traces.jsonl` | `wall_s` ≤ baseline, pas de régression `gen_tps` |
| **C — Session** | Risque élevé (refactor, context, compaction) | Session complète : chargement groupe, dialogue, combat, TTS, sorts | Sorts/tour/monstres corrects, pas d'erreur Ollama |

Si résultat ambigu → documenter l'observation et demander aval avant de décider.

---

## Debug

```bash
# 1. Identifier la zone
grep "\[PERF\]\|\[CONTEXT\]\|ERROR\|WARNING" app.log

# 2. Analyser les traces structurées
jq '{ts,wall_s,ttft_s,load_s,prefill_s,gen_s,capped}' perf_traces.jsonl

# 3. Vérifier les dépendances
curl -s http://localhost:8000/health | jq
```

**Règle : attendre l'output de chaque commande avant de conclure.**

Référence complète des failure modes et commandes → `STANDARDS.md §Failure modes` et `§Protocole diagnostic`.

---

## Wrap-up

Avant tout commit, vérifier la DoD (`STANDARDS.md §Definition of Done`).

```bash
git diff --stat          # fichiers attendus uniquement, aucun hors périmètre
git log --oneline -3     # format de commit cohérent
```

Format de commit : `type(scope): résumé court` — types définis dans `STANDARDS.md §Commit`.
Si l'architecture a changé → mettre à jour `STANDARDS.md` dans le même commit.

---

## Priorités d'architecture

Justesse → robustesse → maintenabilité → observabilité → performance.

Si deux priorités s'affrontent → expliciter le compromis et demander aval avant de trancher.
Éviter : complexité prématurée · paramètres hardcodés · logique implicite · refactor hors périmètre.

---

## Référence technique

### Lancement de l'app

```bash
# The venv lives one level above the project root (shared venv pattern)
source ../.venv/bin/activate
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Access at `http://localhost:8000`. Diagnostics at `http://localhost:8000/health`.

**External dependencies that must be running:**
- **Ollama** (`ollama serve`) with the desired model loaded. Three Modelfiles are provided:
  - `Modelfile_dnd-dm-gemma4:latest.txt` — Gemma4 26B, default (`num_ctx 32000`)
  - `Modelfile_Magistral:latest.txt` — Magistral 24B, combat-heavy sessions (`num_ctx 32000`)
  - `Modelfile_dnd-dm-8b:latest.txt` — Llama 3.1 8B, fast fallback (`num_ctx 16000`)
- Active model is set in `Config.yml` under `ollama.model` (two alternatives are commented out)
- Override for a single run: `OLLAMA_MODEL=other-model uvicorn main:app ...`

**Verify Piper TTS manually:**
```bash
LD_LIBRARY_PATH=bin/piper_amd64 \
ESPEAK_DATA_PATH=bin/piper_amd64/espeak-ng-data \
bin/piper --model voices/fr_FR-gilles-low.onnx --output_file /tmp/test.wav <<< "Test"
```

### Architecture

Single-file FastAPI backend (`main.py`) — no database, no auth, no package structure.

**In-RAM state** (lost on restart, session-keyed by UUID cookie):
All session state is held in `SESSIONS: dict[str, Session]` where each `Session` dataclass contains:
- `conversations` — chat history sent to Ollama
- `party` — character sheets parsed from player-submitted JSON
- `active_character` — index of whose turn it is (round-robin)
- `spell_slots_used` — tracking per caster per session
- `last_active` — monotonic timestamp used for TTL expiry

**Request flow:** POST /send_stream → SSE tokens → browser (primary path).
POST /send → process → `303 Redirect` → GET / (PRG fallback, no streaming).

**System prompt** (`prompts/system_prompt.txt`) is cached by mtime — edit it and the next Ollama call picks up the change automatically (no restart needed).

**Data files loaded at startup** (`data/monsters.json`, `data/spells.json`):
- `MONSTERS_DB` — keyed by English name, each entry has `fr: [...]` aliases
- `MONSTER_FR_INDEX` — inverted FR-name → key index for `detect_monsters_in_text()`
- `SPELLS_DB` — spell slot tables by caster type and level

### Key design patterns

**Monster injection:** When the DM response contains a `[COMBAT: gobelin, loup géant]` tag, `detect_monsters_in_text()` extracts it, looks up official SRD stats, and injects them as a system message on the *next* Ollama call via `build_monsters_context()`. The tag is stripped from the text displayed to the player.

**Message construction** (`build_messages()`): system messages are always prepended in this order — DM system prompt, party state + active character, spell slots, combat monster stats — followed by the sliding history window (`MAX_HISTORY_TURNS` turns × 2 messages, configured in `Config.yml`).

**Markdown stripping** (`strip_markdown()`): applied to every Ollama reply before storage and display, so both the UI and Piper TTS receive clean text.

**TTS voice security:** `GET /tts?text=&voice=` validates that the resolved `voice` path stays within `voices/` and has `.onnx` extension before invoking the bundled `bin/piper` binary.

**Voice discovery:** any `.onnx` file dropped into `voices/` is automatically listed by `GET /voices` and appears in the UI — no restart needed.

### Configuration

All tuneable parameters live in **`Config.yml`** (YAML, loaded at startup). Edit the file and restart — no code change needed.

| `Config.yml` key | Default | Effect |
|---|---|---|
| `ollama.url` | `http://localhost:11434` | Ollama endpoint |
| `ollama.model` | `dnd-dm-gemma4` | Model name (see Modelfiles) |
| `ollama.keep_alive` | `30m` | Durée de maintien du modèle en VRAM |
| `ollama.max_tokens` | `400` | Plafond tokens par réponse DM (`num_predict`) |
| `tts.default_voice` | `fr_FR-gilles-low.onnx` | Piper voice |
| `session.max_history_turns` | `50` | Sliding history window |
| `session.max_user_input_len` | `2000` | Max chars per player message |
| `session.ttl_seconds` | `10800` | Session expiry (3 h) |
| `context_compaction.threshold` | `80` | Compact when history exceeds N messages |
| `context_compaction.keep_recent` | `40` | Messages kept verbatim after compaction |

`OLLAMA_URL` and `OLLAMA_MODEL` env-vars override the YAML values when set.

### Spell slot system

`init_spell_slots_for_party()` detects caster types (`full`, `half`, `warlock`, `third`) from `spells.json` class lists and pre-populates `_slots_max` on each character dict. The frontend calls `POST /spells/use` (delta ±1) to track usage; `build_slots_context()` injects available slots into Ollama context so the DM can enforce limits.

Warlocks recover slots on short rest (`!rest`); all casters recover on long rest (`!longrest`).

### Performance traces

`perf_traces.jsonl` (project root) — one JSON line per exchange, written after every `/send_stream` response. Fields: `ts`, `session`, `model`, `context_t`, `ttft_s`, `load_s`, `prefill_s/tps`, `gen_s/tps`, `response_t`, `total_s`, `wall_s`, `capped`.

Quick diagnostic:
```bash
grep "\[PERF\]" app.log
jq -r '[.ts,.wall_s,.response_t,.gen_tps,.capped] | @tsv' perf_traces.jsonl
```

Full grep commands and analysis queries → `STANDARDS.md §Fichier de traces de performance`.
