"""Small text helpers."""


def slugify(name: str) -> str:
    """Lowercase a product name into a url slug."""
    return "-".join(name.lower().split())


def truncate(text: str, width: int) -> str:
    """Cut text to width characters with an ellipsis."""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"
