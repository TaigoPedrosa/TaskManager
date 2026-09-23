import hashlib
import mimetypes
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

MAX_ASSET_BYTES = 20 * 1024 * 1024

# Content-addressed asset names are always 16 hex chars plus the source file's own extension
# (`store_asset` below); anything else cannot be one of ours, so both the name a caller joins
# onto `assets_dir` and the name `/assets/{name}` serves are checked against this one pattern.
ASSET_NAME_RE = re.compile(r"^[0-9a-f]{16}\.[A-Za-z0-9]{1,8}$")


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
    URL: no scheme separator, not the `figma:<fileKey>:<nodeId>` convention, and confined to the
    project root -- an absolute path or a `..` segment would otherwise let `--source` read (and
    report the hash of) any file on disk, not just one inside the project."""
    if "://" in uri or uri.startswith("figma:"):
        return False
    p = PurePosixPath(uri)
    return not p.is_absolute() and ".." not in p.parts


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
