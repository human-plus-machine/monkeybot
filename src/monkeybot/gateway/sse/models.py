"""
Pydantic models and API errors for the v2 SSE gateway.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from monkeybot.core.path_safety import validate_session_id_component


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
    model_provider: str | None = Field(
        default=None,
        description="Canonical or alias provider id (e.g. 'openai', 'gemini', 'vertex-claude'). None → server env default.",
    )
    model_name: str | None = Field(
        default=None,
        max_length=200,
        description="Model id for the chosen provider. None → server env default.",
    )

    @field_validator("session_id")
    @classmethod
    def _reject_unsafe_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return validate_session_id_component(value)


class CreateSessionResponse(BaseModel):
    """POST /sessions response (201)."""

    session_id: str
    created_at: int = Field(..., description="Unix time in milliseconds.")


class DeleteSessionResponse(BaseModel):
    """DELETE /sessions/{id} response (200)."""

    deleted: bool
    transcript_report_dir: str | None = Field(
        default=None,
        description="Absolute path to the session transcript folder when analysis ran.",
    )


class ReplyBodyFields(BaseModel):
    """Shared ``message`` / ``content`` fields for reply, steer, and queue."""

    message: str | None = Field(default=None, max_length=32000)
    content: list[dict[str, Any]] | None = None


class ReplyRequest(ReplyBodyFields):
    """POST /sessions/{id}/reply body."""

    request_id: str


class SteerRequest(ReplyBodyFields):
    """POST /sessions/{id}/steer — mid-turn user injection (requires busy session)."""


class QueueRequest(ReplyBodyFields):
    """POST /sessions/{id}/queue — follow-up prompt drained when the session goes idle."""

    request_id: str


class AttachmentUploadResponse(BaseModel):
    """POST /sessions/{id}/attachments response (201)."""

    attachment_id: str
    mime_type: str
    size_bytes: int
    filename: str
    created_at: int


class ReplyResponse(BaseModel):
    """POST /sessions/{id}/reply response (200)."""

    request_id: str


class AdmissionAcceptedResponse(BaseModel):
    """POST /steer or /queue acceptance."""

    request_id: str
    queue: Literal["steer", "follow_up"]
    position: int


class CancelRequest(BaseModel):
    """POST /sessions/{id}/cancel body."""

    request_id: str


class ToolConfirmationPOST(BaseModel):
    """POST /sessions/{id}/tool-confirmations/{tool_call_id} body."""

    approved: bool
    reason: str | None = None
    always: bool = False
    """When true with approved, remember this tool+resource for the rest of the session."""


class ElicitationPOST(BaseModel):
    """POST /sessions/{id}/elicitations/{id} body."""

    user_data: Any | None = None


class FrontendToolResultPOST(BaseModel):
    """POST /sessions/{id}/frontend-tool-results/{tool_call_id} body."""

    result: list[dict[str, Any]]
    is_error: bool


class HealthResponse(BaseModel):
    """GET /health response."""

    status: Literal["ok"]
    version: Literal["2.0.0"]
    memory: Literal["enabled", "disabled", "unavailable", "unknown"] = "unknown"
    memory_detail: str | None = None


class SessionUsageResponse(BaseModel):
    """GET /sessions/{id}/usage response."""

    session_id: str
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    period_start: int = 0
    period_end: int = 0
    last_prompt_tokens: int = Field(
        0,
        description="Input (prompt) tokens reported for the most recent completed turn in this session.",
    )
    estimated_prompt_tokens: int = Field(
        0,
        description="Peak pre-stream prompt input token count for the latest turn (provider count_tokens / OpenAI tiktoken); aligns with summarization checks.",
    )
    summarization_threshold_tokens: int = Field(
        0,
        description="``floor(MODEL_CONTEXT_WINDOW * 0.85)`` — same bar as sync summarization in the agent loop.",
    )
    context_window_tokens: int = Field(
        1_000_000,
        description="Configured model context window cap used for UI fill estimates (see MODEL_CONTEXT_WINDOW).",
    )


class UsageBucketResponse(BaseModel):
    """One model or day bucket in GET /usage."""

    key: str
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class UsageSeriesPointResponse(BaseModel):
    """One (time bucket, model) cell in GET /usage ``by_bucket_model``."""

    bucket: str
    model: str
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class AgentUsageResponse(BaseModel):
    """GET /usage — agent-wide totals plus spend split by model, UTC day, and series."""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    period_start: int = 0
    period_end: int = 0
    by_model: list[UsageBucketResponse] = Field(default_factory=list)
    by_day: list[UsageBucketResponse] = Field(default_factory=list)
    by_bucket_model: list[UsageSeriesPointResponse] = Field(default_factory=list)


def error_payload_dict(code: str, message: str, request_id: str) -> dict[str, Any]:
    """Build a JSON-serializable error body for JSONResponse."""
    return {"error": {"code": code, "message": message, "request_id": request_id}}
