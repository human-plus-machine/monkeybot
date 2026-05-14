"""
Pydantic models and API errors for the v2 SSE gateway.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    """Error payload matching design contract 1b."""

    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    """Top-level error response `{ \"error\": ... }`."""

    error: ErrorBody


class APIError(Exception):
    """Raised for API failures; maps to JSON error envelope via exception handler."""

    def __init__(self, status_code: int, code: str, message: str, request_id: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id


class CreateSessionRequest(BaseModel):
    """POST /sessions body."""

    session_id: str | None = Field(
        default=None,
        max_length=128,
        description="Client id; server generates UUID when omitted.",
    )
    agent_md: str | None = Field(default=None, description="Optional AGENT.md path override.")


class CreateSessionResponse(BaseModel):
    """POST /sessions response (201)."""

    session_id: str
    created_at: int = Field(..., description="Unix time in milliseconds.")


class ReplyRequest(BaseModel):
    """POST /sessions/{id}/reply body."""

    request_id: str
    message: str = Field(..., max_length=32000)


class ReplyResponse(BaseModel):
    """POST /sessions/{id}/reply response (200)."""

    request_id: str


class CancelRequest(BaseModel):
    """POST /sessions/{id}/cancel body."""

    request_id: str


class HealthResponse(BaseModel):
    """GET /health response."""

    status: Literal["ok"]
    version: Literal["2.0.0"]


class SessionUsageResponse(BaseModel):
    """GET /sessions/{id}/usage response."""

    session_id: str
    turns: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    period_start: int
    period_end: int
    last_prompt_tokens: int = Field(
        0,
        description="Input (prompt) tokens reported for the most recent completed turn in this session.",
    )
    context_window_tokens: int = Field(
        1_000_000,
        description="Configured model context window cap used for UI fill estimates (see MODEL_CONTEXT_WINDOW).",
    )


def error_payload_dict(code: str, message: str, request_id: str) -> dict[str, Any]:
    """Build a JSON-serializable error body for JSONResponse."""
    return {"error": {"code": code, "message": message, "request_id": request_id}}
