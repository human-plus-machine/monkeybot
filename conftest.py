"""Top-level pytest plugins.

``tests/conftest.py`` loads only when ``tests/`` is on the command line. A
regression-only run still needs the OpenTelemetry fixtures, so they are
registered here once for every invocation from this checkout.

``cli/`` and ``integrations/browser-mcp/`` share this rootdir and load this
file, but their environments do not install OpenTelemetry. Skip the plugin
there. The root dev group always has it.

Importing deepeval copies ``<cwd>/.env`` into ``os.environ``, which would put the
owner's provider key into every test session. Its pytest plugin is disabled in
``pytest.ini`` and the autoload is switched off here for any later import.
"""

import importlib.util
import os

os.environ.setdefault("DEEPEVAL_DISABLE_DOTENV", "1")

if importlib.util.find_spec("opentelemetry") is not None:
    pytest_plugins = ["tests.unit.observability.conftest"]
