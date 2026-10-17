FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system flowops \
    && useradd --system --gid flowops --home-dir /app flowops

WORKDIR /app
COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=flowops:flowops src ./src
COPY --chown=flowops:flowops data/.gitkeep ./data/.gitkeep
COPY --chown=flowops:flowops alembic.ini ./
COPY --chown=flowops:flowops migrations ./migrations
COPY --chown=flowops:flowops tests ./tests
COPY --chown=flowops:flowops scripts/entrypoint.sh /usr/local/bin/flowops-entrypoint
RUN chmod +x /usr/local/bin/flowops-entrypoint \
    && mkdir -p /app/data \
    && chown -R flowops:flowops /app

USER flowops

EXPOSE 8000

ENTRYPOINT ["flowops-entrypoint"]
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
