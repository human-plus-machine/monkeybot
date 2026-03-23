"""
FastAPI server for Gateway module.

This module implements the HTTP interface for Emonk:
- POST /webhook: Handle Google Chat messages
- GET /health: Health check for Cloud Run

Security layers:
1. Allowlist validation (ALLOWED_USERS env var)
2. PII filtering (hash emails, strip metadata)
3. Agent Core processing (LLM + skills)
"""

import json
import logging
import os
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response

from .interfaces import AgentCoreInterface, AgentError
from .mocks import MockAgentCore
from .models import (
    CronTickResponse,
    GoogleChatResponse,
    GoogleChatWebhook,
    GoogleChatWorkspaceResponse,
    HealthCheckResponse,
)
from .pii_filter import filter_google_chat_pii

# Configure structured JSON logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger(__name__)


def log_structured(level: str, message: str, **extra: str | int) -> None:
    """
    Log structured JSON for Cloud Logging.

    Args:
        level: Log level (INFO, WARNING, ERROR, etc.)
        message: Log message
        **extra: Additional fields to include in log entry
    """
    log_entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "severity": level,
        "message": message,
        "component": "gateway",
        **extra,
    }
    getattr(logger, level.lower())(json.dumps(log_entry))


# Create FastAPI application
app = FastAPI(
    title="Emonk Gateway",
    version="1.0.0",
    description="HTTP interface for Emonk AI agent framework",
)

# For Sprint 1: Use mock Agent Core
# Story 4 (Integration) will wire real Agent Core implementation
agent_core: AgentCoreInterface = MockAgentCore()

SUPPORTED_VOICE_MIME_TYPES = {"audio/ogg", "audio/webm", "audio/mp4"}
_VOICE_MAX_AUDIO_BYTES = int(os.getenv("VOICE_MAX_AUDIO_BYTES", str(10 * 1024 * 1024)))


def truncate_response(text: str, max_length: int = 4000) -> str:
    """
    Truncate response text for Google Chat.

    Google Chat has a 4096 character limit for Cards V2 text.
    We truncate at 4000 to leave room for formatting.

    Args:
        text: Response text from Agent Core
        max_length: Maximum length (default 4000)

    Returns:
        Truncated text if needed, original text otherwise
    """
    if len(text) <= max_length:
        return text

    # Truncation message length: "\n\n... (response truncated)" = 28 chars
    truncation_message = "\n\n... (response truncated)"
    truncate_at = max_length - len(truncation_message)

    return text[:truncate_at] + truncation_message


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def webhook(payload: GoogleChatWebhook):
    """
    Handle Google Chat webhook.

    Process flow:
    1. Validate sender email against ALLOWED_USERS allowlist
    2. Filter PII (hash email to user_id, strip Google Chat metadata)
    3. Generate trace_id for request tracking
    4. Call Agent Core to process message
    5. Truncate response if needed (Google Chat limit: 4000 chars)
    6. Format response for Google Chat Cards V2

    Args:
        payload: Google Chat webhook payload (validated by Pydantic)

    Returns:
        GoogleChatResponse with agent's response text

    Raises:
        HTTPException(401): If sender email not in ALLOWED_USERS
        HTTPException(500): If Agent Core processing fails
    """
    trace_id = str(uuid.uuid4())

    # Log incoming webhook (before PII filtering, so we have email for debugging)
    log_structured(
        "INFO",
        "Webhook received",
        trace_id=trace_id,
        sender=payload.message.sender.email,
    )

    # Security Layer 1: Allowlist validation
    # Check sender email against ALLOWED_USERS env var
    allowed_users_str = os.getenv("ALLOWED_USERS", "")
    if not allowed_users_str:
        log_structured(
            "ERROR",
            "ALLOWED_USERS env var not set",
            trace_id=trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: ALLOWED_USERS not set",
        )

    allowed_users = [email.strip() for email in allowed_users_str.split(",")]
    if payload.message.sender.email not in allowed_users:
        log_structured(
            "WARNING",
            "Unauthorized user attempted access",
            trace_id=trace_id,
            sender=payload.message.sender.email,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized user",
        )

    # Security Layer 2: PII filtering
    # Hash email to user_id, strip all Google Chat metadata
    filtered = filter_google_chat_pii(payload.model_dump())

    log_structured(
        "INFO",
        "PII filtered",
        trace_id=trace_id,
        user_id=filtered["user_id"],
        content_length=len(filtered["content"]),
    )

    # Call Agent Core to process message
    try:
        # Try new agent.ainvoke() API first (LangGraph)
        if hasattr(agent_core, 'ainvoke'):
            result = await agent_core.ainvoke(
                {"messages": [{"role": "user", "content": filtered["content"]}]},
                config={
                    "configurable": {
                        "thread_id": filtered["user_id"],
                        "user_id": filtered["user_id"],
                    }
                },
            )
            # Extract response from result
            response_message = result["messages"][-1]
            response_text = response_message.content if hasattr(response_message, "content") else str(response_message)
        elif hasattr(agent_core, 'invoke'):
            # Try sync invoke
            result = await agent_core.invoke(
                {"messages": [{"role": "user", "content": filtered["content"]}]},
                config={
                    "configurable": {
                        "thread_id": filtered["user_id"],
                        "user_id": filtered["user_id"],
                    }
                },
            )
            # Extract response from result
            response_message = result["messages"][-1]
            response_text = response_message.content if hasattr(response_message, "content") else str(response_message)
        else:
            # Fall back to old process_message() API
            response_text = await agent_core.process_message(
                user_id=filtered["user_id"],
                content=filtered["content"],
                trace_id=trace_id,
            )
    except AgentError as e:
        log_structured(
            "ERROR",
            f"Agent Core processing failed: {e.message}",
            trace_id=trace_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent processing failed",
        ) from e
    except Exception as e:
        log_structured(
            "ERROR",
            f"Unexpected error during Agent Core processing: {str(e)}",
            trace_id=trace_id,
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    # Truncate response if needed (Google Chat limit: 4000 chars)
    response_text = truncate_response(response_text)

    log_structured(
        "INFO",
        "Webhook processed successfully",
        trace_id=trace_id,
        response_length=len(response_text),
    )

    # Return response in appropriate format based on GOOGLE_CHAT_FORMAT env var
    chat_format = os.getenv("GOOGLE_CHAT_FORMAT", "workspace_addon")

    if chat_format == "workspace_addon":
        return GoogleChatWorkspaceResponse.from_text(response_text)
    else:
        # Legacy format for backward compatibility
        return GoogleChatResponse(text=response_text)


@app.post("/voice", status_code=status.HTTP_200_OK)
async def voice(
    audio: UploadFile = File(...),  # noqa: B008
    mime_type: str = Form(default="audio/ogg"),  # noqa: B008
    user_email: str = Form(...),  # noqa: B008
    space_name: str = Form(default=""),  # noqa: B008
):
    """Handle voice message: STT → agent → TTS → audio response.

    Process flow:
    1. Check VOICE_ENABLED (at request time for hot-toggle)
    2. Validate user_email against ALLOWED_USERS
    3. Validate mime_type
    4. Read and size-check audio bytes
    5. Transcribe audio via VoiceHandler.transcribe()
    6. Invoke agent with transcript
    7. Synthesize response via VoiceHandler.synthesize()
    8. Return OGG_OPUS audio with X-Transcript-In/Out/Trace-Id headers

    Returns:
        200: OGG_OPUS audio bytes
        400: Missing audio content
        401: Unauthorized user
        404: Voice not enabled
        413: Audio too large
        415: Unsupported mime_type
        422: Empty transcript
        503: STT/TTS API unavailable
        500: Agent processing failed
    """
    trace_id = str(uuid.uuid4())

    # Check VOICE_ENABLED at request time (allows hot-toggle without redeploy)
    if os.getenv("VOICE_ENABLED", "false").lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice endpoint not enabled",
        )

    # Allowlist check
    allowed_users_str = os.getenv("ALLOWED_USERS", "")
    allowed_users = [e.strip() for e in allowed_users_str.split(",") if e.strip()]
    if user_email not in allowed_users:
        log_structured("WARNING", "Unauthorized voice attempt", trace_id=trace_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized user",
        )

    # Validate mime_type
    if mime_type not in SUPPORTED_VOICE_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported mime_type: {mime_type}. "
                f"Supported: {sorted(SUPPORTED_VOICE_MIME_TYPES)}"
            ),
        )

    # Read audio bytes
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing audio content",
        )

    # Size check
    max_bytes = int(os.getenv("VOICE_MAX_AUDIO_BYTES", str(10 * 1024 * 1024)))
    if len(audio_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Audio file too large",
        )

    log_structured(
        "INFO",
        "Voice audio received",
        trace_id=trace_id,
        audio_length_bytes=len(audio_bytes),
        mime_type=mime_type,
    )

    # Get VoiceHandler from agent
    voice_handler = getattr(agent_core, "voice_handler", None)
    if voice_handler is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice handler not initialized",
        )

    # STT: transcribe audio
    try:

        transcript_in = await voice_handler.transcribe(audio_bytes, mime_type)
    except Exception as e:
        err_str = str(e).lower()
        if "empty transcript" in err_str or "no results" in err_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Empty transcript — no speech detected",
            ) from e
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Speech-to-text service unavailable",
        ) from e

    # Agent invocation
    try:
        if hasattr(agent_core, "ainvoke"):
            result = await agent_core.ainvoke(
                {"messages": [{"role": "user", "content": transcript_in}]},
                config={"configurable": {"thread_id": user_email}},
            )
            response_msg = result["messages"][-1]
            response_text = (
                response_msg.content
                if hasattr(response_msg, "content")
                else str(response_msg)
            )
        else:
            response_text = await agent_core.process_message(
                user_id=user_email,
                content=transcript_in,
                trace_id=trace_id,
            )
    except Exception as e:
        log_structured(
            "ERROR",
            f"Agent failed during voice processing: {e}",
            trace_id=trace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent processing failed",
        ) from e

    # TTS: synthesize response
    try:
        audio_out = await voice_handler.synthesize(response_text)
    except Exception as e:
        log_structured("ERROR", f"TTS synthesis failed: {e}", trace_id=trace_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Text-to-speech service unavailable",
        ) from e

    log_structured(
        "INFO",
        "Voice request completed",
        trace_id=trace_id,
        response_length=len(response_text),
    )

    return Response(
        content=audio_out,
        media_type="audio/ogg; codecs=opus",
        headers={
            "X-Transcript-In": transcript_in[:500],
            "X-Transcript-Out": response_text[:500],
            "X-Trace-Id": trace_id,
        },
    )


@app.post("/cron/tick", response_model=CronTickResponse, status_code=status.HTTP_200_OK)
async def cron_tick(request: Request) -> CronTickResponse:
    """
    Handle Cloud Scheduler tick for background job execution.

    This endpoint is called by Cloud Scheduler at a configured interval
    (e.g., every minute) to check for and execute due jobs. It runs a single
    scheduler cycle and returns quickly with summary metrics.

    Authentication:
    - Cloud Scheduler uses OIDC token with service account
    - For MVP, also supports X-Cloudscheduler header from Cloud Scheduler
    - In production, validate OIDC audience/issuer via Cloud Run IAM

    Process flow:
    1. Authenticate request (OIDC token or scheduler header)
    2. Run single scheduler tick (check due jobs, execute)
    3. Return metrics (jobs checked, executed, succeeded, failed)

    Returns:
        CronTickResponse with execution metrics

    Raises:
        HTTPException(401): If authentication fails
        HTTPException(500): If scheduler execution fails
    """
    trace_id = str(uuid.uuid4())
    start_time = datetime.now(UTC)

    log_structured(
        "INFO",
        "Cron tick received",
        trace_id=trace_id,
    )

    # Authentication: Check for Cloud Scheduler headers or OIDC token
    # For MVP: Accept X-Cloudscheduler header (set by Cloud Scheduler)
    # TODO: Add OIDC token validation for production
    scheduler_header = request.headers.get("X-Cloudscheduler")
    cron_secret = os.getenv("CRON_SECRET")

    # Allow requests with Cloud Scheduler header OR matching secret
    if not scheduler_header and cron_secret:
        # Check for secret-based auth as fallback
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            log_structured(
                "WARNING",
                "Unauthorized cron tick attempt - missing auth",
                trace_id=trace_id,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Missing authentication",
            )

        token = auth_header.split(" ")[1]
        if token != cron_secret:
            log_structured(
                "WARNING",
                "Unauthorized cron tick attempt - invalid token",
                trace_id=trace_id,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Invalid token",
            )

    # Execute scheduler tick
    try:
        # Check if agent_core has scheduler (it should if properly initialized)
        if not hasattr(agent_core, 'scheduler'):
            log_structured(
                "ERROR",
                "Scheduler not initialized in agent core",
                trace_id=trace_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Scheduler not initialized",
            )

        # Run single tick (will add this method to scheduler)
        tick_result = await agent_core.scheduler.run_tick()

        execution_time_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

        metrics = {
            "jobs_checked": tick_result.get("jobs_checked", 0),
            "jobs_due": tick_result.get("jobs_due", 0),
            "jobs_executed": tick_result.get("jobs_executed", 0),
            "jobs_succeeded": tick_result.get("jobs_succeeded", 0),
            "jobs_failed": tick_result.get("jobs_failed", 0),
            "execution_time_ms": execution_time_ms,
        }

        log_structured(
            "INFO",
            "Cron tick completed successfully",
            trace_id=trace_id,
            **metrics,
        )

        return CronTickResponse(
            status="success",
            timestamp=datetime.now(UTC).isoformat(),
            trace_id=trace_id,
            metrics=metrics,
        )

    except Exception as e:
        execution_time_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

        log_structured(
            "ERROR",
            f"Cron tick failed: {str(e)}",
            trace_id=trace_id,
            error_type=type(e).__name__,
            execution_time_ms=execution_time_ms,
        )

        return CronTickResponse(
            status="error",
            timestamp=datetime.now(UTC).isoformat(),
            trace_id=trace_id,
            metrics={
                "error": str(e),
                "error_type": type(e).__name__,
                "execution_time_ms": execution_time_ms,
            },
        )


@app.get("/health", response_model=HealthCheckResponse)
async def health() -> HealthCheckResponse:
    """
    Health check endpoint for Cloud Run.

    Cloud Run pings this endpoint every 30 seconds to verify service health.
    Returns 200 OK if healthy, 503 Service Unavailable if unhealthy.

    For Sprint 1 (with MockAgentCore):
        - Always returns healthy (mock never fails)

    For Story 4+ (with real Agent Core):
        - Check Agent Core availability
        - Check LLM connectivity
        - Check GCS availability
        - Return unhealthy if any component down

    Returns:
        HealthCheckResponse with status and component checks
    """
    # For Sprint 1: MockAgentCore is always available
    # Story 4 will add real health checks for Agent Core, LLM, GCS
    checks = {"agent_core": "ok"}

    return HealthCheckResponse(
        status="healthy",
        timestamp=datetime.now(UTC).isoformat(),
        checks=checks,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Global exception handler for unhandled errors.

    Logs the error with trace context and returns a generic error response
    to avoid leaking internal details to users.

    Args:
        request: FastAPI request object
        exc: Unhandled exception

    Returns:
        JSONResponse with error details
    """
    trace_id = str(uuid.uuid4())

    log_structured(
        "ERROR",
        f"Unhandled exception: {str(exc)}",
        trace_id=trace_id,
        error_type=type(exc).__name__,
        path=str(request.url),
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "trace_id": trace_id,
        },
    )
