"""Re-export Principal + helpers for injection into middleware state."""

from __future__ import annotations

import hashlib

from .events import Principal


def hash_email(email: str) -> str:
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()[:16]


def make_user_principal(*, user_id: str, email: str | None = None, tenant: str | None = None) -> Principal:
    return Principal(
        kind="user",
        id=user_id,
        email_hash=hash_email(email) if email else None,
        tenant=tenant,
    )


def make_service_principal(*, service_id: str, tenant: str | None = None) -> Principal:
    return Principal(kind="service", id=service_id, tenant=tenant)


ANONYMOUS = Principal(kind="anonymous", id="anonymous")
