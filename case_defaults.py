"""Textes d'interface neutres (exemples affichés, prompts suggérés).

Aucun texte propre à une affaire ne figure dans le code : le dossier par défaut
peut recevoir les siens depuis un fichier privé (cf. case_private), les autres
dossiers depuis leur fiche (cf. case_state).
"""
from __future__ import annotations

DEFAULT_CASE_ID = "default"
DEFAULT_CASE_NAME = "Dossier principal"

# Libellés neutres, sans référence à une affaire donnée. Une installation peut fournir
# ses propres textes pour le dossier par défaut dans un fichier privé (cf. case_private).
GENERIC_UI = {
    "chat_placeholder": "Ex : Quand cet acte a-t-il été signé, et par qui ?",
    "synthesis_lede": (
        "Construisez un narratif juridique sourcé sur l'ensemble du dossier. Le chatbot "
        "combine les données structurées (chronologie, déclarations, fils) et la recherche "
        "plein texte pour rédiger des synthèses citant chaque source."
    ),
    "synthesis_placeholder": "Ex : Synthétise les positions de chaque partie…",
    "synthesis_prompts": [
        {"label": "📅 Chronologie complète",
         "prompt": "Reconstitue la chronologie complète des événements du dossier, en indiquant les dates clés et les acteurs impliqués."},
        {"label": "👥 Parties et positions",
         "prompt": "Présente chaque partie du dossier, son rôle, et résume la position qu'elle défend avec ses principaux arguments."},
        {"label": "💶 Enjeux financiers",
         "prompt": "Recense les montants en jeu dans le dossier : qui réclame quoi, à qui, sur quel fondement, et ce qui est contesté."},
        {"label": "🚧 Points de blocage",
         "prompt": "Identifie les points de blocage de la procédure : quelles démarches restent en attente, quels délais ont été manqués, quels actes restent à signer ?"},
    ],
    "advisor_prompts": [
        {"label": "⚖️ Répondre à une demande adverse",
         "prompt": "La partie adverse formule une demande à notre encontre. Analyse sa position à partir du dossier et élabore notre stratégie de réponse : arguments à opposer et pièces à mobiliser."},
        {"label": "📜 Contestation d'une pièce",
         "prompt": "La partie adverse conteste la validité ou la portée d'une pièce que nous produisons. Comment en défendre la validité, et quelles pièces complémentaires renforceraient notre position ?"},
        {"label": "🤝 Proposition de règlement",
         "prompt": "La partie adverse propose un règlement amiable qui nous est défavorable. Analyse sa proposition et élabore une contre-proposition argumentée."},
    ],
}
