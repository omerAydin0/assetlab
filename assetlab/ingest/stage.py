"""Build an AssetRipper input tree out of one or more Android packages.

    APK / APKM  ->  package discovery  ->  member detection  ->  staged input + manifest

Staging is deliberately faithful: every member under ``assets/`` is copied, plus
``lib/<chosen abi>/``. Detection decides *which packages* and *which ABI* to take
and produces the diagnostics, but it never filters individual asset files - a
misclassified catalog or companion stream would silently break AssetRipper, and
correctness beats saving a few megabytes.

Only one ABI is staged; two libil2cpp.so in one tree is not a valid input.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .containers import ResolvedPackage, resolve_packages
from .detect import (ASSET_BUNDLE, IL2CPP_METADATA, MANAGED, NATIVE_LIB, NESTED_ARCHIVE,
                     RESOURCE_STREAM, UNITY_DATA, UNKNOWN_BINARY, Member, PackageReport,
                     choose_abi, classify)

HEAD_BYTES = 16
SNIFF_PREFIXES = ("assets/", "lib/")


def scan_package(package: ResolvedPackage) -> tuple[PackageReport, list[str]]:
    """Classify every member of one APK. Only assets/ and lib/ are sniffed."""
    warnings: list[str] = []
    members: list[Member] = []
    try:
        archive = zipfile.ZipFile(package.path)
    except (zipfile.BadZipFile, OSError) as error:
        warnings.append(f"{package.name}: cannot read ({error})")
        return PackageReport(package.name, str(package.path), package.size, []), warnings

    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            lowered = info.filename.lower()
            if not lowered.startswith(SNIFF_PREFIXES):
                continue
            try:
                with archive.open(info) as handle:
                    head = handle.read(HEAD_BYTES)
            except (zipfile.BadZipFile, OSError, RuntimeError) as error:
                warnings.append(f"{package.name}:{info.filename}: unreadable ({error})")
                head = b""
            members.append(classify(package.name, info.filename, info.file_size, head))
    return PackageReport(package.name, str(package.path), package.size, members), warnings


def content_roots(members: list[Member]) -> list[dict]:
    """Group asset-pack bundles by their top directories, e.g. assets/android."""
    groups: dict[str, dict] = {}
    for member in members:
        if member.role != ASSET_BUNDLE:
            continue
        parts = member.path.split("/")
        root = "/".join(parts[:2]) if len(parts) > 2 else "/".join(parts[:1])
        entry = groups.setdefault(root, {"root": root, "bundles": 0, "bytes": 0,
                                         "packages": set(), "examples": []})
        entry["bundles"] += 1
        entry["bytes"] += member.size
        entry["packages"].add(member.package)
        if len(entry["examples"]) < 3:
            entry["examples"].append(member.path)
    result = []
    for entry in sorted(groups.values(), key=lambda item: -item["bundles"]):
        entry["packages"] = sorted(entry["packages"])
        result.append(entry)
    return result


def ingest(target: Path, out_dir: Path, dry_run: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    errors: list[str] = []

    packages, resolve_warnings = resolve_packages(target, out_dir)
    warnings.extend(resolve_warnings)
    if not packages:
        errors.append("no packages resolved from input")
        manifest = {"input": str(target), "packages": [], "errors": errors,
                    "warnings": warnings}
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest

    reports: list[PackageReport] = []
    for package in packages:
        report, package_warnings = scan_package(package)
        warnings.extend(package_warnings)
        reports.append(report)

    all_members = [member for report in reports for member in report.members]
    available_abis = {member.abi for member in all_members
                      if member.role == NATIVE_LIB and member.abi}
    chosen_abi = choose_abi(available_abis)
    if available_abis and len(available_abis) > 1:
        warnings.append(f"multiple ABIs present {sorted(available_abis)}; staged {chosen_abi}")
    if not available_abis:
        warnings.append("no native libraries found; IL2CPP script types may be unresolved")
    nested = [m for m in all_members if m.role == NESTED_ARCHIVE]
    if nested:
        warnings.append(
            f"{len(nested)} archive(s) nested under assets/ (e.g. {nested[0].path}); they are "
            f"staged as-is but AssetRipper does not open them - unpack them separately if "
            f"their contents matter")

    # Decide which packages to stage.
    decisions: list[dict] = []
    for report, package in zip(reports, packages):
        provides_assets = any(m.path.lower().startswith("assets/") for m in report.members)
        provides_chosen_abi = chosen_abi in report.abis if chosen_abi else False
        include = bool(report.unity_relevant and provides_assets) or provides_chosen_abi
        if not include:
            reason = ("no Unity content and no libraries for the selected ABI"
                      if report.members else "no assets/ or lib/ members")
        elif provides_chosen_abi and not provides_assets:
            reason = f"native libraries for {chosen_abi}"
        else:
            reason = f"{report.unity_relevant} Unity-relevant members"
        decisions.append({
            "name": report.name, "path": report.path, "size": report.size,
            "source": package.source, "included": include, "reason": reason,
            "abis": sorted(report.abis), "roles": report.summary(),
        })

    included_names = {d["name"] for d in decisions if d["included"]}
    staged: list[dict] = []
    collisions: list[str] = []

    if not dry_run:
        input_root = out_dir / "input"
        input_root.mkdir(parents=True, exist_ok=True)
        seen: dict[str, tuple[str, int, int]] = {}
        for report, package in zip(reports, packages):
            if report.name not in included_names:
                continue
            by_path = {member.path: member for member in report.members}
            with zipfile.ZipFile(package.path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    lowered = name.lower()
                    if lowered.startswith("lib/"):
                        member = by_path.get(name)
                        if not chosen_abi or not member or member.abi != chosen_abi:
                            continue
                    elif not lowered.startswith("assets/"):
                        continue

                    previous = seen.get(name)
                    if previous is not None:
                        if previous[1] == info.file_size and previous[2] == info.CRC:
                            continue
                        collisions.append(
                            f"{name}: {previous[0]} ({previous[1]} B) vs "
                            f"{package.name} ({info.file_size} B); kept {previous[0]}")
                        continue
                    seen[name] = (package.name, info.file_size, info.CRC)

                    destination = input_root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("wb") as sink:
                        while chunk := source.read(1 << 20):
                            sink.write(chunk)
                    member = by_path.get(name)
                    staged.append({
                        "path": name, "from": package.name, "size": info.file_size,
                        "role": member.role if member else "other",
                        "magic": member.magic if member else None,
                        "evidence": member.evidence if member else [],
                        "confidence": member.confidence if member else "low",
                    })
    warnings.extend(collisions)

    considered = [m for m in all_members if m.package in included_names]
    role_counts: dict[str, int] = {}
    for member in considered:
        role_counts[member.role] = role_counts.get(member.role, 0) + 1

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "input": str(target),
        "staged_root": str((out_dir / "input").resolve()) if not dry_run else None,
        "dry_run": dry_run,
        "chosen_abi": chosen_abi,
        "available_abis": sorted(available_abis),
        "packages": decisions,
        "unity_data_roots": sorted({
            member.path.rsplit("/", 1)[0] for member in considered
            if member.role in {UNITY_DATA, MANAGED} and "/" in member.path}),
        "content_roots": content_roots(considered),
        "il2cpp": {
            "metadata": [m.path for m in considered if m.role == IL2CPP_METADATA],
            "native_libs": [m.path for m in considered
                            if m.role == NATIVE_LIB and m.abi == chosen_abi],
        },
        "resource_streams": [m.path for m in considered if m.role == RESOURCE_STREAM],
        "nested_archives": [{"path": m.path, "size": m.size, "magic": m.magic}
                            for m in considered if m.role == NESTED_ARCHIVE],
        "role_counts": role_counts,
        "unknown_binaries": [
            {"path": m.path, "package": m.package, "size": m.size, "magic": m.magic}
            for m in considered if m.role == UNKNOWN_BINARY][:200],
        "unknown_binary_total": sum(1 for m in considered if m.role == UNKNOWN_BINARY),
        "staged_files": staged,
        "staged_count": len(staged),
        "warnings": warnings,
        "errors": errors,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def print_summary(manifest: dict) -> None:
    print(f"input        {manifest['input']}")
    print(f"ABI          {manifest.get('chosen_abi')} "
          f"(available: {', '.join(manifest.get('available_abis') or []) or 'none'})")
    print("packages:")
    for package in manifest["packages"]:
        mark = "+" if package["included"] else "-"
        print(f"  {mark} {package['name']:<34} {package['size']/1e6:7.1f} MB  "
              f"[{package['source']}] {package['reason']}")
    if manifest.get("unity_data_roots"):
        print("unity data roots:")
        for root in manifest["unity_data_roots"]:
            print(f"    {root}")
    if manifest.get("content_roots"):
        print("content roots:")
        for root in manifest["content_roots"]:
            print(f"    {root['root']:<28} {root['bundles']:>4} bundles  "
                  f"{root['bytes']/1e6:7.1f} MB")
    il2cpp = manifest.get("il2cpp") or {}
    print(f"il2cpp       metadata={len(il2cpp.get('metadata') or [])} "
          f"native_libs={len(il2cpp.get('native_libs') or [])}")
    if manifest.get("unknown_binary_total"):
        print(f"unknown      {manifest['unknown_binary_total']} unrecognised files under assets/ "
              f"(listed in manifest, staged anyway)")
    print(f"staged       {manifest.get('staged_count', 0)} files -> {manifest.get('staged_root')}")
    for warning in manifest.get("warnings", []):
        print(f"  WARN {warning}")
    for error in manifest.get("errors", []):
        print(f"  ERROR {error}")
