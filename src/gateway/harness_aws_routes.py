"""FastAPI router for ``/harness/aws/*`` admin endpoints (Story 8).

Endpoints
---------
``GET /harness/aws/smoke`` — admin-only end-to-end reachability probe for every
    AWS surface the enterprise reference stack depends on. Probes run
    concurrently via :func:`asyncio.gather` and each returns::

        {
            "probe": "<name>",
            "reachable": bool,
            "latency_ms": int,
            "error_class": "<exception class name>"   # only on failure
        }

    Secret values, DSN fragments, and ARNs are never included in the response.
    On failure only the exception class name is surfaced so operators can
    distinguish "this probe is down" from "this probe is mis-configured"
    without risking leaking credentials into logs or dashboards.

The endpoint is double-gated:

* ``X-Admin-Token`` must match ``EMONK_ADMIN_TOKEN`` (reuses :func:`admin_auth`
  from ``harness_identity_routes`` so a single env var governs every admin
  surface).
* ``HARNESS_ENABLE_AWS_SMOKE`` must equal ``"1"``. A deployment that forgets to
  opt in returns ``503`` rather than silently skipping the probe, matching the
  fail-closed posture of the other admin endpoints.

Probes performed:
    1. Postgres ``SELECT 1``                    (env var: ``CKPT_DSN``)
    2. S3 ``HeadBucket``                        (env var: ``S3_BUCKET``)
    3. Secrets Manager ``ListSecrets(MaxResults=1)``
    4. Bedrock ``ListFoundationModels``
    5. KMS ``DescribeKey``                      (env var: ``KMS_KEY_ID``)
    6. STS ``GetCallerIdentity``

All six run concurrently; slow backends do not block fast ones.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .harness_identity_routes import admin_auth

router = APIRouter(prefix="/harness/aws", tags=["harness:aws"])


def _enabled_check() -> None:
    """Raise ``503`` unless ``HARNESS_ENABLE_AWS_SMOKE`` is explicitly enabled."""
    if os.environ.get("HARNESS_ENABLE_AWS_SMOKE") != "1":
        raise HTTPException(
            status_code=503,
            detail="aws smoke disabled; set HARNESS_ENABLE_AWS_SMOKE=1",
        )


async def _probe(name: str, fn: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """Run ``fn`` and return a spec-shaped check dict.

    Return shape matches 1B §7.4::

        {"name": name, "status": "pass" | "fail", "latency_ms": int}

    On failure the exception *class name* is attached as ``error_class``.
    The exception *message* is intentionally discarded — it commonly contains
    ARNs, region hints, and DSN fragments that must not leak into the
    response. Phase-6 wiring also keeps the legacy ``probe``/``reachable``
    fields populated so any pre-existing admin tooling keeps working.
    """
    start = time.monotonic()
    try:
        await fn()
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "name": name,
            "status": "pass",
            "latency_ms": latency_ms,
            "probe": name,
            "reachable": True,
        }
    except Exception as exc:  # noqa: BLE001 - intentionally broad probe
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "name": name,
            "status": "fail",
            "latency_ms": latency_ms,
            "error_class": type(exc).__name__,
            "probe": name,
            "reachable": False,
        }


async def _probe_postgres() -> None:
    """Open a Postgres connection from ``CKPT_DSN`` and issue ``SELECT 1``."""
    import asyncpg

    dsn = os.environ.get("CKPT_DSN")
    if not dsn:
        raise RuntimeError("CKPT_DSN not set")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


async def _probe_s3() -> None:
    """Call ``HeadBucket`` against ``S3_BUCKET`` via the shared aioboto3 session."""
    from src.core.harness.extensions._aws_clients import s3_client

    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET not set")
    region = os.environ.get("AWS_REGION")
    async with s3_client(region) as s3:
        await s3.head_bucket(Bucket=bucket)


async def _probe_secrets_manager() -> None:
    """List one secret to prove the runtime can reach Secrets Manager."""
    from src.core.harness.extensions._aws_clients import secrets_client

    region = os.environ.get("AWS_REGION")
    async with secrets_client(region) as sm:
        await sm.list_secrets(MaxResults=1)


async def _probe_bedrock() -> None:
    """Call ``ListFoundationModels`` via ``boto3.client('bedrock')`` on a worker thread.

    Bedrock's control-plane client is sync-only in boto3 and there is no
    aioboto3 equivalent at the time of writing, so we defer the call to a
    worker thread via :func:`asyncio.to_thread` rather than block the event
    loop.
    """
    import boto3

    region = os.environ.get("AWS_REGION")

    def _call() -> None:
        client = boto3.client("bedrock", region_name=region) if region else boto3.client("bedrock")
        client.list_foundation_models()

    await asyncio.to_thread(_call)


async def _probe_kms() -> None:
    """Call ``DescribeKey`` against ``KMS_KEY_ID`` via a sync boto3 client."""
    import boto3

    key_id = os.environ.get("KMS_KEY_ID")
    if not key_id:
        raise RuntimeError("KMS_KEY_ID not set")
    region = os.environ.get("AWS_REGION")

    def _call() -> None:
        client = boto3.client("kms", region_name=region) if region else boto3.client("kms")
        client.describe_key(KeyId=key_id)

    await asyncio.to_thread(_call)


async def _probe_sts() -> None:
    """Call ``GetCallerIdentity`` to confirm the runtime has a resolvable identity."""
    import boto3

    region = os.environ.get("AWS_REGION")

    def _call() -> None:
        client = boto3.client("sts", region_name=region) if region else boto3.client("sts")
        client.get_caller_identity()

    await asyncio.to_thread(_call)


@router.get("/smoke", dependencies=[Depends(admin_auth)])
async def smoke() -> Any:
    """Return per-check status for every AWS surface the stack depends on.

    Spec-shaped response per 1B §7.4::

        { "data": { "checks": [...], "all_pass": bool } }

    Returns ``503 HARNESS_AWS_SMOKE_FAIL`` when any check fails (body still
    contains the ``checks`` array so operators can see which surfaces are
    degraded). Legacy ``ok``/``probes`` fields remain alongside ``all_pass``
    and ``checks`` so existing admin tooling keeps working.

    Admin auth is enforced by :func:`admin_auth` at the router dependency
    level; the feature flag check runs inside the handler so its ``503`` is
    distinguishable from a missing admin token (``403``).
    """
    _enabled_check()
    results = await asyncio.gather(
        _probe("postgres.ckpt.ping", _probe_postgres),
        _probe("s3.memory.head_bucket", _probe_s3),
        _probe("aws_secrets_manager.list", _probe_secrets_manager),
        _probe("bedrock.list_foundation_models", _probe_bedrock),
        _probe("kms.describe_key", _probe_kms),
        _probe("sts.get_caller_identity", _probe_sts),
    )
    checks = list(results)
    all_pass = all(c["status"] == "pass" for c in checks)
    body = {
        "data": {
            "checks": checks,
            "all_pass": all_pass,
            "ok": all_pass,
            "probes": checks,
        }
    }
    if not all_pass:
        return JSONResponse(
            status_code=503,
            content={
                **body,
                "code": "HARNESS_AWS_SMOKE_FAIL",
                "detail": "one or more AWS smoke checks failed",
            },
        )
    return body


__all__ = ["router"]
