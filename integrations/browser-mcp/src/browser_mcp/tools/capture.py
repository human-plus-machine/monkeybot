"""Screenshot capture."""

from __future__ import annotations

from browser_mcp import backend, dom_indexing, screenshots, tab_ops, tabs
from browser_mcp.app import mcp, _public_tool
from browser_mcp import results

@mcp.tool()
@_public_tool
def browser_screenshot(
    full: bool = False,
    max_dim: int | None = 1200,
    format: str = "jpeg",
    quality: int = 60,
    annotate: bool = False,
    tab: str | None = None,
) -> str:
    """Capture the current viewport as a JPEG (PNG available).

    LAST-RESORT FALLBACK: use browser_get_elements + browser_click_by_index /
    browser_input_by_index for ordinary clicking and typing instead — it's cheaper (no
    image tokens) and more reliable (indexed elements vs. guessed pixels). Reach for
    this tool only when browser_get_elements doesn't surface what you need: canvas-based
    apps, heavy shadow-DOM UIs, drag-and-drop, or visually confirming rendering/layout.
    Pass ``annotate=True`` to draw current index labels so the next click can still
    use browser_click_by_index.

    Returns JSON with a workspace-relative path under ``./browser/Screenshots/`` (for
    ``load_file`` on vision models), ``bytes`` (file size), and ``format`` — not inline
    image bytes.
    """
    fmt = (format or "jpeg").strip().lower()
    if fmt in {"jpg", "jpeg"}:
        fmt = "jpeg"
    elif fmt != "png":
        return results.json_text({"ok": False, "error": "format must be jpeg or png"})
    try:
        q = int(quality)
    except (TypeError, ValueError):
        return results.json_text({"ok": False, "error": "quality must be 1–95"})
    if q < 1 or q > 95:
        return results.json_text({"ok": False, "error": "quality must be 1–95"})

    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)

    dest, rel_path = screenshots.allocate_screenshot_path(fmt)
    native = dest.with_name(f"{dest.stem}-native.png")
    annotated_img = None
    labeled = 0
    try:
        handle.capture_screenshot(path=str(native), full=full, max_dim=None)
        if annotate:
            map_len = 0
            try:
                raw = handle.evaluate("Object.keys(window.__bmcpSelectorMap || {}).length")
                map_len = int(raw or 0)
            except (TypeError, ValueError):
                map_len = 0
            except Exception:
                map_len = 0
            if not map_len:
                dom_indexing.get_elements(handle, viewport_only=not full)
            payload = dom_indexing.get_rects(handle, scroll=False, full=full)
            from PIL import Image

            with Image.open(native) as captured:
                annotated_img, labeled = screenshots.draw_index_labels(
                    captured,
                    payload.get("rects") or {},
                    css_width=float(payload.get("cssWidth") or 0),
                    css_height=float(payload.get("cssHeight") or 0),
                )
        screenshots.encode_screenshot(
            native,
            dest,
            fmt=fmt,
            quality=q,
            max_dim=max_dim,
            image=annotated_img,
        )
    finally:
        native.unlink(missing_ok=True)
        if annotated_img is not None:
            annotated_img.close()

    info = handle.page_info()
    note = (
        "Screenshot saved under the agent workspace. Vision models: call load_file "
        "with path. Text-only models: use browser_get_elements instead of this tool. "
        "Coordinate clicks use viewport metadata from this capture."
    )
    if annotate:
        note = (
            "Labels correspond to the current get_elements indices. Vision models: "
            "call load_file with path, then browser_click_by_index. Text-only models: "
            "use browser_get_elements instead of this tool."
        )
    payload = {
        "ok": True,
        "path": rel_path,
        "screenshots_dir": "./browser/Screenshots",
        "url": info.get("url"),
        "title": info.get("title"),
        "viewport": {"w": info.get("w"), "h": info.get("h")},
        "bytes": dest.stat().st_size,
        "format": fmt,
        "note": note,
    }
    if annotate:
        payload["annotated"] = True
        payload["labeled"] = labeled
    return results.json_text(payload)
