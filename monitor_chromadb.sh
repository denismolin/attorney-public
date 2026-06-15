#!/bin/bash
# Script de monitoring — affiche la consommation CPU/mémoire du conteneur ChromaDB en temps réel.
# Exécution : bash monitor_chromadb.sh

echo "=== Monitoring ChromaDB - CPU / Mémoire ==="
echo "Ctrl+C pour arrêter."
echo ""

watch -n 1 'docker stats chromadb --no-stream | tail -1'
