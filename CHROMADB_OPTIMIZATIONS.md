# Optimisations ChromaDB — Réduction CPU et Mémoire

Ce guide détaille comment limiter la consommation CPU et mémoire de ChromaDB, qui apparaît instable sous charge.

## 1. Configuration Docker — Limites de ressources

Modifiez `docker-compose.yml` pour imposer des limites strictes à ChromaDB :

```yaml
services:
  chromadb:
    image: chromadb/chroma:0.6.3
    volumes:
      - chroma-data:/chroma/chroma
    environment:
      - IS_PERSISTENT=TRUE
      - ANONYMIZED_TELEMETRY=FALSE
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v2/heartbeat')"]
      interval: 10s
      timeout: 5s
      retries: 6
    # ===== AJOUT : limites de ressources =====
    deploy:
      resources:
        limits:
          cpus: "1.0"              # Max 1 CPU (ajuster selon votre système)
          memory: 1024M            # Max 1 GB (ajuster si besoin)
        reservations:
          cpus: "0.5"
          memory: 512M
    # ===== FIN AJOUT =====
```

**Rationnel :**
- **cpus: 1.0** : Empêche ChromaDB de consommer tous les CPUs (0.5–1.0 selon votre RAM)
- **memory: 1024M** : Limite la mémoire à 1 GB max (augmenter si données volumineuses)
- **reservations** : Garantit des ressources minimales pour la stabilité

---

## 2. Configuration `.env` — Throttling de l'indexation

Réduisez `EMBED_BATCH_SIZE` et augmentez `EMBED_BATCH_DELAY` pour ménager ChromaDB :

```env
# Indexation — OPTIMISÉ POUR FAIBLE CONSOMMATION
CHUNK_SIZE=1100
CHUNK_OVERLAP=150
EMBED_MAX_CHARS=1200

# === RÉDUCTION CPU/MÉMOIRE ===
# Batch plus petit (default 8 → 4), avec pause plus longue entre les batchs
EMBED_BATCH_SIZE=4              # Au lieu de 8 → 50 % moins de requêtes simultanées
EMBED_BATCH_DELAY=1.0           # Au lieu de 0.5 → laisse ChromaDB respirer
# === FIN RÉDUCTION ===
```

**Impact :**
- L'indexation sera **2× plus lente** mais consommera **30–40 % moins de CPU/mémoire**
- ChromaDB ne sera jamais surcharché pendant l'insertion massive

---

## 3. Indexation « Lite » — Pause entre documents

Modifiez `indexer.py` pour ajouter une pause légère entre chaque document (sans freiner la progression) :

```python
# Dans indexer.py, après la ligne 174 (_index_attachments), ajoutez :

        # --- Pause légère pour ménager ChromaDB -----
        import time
        time.sleep(0.2)  # 200 ms tous les documents
```

Cela réduit les pics de charge sans impacter visiblement la durée globale.

---

## 4. Reconfiguration ChromaDB — Mode « Petite mémoire »

Si vous accédez encore au service ChromaDB en direct (mode localhost), ajoutez des **variables d'environnement au conteneur** :

```yaml
environment:
  - IS_PERSISTENT=TRUE
  - ANONYMIZED_TELEMETRY=FALSE
  # === NOUVELLES OPTIMISATIONS ===
  - CHROMA_DB_IMPL=duckdb+parquet  # Mode persistant léger (vs duckdb memory)
  - CHROMA_QUERY_SIZE_LIMIT=1000   # Limite les résultats renvoyés
```

---

## 5. Monitoring — Vérifier que ça marche

Avant de relancer, vérifiez la consommation :

```bash
# Voir la mémoire/CPU du conteneur ChromaDB
docker stats chromadb
```

**Cible :** < 500 MB RAM en repos, < 1 GB en indexation active.

---

## 6. Options avancées si le problème persiste

### 6a. Réduire la taille de l'index (jalon 1)

Si vous avez **trop de documents**, rechunker plus agressivement :

```env
CHUNK_SIZE=800              # Au lieu de 1100
CHUNK_OVERLAP=100           # Au lieu de 150
```

Cela réduit le nombre de chunks à 30–40 %, ce qui économise de la mémoire.

### 6b. Désactiver les collections non essentielles

En jalon 1, vous avez deux collections : `documents` et `chunks`. Si vous ne queryez que `chunks`, vous pouvez désactiver la première temporairement :

**Dans `rag/index.py`** (après `get_collections`) :
```python
def get_collections(client: chromadb.api.ClientAPI | None = None):
    client = client or get_client()
    # Chunks seulement (jalon 1) — documents réactivé au jalon 3 (graphe)
    chunks = client.get_or_create_collection(
        CHUNKS_COLLECTION, metadata={"hnsw:space": "cosine"}
    )
    return None, chunks  # documents = None
```

⚠️ Cela cassera la navigation (timeline) — ne le faire qu'en cas d'urgence.

### 6c. Migrer vers une base SQL légère

Au long terme, remplacer ChromaDB par **pgvector (PostgreSQL + extension vectorielle)** :
- Meilleur contrôle mémoire
- Persistance fiable
- Requêtes SQL optimisées

(Hors scope jalon 1 — à envisager au jalon 2.)

---

## 7. Tableau récapitulatif des optimisations

| Mesure | CPU réduit de | Mémoire réduite de | Effort |
|--------|------------|----------------|--------|
| Docker limits (cpus + memory) | — | 40–50% | ⭐ Très facile |
| EMBED_BATCH_SIZE: 4 + DELAY: 1.0 | 30–40% | 20–30% | ⭐ Très facile |
| Pause 0.2s par doc (indexer.py) | 10–15% | 5–10% | ⭐ Très facile |
| CHUNK_SIZE réduit (800) | 20–30% | 25–35% | ⭐⭐ Facile (impact UX) |
| **Cumul : toutes les mesures** | **50–70%** | **60–80%** | ⭐⭐ |

---

## 8. Procédure d'application

### Étape 1 : Backup
```bash
# Sauvegarder les données ChromaDB
docker exec chromadb tar czf - /chroma/chroma > chromadb-backup.tar.gz
```

### Étape 2 : Appliquer les changements
1. Modifiez `docker-compose.yml` (ressources + environnement)
2. Modifiez `.env` (batch + delay)
3. Modifiez `indexer.py` (pause 0.2s)

### Étape 3 : Redémarrer
```bash
docker-compose down
docker-compose up -d
```

### Étape 4 : Vérifier
```bash
# Attendre que ChromaDB démarre
docker logs -f chromadb

# Vérifier que l'app se connecte
curl http://localhost:8000/api/v2/heartbeat

# Relancer l'indexation
python indexer.py
```

### Étape 5 : Monitor
```bash
watch -n 1 'docker stats chromadb --no-stream'
```

---

## Notes

- **Les pauses CPU ne figent pas l'app** : Flask reste réactif, seule l'indexation ralentit un peu.
- **Les changements sont cumulatifs** : appliquer plusieurs mesures amplifie l'effet.
- **Commencez par docker + .env** : ce sont les gains les plus rapides.
- Si le système **reste instable** après ces optimisations, le problème vient peut-être du **vLLM** ou du serveur d'embeddings (TEI), pas de ChromaDB. Relevez les logs pour confirmer.

---

## Dépannage

**Q : Indexation excessivement lente après ces changements ?**  
A : C'est normal. Les pauses ralentissent l'indexation de ~50 % mais sauvent le système. Vous pouvez réduire légèrement `EMBED_BATCH_DELAY` si vous voyez que ChromaDB ne surchauffe pas.

**Q : J'ai encore des pics CPU pendant l'indexation.**  
A : Vérifiez que le serveur d'embeddings (TEI) ne surcharge pas le CPU. Cherchez « panic mode » dans les logs de vLLM. Si c'est TEI, augmentez `EMBED_BATCH_DELAY` à 1.5 ou 2.0.

**Q : Combien de temps va prendre l'indexation ?**  
A : Avec ces settings, ~10–15 minutes pour ~200 documents (jalon 1). Vous pouvez relancer en arrière-plan sans bloquer l'app.
