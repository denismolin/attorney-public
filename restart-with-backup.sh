#!/bin/bash
# Script Shell - Redémarrage ChromaDB avec sauvegarde
# Exécution : bash restart-with-backup.sh

set -e

echo "=== Redémarrage ChromaDB avec Sauvegarde ==="
echo ""

# Étape 1 : Backup
echo "1️⃣  Sauvegarde des données ChromaDB..."
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="chromadb-backup-$TIMESTAMP.tar.gz"

if docker exec chromadb tar czf - /chroma/chroma > "$BACKUP_FILE" 2>&1; then
    echo "✓ Backup sauvegardé : $BACKUP_FILE"
else
    echo "❌ Erreur lors du backup"
    exit 1
fi

# Étape 2 : Arrêter les conteneurs
echo ""
echo "2️⃣  Arrêt des conteneurs..."
if docker-compose down; then
    echo "✓ Conteneurs arrêtés"
else
    echo "❌ Erreur lors de l'arrêt"
    exit 1
fi

# Étape 3 : Redémarrer
echo ""
echo "3️⃣  Redémarrage avec nouvelle configuration..."
if docker-compose up -d; then
    echo "✓ Conteneurs redémarrés"
else
    echo "❌ Erreur lors du redémarrage"
    exit 1
fi

# Étape 4 : Attendre que ChromaDB démarre
echo ""
echo "4️⃣  Attente du démarrage de ChromaDB (10 secondes)..."
sleep 10

# Étape 5 : Vérifier la connexion
echo ""
echo "5️⃣  Vérification de la connexion..."
if curl -s http://localhost:8000/api/v2/heartbeat > /dev/null 2>&1; then
    echo "✓ ChromaDB en ligne!"
else
    echo "⚠️  ChromaDB ne répond pas encore. Patientez quelques secondes."
    echo "   Relancez manuellement : python indexer.py"
    exit 0
fi

# Étape 6 : Relancer l'indexation
echo ""
echo "6️⃣  Lancement de l'indexation..."
echo "   (Cette commande peut prendre 10-20 minutes)"
echo ""

python indexer.py

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ SUCCÈS ! Indexation terminée."
    echo "   Backup sauvegardé : $BACKUP_FILE"
else
    echo ""
    echo "❌ L'indexation a échoué. Consultez les logs ci-dessus."
    exit 1
fi
