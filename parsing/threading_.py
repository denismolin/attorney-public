"""Reconstruction des fils de discussion (threads) à partir des en-têtes.

Les en-têtes `In-Reply-To` et `References` relient un email à ceux qu'il cite.
On construit les fils par union-find sur les `Message-ID` : tous les emails
reliés (directement ou transitivement) partagent un même `thread_id`.

Aucun appel LLM — purement déterministe à partir des en-têtes parsés.
"""
from __future__ import annotations

import hashlib

from parsing.eml_parser import Email


class _UnionFind:
    """Union-find (disjoint set) sur des clés arbitraires (ici des Message-ID)."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Compression de chemin
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def _thread_id_from_root(root: str) -> str:
    """thread_id stable = hash court du Message-ID racine du fil."""
    h = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()[:16]
    return f"thr_{h}"


def build_threads(emails: list[Email]) -> dict[str, str]:
    """Retourne {doc_id: thread_id} pour tous les emails fournis.

    Deux emails sont dans le même fil si l'un cite l'autre via In-Reply-To ou
    References (relation transitive). Un email isolé forme son propre fil.
    """
    uf = _UnionFind()

    # Index Message-ID -> doc_id (pour retrouver les emails cités présents dans le corpus).
    by_mid: dict[str, str] = {}
    for em in emails:
        if em.message_id:
            by_mid[em.message_id] = em.doc_id

    for em in emails:
        # Clé de l'email dans l'union-find : son Message-ID si présent, sinon doc_id.
        key = em.message_id or em.doc_id
        uf.find(key)  # garantit la présence

        cited: list[str] = []
        if em.in_reply_to:
            cited.append(em.in_reply_to)
        cited.extend(em.references or [])
        for ref in cited:
            ref = ref.strip()
            if ref:
                uf.union(key, ref)

    # Mappe chaque doc_id vers le thread_id de la racine de son fil.
    result: dict[str, str] = {}
    for em in emails:
        key = em.message_id or em.doc_id
        root = uf.find(key)
        result[em.doc_id] = _thread_id_from_root(root)
    return result
