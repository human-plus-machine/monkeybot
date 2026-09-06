"""Typed conversation content blocks (goose-style discriminated union).

Each message turn is a list of ContentBlock values serialized as JSON objects
with a ``type`` discriminator. Wire field names use camelCase; Python
dataclass fields use snake_case. This module owns serde invariants shared by
SQLite history (Story 2), provider adapters (Story 3), and gateway payloads.

The variant set is closed; unknown ``type`` tags fail deserialization with
ValueError. Optional fields are omitted from JSON when None.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal, Self, TypeAlias, cast

_DISPATCH: dict[str, type[ContentBlock]] = {}


def _drop_none(values: dict[str, object | None]) -> dict[str, object]:
    """Return a copy with key/value pairs whose value is not None."""
    return {k: v for k, v in values.items() if v is not None}


def _require_str(d: Mapping[str, object], key: str, *, context: str) -> str:
    """Return a required string field or raise ValueError."""
    if key not in d:
        raise ValueError(f"{context}: missing required field {key!r}")
    val = d[key]
    if not isinstance(val, str):
        raise ValueError(
            f"{context}: field {key!r} must be a string, got {type(val).__name__}"
        )
    return val


def _optional_str(d: Mapping[str, object], key: str) -> str | None:
    """Return optional string: absent or JSON null -> None."""
    if key not in d or d[key] is None:
        return None
    val = d[key]
    if not isinstance(val, str):
        raise ValueError(
            f"optional field {key!r}: expected string or null, got {type(val).__name__}"
        )
    return val


def _require_dict(d: Mapping[str, object], key: str, context: str) -> dict[str, object]:
    """Return a required JSON object at key."""
    if key not in d:
        raise ValueError(f"{context}: missing required field {key!r}")
    val = d[key]
    if not isinstance(val, dict):
        raise ValueError(
            f"{context}: field {key!r} must be a JSON object, got {type(val).__name__}"
        )
    return val


def _optional_dict(
    d: Mapping[str, object], key: str, *, context: str
) -> dict[str, object] | None:
    """Return optional JSON object; absent or null -> None."""
    if key not in d or d[key] is None:
        return None
    val = d[key]
    if not isinstance(val, dict):
        raise ValueError(
            f"{context}: field {key!r} must be a JSON object or null, "
            f"got {type(val).__name__}"
        )
    return val


def _require_bool(d: Mapping[str, object], key: str, *, context: str) -> bool:
    """Return a required boolean field."""
    if key not in d:
        raise ValueError(f"{context}: missing required field {key!r}")
    val = d[key]
    if not isinstance(val, bool):
        raise ValueError(
            f"{context}: field {key!r} must be a boolean, got {type(val).__name__}"
        )
    return val


def _parse_tool_response_is_error(d: Mapping[str, object], *, context: str) -> bool:
    """Parse ``isError``; default False when absent (read tolerant)."""
    if "isError" not in d or d["isError"] is None:
        return False
    return _require_bool(d, "isError", context=context)


def _require_block_list(
    d: Mapping[str, object], key: str, *, context: str
) -> list[ContentBlock]:
    """Parse a list of content block objects."""
    if key not in d:
        raise ValueError(f"{context}: missing required field {key!r}")
    val = d[key]
    if not isinstance(val, list):
        raise ValueError(
            f"{context}: field {key!r} must be a list, got {type(val).__name__}"
        )
    blocks: list[ContentBlock] = []
    for i, item in enumerate(val):
        if not isinstance(item, dict):
            raise ValueError(
                f"{context}: {key!r}[{i}] must be a JSON object, "
                f"got {type(item).__name__}"
            )
        blocks.append(ContentBlock.from_dict(item))
    return blocks


def _require_args_dict(d: Mapping[str, object], key: str, *, context: str) -> dict[str, object]:
    """Parse ``args`` / ``arguments`` as a JSON object."""
    return _require_dict(d, key, context)


def _action_data_from_mapping(data: Mapping[str, object]) -> ActionRequiredData:
    """Parse ActionRequired inner ``data`` object."""
    action_type = _require_str(data, "actionType", context="ActionRequired.data")
    ctx = "ActionRequired.data"
    if action_type == ToolConfirmationAction.ACTION_TYPE:
        return ToolConfirmationAction(
            id=_require_str(data, "id", context=f"{ctx}.toolConfirmation"),
            tool_name=_require_str(data, "toolName", context=f"{ctx}.toolConfirmation"),
            arguments=_require_args_dict(
                data, "arguments", context=f"{ctx}.toolConfirmation"
            ),
            prompt=_optional_str(data, "prompt"),
        )
    if action_type == ElicitationAction.ACTION_TYPE:
        return ElicitationAction(
            id=_require_str(data, "id", context=f"{ctx}.elicitation"),
            message=_require_str(data, "message", context=f"{ctx}.elicitation"),
            requested_schema=_require_dict(
                data, "requestedSchema", context=f"{ctx}.elicitation"
            ),
        )
    if action_type == ElicitationResponseAction.ACTION_TYPE:
        return ElicitationResponseAction(
            id=_require_str(data, "id", context=f"{ctx}.elicitationResponse"),
            user_data=_require_dict(data, "userData", context=f"{ctx}.elicitationResponse"),
        )
    raise ValueError(f"{ctx}: unknown actionType: {action_type!r}")


def _action_data_to_dict(data: ActionRequiredData) -> dict[str, object]:
    """Serialize ActionRequired inner data to wire JSON (camelCase)."""
    if isinstance(data, ToolConfirmationAction):
        inner: dict[str, object | None] = {
            "actionType": data.ACTION_TYPE,
            "id": data.id,
            "toolName": data.tool_name,
            "arguments": data.arguments,
            "prompt": data.prompt,
        }
        return _drop_none(inner)
    if isinstance(data, ElicitationAction):
        return {
            "actionType": data.ACTION_TYPE,
            "id": data.id,
            "message": data.message,
            "requestedSchema": data.requested_schema,
        }
    if isinstance(data, ElicitationResponseAction):
        return {
            "actionType": data.ACTION_TYPE,
            "id": data.id,
            "userData": data.user_data,
        }
    raise TypeError(f"unsupported ActionRequiredData: {type(data)!r}")


def _notification_type_from_wire(
    d: Mapping[str, object], *, context: str
) -> SystemNotificationType:
    """Parse and validate ``notificationType``."""
    raw = _require_str(d, "notificationType", context=context)
    if raw not in SYSTEM_NOTIFICATION_TYPES:
        raise ValueError(f"{context}: invalid notificationType: {raw!r}")
    return raw  # type: ignore[return-value]


class ContentBlock(ABC):
    """Abstract base for all typed blocks (see subclasses for variants)."""

    TYPE: ClassVar[str]

    @abstractmethod
    def to_dict(self) -> dict[str, object]:
        """Serialize this block to a JSON-compatible dict (camelCase keys)."""

    @classmethod
    @abstractmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        """Parse validated payload (``type`` already checked by dispatcher)."""

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Self:
        """Deserialize a content block or dispatch from the abstract base."""
        if cls is ContentBlock:
            tag = d.get("type")
            if not isinstance(tag, str) or tag not in _DISPATCH:
                raise ValueError(f"unknown ContentBlock type: {tag!r}")
            concrete = _DISPATCH[tag]
            return cast(Self, concrete._from_dict_payload(d))
        tag = d.get("type")
        if not isinstance(tag, str) or tag != cls.TYPE:
            raise ValueError(f"expected ContentBlock type {cls.TYPE!r}, got {tag!r}")
        return cls._from_dict_payload(d)


@dataclass(frozen=True, kw_only=True)
class ToolConfirmationAction:
    """Tool confirmation payload for :class:`ActionRequired`."""

    ACTION_TYPE: ClassVar[str] = "toolConfirmation"
    id: str
    tool_name: str
    arguments: dict[str, object]
    prompt: str | None = None


@dataclass(frozen=True, kw_only=True)
class ElicitationAction:
    """Elicitation request payload for :class:`ActionRequired`."""

    ACTION_TYPE: ClassVar[str] = "elicitation"
    id: str
    message: str
    requested_schema: dict[str, object]


@dataclass(frozen=True, kw_only=True)
class ElicitationResponseAction:
    """Elicitation response payload for :class:`ActionRequired`."""

    ACTION_TYPE: ClassVar[str] = "elicitationResponse"
    id: str
    user_data: dict[str, object]


ActionRequiredData: TypeAlias = (
    ToolConfirmationAction | ElicitationAction | ElicitationResponseAction
)

SYSTEM_NOTIFICATION_TYPES: tuple[str, ...] = (
    "thinkingMessage",
    "inlineMessage",
    "creditsExhausted",
    "verifierVerdict",
)
SystemNotificationType = Literal[
    "thinkingMessage", "inlineMessage", "creditsExhausted", "verifierVerdict"
]


@dataclass(frozen=True, kw_only=True)
class Text(ContentBlock):
    """Plain text segment."""

    TYPE: ClassVar[str] = "text"
    text: str

    def to_dict(self) -> dict[str, object]:
        return {"type": self.TYPE, "text": self.text}

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(text=_require_str(payload, "text", context="Text"))


@dataclass(frozen=True, kw_only=True)
class Image(ContentBlock):
    """Base64 image payload."""

    TYPE: ClassVar[str] = "image"
    mime_type: str
    data: str
    metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "type": self.TYPE,
                "mimeType": self.mime_type,
                "data": self.data,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            mime_type=_require_str(payload, "mimeType", context="Image"),
            data=_require_str(payload, "data", context="Image"),
            metadata=_optional_dict(payload, "metadata", context="Image"),
        )


@dataclass(frozen=True, kw_only=True)
class File(ContentBlock):
    """Base64 document payload (e.g. PDF)."""

    TYPE: ClassVar[str] = "file"
    mime_type: str
    data: str
    metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "type": self.TYPE,
                "mimeType": self.mime_type,
                "data": self.data,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            mime_type=_require_str(payload, "mimeType", context="File"),
            data=_require_str(payload, "data", context="File"),
            metadata=_optional_dict(payload, "metadata", context="File"),
        )


@dataclass(frozen=True, kw_only=True)
class AttachmentRef(ContentBlock):
    """Reference to a session-scoped uploaded attachment (client / pre-freeze user turn)."""

    TYPE: ClassVar[str] = "attachmentRef"
    attachment_id: str
    mime_type: str
    metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "type": self.TYPE,
                "attachmentId": self.attachment_id,
                "mimeType": self.mime_type,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            attachment_id=_require_str(payload, "attachmentId", context="AttachmentRef"),
            mime_type=_require_str(payload, "mimeType", context="AttachmentRef"),
            metadata=_optional_dict(payload, "metadata", context="AttachmentRef"),
        )


@dataclass(frozen=True, kw_only=True)
class AttachmentDescriptor(ContentBlock):
    """Structured attachment metadata for SSE/UI (not persisted in history)."""

    TYPE: ClassVar[str] = "attachmentDescriptor"
    attachment_id: str
    mime_type: str
    description: str
    metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "type": self.TYPE,
                "attachmentId": self.attachment_id,
                "mimeType": self.mime_type,
                "description": self.description,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            attachment_id=_require_str(payload, "attachmentId", context="AttachmentDescriptor"),
            mime_type=_require_str(payload, "mimeType", context="AttachmentDescriptor"),
            description=_require_str(payload, "description", context="AttachmentDescriptor"),
            metadata=_optional_dict(payload, "metadata", context="AttachmentDescriptor"),
        )


@dataclass(frozen=True, kw_only=True)
class ToolRequest(ContentBlock):
    """Model-issued tool invocation request.

    ``metadata`` carries opaque, provider-specific round-trip data (e.g. Vertex
    Gemini's ``thoughtSignature``). It is omitted from JSON when None or empty.
    """

    TYPE: ClassVar[str] = "toolRequest"
    id: str
    name: str
    args: dict[str, object]
    parse_error: str | None = None
    metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        meta = self.metadata if self.metadata else None
        return _drop_none(
            {
                "type": self.TYPE,
                "id": self.id,
                "name": self.name,
                "args": self.args,
                "parseError": self.parse_error,
                "metadata": meta,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            id=_require_str(payload, "id", context="ToolRequest"),
            name=_require_str(payload, "name", context="ToolRequest"),
            args=_require_args_dict(payload, "args", context="ToolRequest"),
            parse_error=_optional_str(payload, "parseError"),
            metadata=_optional_dict(payload, "metadata", context="ToolRequest"),
        )


@dataclass(frozen=True, kw_only=True)
class ToolResponse(ContentBlock):
    """Tool execution result as nested content blocks."""

    TYPE: ClassVar[str] = "toolResponse"
    id: str
    tool_name: str
    result: list[ContentBlock]
    is_error: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.TYPE,
            "id": self.id,
            "toolName": self.tool_name,
            "result": [b.to_dict() for b in self.result],
            "isError": self.is_error,
        }

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            id=_require_str(payload, "id", context="ToolResponse"),
            tool_name=_require_str(payload, "toolName", context="ToolResponse"),
            result=_require_block_list(payload, "result", context="ToolResponse"),
            is_error=_parse_tool_response_is_error(payload, context="ToolResponse"),
        )


@dataclass(frozen=True, kw_only=True)
class ToolConfirmationRequest(ContentBlock):
    """Request for user confirmation before running a tool."""

    TYPE: ClassVar[str] = "toolConfirmationRequest"
    id: str
    tool_name: str
    arguments: dict[str, object]
    prompt: str | None = None

    def to_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "type": self.TYPE,
                "id": self.id,
                "toolName": self.tool_name,
                "arguments": self.arguments,
                "prompt": self.prompt,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            id=_require_str(payload, "id", context="ToolConfirmationRequest"),
            tool_name=_require_str(payload, "toolName", context="ToolConfirmationRequest"),
            arguments=_require_args_dict(
                payload, "arguments", context="ToolConfirmationRequest"
            ),
            prompt=_optional_str(payload, "prompt"),
        )


@dataclass(frozen=True, kw_only=True)
class FrontendToolRequest(ContentBlock):
    """Tool request directed at frontend tooling."""

    TYPE: ClassVar[str] = "frontendToolRequest"
    id: str
    name: str
    args: dict[str, object]
    parse_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "type": self.TYPE,
                "id": self.id,
                "name": self.name,
                "args": self.args,
                "parseError": self.parse_error,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            id=_require_str(payload, "id", context="FrontendToolRequest"),
            name=_require_str(payload, "name", context="FrontendToolRequest"),
            args=_require_args_dict(payload, "args", context="FrontendToolRequest"),
            parse_error=_optional_str(payload, "parseError"),
        )


@dataclass(frozen=True, kw_only=True)
class Thinking(ContentBlock):
    """Model reasoning text (opaque to most consumers)."""

    TYPE: ClassVar[str] = "thinking"
    thinking: str
    signature: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"type": self.TYPE, "thinking": self.thinking, "signature": self.signature}

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        thinking = _require_str(payload, "thinking", context="Thinking")
        sig_raw = payload.get("signature")
        if sig_raw is None:
            signature = ""
        elif not isinstance(sig_raw, str):
            raise ValueError(
                f"Thinking: field 'signature' must be a string, got {type(sig_raw).__name__}"
            )
        else:
            signature = sig_raw
        return cls(thinking=thinking, signature=signature)


@dataclass(frozen=True, kw_only=True)
class RedactedThinking(ContentBlock):
    """Opaque redacted reasoning blob."""

    TYPE: ClassVar[str] = "redactedThinking"
    data: str

    def to_dict(self) -> dict[str, object]:
        return {"type": self.TYPE, "data": self.data}

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(data=_require_str(payload, "data", context="RedactedThinking"))


@dataclass(frozen=True, kw_only=True)
class SystemNotification(ContentBlock):
    """System-originated notification block."""

    TYPE: ClassVar[str] = "systemNotification"
    notification_type: SystemNotificationType
    msg: str
    data: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "type": self.TYPE,
                "notificationType": self.notification_type,
                "msg": self.msg,
                "data": self.data,
            }
        )

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        return cls(
            notification_type=_notification_type_from_wire(
                payload, context="SystemNotification"
            ),
            msg=_require_str(payload, "msg", context="SystemNotification"),
            data=_optional_dict(payload, "data", context="SystemNotification"),
        )


@dataclass(frozen=True, kw_only=True)
class ActionRequired(ContentBlock):
    """User or UI must take action (sub-tagged by ``data.actionType``)."""

    TYPE: ClassVar[str] = "actionRequired"
    data: ActionRequiredData

    def to_dict(self) -> dict[str, object]:
        return {"type": self.TYPE, "data": _action_data_to_dict(self.data)}

    @classmethod
    def _from_dict_payload(cls, payload: Mapping[str, object]) -> Self:
        inner = _require_dict(payload, "data", "ActionRequired")
        return cls(data=_action_data_from_mapping(inner))


_DISPATCH.update(
    {
        "text": Text,
        "image": Image,
        "file": File,
        "attachmentRef": AttachmentRef,
        "attachmentDescriptor": AttachmentDescriptor,
        "toolRequest": ToolRequest,
        "toolResponse": ToolResponse,
        "toolConfirmationRequest": ToolConfirmationRequest,
        "actionRequired": ActionRequired,
        "frontendToolRequest": FrontendToolRequest,
        "thinking": Thinking,
        "redactedThinking": RedactedThinking,
        "systemNotification": SystemNotification,
    }
)


__all__ = [
    "ActionRequired",
    "ActionRequiredData",
    "AttachmentDescriptor",
    "AttachmentRef",
    "ContentBlock",
    "ElicitationAction",
    "ElicitationResponseAction",
    "File",
    "FrontendToolRequest",
    "Image",
    "RedactedThinking",
    "SystemNotification",
    "SystemNotificationType",
    "Text",
    "Thinking",
    "ToolConfirmationAction",
    "ToolConfirmationRequest",
    "ToolRequest",
    "ToolResponse",
]
