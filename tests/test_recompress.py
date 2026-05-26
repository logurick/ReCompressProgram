from pathlib import Path
import tarfile
import zipfile

import pytest

from recompress import find_archives, recompress_archive, split_wide_images_in_directory


def test_recompress_zip_moves_original_and_reuses_name(tmp_path: Path) -> None:
    source = tmp_path / "sample.zip"
    with zipfile.ZipFile(source, "w") as zip_file:
        zip_file.writestr("hello.txt", "hello")

    result = recompress_archive(source)

    assert result.status == "ok"
    assert result.output == source
    assert (tmp_path / "before" / "sample.zip").exists()
    with zipfile.ZipFile(result.output) as zip_file:
        assert zip_file.read("hello.txt") == b"hello"


def test_recompress_tar_gz_creates_zip(tmp_path: Path) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("payload", encoding="utf-8")
    source = tmp_path / "sample.tar.gz"
    with tarfile.open(source, "w:gz") as tar:
        tar.add(payload, arcname="payload.txt")

    result = recompress_archive(source)

    assert result.status == "ok"
    assert result.output == tmp_path / "sample.zip"
    with zipfile.ZipFile(result.output) as zip_file:
        assert zip_file.read("payload.txt") == b"payload"


def test_find_archives_can_exclude_zip(tmp_path: Path) -> None:
    (tmp_path / "a.zip").write_bytes(b"")
    (tmp_path / "b.tar.gz").write_bytes(b"")
    (tmp_path / "c.txt").write_text("x", encoding="utf-8")

    archives = find_archives([tmp_path], exclude_zip=True)

    assert archives == [tmp_path / "b.tar.gz"]


def test_find_archives_skips_before_folder(tmp_path: Path) -> None:
    before = tmp_path / "before"
    before.mkdir()
    (before / "old.zip").write_bytes(b"")
    (tmp_path / "active.zip").write_bytes(b"")

    archives = find_archives([tmp_path])

    assert archives == [tmp_path / "active.zip"]


def test_split_wide_images_right_to_left(tmp_path: Path) -> None:
    pillow = pytest.importorskip("PIL.Image")

    source = tmp_path / "spread.png"
    image = pillow.new("RGB", (4, 2))
    for x in range(2):
        for y in range(2):
            image.putpixel((x, y), (255, 0, 0))
    for x in range(2, 4):
        for y in range(2):
            image.putpixel((x, y), (0, 0, 255))
    image.save(source)

    count = split_wide_images_in_directory(tmp_path, split_order="right_left")

    assert count == 1
    assert not source.exists()
    with pillow.open(tmp_path / "spread_001.png") as first:
        assert first.getpixel((0, 0)) == (0, 0, 255)
    with pillow.open(tmp_path / "spread_002.png") as second:
        assert second.getpixel((0, 0)) == (255, 0, 0)
