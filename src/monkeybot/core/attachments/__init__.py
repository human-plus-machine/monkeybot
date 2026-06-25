"""Session-scoped attachment upload, resolve, freeze, and catalog."""

from .catalog import AttachmentRecord, SessionAttachmentCatalog
from .config import attachments_enabled_from_env
from .freeze import freeze_attachments_in_history
from .resolve import AttachmentResolveError, resolve_messages_for_provider
from .store import AttachmentStore, FilesystemAttachmentStore, sniff_mime
from .text import parse_attachment_descriptor_text, render_attachment_descriptor_text

__all__ = [
    "AttachmentRecord",
    "AttachmentResolveError",
    "AttachmentStore",
    "FilesystemAttachmentStore",
    "SessionAttachmentCatalog",
    "attachments_enabled_from_env",
    "freeze_attachments_in_history",
    "parse_attachment_descriptor_text",
    "render_attachment_descriptor_text",
    "resolve_messages_for_provider",
    "sniff_mime",
]
