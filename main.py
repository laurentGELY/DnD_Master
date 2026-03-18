#!/usr/bin/env python3
"""
D&D 5e AI Dungeon Master Web App
================================
Version: 1.3.0 (2026-03-13)
Licence: MIT (usage personnel)

NOUVEAUTÉS v1.3.0
-----------------
- Tour par tour strict (combat ET hors combat)
- Suivi du personnage actif en session (ACTIVE_CHARACTER)
- Sélecteur de personnage actif dans l'interface → préfixe auto du message
- Commande !ordre ajoutée au system prompt
- Endpoint GET /party/active → retourne le personnage actif courant
- Endpoint POST /party/active → force manuellement le personnage actif
- Le personnage actif est réinjecté dans le contexte Ollama

LANCEMENT
---------
cd "/home/laurentg/Downloads/Sandbox/DnD/Py/dnd-dm-app"
source ../.venv/bin/activate
uvicorn main:app --reload --host 127.0.0.1 --port 8000
"""

import os
import io
import json
import uuid
import logging
import re
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
CODE_VERSION = "1.5.0"

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "dnd-dm-8b")

BASE_DIR           = Path(__file__).parent
SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
TEMPLATES_DIR      = BASE_DIR / "templates"

PIPER_BIN        = BASE_DIR / "bin" / "piper"
PIPER_MODEL_PATH = BASE_DIR / "voices" / "fr_FR-gilles-low.onnx"
PIPER_LIBS_DIR   = BASE_DIR / "bin" / "piper_amd64"

# Chemins vers les fichiers de données SRD
MONSTERS_PATH = BASE_DIR / "data" / "monsters.json"
SPELLS_PATH   = BASE_DIR / "data" / "spells.json"

MAX_HISTORY_TURNS  = 15
MAX_USER_INPUT_LEN = 2000

PARTY_REQUIRED_KEYS = {"name", "race", "class", "level", "HP", "AC",
                       "classe", "niveau", "hp", "ac", "hit_points"}

# ── Chargement des données SRD au démarrage ───────────────────────────────────
def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        logging.warning(f"{label} introuvable: {path}")
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

MONSTERS_DB: dict = _load_json(MONSTERS_PATH, "Compendium monstres")
SPELLS_DB:   dict = _load_json(SPELLS_PATH,   "Référentiel sorts")

# Index inversé FR→clé pour recherche rapide
# { "gobelin": "goblin", "gobelins": "goblin", ... }
MONSTER_FR_INDEX: dict[str, str] = {}
for key, data in MONSTERS_DB.items():
    for fr_name in data.get("fr", []):
        MONSTER_FR_INDEX[fr_name.lower()] = key

# Spell slots actifs par session : { session_id: { char_name: [used_slot1, ...] } }
# used_slot[i] = nombre d'emplacements de niveau (i+1) utilisés
SPELL_SLOTS_USED: dict[str, dict[str, list[int]]] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# NETTOYAGE MARKDOWN (Amélioration 1)
# ═══════════════════════════════════════════════════════════════════════════════
# Le modèle génère du Markdown par réflexe (**gras**, *italique*, ### titres…).
# Ces caractères sont affichés tels quels dans l'interface et prononcés
# littéralement par le TTS ("astérisque astérisque mot astérisque astérisque").
# On nettoie la réponse une seule fois côté serveur, avant toute sauvegarde,
# ce qui garantit que l'affichage ET le TTS reçoivent du texte brut propre.
# ═══════════════════════════════════════════════════════════════════════════════

def strip_markdown(text: str) -> str:
    """
    Supprime les marqueurs Markdown courants générés par les LLM.
    Opère sur le texte brut — pas de parsing HTML.

    Transformations appliquées dans l'ordre :
      1. Titres   : ### Titre  →  Titre
      2. Gras     : **mot**    →  mot
      3. Italique : *mot*      →  mot   (après le gras pour éviter les conflits)
      4. Italique : _mot_      →  mot
      5. Code     : `mot`      →  mot
      6. Lignes horizontales : --- ou *** seuls sur une ligne → vide
      7. Listes   : - item ou * item → item   (retire seulement le marqueur)
      8. Espaces multiples résiduels → espace simple
    """
    # 1. Titres ATX (# à ######)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # 2. Gras **...**  ou  __...__
    text = re.sub(r'\*{2}(.+?)\*{2}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_{2}(.+?)_{2}',   r'\1', text, flags=re.DOTALL)
    # 3. Italique *...*  (après gras — évite de casser **mot**)
    text = re.sub(r'\*(.+?)\*', r'\1', text, flags=re.DOTALL)
    # 4. Italique _..._
    text = re.sub(r'_(.+?)_', r'\1', text, flags=re.DOTALL)
    # 5. Code inline `...`
    text = re.sub(r'`(.+?)`', r'\1', text)
    # 6. Lignes horizontales (--- ou *** ou ___ seuls)
    text = re.sub(r'^\s*[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # 7. Marqueurs de listes (- item  ou  * item  en début de ligne)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    # 8. Nettoyage résiduel : plus de trois sauts de ligne consécutifs → deux max
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# P2a — DÉTECTION ET INJECTION DES MONSTRES SRD
# ═══════════════════════════════════════════════════════════════════════════════

def detect_monsters_in_text(text: str) -> list[dict]:
    """
    Détecte les monstres du compendium SRD dans un texte (réponse du DM).

    Deux stratégies combinées (tag prioritaire, regex en fallback) :

    1. TAG [COMBAT: nom1, nom2] dans la réponse du DM (strategy B)
       Exemple : [COMBAT: gobelin, loup géant]
       → fiable, explicite, pas d'ambiguïté

    2. Si pas de tag : scan du texte contre l'index FR des noms de monstres
       → utile pour les monstres mentionnés sans tag
       → peut générer des faux positifs sur des noms communs courts

    Returns: liste de dicts stats monstres (peut contenir des doublons intentionnels
             si plusieurs exemplaires du même monstre sont mentionnés).
    """
    found = []

    # Stratégie A : tag [COMBAT: ...]
    tag_match = re.search(r'\[COMBAT:\s*([^\]]+)\]', text, re.IGNORECASE)
    if tag_match:
        names = [n.strip().lower() for n in tag_match.group(1).split(',')]
        for name in names:
            key = MONSTER_FR_INDEX.get(name)
            if not key:
                # Essai avec le nom anglais directement
                key = name if name in MONSTERS_DB else None
            if key and key in MONSTERS_DB:
                found.append({"key": key, **MONSTERS_DB[key]})
        if found:
            logger.info(f"Monstres via tag COMBAT: {[m['key'] for m in found]}")
            return found

    # Stratégie B : regex sur les noms FR du compendium
    text_lower = text.lower()
    for fr_name, key in MONSTER_FR_INDEX.items():
        # Ignore les noms très courts (< 4 chars) — trop de faux positifs
        if len(fr_name) < 4:
            continue
        if fr_name in text_lower and key in MONSTERS_DB:
            # Vérifie qu'on ne l'a pas déjà ajouté
            if not any(m["key"] == key for m in found):
                found.append({"key": key, **MONSTERS_DB[key]})

    if found:
        logger.info(f"Monstres via regex: {[m['key'] for m in found]}")
    return found


def build_monsters_context(monsters: list[dict]) -> str:
    """
    Formate le bloc de stats monstres à injecter dans le contexte Ollama.
    Injecté uniquement quand un combat est détecté — évite de polluer le contexte.
    """
    if not monsters:
        return ""
    lines = ["STATS OFFICIELLES DES MONSTRES EN COMBAT (utiliser ces valeurs exactes):"]
    for m in monsters:
        lines.append(
            f"  {m.get('fr', [m['key']])[0].upper()} — "
            f"HP:{m['HP']} CA:{m['AC']} ATK:{m['ATK']} DMG:{m['DMG']} "
            f"FOR:{m['STR']} DEX:{m['DEX']} CON:{m['CON']} "
            f"INT:{m['INT']} SAG:{m['WIS']} CHA:{m['CHA']} "
            f"CR:{m['CR']} Type:{m['type']}"
        )
    lines.append("INSTRUCTION: utilise ces HP et CA dans toutes les résolutions d'attaque.")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# P2b — SYSTÈME DE REPOS (Short Rest / Long Rest)
# ═══════════════════════════════════════════════════════════════════════════════

def detect_rest_command(text: str) -> Optional[str]:
    """
    Détecte les commandes de repos dans le message du joueur.
    Returns: "short", "long", ou None.
    """
    t = text.strip().lower()
    if re.search(r'!rest|!shortrest|repos court|short rest', t):
        return "short"
    if re.search(r'!longrest|!long|repos long|long rest', t):
        return "long"
    return None


def apply_rest(session_id: str, rest_type: str) -> str:
    """
    Applique les effets d'un repos sur le groupe.

    Short rest :
    - Warlock : récupère tous ses emplacements de pacte
    - Autres lanceurs : rien (leurs slots se récupèrent sur long rest)
    - Toutes classes : peuvent dépenser des Hit Dice (géré narrativement par le DM)

    Long rest :
    - Tous les personnages : HP max, tous les emplacements de sorts récupérés
    - Met à jour PARTY_STATES (HP) et SPELL_SLOTS_USED (reset à 0)

    Returns: message de confirmation formaté pour l'affichage.
    """
    party = PARTY_STATES.get(session_id, [])
    slots_used = SPELL_SLOTS_USED.setdefault(session_id, {})
    results = []

    if rest_type == "long":
        # Récupération complète HP
        for char in party:
            for hp_key in ("HP", "hp", "hit_points"):
                if hp_key in char:
                    hp_max = char.get("HP_max", char.get("hp_max", char[hp_key]))
                    char[hp_key] = hp_max
                    break
        # Remise à zéro de tous les emplacements
        for char in party:
            name = char_name(char)
            if name in slots_used:
                slots_used[name] = [0] * 9
        results.append("Repos long terminé. HP max récupérés, tous les emplacements de sorts récupérés.")
        logger.info(f"Long rest appliqué — session {session_id[:8]}")

    elif rest_type == "short":
        # Warlock récupère ses emplacements de pacte sur repos court
        for char in party:
            name  = char_name(char)
            cls   = (char.get("class", char.get("classe", "")) or "").lower()
            is_warlock = any(w in cls for w in ["warlock", "occultiste", "pacte"])
            if is_warlock and name in slots_used:
                slots_used[name] = [0] * 9
                results.append(f"{name} (Occultiste) récupère ses emplacements de pacte.")
        results.append("Repos court terminé. Dépensez des Dés de Vie si nécessaire.")
        logger.info(f"Short rest appliqué — session {session_id[:8]}")

    return "\n".join(results) if results else "Repos effectue."


# ═══════════════════════════════════════════════════════════════════════════════
# P2c — SPELL SLOTS : INITIALISATION ET UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def _get_caster_type(class_name: str) -> Optional[str]:
    """Retourne le type de lanceur de sorts d'une classe, ou None si non-lanceur."""
    cls = (class_name or "").lower().strip()
    full  = SPELLS_DB.get("full_casters",  {}).get("_classes", [])
    half  = SPELLS_DB.get("half_casters",  {}).get("_classes", [])
    wlock = SPELLS_DB.get("warlock",       {}).get("_classes", [])
    third = SPELLS_DB.get("third_casters", {}).get("_classes", [])
    if any(c in cls for c in full):  return "full"
    if any(c in cls for c in half):  return "half"
    if any(c in cls for c in wlock): return "warlock"
    if any(c in cls for c in third): return "third"
    return None


def init_spell_slots_for_party(session_id: str) -> None:
    """
    Initialise les emplacements de sorts pour tous les personnages du groupe.
    Appelé quand un groupe est chargé. Ne réinitialise pas si déjà présent
    (évite d'écraser l'état en cours si le joueur renvoie son JSON).
    """
    party     = PARTY_STATES.get(session_id, [])
    slots_map = SPELL_SLOTS_USED.setdefault(session_id, {})

    for char in party:
        name        = char_name(char)
        cls         = char.get("class", char.get("classe", ""))
        level       = str(char.get("level", char.get("niveau", 1)))
        caster_type = _get_caster_type(cls)

        if not caster_type or name in slots_map:
            continue   # Non-lanceur ou déjà initialisé

        if caster_type == "warlock":
            wlock_data = SPELLS_DB.get("warlock", {}).get("slots_by_level", {})
            lvl_data   = wlock_data.get(level, {"slots": 0, "slot_level": 1})
            # Warlock : un seul niveau d'emplacement, tous identiques
            # On stocke quand même sur 9 niveaux pour uniformité, seul le niveau
            # de pacte a des slots non-nuls
            slots = [0] * 9
            slot_lvl = lvl_data.get("slot_level", 1) - 1
            # slots_map stocke les emplacements DISPONIBLES (max)
            slots_map[name] = [0] * 9  # used = 0 au départ
            char["_slots_max"]   = [0] * 9
            char["_slots_max"][slot_lvl] = lvl_data.get("slots", 0)
            char["_caster_type"] = "warlock"
            char["_slot_level"]  = slot_lvl + 1
        else:
            tbl_key  = f"{caster_type}_casters" if caster_type != "third" else "third_casters"
            tbl      = SPELLS_DB.get(tbl_key, {}).get("slots_by_level", {})
            max_slots = tbl.get(level, [0]*9)
            slots_map[name]      = [0] * 9   # used
            char["_slots_max"]   = max_slots
            char["_caster_type"] = caster_type

        logger.info(f"Sorts initialisés: {name} ({cls} niv.{level}) type={caster_type}")


def get_slots_display(session_id: str) -> list[dict]:
    """
    Retourne l'état des emplacements de sorts pour tous les lanceurs du groupe.
    Format utilisé par le template Jinja2 pour le panneau sorts.
    """
    party     = PARTY_STATES.get(session_id, [])
    slots_map = SPELL_SLOTS_USED.get(session_id, {})
    result    = []

    for char in party:
        name      = char_name(char)
        max_slots = char.get("_slots_max")
        ctype     = char.get("_caster_type")
        if not max_slots or not ctype:
            continue   # Non-lanceur

        used = slots_map.get(name, [0]*9)
        spell_levels = []
        for i, (mx, us) in enumerate(zip(max_slots, used)):
            if mx > 0:
                spell_levels.append({
                    "level":     i + 1,
                    "max":       mx,
                    "used":      us,
                    "available": mx - us,
                })
        if spell_levels:
            result.append({
                "name":        name,
                "caster_type": ctype,
                "levels":      spell_levels,
            })
    return result


def build_slots_context(session_id: str) -> str:
    slots_display = get_slots_display(session_id)
    if not slots_display:
        return ""
    lines = ["EMPLACEMENTS DE SORTS DISPONIBLES (ne pas autoriser l'utilisation d'emplacements epuises):"]
    for caster in slots_display:
        lvl_strs = [
            f"Niv{sl['level']}:{sl['available']}/{sl['max']}"
            for sl in caster["levels"]
        ]
        lines.append(f"  {caster['name']}: {' | '.join(lvl_strs)}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# ÉTAT APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

CONVERSATIONS:    Dict[str, List[Dict[str, str]]] = {}
PARTY_STATES:     Dict[str, List[Dict]]           = {}

# Personnage actif par session — index dans PARTY_STATES[session_id].
# None = aucun groupe chargé ou mode libre.
# Avance automatiquement après chaque message envoyé (round-robin).
ACTIVE_CHARACTER: Dict[str, Optional[int]]        = {}

app = FastAPI(title="D&D 5e AI DM", version=CODE_VERSION)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — UTILITAIRES GÉNÉRAUX
# ═══════════════════════════════════════════════════════════════════════════════

def get_system_prompt() -> str:
    if not SYSTEM_PROMPT_PATH.exists():
        logger.error(f"Fichier prompt manquant: {SYSTEM_PROMPT_PATH}")
        return "[ERREUR: Créez prompts/system_prompt.txt]"
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        prompt = f.read().strip()
    logger.info(f"Prompt chargé: {len(prompt)} caractères")
    return prompt


def get_or_create_session_id(request: Request) -> str:
    session_id = request.cookies.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
        logger.info(f"Nouvelle session: {session_id[:8]}...")
    return session_id


def char_name(char: Dict) -> str:
    """Retourne le nom d'un personnage (supporte clés FR et EN)."""
    return char.get("name", char.get("nom", "?"))


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — GESTION DU GROUPE (P1b)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_party_from_input(user_input: str) -> Optional[List[Dict]]:
    """Détecte et parse un JSON de groupe dans le message utilisateur."""
    json_match = re.search(r'(\{.*\}|\[.*\])', user_input, re.DOTALL)
    if not json_match:
        return None
    try:
        raw = json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return None

    characters = []
    if isinstance(raw, list):
        characters = raw
    elif isinstance(raw, dict):
        for key in ("party", "characters", "groupe", "personnages"):
            if key in raw and isinstance(raw[key], list):
                characters = raw[key]
                break
        if not characters:
            for key in ("character", "personnage"):
                if key in raw and isinstance(raw[key], dict):
                    characters = [raw[key]]
                    break
        if not characters:
            characters = [raw]

    if not characters:
        return None

    for char in characters:
        if isinstance(char, dict) and (PARTY_REQUIRED_KEYS & set(char.keys())):
            logger.info(f"Groupe détecté: {len(characters)} personnage(s)")
            return characters
    return None


def build_party_context(party: List[Dict], active_idx: Optional[int] = None) -> str:
    lines = ["ÉTAT ACTUEL DU GROUPE (maintenir ces valeurs tout au long de la partie):"]
    for i, char in enumerate(party):
        name   = char_name(char)
        race   = char.get("race", "?")
        cls    = char.get("class",  char.get("classe",  "?"))
        level  = char.get("level",  char.get("niveau",  "?"))
        hp     = char.get("HP",     char.get("hp",      char.get("hit_points", "?")))
        hp_max = char.get("HP_max", char.get("hp_max",  hp))
        ac     = char.get("AC",     char.get("ac",      char.get("armor_class", "?")))

        stats = []
        for stat in ("STR", "DEX", "CON", "INT", "WIS", "CHA"):
            val = char.get(stat, char.get(stat.lower()))
            if val is not None:
                mod  = (int(val) - 10) // 2
                sign = "+" if mod >= 0 else ""
                stats.append(f"{stat}:{val}({sign}{mod})")

        marker = " ◄ ACTIF" if i == active_idx else ""
        lines.append(
            f"  {name} — {race} {cls} niv.{level} | "
            f"HP:{hp}/{hp_max} | CA:{ac}"
            + (f" | {' '.join(stats)}" if stats else "")
            + marker
        )

    # Rappel explicite du personnage actif — crucial pour le respect du tour par tour
    if active_idx is not None and 0 <= active_idx < len(party):
        active_name = char_name(party[active_idx])
        lines.append(f"\nPERSONNAGE QUI AGIT MAINTENANT: {active_name}")
        lines.append(
            "INSTRUCTION: Adresse-toi uniquement à ce personnage. "
            "Après avoir résolu son action, annonce le prochain personnage "
            "avec le format: \"C'est au tour de [NOM] — que fait-il/elle ?\""
        )
    return "\n".join(lines)


def advance_active_character(session_id: str) -> None:
    """
    Passe au personnage suivant dans l'ordre round-robin.
    Appelé après chaque message envoyé avec succès.
    """
    party = PARTY_STATES.get(session_id, [])
    if not party:
        return
    current = ACTIVE_CHARACTER.get(session_id)
    if current is None:
        ACTIVE_CHARACTER[session_id] = 0
    else:
        ACTIVE_CHARACTER[session_id] = (current + 1) % len(party)
    logger.info(
        f"Personnage actif → {char_name(party[ACTIVE_CHARACTER[session_id]])} "
        f"(idx {ACTIVE_CHARACTER[session_id]}) — session {session_id[:8]}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — CONSTRUCTION DES MESSAGES OLLAMA
# ═══════════════════════════════════════════════════════════════════════════════

def build_messages(
    history:        List[Dict[str, str]],
    user_input:     str,
    party:          Optional[List[Dict]] = None,
    active_idx:     Optional[int]        = None,
    session_id:     Optional[str]        = None,
    combat_monsters: Optional[list]      = None,
) -> List[Dict[str, str]]:
    """
    Construit la liste de messages pour l'API Ollama.

    Ordre des messages système (tous permanents, hors fenêtre glissante) :
        1. Prompt DM principal
        2. État du groupe + personnage actif
        3. Emplacements de sorts disponibles (si lanceurs dans le groupe)
        4. Stats des monstres en combat (si combat détecté dans le dernier échange)
    Puis :
        5. Historique récent (fenêtre glissante MAX_HISTORY_TURNS)
        6. Input actuel du joueur
    """
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": get_system_prompt()}
    ]

    if party:
        messages.append({
            "role":    "system",
            "content": build_party_context(party, active_idx)
        })

    # P2c — emplacements de sorts
    if session_id:
        slots_ctx = build_slots_context(session_id)
        if slots_ctx:
            messages.append({"role": "system", "content": slots_ctx})

    # P2a — stats des monstres en combat
    if combat_monsters:
        monsters_ctx = build_monsters_context(combat_monsters)
        if monsters_ctx:
            messages.append({"role": "system", "content": monsters_ctx})

    recent = history[-(MAX_HISTORY_TURNS * 2):]
    messages.extend(recent)
    messages.append({"role": "user", "content": user_input})

    logger.info(
        f"Messages: {len(messages)} | historique: {len(recent)} | "
        f"actif: {char_name(party[active_idx]) if party and active_idx is not None else 'none'} | "
        f"monstres: {len(combat_monsters) if combat_monsters else 0}"
    )
    return messages


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — OLLAMA
# ═══════════════════════════════════════════════════════════════════════════════

async def call_ollama(messages: List[Dict[str, str]]) -> tuple[str, int, int]:
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            )
            resp.raise_for_status()
            data            = resp.json()
            prompt_tokens   = data.get("prompt_eval_count", 0)
            response_tokens = data.get("eval_count", 0)
            reply           = data["message"]["content"]
            logger.info(f"Ollama: prompt={prompt_tokens}t réponse={response_tokens}t")
            return reply, prompt_tokens, response_tokens
    except httpx.ConnectError:
        logger.error("Ollama non accessible")
        return "[ERREUR: Ollama non démarré — lancez 'ollama serve']", 0, 0
    except Exception as e:
        logger.error(f"Erreur Ollama: {e}")
        return f"[ERREUR: {e}]", 0, 0


# ═══════════════════════════════════════════════════════════════════════════════
# PIPER TTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/tts")
async def tts_piper(text: str):
    """GET /tts?text=... — synthèse vocale FR avec piper bundlé."""
    if not PIPER_BIN.exists():
        return HTMLResponse(f"Piper introuvable: {PIPER_BIN}", status_code=500)
    if not PIPER_MODEL_PATH.exists():
        return HTMLResponse(f"Modèle introuvable: {PIPER_MODEL_PATH}", status_code=500)

    # Pas de limite de longueur côté serveur — le découpage en segments
    # est géré côté JS avant l'appel. Chaque segment reçu ici est déjà court.
    text_clean = text.strip()
    piper_env  = {
        **os.environ,
        "LD_LIBRARY_PATH": str(PIPER_LIBS_DIR),
        "ESPEAK_DATA_PATH": str(PIPER_LIBS_DIR / "espeak-ng-data"),
    }
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        proc = subprocess.run(
            [str(PIPER_BIN), "--model", str(PIPER_MODEL_PATH), "--output_file", tmp_path],
            input=text_clean.encode("utf-8"),
            capture_output=True, timeout=30, env=piper_env,
        )
        if proc.returncode != 0:
            logger.error(f"Piper stderr: {proc.stderr.decode()}")
            Path(tmp_path).unlink(missing_ok=True)
            return HTMLResponse("Piper a échoué", status_code=500)
        with open(tmp_path, "rb") as f:
            wav_bytes = f.read()
        Path(tmp_path).unlink(missing_ok=True)
        logger.info(f"TTS OK: {len(text_clean)} chars → {len(wav_bytes)} bytes")
        return StreamingResponse(
            io.BytesIO(wav_bytes),
            media_type="audio/wav",
            headers={"Cache-Control": "no-cache"},
        )
    except subprocess.TimeoutExpired:
        return HTMLResponse("TTS timeout", status_code=500)
    except Exception as e:
        logger.error(f"TTS erreur: {e}")
        return HTMLResponse(f"TTS erreur: {e}", status_code=500)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES WEB
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session_id  = get_or_create_session_id(request)
    history     = CONVERSATIONS.get(session_id, [])
    party       = PARTY_STATES.get(session_id, [])
    active_idx  = ACTIVE_CHARACTER.get(session_id)
    total_chars = sum(len(msg["content"]) for msg in history)

    spell_slots = get_slots_display(session_id)

    response = templates.TemplateResponse(
        "index.html",
        {
            "request":      request,
            "history":      history,
            "party":        party,
            "active_idx":   active_idx,
            "spell_slots":  spell_slots,     # P2c : emplacements de sorts pour le panneau
            "session_id":   session_id[:8],
            "total_chars":  total_chars,
            "code_version": CODE_VERSION,
            "ollama_model": OLLAMA_MODEL,
        },
    )
    if "session_id" not in request.cookies:
        response.set_cookie("session_id", session_id, httponly=True)
    return response


@app.post("/send", response_class=RedirectResponse)
async def send_message(request: Request, user_input: str = Form(...)):
    """
    Traite le message du joueur.

    Le message est attendu préfixé par le JS avec le nom du personnage actif :
    "Thorin : j'attaque le gobelin [d20: 17]"

    Après chaque envoi réussi, advance_active_character() passe
    automatiquement au personnage suivant (round-robin).
    """
    if len(user_input) > MAX_USER_INPUT_LEN:
        logger.warning("Input trop long, tronqué")
        user_input = user_input[:MAX_USER_INPUT_LEN]

    session_id = get_or_create_session_id(request)
    history    = CONVERSATIONS.setdefault(session_id, [])

    # Détection JSON de groupe
    detected_party = extract_party_from_input(user_input)
    if detected_party:
        PARTY_STATES[session_id]     = detected_party
        ACTIVE_CHARACTER[session_id] = 0
        # P2c — initialisation des emplacements de sorts
        init_spell_slots_for_party(session_id)
        logger.info(f"Groupe chargé: {len(detected_party)} perso(s) — session {session_id[:8]}")

    party      = PARTY_STATES.get(session_id)
    active_idx = ACTIVE_CHARACTER.get(session_id)

    # P2b — détection commande de repos
    rest_type = detect_rest_command(user_input)
    if rest_type:
        rest_msg = apply_rest(session_id, rest_type)
        # Le message de repos est ajouté comme action du joueur,
        # puis on laisse Ollama narrer les effets du repos
        user_input = user_input + "\n[Systeme: " + rest_msg + "]"

    # P2a — détection des monstres dans les derniers messages du DM
    # On analyse les 2 derniers messages assistant pour capturer les combats
    # démarrés dans le tour précédent (le DM a pu annoncer un monstre avant)
    recent_dm = [m["content"] for m in history[-4:] if m["role"] == "assistant"]
    combat_monsters = []
    for dm_text in recent_dm:
        combat_monsters.extend(detect_monsters_in_text(dm_text))
    # Dédoublonnage par clé
    seen_keys = set()
    unique_monsters = []
    for m in combat_monsters:
        if m["key"] not in seen_keys:
            seen_keys.add(m["key"])
            unique_monsters.append(m)
    combat_monsters = unique_monsters

    messages   = build_messages(
        history, user_input, party, active_idx,
        session_id=session_id,
        combat_monsters=combat_monsters if combat_monsters else None,
    )
    reply, _, _ = await call_ollama(messages)

    # Nettoyage Markdown + détection monstres dans la NOUVELLE réponse du DM
    reply = strip_markdown(reply)

    # P2a — si la réponse du DM introduit de nouveaux monstres via tag [COMBAT:],
    # on les stocke pour le prochain tour (ils seront dans recent_dm)
    new_monsters = detect_monsters_in_text(reply)
    if new_monsters:
        logger.info(f"Nouveaux monstres détectés dans réponse DM: {[m['key'] for m in new_monsters]}")

    # Suppression du tag [COMBAT:...] de la réponse affichée (tag interne)
    reply_clean = re.sub(r'\[COMBAT:[^\]]*\]', '', reply).strip()

    history.append({"role": "user",      "content": user_input})
    history.append({"role": "assistant", "content": reply_clean})

    # Avance au personnage suivant APRÈS sauvegarde réussie
    advance_active_character(session_id)

    logger.info(f"Tour ajouté — session {session_id[:8]}, {len(history)} messages")
    return RedirectResponse(url="/", status_code=303)


@app.get("/party/active")
async def get_active_character(request: Request):
    """Retourne le personnage actif courant (utilisé par le JS au chargement)."""
    session_id = get_or_create_session_id(request)
    party      = PARTY_STATES.get(session_id, [])
    active_idx = ACTIVE_CHARACTER.get(session_id)
    if not party or active_idx is None:
        return JSONResponse({"active_idx": None, "name": None})
    return JSONResponse({
        "active_idx": active_idx,
        "name":       char_name(party[active_idx]),
    })


@app.post("/party/active")
async def set_active_character(request: Request):
    """
    Force manuellement le personnage actif (clic sur une carte dans le panneau).
    Body JSON : {"index": N}
    """
    session_id = get_or_create_session_id(request)
    party      = PARTY_STATES.get(session_id, [])
    try:
        data = await request.json()
        idx  = int(data["index"])
        if 0 <= idx < len(party):
            ACTIVE_CHARACTER[session_id] = idx
            name = char_name(party[idx])
            logger.info(f"Personnage actif forcé: {name} — session {session_id[:8]}")
            return JSONResponse({"ok": True, "active_idx": idx, "name": name})
    except Exception as e:
        logger.error(f"Erreur set active: {e}")
    return JSONResponse({"ok": False}, status_code=400)


@app.post("/party/hp")
async def update_hp(request: Request):
    """Mise à jour AJAX des HP d'un personnage. Body : {"index": N, "hp": V}"""
    session_id = get_or_create_session_id(request)
    party      = PARTY_STATES.get(session_id, [])
    try:
        data = await request.json()
        idx  = int(data["index"])
        hp   = int(data["hp"])
        if 0 <= idx < len(party):
            for key in ("HP", "hp", "hit_points"):
                if key in party[idx]:
                    party[idx][key] = hp
                    break
            else:
                party[idx]["HP"] = hp
            logger.info(f"HP mis à jour: {char_name(party[idx])} → {hp}")
            return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Erreur update HP: {e}")
    return JSONResponse({"ok": False}, status_code=400)


@app.post("/spells/use")
async def use_spell_slot(request: Request):
    """
    Marque un emplacement de sort comme utilisé.
    Appelé depuis le panneau sorts quand le joueur coche un emplacement.
    Body JSON : {"char_name": "Aria", "slot_level": 2, "delta": 1}
    delta = +1 (utiliser) ou -1 (récupérer manuellement)
    """
    session_id = get_or_create_session_id(request)
    slots_map  = SPELL_SLOTS_USED.get(session_id, {})
    party      = PARTY_STATES.get(session_id, [])
    try:
        data       = await request.json()
        cname      = data["char_name"]
        slot_lvl   = int(data["slot_level"]) - 1   # 0-indexed
        delta      = int(data.get("delta", 1))

        # Trouve le max pour ce niveau
        char = next((c for c in party if char_name(c) == cname), None)
        if not char:
            return JSONResponse({"ok": False, "error": "char not found"}, status_code=400)
        max_slots = char.get("_slots_max", [0]*9)
        mx = max_slots[slot_lvl] if slot_lvl < 9 else 0

        current = slots_map.get(cname, [0]*9)
        new_used = max(0, min(mx, current[slot_lvl] + delta))
        current[slot_lvl] = new_used
        slots_map[cname] = current

        logger.info(f"Slot utilisé: {cname} niv{slot_lvl+1} → {new_used}/{mx}")
        return JSONResponse({"ok": True, "used": new_used, "max": mx, "available": mx - new_used})
    except Exception as e:
        logger.error(f"Erreur use_spell_slot: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/reset", response_class=RedirectResponse)
async def reset_conversation(request: Request):
    session_id = get_or_create_session_id(request)
    CONVERSATIONS.pop(session_id, None)
    PARTY_STATES.pop(session_id, None)
    ACTIVE_CHARACTER.pop(session_id, None)
    SPELL_SLOTS_USED.pop(session_id, None)   # P2c : reset des emplacements
    logger.info(f"Session reset: {session_id[:8]}")
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
async def health_check():
    ollama_status = "DOWN"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
        ollama_status = "OK" if resp.status_code == 200 else "KO"
    except Exception:
        pass
    return {
        "code_version":     CODE_VERSION,
        "ollama_model":     OLLAMA_MODEL,
        "ollama_status":    ollama_status,
        "piper_bin_ok":     PIPER_BIN.exists(),
        "piper_model_ok":   PIPER_MODEL_PATH.exists(),
        "active_sessions":  len(CONVERSATIONS),
        "active_parties":   len(PARTY_STATES),
        "monsters_loaded":  len(MONSTERS_DB),
        "spells_loaded":    bool(SPELLS_DB),
    }


if __name__ == "__main__":
    import uvicorn
    logger.info(f"D&D DM v{CODE_VERSION} — modèle: {OLLAMA_MODEL}")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
