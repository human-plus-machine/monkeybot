# shop

Order pricing library for a small web shop.

## Conventions
- Run the tests with `python -m pytest -q` from the repository root.
- Money is always integer cents. Round with `shop.utils.money.round_cents` (half up), never `int()` or `round()`.
- Every new public function in `shop/` needs type hints, a one-line docstring, and a test in `tests/test_<module>.py`.
- Record every user-visible change under `## Unreleased` in `CHANGELOG.md`.
- Never edit existing tests to make them pass; fix the code.
