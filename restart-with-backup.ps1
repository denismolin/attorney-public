# Script PowerShell - Redémarrage ChromaDB avec sauvegarde
# Exécution : .\restart-with-backup.ps1

Write-Host "=== Redémarrage ChromaDB avec Sauvegarde ===" -ForegroundColor Green
Write-Host ""

# Étape 1 : Backup
Write-Host "1️⃣  Sauvegarde des données ChromaDB..." -ForegroundColor Cyan
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupFile = "chromadb-backup-$timestamp.tar.gz"

try {
    docker exec chromadb tar czf - /chroma/chroma | Out-File $backupFile -AsByteStream -ErrorAction Stop
    Write-Host "✓ Backup sauvegardé : $backupFile" -ForegroundColor Green
} catch {
    Write-Host "❌ Erreur lors du backup : $_" -ForegroundColor Red
    exit 1
}

# Étape 2 : Arrêter les conteneurs
Write-Host ""
Write-Host "2️⃣  Arrêt des conteneurs..." -ForegroundColor Cyan
docker-compose down

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Erreur lors de l'arrêt" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Conteneurs arrêtés" -ForegroundColor Green

# Étape 3 : Redémarrer
Write-Host ""
Write-Host "3️⃣  Redémarrage avec nouvelle configuration..." -ForegroundColor Cyan
docker-compose up -d

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Erreur lors du redémarrage" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Conteneurs redémarrés" -ForegroundColor Green

# Étape 4 : Attendre que ChromaDB démarre
Write-Host ""
Write-Host "4️⃣  Attente du démarrage de ChromaDB (10 secondes)..." -ForegroundColor Cyan
Start-Sleep -Seconds 10

# Étape 5 : Vérifier la connexion
Write-Host ""
Write-Host "5️⃣  Vérification de la connexion..." -ForegroundColor Cyan
try {
    $heartbeat = Invoke-WebRequest -Uri "http://localhost:8000/api/v2/heartbeat" -ErrorAction Stop
    Write-Host "✓ ChromaDB en ligne!" -ForegroundColor Green
} catch {
    Write-Host "⚠️  ChromaDB ne répond pas encore. Patientez quelques secondes." -ForegroundColor Yellow
    Write-Host "   Relancez manuellement : python indexer.py" -ForegroundColor Yellow
    exit 0
}

# Étape 6 : Relancer l'indexation
Write-Host ""
Write-Host "6️⃣  Lancement de l'indexation..." -ForegroundColor Cyan
Write-Host "   (Cette commande peut prendre 10-20 minutes)" -ForegroundColor Gray
Write-Host ""

python indexer.py

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✅ SUCCÈS ! Indexation terminée." -ForegroundColor Green
    Write-Host "   Backup sauvegardé : $backupFile" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "❌ L'indexation a échoué. Consultez les logs ci-dessus." -ForegroundColor Red
}
