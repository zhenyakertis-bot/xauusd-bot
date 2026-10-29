# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 1000 bot

COPY pyproject.toml ./
COPY app ./app
# Optional extra CA (only for build environments behind a TLS-inspecting proxy):
#   docker build --secret id=extra_ca,src=/path/ca.crt .
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install .

COPY config ./config

RUN mkdir -p /app/data /app/logs /app/cache && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "app.main"]
