# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv resolves and installs from uv.lock, so the image gets exactly the
# versions that were tested locally.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilds when the
# lockfile changes, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app.py ./
COPY static ./static

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Cloud Run injects PORT (8080 by default) and expects the container to
# listen on it; the shell form is needed so the variable is expanded.
ENV PORT=8080
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT}
