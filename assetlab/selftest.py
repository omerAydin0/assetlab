"""Self-test for the parts that must hold on games other than the one used to build this.

Run: python -m assetlab.selftest
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image

from .animations import parse_clip, parse_prefab_rig, quaternion_z_degrees
from .classify import (feature_from_project_dir, feature_from_container_path,
                       match_vocabulary, mechanics_from_holders)
from .core import infer_type
from .models import parse_material, parse_prefab_models
from .profile import FLAT_SHADER_RE, LIT_SHADER_RE, _curve
from .doctor import (diagnose_export, diagnose_levels, diagnose_outcome,
                     diagnose_staging)
from .levels import corpus_candidates, looks_like_tiled, parse_level
from .slice_sprites import (ROTATION_90, anchor, packing_rotation, parse_sprite,
                            unrotate)

PASSED, FAILED = [], []


def check(label: str, got, want) -> None:
    (PASSED if got == want else FAILED).append(f"{label}: got {got!r}, want {want!r}")


SPRITE_YAML = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!213 &21300000
Sprite:
  m_Name: shop_button_buy
  m_Rect:
    serializedVersion: 2
    x: 10
    y: 20
    width: 64
    height: 32
  m_Offset: {x: 0, y: 0}
  m_Border: {x: 12, y: 0, z: 12, w: 0}
  m_PixelsToUnits: 140
  m_Pivot: {x: 0.25, y: 0.75}
  m_RD:
    serializedVersion: 3
    texture: {fileID: 2800000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}
    textureRect:
      serializedVersion: 2
      x: 100
      y: 200
      width: 64
      height: 32
    settingsRaw: 67
  m_AtlasRD:
    serializedVersion: 3
    texture: {fileID: 0}
    textureRect:
      serializedVersion: 2
      x: 999
      y: 999
      width: 999
      height: 999
    settingsRaw: 0
"""


# Two children share the name "Arm" - a name-keyed map keeps only one of them. The
# root carries a 0.7 scale that every part inherits, and Head sits on a higher
# sorting layer at a *lower* order so layer precedence is exercised.
RIG_PREFAB = """%YAML 1.1
--- !u!1 &100
GameObject:
  m_Name: Root
  m_IsActive: 1
--- !u!4 &400
Transform:
  m_GameObject: {fileID: 100}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalScale: {x: 0.7, y: 0.7, z: 1}
  m_Children:
  - {fileID: 401}
  m_Father: {fileID: 0}
--- !u!1 &101
GameObject:
  m_Name: Body
  m_IsActive: 1
--- !u!4 &401
Transform:
  m_GameObject: {fileID: 101}
  m_LocalPosition: {x: 0, y: 1, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Children:
  - {fileID: 402}
  - {fileID: 403}
  - {fileID: 404}
  m_Father: {fileID: 400}
--- !u!1 &102
GameObject:
  m_Name: Arm
  m_IsActive: 1
--- !u!4 &402
Transform:
  m_GameObject: {fileID: 102}
  m_LocalPosition: {x: -1, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Children: []
  m_Father: {fileID: 401}
--- !u!212 &212002
SpriteRenderer:
  m_GameObject: {fileID: 102}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 2}
  m_Color: {r: 1, g: 1, b: 1, a: 1}
  m_FlipX: 0
  m_FlipY: 0
  m_SortingLayer: 0
  m_SortingOrder: 1
--- !u!1 &103
GameObject:
  m_Name: Arm
  m_IsActive: 1
--- !u!4 &403
Transform:
  m_GameObject: {fileID: 103}
  m_LocalPosition: {x: 1, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Children: []
  m_Father: {fileID: 401}
--- !u!212 &212003
SpriteRenderer:
  m_GameObject: {fileID: 103}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, type: 2}
  m_Color: {r: 1, g: 1, b: 1, a: 1}
  m_FlipX: 1
  m_FlipY: 0
  m_SortingLayer: 0
  m_SortingOrder: 99
--- !u!1 &104
GameObject:
  m_Name: Head
  m_IsActive: 0
--- !u!4 &404
Transform:
  m_GameObject: {fileID: 104}
  m_LocalPosition: {x: 0, y: 2, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Children: []
  m_Father: {fileID: 401}
--- !u!212 &212004
SpriteRenderer:
  m_GameObject: {fileID: 104}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: cccccccccccccccccccccccccccccccc, type: 2}
  m_Color: {r: 0, g: 0, b: 0, a: 0.5}
  m_FlipX: 0
  m_FlipY: 0
  m_SortingLayer: 6
  m_SortingOrder: 0
"""


# A material on a lit shader that binds no texture at all. Half the models in the
# 3D build measured here look exactly like this: the surface is a colour.
FLAT_MATERIAL = """%YAML 1.1
--- !u!21 &2100000
Material:
  m_Name: ChairSecondary
  m_Shader: {fileID: 4800000, guid: 98c84dc6b2bdef1449b918ccce5c135e, type: 3}
  m_SavedProperties:
    m_TexEnvs:
      _BaseMap:
        m_Texture: {fileID: 0}
        m_Scale: {x: 1, y: 1}
      _BumpMap:
        m_Texture: {fileID: 0}
        m_Scale: {x: 1, y: 1}
    m_Colors:
      _BaseColor: {r: 0.10087212, g: 0.13451827, b: 0.3164476, a: 1}
      _EmissionColor: {r: 0, g: 0, b: 0, a: 0}
"""

TEXTURED_MATERIAL = """%YAML 1.1
--- !u!21 &2100000
Material:
  m_Name: FlowerBlue
  m_Shader: {fileID: 4800000, guid: 98c84dc6b2bdef1449b918ccce5c135e, type: 3}
  m_SavedProperties:
    m_TexEnvs:
      _BaseMap:
        m_Texture: {fileID: 2800000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}
        m_Scale: {x: 1, y: 1}
      _BumpMap:
        m_Texture: {fileID: 0}
        m_Scale: {x: 1, y: 1}
    m_Colors:
      _BaseColor: {r: 1, g: 1, b: 1, a: 1}
"""

MODEL_PREFAB = """%YAML 1.1
--- !u!1 &100
GameObject:
  m_Name: Root
--- !u!4 &400
Transform:
  m_GameObject: {fileID: 100}
  m_Children:
  - {fileID: 401}
  m_Father: {fileID: 0}
--- !u!1 &101
GameObject:
  m_Name: Chair
--- !u!4 &401
Transform:
  m_GameObject: {fileID: 101}
  m_Children: []
  m_Father: {fileID: 400}
--- !u!33 &3300
MeshFilter:
  m_GameObject: {fileID: 101}
  m_Mesh: {fileID: 4300000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, type: 3}
--- !u!23 &2300
MeshRenderer:
  m_GameObject: {fileID: 101}
  m_Materials:
  - {fileID: 2100000, guid: cccccccccccccccccccccccccccccccc, type: 2}
  - {fileID: 2100000, guid: dddddddddddddddddddddddddddddddd, type: 2}
"""

TILED_JSON = ('{ "compressionlevel":-1,\n "height":12,\n "layers":[\n  {"data":[1,0,1],'
              '"name":"Shelf","type":"tilelayer"}],\n "nextlayerid":3,\n'
              ' "orientation":"orthogonal",\n "tiledversion":"1.9.2",\n "width":6 }')



# A minimal export: enough shape for the gate to reach the checks under test.
GATE_SPRITE = """%YAML 1.1
--- !u!213 &21300000
Sprite:
  m_Name: icon
  m_Rect:
    serializedVersion: 2
    x: 4
    y: 8
    width: 16
    height: 16
  m_Offset: {x: 0, y: 0}
  m_RD:
    texture: {fileID: 2800000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, type: 3}
    settingsRaw: 1
"""


def status_of(report, needle: str) -> str | None:
    """The verdict of the one check that talks about `needle`."""
    for check_result in report.checks:
        if needle in check_result.message:
            return check_result.status
    return None


def make_export(root: Path) -> None:
    (root / "Sprite").mkdir(parents=True, exist_ok=True)
    (root / "Prefab").mkdir(parents=True, exist_ok=True)
    (root / "Prefab" / "Thing.prefab").write_text("--- !u!1 &1\nGameObject:\n")
    (root / "Prefab" / "Thing.prefab.meta").write_text("guid: " + "a" * 32 + "\n")
    (root / "Sprite" / "icon.asset").write_text(GATE_SPRITE)
    (root / "Sprite" / "icon.asset.meta").write_text("guid: " + "c" * 32 + "\n")
    (root / "Texture2D").mkdir(exist_ok=True)
    (root / "Texture2D" / "atlas.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def gate_checks() -> None:
    """Metadata in, scripts out - or a stated reason why not."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Assets"
        make_export(root)
        with_metadata = {"il2cpp": {"metadata": ["assets/bin/Data/.../global-metadata.dat"]}}

        report = diagnose_export(root, None, with_metadata)
        check("metadata staged but no scripts is a failure",
              status_of(report, "no .cs files"), "fail")
        check("and it says what to do about it",
              any("re-export" in (c.remedy or "") for c in report.checks
                  if "no .cs files" in c.message), True)

        # The same absence, with nothing staged that could have produced them, is a
        # limit of the build rather than a fault in the run.
        report = diagnose_export(root, None, {})
        check("no metadata and no scripts is only a warning",
              status_of(report, "no .cs files"), "warn")

        # A caller that knows nothing about staging must not be told off for it.
        report = diagnose_export(root, None, None)
        check("an unknown staging plan does not manufacture a failure",
              status_of(report, "no .cs files"), "warn")

        # One script against thousands of assets is the shape the real miss took:
        # present, so a presence check passes it, and useless.
        (root / "Scripts").mkdir()
        (root / "Scripts" / "Board.cs").write_text("public enum Obstacle { Crate }\n")
        for number in range(600):
            (root / "Prefab" / f"filler_{number}.prefab.meta").write_text("guid: x\n")
        report = diagnose_export(root, None, with_metadata)
        check("a single script against a large export is still a failure",
              status_of(report, "only 1 .cs files"), "fail")

        for number in range(40):
            (root / "Scripts" / f"Type{number}.cs").write_text("class T {}\n")
        report = diagnose_export(root, None, with_metadata)
        check("scripts in proportion clear the gate",
              status_of(report, ".cs files ->"), "ok")

        check("the export still reports its GUID graph",
              status_of(report, ".meta files ->"), "ok")


def absent_tree_checks() -> None:
    """A staging tree that was never materialised must not read as a broken one."""
    import json
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / "dry"
        staging.mkdir()
        (staging / "manifest.json").write_text(json.dumps({
            "dry_run": True, "staged_root": None, "staged_count": 0,
            "packages": [], "packaging": {"shape": "single package"},
            "warnings": [], "errors": []}))

        report = diagnose_staging(staging)
        check("an absent tree is reported once, not as every consequence",
              len([c for c in report.checks if "no staged tree" in c.message]), 1)
        check("what the manifest did find is still reported",
              any("package(s) staged" in c.message for c in report.checks), True)
        check("and it does not block", report.blockers, [])
        check("the reason names the dry run",
              any("dry run" in c.message for c in report.checks), True)
        check("and it says how to fix it",
              any("assetlab.ingest" in (c.remedy or "") for c in report.checks), True)


def corpus_checks() -> None:
    """Three answers about levels, not two."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Assets"
        make_export(root)

        checks = diagnose_levels(root)
        check("an export with no level data says so plainly",
              "NOT DETECTED" in checks[0].message, True)

        # Twenty indexed data files in one directory: a level set in a format the
        # parser does not read.
        levels_dir = root / "Resources" / "maps"
        levels_dir.mkdir(parents=True)
        for number in range(1, 21):
            (levels_dir / f"stage_{number}.dat").write_text("binary-ish")
        checks = diagnose_levels(root)
        check("an unreadable corpus is detected, not called absent",
              "DETECTED BUT UNSUPPORTED" in checks[0].message, True)
        check("and the finding names where it is",
              "Resources/maps" in checks[0].message, True)

        found = corpus_candidates(root)
        check("the candidate is counted", found[0]["count"], 20)
        check("and its naming pattern is reported", found[0]["pattern"], "stage_#.dat")

        # Configuration files sitting together are not a corpus: nothing indexes them.
        settings = root / "Resources" / "config"
        settings.mkdir(parents=True)
        for name in ("audio", "graphics", "input", "network", "locale", "ads",
                     "analytics", "shop", "push", "debug", "iap", "remote"):
            (settings / f"{name}.json").write_text("{}")
        check("unrelated config files are not mistaken for levels",
              [entry for entry in corpus_candidates(root)
               if entry["directory"].endswith("config")], [])



def make_catalogue(directory: Path, assets: int, mechanics: int, features: int) -> None:
    """A catalogue with only the columns the outcome check reads."""
    import sqlite3
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "assetlab.db")
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE tags (asset_id INTEGER, kind TEXT, value TEXT)")
    conn.execute("CREATE TABLE sprites (asset_id INTEGER)")
    conn.execute("CREATE TABLE refs (src INTEGER)")
    conn.execute("CREATE TABLE animations (asset_id INTEGER)")
    conn.executemany("INSERT INTO assets (id) VALUES (?)",
                     [(n,) for n in range(1, assets + 1)])
    conn.executemany("INSERT INTO tags (asset_id, kind, value) VALUES (?,?,?)",
                     [(1, "mechanic", f"m{n}") for n in range(mechanics)]
                     + [(1, "feature", f"f{n}") for n in range(features)])
    conn.commit()
    conn.close()


def outcome_checks() -> None:
    """The vocabulary ratio, and the small build it must not accuse."""
    with tempfile.TemporaryDirectory() as temporary:
        gallery = Path(temporary)
        # Four healthy peers, in the range the real catalogues occupy.
        for number, (assets, mechanics, features) in enumerate(
                [(20000, 69, 256), (11000, 132, 353), (27000, 190, 747),
                 (37000, 147, 674)]):
            make_catalogue(gallery / f"peer{number}", assets, mechanics, features)

        # The shape the real miss took: many features, almost no mechanics.
        make_catalogue(gallery / "thin", 31000, 11, 1630)
        report = diagnose_outcome(gallery / "thin")
        check("a vocabulary inconsistent with itself is flagged",
              [c.status for c in report.checks if "inconsistent" in c.message], ["fail"])
        check("but it never blocks the run", report.blockers, [])

        # A small build is not a broken one: too few features for a ratio to mean
        # anything, so no claim is made about it either way.
        make_catalogue(gallery / "small", 1378, 19, 41)
        report = diagnose_outcome(gallery / "small")
        check("a small build is not accused", report.gaps, [])
        check("and it is not silently called healthy either",
              [c for c in report.checks if "self-consistent" in c.message], [])

        make_catalogue(gallery / "sound", 19000, 69, 256)
        report = diagnose_outcome(gallery / "sound")
        check("a healthy build is confirmed",
              [c.status for c in report.checks if "self-consistent" in c.message], ["ok"])
        check("the peer comparison is reported",
              any("against" in c.message and "peers" in c.message
                  for c in report.checks), True)

        # Too few peers to compare against is a fact, not a failure.
        lonely = Path(temporary) / "alone"
        make_catalogue(lonely / "only", 19000, 69, 256)
        report = diagnose_outcome(lonely / "only")
        check("with no peers the comparison is skipped, not faked",
              any("too few to compare" in c.message for c in report.checks), True)


def main() -> None:
    # 1. Developer folders must be recognised without relying on an underscore prefix.
    check("project_dir underscore",
          feature_from_project_dir("_Studio/Gameplay/Items/CrateItem/x.prefab"),
          ("CrateItem", "Items"))
    check("project_dir plain name",
          feature_from_project_dir("MyGame/UI/Shop/panel.prefab"), ("Shop", None))
    check("project_dir nested",
          feature_from_project_dir("Game/Features/Shop/Prefabs/panel.prefab"),
          ("Shop", "Features"))
    check("project_dir engine folder",
          feature_from_project_dir("Sprite/foo.asset"), (None, None))
    check("project_dir type folder",
          feature_from_project_dir("Texture2D/atlas.png"), (None, None))
    check("container path",
          feature_from_container_path("Assets/_X/LiveOps/SummerEvent/Assets/a.spriteatlas"),
          "SummerEvent")

    # 2. Sprite packing settings.
    for raw in (0, 3, 64, 67):          # every value present in the reference corpus
        check(f"rotation raw={raw}", packing_rotation(raw), 0)
    check("rotation rotate90", packing_rotation((ROTATION_90 << 2) | 1), ROTATION_90)
    check("rotation flip_h", packing_rotation((1 << 2) | 1), 1)

    # 3. Un-rotation is lossless for the flip cases.
    source = Image.new("RGBA", (6, 4))
    source.putpixel((0, 0), (255, 0, 0, 255))
    source.putpixel((5, 3), (0, 255, 0, 255))
    for rotation, transpose in ((1, Image.Transpose.FLIP_LEFT_RIGHT),
                                (2, Image.Transpose.FLIP_TOP_BOTTOM),
                                (3, Image.Transpose.ROTATE_180)):
        packed = source.transpose(transpose)
        check(f"unrotate {rotation}", unrotate(packed, rotation).tobytes(), source.tobytes())
    check("unrotate 90 restores shape",
          unrotate(source.transpose(Image.Transpose.ROTATE_90), ROTATION_90).size, source.size)

    # 4. Sprite parsing reads m_RD, never the empty m_AtlasRD that follows it.
    name, guid, rect, rotation, ppu, pivot, border = parse_sprite(SPRITE_YAML)
    check("parse name", name, "shop_button_buy")
    check("parse guid", guid, "a" * 32)
    check("parse rect (m_RD, not m_AtlasRD)", rect, (100, 200, 64, 32))
    check("parse rotation", rotation, 0)
    # The build authors at 140, not the 100 default; assuming the default draws every
    # sprite 40% too large for the transform offsets it has to line up with.
    check("parse pixels-per-unit", ppu, 140.0)
    check("parse nine-slice border", border, [12.0, 0.0, 12.0, 0.0])

    # 4b. A renderer puts the sprite's pivot on the transform, and CSS measures from
    #     the top while Unity measures from the bottom.
    check("anchor centre stays centre",
          anchor((0, 0, 64, 32), (0, 0, 64, 32), (0, 0), (0.5, 0.5)), (0.5, 0.5))
    check("anchor flips the vertical axis", pivot, (0.25, 0.25))
    # Tight packing trims transparent padding; textureRectOffset says how much, and
    # ignoring it shifts every trimmed sprite by that amount.
    check("anchor accounts for a trimmed rect",
          anchor((0, 0, 100, 100), (0, 0, 80, 80), (10, 10), (0.5, 0.5)), (0.5, 0.5))

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)

        # 5. A YAML asset's own class id beats the folder it happens to sit in.
        odd = root / "Weird" / "Folder" / "BS_147.asset"
        odd.parent.mkdir(parents=True)
        odd.write_text(SPRITE_YAML, encoding="utf-8")
        check("infer_type from class id", infer_type(odd.relative_to(root), odd), "Sprite")

        # 6. Tiled levels are found by content, at any path and either extension.
        for name in ("Levels/Chapter1/level_0007.json", "TextAsset/stage_12.bytes"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TILED_JSON, encoding="utf-8")
            check(f"tiled sniff {path.suffix}", looks_like_tiled(path), True)
        plain = root / "config.json"
        plain.write_text('{"volume":0.8,"language":"tr"}', encoding="utf-8")
        check("tiled sniff rejects plain json", looks_like_tiled(plain), False)

        # 7. Tiled properties come as a map before 1.0 and a list after; both parse.
        old_style = root / "Levels/old/level_1.json"
        old_style.parent.mkdir(parents=True, exist_ok=True)
        old_style.write_text(TILED_JSON.replace(
            '"name":"Shelf","type":"tilelayer"',
            '"name":"Shelf","type":"tilelayer","properties":{"time":90,"snac":"4"}'),
            encoding="utf-8")
        parsed_old = parse_level(old_style)
        check("tiled properties as a map", parsed_old["time_limit"], 90)
        new_style = root / "Levels/new/level_2.json"
        new_style.parent.mkdir(parents=True, exist_ok=True)
        new_style.write_text(TILED_JSON.replace(
            '"name":"Shelf","type":"tilelayer"',
            '"name":"Shelf","type":"tilelayer","properties":'
            '[{"name":"time","value":90},{"name":"snac","value":"4"}]'),
            encoding="utf-8")
        check("tiled properties as a list", parse_level(new_style)["time_limit"], 90)

    # 8. Animation curves: Unity writes a curve either with a serializedVersion line
    #    or straight as `- curve:`. Missing the second form silently concatenates
    #    every track into one bogus sequence.
    for header in ("- serializedVersion: 2\n    curve:", "- curve:"):
        clip = ("%YAML 1.1\nAnimationClip:\n  m_Name: c\n  m_PPtrCurves:\n  " + header +
                "\n    - time: 0\n      value: {fileID: 21300000, guid: " + "a" * 32 +
                ", type: 2}\n    attribute: m_Sprite\n    path: A\n  " + header +
                "\n    - time: 0\n      value: {fileID: 21300000, guid: " + "b" * 32 +
                ", type: 2}\n    - time: 0.5\n      value: {fileID: 21300000, guid: " +
                "c" * 32 + ", type: 2}\n    attribute: m_Sprite\n    path: B\n"
                "  m_SampleRate: 60\n")
        parsed = parse_clip(clip)
        label = "serializedVersion" if "serialized" in header else "bare curve"
        check(f"animation tracks kept separate ({label})",
              [len(t["frames"]) for t in parsed["tracks"]], [1, 2])
        check(f"animation track paths ({label})",
              [t["path"] for t in parsed["tracks"]], ["A", "B"])

    # 9. Rig extraction. Every one of these was a real defect: a name-keyed lookup
    #    lost same-named siblings and dropped the root's own transform, a leaf-name
    #    fallback bound one clip path to several objects so the same sprite was drawn
    #    twice, and sorting on order alone left ties to dictionary order.
    records = parse_prefab_rig(RIG_PREFAB)
    check("rig keeps same-named siblings apart",
          [r["path"] for r in records],
          ["Body/Arm", "Body/Arm", "Body/Head"])
    check("rig draws each renderer once", len(records), len({id(r) for r in records}))
    check("rig chain starts at the prefab root and keeps its transform",
          [n["path"] for n in records[0]["chain"]], ["", "Body", "Body/Arm"])
    check("rig root transform survives", records[0]["chain"][0]["base"], [0.0, 0.0, 0.7, 0.7])
    check("rig reads flipX", [r["flip"] for r in records], [0, 1, 0])
    check("rig reads the full colour, not only alpha",
          records[2]["rgb"] + [records[2]["tint"]], [0.0, 0.0, 0.0, 0.5])
    check("rig honours an inactive GameObject", [r["on"] for r in records],
          [True, True, False])
    # Sorting layer outranks order in layer, which is why a layer-6 part at order 0
    # must still draw in front of a layer-0 part at order 99.
    check("rig sorts by layer before order", [r["path"] for r in records][-1], "Body/Head")
    check("quaternion to degrees", round(quaternion_z_degrees(0, 0, 0.7071068, 0.7071068)), 90)

    # 10. A SpriteMask clips the renderers beneath it that ask to be visible inside
    #     a mask. Without it a slot machine's reel - far larger than the window it
    #     shows through - covers the whole preview.
    masked = parse_prefab_rig(RIG_PREFAB.replace(
        """--- !u!212 &212003""",
        """--- !u!331 &331001
SpriteMask:
  m_GameObject: {fileID: 101}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: dddddddddddddddddddddddddddddddd, type: 2}
--- !u!212 &212003""").replace(
        """  m_FlipX: 1
  m_FlipY: 0""",
        """  m_FlipX: 1
  m_FlipY: 0
  m_MaskInteraction: 1"""))
    inside = [r for r in masked if r["mask"]]
    check("only the renderer asking to be masked is clipped", len(inside), 1)
    check("the clip comes from the nearest SpriteMask ancestor",
          inside[0]["mask"]["guid"], "d" * 32)
    check("an unmasked sibling is left alone",
          [r["mask"] for r in masked if r["path"] == "Body/Head"], [None])

    # 11. Design words are compounds, so a single token never spells one: matching
    #     token by token found `Box` and never `DynamiteBox`, and the 113 board
    #     pieces of the build this was measured on stayed invisible.
    vocabulary = {"dynamitebox": "DynamiteBox", "box": "Box", "birdnest": "BirdNest"}
    check("longest run wins over its own suffix",
          match_vocabulary("dynamite_box_icon_2", vocabulary), "dynamitebox")
    check("a plain word still matches", match_vocabulary("box_shadow", vocabulary), "box")
    check("camel case and the Item suffix reach the same key",
          match_vocabulary("BirdNestItemPrefab", vocabulary), "birdnest")
    check("no match is no guess", match_vocabulary("ui_button_ok", vocabulary), None)

    # 12. An obstacle's art is usually packed onto an atlas named after something
    #     else, so the prefab that uses it is the only thing that knows.
    links = [{"asset_id": 1, "holder_name": "DynamiteBoxItemPrefab"},
             {"asset_id": 1, "holder_name": "DynamiteBoxExplodeParticles"},
             {"asset_id": 2, "holder_name": "DynamiteBoxItemPrefab"},
             {"asset_id": 2, "holder_name": "BirdNestItemPrefab"},
             {"asset_id": 3, "holder_name": "SomeUnrelatedThing"}]
    graphed = mechanics_from_holders(links, vocabulary)
    check("agreeing holders name the asset", graphed.get(1), "dynamitebox")
    check("a sprite shared by two obstacles is left alone", 2 in graphed, False)
    check("a holder with no design word votes for nothing", 3 in graphed, False)

    # 13. Telling a 2D build from a 3D one. Each of these encodes a wrong turn:
    #     `lit` hides inside `Unlit`, `Blit` and `Split`, and `Standard` inside
    #     `2DxFX_Standard_GrayScale`, which is a sprite effect.
    def kind(name: str) -> str:
        if FLAT_SHADER_RE.search(name):
            return "flat"
        return "lit" if LIT_SHADER_RE.search(name) else "neither"

    for shader in ("Universal Render Pipeline_Lit", "Custom_URP_StorybookSoftLit",
                   "Custom_Standard_Clipped", "Toony Colors Pro 2_Hybrid Shader 2",
                   "Shader Graphs_WaterSurface"):
        check(f"lit shader: {shader}", kind(shader), "lit")

    for shader in ("Universal Render Pipeline_Unlit", "Hidden_Universal_CoreBlit",
                   "2DxFX_Standard_GrayScale", "Sprites_Default",
                   "TextMeshPro_Distance Field", "Spine_Skeleton",
                   "Universal Render Pipeline_Particles_Unlit"):
        check(f"flat shader: {shader}", kind(shader), "flat")

    check("curve clamps below the range", _curve(0.01, 0.05, 0.40), 0.0)
    check("curve clamps above the range", _curve(0.90, 0.05, 0.40), 1.0)

    # 14. The mesh/material/texture chain a 3D build is read through.
    flat = parse_material(FLAT_MATERIAL)
    check("flat material binds no texture", flat["textures"], [])
    # An unbound slot is still serialised; counting slots rather than bindings is
    # what made a 2D build look 100% lit.
    check("flat material keeps its colour", flat["colour"]["hex"], "#1a2251")

    textured = parse_material(TEXTURED_MATERIAL)
    check("bound texture is found",
          [(t["slot"], t["guid"]) for t in textured["textures"]],
          [("BaseMap", "a" * 32)])

    models = parse_prefab_models(MODEL_PREFAB)
    check("one model found", len(models), 1)
    check("mesh resolved", models[0]["mesh_guid"], "b" * 32)
    check("both materials kept", models[0]["material_guids"],
          ["c" * 32, "d" * 32])
    check("object path excludes the root", models[0]["path"], "Chair")
    check("static geometry is not marked rigged", models[0]["skinned"], False)

    skinned = parse_prefab_models(MODEL_PREFAB.replace(
        """--- !u!33 &3300
MeshFilter:""", """--- !u!137 &13700
SkinnedMeshRenderer:""").replace("--- !u!23 &2300\nMeshRenderer:",
                                 "--- !u!23 &2300\nMeshRenderer:"))
    check("skinned renderer is marked rigged", skinned[0]["skinned"], True)

    gate_checks()
    absent_tree_checks()
    corpus_checks()
    outcome_checks()

    for line in FAILED:
        print("FAIL", line)
    print(f"analysis: {len(PASSED)} passed, {len(FAILED)} failed")

    from .ingest.selftest import main as ingest_main
    ingest_failed = ingest_main()
    raise SystemExit(1 if (FAILED or ingest_failed) else 0)


if __name__ == "__main__":
    main()
