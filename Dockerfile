# Image de l'application (python:3.12-slim) : uniquement le code, aucune donnée.
# Ce qui entre dans l'image est borné par .dockerignore (ni data/, ni .env, ni archives).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dépendances Python d'abord (cache de build)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif : appartient à root et n'est pas modifiable par l'application, qui
# tourne sous un compte sans privilèges (cf. docker-entrypoint.sh).
COPY . .
RUN chmod 0755 /app/docker-entrypoint.sh \
    && mkdir -p /app/data/sources /app/data/generated /app/backups

EXPOSE 5000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "--workers=2", "--threads=4", "--bind=0.0.0.0:5000", "--timeout=300", "app:app"]
