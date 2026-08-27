"""Android package ingestion: turn APK/APKM inputs into an AssetRipper input tree.

This layer sits *before* AssetRipper and is deliberately separate from the Unity
export analysis in the rest of AssetLab:

    APK/APKM -> discovery -> detection -> staging + manifest
             -> AssetRipper (Unity Project export)
             -> assetlab.run (index, graph, slice, classify, dedup, levels, browser)

Nothing here is game specific; layouts are discovered from file magics and paths.
"""

from .containers import ResolvedPackage, resolve_packages
from .detect import Member, PackageReport, choose_abi, classify, sniff_magic
from .stage import ingest, print_summary, scan_package

__all__ = ["ResolvedPackage", "resolve_packages", "Member", "PackageReport",
           "choose_abi", "classify", "sniff_magic", "ingest", "print_summary",
           "scan_package"]
