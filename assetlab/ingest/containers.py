"""Resolve whatever the user points at into a flat list of APK files on disk.

Accepts a single .apk, a directory of them, or an .apkm/.apks/.xapk/.zip split
container. Container members are extracted into ``<staging>/_packages`` so the
result is inspectable rather than hidden inside a temporary buffer.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

CONTAINER_SUFFIXES = {".apkm", ".apks", ".xapk", ".zip"}
APK_SUFFIX = ".apk"


@dataclass
class ResolvedPackage:
    name: str
    path: Path
    source: str          # "loose" or "container:<file name>"
    size: int


def _is_container(path: Path) -> bool:
    return path.suffix.lower() in CONTAINER_SUFFIXES


def _extract_container(container: Path, work_dir: Path,
                       warnings: list[str]) -> list[ResolvedPackage]:
    try:
        archive = zipfile.ZipFile(container)
    except (zipfile.BadZipFile, OSError) as error:
        warnings.append(f"{container.name}: not a readable archive ({error})")
        return []

    found: list[ResolvedPackage] = []
    with archive:
        members = [n for n in archive.namelist() if n.lower().endswith(APK_SUFFIX)]
        if not members:
            warnings.append(f"{container.name}: archive holds no .apk members")
            return []
        target_dir = work_dir / "_packages" / container.stem
        target_dir.mkdir(parents=True, exist_ok=True)
        for member in sorted(members):
            info = archive.getinfo(member)
            target = target_dir / Path(member).name
            if not target.exists() or target.stat().st_size != info.file_size:
                with archive.open(member) as source, target.open("wb") as sink:
                    while chunk := source.read(1 << 20):
                        sink.write(chunk)
            found.append(ResolvedPackage(target.name, target,
                                         f"container:{container.name}", info.file_size))
    return found


def resolve_packages(target: Path, work_dir: Path) -> tuple[list[ResolvedPackage], list[str]]:
    """Return every APK reachable from `target`, de-duplicated by file name.

    Loose APKs win over container copies of the same name, because a user who
    already unpacked a build normally means those. The container is still read so
    that pieces missing from the loose set (commonly the ABI split) are recovered.
    """
    warnings: list[str] = []
    loose: list[Path] = []
    containers: list[Path] = []

    if target.is_file():
        (containers if _is_container(target) else loose).append(target)
    elif target.is_dir():
        for path in sorted(target.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() == APK_SUFFIX:
                loose.append(path)
            elif _is_container(path):
                containers.append(path)
    else:
        warnings.append(f"input not found: {target}")
        return [], warnings

    resolved: list[ResolvedPackage] = [
        ResolvedPackage(path.name, path, "loose", path.stat().st_size) for path in loose
    ]
    for container in containers:
        for package in _extract_container(container, work_dir, warnings):
            existing = next((p for p in resolved if p.name == package.name), None)
            if existing is None:
                resolved.append(package)
                continue
            if existing.size != package.size:
                warnings.append(
                    f"{package.name}: '{existing.source}' ({existing.size} B) and "
                    f"'{package.source}' ({package.size} B) differ; kept {existing.source}")
            else:
                warnings.append(f"{package.name}: also present in {package.source}, "
                                f"kept {existing.source}")

    if not resolved:
        warnings.append(f"no .apk found in {target}")
    return resolved, warnings
