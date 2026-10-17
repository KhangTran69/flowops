FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system flowops \
    && useradd --system --gid flowops --home-dir /app flowops

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=flowops:flowops src ./src
COPY --chown=flowops:flowops data/.gitkeep ./data/.gitkeep
COPY --chown=flowops:flowops alembic.ini ./
COPY --chown=flowops:flowops migrations ./migrations
COPY --chown=flowops:flowops scripts/entrypoint.sh /usr/local/bin/flowops-entrypoint
RUN chmod +x /usr/local/bin/flowops-entrypoint \
    && mkdir -p /app/data \
    && chown -R flowops:flowops /app

USER flowops

EXPOSE 8000

ENTRYPOINT ["flowops-entrypoint"]
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
