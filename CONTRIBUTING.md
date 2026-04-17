# Contributing

Thank you for helping improve monkey-bot (`emonk`).

## Getting started

1. Fork the repository and clone your fork.
2. Create a Python 3.11+ virtual environment.
3. Install dev dependencies:

```bash
pip install -e ".[dev]"
```

## Checks before you open a PR

```bash
ruff check .
ruff format .
pytest -q
```

Fix any failures. Keep changes focused on one concern per pull request when possible.

## Reporting issues

Use [GitHub Issues](https://github.com/human-and-machine/monkey-bot/issues). Include steps to reproduce, expected vs actual behavior, and your Python version.

## Code of conduct

All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
