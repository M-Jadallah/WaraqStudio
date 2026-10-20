"""Initialize the explicit v1 schema, then replace this process with Uvicorn."""
import os
import re

from sqlalchemy.engine import make_url

from relay.config import Settings
from relay.db import Database


def main():
    settings = Settings.from_env()
    if settings.allow_insecure:
        raise SystemExit("Insecure test mode is forbidden by the deployment entrypoint.")
    if not settings.database_url.startswith("postgresql+psycopg://"):
        raise SystemExit("Production deployment requires PostgreSQL via psycopg.")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", make_url(settings.database_url).password or ""):
        raise SystemExit("Database password must be 64 hex characters. Coolify generates SERVICE_HEX_64_WARAQDB; "
                         "outside Coolify set that variable explicitly. Preserve the current password for an existing database.")
    database = Database(settings.database_url)
    try:
        database.initialize()
    finally:
        database.engine.dispose()
    # One worker owns the authenticated live-device routing map. Browser and
    # device traffic share this ASGI process; multiple workers require a broker.
    os.execvp("python", [
        "python", "-m", "uvicorn", "relay.app:application", "--factory",
        "--host", "0.0.0.0", "--port", "8000", "--workers", "1",
        "--ws-max-size", "16777216", "--ws-max-queue", "8",
        "--limit-concurrency", "64", "--timeout-keep-alive", "10",
        "--no-access-log", "--no-proxy-headers",
    ])


if __name__ == "__main__":
    main()
