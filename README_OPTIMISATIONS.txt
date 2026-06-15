═══════════════════════════════════════════════════════════════════════════════
  OPTIMISATIONS ChromaDB — RÉDUCTION CPU ET MÉMOIRE
═══════════════════════════════════════════════════════════════════════════════

✅ CHANGEMENTS APPLIQUÉS :

1. .env
   • EMBED_BATCH_SIZE : 8 → 4 (moins de requêtes simultanées)
   • EMBED_BATCH_DELAY : 0.5 → 1.0 (pause plus longue)

2. docker-compose.yml
   • Limites CPU : max 1.0 (empêche la surcharge)
   • Limites RAM : max 1024M (1 GB)
   • Optimisations : duckdb+parquet, CHROMA_QUERY_SIZE_LIMIT

3. indexer.py
   • Pause 0.2s après chaque document (lisse les pics)

───────────────────────────────────────────────────────────────────────────────

🚀 COMMENT APPLIQUER :

Windows (PowerShell) :
  .\restart-with-backup.ps1

Linux/Mac (Bash) :
  bash restart-with-backup.sh

Ou manuellement :
  docker-compose down
  docker-compose up -d
  sleep 10
  python indexer.py

───────────────────────────────────────────────────────────────────────────────

📊 RÉSULTATS ATTENDUS :

Avant :     RAM 1.5-2 GB, CPU 100%, instabilité
Après :     RAM < 1 GB,   CPU 40-60%, stable

Temps indexation : +50% (normal, le throttling ralentit mais stabilise)

───────────────────────────────────────────────────────────────────────────────

🔍 VÉRIFIER QUE ÇA FONCTIONNE :

Terminal 1 - Voir les ressources en temps réel :
  docker stats chromadb

Terminal 2 - Lancer l'indexation :
  python indexer.py

Cible : RAM < 500 MB au repos, < 1 GB en indexation

───────────────────────────────────────────────────────────────────────────────

❌ SI ÇA NE SUFFIT PAS (rare) :

Option 1 - Mode LIGHTWEIGHT (ultra-conservative) :
  Dans .env, changer :
    EMBED_BATCH_SIZE=2
    EMBED_BATCH_DELAY=1.5
  Relancer : python indexer.py

Option 2 - Réduire les chunks :
  Dans .env :
    CHUNK_SIZE=800
    CHUNK_OVERLAP=100
  Relancer : python indexer.py

Option 3 - Vérifier les logs :
  docker logs chromadb
  docker logs vllm-service

───────────────────────────────────────────────────────────────────────────────

📚 FICHIERS DE RÉFÉRENCE :

• APPLY_OPTIMIZATIONS.md         ← START HERE (quick start)
• CHROMADB_OPTIMIZATIONS.md      ← Explications détaillées
• ENV_PRESETS.md                 ← Différentes configs prédéfinies
• OPTIMIZATION_SUMMARY.md        ← Résumé de tous les changements
• monitor_chromadb.sh            ← Script de monitoring

───────────────────────────────────────────────────────────────────────────────

⏱️  TIMELINE :

5-10 min  : appliquer les changements
2 min     : redémarrage Docker
5 min     : démarrage de ChromaDB
30 min    : indexation (~200 documents)
───────────
~45 min total jusqu'à être opérationnel

───────────────────────────────────────────────────────────────────────────────

✔️  C'EST TOUT !

Les optimisations sont déjà appliquées. Relancez simplement :

  docker-compose down && docker-compose up -d && sleep 10 && python indexer.py

Puis patientez. Vous allez voir une grosse différence. 🚀

═══════════════════════════════════════════════════════════════════════════════
