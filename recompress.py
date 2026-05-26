from __future__ import annotations

import bz2
import gzip
import lzma
import os
import io
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4


LogCallback = Callable[[str], None]
SplitOrder = str

ZIP_EXTENSIONS = {".zip"}
TAR_EXTENSIONS = {
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
}
SINGLE_FILE_EXTENSIONS = {".gz", ".bz2", ".xz"}
SEVEN_ZIP_EXTENSIONS = {".7z", ".rar", ".cab", ".iso", ".arj", ".lzh"}
SUPPORTED_EXTENSIONS = ZIP_EXTENSIONS | TAR_EXTENSIONS | SINGLE_FILE_EXTENSIONS | SEVEN_ZIP_EXTENSIONS
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
SEVEN_ZIP_EXECUTABLE_NAMES = ("7z", "7zz", "7z.exe", "7zz.exe")
SEVEN_ZIP_DEFAULT_PATHS = (
    Path(os.environ.get("ProgramFiles", "")) / "7-Zip" / "7z.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "7-Zip" / "7z.exe",
)


@dataclass(frozen=True)
class RecompressResult:
    source: Path
    output: Path | None
    status: str
    message: str


@dataclass(frozen=True)
class ArchiveImageSummary:
    source: Path
    wide_image_count: int | None
    first_image_png: bytes | None
    message: str = ""


def find_archives(paths: Iterable[Path], exclude_zip: bool = False) -> list[Path]:
    archives: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            for child in path.rglob("*"):
                if is_internal_path(child, path):
                    continue
                if child.is_file() and is_supported_archive(child, exclude_zip):
                    archives.append(child)
        elif path.is_file() and is_supported_archive(path, exclude_zip):
            archives.append(path)
    return sorted(set(archives))


def is_internal_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part == "before" or part.startswith("recompress_") for part in parts)


def is_supported_archive(path: Path, exclude_zip: bool = False) -> bool:
    archive_suffix = get_archive_suffix(path)
    if not archive_suffix:
        return False
    if exclude_zip and archive_suffix in ZIP_EXTENSIONS:
        return False
    return True


def get_archive_suffix(path: Path) -> str | None:
    name = path.name.lower()
    for suffix in sorted(SUPPORTED_EXTENSIONS, key=len, reverse=True):
        if name.endswith(suffix):
            return suffix
    return None


def recompress_paths(
    paths: Iterable[Path],
    exclude_zip: bool = False,
    split_wide_images: bool = False,
    split_order: SplitOrder = "right_left",
    log: LogCallback | None = None,
) -> list[RecompressResult]:
    archives = find_archives(paths, exclude_zip=exclude_zip)
    if not archives:
        return []

    results: list[RecompressResult] = []
    for archive in archives:
        results.append(
            recompress_archive(
                archive,
                split_wide_images=split_wide_images,
                split_order=split_order,
                log=log,
            )
        )
    return results


def recompress_archive(
    source: Path,
    split_wide_images: bool = False,
    split_order: SplitOrder = "right_left",
    log: LogCallback | None = None,
) -> RecompressResult:
    source = source.resolve()
    suffix = get_archive_suffix(source)
    if suffix is None:
        return RecompressResult(source, None, "skipped", "Unsupported archive type")

    output = final_output_path(source)
    if output.exists() and output != source:
        return RecompressResult(source, None, "failed", f"Output already exists: {output}")
    temp_output = temporary_output_path(source)
    try:
        if log:
            log(f"Extracting: {source}")
        temp_path = create_work_directory(source)
        try:
            extract_archive(source, temp_path, suffix)
            if split_wide_images:
                count = split_wide_images_in_directory(temp_path, split_order=split_order)
                if log:
                    log(f"Split wide images: {count}")

            if log:
                log(f"Creating ZIP: {temp_output}")
            create_zip_from_directory(temp_path, temp_output)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

        if log:
            log(f"Moving original to before: {source}")
        move_original_to_before(source)
        temp_output.replace(output)
    except Exception as exc:  # noqa: BLE001 - GUI should surface any archive failure.
        temp_output.unlink(missing_ok=True)
        return RecompressResult(source, None, "failed", str(exc))

    return RecompressResult(source, output, "ok", "Recompressed successfully")


def extract_archive(source: Path, destination: Path, suffix: str) -> None:
    if suffix in ZIP_EXTENSIONS:
        extract_zip(source, destination)
    elif suffix in TAR_EXTENSIONS:
        extract_tar(source, destination)
    elif suffix in SINGLE_FILE_EXTENSIONS:
        extract_single_file(source, destination, suffix)
    elif suffix in SEVEN_ZIP_EXTENSIONS:
        extract_with_7zip(source, destination)
    else:
        raise ValueError(f"Unsupported archive type: {suffix}")


def extract_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as zip_file:
        for member in zip_file.infolist():
            target = safe_join(destination, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def extract_tar(source: Path, destination: Path) -> None:
    with tarfile.open(source) as tar:
        for member in tar.getmembers():
            target = safe_join(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            with extracted, target.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)


def extract_single_file(source: Path, destination: Path, suffix: str) -> None:
    output_name = strip_archive_suffix(source.name, suffix)
    target = safe_join(destination, output_name)
    target.parent.mkdir(parents=True, exist_ok=True)

    if suffix == ".gz":
        opener = gzip.open
    elif suffix == ".bz2":
        opener = bz2.open
    elif suffix == ".xz":
        opener = lzma.open
    else:
        raise ValueError(f"Unsupported single-file archive: {suffix}")

    with opener(source, "rb") as src, target.open("wb") as dst:
        shutil.copyfileobj(src, dst)


def extract_with_7zip(source: Path, destination: Path) -> None:
    seven_zip = find_7zip_executable()
    if not seven_zip:
        raise RuntimeError("7-Zip was not found. Install 7-Zip to process .rar, .7z, and related archive types.")

    command = [seven_zip, "x", str(source), "-y", f"-o{destination}"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(details or "7-Zip extraction failed.")


def find_7zip_executable() -> str | None:
    for executable in SEVEN_ZIP_EXECUTABLE_NAMES:
        found = shutil.which(executable)
        if found:
            return found

    for path in SEVEN_ZIP_DEFAULT_PATHS:
        if path and path.exists():
            return str(path)

    return None


def create_zip_from_directory(source_dir: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                zip_file.write(path, path.relative_to(source_dir))


def inspect_archive_images(source: Path, preview_size: tuple[int, int] = (260, 260)) -> ArchiveImageSummary:
    source = source.resolve()
    suffix = get_archive_suffix(source)
    if suffix is None:
        return ArchiveImageSummary(source, None, None, "Unsupported archive type")

    temp_path = create_work_directory(source)
    try:
        extract_archive(source, temp_path, suffix)
        return inspect_images_in_directory(source, temp_path, preview_size=preview_size)
    except Exception as exc:  # noqa: BLE001 - GUI should show a short inspection failure.
        return ArchiveImageSummary(source, None, None, str(exc))
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def inspect_images_in_directory(
    source: Path,
    directory: Path,
    preview_size: tuple[int, int] = (260, 260),
) -> ArchiveImageSummary:
    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:
        return ArchiveImageSummary(source, None, None, f"Pillow is required: {exc}")

    wide_count = 0
    first_preview: bytes | None = None
    for image_path in sorted(directory.rglob("*")):
        if not image_path.is_file() or image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        try:
            with Image.open(image_path) as image:
                frame = next(ImageSequence.Iterator(image)).copy()
                width, height = frame.size
                if width > height:
                    wide_count += 1
                if first_preview is None:
                    first_preview = image_to_png_bytes(frame, preview_size)
        except Exception:
            continue

    message = "" if first_preview else "No image found"
    return ArchiveImageSummary(source, wide_count, first_preview, message)


def image_to_png_bytes(image: object, preview_size: tuple[int, int]) -> bytes:
    if getattr(image, "mode", "") not in {"RGB", "RGBA"}:
        image = image.convert("RGBA")
    image.thumbnail(preview_size)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def create_work_directory(source: Path) -> Path:
    base = source.parent
    while True:
        candidate = base / f"recompress_{uuid4().hex}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue


def split_wide_images_in_directory(directory: Path, split_order: SplitOrder = "right_left") -> int:
    if split_order not in {"right_left", "left_right"}:
        raise ValueError("split_order must be 'right_left' or 'left_right'.")

    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image splitting. Run `pip install -r requirements.txt`.") from exc

    split_count = 0
    for image_path in sorted(directory.rglob("*")):
        if not image_path.is_file() or image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        with Image.open(image_path) as image:
            frame = next(ImageSequence.Iterator(image)).copy()
            width, height = frame.size
            if width <= height:
                continue

            center = width // 2
            left = frame.crop((0, 0, center, height))
            right = frame.crop((center, 0, width, height))

            first, second = (right, left) if split_order == "right_left" else (left, right)
            first_path, second_path = split_output_paths(image_path)

            save_image(first, first_path)
            save_image(second, second_path)

        image_path.unlink()
        split_count += 1

    return split_count


def split_output_paths(image_path: Path) -> tuple[Path, Path]:
    stem = image_path.stem
    suffix = image_path.suffix
    first = image_path.with_name(f"{stem}_001{suffix}")
    second = image_path.with_name(f"{stem}_002{suffix}")
    if not first.exists() and not second.exists():
        return first, second

    counter = 1
    while True:
        first = image_path.with_name(f"{stem}_split{counter}_001{suffix}")
        second = image_path.with_name(f"{stem}_split{counter}_002{suffix}")
        if not first.exists() and not second.exists():
            return first, second
        counter += 1


def save_image(image: object, path: Path) -> None:
    suffix = path.suffix.lower()
    save_kwargs = {}
    if suffix in {".jpg", ".jpeg"} and getattr(image, "mode", "") not in {"RGB", "L"}:
        image = image.convert("RGB")
    if suffix in {".jpg", ".jpeg"}:
        save_kwargs["quality"] = 95
    image.save(path, **save_kwargs)


def final_output_path(source: Path) -> Path:
    suffix = get_archive_suffix(source)
    if suffix is None:
        raise ValueError(f"Unsupported archive type: {source}")

    base_name = strip_archive_suffix(source.name, suffix)
    return source.with_name(f"{base_name}.zip")


def temporary_output_path(source: Path) -> Path:
    while True:
        candidate = source.with_name(f"recompress_output_{uuid4().hex}.zip")
        if not candidate.exists():
            return candidate


def move_original_to_before(source: Path) -> Path:
    before_dir = source.parent / "before"
    before_dir.mkdir(exist_ok=True)
    destination = before_dir / source.name
    if destination.exists():
        destination = next_before_path(destination)
    source.replace(destination)
    return destination


def next_before_path(path: Path) -> Path:
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def strip_archive_suffix(name: str, suffix: str) -> str:
    return name[: -len(suffix)] if name.lower().endswith(suffix) else Path(name).stem


def safe_join(base: Path, name: str) -> Path:
    cleaned = name.replace("\\", "/").lstrip("/")
    target = (base / cleaned).resolve()
    base_resolved = base.resolve()
    if os.path.commonpath([base_resolved, target]) != str(base_resolved):
        raise ValueError(f"Unsafe archive member path: {name}")
    return target
