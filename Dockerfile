ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TWIPSYBOT_UP_MODE=foreground

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml README.md LICENSE ./
RUN uv pip install --system -r pyproject.toml

COPY twipsybot /app/twipsybot
COPY plugins /app/plugins
RUN uv pip install --system --no-deps . && \
    useradd -r -u 10001 -m -U -s /usr/sbin/nologin appuser && \
    mkdir -p /app/logs /app/data && \
    chown -R appuser:appuser /app/logs /app/data

USER appuser

ENTRYPOINT ["twipsybot"]
CMD ["up"]
