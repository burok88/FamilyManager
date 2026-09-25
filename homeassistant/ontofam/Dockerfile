# Copyright (c) 2026. Erstellt via Vibe Coding. Nur für private und nicht-kommerzielle Nutzung.
# Debian statt des ueblichen Alpine-Basisimages: Kuzu gibt es nur fuer glibc
# (manylinux), nicht fuer musl. python:slim bringt tzdata fuer die Ortszeit mit.
FROM python:3.13-slim-bookworm

ARG BUILD_VERSION=dev
LABEL io.hass.type="addon" io.hass.arch="amd64" io.hass.version="${BUILD_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt waitress==3.0.2

COPY app/ /app/

CMD ["python", "/app/start.py"]
