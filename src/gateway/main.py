"""
Main entry point for Gateway server.

Run locally with:
    python -m emonk.gateway.main

Or with uvicorn:
    uvicorn emonk.gateway.server:app --reload --port 8080
"""

import os

from dotenv import load_dotenv

# Load .env file for local development
# In production (Cloud Run), env vars are set by deployment config
load_dotenv()

# Import app after loading env vars
from .server import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn

    # Get port from env var (default 8080 for Cloud Run compatibility)
    port = int(os.getenv("PORT", "8080"))

    # Get log level from env var
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    print(f"Starting Emonk Gateway on port {port}...")
    print(f"Log level: {log_level}")
    print(f"Allowed users: {os.getenv('ALLOWED_USERS', 'NOT SET - REQUIRED!')}")
    print()

    uvicorn.run(
        app,
        host="0.0.0.0",  # Listen on all interfaces
        port=port,
        log_level=log_level,
        access_log=True,
    )
