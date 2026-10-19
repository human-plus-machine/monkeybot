"""Pytest config for the live-eval helpers."""

# Fixture repos hold their own (intentionally failing) tests; never collect them here.
collect_ignore = ["fixtures"]
