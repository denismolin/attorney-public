"""Initialisation unique du schéma SQLite, partagée par tous les points d'entrée.

Appelée par l'app (app.py), l'import de migration (migrate.py) et les processus
annexes (démon mail). Toutes les migrations sont additives et idempotentes.
"""
from __future__ import annotations


def ensure_all(reset_stale: bool = False) -> None:
    """Crée/migre toutes les tables.

    `reset_stale=True` est réservé au démarrage de l'app web : il remet à zéro les
    drapeaux « en cours » laissés par un crash. Les autres processus passent False
    pour ne pas effacer l'état d'un job vivant ailleurs.
    """
    import auth_state
    import case_state
    import eval_state
    import graph_state
    import mailbox_state
    import state
    from config import cfg

    cfg.ensure_dirs()
    state.init_db()
    case_state.init_cases_db()
    if reset_stale:
        state.reset_stale_running()
    eval_state.init_eval_db(reset_running=reset_stale)
    graph_state.init_graph_db()
    mailbox_state.init_mailbox_db()
    auth_state.init_users_db()
    auth_state.apply_password_policy_migration()
