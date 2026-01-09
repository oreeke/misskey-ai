FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    python -m venv /opt/venv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd -r botuser && useradd -r -g botuser botuser && \
    mkdir -p /app/logs /app/data && \
    chown -R botuser:botuser /app/logs /app/data && \
    chmod 0750 /app/logs /app/data && \
    chmod 0555 /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=root:root --chmod=0555 src /app/src
COPY --chown=root:root --chmod=0555 plugins /app/plugins
COPY --chown=root:root --chmod=0444 run.py /app/run.py

USER botuser

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
    CMD python -c "from src.shared.utils import health_check; exit(0 if health_check() else 1)"

CMD ["python", "run.py"]
