#!/bin/sh
# Point d'entrée de l'image : l'application ne tourne pas en root.
#
# Le conteneur démarre en root uniquement le temps d'aligner les droits des dossiers de
# données, puis abandonne ses privilèges avant de lancer l'application.
#
# Compte utilisé, par ordre de priorité :
#   1. APP_UID / APP_GID si définis ;
#   2. le propriétaire du dossier de données monté (les fichiers restent lisibles et
#      modifiables depuis l'hôte) ;
#   3. à défaut (dossiers appartenant à root, cas d'une installation antérieure ou d'un
#      volume neuf) : le compte 10001.
# Les fichiers créés en root par une version antérieure sont réattribués à ce compte.
#
# RUN_AS_ROOT=1 conserve l'ancien comportement (déconseillé).
set -e

if [ "$(id -u)" != "0" ] || [ "${RUN_AS_ROOT:-0}" = "1" ]; then
  exec "$@"
fi

GEN="${GENERATED_DIR:-/app/data/generated}"
SRC="${SOURCE_DIR:-/app/data/sources}"
BAK="/app/backups"
mkdir -p "$GEN" "$SRC" "$BAK" 2>/dev/null || true

uid="${APP_UID:-}"
gid="${APP_GID:-}"
if [ -z "$uid" ]; then
  for d in "$GEN" "$SRC" "$(dirname "$GEN")"; do
    u="$(stat -c %u "$d" 2>/dev/null || echo 0)"
    if [ "$u" != "0" ]; then
      uid="$u"
      gid="$(stat -c %g "$d")"
      break
    fi
  done
fi
uid="${uid:-10001}"
gid="${gid:-$uid}"

for d in "$GEN" "$SRC" "$BAK"; do
  [ -d "$d" ] || continue
  find "$d" ! -user "$uid" -exec chown "$uid:$gid" {} + 2>/dev/null || true
done

export HOME=/tmp
if setpriv --reuid="$uid" --regid="$gid" --clear-groups test -w "$GEN" 2>/dev/null; then
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
fi

echo "[entrypoint] ATTENTION : $GEN n'est pas accessible en écriture au compte $uid ;" \
     "l'application démarre en root. Corrigez les droits du dossier de données." >&2
exec "$@"
