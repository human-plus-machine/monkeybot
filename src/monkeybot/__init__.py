"""MonkeyBot — thin owned agent harness."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("monkeybot")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
