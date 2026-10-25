"""Top-level pytest plugins.

``tests/conftest.py`` loads only when ``tests/`` is on the command line. A
regression-only run still needs the OpenTelemetry fixtures, so they are
registered here once for every invocation from this checkout.

``cli/`` and ``integrations/browser-mcp/`` share this rootdir and load this
file, but their environments do not install OpenTelemetry. Skip the plugin
there. The root dev group always has it.
"""

import importlib.util

if importlib.util.find_spec("opentelemetry") is not None:
    pytest_plugins = ["tests.unit.observability.conftest"]
