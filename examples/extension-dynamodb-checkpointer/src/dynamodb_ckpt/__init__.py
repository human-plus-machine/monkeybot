"""DynamoDB-backed :class:`emonk.core.harness.extensions.Checkpointer` plugin.

This is the canonical worked example accompanying the monkey-bot Harness
Extensibility guide (``docs/extending-the-harness.md``). It demonstrates the
"ship a new backend in ~80 lines" story using nothing but the public
extension ABC and a third-party AWS SDK.
"""

from __future__ import annotations

from .checkpointer import DynamoDBCheckpointer

__all__ = ["DynamoDBCheckpointer"]
