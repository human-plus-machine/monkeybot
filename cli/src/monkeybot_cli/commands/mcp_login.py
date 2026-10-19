"""monkeybot mcp-login — browser sign-in for one remote MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.client.auth.exceptions import OAuthFlowError
from monkeybot.core.mcp.mcp_client import MCPAuthError
from monkeybot.core.mcp.oauth_flow import login_mcp_server

logger = logging.getLogger(__name__)

CLIENT_SECRET_ENV = "MONKEYBOT_MCP_CLIENT_SECRET"


def run_mcp_login(args: argparse.Namespace) -> int:
    """Sign in, print JSON progress on stdout, and return 0 when the attempt finishes."""
    client_secret = args.client_secret or os.environ.get(CLIENT_SECRET_ENV, "")
    try:
        result = asyncio.run(
            login_mcp_server(
                args.url,
                Path(args.storage).expanduser(),
                open_browser=not args.no_browser,
                client_id=args.client_id,
                client_secret=client_secret,
                redirect_port=args.redirect_port,
            )
        )
    except (MCPAuthError, OAuthFlowError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        logger.exception("mcp oauth sign-in failed")
        print("Sign-in failed.", file=sys.stderr)
        return 1
    print(json.dumps(result), flush=True)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``mcp-login`` subcommand."""
    command = sub.add_parser(
        "mcp-login",
        help="Sign in to a remote MCP server in the browser",
    )
    command.add_argument("--url", required=True, help="Streamable HTTP URL of the MCP server")
    command.add_argument(
        "--storage", required=True, help="Directory for this machine's OAuth tokens"
    )
    command.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser",
    )
    command.add_argument(
        "--client-id",
        default="",
        help="OAuth client id, for a server that will not register one automatically",
    )
    command.add_argument(
        "--client-secret",
        default="",
        help=(
            f"OAuth client secret, when that client requires one. Prefer {CLIENT_SECRET_ENV}: "
            "command-line arguments are visible to other processes."
        ),
    )
    command.add_argument(
        "--redirect-port",
        type=int,
        default=None,
        help="Loopback port for the sign-in redirect, for a client registered with an exact URI",
    )
    command.set_defaults(func=run_mcp_login)
