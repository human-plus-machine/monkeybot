"""Workspace screenshot paths, JPEG encode, and index-label overlay."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_SCREENSHOTS_REL = Path("browser") / "Screenshots"
_EXT_BY_FORMAT = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png"}
LABEL_FILL = (245, 197, 24)
LABEL_TEXT = (0, 0, 0)
MAX_LABELS = 150
_DEFAULT_FONT_SIZE = 14


def workspace_root() -> Path:
    """monkeybot workspace root (for workspace-relative paths returned to the agent)."""
    for env_name in ("MONKEYBOT_WORKSPACE_ROOT", "WORKSPACE_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            p = Path(raw).expanduser()
            p = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()
            return p
    return (Path.cwd() / "workspace").resolve()


def screenshots_dir() -> Path:
    """Absolute directory where browser screenshot captures are written."""
    raw = os.environ.get("BROWSER_MCP_SCREENSHOTS_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        p = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()
        return p
    return (workspace_root() / _SCREENSHOTS_REL).resolve()


def workspace_relative(abs_path: Path) -> str:
    """Return a workspace-relative path suitable for ``load_file`` / ``read_file``."""
    resolved = abs_path.resolve()
    root = workspace_root()
    try:
        rel = resolved.relative_to(root)
        return f"./{rel.as_posix()}"
    except ValueError:
        shots = screenshots_dir()
        try:
            rel = resolved.relative_to(shots)
            return f"./{_SCREENSHOTS_REL.as_posix()}/{rel.as_posix()}"
        except ValueError:
            return f"./{_SCREENSHOTS_REL.as_posix()}/{resolved.name}"


def allocate_screenshot_path(fmt: str = "jpeg") -> tuple[Path, str]:
    """Create a unique screenshot path under the screenshots directory."""
    ext = _EXT_BY_FORMAT.get(fmt, ".jpg")
    shots_dir = screenshots_dir()
    shots_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"shot-{ts}-{uuid.uuid4().hex[:8]}{ext}"
    abs_path = shots_dir / name
    return abs_path, workspace_relative(abs_path)


def _label_font() -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=_DEFAULT_FONT_SIZE)
    except TypeError:
        return ImageFont.load_default()


def draw_index_labels(
    img: Image.Image,
    rects: dict[Any, Any],
    *,
    css_width: float,
    css_height: float,
    max_labels: int = MAX_LABELS,
) -> tuple[Image.Image, int]:
    """Draw index badges at each rect's top-left. Returns (RGB image, labeled count)."""
    out = img.convert("RGB")
    if css_width <= 0 or css_height <= 0 or not rects:
        return out, 0
    sx = out.width / css_width
    sy = out.height / css_height
    items: list[tuple[int, dict[str, Any]]] = []
    for key, box in rects.items():
        if not isinstance(box, dict):
            continue
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        items.append((idx, box))
    items.sort(key=lambda item: item[0])
    draw = ImageDraw.Draw(out)
    font = _label_font()
    padding = 2
    labeled = 0
    for idx, box in items[:max_labels]:
        x = float(box.get("x") or 0) * sx
        y = float(box.get("y") or 0) * sy
        text = str(idx)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x0 = max(0, min(int(x), max(out.width - 1, 0)))
        y0 = max(0, min(int(y), max(out.height - 1, 0)))
        x1 = min(out.width, x0 + tw + padding * 2)
        y1 = min(out.height, y0 + th + padding * 2)
        draw.rectangle([x0, y0, x1, y1], fill=LABEL_FILL)
        draw.text((x0 + padding, y0 + padding), text, fill=LABEL_TEXT, font=font)
        labeled += 1
    return out, labeled


def encode_screenshot(
    src: Path,
    dest: Path,
    *,
    fmt: str = "jpeg",
    quality: int = 60,
    max_dim: int | None = 1200,
    image: Image.Image | None = None,
) -> None:
    """Resize and write ``src`` (or ``image``) as JPEG or PNG at ``dest``."""
    opened = image if image is not None else Image.open(src)
    img = opened.convert("RGB") if fmt in {"jpeg", "jpg"} else opened.convert("RGB")
    if image is None:
        opened.close()
    if max_dim and max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        if fmt in {"jpeg", "jpg"}:
            img.save(tmp, format="JPEG", quality=quality, optimize=True)
        else:
            img.save(tmp, format="PNG")
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        img.close()
