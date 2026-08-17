"""Session-scoped attachment upload, resolve, freeze, and catalog."""

from .catalog import AttachmentRecord, SessionAttachmentCatalog
from .config import attachments_enabled_from_env
from .freeze import freeze_attachments_in_history
from .resolve import AttachmentResolveError, resolve_messages_for_provider
from .store import (
    AttachmentStore,
    FilesystemAttachmentStore,
    attachment_workspace_path,
    sniff_mime,
)
from .text import parse_attachment_descriptor_text, render_attachment_descriptor_text

__all__ = [
    "AttachmentRecord",
    "AttachmentResolveError",
    "AttachmentStore",
    "FilesystemAttachmentStore",
    "SessionAttachmentCatalog",
    "attachment_workspace_path",
    "attachments_enabled_from_env",
    "freeze_attachments_in_history",
    "parse_attachment_descriptor_text",
    "render_attachment_descriptor_text",
    "resolve_messages_for_provider",
    "sniff_mime",
]
