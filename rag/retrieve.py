"""Recherche : embed la requête, frappe les chunks, remonte aux documents parents.

Au jalon 1, l'enrichissement par le graphe n'existe pas encore (jalon 3) ; on
remonte simplement chaque chunk à son document parent via `parent_doc_id` et on
renvoie les métadonnées (date, correspondant, sujet, source) pour le sourcing.
"""
from __future__ import annotations

from dataclasses import dataclass

from rag.index import get_collections
from rag.providers import Embedder, get_embedder


@dataclass
class Hit:
    chunk_id: str
    parent_doc_id: str
    text: str
    distance: float
    metadata: dict


def search(
    query: str,
    top_k: int = 8,
    affaire: str | None = None,
    correspondent: str | None = None,
    embedder: Embedder | None = None,
    expand: bool = False,
) -> list[Hit]:
    embedder = embedder or get_embedder()
    _, chunks = get_collections()

    where: dict = {}
    if affaire:
        where["affaire"] = affaire
    if correspondent:
        where["correspondent"] = correspondent

    from rag.index import _truncate_for_embed

    q_emb = embedder.embed([_truncate_for_embed(query)])[0]
    res = chunks.query(
        query_embeddings=[q_emb],
        n_results=top_k,
        where=where or None,
        include=["documents", "metadatas", "distances"],
    )

    hits: list[Hit] = []
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for cid, text, meta, dist in zip(ids, docs, metas, dists):
        hits.append(
            Hit(
                chunk_id=cid,
                parent_doc_id=meta.get("parent_doc_id", ""),
                text=text,
                distance=dist,
                metadata=meta,
            )
        )

    if expand:
        hits = expand_with_graph(hits)
    return hits


def expand_with_graph(hits: list[Hit], max_extra: int = 4) -> list[Hit]:
    """Ajoute au contexte les documents voisins dans le graphe de connaissance.

    Pour les meilleurs documents trouvés, on remonte leurs voisins (PJ du mail,
    autres mails du fil, docs partageant un code) absents des hits, et on ajoute
    un chunk représentatif (résumé d'enrichissement ou 1er chunk) de chacun.
    Les Hits ajoutés portent metadata['graph_rel'] pour les distinguer.
    """
    import graph_state
    from rag.index import get_doc_chunks

    if not hits:
        return hits

    present_docs = {h.parent_doc_id for h in hits if h.parent_doc_id}
    # On part des 3 meilleurs documents distincts.
    seed_docs: list[str] = []
    for h in hits:
        if h.parent_doc_id and h.parent_doc_id not in seed_docs:
            seed_docs.append(h.parent_doc_id)
        if len(seed_docs) >= 3:
            break

    added: list[Hit] = []
    seen_neighbors: set[str] = set()
    for doc_id in seed_docs:
        for nb in graph_state.neighbors(doc_id):
            nb_id = nb["doc_id"]
            if nb_id in present_docs or nb_id in seen_neighbors:
                continue
            seen_neighbors.add(nb_id)
            for item in get_doc_chunks(nb_id, limit=1):
                meta = dict(item.get("metadata") or {})
                meta["graph_rel"] = nb["rel"]
                added.append(
                    Hit(
                        chunk_id=item["id"],
                        parent_doc_id=nb_id,
                        text=item["text"],
                        distance=1.0,  # voisin de graphe, pas un score vectoriel
                        metadata=meta,
                    )
                )
                break
            if len(added) >= max_extra:
                break
        if len(added) >= max_extra:
            break

    return hits + added


def format_context(hits: list[Hit]) -> tuple[str, list[dict]]:
    """Construit le bloc de contexte pour le LLM + la liste de sources.

    Retourne (contexte_texte, sources) où sources est dédupliqué par document
    parent et porte les métadonnées d'affichage/lien.
    """
    _REL_LABELS = {
        "reply_to": "réponse à", "same_thread": "même fil",
        "attached_to": "pièce jointe du mail", "principal": "document principal",
        "annexe_of": "annexe de", "accompanies": "accompagne",
        "shares_code": "partage une référence",
    }

    context_blocks: list[str] = []
    sources: dict[str, dict] = {}
    for i, h in enumerate(hits, 1):
        m = h.metadata
        # Annotations : résumé (enrichissement) et provenance graphe (document lié).
        extra = ""
        if m.get("kind") == "enrichment":
            extra += " — résumé"
        if m.get("graph_rel"):
            extra += f" — document lié ({_REL_LABELS.get(m['graph_rel'], m['graph_rel'])})"
        tag = (
            f"[S{i}] {m.get('date', '?')[:10]} — {m.get('correspondent', '?')} — "
            f"{m.get('title', m.get('subject', ''))}{extra}"
        )
        context_blocks.append(f"{tag}\n{h.text}")
        pid = h.parent_doc_id
        if pid and pid not in sources:
            sources[pid] = {
                "doc_id": pid,
                "type": m.get("type", "mail"),
                "date": m.get("date", ""),
                "correspondent": m.get("correspondent", ""),
                "title": m.get("title", m.get("subject", "")),
                "email_id": m.get("email_id", pid if m.get("type") == "mail" else ""),
            }
    return "\n\n---\n\n".join(context_blocks), list(sources.values())
