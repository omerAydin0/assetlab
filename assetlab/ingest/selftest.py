"""Ingestion tests over synthetic packages shaped like three real layouts.

Fixtures are built as real zips, run through the real ingest path, and asserted on
the resulting manifest - so these check discovery and staging behaviour, not string
matching against hardcoded paths.

    A  base(assets/bin/Data + assets/aa bundles) + a loose ABI split
    B  base + UnityDataAssetPack(assets/android/<groups>), with the ABI split
       only inside an .apkm container
    C  everything inside one .apkm, bundles both extensionless and .ab
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from .detect import (ASSET_BUNDLE, IL2CPP_METADATA, NATIVE_LIB, NESTED_ARCHIVE,
                     RESOURCE_STREAM, UNITY_DATA, UNKNOWN_BINARY, classify,
                     serialized_file_version)
from .stage import ingest

PASSED: list[str] = []
FAILED: list[str] = []

UNITYFS = b"UnityFS\x00\x00\x00\x00\x08" + b"\x00" * 200
METADATA = b"\xaf\x1b\xb1\xfa\x1f\x00\x00\x00" + b"\x00" * 200
ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 200
FSB5 = b"FSB5\x01\x00\x00\x00" + b"\x00" * 200
ZIP = b"PK\x03\x04\x14\x00\x00\x00" + b"\x00" * 200
# SerializedFile headers: four big-endian uint32; v22+ zeroes the 32-bit sizes.
SERIALIZED_V22 = b"\x00" * 8 + (22).to_bytes(4, "big") + b"\x00" * 4 + b"\x00" * 200
SERIALIZED_V17 = ((512).to_bytes(4, "big") + (4096).to_bytes(4, "big")
                  + (17).to_bytes(4, "big") + (600).to_bytes(4, "big") + b"\x00" * 200)
JUNK = b"\x00" * 64 + b"not a unity file"


def check(label: str, got, want) -> None:
    (PASSED if got == want else FAILED).append(f"{label}: got {got!r}, want {want!r}")


def check_true(label: str, value) -> None:
    check(label, bool(value), True)


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def base_apk_entries(extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    entries = {
        "AndroidManifest.xml": b"\x03\x00\x08\x00manifest",
        "classes.dex": b"dex\n035\x00" + b"\x00" * 100,
        "assets/bin/Data/data.unity3d": UNITYFS,
        "assets/bin/Data/boot.config": b"scripting-runtime-version=latest\n",
        "assets/bin/Data/Managed/Metadata/global-metadata.dat": METADATA,
        "assets/bin/Data/unity default resources": JUNK,
    }
    entries.update(extra or {})
    return entries


def abi_apk(path: Path, abi: str) -> Path:
    return make_zip(path, {
        "AndroidManifest.xml": b"\x03\x00\x08\x00manifest",
        f"lib/{abi}/libil2cpp.so": ELF,
        f"lib/{abi}/libunity.so": ELF,
    })


def roles_of(manifest: dict) -> dict[str, int]:
    return manifest.get("role_counts", {})


def staged_paths(manifest: dict) -> set[str]:
    return {entry["path"] for entry in manifest.get("staged_files", [])}


def included(manifest: dict) -> set[str]:
    return {p["name"] for p in manifest["packages"] if p["included"]}


def excluded(manifest: dict) -> set[str]:
    return {p["name"] for p in manifest["packages"] if not p["included"]}


def case_a(work: Path) -> None:
    """Layout A: one base APK plus a loose ABI split, Addressables under assets/aa."""
    source = work / "a_in"
    make_zip(source / "com.example.match.apk", base_apk_entries({
        "assets/aa/catalog.json": b'{"m_LocatorId":"AddressablesMainContentCatalog"}',
        "assets/aa/Android/ui_common_assets_all_abc.bundle": UNITYFS,
        "assets/aa/Android/particles_assets_all_def.bundle": UNITYFS,
        "assets/mraid.js": b"// ad sdk",
    }))
    abi_apk(source / "config.arm64_v8a.apk", "arm64-v8a")

    manifest = ingest(source, work / "a_out")
    check("A included packages", included(manifest),
          {"com.example.match.apk", "config.arm64_v8a.apk"})
    check("A chosen abi", manifest["chosen_abi"], "arm64-v8a")
    check("A unity data root", "assets/bin/Data" in manifest["unity_data_roots"], True)
    check("A content root", [r["root"] for r in manifest["content_roots"]], ["assets/aa"])
    check("A addressable bundles", manifest["content_roots"][0]["bundles"], 2)
    check("A il2cpp metadata", len(manifest["il2cpp"]["metadata"]), 1)
    check("A native libs", len(manifest["il2cpp"]["native_libs"]), 2)
    check_true("A staged data.unity3d",
               "assets/bin/Data/data.unity3d" in staged_paths(manifest))
    check_true("A staged libil2cpp",
               "lib/arm64-v8a/libil2cpp.so" in staged_paths(manifest))
    # Nothing outside assets/ or lib/ belongs in an AssetRipper input tree.
    check("A no dex staged", any(p.endswith(".dex") for p in staged_paths(manifest)), False)


def case_b(work: Path) -> None:
    """Layout B: loose base + asset pack, ABI split only inside an .apkm."""
    source = work / "b_in"
    make_zip(source / "base.apk", base_apk_entries())
    make_zip(source / "split_UnityDataAssetPack.apk", {
        "AndroidManifest.xml": b"\x03\x00\x08\x00manifest",
        "assets/bin/Data/datapack.unity3d": UNITYFS,
        "assets/bin/Data/resources.resource": FSB5,
        "assets/android/gameplay/gameplay_bundle_config.json": b'{"bundles":[]}',
        "assets/android/gameplay/forest_level/ui": UNITYFS,
        "assets/android/gameplay/richards_journey/rock_fall": UNITYFS,
        "assets/android/world/world_common": UNITYFS,
        "assets/android/liveops/card_collection": UNITYFS,
        "assets/android/game_common": UNITYFS,
        "assets/android/notes.txt": JUNK,
    })
    # The container also holds copies of base and the asset pack, plus both ABIs.
    inner = work / "b_inner"
    packed = {
        "base.apk": make_zip(inner / "base.apk", base_apk_entries()).read_bytes(),
        "split_UnityDataAssetPack.apk": (source / "split_UnityDataAssetPack.apk").read_bytes(),
        "split_config.arm64_v8a.apk": abi_apk(inner / "arm64.apk", "arm64-v8a").read_bytes(),
        "split_config.armeabi_v7a.apk": abi_apk(inner / "v7a.apk", "armeabi-v7a").read_bytes(),
    }
    make_zip(source / "com.example.layout_b.apkm", packed)

    manifest = ingest(source, work / "b_out")
    check("B loose base preferred",
          next(p["source"] for p in manifest["packages"] if p["name"] == "base.apk"), "loose")
    check("B abi recovered from container",
          next(p["source"].startswith("container:") for p in manifest["packages"]
               if p["name"] == "split_config.arm64_v8a.apk"), True)
    check("B second abi excluded", "split_config.armeabi_v7a.apk" in excluded(manifest), True)
    check("B chosen abi", manifest["chosen_abi"], "arm64-v8a")
    check("B content root", [r["root"] for r in manifest["content_roots"]], ["assets/android"])
    check("B extensionless bundles counted", manifest["content_roots"][0]["bundles"], 5)
    check("B resource stream detected", manifest["resource_streams"],
          ["assets/bin/Data/resources.resource"])
    staged = staged_paths(manifest)
    # Both packages contribute to one bin/Data directory; neither may be lost.
    check_true("B merged base data", "assets/bin/Data/data.unity3d" in staged)
    check_true("B merged pack data", "assets/bin/Data/datapack.unity3d" in staged)
    check_true("B duplicate packages warned",
               any("also present in container" in w for w in manifest["warnings"]))
    check("B non-unity file still staged, not dropped",
          "assets/android/notes.txt" in staged, True)


def case_c(work: Path) -> None:
    """Layout C: a single .apkm, bundles both extensionless and .ab."""
    source = work / "c_in"
    inner = work / "c_inner"
    pack_entries = {
        "AndroidManifest.xml": b"\x03\x00\x08\x00manifest",
        "assets/bin/Data/datapack.unity3d": UNITYFS,
        "assets/bin/Data/resources.resource": FSB5,
        "assets/android/area01.ab": UNITYFS,
        "assets/android/area02.ab": UNITYFS,
        "assets/android/extra01.ab": UNITYFS,
        "assets/android/episodes_1_2_3_4": UNITYFS,
        "assets/android/fallbackfont": UNITYFS,
        "assets/android/kingdrill_v3": UNITYFS,
        "assets/android/readme": JUNK,
    }
    packed = {
        "base.apk": make_zip(inner / "base.apk", base_apk_entries()).read_bytes(),
        "split_UnityDataAssetPack.apk": make_zip(inner / "pack.apk", pack_entries).read_bytes(),
        "split_config.arm64_v8a.apk": abi_apk(inner / "arm64.apk", "arm64-v8a").read_bytes(),
        "split_config.armeabi_v7a.apk": abi_apk(inner / "v7a.apk", "armeabi-v7a").read_bytes(),
    }
    make_zip(source / "com.example.layout_c.apkm", packed)

    manifest = ingest(source, work / "c_out")
    check("C all packages from container",
          {p["source"].split(":")[0] for p in manifest["packages"]}, {"container"})
    check("C included", included(manifest),
          {"base.apk", "split_UnityDataAssetPack.apk", "split_config.arm64_v8a.apk"})
    check("C excluded", excluded(manifest), {"split_config.armeabi_v7a.apk"})
    check("C content root", [r["root"] for r in manifest["content_roots"]], ["assets/android"])
    check("C mixed .ab and extensionless", manifest["content_roots"][0]["bundles"], 6)
    # Only `assets/android/readme` qualifies: files inside bin/Data are Unity support
    # files, not unknowns, even when their bytes carry no recognisable signature.
    check("C unknown binary reported", manifest["unknown_binary_total"], 1)
    check_true("C readme flagged unknown, not a bundle",
               any(entry["path"].endswith("android/readme")
                   for entry in manifest["unknown_binaries"]))
    check("C only one abi staged",
          {Path(p).parts[1] for p in staged_paths(manifest) if p.startswith("lib/")},
          {"arm64-v8a"})


def unit_checks() -> None:
    """Format sniffing must never trust an extension."""
    check("extensionless UnityFS -> bundle",
          classify("p", "assets/android/foo", 10, UNITYFS).role, ASSET_BUNDLE)
    check(".ab that is not UnityFS -> not a bundle",
          classify("p", "assets/android/fake.ab", 10, JUNK).role, UNKNOWN_BINARY)
    check("bin/Data UnityFS -> unity data",
          classify("p", "assets/bin/Data/datapack.unity3d", 10, UNITYFS).role, UNITY_DATA)
    check("metadata by magic alone",
          classify("p", "assets/bin/Data/Managed/Metadata/x.dat", 10, METADATA).role,
          IL2CPP_METADATA)
    check("native lib", classify("p", "lib/arm64-v8a/libil2cpp.so", 10, ELF).role, NATIVE_LIB)
    check("native lib abi", classify("p", "lib/armeabi-v7a/libunity.so", 10, ELF).abi,
          "armeabi-v7a")
    check("resource stream",
          classify("p", "assets/bin/Data/resources.resource", 10, FSB5).role, RESOURCE_STREAM)
    check("evidence recorded",
          "magic:UnityFS" in classify("p", "assets/android/x", 10, UNITYFS).evidence, True)

    # Loose player data has no ASCII magic; the SerializedFile header must be decoded.
    check("serialized file v22 -> unity data",
          classify("p", "assets/bin/Data/globalgamemanagers", 10, SERIALIZED_V22).role,
          UNITY_DATA)
    check("serialized file v17 -> unity data",
          classify("p", "assets/bin/Data/level0", 10, SERIALIZED_V17).role, UNITY_DATA)
    check("serialized header outside bin/Data is not player data",
          classify("p", "assets/other/thing", 10, SERIALIZED_V22).role, UNKNOWN_BINARY)
    check("split part -> unity data",
          classify("p", "assets/bin/Data/sharedassets0.assets.split3", 10, JUNK).role,
          UNITY_DATA)
    check("nested zip flagged, not called a bundle",
          classify("p", "assets/MapBundles/map_1_2.zip", 10, ZIP).role, NESTED_ARCHIVE)
    check("version out of range is not a serialized file",
          serialized_file_version(b"\x00" * 8 + (99).to_bytes(4, "big") + b"\x00" * 4), None)


def main() -> int:
    unit_checks()
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        case_a(work)
        case_b(work)
        case_c(work)
    for line in FAILED:
        print("FAIL", line)
    print(f"ingest: {len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
