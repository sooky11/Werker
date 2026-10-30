FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WERKER_DB=/data/werker.db \
    WERKER_PRICES_FILE=/app/prices.example.yaml

RUN groupadd --system --gid 1000 werker \
    && useradd --system --uid 1000 --gid werker --home-dir /app --shell /usr/sbin/nologin werker \
    && mkdir -p /data \
    && chown werker:werker /data

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY prices.example.yaml ./
RUN pip install ".[gmail,outlook]" \
    && rm -rf /app/src /app/build /app/*.egg-info

USER werker

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=3).close()"

CMD ["werker", "serve", "--host", "0.0.0.0", "--port", "8000"]
