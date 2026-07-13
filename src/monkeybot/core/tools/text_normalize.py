"""Shared text normalization for fuzzy match paths (patch + replace_in_file)."""

from __future__ import annotations


def normalize_unicode_punctuation(s: str) -> str:
    """Map common smart quotes/dashes/nbsp to ASCII equivalents for matching."""
    return (
        s.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201a", "'")
        .replace("\u201b", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u201e", '"')
        .replace("\u201f", '"')
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2015", "-")
        .replace("\u2026", "...")
        .replace("\u00a0", " ")
    )
