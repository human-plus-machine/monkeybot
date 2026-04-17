"""Sandbox abstraction: protocol, policy dataclass, built-in backends.

Public symbols:
    SandboxBackend, SandboxCapabilities, ExecuteResult, FileInfo, WriteResult,
    Policy, LocalShellSandbox, ModalSandbox.

Custom backends plug in via ``SandboxSpec(backend="custom",
custom_import_path="mypkg:OpenShellBackend")`` — see docs/harness/openshell-example.md.
"""

from .policy import Policy
from .protocol import (
    ExecuteResult,
    FileInfo,
    SandboxBackend,
    SandboxCapabilities,
    WriteResult,
)
from .local_shell import LocalShellSandbox
from .modal_backend import ModalSandbox

__all__ = [
    "ExecuteResult",
    "FileInfo",
    "LocalShellSandbox",
    "ModalSandbox",
    "Policy",
    "SandboxBackend",
    "SandboxCapabilities",
    "WriteResult",
]
