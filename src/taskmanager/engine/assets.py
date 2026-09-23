import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

MAX_ASSET_BYTES = 20 * 1024 * 1024


class AssetError(ValueError):
    """A refused attach; its message is shown verbatim by the CLI and the web."""


class AttachmentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str | None = None
    sha256: str | None = None
    captured_at: datetime
    checked_at: datetime | None = None
    state: Literal["fresh", "stale", "missing", "unverifiable"] = "unverifiable"


def is_project_relative(uri: str) -> bool:
    """A source counts as a project file (hashable, checkable) rather than a Figma node or a
    URL: no scheme separator and not the `figma:<fileKey>:<nodeId>` convention."""
    return "://" not in uri and not uri.startswith("figma:")


def store_asset(assets_dir: Path, source: Path) -> tuple[str, str]:
    """Copy `source` into `assets_dir`, content-addressed so re-attaching is idempotent.

    Returns (asset name, mime type).
    """
    data = source.read_bytes()
    if len(data) > MAX_ASSET_BYTES:
        raise AssetError(f"'{source.name}' is {len(data)} bytes, over the 20 MB attachment limit")
    digest = hashlib.sha256(data).hexdigest()[:16]
    name = f"{digest}{source.suffix}"
    assets_dir.mkdir(parents=True, exist_ok=True)
    dest = assets_dir / name
    if not dest.exists():
        dest.write_bytes(data)
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return name, mime
