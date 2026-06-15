"""Graphe de connaissance interactif (pyvis) à partir de doc_edges + documents.

Un nœud par document, une arête par relation (reply_to, attached_to, shares_code…).
Couleur des nœuds = affaire. Couleur des arêtes = type de relation.
Retourne un fragment HTML autonome (vis.js embarqué) à embarquer dans graph.html.
"""
from __future__ import annotations

from pyvis.network import Network

_AFFAIRE_COLORS = {
    "succession": "#2c7fb8",
    "dette":      "#d95f0e",
    "pro":        "#756bb1",
    "autre":      "#969696",
}

_REL_COLORS = {
    "reply_to":    "#4477cc",
    "same_thread": "#4477cc",
    "attached_to": "#aaaaaa",
    "principal":   "#aaaaaa",
    "annexe_of":   "#aaaaaa",
    "accompanies": "#aaaaaa",
    "shares_code": "#e67e00",
}

_REL_LABELS = {
    "reply_to":    "réponse à",
    "same_thread": "même fil",
    "attached_to": "pièce jointe",
    "principal":   "doc principal",
    "annexe_of":   "annexe de",
    "accompanies": "accompagne",
    "shares_code": "code partagé",
}


def build_network_html(documents: list[dict], edges: list[dict]) -> str:
    """documents : lignes de state.list_documents().
    edges : lignes de doc_edges (src, dst, rel, weight).
    Retourne un fragment HTML complet (vis.js embarqué).
    """
    if not documents:
        return "<p>Aucun document indexé.</p>"

    # Dédupliquer par doc_id (plusieurs lignes possibles si upsert a créé des doublons).
    seen_ids: set[str] = set()
    unique_docs = []
    for d in documents:
        if d["doc_id"] not in seen_ids:
            seen_ids.add(d["doc_id"])
            unique_docs.append(d)
    documents = unique_docs

    net = Network(
        height="700px",
        width="100%",
        bgcolor="#fafafa",
        font_color="#333333",
        directed=True,
        notebook=False,
    )
    net.barnes_hut(
        gravity=-8000,
        central_gravity=0.3,
        spring_length=120,
        spring_strength=0.05,
        damping=0.09,
    )

    # Index des doc_id présents pour ne pas ajouter d'arêtes vers des nœuds absents.
    present = {d["doc_id"] for d in documents}

    for d in documents:
        doc_id = d["doc_id"]
        affaire = d.get("affaire") or "autre"
        color = _AFFAIRE_COLORS.get(affaire, "#969696")
        is_mail = d.get("type") == "mail"
        shape = "dot" if is_mail else "diamond"
        date_short = (d.get("date") or "")[:7]
        correspondent = (d.get("correspondent") or "")[:20]
        label = f"{correspondent}\n{date_short}"
        title = (d.get("subject") or doc_id)[:80]
        net.add_node(
            doc_id,
            label=label,
            title=title,
            color=color,
            shape=shape,
            size=12 if is_mail else 8,
        )

    # Comptage des codes partagés pour épaisseur proportionnelle.
    code_weight: dict[tuple, float] = {}
    for e in edges:
        if e.get("rel") == "shares_code":
            key = (e["src"], e["dst"])
            code_weight[key] = code_weight.get(key, 0) + 1.0

    seen_edges: set[tuple] = set()
    for e in edges:
        src, dst, rel = e.get("src"), e.get("dst"), e.get("rel", "")
        if not src or not dst or src not in present or dst not in present:
            continue
        key = (src, dst, rel)
        if key in seen_edges:
            continue
        seen_edges.add(key)

        color = _REL_COLORS.get(rel, "#cccccc")
        label = _REL_LABELS.get(rel, rel)
        width = code_weight.get((src, dst), 1.0) if rel == "shares_code" else 1.0
        dashes = rel in ("attached_to", "principal", "annexe_of", "accompanies")
        net.add_edge(
            src, dst,
            title=label,
            color=color,
            width=width,
            dashes=dashes,
            arrows="to",
        )

    # Désactiver les boutons de configuration (trop verbeux).
    net.set_options("""
    {
      "interaction": {
        "hover": true,
        "navigationButtons": true,
        "keyboard": true
      },
      "edges": {
        "smooth": { "type": "dynamic" }
      }
    }
    """)

    return net.generate_html(notebook=False)
