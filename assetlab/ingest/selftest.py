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

from .detect import (ASSET_BUNDLE, IL2CPP_METADATA, MANAGED, NATIVE_LIB, NESTED_ARCHIVE,
                     RESOURCE_STREAM, UNITY_DATA, UNKNOWN_BINARY, classify,
                     serialized_file_version, unity_version)
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
#: The first bytes of any PE file. A Mono build ships these managed assemblies in
#: place of IL2CPP's metadata and native library.
DLL = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200


def unityfs(version: str = "2022.3.10f1") -> bytes:
    """A UnityFS head shaped like a real one: signature, format, then two versions.

    The bare UNITYFS constant deliberately declares no version, so the detection
    tests need their own fixture rather than borrowing one that would pass by
    accident.
    """
    return (b"UnityFS\x00" + (8).to_bytes(4, "big") + b"5.x.x\x00"
            + version.encode() + b"\x00" + b"\x00" * 180)


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


def case_hostile(work: Path) -> None:
    """A build whose zip entries try to write outside the staging tree.

    Builds come from wherever the user got them, and a zip entry names its own
    destination. Nothing in the format stops it naming one several levels up.
    """
    source = work / "hostile_in"
    make_zip(source / "com.example.hostile.apk", base_apk_entries({
        "assets/../../../escaped.txt": b"should never be written",
        "assets/ok/real.bundle": UNITYFS,
    }))

    out = work / "hostile_out"
    manifest = ingest(source, out)
    staged = staged_paths(manifest)

    check("hostile traversal entry not staged",
          any("escaped" in p for p in staged), False)
    check_true("hostile traversal reported",
               any("escapes the staging tree" in note
                   for note in manifest.get("warnings", [])))
    # The parent of the staging tree must be untouched, whatever the entry claimed.
    check("hostile nothing written above the tree",
          list(out.parent.glob("escaped.txt")) + list(out.glob("../escaped.txt")), [])
    check_true("hostile legitimate entry still staged",
               any(p.endswith("assets/ok/real.bundle") for p in staged))


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

    # The declared Unity version, read from bytes - so a build that says nothing
    # produces no answer instead of a guess.
    check("unity version from a UnityFS head",
          unity_version(unityfs("2021.3.37f1")), "2021.3.37f1")
    check("unity 6 versions read the same way",
          unity_version(unityfs("6000.0.71f1")), "6000.0.71f1")
    check("a container declaring nothing yields nothing", unity_version(UNITYFS), None)
    check("a bare number is not a version", unity_version(b"12.5\x00" + JUNK), None)
    check("an unterminated version is not accepted",
          unity_version(b"UnityFS\x00" + b"5.x.x\x002021.3.37f1"), None)
    check("the version reaches the member record",
          classify("p", "assets/bin/Data/data.unity3d", 10, unityfs()).version,
          "2022.3.10f1")
    check("the version is recorded as evidence",
          "unity:2022.3.10f1" in
          classify("p", "assets/bin/Data/data.unity3d", 10, unityfs()).evidence, True)
    check("a managed assembly is not mistaken for player data",
          classify("p", "assets/bin/Data/Managed/Assembly-CSharp.dll", 10, DLL).role,
          MANAGED)


def case_mono(work: Path) -> None:
    """A Mono build: managed assemblies, and no IL2CPP anything.

    Missing global-metadata.dat and libil2cpp.so is a property of how this game was
    built, not a fault in the package, so it must read as a limit and never as a
    failure.
    """
    source = work / "mono_in"
    make_zip(source / "com.example.mono.apk", {
        "AndroidManifest.xml": b"\x03\x00\x08\x00manifest",
        "classes.dex": b"dex\n035\x00" + b"\x00" * 100,
        "assets/bin/Data/data.unity3d": unityfs("2019.4.40f1"),
        "assets/bin/Data/Managed/Assembly-CSharp.dll": DLL,
        "assets/bin/Data/Managed/UnityEngine.dll": DLL,
        "assets/bin/Data/boot.config": b"scripting-runtime-version=latest\n",
    })
    manifest = ingest(source, work / "mono_out")

    check("mono: the package is staged", included(manifest), {"com.example.mono.apk"})
    check("mono: no il2cpp metadata is claimed", manifest["il2cpp"]["metadata"], [])
    check("mono: managed assemblies detected", roles_of(manifest).get(MANAGED), 2)
    check("mono: player data still found", roles_of(manifest).get(UNITY_DATA), 1)
    check("mono: version read despite no metadata",
          manifest["unity"]["version"], "2019.4.40f1")
    check("mono: no ABI chosen", manifest["chosen_abi"], None)
    check_true("mono: the missing libraries are reported, not hidden",
               any("no native libraries" in warning for warning in manifest["warnings"]))
    check_true("mono: assemblies are staged",
               "assets/bin/Data/Managed/Assembly-CSharp.dll" in staged_paths(manifest))
    check("mono: nothing failed", manifest["errors"], [])


def case_nested(work: Path) -> None:
    """Packages a directory below the path the user pointed at."""
    source = work / "nested_in"
    make_zip(source / "build_v3" / "base.apk", base_apk_entries())
    abi_apk(source / "build_v3" / "split_config.arm64_v8a.apk", "arm64-v8a")
    manifest = ingest(source, work / "nested_out")

    check("nested: both packages found one level down",
          included(manifest), {"base.apk", "split_config.arm64_v8a.apk"})
    check("nested: the ABI came from the split", manifest["chosen_abi"], "arm64-v8a")
    check_true("nested: player data was staged",
               "assets/bin/Data/data.unity3d" in staged_paths(manifest))


def case_multi_abi(work: Path) -> None:
    """Base plus three ABI splits: exactly one ABI may reach AssetRipper."""
    source = work / "multi_in"
    make_zip(source / "base.apk", base_apk_entries())
    abi_apk(source / "split_config.arm64_v8a.apk", "arm64-v8a")
    abi_apk(source / "split_config.armeabi_v7a.apk", "armeabi-v7a")
    abi_apk(source / "split_config.x86_64.apk", "x86_64")
    manifest = ingest(source, work / "multi_out")

    check("multi-abi: arm64 preferred", manifest["chosen_abi"], "arm64-v8a")
    check("multi-abi: all three were seen",
          set(manifest["available_abis"]), {"arm64-v8a", "armeabi-v7a", "x86_64"})
    check("multi-abi: only the chosen ABI is staged",
          {path for path in staged_paths(manifest) if path.startswith("lib/")},
          {"lib/arm64-v8a/libil2cpp.so", "lib/arm64-v8a/libunity.so"})
    check_true("multi-abi: the choice is reported",
               any("multiple ABIs" in warning for warning in manifest["warnings"]))
    check("multi-abi: the unused splits are excluded, not silently dropped",
          excluded(manifest),
          {"split_config.armeabi_v7a.apk", "split_config.x86_64.apk"})


def case_version_conflict(work: Path) -> None:
    """Two editor versions in one build: the larger payload is the better witness."""
    source = work / "conflict_in"
    make_zip(source / "base.apk", {
        "AndroidManifest.xml": b"\x03\x00\x08\x00manifest",
        # Unity's own shipped resource: small, and built by whichever editor patch
        # cut the installer rather than by the team that made the game.
        "assets/bin/Data/unity default resources": unityfs("2022.3.1f1"),
        "assets/bin/Data/data.unity3d": unityfs("2022.3.55f1") + b"\x00" * 40000,
    })
    manifest = ingest(source, work / "conflict_out")

    check("conflict: the payload's version wins the tie",
          manifest["unity"]["version"], "2022.3.55f1")
    check("conflict: both were counted", manifest["unity"]["sources"], 2)
    check("conflict: the loser is still reported",
          list(manifest["unity"]["others"]), ["2022.3.1f1"])


def case_unpacked(work: Path) -> None:
    """A build someone already unpacked: no packages, and nothing missing."""
    tree = work / "unpacked_in"
    files = {
        "assets/bin/Data/data.unity3d": unityfs("2021.3.37f1"),
        "assets/bin/Data/boot.config": b"gfx-threading\n",
        "assets/bin/Data/Managed/Metadata/global-metadata.dat": METADATA,
        "assets/bin/Data/resources.resource": FSB5,
        "lib/arm64-v8a/libil2cpp.so": ELF,
        "assets/aa/Android/blob_0": UNITYFS,
    }
    for name, payload in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    manifest = ingest(tree, work / "unpacked_out")

    check("unpacked: recognised as a build", manifest["errors"], [])
    check("unpacked: described as read in place",
          manifest["packaging"]["form"], "unpacked")
    check("unpacked: the tree is its own staged root",
          Path(manifest["staged_root"]), tree.resolve())
    check("unpacked: nothing was copied", manifest["staged_files"], [])
    check("unpacked: version read from the payload",
          manifest["unity"]["version"], "2021.3.37f1")
    check("unpacked: metadata found",
          manifest["il2cpp"]["metadata"],
          ["assets/bin/Data/Managed/Metadata/global-metadata.dat"])
    check("unpacked: the ABI is known", manifest["chosen_abi"], "arm64-v8a")
    check("unpacked: the asset pack bundle is seen",
          roles_of(manifest).get(ASSET_BUNDLE), 1)
    check_true("unpacked: there is something to hand over",
               manifest["staged_count"] > 0)

    # A directory that merely contains files is not a build.
    plain = work / "not_a_build"
    plain.mkdir()
    (plain / "notes.txt").write_text("nothing to see")
    manifest = ingest(plain, work / "plain_out")
    check_true("a directory of unrelated files is still rejected",
               any("no .apk" in warning for warning in manifest["warnings"]))


def main() -> int:
    unit_checks()
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        case_a(work)
        case_b(work)
        case_c(work)
        case_hostile(work)
        case_mono(work)
        case_nested(work)
        case_multi_abi(work)
        case_version_conflict(work)
        case_unpacked(work)
    for line in FAILED:
        print("FAIL", line)
    print(f"ingest: {len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
