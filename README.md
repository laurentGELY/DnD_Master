# D&D 5e AI Dungeon Master

Application web locale pour jouer à Donjons & Dragons 5e avec un Maître du Donjon propulsé par IA, avec synthèse vocale française.

---

## Fonctionnalités

- **IA Dungeon Master** : Llama 3.1 8B via Ollama, guidé par un system prompt D&D 5e complet
- **Synthèse vocale** : voix française naturelle via Piper TTS, voix sélectionnable dans l'interface (défaut : fr_FR-gilles-low)
- **Interface sobre** : thème sombre, design minimaliste, entièrement dans le navigateur
- **Sessions en RAM** : pas de base de données, zéro configuration
- **Prompt versionnable** : le comportement du DM se modifie en éditant un simple fichier texte, sans redémarrer le serveur

---

## Architecture

```
Browser (Firefox/Chromium)
    │
    │  GET /          → affiche la page (Jinja2 + HTML)
    │  POST /send     → envoie un message, reçoit la réponse du DM
    │  POST /reset    → efface la session courante
    │  GET /tts?text=&voice= → reçoit le fichier WAV de la voix
    │  GET /voices    → liste les modèles .onnx disponibles
    │  GET /health    → diagnostic JSON
    ▼
FastAPI (main.py) — écoute sur 127.0.0.1:8000
    │
    ├── Jinja2 Templates (templates/index.html)
    ├── Sessions RAM  (dict Python, perdu au redémarrage)
    ├── System Prompt (prompts/system_prompt.txt, relu à chaque requête)
    │
    ├── Ollama API ──► http://localhost:11434/api/chat
    │       Modèle : dnd-dm-8b (Llama 3.1 8B, num_ctx=8192)
    │
    └── Piper TTS ──► bin/piper (binaire AMD64 bundlé)
            Libs  : bin/piper_amd64/ (onnxruntime, phonemize, espeak-ng)
            Voix  : voices/*.onnx (sélectionnable dans l'UI, défaut : fr_FR-gilles-low)
```

### Pattern PRG (Post/Redirect/Get)

Toutes les actions de formulaire suivent ce pattern :

```
POST /send ──► traitement ──► 303 Redirect ──► GET /
```

Cela évite la re-soumission accidentelle du formulaire si l'utilisateur appuie sur F5 après avoir envoyé un message.

---

## Structure des fichiers

```
dnd-dm-app/
├── main.py                          # Backend FastAPI (serveur + logique)
├── requirements.txt                 # Dépendances Python
│
├── prompts/
│   └── system_prompt.txt            # Comportement du DM (éditable à chaud)
│
├── templates/
│   └── index.html                   # Interface web (Jinja2)
│
├── static/                          # Assets statiques (dossier vide par défaut)
│
├── bin/
│   ├── piper                        # Binaire Piper TTS (AMD64, 2.8 Mo)
│   └── piper_amd64/                 # Bibliothèques bundlées
│       ├── libpiper_phonemize.so.1
│       ├── libonnxruntime.so.1.14.1
│       ├── libespeak-ng.so.1.1.51
│       └── espeak-ng-data/          # Données phonétiques
│
└── voices/
    ├── fr_FR-gilles-low.onnx        # Voix par défaut (masculine, qualité low)
    ├── fr_FR-gilles-low.onnx.json
    ├── fr_FR-siwis-medium.onnx      # Voix féminine, qualité medium
    ├── fr_FR-siwis-medium.onnx.json
    ├── fr_FR-tom-medium.onnx        # Voix masculine, qualité medium
    ├── fr_FR-tom-medium.onnx.json
    ├── fr_FR-mls-medium.onnx
    ├── fr_FR-mls-medium.onnx.json
    ├── fr_FR-upmc-medium.onnx
    └── fr_FR-upmc-medium.onnx.json  # Tout fichier .onnx ajouté ici est détecté automatiquement
```

---

## Prérequis

| Composant | Version testée | Notes |
|-----------|---------------|-------|
| Ubuntu    | 24.04 LTS     | x86_64 |
| Python    | 3.12.x        | |
| Ollama    | récent        | Doit tourner en arrière-plan |
| Firefox / Chromium | — | Tout navigateur moderne |

---

## Installation

### 1. Cloner le projet

```bash
git clone https://github.com/laurentGELY/DnD_Master.git
cd dnd-dm-app
```

### 2. Créer et activer le virtualenv

```bash
# Depuis le dossier parent de dnd-dm-app/ (un cran au-dessus)
python3 -m venv .venv
source .venv/bin/activate
```

> **Pourquoi un venv dans le dossier parent ?** Cette organisation permet de partager un même `.venv` entre plusieurs projets voisins. Le chemin d'activation depuis `dnd-dm-app/` est donc `../.venv/bin/activate`.

### 3. Installer les dépendances Python

```bash
pip install -r dnd-dm-app/requirements.txt
```

### 4. Vérifier Ollama

```bash
# Ollama doit être démarré (il tourne souvent en service systemd)
ollama serve &        # Si pas déjà actif
ollama list           # Vérifier que dnd-dm-8b est présent

# Si le modèle n'existe pas encore, le créer depuis Llama 3.1 :
# ollama pull llama3.1:8b
# Puis créer un Modelfile avec num_ctx 8192 et ollama create dnd-dm-8b
```

### 5. Vérifier Piper TTS

```bash
cd dnd-dm-app/
LD_LIBRARY_PATH=bin/piper_amd64 \
ESPEAK_DATA_PATH=bin/piper_amd64/espeak-ng-data \
bin/piper --model voices/fr_FR-gilles-low.onnx \
          --output_file /tmp/test.wav <<< "Bonjour aventurier" \
&& echo "✅ Piper OK" || echo "❌ Piper a échoué"
```

---

## Lancement

```bash
cd dnd-dm-app
source ../.venv/bin/activate
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Ouvrir dans le navigateur : **http://localhost:8000**

Vérifier l'état du système : **http://localhost:8000/health**

---

## Utilisation

### Démarrer une partie

1. Fournir vos personnages en JSON dans le champ de saisie :

```json
{
  "party": [
    {
      "name": "Thorin",
      "race": "Nain",
      "class": "Guerrier",
      "level": 1,
      "AC": 16,
      "HP": 12,
      "STR": 16, "DEX": 10, "CON": 14,
      "INT": 8,  "WIS": 11, "CHA": 9
    }
  ]
}
```

2. Le DM valide les personnages et propose trois quêtes pour débutants.
3. Choisir une quête et commencer l'aventure.

### Commandes spéciales

| Commande | Effet |
|----------|-------|
| `!save`  | Le DM génère un JSON de sauvegarde de la campagne |
| `!load`  | Charger une sauvegarde (coller le JSON) |
| `!recap` | Résumé de la situation actuelle |

### Bouton Voix

Cliquer sur **🎤 Voix** après un message du DM pour l'entendre lu à voix haute.
Le bouton affiche **⏸ Lecture…** pendant la lecture, puis revient à son état initial.

Le menu déroulant à gauche du bouton permet de choisir la voix parmi toutes celles disponibles dans `voices/`.
Le choix est mémorisé dans le navigateur (localStorage) et persisté entre les sessions.

---

## Configuration avancée

### Changer de modèle Ollama

```bash
export OLLAMA_MODEL=llama3.2:3b   # Modèle plus léger
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Ou modifier directement la valeur par défaut dans `main.py` :
```python
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mon-autre-modele")
```

### Ajouter une voix Piper

Télécharger un modèle depuis https://huggingface.co/rhasspy/piper-voices/tree/main,
placer le `.onnx` et le `.onnx.json` dans `voices/`.
La nouvelle voix apparaît automatiquement dans le menu déroulant de l'interface sans redémarrer le serveur.

### Modifier le comportement du DM

Éditer `prompts/system_prompt.txt` directement. Les changements sont pris en compte
au prochain message envoyé, sans redémarrer le serveur.

### Ajuster la fenêtre de contexte

```python
MAX_HISTORY_TURNS = 10   # Augmenter pour une mémoire plus longue
                          # (attention à la limite num_ctx du modèle)
```

---

## Dépendances Python

| Paquet | Version | Rôle |
|--------|---------|------|
| `fastapi` | 0.115.0 | Framework web async |
| `uvicorn[standard]` | 0.32.0 | Serveur ASGI (avec uvloop pour les perfs) |
| `jinja2` | 3.1.4 | Rendu des templates HTML |
| `httpx` | 0.27.0 | Client HTTP async pour appeler Ollama |
| `python-multipart` | 0.0.9 | Parsing des formulaires HTML (POST /send) |

---

## Dépannage

### Le serveur ne démarre pas

```
SyntaxError ou ImportError au lancement
```
→ Vérifier que le venv est activé : `which python` doit pointer vers `.venv/bin/python`

```
StaticFiles directory "static" not found
```
→ Créer le dossier manquant : `mkdir -p dnd-dm-app/static`

---

### Ollama ne répond pas

```
[ERREUR: Ollama non démarré — lancez 'ollama serve']
```

```bash
# Vérifier si Ollama tourne
curl http://localhost:11434/api/tags

# Démarrer si nécessaire
ollama serve
```

```
ERREUR: model "dnd-dm-8b" not found
```

```bash
ollama list   # Lister les modèles disponibles
# Utiliser le nom exact affiché, ou changer OLLAMA_MODEL
```

---

### Piper TTS ne fonctionne pas

**Erreur : `libpiper_phonemize.so.1: cannot open shared object file`**

→ Les libs bundlées ne sont pas trouvées. Vérifier que `bin/piper_amd64/` existe et contient les `.so`.

**Erreur : `No such file or directory: '/usr/share/espeak-ng-data/phontab'`**

→ `ESPEAK_DATA_PATH` n'est pas injecté. Ce problème est géré automatiquement par `main.py` via `piper_env`. Si l'erreur persiste, vérifier que `bin/piper_amd64/espeak-ng-data/` existe.

**Test manuel :**

```bash
cd dnd-dm-app/
LD_LIBRARY_PATH=bin/piper_amd64 \
ESPEAK_DATA_PATH=bin/piper_amd64/espeak-ng-data \
bin/piper --model voices/fr_FR-gilles-low.onnx \
          --output_file /tmp/test.wav <<< "Test"
aplay /tmp/test.wav   # Écouter le résultat
```

---

### Le bouton Voix ne produit pas de son

→ Ouvrir la console du navigateur (F12) et chercher les erreurs JS.
→ Vérifier `/health` : `piper_bin_ok` et `piper_model_ok` doivent être `true`.
→ Certains navigateurs bloquent la lecture audio sans interaction préalable de l'utilisateur — cliquer d'abord dans la page.

---

### Les sessions sont perdues après redémarrage

C'est le comportement attendu — les sessions sont en RAM. Pour persister les sessions,
une évolution possible serait de sérialiser `CONVERSATIONS` en JSON sur disque
dans un dossier `sessions/` à chaque modification.

---

## Évolutions possibles

| Évolution | Complexité | Notes |
|-----------|-----------|-------|
| Persistance des sessions sur disque | Faible | JSON dans `sessions/{uuid}.json` |
| Streaming de la réponse Ollama (SSE) | Moyenne | Remplacer le pattern PRG par WebSocket ou EventSource |
| ~~Plusieurs voix Piper sélectionnables~~ | ✅ Fait | Menu déroulant dans l'UI, `/voices` endpoint, `voice=` param sur `/tts` |
| Export de la conversation en PDF | Moyenne | WeasyPrint ou pandoc |
| Carte tactique simple (Theatre of the Mind) | Haute | Canvas HTML5 |

---

## Licence

MIT — Usage personnel.
# DnD_Master
