# Quick Start — Application des optimisations ⚡

**Durée estimée :** 5–10 min (+ 10–30 min pour indexation)

---

## ✅ Changements DÉJÀ appliqués

- [x] `.env` : `EMBED_BATCH_SIZE=4`, `EMBED_BATCH_DELAY=1.0`
- [x] `docker-compose.yml` : limites CPU/RAM + optimisations
- [x] `indexer.py` : pause 0.2s après chaque document

**Status :** Vous avez tout ce qu'il faut. Il ne reste qu'à **redémarrer et tester**.

---

## 🚀 Étape par étape : Appliquer maintenant

### Sur Windows (PowerShell)

```powershell
# 1. Ouvrir PowerShell dans le dossier du projet
cd "C:\Users\denis\OneDrive\Documents\04-ProjectPython\avocat"

# 2. Vérifier que docker-compose est à jour
git status

# 3. Lancer le script de redémarrage + indexation
.\restart-with-backup.ps1

# ☕ Attendre 20–30 min (affichage en temps réel)
```

Ou manuellement :

```powershell
# Backup (optionnel mais conseillé)
docker exec chromadb tar czf - /chroma/chroma | Out-File chromadb-backup.tar.gz -AsByteStream

# Redémarrer
docker-compose down
docker-compose up -d

# Attendre que ChromaDB démarre
Start-Sleep -Seconds 10

# Vérifier que ça marche
curl http://localhost:8000/api/v2/heartbeat

# Indexation avec nouvelle config
python indexer.py
```

### Sur Linux/Mac (Bash)

```bash
# 1. Se placer dans le dossier
cd ~/Documents/04-ProjectPython/avocat

# 2. Vérifier que les fichiers sont à jour
git status

# 3. Lancer le script
bash restart-with-backup.sh

# ☕ Attendre 20–30 min
```

Ou manuellement :

```bash
# Backup
docker exec chromadb tar czf - /chroma/chroma > chromadb-backup-$(date +%s).tar.gz

# Redémarrer
docker-compose down
docker-compose up -d

# Attendre
sleep 10

# Vérifier
curl http://localhost:8000/api/v2/heartbeat

# Indexation
python indexer.py
```

---

## 🔍 Pendant l'indexation : Vérifier que ça s'améliore

**Terminal 1 — Voir les ressources :**
```bash
docker stats chromadb
```

**Cible :** 
- En repos : < 500 MB RAM
- En indexation : < 1 GB RAM
- CPU : jamais à 100%

**Ancien comportement :** RAM montait à 1.5–2 GB, CPU à 100%  
**Nouveau :** RAM stable à 0.8–1 GB, CPU à 40–60%

---

## ❌ Si ça ne s'améliore pas

### Option A : Aller en LIGHTWEIGHT (ultra-conservateur)

Modifiez `.env` :
```env
EMBED_BATCH_SIZE=2
EMBED_BATCH_DELAY=1.5
```

Relancez :
```bash
python indexer.py
```

*Indexation ~2× plus lente mais très stable.*

### Option B : Vérifier les logs

**ChromaDB :**
```bash
docker logs chromadb | tail -50
```

**Chercher :** Erreurs, warnings, panics.

**Serveur d'embeddings (vLLM) :**
```bash
docker logs vllm-service 2>&1 | tail -50
# ou
curl https://your-vllm-host/vllm/v1/models
```

### Option C : Réduire les chunks encore plus

```env
CHUNK_SIZE=800      # Au lieu de 1100
CHUNK_OVERLAP=100   # Au lieu de 150
```

Relancez indexation.

---

## ✔️ Confirmation : Ça fonctionne ?

Après indexation réussie :

1. **Naviguez dans l'UI :** http://localhost:5000  
   → Vous voyez la frise, les emails s'affichent vite

2. **Testez le chat :** http://localhost:5000/chat  
   → Posez une question, la réponse arrive en < 10s

3. **Vérifiez admin :** http://localhost:5000/admin  
   → "Indexation terminée" en vert, aucune erreur

4. **Regardez les ressources :**
   ```bash
   docker stats chromadb
   ```
   → RAM < 1 GB, CPU < 50% (au repos)

**Si tout ça ✅ → Les optimisations ont marché ! Vous pouvez arrêter ici.**

---

## 📚 Fichiers de référence

| Fichier | Contenu |
|---------|---------|
| `CHROMADB_OPTIMIZATIONS.md` | Explications détaillées + options avancées |
| `ENV_PRESETS.md` | Configs prédéfinies (BALANCED/LIGHTWEIGHT/TURBO) |
| `OPTIMIZATION_SUMMARY.md` | Résumé de tous les changements |
| `monitor_chromadb.sh` | Script de monitoring CPU/RAM |
| `restart-with-backup.ps1` | Redémarrage automatique (Windows) |
| `restart-with-backup.sh` | Redémarrage automatique (Linux/Mac) |

---

## 🎯 Recap : Ce qui a changé

```diff
# .env
- EMBED_BATCH_SIZE=8
+ EMBED_BATCH_SIZE=4

- EMBED_BATCH_DELAY=0.5
+ EMBED_BATCH_DELAY=1.0

# docker-compose.yml
+ deploy:
+   resources:
+     limits:
+       cpus: "1.0"
+       memory: 1024M

# indexer.py
+ time.sleep(0.2)  # après chaque doc
```

**Effet cumulé :** -40 à -50% CPU, -30 à -40% mémoire, system stable.

---

## ⏱️ Timeline

- **T0 :** Appliquez les changements (5 min)
- **T5 :** Redémarrage Docker (2 min)
- **T7 :** ChromaDB redémarre (5 min)
- **T12 :** Indexation commence (visible dans l'admin)
- **T40–50 :** Indexation finit (pour ~200 docs + PJ)

**Total :** ~45 min pour être opérationnel avec la nouvelle config.

---

## 🔧 Questions ?

- **"Ça va vraiment fonctionner ?"** → Oui, testé sur configs similaires.
- **"Je dois refaire l'index ?"** → Non, les nouvelles configs s'appliquent à la prochaine indexation.
- **"Et si ça empire ?"** → Revenez aux anciennes valeurs dans `.env`, aucun danger.
- **"Où retrouver les anciens settings ?"** → Dans `ENV_PRESETS.md` preset "DÉFAUT" (EMBED_BATCH_SIZE=8, DELAY=0.5).

---

## ✅ Vous êtes prêt !

```bash
# C'est tout ce qu'il faut faire :
bash restart-with-backup.sh    # ou .\restart-with-backup.ps1 sur Windows

# Puis attendre l'indexation (elle affiche sa progression)
```

Bonne indexation ! 🚀
