from pathlib import Path

import pytest

from taskmanager.engine.assets import ASSET_NAME_RE, store_asset


@pytest.mark.parametrize(
    ("filename", "suffix"),
    [
        ("Dockerfile", ".bin"),
        ("LICENSE", ".bin"),
        ("a.typescript", ".bin"),
        ("a.c++", ".bin"),
        ("a.tar-gz", ".bin"),
        ("shot.png", ".png"),
        ("a.12345678", ".12345678"),
    ],
)
def test_store_asset_names_every_source_with_a_servable_name(
    tmp_path: Path, filename: str, suffix: str
) -> None:
    source = tmp_path / filename
    source.write_bytes(b"content")

    name, _ = store_asset(tmp_path / "assets", source)

    assert ASSET_NAME_RE.fullmatch(name)
    assert name.endswith(suffix)
    assert (tmp_path / "assets" / name).read_bytes() == b"content"
