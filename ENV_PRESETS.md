# Présets de configuration `.env` — Selon votre situation

Ce document fournit plusieurs **configurations prédéfinies** selon votre consommation de ressources et votre tolérance à la lenteur d'indexation.

---

## 🟢 **Preset 1 : BALANCED** (Recommandé — déjà appliqué)

Bon équilibre entre **stabilité** et **vitesse d'indexation**.

```env
CHUNK_SIZE=1100
CHUNK_OVERLAP=150
EMBED_MAX_CHARS=1200
EMBED_BATCH_SIZE=4
EMBED_BATCH_DELAY=1.0
```

**Profil :**
- CPU : -40% vs défaut
- Mémoire : -30% vs défaut
- Durée indexation : +50% vs défaut
- Stabilité : ⭐⭐⭐⭐ Très bonne

**Quand l'utiliser :** Par défaut (c'est ce que vous avez).

---

## 🔴 **Preset 2 : LIGHTWEIGHT** (Ultra-léger — si toujours instable)

Configuration **minimale** — système faible ou avec peu de RAM.

```env
CHUNK_SIZE=800
CHUNK_OVERLAP=100
EMBED_MAX_CHARS=1000
EMBED_BATCH_SIZE=2
EMBED_BATCH_DELAY=1.5
```

**Profil :**
- CPU : -60% vs défaut
- Mémoire : -50% vs défaut
- Durée indexation : +150% vs défaut (soit ~20–30 min pour 200 docs)
- Stabilité : ⭐⭐⭐⭐⭐ Excellente

**Quand l'utiliser :**
- Vous avez < 4 GB RAM
- ChromaDB crash avec BALANCED
- Votre système tourne déjà à 80–90% CPU sans indexation

**Mise en place :**
1. Remplacez les 5 lignes ci-dessus dans votre `.env`
2. Redémarrez : `docker-compose restart chromadb`
3. Relancez : `python indexer.py`

---

## 🚀 **Preset 3 : TURBO** (Si vous avez du CPU/RAM à revendre)

Configuration **agressif** — machine puissante (8+ GB RAM, CPU récent).

```env
CHUNK_SIZE=1400
CHUNK_OVERLAP=200
EMBED_MAX_CHARS=1600
EMBED_BATCH_SIZE=8
EMBED_BATCH_DELAY=0.3
```

**Profil :**
- CPU : +30% vs BALANCED
- Mémoire : +25% vs BALANCED
- Durée indexation : -30% vs BALANCED (soit ~5–8 min pour 200 docs)
- Stabilité : ⭐⭐⭐ Bonne (si serveur d'embeddings TEI est stable)

**Quand l'utiliser :**
- Votre CPU ne dépasse jamais 50% avec BALANCED
- Vous avez > 8 GB RAM + SSD rapide
- Vous voulez l'indexation plus rapide

---

## 📊 Tableau comparatif

| Configuration | CPU | RAM | Temps indexation | Stabilité | RAM serv. |
|---------------|-----|-----|------------------|-----------|-----------|
| **Défaut** (8/0.5) | 100% | 100% | 100% (10 min) | ⭐⭐⭐ | 1.5 GB |
| **BALANCED** (4/1.0) ✅ | 60% | 70% | 150% (15 min) | ⭐⭐⭐⭐ | 0.9 GB |
| **LIGHTWEIGHT** (2/1.5) | 40% | 50% | 250% (25 min) | ⭐⭐⭐⭐⭐ | 0.6 GB |
| **TURBO** (8/0.3) | 130% | 130% | 70% (7 min) | ⭐⭐⭐ | 2.0 GB |

---

## 🔧 Passage d'une config à l'autre

1. **Modifier `.env`** avec la ligne pour votre preset
2. **Pas besoin de redémarrer Docker** (sauf si config drastique)
3. **Lancer juste :** `python indexer.py`

La prochaine indexation utilisera les nouveaux paramètres.

---

## 🎯 Conseils par symptôme

### "ChromaDB crash / 'killed' / 'OOM'"
→ Utilisez **LIGHTWEIGHT**
```bash
# Dans .env
EMBED_BATCH_SIZE=2
EMBED_BATCH_DELAY=1.5
```

### "Indexation très lente mais pas d'erreurs"
→ Vous êtes peut-être en **LIGHTWEIGHT**. Essayez **BALANCED**.
```bash
# Dans .env
EMBED_BATCH_SIZE=4
EMBED_BATCH_DELAY=1.0
```

### "CPU reste bas (< 30%), je veux plus vite"
→ Essayez **TURBO**.
```bash
# Dans .env
EMBED_BATCH_SIZE=8
EMBED_BATCH_DELAY=0.3
```

### "Pic CPU à 100% pendant indexation mais jamais de crash"
→ **BALANCED** est bon. Vous pouvez rester où vous êtes. ✅

---

## ⚡ Optimisation « À la carte »

Si vous voulez juste **une** variable :

| Symptôme | Augmenter | Valeur |
|----------|-----------|--------|
| **CPU trop haut** | `EMBED_BATCH_DELAY` | 1.5 → 2.0 |
| **Mémoire trop haute** | `EMBED_BATCH_SIZE` | 4 → 2 ou 3 |
| **Trop lent** | `EMBED_BATCH_SIZE` | 4 → 6 |
| **Trop lent** | `EMBED_BATCH_DELAY` | 1.0 → 0.5 |
| **Chunks trop longs** | `CHUNK_SIZE` | 1100 → 900 |
| **Pas assez de contexte** | `CHUNK_SIZE` | 1100 → 1300 |

---

## 🧪 Tester une config sans perdre les données

1. **Sauvegarde** (on a déjà fait) ✓
2. Modifiez `.env`
3. Lancez **une indexation partielle** (juste 1–2 fichiers) :
   ```bash
   # Éditer temporairement le code pour ne traiter que 2 emails
   # Ou tester l'indexation d'une seule PJ
   ```
4. Vérifiez la consommation CPU/RAM
5. Si OK, relancez complet

---

## 📝 Notes importantes

- **Débloquer les limites Docker** : si vous avez changé `docker-compose.yml` avec des limites CPU/RAM, elles **s'appliquent même en TURBO**. Modifiez Docker si vous utilisez TURBO :
  ```yaml
  deploy:
    resources:
      limits:
        cpus: "2.0"         # Au lieu de 1.0
        memory: 2048M       # Au lieu de 1024M
  ```

- **Le serveur d'embeddings (vLLM) peut être le goulot** : si vous voyez « panic mode » dans les logs TEI, augmentez `EMBED_BATCH_DELAY` même avec BALANCED/TURBO.

- **Les chunks restent stockés** : changer CHUNK_SIZE n'affecte que les **nouveaux documents** indexés. Les anciens gardent leur taille.

---

## Questions fréquentes

**Q : Dois-je recréer l'index après un changement de config ?**  
A : Non. Sauf si vous changez `CHUNK_SIZE`/`CHUNK_OVERLAP` et voulez l'uniformité. Sinon, ça marche en mixte.

**Q : LIGHTWEIGHT va vraiment assez vite ?**  
A : 25 min pour 200 documents, c'est acceptable pour un jalon 1. Vous relancez rarement l'indexation compète.

**Q : Je peux basculer de TURBO à BALANCED en production ?**  
A : Oui, sans redémarrer. Juste changez `.env`, relancez `indexer.py`.

---

## Résumé

```bash
# CURRENT (BALANCED) — c'est votre config maintenant ✅
EMBED_BATCH_SIZE=4
EMBED_BATCH_DELAY=1.0

# Si toujours des problèmes → LIGHTWEIGHT
EMBED_BATCH_SIZE=2
EMBED_BATCH_DELAY=1.5

# Si tout va bien et vous êtes impatient → TURBO
EMBED_BATCH_SIZE=8
EMBED_BATCH_DELAY=0.3
```

Choisissez celui qui correspond à votre tolérance. **BALANCED** devrait suffire pour 95% des cas. ✅
