# RAG Legal Assistant

A Flask **RAG** (Retrieval-Augmented Generation) application for analysing the
correspondence of a French/Italian succession (estate) case. It parses emails
(`.eml`) and their attachments, indexes them in a vector store, builds a document
graph, and exposes several conversational agents to query, synthesise and devise a
legal strategy over the case file.

> ⚠️ **Confidential data.** Emails, attachments, indexes and databases are **not**
> versioned (see [`.gitignore`](.gitignore)). This repository contains **code only**.
> The `.eml` files go locally into `data/` (see below).

## Features

- **EML parsing**, multi-encoding (Windows-1252/quoted-printable, UTF-8/base64,
  `multipart`), attachment extraction (PDF, DOCX, RTF) and OCR of scanned PDFs via
  Claude Vision.
- **Vector indexing** in ChromaDB, with embeddings provided by the chosen provider
  (no local `onnxruntime`).
- **Document graph** (email threads, neighbourhood) to enrich retrieval.
- **Three agents** (each with its own web panel):
  - **Chat** — one-off Q&A (analyse → plan → execute → synthesise).
  - **Synthesis** — sourced narrative over the whole case file (structured SQL
    briefing + full-text search), every claim cited `[date — correspondent — subject]`.
  - **Advisor** — strategist lawyer: reacts to a scenario/argument from the opposing
    party, delegates to the other two agents via *function-calling*, and lists useful
    supporting documents. Supports *reasoning models* (effort low/medium/high).
- **Visualisations**: timeline and network graph (Plotly / PyVis).
- **RAG evaluation** (question generation + LLM judge).
- **Multi-provider**: OpenAI, Anthropic (chat), Mistral, vLLM (OpenAI-compatible).

## Architecture

> 📐 For details on the **RAG & agentic design** (indexing pipeline, vector search +
> graph expansion, the 3 agents and the *function-calling* orchestration), see
> [ARCHITECTURE.md](ARCHITECTURE.md).

```
app.py            Flask server: page routes + API (SSE for streaming)
config.py         Central configuration (reads .env)
indexer.py        Indexing pipeline (parse → enrich → chunk → embed → Chroma)
migrate.py        Database export/import (Chroma + SQLite + files) — portability
state.py          Document store (SQLite)
graph_state.py    Document graph

parsing/          EML parsing, attachments, OCR, classification, threading
rag/              RAG core: providers, retrieve, chunker, index, intent, planner
                  + the 3 agents: chat.py, synthesis_chat.py, advisor_chat.py
viz/              Timeline, network graph, synthesis data
eval/             Evaluation question generation + LLM judge

templates/        Jinja views (base, chat, synthesis, advisor, graph, timeline, …)
static/           JS/CSS for the panels
```

Web pages (once the app is running): `/` (timeline), `/graph`, `/synthesis`,
`/advisor`, `/chat`, `/admin` (indexing + migration), `/eval`,
`/settings` (providers & keys).

## Requirements

- Python 3.11+
- Docker (for the **ChromaDB** service)
- At least one LLM provider API key (OpenAI / Anthropic / Mistral) or a vLLM endpoint

## Installation

Two paths. **A. Docker** is recommended for installing on a new machine;
**B. venv** for development.

### A. Docker (recommended, new machine)

Prerequisite: **Docker Desktop**. The app and ChromaDB are launched via Docker
Compose; the app image is built locally on first startup.

```bash
# 1. Get the code (or copy the project folder)
git clone <repo> avocat && cd avocat

# 2. Minimal configuration
cp .env.example .env        # API keys can also be entered later via /settings

# 3. Start (local build of the app + chromadb)
docker compose -f docker-compose.prod.yml up -d --build
# → http://localhost:5000
```

Then: open **`/settings`** to enter providers, models and API keys (applied live, no
restart), and **restore the data** via `/admin` (*Import* button) or
`python migrate.py import` — see [Portability](#portability--migration).

### B. Local dev (venv)

```bash
# 1. Python environment
python -m venv .venv
# Windows: .venv\Scripts\activate   —   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. Configuration (or everything via /settings after launch)
cp .env.example .env

# 3. ChromaDB (Docker), indexing, then the app
docker compose up -d chromadb
python indexer.py           # or via the /admin interface
python app.py               # → http://localhost:5000
```

### Configuration

Three ways (in increasing order of precedence): defaults → `.env` →
**`/settings` menu** (written to `data/settings.json`, never committed). Main
variables (see [`.env.example`](.env.example)):

| Variable | Role |
|---|---|
| `EMBED_PROVIDER` / `CHAT_PROVIDER` | `openai` \| `anthropic`* \| `mistral` \| `vllm` (*Anthropic = chat only) |
| `EMBED_MODEL` / `CHAT_MODEL` | models per provider (e.g. `text-embedding-3-large` / `gpt-4o`) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `MISTRAL_API_KEY` | keys depending on providers |
| `VLLM_BASE_URL` / `VLLM_API_KEY` | OpenAI-compatible vLLM endpoint (optional) |
| `CHROMA_HOST` / `CHROMA_PORT` | ChromaDB service (`localhost:8000` in dev, `chromadb:8000` in compose) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | chunking at indexing time |

> Providers, models and keys are managed most easily from **`/settings`**.
> Infrastructure (Chroma, Flask, chunking) stays driven by `.env` / `docker-compose`.

## Data

Place the source emails in `data/`, organised by sender/party, e.g.:

```
data/
  mails de dubois/      001 ….eml, 002 ….eml, …
  mails de leroy/
  mails de lefebvre/
  …
```

Extracted attachments, OCR text, the SQLite database and the index are **generated**
by indexing and stay local (ignored by git).

## Portability & migration

To move to another machine **without paying for indexing again** (ChromaDB
embeddings) or for LLM enrichment, export/import the databases with
[`migrate.py`](migrate.py). The `.zip` archive contains the **already-computed
ChromaDB vectors**, the **SQLite** database (registry, graph, enrichment,
evaluations), the **attachments**, the **OCR text** and (optionally) the **source
emails**. **API keys are never included.**

```bash
# Old machine — create the archive (or the “Export” button in /admin)
python migrate.py export                 # → backups/avocat-export-<date>.zip
python migrate.py export --no-sources    # lightweight archive (without the source .eml)

# New machine — AFTER configuring the SAME embedding model via /settings
python migrate.py import backups/avocat-export-<date>.zip --yes
```

On import: the current SQLite database is backed up (`app.sqlite.pre-import-…`), then
the Chroma collections are recreated and **the vectors re-injected as-is** (zero
embedding calls). ⚠️ Set the **same `EMBED_PROVIDER`/`EMBED_MODEL`** as the one
listed in the archive's `manifest.json`: vectors are tied to the model that produced
them.

Export/import is also available in the **`/admin`** interface (*Migration / Backup*
panel).

## Publishing the Docker image (optional)

To push the image to your own registry (e.g. Docker Hub), set your namespace —
requires `docker login`:

```bash
# Linux/macOS
IMAGE=<your-namespace>/avocat-app ./scripts/docker-push.sh        # TAG=v1.0 to version
# Windows
.\scripts\docker-push.ps1 -Image <your-namespace>/avocat-app     # -Tag v1.0, -MultiArch for ARM
```

`docker-compose.yml` and `docker-compose.prod.yml` build the image locally
(`build: .`). To deploy a pre-published image, replace the `build: .` block of the
`app` service with `image: <your-namespace>/avocat-app:latest`.

## Notes

- ChromaDB is used as a **thin client** (`chromadb-client`): embeddings are provided
  by `rag/providers.py`, which avoids `onnxruntime` issues on Windows.
- The **Advisor** agent enables reasoning via `reasoning_effort` (mapped to
  `reasoning_effort` on OpenAI, and adaptive `thinking` + `effort` on Anthropic);
  ignored by providers that don't support it.
