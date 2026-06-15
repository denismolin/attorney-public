# Architecture — RAG & Agentic Design

This document explains how the application retrieves, reasons over, and answers
questions about a corpus of legal email correspondence. It covers the **indexing
pipeline**, the **retrieval layer** (vector search + knowledge-graph expansion),
and the **three conversational agents** built on top — including the agentic
*function-calling* orchestration used by the strategy advisor.

> Code references use `file:line`-style links. The terms *document*, *chunk*,
> *correspondent* and *affaire* (case/matter) recur throughout.

---

## 1. Big picture

```
        .eml files + attachments
                 │
        ┌────────▼─────────┐     background thread
        │  Indexing        │     (indexer.py)
        │  pipeline        │
        └────────┬─────────┘
                 │ embeddings + metadata + graph edges
       ┌─────────┼──────────────────────────┐
       ▼         ▼                           ▼
  ChromaDB   data/app.sqlite          knowledge graph
 (vectors)   (registry, enrichment,   (doc ↔ doc edges,
             eval data)                in app.sqlite)
       │         │                           │
       └─────────┴───────────┬───────────────┘
                             ▼
                   ┌───────────────────┐
                   │  Retrieval layer  │   rag/retrieve.py
                   │  search + graph   │
                   │  expansion        │
                   └─────────┬─────────┘
                             ▼
                ┌──────────────────────────┐
                │      Advisor agent       │  rag/advisor_chat.py
                │ (function-calling        │
                │  orchestrator)           │
                └─────────────┬────────────┘
            function-calling delegates to ↓ (tools)
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   Chat agent          Synthesis agent       direct search
  (plan→exec)         (SQL briefing +       (raw passages)
   rag/chat.py         full-text)            rag/retrieve.py
                       rag/synthesis_chat.py
        │                     │
        └─────────┬───────────┘
                  ▼
          Retrieval layer (search + graph expansion)

  Chat and Synthesis are ALSO usable directly (their own /chat, /synthesis
  panels); the Advisor calls them as tools and merges their sourced results.
```

The system follows a classic **Retrieval-Augmented Generation** shape — embed the
query, fetch relevant passages, ground the LLM's answer in them — but layers three
things on top that distinguish it:

1. **Structured pre-retrieval** — the chat agent *plans* its searches; the
   synthesis agent prepends a **SQL briefing** of structured facts.
2. **Knowledge-graph expansion** — retrieved passages pull in their graph
   neighbours (email thread siblings, attachments, docs sharing a reference).
3. **Agentic orchestration** — the **Advisor sits one tier above** the other two
   agents: it is a tool-using agent that, via *function-calling*, delegates to the
   **Chat agent**, the **Synthesis agent**, and to raw search — then merges their
   sourced results into a defence strategy (§4.3). Chat and Synthesis remain usable
   on their own; the Advisor reuses them as building blocks.

Every factual claim an agent makes is **cited** `[date — correspondent — subject]`,
and answers are grounded *only* in retrieved context (the system prompts forbid
answering from memory).

---

## 2. Indexing pipeline

`indexer.py` runs in a background thread (triggered from `/admin` or the CLI). It is
**idempotent**: each document carries a `content_hash`; an unchanged, already-indexed
document is skipped.

Stages (`indexer.py:1`):

| # | Stage | Module | Output |
|---|---|---|---|
| 1 | **Parse** `.eml` | `parsing/eml_parser.py` | `Email` objects (multi-encoding, multipart) |
| 2 | **Classify** by *affaire* | `parsing/classifier.py` | matter tag per document |
| 3 | **Extract** attachments | `parsing/attachments.py` | text from PDF/DOCX/RTF (+ OCR for scanned PDFs) |
| 4 | **Enrich** (LLM) | `parsing/enrich.py` | per-doc summary, entities, key facts |
| 5 | **Thread** | `parsing/threading_.py` | reply/thread edges between emails |
| 6 | **Chunk** | `rag/chunker.py` | overlapping text chunks |
| 7 | **Embed** | `rag/providers.py` | vectors via the configured embedder |
| 8 | **Upsert** | `rag/index.py` | documents + chunks into ChromaDB |

The **knowledge graph** is built alongside (in `graph_state.py`): edges connect a
document to its attachments, to sibling emails in the same thread, and to documents
sharing a reference/code. These edges power graph expansion at query time (§3.2).

### Where data lives
- **ChromaDB** — chunk vectors + metadata, accessed over HTTP (`HttpClient`).
- **`data/app.sqlite`** — document registry, indexing progress/log, the knowledge
  graph, LLM enrichment, and evaluation data. (Confidential `data/` is gitignored.)

---

## 3. Retrieval layer (`rag/retrieve.py`)

The retrieval primitive is `search()` (`rag/retrieve.py:24`):

1. Embed the query with the configured embedder (truncated to the model limit).
2. Query the `chunks` ChromaDB collection for the top-k nearest chunks, optionally
   filtered by metadata (`affaire`, `correspondent`).
3. Map each chunk back to its **parent document** (`parent_doc_id`) and return `Hit`
   objects carrying the text, distance, and metadata used for citation.

### 3.1 `Hit` and `format_context`
A `Hit` (`rag/retrieve.py:15`) bundles chunk id, parent doc id, text, distance and
metadata. `format_context()` (`rag/retrieve.py:124`) turns a list of hits into:
- a numbered context block (`[S1] date — correspondent — subject … text`), and
- a **deduplicated source list** (one entry per parent document) for the UI.

Passages are annotated when relevant — `— résumé` for enrichment summaries, and
`— document lié (réponse à / pièce jointe / …)` for graph-expanded neighbours.

### 3.2 Knowledge-graph expansion
`expand_with_graph()` (`rag/retrieve.py:72`) is the key retrieval enhancement. After
vector search, it takes the top ~3 distinct documents, looks up their **graph
neighbours** (`graph_state.neighbors`), and adds a representative chunk from each
neighbour that wasn't already retrieved. Relationship labels include:

`reply_to`, `same_thread`, `attached_to`, `principal`, `annexe_of`, `accompanies`,
`shares_code`.

This pulls in context that pure semantic similarity misses — e.g. the PDF attached
to a relevant email, or the earlier message in a thread — which matters for legal
reasoning where a document's meaning depends on what it answers or accompanies.

---

## 4. The three agents

All three share the providers abstraction (`rag/providers.py`) so any agent runs on
OpenAI / Anthropic / Mistral / vLLM, and all enforce sourced, context-grounded
answers.

### 4.1 Chat agent — *plan → execute → synthesize* (`rag/chat.py`)

A **structured** Q&A agent, not a single retrieve-then-read call:

1. **Analyse** (`rag/intent.py`) — decompose the question into sub-questions, extract
   key entities, and fold in facts already established in the session.
2. **Plan** (`rag/planner.py`) — turn sub-questions into concrete Chroma queries
   (each with its own `top_k` and optional `affaire` filter).
3. **Execute** (`_execute_plan`, `rag/chat.py:56`) — run each query (with graph
   expansion) and deduplicate hits by parent document.
4. **Synthesize** — answer from the assembled context, citing sources.

**Fallback:** if analysis/planning yields nothing, it degrades gracefully to a direct
`_retrieve_then_read` (`rag/chat.py:132`).

**Session memory:** `_update_session_ctx` (`rag/chat.py:91`) carries forward
established facts, seen documents, and known entities across turns, so follow-up
questions inherit context.

### 4.2 Synthesis agent — *SQL briefing + full text* (`rag/synthesis_chat.py`)

Produces a **sourced narrative** over a whole theme (timeline of a dispute, each
party's position). Its distinctive move: before retrieving chunks, it builds a
compact **SQL briefing** (`_build_sql_briefing`, ≤ 4000 chars) from the structured,
LLM-enriched data in SQLite — a filtered chronology and key facts — and injects it
*ahead of* the vector-retrieved passages. The LLM thus reasons over both
**structured** (briefing) and **unstructured** (chunks) views of the case.

### 4.3 Advisor agent — *agentic function-calling orchestrator* (`rag/advisor_chat.py`)

The most agentic component. The user submits an adversary's argument/scenario; the
advisor builds a **defence strategy**. Rather than a fixed pipeline, it is given
**tools** and decides for itself how to gather information (`TOOLS`,
`rag/advisor_chat.py:75`):

| Tool | Delegates to | Use |
|---|---|---|
| `interroger_chat` | the Chat agent (§4.1) | a precise fact (date, amount, who said what) |
| `demander_synthese` | the Synthesis agent (§4.2) | a sourced overview of a theme |
| `rechercher_documents` | raw `search()` (§3) | fish through raw passages directly |

**Tool loop** (`_run_tool_loop`, `rag/advisor_chat.py:198`): the LLM is called with
the tools; for each `tool_call` returned, the tool executes, its result is fed back
as a message, and the loop repeats — up to `_MAX_TOOL_TURNS` (5). Sources from every
tool call are accumulated and deduplicated. When the model stops calling tools, a
**final answer is composed** and *streamed* (`advise_stream`) — note the loop runs in
non-streaming `complete()` because streaming doesn't support tools, then the final
write-up streams via `complete_stream()`.

**Deterministic fallback** (`_fallback_context`, `rag/advisor_chat.py:251`): if a
provider returns *no* tool call (e.g. a model without tool support), the agent
injects a SQL briefing + a direct search itself, guaranteeing a grounded answer
regardless of provider capability.

The advisor always ends with a `## Pièces complémentaires utiles` section listing
documents that would strengthen the strategy.

### Streaming
The synthesis and advisor agents expose **SSE generators** (`*_stream`) emitting
typed events — `step` (progress), `tool` (which tool was consulted), `thinking`
(reasoning, for reasoning models), `text` (incremental answer), and `sources` (final
payload). The advisor additionally supports `reasoning_effort` (low/medium/high),
mapped per provider.

---

## 5. Provider abstraction (`rag/providers.py`)

`get_chat()` / `get_embedder()` re-read configuration on every call, so provider,
model and keys switched in `/settings` apply live without a restart. The layer
normalises chat completion, streaming, and tool-calling across OpenAI, Anthropic
(chat only), Mistral and vLLM (OpenAI-compatible) — the agents above are written
once against this interface.

---

## 6. Evaluation harness (`eval/`)

A self-contained RAG evaluation loop:
- **`eval/generator.py`** — samples chunks from ChromaDB and asks an LLM to produce
  a golden dataset of `(question, expected_answer, category, difficulty)`.
- **`eval/judge.py`** — for each question, runs the real RAG answer, then an
  **LLM-as-judge** scores it on *faithfulness*, *relevance* and *completeness*,
  yielding a per-question verdict and an aggregate pass rate.

This makes retrieval/answer quality measurable when prompts, models, or chunking
change.
