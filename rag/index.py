"""Index vectoriel ChromaDB (mode client/serveur via HttpClient).

RAG hiérarchique 2 niveaux :
- collection "documents" (niv.1) : 1 entrée par document (mail ou PJ) = résumé
  court + métadonnées. Sert à la vue d'ensemble et au clustering (jalon 2).
- collection "chunks" (niv.2) : les morceaux de texte, avec `parent_doc_id`.

Les embeddings sont fournis par `rag.providers` (on passe `embeddings=` à Chroma,
donc Chroma ne calcule rien lui-même). Upsert idempotent par id stable.
"""
from __future__ import annotations

import chromadb

from config import cfg
from rag.providers import Embedder

DOCUMENTS_COLLECTION = "documents"
CHUNKS_COLLECTION = "chunks"


def _truncate_for_embed(text: str) -> str:
    """Tronque un texte à la limite de caractères de l'embedder (anti-413).

    Le modèle e5-large refuse les entrées > 512 tokens. On borne en caractères
    (approximation prudente) ; le texte complet reste stocké séparément pour
    l'affichage et le contexte du chat.
    """
    limit = cfg.embed_max_chars
    if len(text) <= limit:
        return text
    return text[:limit]


def get_client() -> chromadb.api.ClientAPI:
    return chromadb.HttpClient(host=cfg.chroma_host, port=cfg.chroma_port)


def get_collections(client: chromadb.api.ClientAPI | None = None):
    client = client or get_client()
    docs = client.get_or_create_collection(
        DOCUMENTS_COLLECTION, metadata={"hnsw:space": "cosine"}
    )
    chunks = client.get_or_create_collection(
        CHUNKS_COLLECTION, metadata={"hnsw:space": "cosine"}
    )
    return docs, chunks


def _sanitize_metadata(meta: dict) -> dict:
    """Chroma n'accepte que str/int/float/bool en métadonnée."""
    clean: dict = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)):
            clean[k] = v
        elif v is None:
            continue
        else:
            clean[k] = str(v)
    return clean


def existing_ids(collection, ids: list[str]) -> set[str]:
    """Renvoie le sous-ensemble d'ids déjà présents (pour l'idempotence)."""
    if not ids:
        return set()
    got = collection.get(ids=ids, include=[])
    return set(got.get("ids", []))


def get_doc_chunks(parent_doc_id: str, limit: int = 2) -> list[dict]:
    """Récupère jusqu'à `limit` chunks d'un document parent (pour l'expansion graphe).

    Privilégie le chunk d'enrichissement (résumé) puis les premiers chunks de texte.
    Retourne [{'id', 'text', 'metadata'}].
    """
    _, chunks = get_collections()
    try:
        res = chunks.get(
            where={"parent_doc_id": parent_doc_id},
            include=["documents", "metadatas"],
        )
    except Exception:
        return []
    ids = res.get("ids", [])
    docs = res.get("documents", [])
    metas = res.get("metadatas", [])
    items = [
        {"id": cid, "text": txt, "metadata": meta or {}}
        for cid, txt, meta in zip(ids, docs, metas)
    ]
    # Enrichissement (chunk_index == -1) en premier, puis ordre des chunks.
    items.sort(key=lambda it: it["metadata"].get("chunk_index", 0))
    enrich_first = [it for it in items if it["metadata"].get("kind") == "enrichment"]
    others = [it for it in items if it["metadata"].get("kind") != "enrichment"]
    return (enrich_first + others)[:limit]


def upsert_documents(
    docs_collection,
    embedder: Embedder,
    items: list[dict],
) -> int:
    """items: [{'id','text','metadata'}]. Embeddings calculés via le provider."""
    if not items:
        return 0
    texts = [it["text"] for it in items]
    # Le texte envoyé à l'embedding est tronqué (limite 512 tokens du modèle e5) ;
    # le texte stocké (`documents`) reste complet pour l'affichage.
    embeddings = embedder.embed([_truncate_for_embed(t) for t in texts])
    docs_collection.upsert(
        ids=[it["id"] for it in items],
        embeddings=embeddings,
        documents=texts,
        metadatas=[_sanitize_metadata(it["metadata"]) for it in items],
    )
    return len(items)


def upsert_chunks(
    chunks_collection,
    embedder: Embedder,
    items: list[dict],
    batch_size: int = 64,
) -> int:
    """items: [{'id','text','metadata'}]. Embedding par lots."""
    total = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        texts = [it["text"] for it in batch]
        embeddings = embedder.embed([_truncate_for_embed(t) for t in texts])
        chunks_collection.upsert(
            ids=[it["id"] for it in batch],
            embeddings=embeddings,
            documents=texts,
            metadatas=[_sanitize_metadata(it["metadata"]) for it in batch],
        )
        total += len(batch)
    return total


def reset_index() -> dict:
    """Supprime et recrée les deux collections + vide le registre SQLite.

    Retourne {'documents': n, 'chunks': n} — nombre d'entrées supprimées.
    """
    import state

    client = get_client()
    counts = {}
    for name in (DOCUMENTS_COLLECTION, CHUNKS_COLLECTION):
        try:
            col = client.get_collection(name)
            counts[name] = col.count()
            client.delete_collection(name)
        except Exception:
            counts[name] = 0
    # Recrée les collections vides (évite une erreur au prochain heartbeat).
    get_collections(client)
    # Vide le registre SQLite des documents (repart de zéro pour l'idempotence).
    state.reset_documents()
    # Vide aussi le graphe de connaissance (enrichissement, entités, arêtes).
    import graph_state

    graph_state.clear_graph()
    return counts


def heartbeat_ok() -> bool:
    """Vérifie que le service ChromaDB répond (utilisé au démarrage)."""
    try:
        get_client().heartbeat()
        return True
    except Exception:
        return False
