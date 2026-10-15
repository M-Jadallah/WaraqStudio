FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY relay/requirements.txt /app/relay-requirements.txt
RUN python -m pip install --no-cache-dir -r /app/relay-requirements.txt \
    && groupadd --gid 10001 waraq \
    && useradd --uid 10001 --gid 10001 --no-create-home waraq

COPY --chown=10001:10001 relay /app/relay
COPY --chown=10001:10001 deploy/relay /app/deploy/relay
COPY --chown=10001:10001 waraq/mobile /app/waraq/mobile
COPY --chown=10001:10001 waraq/mobile_assets.py /app/waraq/mobile_assets.py
COPY --chown=10001:10001 waraq/icon.svg /app/waraq/icon.svg

USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "deploy.relay.entrypoint"]
