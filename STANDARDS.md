# STANDARDS.md — D&D 5e AI Dungeon Master · v1.0

> Référence technique permanente du dépôt.
> Complémentaire à `CLAUDE.md` — ne contient pas le détail d'architecture.
> Mettre à jour dans le même commit que tout changement architectural.

---

## Observabilité — Standards de log

### Préfixes obligatoires

| Préfixe | Usage |
|---------|-------|
| `[PERF]` | Toute mesure de durée ou débit (Ollama, TTS, request) |
| `[CONTEXT]` | Taille et composition du contexte envoyé à Ollama |
| `[SESSION]` | État de session au début d'une requête |
| `[MOCK]` | Quand `OLLAMA_MOCK=1` est actif |

Sans préfixe, le log n'est pas filtrable par `grep "\[PERF\]"`.

### Format de log

```
YYYY-MM-DD HH:MM:SS LEVEL message [PREFIXE] détail — session XXXXXXXX
```

Exemple :
```
2026-05-19 21:17:40,687 - INFO - [PERF] Ollama stream: total=140.6s | chargement=0.3s | analyse=2.9s (1572 t/s, 4026t) | réponse=51.0s (19 t/s, 960t)
```

### Failure modes officiels

| Code | Condition | Log attendu |
|------|-----------|-------------|
| `CONNECT_ERROR` | Ollama non démarré (`ollama serve` absent) | `Ollama non accessible` |
| `TIMEOUT_OLLAMA` | Génération > 120s (read timeout client httpx) | `Ollama timeout` |
| `HTTP_ERROR` | Réponse HTTP non-2xx d'Ollama | `Ollama HTTP NNN: ...` |
| `PREWARM_FAILED` | Préchauffage échoué (modèle absent ou Ollama down) | `Pré-chargement Ollama échoué après Xs` |
| `MODEL_NOT_IN_VRAM` | `load_duration > 5s` sur une requête (modèle déchargé entre deux tours) | `[PERF] Modèle non en VRAM` |
| `TTS_TIMEOUT` | Subprocess Piper bloqué > 30s | `TTS timeout` |
| `TTS_FAILED` | Piper returncode != 0 | `Piper a échoué` |
| `SESSION_MISSING` | Cookie valide mais session expirée (TTL 3h) | aucun — session recréée silencieusement |
| `INPUT_TOO_LONG` | Message joueur > `max_user_input_len` | `Input trop long, tronqué` |

---

## Carte du cycle de requête

*(validée sur main.py — mettre à jour à chaque changement de flux)*

```
POST /send_stream  (chemin normal — SSE streaming)
  │
  ├─ 1. Validation input (troncature si > max_user_input_len)
  ├─ 2. Détection JSON de groupe → init_spell_slots_for_party()
  ├─ 3. Détection commande repos (!rest / !longrest) → apply_rest()
  ├─ 4. Détection monstres dans les 4 derniers msgs DM → combat_monsters[]
  ├─ 5. build_messages() — construction contexte Ollama
  │      ordre : [system prompt] [historique] [état groupe] [sorts] [monstres] [user]
  ├─ 6. _ollama_client.stream() → SSE tokens → navigateur
  ├─ 7. Sur done : strip_markdown(), detect_monsters_in_text(), advance_active_character()
  ├─ 8. Écriture perf_traces.jsonl
  └─ 9. asyncio.create_task(maybe_compact_history())  ← non-bloquant

POST /send  (chemin de fallback — pleine page, PRG pattern)
  └─ Même logique que /send_stream sans SSE ; redirige vers GET /
```

**Invariants à ne pas casser :**
- Le tour n'avance (`advance_active_character`) que si Ollama répond sans erreur.
- L'historique n'est jamais pollué par une réponse `[ERREUR: ...]`.
- `strip_markdown()` est appelé avant tout stockage et tout TTS.
- La compaction ne bloque pas la réponse (tâche de fond).

---

## Fichier de traces de performance

### `perf_traces.jsonl`

Une ligne JSON par échange, dans le répertoire racine du projet.
Écrit par `_write_trace()` à la fin de chaque requête `/send_stream` réussie.

#### Schéma

| Champ | Type | Description |
|-------|------|-------------|
| `ts` | ISO 8601 UTC | Horodatage fin d'échange |
| `session` | str (8 chars) | Préfixe UUID de session |
| `model` | str | Nom du modèle Ollama actif |
| `context_t` | int | Tokens de contexte (prompt_eval_count) |
| `ttft_s` | float\|null | Time To First Token (s) depuis ouverture stream |
| `load_s` | float | Durée chargement modèle en VRAM (0 si déjà chargé) |
| `prefill_s` | float | Durée analyse du contexte (prompt_eval_duration) |
| `prefill_tps` | float | Débit prefill (tokens/s) |
| `gen_s` | float | Durée génération réponse (eval_duration) |
| `gen_tps` | float | Débit génération (tokens/s) |
| `response_t` | int | Tokens générés (eval_count) |
| `total_s` | float | Durée totale Ollama (total_duration) |
| `wall_s` | float | Durée mur depuis réception requête |
| `capped` | bool | `true` si response_t >= ollama.max_tokens |

#### Commandes d'analyse

```bash
# Vue tabulaire : ts | session | context_t | ttft_s | gen_s | gen_tps | response_t | wall_s | capped
jq -r '[.ts,.session,.context_t,.ttft_s,.gen_s,.gen_tps,.response_t,.wall_s,.capped] | @tsv' perf_traces.jsonl

# Réponses tronquées par num_predict (DM coupé en plein milieu)
jq 'select(.capped == true)' perf_traces.jsonl

# Débit moyen de génération
jq -s '[.[].gen_tps] | add/length' perf_traces.jsonl

# Échanges les plus lents (wall_s > 30s)
jq 'select(.wall_s > 30)' perf_traces.jsonl

# TTFT moyen (hors nulls)
jq -s '[.[].ttft_s | select(. != null)] | add/length' perf_traces.jsonl
```

#### Baselines observées (Gemma4 26B, mai 2026)

| Métrique | Valeur de référence |
|----------|---------------------|
| `gen_tps` | 19–21 t/s |
| `load_s` normal | < 0.5s (modèle en VRAM) |
| `prefill_tps` | 1500–9000 t/s selon taille contexte |
| `wall_s` avec max_tokens=400 | < 25s attendu |

---

## Règles d'archivage diagnostic

### `doc/DIAGNOSTIC_CMDS.md`

Toute commande `grep` ayant localisé ou résolu un problème → archivée avant le wrap-up.

Format :
```
## Symptôme : <description>
Date : JJ/MM/AAAA
Commande : <commande exacte>
Résultat observé : <ce qu'on a vu>
Conclusion : <ce que ça a confirmé ou infirmé>
```

---

## Definition of Done

### Livrable

- Le comportement en jeu est vérifié manuellement via l'interface (`http://localhost:8000`)
- Aucun fichier hors portée dans le diff (`git diff --stat`)
- `CLAUDE.md`, `STANDARDS.md`, `Config.yml` mis à jour si impactés
- `perf_traces.jsonl` consulté si le changement touche le chemin de requête

### Commit

Format : `type(scope): résumé court`

| Type | Quand |
|------|-------|
| `feat` | Nouvelle fonctionnalité |
| `fix` | Correction de bug |
| `perf` | Optimisation latence/débit |
| `config` | Changement Config.yml uniquement |
| `prompt` | Modification system prompt |
| `doc` | CLAUDE.md / STANDARDS.md / README uniquement |
| `refactor` | Restructuration sans changement de comportement |

---

## Types de travaux

| Type | Description | Output attendu |
|------|-------------|----------------|
| **Feature** | Nouvelle fonctionnalité (endpoint, mécanique de jeu) | Code + vérif manuelle |
| **Fix** | Correction bug ou régression | Code corrigé + test mur |
| **Perf** | Optimisation latence (num_predict, contexte, streaming) | Mesure avant/après dans perf_traces.jsonl |
| **Tuning** | Ajustement Config.yml, system prompt, seuils | Mesure avant/après documentée |
| **Doc** | CLAUDE.md, STANDARDS.md, README | Zéro code Python |
| **Spike** | Investigation bornée | Décision documentée uniquement — pas de code partiel |

---

## Contraintes stack

```
Installation     : venv partagé dans ../.venv (un niveau au-dessus du projet)
Config.yml       : source de vérité — aucun paramètre hardcodé dans main.py
                   OLLAMA_URL et OLLAMA_MODEL en env-var overrident Config.yml
perf_traces.jsonl: jamais supprimé manuellement — archiver avant de vider
git              : source de vérité du code — restaurer depuis git avant tout patch
Modelfiles       : tout nouveau modèle = nouveau Modelfile dans le répertoire racine
```
