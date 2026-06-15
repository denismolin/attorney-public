# Assistant juridique RAG — Succession

Application Flask de **RAG** (Retrieval-Augmented Generation) pour analyser la
correspondance d'une succession franco-italienne. Elle parse les emails (`.eml`)
et leurs pièces jointes, les indexe dans une base vectorielle, construit un graphe
documentaire, et expose plusieurs agents conversationnels pour interroger, synthétiser
et élaborer une stratégie juridique sur le dossier.

> ⚠️ **Données confidentielles.** Les emails, pièces jointes, index et bases de
> données ne sont **pas** versionnés (cf. [`.gitignore`](.gitignore)). Ce dépôt ne
> contient que le **code**. Les `.eml` se placent en local dans `data/` (voir plus bas).

## Fonctionnalités

- **Parsing EML** multi-encodage (Windows-1252/quoted-printable, UTF-8/base64,
  `multipart`), extraction des pièces jointes (PDF, DOCX, RTF) et OCR des PDF scannés
  via Claude Vision.
- **Indexation vectorielle** dans ChromaDB, embeddings fournis par le provider choisi
  (pas d'`onnxruntime` local).
- **Graphe documentaire** (fils de discussion, voisinage) pour enrichir la recherche.
- **Trois agents** (chacun avec son panneau web) :
  - **Chat** — Q&A ponctuelle (analyse → plan → exécution → synthèse).
  - **Synthèse** — narratif sourcé sur l'ensemble du dossier (briefing SQL structuré
    + recherche plein texte), chaque affirmation citée `[date — correspondant — sujet]`.
  - **Conseil** — avocat stratège : réagit à un scénario/dire de la partie adverse,
    délègue aux deux autres agents via *function-calling*, et liste les pièces
    complémentaires utiles. Supporte les *reasoning models* (effort low/medium/high).
- **Visualisations** : frise chronologique et graphe réseau (Plotly / PyVis).
- **Évaluation** RAG (génération de questions + juge LLM).
- **Multi-provider** : OpenAI, Anthropic (chat), Mistral, vLLM (OpenAI-compatible).

## Architecture

```
app.py            Serveur Flask : routes pages + API (SSE pour le streaming)
config.py         Configuration centrale (lecture .env)
indexer.py        Pipeline d'indexation (parse → enrich → chunk → embed → Chroma)
migrate.py        Export/import des bases (Chroma + SQLite + fichiers) — portabilité
state.py          Base documentaire (SQLite)
graph_state.py    Graphe documentaire

parsing/          Parsing EML, pièces jointes, OCR, classification, threading
rag/              Cœur RAG : providers, retrieve, chunker, index, intent, planner
                  + les 3 agents : chat.py, synthesis_chat.py, advisor_chat.py
viz/              Frise, graphe réseau, données de synthèse
eval/             Génération de questions d'évaluation + juge LLM

templates/        Vues Jinja (base, chat, synthesis, advisor, graph, timeline, …)
static/           JS/CSS des panneaux
```

Pages web (une fois l'app lancée) : `/` (frise), `/graph`, `/synthesis`,
`/advisor` (conseil), `/chat`, `/admin` (indexation + migration), `/eval`,
`/settings` (providers & clés).

## Prérequis

- Python 3.11+
- Docker (pour le service **ChromaDB**)
- Au moins une clé API d'un provider LLM (OpenAI / Anthropic / Mistral) ou un endpoint vLLM

## Installation

Deux parcours. **A. Docker** est recommandé pour installer sur un nouveau poste ;
**B. venv** pour développer.

### A. Docker (recommandé, nouveau poste)

Prérequis : **Docker Desktop**. L'image de l'app est publiée sur Docker Hub
(`denismolin/avocat-app`), donc aucun build local n'est nécessaire.

```bash
# 1. Récupérer le code (ou copier le dossier du projet)
git clone <repo> avocat && cd avocat

# 2. Configuration minimale
cp .env.example .env        # les clés API peuvent aussi être saisies ensuite via /settings

# 3. Démarrer (pull de l'image publique + chromadb)
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
# → http://localhost:5000
```

Ensuite : ouvrir **`/settings`** pour renseigner providers, modèles et clés API
(appliqués à chaud, sans redémarrage), puis **restaurer les données** via `/admin`
(bouton *Importer*) ou `python migrate.py import` — cf. [Portabilité](#portabilité--migration).

### B. Dev local (venv)

```bash
# 1. Environnement Python
python -m venv .venv
# Windows : .venv\Scripts\activate   —   macOS/Linux : source .venv/bin/activate
pip install -r requirements.txt

# 2. Configuration (ou tout via /settings après lancement)
cp .env.example .env

# 3. ChromaDB (Docker), indexation, puis app
docker compose up -d chromadb
python indexer.py           # ou via l'interface /admin
python app.py               # → http://localhost:5000
```

### Configuration

Trois façons (par ordre de priorité croissante) : valeurs par défaut → `.env` →
**menu `/settings`** (écrit dans `data/settings.json`, jamais commité). Variables
principales (cf. [`.env.example`](.env.example)) :

| Variable | Rôle |
|---|---|
| `EMBED_PROVIDER` / `CHAT_PROVIDER` | `openai` \| `anthropic`* \| `mistral` \| `vllm` (*Anthropic = chat uniquement) |
| `EMBED_MODEL` / `CHAT_MODEL` | modèles par provider (ex. `text-embedding-3-large` / `gpt-4o`) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `MISTRAL_API_KEY` | clés selon providers |
| `VLLM_BASE_URL` / `VLLM_API_KEY` | endpoint vLLM OpenAI-compatible (optionnel) |
| `CHROMA_HOST` / `CHROMA_PORT` | service ChromaDB (`localhost:8000` en dev, `chromadb:8000` en compose) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | découpage à l'indexation |

> Providers, modèles et clés se gèrent au plus simple depuis **`/settings`**.
> L'infra (Chroma, Flask, chunking) reste pilotée par le `.env` / `docker-compose`.

## Données

Place les emails source dans `data/`, organisés par expéditeur/partie, p. ex. :

```
data/
  mails de dubois/      001 ….eml, 002 ….eml, …
  mails de leroy/
  mails de lefebvre/
  …
```

Les pièces jointes extraites, le texte OCR, la base SQLite et l'index sont **générés**
par l'indexation et restent locaux (ignorés par git).

## Portabilité & migration

Pour changer de poste **sans repayer l'indexation** (embeddings ChromaDB) ni
l'enrichissement LLM, on exporte/importe les bases avec [`migrate.py`](migrate.py).
L'archive `.zip` contient les **vecteurs ChromaDB déjà calculés**, la base **SQLite**
(registre, graphe, enrichissement, évaluations), les **pièces jointes**, le **texte OCR**
et (option) les **emails sources**. Les **clés API ne sont jamais incluses**.

```bash
# Ancien poste — créer l'archive (ou bouton « Exporter » dans /admin)
python migrate.py export                 # → backups/avocat-export-<date>.zip
python migrate.py export --no-sources    # archive légère (sans les .eml sources)

# Nouveau poste — APRÈS avoir configuré le MÊME modèle d'embedding via /settings
python migrate.py import backups/avocat-export-<date>.zip --yes
```

À l'import : la base SQLite courante est sauvegardée (`app.sqlite.pre-import-…`), puis
les collections Chroma sont recréées et **les vecteurs réinjectés tels quels** (zéro
appel d'embedding). ⚠️ Configurez le **même `EMBED_PROVIDER`/`EMBED_MODEL`** que celui
indiqué dans le `manifest.json` de l'archive : les vecteurs sont liés au modèle qui les
a produits.

L'export/import est aussi disponible dans l'interface **`/admin`** (panneau
*Migration / Sauvegarde*).

## Publier l'image Docker (mainteneur)

```bash
# Build + push sur Docker Hub (namespace denismolin) — nécessite `docker login`
./scripts/docker-push.sh                 # Linux/macOS  (TAG=v1.0 pour versionner)
.\scripts\docker-push.ps1                # Windows      (-Tag v1.0, -MultiArch si ARM)
```

`docker-compose.yml` (build local, deux services `app` + `chromadb`) sert au dev ;
`docker-compose.prod.yml` (pull de l'image publique) sert au déploiement.

## Notes

- ChromaDB est utilisé en **client léger** (`chromadb-client`) : les embeddings sont
  fournis par `rag/providers.py`, ce qui évite les soucis d'`onnxruntime` sous Windows.
- L'agent **Conseil** active le raisonnement via `reasoning_effort` (mappé vers
  `reasoning_effort` côté OpenAI, et `thinking` adaptatif + `effort` côté Anthropic) ;
  ignoré par les providers qui ne le supportent pas.
