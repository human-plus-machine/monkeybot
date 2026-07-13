# Model Context Protocol (MCP) in monkeybot

monkeybot fully supports the Model Context Protocol (MCP), enabling your agents to interact with external tools, APIs, and data sources. Both local `stdio` subprocess transport and remote `Streamable HTTP` SSE transports are supported.

**See also:** [Progressive MCP tool disclosure](progressive-mcp-tools.md) (on-demand `enable_mcp` + mid-turn tool refresh for TTFT).

---

## Configuration

By default, monkeybot loads its MCP servers map from a JSON file specified under `paths.mcp_config` in `monkeybot.yaml` (typically `./monkeybot_config/mcp.json`).

A template `mcp.json` looks like this. Every entry under `mcpServers` is catalogued at
startup (unless `"enabled": false`); the model calls `enable_mcp("name")` before those
tools appear. Use `"autoConnect": true` when a server must connect at startup. Delete an
entry to remove it from the catalog.

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/path/to/workspace"
      ],
      "env": {
        "NODE_ENV": "production",
        "API_SECRET": "${MY_SECRET_ENV_VAR}"
      }
    },
    "browser": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/path/to/monkeybot/integrations/browser-mcp",
        "python",
        "-m",
        "browser_mcp.server"
      ],
      "env": {
        "BU_NAME": "monkeybot",
        "BROWSER_MCP_PLAYBOOKS_DIR": "./workspace/skills/browser/playbooks",
        "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
      }
    }
  }
}
```

Demo agent: `./run.sh` copies `monkeybot_config_example/mcp.example.json` → `demo_agent/monkeybot_config/mcp.json` if missing (live config is gitignored). Scaffolded agents get stdio / HTTP / OAuth / browser examples from the packaged `monkeybot_config/mcp.json` — keep only the servers you want and fix placeholder paths.

---

## Features

### 0. Progressive MCP (always on-demand)

Servers listed in `mcp.json` are **catalogued at startup but not connected** by default.
Schemas stay out of the provider payload until the model calls `enable_mcp("name")`; tools
then appear on the **next model step of the same turn** (text/SSE loop). Prefer that over
inventing `add_mcp_server` command/args for servers already in config.

**Config flags (per server):**

| Flag | Effect |
|------|--------|
| *(default)* | Catalog only; model must `enable_mcp` |
| `"enabled": false` | Skip entirely — not catalogued, not connectable by the model (trust gate) |
| `"autoConnect": true` | Connect at startup and advertise tools immediately (escape hatch for skills that predate `enable_mcp`) |

**Breaking change:** servers that used to auto-connect when listed (unless `enabled: false`)
now stay catalog-only. Set `"autoConnect": true` to restore the old startup behavior, or
update skills to call `enable_mcp("name")` first.

**Realtime / voice:** `enable_mcp` / `disable_mcp` still mutate the harness MCP client and
refresh `ctx.tools`, but live vendor sessions fix tool schemas at connect time. Newly
enabled schemas apply only after starting a new realtime session (v1 has no
reconnect/resume).

See [Progressive MCP tool disclosure](progressive-mcp-tools.md).

### 1. Environment Variable Interpolation

To prevent checking secrets or environment-specific paths into version control, monkeybot recursively interpolates values in `mcp.json` matching the `${VAR_NAME}` pattern.

* Missing environment variables are interpolated as empty strings `""`.
* Interpolation applies recursively to keys and nested elements under `env`, `headers`, `args`, `url`, and `auth`.

### 2. OAuth2 / OpenID Connect (OIDC) Authentication

For remote Streamable HTTP MCP endpoints requiring dynamic or short-lived credentials, monkeybot can perform background OAuth2 token retrieval and automatic bearer token rotation.

Add an optional `"auth"` block to your server specification inside `mcp.json`:

#### Client Credentials Flow (Machine-to-Machine)

```json
"langchain-docs": {
  "url": "https://mcp-server.example.com/mcp",
  "auth": {
    "flow": "client_credentials",
    "token_url": "https://identity.provider.com/oauth2/token",
    "client_id": "${OAUTH_CLIENT_ID}",
    "client_secret": "${OAUTH_CLIENT_SECRET}",
    "scope": "read:tools write:tools",
    "client_auth_method": "body"
  }
}
```

#### Resource Owner Password Flow

```json
"internal-admin-tool": {
  "url": "https://admin-mcp.internal.net",
  "auth": {
    "flow": "password",
    "token_url": "https://auth.internal.net/oauth/token",
    "username": "${BOT_USERNAME}",
    "password": "${BOT_PASSWORD}",
    "client_id": "${OAUTH_CLIENT_ID}",
    "client_secret": "${OAUTH_CLIENT_SECRET}"
  }
}
```

#### Authentication Properties:

| Key | Description | Optional / Required |
| --- | --- | --- |
| `flow` | The grant type flow. Must be `client_credentials` or `password`. | **Required** |
| `token_url` | The URL of the OAuth2 token endpoint. | **Required** |
| `client_id` | OAuth client ID. | Optional (Depending on endpoint) |
| `client_secret` | OAuth client secret. | Optional (Depending on endpoint) |
| `scope` | Space-separated list of scopes to request. | Optional |
| `audience` | Target audience parameter (optional). | Optional |
| `resource` | Target resource parameter (optional). | Optional |
| `client_auth_method` | Specifies how the client ID and secret are sent. Can be `body` (sent in URL-encoded post body) or `basic` (sent in `Authorization: Basic ...` header). Default is `body`. | Optional |
| `extra` | Dictionary of additional custom query or body params to include in the token request. | Optional |

#### Token Refresh and Expiry Behavior:

* **Background Refresh**: Token expiry is calculated based on the `expires_in` response property (default: 3600 seconds). monkeybot automatically refreshes the token in the background 60 seconds before it expires.
* **401 Retry**: If the target MCP endpoint returns a `401 Unauthorized` response, the authentication handler immediately discards the current token, issues a fresh token request, and retries the failed request once.
* **Header Priority**: If an `"auth"` block is provided, static `"Authorization"` / `"authorization"` parameters in the `"headers"` block are automatically popped and ignored to avoid conflict.

---

## Startup Validation & Diagnostics

In cloud production environments or CI test pipelines, you may want to detect invalid configurations immediately rather than having background workers fail gracefully and silently ignore unavailable tools.

### Fail-Fast (Strict Load)

Set the environment variable `MCP_STRICT_LOAD` to `true` (or `1`, `yes`):

```bash
export MCP_STRICT_LOAD=true
```

When enabled:
1. Any error connecting or handshaking with an MCP server (including stdio exit codes, unreachable HTTP servers, or DNS failures) will raise an exception during bootstrap.
2. The exception halts the application boot sequence, preventing unhealthy gateway nodes from serving traffic.

### Actionable Troubleshooting Banners

If startup fails under strict load, monkeybot prints a high-signal diagnostic banner to stderr containing user-actionable remedies before exiting:

```
======================================================================
[MCP_STARTUP_FAILURE_DIAGNOSTIC]
======================================================================
Config file: /app/monkeybot_config/mcp.json
Server name: langchain-docs
Exception:   MCPAuthError: Token endpoint returned 400 (invalid_client): Client authentication failed
Remedy:      Verify client_id and client_secret (or Basic-auth client credentials) against your identity provider.
======================================================================
```

---

## Developer Implementation Notes

* The authentication exchange runs inside an `httpx.Auth` subclass (`MCPAuthHandler`).
* It utilizes `asyncio.Lock` to guarantee that concurrent tool calls from different agent loops do not trigger duplicate token-refresh HTTP calls to the identity provider.
* Custom exceptions (`MCPAuthError`, `MCPConnectivityError`, `MCPDiagnosticError`) provide descriptive messages and standard troubleshooting remedies for operators.

---

## Browser MCP

For real-browser control (CDP) via the optional `browser` stdio server — local dev, self-hosted headless Chromium, and Browser Use Cloud — see [Browser MCP](browser-mcp.md).
