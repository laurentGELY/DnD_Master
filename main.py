#!/usr/bin/env python3
"""
D&D 5e AI Dungeon Master Web App
================================
Version: see Config.yml → app.version
Licence: MIT (usage personnel)

NOUVEAUTÉS v1.8.0
-----------------
- Configuration externalisée dans Config.yml (plus de constantes codées en dur)
- Env-vars OLLAMA_URL et OLLAMA_MODEL restent prioritaires sur Config.yml
- pyyaml ajouté aux dépendances

NOUVEAUTÉS v1.7.0
-----------------
- Subprocess Piper asynchrone — ne bloque plus l'event loop pendant la synthèse vocale
- Client httpx partagé avec pool de connexions (une seule connexion TCP vers Ollama)
- System prompt mis en cache par mtime — pas de lecture disque à chaque requête
- État de session consolidé dans un dataclass Session (4 dicts → 1)
- Expiration automatique des sessions inactives (SESSION_TTL_SECONDS, défaut 3h)
- Constantes module-level pour PIPER_ENV et les listes de classes de lanceurs
- Avancement du tour protégé : bloqué en cas d'erreur Ollama

NOUVEAUTÉS v1.6.0
-----------------
- (voir historique git)

NOUVEAUTÉS v1.4.0
-----------------
- Sélection de voix Piper dans l'interface (menu déroulant)
- Endpoint GET /voices → liste tous les modèles .onnx disponibles dans voices/
- Endpoint GET /tts accepte maintenant un paramètre optionnel voice= (défaut : fr_FR-gilles-low.onnx)
- Choix de voix persisté dans le localStorage du navigateur
- Ajout d'une nouvelle voix : poser le .onnx dans voices/ suffit, sans redémarrage

NOUVEAUTÉS v1.3.0
-----------------
- Tour par tour strict (combat ET hors combat)
- Suivi du personnage actif en session (active_character)
- Sélecteur de personnage actif dans l'interface → préfixe auto du message
- Commande !ordre ajoutée au system prompt
- Endpoint GET /party/active → retourne le personnage actif courant
- Endpoint POST /party/active → force manuellement le personnage actif
- Le personnage actif est réinjecté dans le contexte Ollama

LANCEMENT
---------
cd dnd-dm-app
source ../.venv/bin/activate
uvicorn main:app --reload --host 127.0.0.1 --port 8000
"""

import asyncio
import io
import json
import os
import time
import tempfile
import uuid
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

import yaml

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
BASE_DIR           = Path(__file__).parent
SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
TEMPLATES_DIR      = BASE_DIR / "templates"

# ── Chargement de Config.yml ──────────────────────────────────────────────────
_CONFIG_PATH = BASE_DIR / "Config.yml"

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        logging.warning(f"Config.yml introuvable ({_CONFIG_PATH}) — valeurs par défaut utilisées")
        return {}
    with open(_CONFIG_PATH, encoding="utf-8") as _f:
        return yaml.safe_load(_f) or {}

_CFG = _load_config()

CODE_VERSION        = _CFG.get("app",               {}).get("version",          "0.0.0")

OLLAMA_URL        = os.getenv("OLLAMA_URL",   _CFG.get("ollama", {}).get("url",        "http://localhost:11434"))
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", _CFG.get("ollama", {}).get("model",      "dnd-dm-magistral"))
OLLAMA_KEEP_ALIVE =                           _CFG.get("ollama", {}).get("keep_alive", "30m")

DEFAULT_VOICE       = _CFG.get("tts",                {}).get("default_voice",      "fr_FR-gilles-low.onnx")

MAX_HISTORY_TURNS   = _CFG.get("session",            {}).get("max_history_turns",  50)
MAX_USER_INPUT_LEN  = _CFG.get("session",            {}).get("max_user_input_len", 2000)
SESSION_TTL_SECONDS = _CFG.get("session",            {}).get("ttl_seconds",        10800)

COMPACT_THRESHOLD   = _CFG.get("context_compaction", {}).get("threshold",          80)
COMPACT_KEEP_RECENT = _CFG.get("context_compaction", {}).get("keep_recent",        40)

PIPER_BIN      = BASE_DIR / "bin" / "piper"
PIPER_LIBS_DIR = BASE_DIR / "bin" / "piper_amd64"
VOICES_DIR     = BASE_DIR / "voices"

# Piper environment — construit une seule fois au démarrage
PIPER_ENV: dict = {
    **os.environ,
    "LD_LIBRARY_PATH":  str(PIPER_LIBS_DIR),
    "ESPEAK_DATA_PATH": str(PIPER_LIBS_DIR / "espeak-ng-data"),
}

# Chemins vers les fichiers de données SRD
MONSTERS_PATH = BASE_DIR / "data" / "monsters.json"
SPELLS_PATH   = BASE_DIR / "data" / "spells.json"

PARTY_REQUIRED_KEYS = {"name", "race", "class", "level", "HP", "AC",
                       "classe", "niveau", "hp", "ac", "hit_points"}


class Character(BaseModel):
    """Schéma de validation d'un personnage joueur. Accepte les variantes FR et EN."""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name:  str           = Field("?",  validation_alias=AliasChoices("name", "nom"))
    race:  str           = Field("?")
    cls:   str           = Field("?",  validation_alias=AliasChoices("class", "classe"),
                                       serialization_alias="class")
    level: int           = Field(1,    validation_alias=AliasChoices("level", "niveau"))
    hp:    int           = Field(0,    validation_alias=AliasChoices("HP", "hp", "hit_points",
                                                                     "hp_current"),
                                       serialization_alias="HP")
    hp_max: Optional[int] = Field(None, validation_alias=AliasChoices("HP_max", "hp_max",
                                                                       "max_hp"),
                                        serialization_alias="HP_max")
    ac:    int           = Field(10,   validation_alias=AliasChoices("AC", "ac", "armor_class"),
                                       serialization_alias="AC")
    str_:  Optional[int] = Field(None, validation_alias=AliasChoices("STR", "str"),
                                       serialization_alias="STR")
    dex:   Optional[int] = Field(None, validation_alias=AliasChoices("DEX", "dex"),
                                       serialization_alias="DEX")
    con:   Optional[int] = Field(None, validation_alias=AliasChoices("CON", "con"),
                                       serialization_alias="CON")
    int_:  Optional[int] = Field(None, validation_alias=AliasChoices("INT", "int"),
                                       serialization_alias="INT")
    wis:   Optional[int] = Field(None, validation_alias=AliasChoices("WIS", "wis"),
                                       serialization_alias="WIS")
    cha:   Optional[int] = Field(None, validation_alias=AliasChoices("CHA", "cha"),
                                       serialization_alias="CHA")


def _normalise_char(raw: dict) -> dict:
    """Valide et normalise un dict personnage vers les clés canoniques (HP, AC, class…).
    Les champs supplémentaires du joueur (gold, inventory…) sont conservés via extra='allow'."""
    return Character.model_validate(raw).model_dump(by_alias=True, exclude_none=False)


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

# Listes de classes de lanceurs — extraites une fois au démarrage (Step 1)
_FULL_CASTER_CLASSES:  list[str] = SPELLS_DB.get("full_casters",  {}).get("_classes", [])
_HALF_CASTER_CLASSES:  list[str] = SPELLS_DB.get("half_casters",  {}).get("_classes", [])
_WARLOCK_CLASSES:      list[str] = SPELLS_DB.get("warlock",       {}).get("_classes", [])
_THIRD_CASTER_CLASSES: list[str] = SPELLS_DB.get("third_casters", {}).get("_classes", [])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# ÉTAT DE SESSION (Step 4)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Session:
    """Tout l'état d'une session de jeu. Une instance par UUID cookie."""
    conversations:     list[dict]           = field(default_factory=list)
    party:             list[dict]           = field(default_factory=list)
    active_character:  int | None           = None
    spell_slots_used:  dict[str, list[int]] = field(default_factory=dict)
    last_active:       float                = field(default_factory=time.monotonic)  # monotonic: immune to wall-clock adjustments
    last_error:        str | None           = None
    last_failed_input: str | None           = None

SESSIONS: dict[str, Session] = {}


def get_session(session_id: str) -> Session:
    """Retourne la session existante ou en crée une nouvelle ; met à jour last_active."""
    # Use get_session (creates) for write paths; SESSIONS.get (returns None) for read-only paths
    # so that a stale/missing cookie never silently creates an empty session.
    if session_id not in SESSIONS:
        SESSIONS[session_id] = Session()
    sess = SESSIONS[session_id]
    sess.last_active = time.monotonic()
    return sess


# ═══════════════════════════════════════════════════════════════════════════════
# NETTOYAGE MARKDOWN
# ═══════════════════════════════════════════════════════════════════════════════
# Le modèle génère du Markdown par réflexe (**gras**, *italique*, ### titres…).
# Ces caractères sont affichés tels quels dans l'interface et prononcés
# littéralement par le TTS ("astérisque astérisque mot astérisque astérisque").
# On nettoie la réponse une seule fois côté serveur, avant toute sauvegarde,
# ce qui garantit que l'affichage ET le TTS reçoivent du texte brut propre.

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
      8. Nettoyage résiduel : plus de trois sauts de ligne → deux max
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
    # 8. Plus de trois sauts de ligne consécutifs → deux max
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# P2a — DÉTECTION ET INJECTION DES MONSTRES SRD
# ═══════════════════════════════════════════════════════════════════════════════

def detect_monsters_in_text(text: str) -> list[dict]:
    """
    Détecte les monstres du compendium SRD dans un texte (réponse du DM).

    Deux stratégies combinées (tag prioritaire, regex en fallback) :

    1. TAG [COMBAT: nom1, nom2] dans la réponse du DM
       Exemple : [COMBAT: gobelin, loup géant]
       → fiable, explicite, pas d'ambiguïté

    2. Si pas de tag : scan du texte contre l'index FR des noms de monstres
       → utile pour les monstres mentionnés sans tag
       → peut générer des faux positifs sur des noms communs courts

    Returns: liste de dicts stats monstres.
    """
    found = []

    # Stratégie A : tag [COMBAT: ...]
    tag_match = re.search(r'\[COMBAT:\s*([^\]]+)\]', text, re.IGNORECASE)
    if tag_match:
        names = [n.strip().lower() for n in tag_match.group(1).split(',')]
        for name in names:
            key = MONSTER_FR_INDEX.get(name)
            if not key:
                key = name if name in MONSTERS_DB else None
            if key and key in MONSTERS_DB:
                found.append({"key": key, **MONSTERS_DB[key]})
        if found:
            logger.info(f"Monstres via tag COMBAT: {[m['key'] for m in found]}")
            return found

    # Stratégie B : regex sur les noms FR du compendium
    text_lower = text.lower()
    for fr_name, key in MONSTER_FR_INDEX.items():
        if len(fr_name) < 4:   # noms trop courts → trop de faux positifs
            continue
        if fr_name in text_lower and key in MONSTERS_DB:
            if not any(m["key"] == key for m in found):
                found.append({"key": key, **MONSTERS_DB[key]})

    if found:
        logger.info(f"Monstres via regex: {[m['key'] for m in found]}")
    return found


def build_monsters_context(monsters: list[dict]) -> str:
    """
    Formate le bloc de stats monstres à injecter dans le contexte Ollama.
    Injecté uniquement quand un combat est détecté.
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
    if re.search(r'!rest|!shortrest|repos court|short rest', t):
        return "short"
    if re.search(r'!longrest|!long|repos long|long rest', t):
        return "long"
    return None


def apply_rest(session_id: str, rest_type: str) -> str:
    """
    Applique les effets d'un repos sur le groupe.

    Short rest :
    - Warlock : récupère tous ses emplacements de pacte
    - Toutes classes : peuvent dépenser des Hit Dice (géré narrativement par le DM)

    Long rest :
    - Tous les personnages : HP max, tous les emplacements de sorts récupérés
    """
    sess       = get_session(session_id)
    party      = sess.party
    slots_used = sess.spell_slots_used
    results    = []

    if rest_type == "long":
        for char in party:
            char["HP"] = char.get("HP_max", char["HP"])
        for char in party:
            name = char_name(char)
            if name in slots_used:
                slots_used[name] = [0] * 9
        results.append("Repos long terminé. HP max récupérés, tous les emplacements de sorts récupérés.")
        logger.info(f"Long rest appliqué — session {session_id[:8]}")

    elif rest_type == "short":
        for char in party:
            name       = char_name(char)
            cls        = (char.get("class", "") or "").lower()
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
    if any(c in cls for c in _FULL_CASTER_CLASSES):  return "full"
    if any(c in cls for c in _HALF_CASTER_CLASSES):  return "half"
    if any(c in cls for c in _WARLOCK_CLASSES):       return "warlock"
    if any(c in cls for c in _THIRD_CASTER_CLASSES):  return "third"
    return None


def init_spell_slots_for_party(session_id: str) -> None:
    """
    Initialise les emplacements de sorts pour tous les personnages du groupe.
    Appelé quand un groupe est chargé. Ne réinitialise pas si déjà présent
    (évite d'écraser l'état en cours si le joueur renvoie son JSON).
    """
    sess      = get_session(session_id)
    party     = sess.party
    slots_map = sess.spell_slots_used

    for char in party:
        name        = char_name(char)
        cls         = char.get("class", "")
        level       = str(char.get("level", 1))
        caster_type = _get_caster_type(cls)

        if not caster_type or name in slots_map:
            continue   # Non-lanceur ou déjà initialisé

        if caster_type == "warlock":
            wlock_data = SPELLS_DB.get("warlock", {}).get("slots_by_level", {})
            lvl_data   = wlock_data.get(level, {"slots": 0, "slot_level": 1})
            # Warlock : un seul niveau d'emplacement — stocké sur 9 niveaux pour uniformité
            slots_map[name]    = [0] * 9
            char["_slots_max"] = [0] * 9
            slot_lvl           = lvl_data.get("slot_level", 1) - 1
            char["_slots_max"][slot_lvl] = lvl_data.get("slots", 0)
            char["_caster_type"] = "warlock"
            char["_slot_level"]  = slot_lvl + 1
        else:
            tbl_key   = f"{caster_type}_casters" if caster_type != "third" else "third_casters"
            tbl       = SPELLS_DB.get(tbl_key, {}).get("slots_by_level", {})
            max_slots = tbl.get(level, [0] * 9)
            slots_map[name]      = [0] * 9
            char["_slots_max"]   = max_slots
            char["_caster_type"] = caster_type

        logger.info(f"Sorts initialisés: {name} ({cls} niv.{level}) type={caster_type}")


def get_slots_display(session_id: str) -> list[dict]:
    """
    Retourne l'état des emplacements de sorts pour tous les lanceurs du groupe.
    Format utilisé par le template Jinja2 pour le panneau sorts.
    """
    sess = SESSIONS.get(session_id)
    if not sess:
        return []
    party     = sess.party
    slots_map = sess.spell_slots_used
    result    = []

    for char in party:
        name      = char_name(char)
        max_slots = char.get("_slots_max")
        ctype     = char.get("_caster_type")
        if not max_slots or not ctype:
            continue   # Non-lanceur

        used         = slots_map.get(name, [0] * 9)
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
# HELPERS — UTILITAIRES GÉNÉRAUX
# ═══════════════════════════════════════════════════════════════════════════════

# Cache du system prompt — relu seulement si le fichier a changé sur disque (Step 2)
_prompt_cache: str   = ""
_prompt_mtime: float = 0.0


def get_system_prompt() -> str:
    global _prompt_cache, _prompt_mtime
    if not SYSTEM_PROMPT_PATH.exists():
        logger.error(f"Fichier prompt manquant: {SYSTEM_PROMPT_PATH}")
        return "[ERREUR: Créez prompts/system_prompt.txt]"
    mtime = SYSTEM_PROMPT_PATH.stat().st_mtime
    if mtime != _prompt_mtime:
        _prompt_cache = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        _prompt_mtime = mtime
        logger.info(f"Prompt rechargé: {len(_prompt_cache)} caractères")
    return _prompt_cache


def get_or_create_session_id(request: Request) -> str:
    session_id = request.cookies.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
        logger.info(f"Nouvelle session: {session_id[:8]}...")
    return session_id


def char_name(char: Dict) -> str:
    """Retourne le nom d'un personnage. Après normalisation, la clé est toujours 'name'."""
    return char.get("name", "?")


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
            if key in raw:
                val = raw[key]
                if isinstance(val, list):
                    characters = val
                elif isinstance(val, dict):
                    # "party": { single character } — wrap in list
                    characters = [val]
                if characters:
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
        # Intersection (not subset): a single matching key is enough to recognise
        # a character dict, because players use inconsistent field names (HP/hp/hit_points).
        if isinstance(char, dict) and (PARTY_REQUIRED_KEYS & set(char.keys())):
            logger.info(f"Groupe détecté: {len(characters)} personnage(s)")
            return [_normalise_char(c) for c in characters if isinstance(c, dict)]
    return None


def build_party_context(party: List[Dict], active_idx: Optional[int] = None) -> str:
    lines = ["ÉTAT ACTUEL DU GROUPE (maintenir ces valeurs tout au long de la partie):"]
    for i, char in enumerate(party):
        name   = char_name(char)
        race   = char.get("race",   "?")
        cls    = char.get("class",  "?")
        level  = char.get("level",  "?")
        hp     = char.get("HP",     0)
        hp_max = char.get("HP_max", hp)
        ac     = char.get("AC",     0)

        stats = []
        for stat in ("STR", "DEX", "CON", "INT", "WIS", "CHA"):
            val = char.get(stat)
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
    Appelé uniquement après un échange Ollama réussi.
    """
    sess  = get_session(session_id)
    party = sess.party
    if not party:
        return
    current = sess.active_character
    if current is None:
        sess.active_character = 0
    else:
        sess.active_character = (current + 1) % len(party)
    logger.info(
        f"Personnage actif → {char_name(party[sess.active_character])} "
        f"(idx {sess.active_character}) — session {session_id[:8]}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — CONSTRUCTION DES MESSAGES OLLAMA
# ═══════════════════════════════════════════════════════════════════════════════

def build_messages(
    history:         List[Dict[str, str]],
    user_input:      str,
    party:           Optional[List[Dict]] = None,
    active_idx:      Optional[int]        = None,
    session_id:      Optional[str]        = None,
    combat_monsters: Optional[list]       = None,
) -> List[Dict[str, str]]:
    """
    Construit la liste de messages pour l'API Ollama.

    Ordre des messages système (hors fenêtre glissante) :
        1. Prompt DM principal
        2. État du groupe + personnage actif
        3. Emplacements de sorts disponibles (si lanceurs dans le groupe)
        4. Stats des monstres en combat (si combat détecté dans le dernier échange)
    Puis :
        5. Historique récent (fenêtre glissante MAX_HISTORY_TURNS)
        6. Input actuel du joueur
    """
    # System prompt first — static, never changes → Ollama can cache this prefix across turns.
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": get_system_prompt()}
    ]

    # History second — stable prefix (old entries never mutate, only new ones appended).
    # Keeping history here maximises KV-cache reuse: the cache covers system + all prior turns.
    # ×2 because each "turn" is two messages: one user + one assistant.
    recent = history[-(MAX_HISTORY_TURNS * 2):]
    messages.extend(recent)

    # Dynamic context last — changes every turn (HP, slots, monsters).
    # Injected right before the user message so the model reads fresh state.
    # Placing it here prevents it from invalidating the cached static prefix.
    if party:
        messages.append({"role": "system", "content": build_party_context(party, active_idx)})

    if session_id:
        slots_ctx = build_slots_context(session_id)
        if slots_ctx:
            messages.append({"role": "system", "content": slots_ctx})

    if combat_monsters:
        monsters_ctx = build_monsters_context(combat_monsters)
        if monsters_ctx:
            messages.append({"role": "system", "content": monsters_ctx})

    messages.append({"role": "user", "content": user_input})

    ctx_chars = sum(len(m["content"]) for m in messages)
    # Rough estimate: French LLM text ≈ 4 chars/token; num_ctx is 40 000 tokens = ~160 000 chars.
    # Warn at 75 % to leave headroom for the reply.
    if ctx_chars > 120_000:
        logger.warning(
            f"[CONTEXT] Contexte très large: {ctx_chars} chars (~{ctx_chars // 4}t) "
            f"— proche de la limite num_ctx (40 000 t)"
        )
    logger.info(
        f"[CONTEXT] {len(messages)} msgs | historique: {len(recent)} | "
        f"actif: {char_name(party[active_idx]) if party and active_idx is not None else 'none'} | "
        f"monstres: {len(combat_monsters) if combat_monsters else 0} | "
        f"contexte: {ctx_chars} chars (~{ctx_chars // 4}t)"
    )
    return messages


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — OLLAMA (Step 3)
# ═══════════════════════════════════════════════════════════════════════════════

# Client httpx partagé — initialisé au démarrage par le lifespan
_ollama_client: httpx.AsyncClient | None = None


async def call_ollama(messages: List[Dict[str, str]]) -> tuple[str, int, int]:
    if os.getenv("OLLAMA_MOCK"):
        reply = os.getenv("OLLAMA_MOCK_REPLY", "Réponse simulée du Maître du Donjon.")
        logger.info("[MOCK] Ollama mock actif — réponse fixe")
        return reply, 0, 0
    try:
        t0   = time.perf_counter()
        resp = await _ollama_client.post(
            "/api/chat",
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False,
                  "keep_alive": OLLAMA_KEEP_ALIVE},
        )
        resp.raise_for_status()
        elapsed         = time.perf_counter() - t0
        data            = resp.json()
        prompt_tokens   = data.get("prompt_eval_count", 0)
        response_tokens = data.get("eval_count", 0)
        reply           = data["message"]["content"]
        load_s          = data.get("load_duration",        0) / 1e9
        prefill_s       = data.get("prompt_eval_duration", 0) / 1e9
        gen_s           = data.get("eval_duration",        0) / 1e9
        prefill_tps     = prompt_tokens   / prefill_s if prefill_s > 0 else 0
        gen_tps         = response_tokens / gen_s     if gen_s     > 0 else 0
        logger.info(
            f"[PERF] Ollama: total={elapsed:.1f}s | "
            f"chargement={load_s:.1f}s | "
            f"analyse={prefill_s:.1f}s ({prefill_tps:.0f} t/s, {prompt_tokens}t) | "
            f"réponse={gen_s:.1f}s ({gen_tps:.0f} t/s, {response_tokens}t)"
        )
        return reply, prompt_tokens, response_tokens
    except httpx.ConnectError:
        logger.error("Ollama non accessible")
        return "[ERREUR: Ollama non démarré — lancez 'ollama serve']", 0, 0
    except httpx.TimeoutException:
        logger.error("Ollama timeout")
        return "[ERREUR: Délai dépassé — Ollama trop lent ou contexte trop long]", 0, 0
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        logger.error(f"Ollama HTTP {e.response.status_code}: {body}")
        return f"[ERREUR: HTTP {e.response.status_code} — {body}]", 0, 0
    except Exception as e:
        err_type = type(e).__name__
        detail   = str(e) or "(aucun détail)"
        logger.error(f"Erreur Ollama ({err_type}): {detail}")
        return f"[ERREUR: {err_type} — {detail}]", 0, 0


# ═══════════════════════════════════════════════════════════════════════════════
# COMPACTION DE L'HISTORIQUE
# ═══════════════════════════════════════════════════════════════════════════════

async def maybe_compact_history(session_id: str) -> None:
    """
    Compacte l'historique quand il dépasse COMPACT_THRESHOLD messages.

    Les anciens échanges sont résumés par Ollama en un seul message système,
    puis remplacent les messages originaux. Les COMPACT_KEEP_RECENT messages
    les plus récents sont conservés verbatim pour la continuité narrative.

    En cas d'erreur Ollama, l'historique est conservé intact (pas de perte).
    """
    sess = get_session(session_id)
    if len(sess.conversations) <= COMPACT_THRESHOLD:
        return

    to_compact = sess.conversations[:-COMPACT_KEEP_RECENT]
    to_keep    = sess.conversations[-COMPACT_KEEP_RECENT:]

    logger.info(
        f"Compaction déclenchée — session {session_id[:8]}: "
        f"{len(to_compact)} messages → résumé + {len(to_keep)} récents"
    )

    summary_messages = [
        {"role": "system", "content": get_system_prompt()},
        *to_compact,
        {
            "role": "user",
            "content": (
                "Génère un résumé compact et exhaustif de tout ce qui s'est passé "
                "dans la campagne jusqu'ici. Inclus : composition du groupe et état "
                "actuel (HP, conditions), quête en cours et phase narrative, lieux "
                "visités, PNJs importants et leur dernière position connue, événements "
                "clés, décisions importantes des joueurs et leurs conséquences, ordre "
                "du tour actuel. Sois précis et factuel — ce résumé remplacera "
                "l'historique complet dans ton contexte."
            ),
        },
    ]

    summary, _, _ = await call_ollama(summary_messages)

    if summary.startswith("[ERREUR:"):
        logger.warning("Compaction échouée (erreur Ollama) — historique conservé intact")
        return

    summary_msg = {
        "role":    "system",
        "content": f"[RÉSUMÉ DE CAMPAGNE — {len(to_compact)} messages compactés]\n{strip_markdown(summary)}",
    }
    sess.conversations = [summary_msg] + to_keep
    logger.info(
        f"Compaction terminée — session {session_id[:8]}: "
        f"historique réduit à {len(sess.conversations)} messages"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DÉMARRAGE / ARRÊT (Steps 3 + 7)
# ═══════════════════════════════════════════════════════════════════════════════

async def _cleanup_sessions() -> None:
    """Supprime les sessions inactives depuis plus de SESSION_TTL_SECONDS."""
    while True:
        await asyncio.sleep(600)   # vérification toutes les 10 minutes
        cutoff  = time.monotonic() - SESSION_TTL_SECONDS
        expired = [sid for sid, s in list(SESSIONS.items()) if s.last_active < cutoff]
        for sid in expired:
            SESSIONS.pop(sid, None)
        if expired:
            logger.info(f"{len(expired)} session(s) expirées supprimées — {len(SESSIONS)} actives")


async def _prewarm_model() -> None:
    t0_warm = time.perf_counter()
    logger.info(f"Pré-chargement {OLLAMA_MODEL} démarré…")
    try:
        await _ollama_client.post(
            "/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": "", "keep_alive": OLLAMA_KEEP_ALIVE},
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0),
        )
        elapsed_warm = time.perf_counter() - t0_warm
        logger.info(f"[PERF] Pré-chargement {OLLAMA_MODEL} terminé ({elapsed_warm:.1f}s) — modèle en VRAM")
    except Exception as e:
        elapsed_warm = time.perf_counter() - t0_warm
        logger.warning(
            f"[PERF] Pré-chargement Ollama échoué après {elapsed_warm:.1f}s ({type(e).__name__}: {e}) "
            "— le modèle sera chargé à la première requête"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ollama_client
    _ollama_client = httpx.AsyncClient(
        base_url=OLLAMA_URL,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    )
    # Préchauffage en arrière-plan : l'app répond immédiatement,
    # le modèle se charge en VRAM pendant que la page s'affiche.
    asyncio.create_task(_prewarm_model())
    cleanup_task = asyncio.create_task(_cleanup_sessions())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await _ollama_client.aclose()


# ═══════════════════════════════════════════════════════════════════════════════
# APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="D&D 5e AI DM", version=CODE_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ═══════════════════════════════════════════════════════════════════════════════
# PIPER TTS (Step 6 — subprocess asynchrone)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/voices")
def list_voices():
    """GET /voices — liste les modèles Piper disponibles dans voices/."""
    models = sorted(p.name for p in VOICES_DIR.glob("*.onnx"))
    return {"voices": models, "default": DEFAULT_VOICE}


@app.get("/tts")
async def tts_piper(text: str, voice: str = DEFAULT_VOICE):
    """GET /tts?text=...&voice=... — synthèse vocale FR avec piper bundlé."""
    # Sécurité : le modèle doit être un fichier .onnx situé dans VOICES_DIR
    model_path = (VOICES_DIR / voice).resolve()
    if model_path.parent != VOICES_DIR.resolve() or model_path.suffix != ".onnx":
        return HTMLResponse("Voix invalide", status_code=400)
    if not PIPER_BIN.exists():
        return HTMLResponse(f"Piper introuvable: {PIPER_BIN}", status_code=500)
    if not model_path.exists():
        return HTMLResponse(f"Modèle introuvable: {model_path}", status_code=500)

    # Pas de limite de longueur côté serveur — le découpage en segments
    # est géré côté JS avant l'appel. Chaque segment reçu ici est déjà court.
    text_clean = text.strip()
    try:
        # Piper does not support streaming stdout output, so we write to a temp file
        # and read it back after the process exits.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        proc = await asyncio.create_subprocess_exec(
            str(PIPER_BIN), "--model", str(model_path), "--output_file", tmp_path,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=PIPER_ENV,
        )
        try:
            t0_tts = time.perf_counter()
            _, stderr_data = await asyncio.wait_for(
                proc.communicate(input=text_clean.encode("utf-8")),
                timeout=30.0,
            )
            elapsed_tts = time.perf_counter() - t0_tts
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            Path(tmp_path).unlink(missing_ok=True)
            return HTMLResponse("TTS timeout", status_code=500)
        if proc.returncode != 0:
            logger.error(f"Piper stderr: {stderr_data.decode()}")
            Path(tmp_path).unlink(missing_ok=True)
            return HTMLResponse("Piper a échoué", status_code=500)
        with open(tmp_path, "rb") as f:
            wav_bytes = f.read()
        Path(tmp_path).unlink(missing_ok=True)
        logger.info(f"[PERF] TTS: {elapsed_tts:.2f}s | {len(text_clean)} chars → {len(wav_bytes)} bytes")
        return StreamingResponse(
            io.BytesIO(wav_bytes),
            media_type="audio/wav",
            headers={"Cache-Control": "no-cache"},
        )
    except Exception as e:
        logger.error(f"TTS erreur: {e}")
        return HTMLResponse(f"TTS erreur: {e}", status_code=500)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES WEB
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session_id  = get_or_create_session_id(request)
    sess        = get_session(session_id)
    total_chars = sum(len(msg["content"]) for msg in sess.conversations)
    spell_slots = get_slots_display(session_id)

    # Lecture flash : on passe l'erreur au template puis on l'efface
    last_error        = sess.last_error
    last_failed_input = sess.last_failed_input
    sess.last_error        = None
    sess.last_failed_input = None

    response = templates.TemplateResponse(
        "index.html",
        {
            "request":           request,
            "history":           sess.conversations,
            "party":             sess.party,
            "active_idx":        sess.active_character,
            "spell_slots":       spell_slots,
            "session_id":        session_id[:8],
            "total_chars":       total_chars,
            "code_version":      CODE_VERSION,
            "ollama_model":      OLLAMA_MODEL,
            "last_error":        last_error,
            "last_failed_input": last_failed_input,
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

    Après chaque échange Ollama réussi, advance_active_character() passe
    automatiquement au personnage suivant (round-robin).
    En cas d'erreur Ollama, le tour n'avance pas (Step 5).
    """
    t0_request = time.perf_counter()

    if len(user_input) > MAX_USER_INPUT_LEN:
        logger.warning("Input trop long, tronqué")
        user_input = user_input[:MAX_USER_INPUT_LEN]

    session_id = get_or_create_session_id(request)
    sess       = get_session(session_id)

    active_name = (
        char_name(sess.party[sess.active_character])
        if sess.party and sess.active_character is not None
        else "aucun"
    )
    logger.info(
        f"[SESSION] {session_id[:8]} | groupe: {len(sess.party)} perso(s) | "
        f"historique: {len(sess.conversations)} msgs | actif: {active_name}"
    )

    # Détection JSON de groupe
    detected_party = extract_party_from_input(user_input)
    if detected_party:
        sess.party            = detected_party
        sess.active_character = 0
        init_spell_slots_for_party(session_id)
        names = [char_name(c) for c in detected_party]
        logger.info(f"Groupe chargé: {names} — session {session_id[:8]}")

    # Conserver l'input original pour le bouton "Retenter" en cas d'erreur
    original_input = user_input

    # P2b — détection commande de repos
    rest_type = detect_rest_command(user_input)
    if rest_type:
        rest_msg   = apply_rest(session_id, rest_type)
        user_input = user_input + "\n[Systeme: " + rest_msg + "]"

    # P2a — détection des monstres dans les derniers messages du DM
    # On analyse les 2 derniers messages assistant pour capturer les combats
    # démarrés dans le tour précédent (le DM a pu annoncer un monstre avant)
    recent_dm       = [m["content"] for m in sess.conversations[-4:] if m["role"] == "assistant"]
    combat_monsters = []
    for dm_text in recent_dm:
        combat_monsters.extend(detect_monsters_in_text(dm_text))
    combat_monsters = list({m["key"]: m for m in combat_monsters}.values())

    messages    = build_messages(
        sess.conversations, user_input, sess.party, sess.active_character,
        session_id=session_id,
        combat_monsters=combat_monsters if combat_monsters else None,
    )
    reply, _, _ = await call_ollama(messages)

    # Sur erreur : ne pas polluer l'historique, stocker pour affichage + retry
    if reply.startswith("[ERREUR:"):
        error_detail = reply[len("[ERREUR:"):-1].strip()
        sess.last_error        = error_detail
        sess.last_failed_input = original_input
        logger.warning(f"Erreur Ollama stockée pour retry — session {session_id[:8]}: {error_detail}")
        return RedirectResponse(url="/", status_code=303)

    # Succès — effacer l'état d'erreur précédent
    sess.last_error        = None
    sess.last_failed_input = None

    # Must happen after the error check — strip_markdown would destroy the [ERREUR: prefix.
    reply = strip_markdown(reply)

    # P2a — si la réponse du DM introduit de nouveaux monstres via tag [COMBAT:]
    new_monsters = detect_monsters_in_text(reply)
    if new_monsters:
        logger.info(f"Nouveaux monstres détectés dans réponse DM: {[m['key'] for m in new_monsters]}")

    # Suppression du tag [COMBAT:...] de la réponse affichée (tag interne)
    reply_clean = re.sub(r'\[COMBAT:[^\]]*\]', '', reply).strip()

    sess.conversations.append({"role": "user",      "content": user_input})
    sess.conversations.append({"role": "assistant", "content": reply_clean})

    advance_active_character(session_id)

    logger.info(f"Tour ajouté — session {session_id[:8]}, {len(sess.conversations)} messages")

    # Compaction si l'historique dépasse le seuil
    await maybe_compact_history(session_id)

    logger.info(f"[PERF] /send total: {time.perf_counter() - t0_request:.1f}s — session {session_id[:8]}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/send_stream")
async def send_message_stream(request: Request, user_input: str = Form(...)):
    """
    Variante streaming de /send : retourne les tokens Ollama en SSE au fur et à mesure.
    Le JS intercepte le submit du formulaire et appelle cet endpoint.
    L'état de session est mis à jour côté serveur à la fin du stream.
    En cas d'erreur, renvoie un événement SSE {"error": "..."} sans polluer l'historique.
    """
    t0_request = time.perf_counter()

    if len(user_input) > MAX_USER_INPUT_LEN:
        user_input = user_input[:MAX_USER_INPUT_LEN]

    session_id     = get_or_create_session_id(request)
    sess           = get_session(session_id)
    original_input = user_input

    detected_party = extract_party_from_input(user_input)
    if detected_party:
        sess.party            = detected_party
        sess.active_character = 0
        init_spell_slots_for_party(session_id)
        logger.info(f"Groupe chargé (stream): {[char_name(c) for c in detected_party]} — {session_id[:8]}")

    rest_type = detect_rest_command(user_input)
    if rest_type:
        rest_msg   = apply_rest(session_id, rest_type)
        user_input = user_input + "\n[Systeme: " + rest_msg + "]"

    recent_dm       = [m["content"] for m in sess.conversations[-4:] if m["role"] == "assistant"]
    combat_monsters = []
    for dm_text in recent_dm:
        combat_monsters.extend(detect_monsters_in_text(dm_text))
    combat_monsters = list({m["key"]: m for m in combat_monsters}.values())

    messages = build_messages(
        sess.conversations, user_input, sess.party, sess.active_character,
        session_id=session_id,
        combat_monsters=combat_monsters if combat_monsters else None,
    )

    t_ready = time.perf_counter() - t0_request
    logger.info(f"[PERF] /send_stream contexte prêt: {t_ready:.3f}s — session {session_id[:8]}")

    async def event_stream() -> AsyncGenerator[str, None]:
        accumulated: list[str] = []
        first_token_logged = False
        t0_stream = time.perf_counter()
        try:
            async with _ollama_client.stream(
                "POST", "/api/chat",
                json={"model": OLLAMA_MODEL, "messages": messages, "stream": True,
                      "keep_alive": OLLAMA_KEEP_ALIVE},
            ) as response:
                response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    if not raw_line:
                        continue
                    try:
                        chunk = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        if not first_token_logged:
                            ttft = time.perf_counter() - t0_stream
                            logger.info(f"[PERF] TTFT: {ttft:.2f}s — session {session_id[:8]}")
                            first_token_logged = True
                        accumulated.append(token)
                        yield f"data: {json.dumps({'token': token})}\n\n"
                    if chunk.get("done"):
                        prompt_t    = chunk.get("prompt_eval_count", 0)
                        response_t  = chunk.get("eval_count", 0)
                        elapsed     = chunk.get("total_duration",        0) / 1e9
                        load_s      = chunk.get("load_duration",         0) / 1e9
                        prefill_s   = chunk.get("prompt_eval_duration",  0) / 1e9
                        gen_s       = chunk.get("eval_duration",         0) / 1e9
                        prefill_tps = prompt_t   / prefill_s if prefill_s > 0 else 0
                        gen_tps     = response_t / gen_s     if gen_s     > 0 else 0
                        total_wall  = time.perf_counter() - t0_request
                        logger.info(
                            f"[PERF] Ollama stream: total={elapsed:.1f}s | "
                            f"chargement={load_s:.1f}s | "
                            f"analyse={prefill_s:.1f}s ({prefill_tps:.0f} t/s, {prompt_t}t) | "
                            f"réponse={gen_s:.1f}s ({gen_tps:.0f} t/s, {response_t}t)"
                        )
                        if load_s > 5.0:
                            logger.warning(
                                f"[PERF] Modèle non en VRAM au moment de la requête "
                                f"(chargement={load_s:.1f}s) — vérifier préchauffage"
                            )
                        logger.info(f"[PERF] /send_stream total mur: {total_wall:.1f}s — session {session_id[:8]}")
                        complete   = strip_markdown("".join(accumulated))
                        reply_clean = re.sub(r'\[COMBAT:[^\]]*\]', '', complete).strip()
                        sess.conversations.append({"role": "user",      "content": user_input})
                        sess.conversations.append({"role": "assistant", "content": reply_clean})
                        sess.last_error        = None
                        sess.last_failed_input = None
                        advance_active_character(session_id)
                        logger.info(f"Tour ajouté (stream) — session {session_id[:8]}, {len(sess.conversations)} msgs")
                        asyncio.create_task(maybe_compact_history(session_id))
                        yield f"data: {json.dumps({'done': True, 'active_idx': sess.active_character})}\n\n"
        except httpx.ConnectError:
            err = "Ollama non démarré — lancez 'ollama serve'"
            sess.last_error = err
            sess.last_failed_input = original_input
            logger.error(f"Ollama stream ConnectError — {session_id[:8]}")
            yield f"data: {json.dumps({'error': err})}\n\n"
        except httpx.TimeoutException:
            err = "Délai dépassé — Ollama trop lent ou contexte trop long"
            sess.last_error = err
            sess.last_failed_input = original_input
            logger.error(f"Ollama stream timeout — {session_id[:8]}")
            yield f"data: {json.dumps({'error': err})}\n\n"
        except Exception as e:
            err = f"{type(e).__name__} — {str(e) or '(aucun détail)'}"
            sess.last_error = err
            sess.last_failed_input = original_input
            logger.error(f"Ollama stream erreur ({session_id[:8]}): {err}")
            yield f"data: {json.dumps({'error': err})}\n\n"

    resp = StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    if "session_id" not in request.cookies:
        resp.set_cookie("session_id", session_id, httponly=True)
    return resp


@app.get("/party/active")
async def get_active_character(request: Request):
    """Retourne le personnage actif courant (utilisé par le JS au chargement)."""
    session_id = get_or_create_session_id(request)
    sess       = SESSIONS.get(session_id)
    if not sess or not sess.party or sess.active_character is None:
        return JSONResponse({"active_idx": None, "name": None})
    return JSONResponse({
        "active_idx": sess.active_character,
        "name":       char_name(sess.party[sess.active_character]),
    })


@app.post("/party/active")
async def set_active_character(request: Request):
    """
    Force manuellement le personnage actif (clic sur une carte dans le panneau).
    Body JSON : {"index": N}
    """
    session_id = get_or_create_session_id(request)
    sess       = SESSIONS.get(session_id)
    if not sess:
        return JSONResponse({"ok": False}, status_code=400)
    try:
        data = await request.json()
        idx  = int(data["index"])
        if 0 <= idx < len(sess.party):
            sess.active_character = idx
            name = char_name(sess.party[idx])
            logger.info(f"Personnage actif forcé: {name} — session {session_id[:8]}")
            return JSONResponse({"ok": True, "active_idx": idx, "name": name})
    except Exception as e:
        logger.error(f"Erreur set active: {e}")
    return JSONResponse({"ok": False}, status_code=400)


@app.post("/party/hp")
async def update_hp(request: Request):
    """Mise à jour AJAX des HP d'un personnage. Body : {"index": N, "hp": V}"""
    session_id = get_or_create_session_id(request)
    sess       = SESSIONS.get(session_id)
    if not sess:
        return JSONResponse({"ok": False}, status_code=400)
    try:
        data = await request.json()
        idx  = int(data["index"])
        hp   = int(data["hp"])
        if 0 <= idx < len(sess.party):
            sess.party[idx]["HP"] = hp
            logger.info(f"HP mis à jour: {char_name(sess.party[idx])} → {hp}")
            return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Erreur update HP: {e}")
    return JSONResponse({"ok": False}, status_code=400)


@app.post("/spells/use")
async def use_spell_slot(request: Request):
    """
    Marque un emplacement de sort comme utilisé.
    Body JSON : {"char_name": "Aria", "slot_level": 2, "delta": 1}
    delta = +1 (utiliser) ou -1 (récupérer manuellement)
    """
    session_id = get_or_create_session_id(request)
    sess       = SESSIONS.get(session_id)
    if not sess:
        return JSONResponse({"ok": False, "error": "session not found"}, status_code=400)
    try:
        data     = await request.json()
        cname    = data["char_name"]
        slot_lvl = int(data["slot_level"]) - 1   # 0-indexed
        delta    = int(data.get("delta", 1))

        char = next((c for c in sess.party if char_name(c) == cname), None)
        if not char:
            return JSONResponse({"ok": False, "error": "char not found"}, status_code=400)
        max_slots = char.get("_slots_max", [0] * 9)
        mx        = max_slots[slot_lvl] if slot_lvl < 9 else 0

        current              = sess.spell_slots_used.get(cname, [0] * 9)
        new_used             = max(0, min(mx, current[slot_lvl] + delta))
        current[slot_lvl]    = new_used
        sess.spell_slots_used[cname] = current

        logger.info(f"Slot utilisé: {cname} niv{slot_lvl+1} → {new_used}/{mx}")
        return JSONResponse({"ok": True, "used": new_used, "max": mx, "available": mx - new_used})
    except Exception as e:
        logger.error(f"Erreur use_spell_slot: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/reset", response_class=RedirectResponse)
async def reset_conversation(request: Request):
    session_id = get_or_create_session_id(request)
    SESSIONS.pop(session_id, None)
    logger.info(f"Session reset: {session_id[:8]}")
    return RedirectResponse(url="/", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES DE TEST / DEBUG
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/party")
async def load_party_direct(request: Request):
    """
    Charge un groupe directement depuis un corps JSON — sans appel Ollama.
    Accepte un tableau ou un objet unique (normalisé via Character).
    Utile pour les tests automatisés et le débogage.
    """
    session_id = get_or_create_session_id(request)
    sess = get_session(session_id)
    try:
        data = await request.json()
        chars = data if isinstance(data, list) else [data]
        if not chars:
            return JSONResponse({"ok": False, "error": "liste vide"}, status_code=400)
        sess.party            = [_normalise_char(c) for c in chars]
        sess.active_character = 0
        init_spell_slots_for_party(session_id)
        names = [char_name(c) for c in sess.party]
        logger.info(f"Groupe chargé via /party: {names} — session {session_id[:8]}")
        response = JSONResponse({"ok": True, "party": names, "count": len(names)})
        response.set_cookie("session_id", session_id, httponly=True)
        return response
    except Exception as e:
        logger.error(f"Erreur /party: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/debug/session")
async def debug_session(request: Request):
    """
    Retourne l'état complet de la session en JSON.
    Nécessite DEBUG=1 dans l'environnement — retourne 403 sinon.
    """
    if not os.getenv("DEBUG"):
        return JSONResponse({"error": "debug non activé (DEBUG=1 requis)"}, status_code=403)
    session_id = get_or_create_session_id(request)
    sess = SESSIONS.get(session_id)
    if not sess:
        return JSONResponse({"error": "session introuvable"}, status_code=404)
    return JSONResponse({
        "session_id":         session_id,
        "party_names":        [char_name(c) for c in sess.party],
        "active_character":   sess.active_character,
        "conversation_count": len(sess.conversations),
        "spell_slots_used":   sess.spell_slots_used,
    })


@app.post("/load-session")
async def load_session(request: Request):
    """
    Restaure un état de campagne complet depuis un fichier de sauvegarde JSON.

    Structure attendue (tous les champs sauf party sont optionnels) :
      { "party": {...} ou [{...}],
        "recent_session_summary": "...",
        "world_state": {...},
        "npcs": [...],
        "factions": [...],
        "active_quests": [...],
        "open_threads": [...] }

    Injecte deux messages système en tête de l'historique :
      1. [CONTEXTE DE CAMPAGNE]  — monde, PNJs, factions, quêtes
      2. [RÉSUMÉ DE CAMPAGNE]    — résumé narratif de la session précédente
    """
    session_id = get_or_create_session_id(request)
    sess = get_session(session_id)
    try:
        data = await request.json()

        # ── Chargement du groupe ───────────────────────────────────────────
        raw_party = data.get("party", [])
        if isinstance(raw_party, dict):
            raw_party = [raw_party]
        if not raw_party:
            return JSONResponse({"ok": False, "error": "party manquant"}, status_code=400)
        sess.party            = [_normalise_char(c) for c in raw_party]
        sess.active_character = 0
        init_spell_slots_for_party(session_id)

        context_msgs = 0

        # ── Message 1 : contexte monde / PNJs / factions / quêtes ─────────
        ctx_lines = ["[CONTEXTE DE CAMPAGNE]"]
        world = data.get("world_state", {})
        if world:
            parts = []
            if world.get("city"):    parts.append(f"Ville: {world['city']}")
            if world.get("weather"): parts.append(f"Météo: {world['weather']}")
            if world.get("time"):    parts.append(f"Heure: {world['time']}")
            if parts:
                ctx_lines.append(" | ".join(parts))

        npcs = data.get("npcs", [])
        if npcs:
            ctx_lines.append("\nPNJs CONNUS:")
            for npc in npcs:
                line = f"  {npc.get('name','?')} — {npc.get('role','')}"
                if npc.get("status"):   line += f" ({npc['status']})"
                if npc.get("relation_to_player"): line += f" | Relation: {npc['relation_to_player']}"
                ctx_lines.append(line)

        factions = data.get("factions", [])
        if factions:
            ctx_lines.append("\nFACTIONS:")
            for f in factions:
                ctx_lines.append(f"  {f.get('name','?')} — {f.get('status','')}")

        quests = data.get("active_quests", [])
        if quests:
            ctx_lines.append("\nQUÊTES ACTIVES:")
            for q in quests:
                ctx_lines.append(f"  - {q.get('title','?')} ({q.get('status','')})")

        threads = data.get("open_threads", [])
        if threads:
            ctx_lines.append("\nFILS OUVERTS:")
            for t in threads:
                ctx_lines.append(f"  - {t}")

        if len(ctx_lines) > 1:
            sess.conversations.append({"role": "system", "content": "\n".join(ctx_lines)})
            context_msgs += 1

        # ── Message 2 : résumé narratif ────────────────────────────────────
        summary = data.get("recent_session_summary", "").strip()
        if summary:
            sess.conversations.append({
                "role":    "system",
                "content": f"[RÉSUMÉ DE CAMPAGNE]\n{summary}",
            })
            context_msgs += 1

        names = [char_name(c) for c in sess.party]
        logger.info(
            f"Session chargée via /load-session: {names}, {context_msgs} msg(s) contexte "
            f"— session {session_id[:8]}"
        )
        response = JSONResponse({"ok": True, "party": names, "context_msgs": context_msgs})
        response.set_cookie("session_id", session_id, httponly=True)
        return response
    except Exception as e:
        logger.error(f"Erreur /load-session: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/health")
async def health_check():
    ollama_status = "DOWN"
    try:
        resp = await _ollama_client.get("/api/tags", timeout=5.0)
        ollama_status = "OK" if resp.status_code == 200 else "KO"
    except Exception:
        pass
    return {
        "code_version":     CODE_VERSION,
        "ollama_model":     OLLAMA_MODEL,
        "ollama_status":    ollama_status,
        "piper_bin_ok":     PIPER_BIN.exists(),
        "piper_model_ok":   (VOICES_DIR / DEFAULT_VOICE).exists(),
        "voices_available": sorted(p.name for p in VOICES_DIR.glob("*.onnx")),
        "active_sessions":  len(SESSIONS),
        "active_parties":   sum(1 for s in SESSIONS.values() if s.party),
        "monsters_loaded":  len(MONSTERS_DB),
        "spells_loaded":    bool(SPELLS_DB),
    }


if __name__ == "__main__":
    import uvicorn
    logger.info(f"D&D DM v{CODE_VERSION} — modèle: {OLLAMA_MODEL}")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
