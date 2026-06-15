# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A Flask **RAG** application that parses, indexes and analyses the legal email
correspondence of a French/Italian succession dispute (DUPONT / MARTIN / DURAND).
It parses `.eml` emails and their attachments, indexes them in a vector store
(ChromaDB), builds a document graph, and exposes several conversational agents
(chat, synthesis, advisor) plus timeline/network visualisations and a RAG evaluation
panel. See [README.md](README.md) for the full feature/architecture overview.

> ⚠️ **Confidential data.** The emails, attachments, indexes and databases under
> `data/` are **not** versioned (cf. `.gitignore`). Only code is committed.

## Architecture (quick map)

- [app.py](app.py) — Flask server: page routes + JSON/SSE API.
- [config.py](config.py) — central config. Precedence: defaults → `.env` → `data/settings.json`
  (UI overrides). The shared singleton `cfg` is mutated **in place** by the `/settings`
  menu, so new requests pick up changes without a restart.
- [indexer.py](indexer.py) — indexing pipeline (parse → classify → extract → enrich →
  chunk → embed → upsert Chroma), runs in a background thread.
- [migrate.py](migrate.py) — export/import of all databases for portability (see below).
- `state.py` / `graph_state.py` / `eval_state.py` — **all share one SQLite file**
  `data/app.sqlite` (registry + index progress; knowledge graph + LLM enrichment;
  eval questions/runs). `data/index_state.db` is a stale, unused leftover.
- `rag/` — RAG core. [rag/providers.py](rag/providers.py) abstracts embedders/chat across
  OpenAI / Anthropic (chat only) / Mistral / vLLM; factories `get_chat()`/`get_embedder()`
  re-read `cfg.*` on every call. [rag/index.py](rag/index.py) is the Chroma client layer.
- `parsing/`, `viz/`, `eval/`, `templates/`, `static/` — parsing, visualisations,
  evaluation, Jinja views, JS/CSS.

### Persistence — what lives where (matters for backup/migration)
- **ChromaDB vectors** (the expensive-to-recompute embeddings) live in a **Docker
  named volume `chroma-data`**, *outside* `data/`. Accessed over HTTP (`HttpClient`).
- **`data/app.sqlite`** — registry, document graph, LLM enrichment, eval data.
- **`data/`** files — `attachments/` (extracted), `extracted_text/` (OCR),
  `uploads/`, and the source `mails de */` folders.

## Common commands

```bash
# Dev: start Chroma, index, run app
docker compose up -d chromadb
python indexer.py            # or trigger from /admin
python app.py                # http://localhost:5000

# Full stack (build local image + chromadb)
docker compose up --build

# Production / new machine (build local image + chromadb)
docker compose -f docker-compose.prod.yml up -d --build

# Migration (no re-indexing): export on old machine, import on new one
python migrate.py export [--no-sources]
python migrate.py import backups/avocat-export-<date>.zip --yes

# Publish the app image to your own registry — needs `docker login`
IMAGE=<your-namespace>/avocat-app ./scripts/docker-push.sh   # or .\scripts\docker-push.ps1 -Image ...
```

## Key conventions
- **Providers/keys** are configured via the `/settings` page (writes `data/settings.json`,
  applied live). Infra (Chroma host/port, Flask, chunking) stays in `.env`/compose.
- **Secrets are never committed and never put in a migration archive** (`.env`,
  `data/settings.json` are excluded by `migrate.py`).
- **Migration preserves embeddings as-is** — on import, set the SAME
  `EMBED_PROVIDER`/`EMBED_MODEL` as the archive `manifest.json`, since vectors are tied
  to the model that produced them.

## Data

`data/` holds `.eml` email files organised by sender/party (more senders exist on
disk than the table below — e.g. `mails de sophie/`, `mails de renard/`,
`mails de bernard/`):

| Folder | Count | Who |
|---|---|---|
| `mails de dubois/` | 116 | Maître Dubois — Pierre MARTIN's lawyer |
| `mails de leroy/` | 32 | Maître Leroy — opposing or third-party lawyer |
| `mails de lefebvre/` | 20 | Maître Lefebvre — notary (Notaire à Massy) |

Files are numbered `001 …`, `002 …`, etc., and the subject lines track the case progression (partage, assignation, PV de difficulté, etc.).

### EML format notes
- Encoding is mixed: some messages use `text/plain; charset="Windows-1252"` with `quoted-printable`, others use `text/plain; charset="utf-8"` with `base64`.
- Many messages are `multipart/alternative` (plain + HTML) or `multipart/mixed` (with attachments).
- Python's standard `email` library handles parsing; `quopri` and `base64` modules decode the bodies.

## Case context

- **Deceased**: Jean DUPONT, died 15 January 2024
- **Heirs**: Marie MARTIN and her sister Claire DUPONT (children); Madame DURAND (PACS partner since 10 April 2018)
- **Key disputes**: delivery of testamentary bequests (usufruct on two properties — Versailles + Italy), a debt acknowledgement (€98 500) signed by DURAND in favour of the deceased (15 March 2012), funds withdrawn from the deceased's accounts around the time of death, and the partition (partage) of the estate
- **Notary**: Maître Lefebvre, Massy

## Parent repository

The git root is one level up (`04-ProjectPython/`). Commits here are visible across the shared repo history alongside other sibling projects (deep-learning, crypto, energy, PyCoral, etc.).
