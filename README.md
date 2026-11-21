# RAG Legal Assistant

A Flask **RAG** (Retrieval-Augmented Generation) application for analysing the
correspondence of a French/Italian succession (estate) case. It parses emails
(`.eml`) and their attachments, indexes them in a vector store, builds a document
graph, and exposes several conversational agents to query, synthesise and devise a
legal strategy over the case file.

> ⚠️ **Confidential data.** Emails, attachments, indexes and databases are **not**
> versioned (see [`.gitignore`](.gitignore)). This repository contains **code only**.
> The `.eml` files go locally into `data/sources/` (see below).

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
- **Access control**: login required for every page; an admin role manages user
  accounts and holds exclusive access to `/settings` and `/admin` (see
  [Authentication](#authentication) below).

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
`/advisor`, `/chat`, `/admin` (indexing + migration, **admin only**), `/eval`,
`/settings` (providers & keys, **admin only**), `/admin/users` (user management,
**admin only**). Every page requires login — see [Authentication](#authentication).

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

# 4. Read the one-time setup code printed on first startup
docker compose -f docker-compose.prod.yml logs app | grep -A2 "code d'installation"
```

Open the app in a browser: with no account yet, it shows the **setup wizard**
(see [First-run setup](#first-run-setup)). Enter the setup code, create the
administrator account, then follow the optional steps (vector database location,
models and API keys, collection mailbox, first case). To bring existing data over
instead, **restore it** via `/admin` (*Import* button) or `python migrate.py import`
— see [Portability](#portability--migration).

The compose files start three services: `chromadb`, `app`, and `maild` (the mailbox
daemon, idle until a mailbox is enabled in the settings).

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
# On first startup, the admin password is printed once to the console — note it
# down immediately, then log in at /login (see Authentication below).
```

### Configuration

Three ways (in increasing order of precedence): defaults → `.env` →
**`/settings` menu** (written to `data/generated/settings.json`, never committed). Main
variables (see [`.env.example`](.env.example)):

| Variable | Role |
|---|---|
| `EMBED_PROVIDER` / `CHAT_PROVIDER` | `openai` \| `anthropic`* \| `mistral` \| `vllm` (*Anthropic = chat only) |
| `EMBED_MODEL` / `CHAT_MODEL` | models per provider (e.g. `text-embedding-3-large` / `gpt-4o`) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `MISTRAL_API_KEY` | keys depending on providers |
| `VLLM_BASE_URL` / `VLLM_API_KEY` | OpenAI-compatible vLLM endpoint (optional) |
| `CHROMA_HOST` / `CHROMA_PORT` | ChromaDB service (`localhost:8000` in dev, `chromadb:8000` in compose) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | chunking at indexing time |
| `SOURCE_DIR` / `GENERATED_DIR` | override the default `data/sources` / `data/generated` roots (optional) |
| `FLASK_SECRET_KEY` | Flask session signing key — **required** for logins to persist across restarts (see [Authentication](#authentication)) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | bootstrap admin account created on first startup (optional — see below) |

> Providers, models and keys are managed most easily from **`/settings`**.
> Infrastructure (Chroma, Flask, chunking) stays driven by `.env` / `docker-compose`.

## Data

`data/` splits into two independent roots — input you provide vs. output the app
generates — configurable via `SOURCE_DIR` / `GENERATED_DIR` in `.env` (defaults shown):

Place the source emails in `data/sources/`, organised by sender/party, e.g.:

```
data/sources/
  mails de dubois/      001 ….eml, 002 ….eml, …
  mails de leroy/
  mails de lefebvre/
  uploads/              .eml files added via the web UI
  …
```

Extracted attachments, OCR text, the SQLite database and `settings.json` are
**generated** by indexing/the app and land under `data/generated/` (ignored by git):

```
data/generated/
  attachments/           extracted from emails
  extracted_text/        OCR
  app.sqlite             registry, document graph, enrichment, eval data
  settings.json          /settings overrides (secrets — never committed)
```

Existing installs created before this split can be migrated in place (stop the app
first):

```bash
python migrate.py reorganize --yes
```

## Authentication

Every page requires login (Flask-Login + server-side sessions). There are two
roles:

- **admin** — full access, plus `/settings` (providers/API keys), `/admin`
  (indexing, migration import/export) and `/admin/users` (user management).
- **user** — works on cases: dashboard, timeline, graph, chat, synthesis, advisor,
  the triage inbox (confirm the case of forwarded emails) and the case switcher;
  no access to settings, indexing, evaluation, case management or user management.

Admins additionally manage cases (`/cases`), evaluation (`/eval`) and the mailbox.
Access is enforced on every route and API, not only by hiding links (403 page,
JSON 403 on `/api/*`).

### First-run setup

On first startup (when the `users` table is empty) there are two paths:

- **Setup wizard (default).** The login page redirects to `/setup`. The wizard asks
  for a **one-time setup code printed in the server logs**, so only the person
  installing the app can create the administrator account. It then offers four
  optional, skippable steps: where the vector database runs (this machine or another
  machine on the LAN), models and API keys, the collection mailbox, and the first
  case. Every step can be changed later in `/settings` and `/cases`. The wizard can
  be left and resumed, and disappears once finished. After ten wrong codes a new
  code is generated and printed.
- **Scripted install.** If `ADMIN_PASSWORD` is set in `.env`, the admin account
  (`ADMIN_USERNAME`, default `admin`) is created at startup and the wizard is
  skipped, as in previous versions.

```bash
# venv: look at the terminal running `python app.py`
# Docker: docker compose -f docker-compose.prod.yml logs app | grep -A2 "code d'installation"
```

Installs that already have accounts never see the wizard.

### Passwords

Every user can change their password from the sidebar (`/account/password`).
Accounts created or reset by an administrator receive a generated password and must
choose their own at first login. Accounts that existed before this version are not
forced to change theirs.

### Session key

Set `FLASK_SECRET_KEY` in `.env` (generate one with
`python -c "import secrets; print(secrets.token_hex(32))"`). Without it, a key is
generated once and kept with the data (`GENERATED_DIR/.session_key`, never exported).

### Built-in protections

- **Login throttling**: 8 failed attempts on one username (40 from one address) within
  15 minutes block further attempts until the window passes.
- **Cross-site requests**: any modifying request coming from another site is refused.
  If the app is served under a name your proxy does not forward in `Host`, list it in
  `TRUSTED_ORIGINS`.
- **Container**: the app does not run as root. It runs as the owner of the mounted data
  folder (or `APP_UID`/`APP_GID`, default 10001); files written as root by an earlier
  version are re-owned on first start. `RUN_AS_ROOT=1` restores the old behaviour.

### Case-specific texts stay out of the image

The code holds only neutral prompts and examples. Texts specific to the default case
(prompts, suggested questions, keywords of the `affaire` classifier) go in a private JSON
file kept with your data: `SOURCE_DIR/_private/default_case.json` (format documented in
[case_private.py](case_private.py)). It is read at runtime, reloaded when it changes, and
is never part of the image.

### Managing users

As an admin, go to **`/admin/users`** to create additional accounts. Passwords are
always **server-generated** and displayed **once**, immediately after creation or
after a password reset — there is no way to view them again afterward. Deleting
your own account, or the last remaining admin account, is blocked.

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

## Cases

The app handles several cases. Each case has a name, a short description and a
**context note** given to the assistant with every question on that case. The case
that existed before multi-case support is the *default case*: all existing data
belongs to it and its prompts are unchanged.

- **Switching**: the sidebar selector (shown once a second case exists). Search,
  chat, synthesis, advisor, timeline, graph and evaluation only see the active case.
- **Where emails live**: the folder decides the case. `data/sources/<folder>/` (e.g.
  `mails de dubois/`, `uploads/`) is the default case;
  `data/sources/cases/<case-id>/<folder>/` belongs to `<case-id>`. A reset followed
  by a re-index therefore restores every email to its case.
- **Creating the first additional case** tags the existing vectors with the default
  case (metadata only, nothing is re-embedded). The same step is available as
  `python migrate.py add-cases`.
- One email belongs to one case.

## Collection mailbox

Users forward emails to a dedicated mailbox; the `maild` service polls it over IMAP
and puts each email in a **triage queue** (`/inbox`) with a proposed case and the
reasons for it. A user confirms or changes the case; only then does the email join
the case and get indexed. Nothing is ever deleted from the mailbox.

Configure it in `/settings` → *Boîte de collecte* (or in the setup wizard):

- **IMAP** host, port, user and password. For Gmail / Google Workspace use an *app
  password* with `imap.gmail.com:993`. Password IMAP does not work with Microsoft 365.
- **Allowed senders** (addresses or `@domain`). Mail from anyone else is quarantined,
  and while the list is empty everything is quarantined. Optionally require the
  receiving server's SPF/DKIM/DMARC verdict, since a sender address can be forged.
- **Automatic filing** (off by default): files without review only when the case is
  certain — the email replies to an already-filed email, comes from a participant of
  a single case, or carries a `[dossier: name]` tag from an allowed sender. Content
  similarity and the LLM only *suggest*; text inside an email cannot decide its own
  filing.

Forwarded messages are unwrapped so that the **original** email is indexed (its
sender, date, text and attachments), whether it was forwarded as an attachment or
inline. Incoming mail is held in `data/sources/_mailbox/`, which indexing ignores.

```bash
python mail_daemon.py                      # the service (compose: maild)
python mail_daemon.py --once --from-dir D  # run the pipeline on local .eml files
```

## Vector database on another machine

ChromaDB can run on another machine of the LAN (for example a NAS) while the app,
the emails and the SQLite database stay where they are.

1. On the NAS, start ChromaDB with [deploy/docker-compose.nas.yml](deploy/docker-compose.nas.yml)
   (QNAP: Container Station). **Read the security notes in that file**: ChromaDB has
   no authentication and holds the text of the indexed emails, so restrict its port
   to the app machine's IP in the NAS firewall.
2. Move the existing vectors without re-embedding: `/settings` → *Base vectorielle*
   → *Copier les vecteurs vers cette adresse*, or
   `python migrate.py copy-vectors --to <nas-ip>:8000`.
3. Save the address in `/settings` (or pick it in the setup wizard). It applies to
   new requests immediately, on every worker.

Language and embedding models need a GPU machine or a cloud provider; a NAS cannot
serve them. A LAN model server (vLLM, Ollama, any OpenAI-compatible endpoint) is
configured with the existing *vLLM* fields.

## Publishing the Docker image (optional)

**Before pushing, check what the image contains.** `.dockerignore` keeps `data/`,
`backups/`, `.env`, `settings.json`, local tooling folders and documentation out of
the build context, so the image holds application code only. Two things remain your
decision: the code itself contains the default case's prompts and keyword lists
(party names, amounts), and a repository on Docker Hub is public unless you create
it as private.

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
