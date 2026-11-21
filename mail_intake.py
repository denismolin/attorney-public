"""Boîte de collecte : de l'email reçu à l'email classé dans un dossier.

Étapes (`ingest`) :
  1. le message reçu est conservé tel quel dans `SOURCE_DIR/_mailbox/raw/` ;
  2. contrôle de l'expéditeur (liste d'autorisation, authentification) — sinon quarantaine ;
  3. reconstitution de l'email d'origine (cf. parsing/forward.py) dans `_mailbox/pending/` ;
  4. détection des doublons (email déjà présent dans un dossier ou déjà en file) ;
  5. proposition de dossier (cf. rag/case_classifier.py) ;
  6. mise en file de tri — ou classement direct si le classement automatique est
     activé ET qu'un signal déterministe désigne le dossier.

Le dossier `_mailbox/` est ignoré par l'indexation : rien de ce qui s'y trouve
n'entre dans la base de connaissances avant d'avoir été classé (`file_item`), ce
qui déplace l'email dans le dossier choisi. Le démon l'indexe ensuite.

Ce module ne dépend ni de Flask ni d'IMAP : il est utilisé par le démon
(mail_daemon.py) et par les routes de tri (app.py).
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
import unicodedata
from email.utils import parseaddr
from pathlib import Path

import case_state
import mailbox_state
import state
from config import SOURCE_DIR, cfg
from parsing import forward
from parsing.eml_parser import parse_eml

MAILBOX_DIR = SOURCE_DIR / "_mailbox"
RAW_DIR = MAILBOX_DIR / "raw"
PENDING_DIR = MAILBOX_DIR / "pending"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(text: str, fallback: str = "inconnu") -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or fallback


def sender_folder(from_header: str) -> str:
    """Nom du dossier d'emails d'un expéditeur (« mails de <nom> »), comme les
    dossiers déposés à la main : le correspondant affiché reste lisible."""
    name, addr = parseaddr(from_header or "")
    name = re.sub(r"\b(ma[iî]tre|me|mme|mr?|m\.|dr|madame|monsieur)\b\.?", " ", name, flags=re.IGNORECASE)
    tokens = [t for t in re.split(r"[\s,]+", name.strip()) if t]
    slug = _slug(tokens[-1]) if tokens else _slug(addr.partition("@")[0])
    return f"mails de {slug}"


def _parse_pending(path: Path):
    return parse_eml(path, "boite")


# --------------------------------------------------------------------------- #
# Réception
# --------------------------------------------------------------------------- #
def ingest(raw: bytes, uid: int | None = None, uidvalidity: int | None = None) -> list[dict]:
    """Traite un message reçu. Retourne les éléments créés (un par email d'origine)."""
    wrapper_sha = _sha(raw)
    if mailbox_state.wrapper_seen(wrapper_sha):
        return []
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"{wrapper_sha[:20]}.eml"
    raw_path.write_bytes(raw)

    results = forward.unwrap(raw)
    first = results[0]
    base = {
        "uid": uid, "uidvalidity": uidvalidity, "wrapper_sha256": wrapper_sha,
        "raw_path": str(raw_path), "forwarder": first.forwarder,
    }

    # --- Contrôle de l'expéditeur du transfert -------------------------------
    allowed = cfg.allowed_senders()
    verified = forward.sender_authenticated(raw)
    refusal = None
    if not allowed:
        refusal = "Aucun expéditeur autorisé n'est configuré : tout message est mis en quarantaine."
    elif not forward.sender_allowed(first.forwarder, allowed):
        refusal = f"Expéditeur non autorisé : {first.forwarder or 'inconnu'}."
    elif cfg.mailbox_require_auth_results and not verified:
        refusal = "L'expéditeur n'est pas authentifié par le serveur de réception (SPF/DKIM/DMARC)."
    if refusal:
        item_id = mailbox_state.insert_item({
            **base, "sha256": wrapper_sha, "status": "quarantined", "sender_verified": int(verified),
            "subject": first.wrapper_subject, "unwrap_mode": first.mode, "error": refusal,
            "reasons": [refusal],
        })
        mailbox_state.log(f"Quarantaine : « {first.wrapper_subject[:60]} » — {refusal}", level="warning")
        return [mailbox_state.get_item(item_id)] if item_id else []

    created = []
    for res in results:
        item = _ingest_one(res, base, verified)
        if item:
            created.append(item)
    return created


def _ingest_one(res: forward.UnwrapResult, base: dict, verified: bool) -> dict | None:
    sha = _sha(res.eml)
    pending_path = PENDING_DIR / f"{sha[:20]}.eml"
    pending_path.write_bytes(res.eml)
    fields = {
        **base, "sha256": sha, "stored_path": str(pending_path), "unwrap_mode": res.mode,
        "forwarder_note": res.forwarder_note[:2000], "sender_verified": int(verified),
    }
    try:
        em = _parse_pending(pending_path)
    except Exception as exc:
        item_id = mailbox_state.insert_item({
            **fields, "status": "error", "subject": res.wrapper_subject,
            "error": f"Email illisible : {type(exc).__name__}", "reasons": []})
        mailbox_state.log(f"Email illisible : « {res.wrapper_subject[:60]} » ({exc})", level="error")
        return mailbox_state.get_item(item_id) if item_id else None

    fields.update({
        "orig_from": em.from_, "orig_to": ", ".join(em.to)[:500], "orig_date": em.date_iso,
        "subject": em.subject, "message_id": em.message_id, "doc_id": em.doc_id,
        "preview": " ".join(em.body_text.split())[:600], "n_attachments": len(em.attachments),
    })

    # --- Doublons ------------------------------------------------------------
    existing = state.get_document(em.doc_id)
    waiting = mailbox_state.find_by_message_id(em.message_id, ("pending", "needs_admin", "filed"))
    if existing or waiting:
        if existing:
            case = case_state.get_case(existing.get("case_id") or case_state.DEFAULT)
            why = f"Cet email est déjà dans le dossier « {case['name'] if case else existing.get('case_id')} »."
            fields["case_id"] = existing.get("case_id")
        else:
            why = "Cet email est déjà en attente de tri."
        item_id = mailbox_state.insert_item({**fields, "status": "duplicate", "reasons": [why], "error": why})
        pending_path.unlink(missing_ok=True)
        mailbox_state.log(f"Doublon ignoré : « {em.subject[:60]} »")
        return mailbox_state.get_item(item_id) if item_id else None

    # --- Proposition de dossier ---------------------------------------------
    from rag import case_classifier
    try:
        verdict = case_classifier.classify(
            em, wrapper_subject=res.wrapper_subject, forwarder_note=res.forwarder_note,
            wrapper_to=res.wrapper_to, log=mailbox_state.log,
        )
    except Exception as exc:
        verdict = {"case_id": None, "confidence": 0.0, "deterministic": False,
                   "reasons": [f"Proposition de dossier indisponible ({type(exc).__name__})."], "candidates": []}
    fields.update({
        "suggested_case_id": verdict["case_id"], "confidence": verdict["confidence"],
        "deterministic": int(bool(verdict["deterministic"])),
        "reasons": verdict["reasons"], "candidates": verdict["candidates"],
    })
    item_id = mailbox_state.insert_item({**fields, "status": "pending"})
    if not item_id:
        return None          # même email reçu en parallèle
    mailbox_state.log(
        f"Reçu : « {em.subject[:60]} » de {em.from_addr or '?'} — dossier proposé : "
        f"{verdict['case_id'] or 'aucun'} ({verdict['confidence']:.0%})"
    )

    # --- Classement automatique (optionnel) ----------------------------------
    if (cfg.mailbox_autofile and verdict["case_id"] and verdict["deterministic"]
            and verdict["confidence"] >= cfg.mailbox_autofile_threshold):
        try:
            file_item(item_id, verdict["case_id"], decided_by="auto")
        except Exception as exc:
            mailbox_state.log(f"Classement automatique impossible : {exc}", level="error")
    return mailbox_state.get_item(item_id)


# --------------------------------------------------------------------------- #
# Décisions de tri
# --------------------------------------------------------------------------- #
class TriageError(Exception):
    """Décision impossible (élément déjà traité, dossier inconnu, fichier manquant…)."""


def file_item(item_id: int, case_id: str, decided_by: str) -> dict:
    """Classe un email dans un dossier : le fichier rejoint les sources du dossier.
    L'indexation est faite ensuite par le démon (ou par une indexation manuelle)."""
    item = mailbox_state.get_item(item_id)
    if not item:
        raise TriageError("Élément introuvable.")
    if not case_state.exists(case_id):
        raise TriageError("Dossier inconnu ou archivé.")
    src = Path(item.get("stored_path") or "")
    if not src.is_file():
        raise TriageError("Le fichier de cet email est introuvable.")
    # Verrou logique : un seul des clics concurrents obtient le classement.
    if not mailbox_state.claim_item(item_id, mailbox_state.OPEN_STATUSES, "filed", case_id=case_id,
                                    decided_by=decided_by, decided_at=time.time(), error=None):
        raise TriageError("Cet email a déjà été traité.")

    root = SOURCE_DIR if case_id == case_state.DEFAULT else case_state.case_dir(case_id)
    folder = root / sender_folder(item.get("orig_from") or "")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (item.get("orig_date") or "")[:10] or time.strftime("%Y-%m-%d")
    dest = folder / f"{stamp} {_slug(item.get('subject') or 'email', 'email')} {item['sha256'][:8]}.eml"
    try:
        shutil.move(str(src), str(dest))
    except Exception as exc:
        mailbox_state.update_item(item_id, status="pending", case_id=None, decided_by=None, decided_at=None)
        raise TriageError(f"Déplacement impossible : {exc}")
    mailbox_state.update_item(item_id, stored_path=str(dest))

    # Apprentissage : une décision humaine rattache l'expéditeur d'origine au dossier.
    if decided_by != "auto":
        addr = parseaddr(item.get("orig_from") or "")[1].strip().lower()
        if "@" in addr and not addr.endswith("@invalid"):
            try:
                case_state.add_participant(case_id, "email", addr, source="confirmed")
            except Exception:
                pass
    mailbox_state.log(f"Classé dans « {case_id} » par {decided_by} : « {(item.get('subject') or '')[:60]} »")
    return mailbox_state.get_item(item_id)


def reject_item(item_id: int, decided_by: str) -> dict:
    if not mailbox_state.claim_item(item_id, mailbox_state.OPEN_STATUSES, "rejected",
                                    decided_by=decided_by, decided_at=time.time()):
        raise TriageError("Cet email a déjà été traité.")
    return mailbox_state.get_item(item_id)


def propose_case(item_id: int, name: str, decided_by: str) -> dict:
    name = " ".join((name or "").split())[:80]
    if not name:
        raise TriageError("Indiquez le nom du dossier proposé.")
    if not mailbox_state.claim_item(item_id, ("pending",), "needs_admin", proposed_case_name=name,
                                    decided_by=decided_by, decided_at=time.time()):
        raise TriageError("Cet email a déjà été traité.")
    return mailbox_state.get_item(item_id)


def restore_item(item_id: int, decided_by: str) -> dict:
    """Remet en file un email écarté ou mis en quarantaine (décision d'administrateur)."""
    item = mailbox_state.get_item(item_id)
    if not item or item["status"] not in ("rejected", "quarantined", "needs_admin", "error"):
        raise TriageError("Cet email ne peut pas être remis en file.")
    if item["status"] == "quarantined":
        # Jamais reconstitué : on reprend le message reçu, sans le contrôle d'expéditeur.
        raw = Path(item["raw_path"]).read_bytes()
        mailbox_state.delete_item(item_id)
        created = []
        for res in forward.unwrap(raw):
            one = _ingest_one(res, {"uid": item.get("uid"), "uidvalidity": item.get("uidvalidity"),
                                    "wrapper_sha256": item["wrapper_sha256"], "raw_path": item["raw_path"],
                                    "forwarder": res.forwarder}, bool(item.get("sender_verified")))
            if one:
                created.append(one)
        mailbox_state.log(f"Quarantaine levée par {decided_by} : « {(item.get('subject') or '')[:60]} »")
        if not created:
            raise TriageError("Aucun email exploitable dans ce message.")
        return created[0]
    if not Path(item.get("stored_path") or "").is_file():
        raise TriageError("Le fichier de cet email est introuvable.")
    mailbox_state.update_item(item_id, status="pending", proposed_case_name=None,
                              decided_by=None, decided_at=None)
    return mailbox_state.get_item(item_id)


# --------------------------------------------------------------------------- #
# Indexation des emails classés (appelée par le démon)
# --------------------------------------------------------------------------- #
def index_filed(log=mailbox_state.log) -> int:
    """Indexe les emails classés en attente. Retourne le nombre d'emails indexés."""
    import indexer

    items = mailbox_state.list_items(("filed",))
    if not items:
        return 0
    todo = []
    for it in items:
        if it.get("doc_id") and state.get_document(it["doc_id"]):
            # Déjà indexé par ailleurs (indexation manuelle complète).
            state.set_source(it["doc_id"], "mailbox")
            mailbox_state.update_item(it["id"], status="indexed")
        elif not Path(it.get("stored_path") or "").is_file():
            mailbox_state.update_item(it["id"], status="error", error="Fichier classé introuvable.")
        else:
            todo.append(it)
    if not todo:
        return 0
    try:
        indexer.run(paths=[it["stored_path"] for it in todo])
    except indexer.IndexAlreadyRunning:
        log("Indexation déjà en cours : les emails classés seront indexés au prochain passage.")
        return 0
    done = 0
    for it in todo:
        if it.get("doc_id") and state.get_document(it["doc_id"]):
            state.set_source(it["doc_id"], "mailbox")
            mailbox_state.update_item(it["id"], status="indexed")
            done += 1
        else:
            mailbox_state.update_item(it["id"], status="error", error="L'indexation de cet email a échoué.")
    log(f"{done} email(s) classé(s) indexé(s).")
    return done
