"""Démon de la boîte de collecte.

Relève périodiquement une boîte IMAP où les utilisateurs transfèrent des emails,
les met en file de tri (cf. mail_intake.py) et indexe ceux qui ont été classés.

Lancement :
    python mail_daemon.py                    # boucle (service docker `maild`)
    python mail_daemon.py --once             # un seul passage
    python mail_daemon.py --once --from-dir DOSSIER   # traite des .eml locaux, sans IMAP

Tant que la boîte n'est pas activée dans les réglages (`imap_enabled`), le démon
ne se connecte à rien : il se contente d'indexer les emails déjà classés.
Processus séparé de l'app web (gunicorn lance plusieurs workers : une relève
intégrée s'exécuterait une fois par worker). Il n'importe pas app.py.
"""
from __future__ import annotations

import argparse
import imaplib
import re
import signal
import socket
import sys
import time
from pathlib import Path

import db_init
import mail_intake
import mailbox_state
from config import cfg

IMAP_TIMEOUT = 60
_stop = False


def _on_signal(_sig, _frame):
    global _stop
    _stop = True


def _safe_error(exc: Exception) -> str:
    """Message d'erreur sans secret (le mot de passe ne doit jamais atteindre le journal)."""
    text = f"{type(exc).__name__}: {exc}"
    if cfg.imap_password:
        text = text.replace(cfg.imap_password, "***")
    return text[:300]


# --------------------------------------------------------------------------- #
# IMAP
# --------------------------------------------------------------------------- #
def imap_connect(conf=None):
    conf = conf or cfg
    if conf.imap_ssl:
        conn = imaplib.IMAP4_SSL(conf.imap_host, int(conf.imap_port), timeout=IMAP_TIMEOUT)
    else:
        # Sans TLS implicite : chiffrement négocié par STARTTLS, jamais de mot de passe en clair.
        conn = imaplib.IMAP4(conf.imap_host, int(conf.imap_port), timeout=IMAP_TIMEOUT)
        conn.starttls()
    conn.login(conf.imap_user, conf.imap_password or "")
    return conn


def imap_test(conf=None) -> dict:
    """Connexion + ouverture du dossier, sans rien lire ni modifier (bouton « Tester »)."""
    conf = conf or cfg
    conn = imap_connect(conf)
    try:
        typ, data = conn.select(conf.imap_folder or "INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Dossier « {conf.imap_folder} » introuvable.")
        return {"ok": True, "messages": int(data[0] or 0)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _uidvalidity(conn) -> int | None:
    typ, data = conn.response("UIDVALIDITY")
    try:
        return int(data[0]) if data and data[0] else None
    except (TypeError, ValueError):
        return None


def poll_imap() -> int:
    """Relève les nouveaux messages. Retourne le nombre de messages traités."""
    account_key = f"{cfg.imap_host}|{cfg.imap_user}|{cfg.imap_folder}"
    st = mailbox_state.get_state()
    conn = imap_connect()
    handled = 0
    try:
        typ, _ = conn.select(cfg.imap_folder or "INBOX")
        if typ != "OK":
            raise RuntimeError(f"Dossier IMAP « {cfg.imap_folder} » introuvable.")
        validity = _uidvalidity(conn)
        last_uid = int(st.get("last_uid") or 0)
        if st.get("account_key") != account_key or (validity and st.get("uidvalidity") != validity):
            # Autre boîte, ou numérotation réinitialisée par le serveur : on repart de zéro.
            # Les messages déjà vus sont reconnus par leur empreinte et ignorés.
            last_uid = 0
            mailbox_state.update_state(account_key=account_key, uidvalidity=validity, last_uid=0)

        typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid) if typ == "OK" else []
        for uid in uids:
            if _stop:
                break
            typ, size_data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
            m = re.search(rb"RFC822\.SIZE (\d+)", size_data[0] if size_data and size_data[0] else b"")
            size = int(m.group(1)) if m else 0
            if size > cfg.mailbox_max_bytes:
                mailbox_state.log(f"Message {uid} ignoré : {size // 1_000_000} Mo dépasse la limite.", level="warning")
                mailbox_state.update_state(last_uid=uid)
                continue
            # BODY.PEEK : lecture sans marquer le message comme lu.
            typ, msg_data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((part[1] for part in msg_data or [] if isinstance(part, tuple)), None)
            if typ != "OK" or not raw:
                mailbox_state.log(f"Message {uid} illisible sur le serveur.", level="error")
                mailbox_state.update_state(last_uid=uid)
                continue
            mail_intake.ingest(raw, uid=uid, uidvalidity=validity)
            handled += 1
            _mark_processed(conn, uid)
            mailbox_state.update_state(last_uid=uid)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return handled


def _mark_processed(conn, uid: int) -> None:
    """Marque le message comme traité. Ne supprime jamais rien de la boîte."""
    try:
        if cfg.imap_processed_action == "move" and cfg.imap_processed_folder:
            conn.create(cfg.imap_processed_folder)          # sans effet si le dossier existe
            typ, _ = conn.uid("MOVE", str(uid), cfg.imap_processed_folder)
            if typ == "OK":
                return
            conn.uid("COPY", str(uid), cfg.imap_processed_folder)
        conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
    except Exception as exc:
        mailbox_state.log(f"Marquage du message {uid} impossible : {_safe_error(exc)}", level="warning")


# --------------------------------------------------------------------------- #
# Boucle
# --------------------------------------------------------------------------- #
def cycle(from_dir: Path | None = None) -> None:
    cfg.refresh_if_changed()
    mailbox_state.heartbeat(socket.gethostname())
    now = time.time()

    if from_dir is not None:
        n = 0
        for path in sorted(Path(from_dir).glob("*.eml")):
            n += len(mail_intake.ingest(path.read_bytes()))
        mailbox_state.log(f"{n} email(s) mis en file depuis {from_dir}.")
    elif cfg.imap_enabled and cfg.imap_host and cfg.imap_user:
        st = mailbox_state.get_state()
        try:
            n = poll_imap()
            mailbox_state.update_state(last_poll_at=now, last_ok_at=now, last_error=None, consecutive_errors=0)
            if n:
                mailbox_state.log(f"Relève : {n} message(s) traité(s).")
        except Exception as exc:
            errors = int(st.get("consecutive_errors") or 0) + 1
            mailbox_state.update_state(last_poll_at=now, last_error=_safe_error(exc), consecutive_errors=errors)
            if errors in (1, 5) or errors % 20 == 0:        # journal sobre en cas de panne durable
                mailbox_state.log(f"Relève impossible : {_safe_error(exc)}", level="error")

    try:
        mail_intake.index_filed()
    except Exception as exc:
        mailbox_state.log(f"Indexation des emails classés impossible : {_safe_error(exc)}", level="error")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Démon de la boîte de collecte.")
    parser.add_argument("--once", action="store_true", help="Un seul passage puis arrêt.")
    parser.add_argument("--from-dir", type=Path, default=None,
                        help="Traite les .eml d'un dossier local au lieu de la boîte IMAP.")
    args = parser.parse_args(argv)

    db_init.ensure_all()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if args.once or args.from_dir:
        cycle(args.from_dir)
        return 0

    mailbox_state.log("Démon démarré" + ("" if cfg.imap_enabled else " (boîte de collecte désactivée)") + ".")
    announced_disabled = not cfg.imap_enabled
    while not _stop:
        cycle()
        if not cfg.imap_enabled and not announced_disabled:
            mailbox_state.log("Boîte de collecte désactivée.")
        if cfg.imap_enabled and announced_disabled:
            mailbox_state.log("Boîte de collecte activée.")
        announced_disabled = not cfg.imap_enabled
        # Attente fractionnée : un arrêt ou un changement de réglage est pris en compte vite.
        deadline = time.time() + max(30, int(cfg.imap_poll_interval or 300))
        while not _stop and time.time() < deadline:
            time.sleep(5)
            mailbox_state.heartbeat(socket.gethostname())
            if cfg.refresh_if_changed():
                break
    mailbox_state.log("Démon arrêté.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
