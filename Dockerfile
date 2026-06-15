# Jalon 1 — image de l'app Flask (python:3.12-slim).
# Jalon 2 ajoutera : tesseract-ocr (fra/ita/eng) + ghostscript pour l'OCR et camelot.
FROM python:3.12-slim

WORKDIR /app

# Dépendances Python d'abord (cache de build)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif
COPY . .

EXPOSE 5000

CMD ["gunicorn", "--workers=2", "--threads=4", "--bind=0.0.0.0:5000", "--timeout=300", "app:app"]
