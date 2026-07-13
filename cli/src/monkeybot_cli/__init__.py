"""MonkeyBot setup CLI — create, configure, validate, and chat with agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("monkeybot-cli")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
