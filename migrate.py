"""Migration des bases d'un poste à l'autre — SANS repayer l'indexation.

Exporte/importe dans une seule archive `.zip` :
  - les **vecteurs ChromaDB** déjà calculés (collections `documents` + `chunks`),
    réinjectés tels quels à l'import → aucun appel d'embedding payant ;
  - la base **SQLite** `GENERATED_DIR/app.sqlite` (registre, graphe documentaire,
    enrichissement LLM, jeux d'évaluation) — snapshot cohérent via l'API backup de sqlite3 ;
  - les **fichiers générés** sous `GENERATED_DIR` : pièces jointes extraites, texte OCR ;
  - (option) les **fichiers sources** sous `SOURCE_DIR` : emails `.eml` + `uploads/`.

Dans l'archive, ces deux racines sont stockées sous les préfixes internes
`data/generated/...` et `data/sources/...` (indépendants de la config locale
SOURCE_DIR/GENERATED_DIR de la machine qui importe).

Les **secrets ne sont jamais inclus** (`.env`, `settings.json`).

Utilisation (CLI) :
    python migrate.py export [--no-sources] [--out DIR]
    python migrate.py import <archive.zip> [--yes]
    python migrate.py reorganize [--yes]

Les mêmes fonctions sont réutilisées par l'UI /admin (cf. app.py).

⚠️  Les vecteurs sont liés au modèle d'embedding qui les a produits : avant
d'importer sur un nouveau poste, configurer le **même EMBED_PROVIDER / EMBED_MODEL**
(menu /settings) que celui indiqué dans `manifest.json`.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import chromadb

from config import DATA_DIR, DB_PATH, GENERATED_DIR, ROOT, SOURCE_DIR, cfg
from rag.index import (
    CHUNKS_COLLECTION,
    DOCUMENTS_COLLECTION,
    get_client,
    get_collections,
)

SCHEMA_VERSION = 1
_PAGE = 500            # taille de page de lecture Chroma
_UPSERT_BATCH = 256    # taille de lot de réinjection Chroma

# Préfixes internes à l'archive (indépendants des chemins locaux SOURCE_DIR/GENERATED_DIR).
_ARC_GENERATED = "data/generated"
_ARC_SOURCES = "data/sources"
# Entrées à ne jamais exporter (secrets + résidus).
_EXCLUDE = {"settings.json", ".env", "index_state.db", "__pycache__"}
# Compat : noms de dossiers "générés" dans une archive pré-split (data/ à plat),
# pour router chaque entrée vers GENERATED_DIR ou SOURCE_DIR à l'import.
_LEGACY_GENERATED_DIRS = {"attachments", "extracted_text"}


def _log(msg: str, log=None) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # Console Windows en cp1252 : remplace les caractères non encodables.
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc), flush=True)
    if log:
        log(msg)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def _sqlite_snapshot(dest: Path) -> None:
    """Copie cohérente de app.sqlite (sûre même si l'app tourne)."""
    if not DB_PATH.exists():
        return
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _dump_collection_to(zf: zipfile.ZipFile, arcname: str, name: str, log=None) -> int:
    """Écrit une collection Chroma en JSONL dans l'archive. Retourne le nb d'entrées."""
    client = get_client()
    try:
        col = client.get_collection(name)
    except Exception:
        _log(f"  collection {name!r} absente — ignorée.", log)
        zf.writestr(arcname, "")
        return 0

    total = col.count()
    written = 0
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl",
                                     delete=False, newline="\n") as tmp:
        tmp_path = Path(tmp.name)
        offset = 0
        while offset < total:
            res = col.get(
                include=["embeddings", "documents", "metadatas"],
                limit=_PAGE,
                offset=offset,
            )
            ids = res.get("ids") or []
            if not ids:
                break
            embs = res.get("embeddings")
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            for i, _id in enumerate(ids):
                emb = embs[i] if embs is not None else None
                if emb is not None and hasattr(emb, "tolist"):
                    emb = emb.tolist()
                row = {
                    "id": _id,
                    "embedding": emb,
                    "document": docs[i] if i < len(docs) else None,
                    "metadata": metas[i] if i < len(metas) else None,
                }
                tmp.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
            offset += len(ids)
            _log(f"  {name}: {written}/{total}", log)
    zf.write(tmp_path, arcname)
    tmp_path.unlink(missing_ok=True)
    return written


def export_archive(
    dest_dir: Path | str | None = None,
    with_sources: bool = True,
    log=None,
) -> Path:
    """Crée une archive de migration et retourne son chemin.

    with_sources=False saute tout SOURCE_DIR (emails .eml + uploads/, archive légère
    de vérification) ; les vecteurs, le SQLite, les PJ et le texte OCR restent inclus.
    """
    dest_dir = Path(dest_dir) if dest_dir else (ROOT / "backups")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = dest_dir / f"avocat-export-{stamp}.zip"

    _log(f"Export → {archive.name}", log)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1) Vecteurs ChromaDB.
        _log("Lecture des collections ChromaDB…", log)
        n_docs = _dump_collection_to(zf, "chroma/documents.jsonl", DOCUMENTS_COLLECTION, log)
        n_chunks = _dump_collection_to(zf, "chroma/chunks.jsonl", CHUNKS_COLLECTION, log)

        # 2) SQLite (snapshot cohérent).
        _log("Snapshot SQLite…", log)
        sqlite_count = 0
        if DB_PATH.exists():
            with tempfile.TemporaryDirectory() as td:
                snap = Path(td) / "app.sqlite"
                _sqlite_snapshot(snap)
                zf.write(snap, f"{_ARC_GENERATED}/app.sqlite")
                try:
                    c = sqlite3.connect(snap)
                    sqlite_count = c.execute("SELECT count(*) FROM documents").fetchone()[0]
                    c.close()
                except Exception:
                    pass

        # 3) Fichiers générés (PJ, OCR) — toujours inclus.
        _log("Copie des fichiers générés…", log)
        if GENERATED_DIR.exists():
            for entry in sorted(GENERATED_DIR.iterdir()):
                name = entry.name
                # Saute les secrets/résidus, et tous les fichiers app.sqlite* : la base
                # est ajoutée séparément via un snapshot cohérent (évite -wal/-shm/.pre-import-*).
                if name in _EXCLUDE or name.startswith("app.sqlite"):
                    continue
                if entry.is_dir():
                    for f in entry.rglob("*"):
                        if f.is_file() and "__pycache__" not in f.parts:
                            zf.write(f, f"{_ARC_GENERATED}/{f.relative_to(GENERATED_DIR).as_posix()}")
                elif entry.is_file():
                    zf.write(entry, f"{_ARC_GENERATED}/{name}")

        # 4) Fichiers sources (emails .eml + uploads/) — optionnel (archive légère si off).
        if with_sources and SOURCE_DIR.exists():
            _log("Copie des fichiers sources…", log)
            for entry in sorted(SOURCE_DIR.iterdir()):
                name = entry.name
                if name in _EXCLUDE:
                    continue
                if entry.is_dir():
                    for f in entry.rglob("*"):
                        if f.is_file() and "__pycache__" not in f.parts:
                            zf.write(f, f"{_ARC_SOURCES}/{f.relative_to(SOURCE_DIR).as_posix()}")
                elif entry.is_file():
                    zf.write(entry, f"{_ARC_SOURCES}/{name}")

        # 4) Manifest.
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": stamp,
            "with_sources": with_sources,
            "embed_provider": cfg.embed_provider,
            "embed_model": cfg.embed_model,
            "chat_provider": cfg.chat_provider,
            "chroma_version": getattr(chromadb, "__version__", "?"),
            "counts": {
                "documents": n_docs,
                "chunks": n_chunks,
                "sqlite_documents": sqlite_count,
            },
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    _log(
        f"✓ Export terminé : {n_docs} documents, {n_chunks} chunks, "
        f"{sqlite_count} lignes SQLite → {archive}",
        log,
    )
    return archive


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def _restore_collection_from(path: Path, name: str, log=None) -> int:
    """Recrée la collection `name` et réinjecte ses vecteurs depuis le JSONL.

    Aucun recalcul d'embedding : on `upsert` les vecteurs stockés.
    """
    client = get_client()
    # Repart d'une collection propre (évite un conflit de dimension d'embedding).
    try:
        client.delete_collection(name)
    except Exception:
        pass
    col = client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})

    if not path.exists():
        return 0

    ids: list[str] = []
    embs: list = []
    docs: list = []
    metas: list = []
    restored = 0

    def _flush():
        nonlocal ids, embs, docs, metas, restored
        if not ids:
            return
        col.upsert(ids=ids, embeddings=embs, documents=docs, metadatas=metas)
        restored += len(ids)
        _log(f"  {name}: {restored} réinjectés", log)
        ids, embs, docs, metas = [], [], [], []

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("embedding") is None:
                continue
            ids.append(row["id"])
            embs.append(row["embedding"])
            docs.append(row.get("document") or "")
            metas.append(row.get("metadata") or {})
            if len(ids) >= _UPSERT_BATCH:
                _flush()
    _flush()
    return restored


def _safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extrait l'archive en se prémunissant contre le « zip-slip ».

    Un membre dont le chemin (après normalisation) sortirait de `dest` — via
    `..` ou un chemin absolu — est rejeté, pour qu'une archive malveillante ne
    puisse pas écrire en dehors du dossier temporaire.
    """
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"Entrée d'archive non sécurisée (zip-slip) : {member!r}")
    zf.extractall(dest)


def import_archive(zip_path: Path | str, assume_yes: bool = False, log=None) -> dict:
    """Restaure une archive de migration. Retourne un résumé.

    Sauvegarde l'app.sqlite courant avant écrasement. Réinjecte les vecteurs Chroma
    sans recalcul.
    """
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise FileNotFoundError(f"Archive introuvable : {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            raise ValueError("Archive invalide : manifest.json manquant.")

        warnings: list[str] = []
        man_model = manifest.get("embed_model")
        if man_model and man_model != cfg.embed_model:
            warnings.append(
                f"Le modèle d'embedding de l'archive ({man_model}) diffère de la config "
                f"actuelle ({cfg.embed_model}). Configurez le MÊME modèle via /settings "
                "pour que les recherches soient cohérentes avec les vecteurs importés."
            )
            _log("⚠ " + warnings[-1], log)

        tmp = Path(tempfile.mkdtemp(prefix="avocat-import-"))
        try:
            _safe_extractall(zf, tmp)

            # 1) Restaure les fichiers (avec backup de l'app.sqlite courant).
            GENERATED_DIR.mkdir(parents=True, exist_ok=True)
            SOURCE_DIR.mkdir(parents=True, exist_ok=True)
            if DB_PATH.exists():
                bak = DB_PATH.with_name(
                    f"app.sqlite.pre-import-{datetime.now():%Y%m%d-%H%M%S}"
                )
                shutil.copy2(DB_PATH, bak)
                _log(f"Sauvegarde de l'ancien SQLite → {bak.name}", log)

            src_generated = tmp / _ARC_GENERATED
            src_sources = tmp / _ARC_SOURCES
            if src_generated.exists() or src_sources.exists():
                # Archive au format actuel (post-split) : deux racines dédiées.
                for f in src_generated.rglob("*") if src_generated.exists() else []:
                    if f.is_file():
                        target = GENERATED_DIR / f.relative_to(src_generated)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, target)
                for f in src_sources.rglob("*") if src_sources.exists() else []:
                    if f.is_file():
                        target = SOURCE_DIR / f.relative_to(src_sources)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, target)
                _log("Fichiers restaurés (generated/ + sources/).", log)
            else:
                # Compat : archive pré-split (data/ à plat, générés + sources mélangés).
                src_data = tmp / "data"
                if src_data.exists():
                    for f in src_data.rglob("*"):
                        if not f.is_file():
                            continue
                        rel = f.relative_to(src_data)
                        top = rel.parts[0]
                        dest_root = GENERATED_DIR if top in _LEGACY_GENERATED_DIRS or top.startswith("app.sqlite") else SOURCE_DIR
                        target = dest_root / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, target)
                    _log("Fichiers restaurés depuis une archive pré-split (data/ à plat).", log)

            # 2) Réinjecte les vecteurs Chroma.
            _log("Réinjection des vecteurs ChromaDB…", log)
            n_docs = _restore_collection_from(tmp / "chroma" / "documents.jsonl",
                                              DOCUMENTS_COLLECTION, log)
            n_chunks = _restore_collection_from(tmp / "chroma" / "chunks.jsonl",
                                               CHUNKS_COLLECTION, log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    expected = manifest.get("counts", {})
    summary = {
        "manifest": manifest,
        "restored": {"documents": n_docs, "chunks": n_chunks},
        "expected": expected,
        "warnings": warnings,
        "ok": n_docs == expected.get("documents") and n_chunks == expected.get("chunks"),
    }
    _log(
        f"✓ Import terminé : {n_docs}/{expected.get('documents', '?')} documents, "
        f"{n_chunks}/{expected.get('chunks', '?')} chunks réinjectés.",
        log,
    )
    return summary


# --------------------------------------------------------------------------- #
# Réorganisation en place (installs existantes, data/ à plat -> sources/ + generated/)
# --------------------------------------------------------------------------- #
def reorganize_data_dir(log=None) -> dict:
    """Déplace le contenu existant de `data/` (à plat) vers SOURCE_DIR/GENERATED_DIR.

    Idempotent : si SOURCE_DIR/GENERATED_DIR n'existent pas encore sous DATA_DIR ou
    contiennent déjà des fichiers, les entrées restantes à la racine de DATA_DIR sont
    déplacées une par une (settings.json/.env restent exclus, cf. _EXCLUDE).
    """
    if not DATA_DIR.exists():
        _log("data/ introuvable — rien à réorganiser.", log)
        return {"moved": 0}

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    moved = 0
    for entry in sorted(DATA_DIR.iterdir()):
        name = entry.name
        # Ne touche pas aux nouvelles racines elles-mêmes, ni aux secrets/résidus.
        if entry.resolve() in (SOURCE_DIR.resolve(), GENERATED_DIR.resolve()):
            continue
        if name in _EXCLUDE:
            continue
        is_generated = name.startswith("app.sqlite") or name in _LEGACY_GENERATED_DIRS
        dest_root = GENERATED_DIR if is_generated else SOURCE_DIR
        dest = dest_root / name
        if dest.exists():
            _log(f"  ⚠ {dest} existe déjà — {name} laissé en place dans data/.", log)
            continue
        shutil.move(str(entry), str(dest))
        _log(f"  {name} -> {dest_root.name}/{name}", log)
        moved += 1

    # settings.json reste géré à part : il doit suivre GENERATED_DIR (contient des
    # secrets, jamais migré silencieusement s'il existe déjà à destination).
    legacy_settings = DATA_DIR / "settings.json"
    if legacy_settings.exists():
        dest = GENERATED_DIR / "settings.json"
        if not dest.exists():
            shutil.move(str(legacy_settings), str(dest))
            _log(f"  settings.json -> {GENERATED_DIR.name}/settings.json", log)
            moved += 1
        else:
            _log("  ⚠ settings.json existe déjà sous generated/ — ancien fichier laissé en place.", log)

    _log(f"✓ Réorganisation terminée : {moved} entrée(s) déplacée(s).", log)
    return {"moved": moved}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Migration des bases (Chroma + SQLite + fichiers).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="Crée une archive de migration.")
    p_exp.add_argument("--out", default=None, help="Répertoire de sortie (défaut : ./backups).")
    p_exp.add_argument("--no-sources", action="store_true",
                       help="N'inclut pas les emails sources .eml ni uploads/ (archive légère).")

    p_imp = sub.add_parser("import", help="Restaure une archive de migration.")
    p_imp.add_argument("archive", help="Chemin de l'archive .zip à importer.")
    p_imp.add_argument("--yes", action="store_true", help="Ne pas demander de confirmation.")

    p_reorg = sub.add_parser(
        "reorganize",
        help="Réorganise un data/ existant (à plat) vers SOURCE_DIR/GENERATED_DIR.",
    )
    p_reorg.add_argument("--yes", action="store_true", help="Ne pas demander de confirmation.")

    args = parser.parse_args(argv)

    if args.cmd == "export":
        export_archive(dest_dir=args.out, with_sources=not args.no_sources)
        return 0

    if args.cmd == "import":
        if not args.yes:
            print(
                "⚠ L'import va ÉCRASER la base SQLite et les collections ChromaDB "
                "actuelles (une sauvegarde de l'ancien SQLite est faite).\n"
                "Relancez avec --yes pour confirmer."
            )
            return 1
        import_archive(args.archive, assume_yes=True)
        return 0

    if args.cmd == "reorganize":
        if not args.yes:
            print(
                "⚠ Va déplacer le contenu existant de data/ vers "
                f"{SOURCE_DIR} et {GENERATED_DIR}.\n"
                "Arrêtez l'application avant de lancer cette commande.\n"
                "Relancez avec --yes pour confirmer."
            )
            return 1
        reorganize_data_dir()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
