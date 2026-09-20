"""Self-test for the parts that must hold on games other than the one used to build this.

Run: python -m assetlab.selftest
"""

from __future__ import annotations

import http.server
import tempfile
import threading
import urllib.parse
from pathlib import Path

from PIL import Image

from .addressables import read_catalog
from .animations import (parse_clip, parse_prefab_rig, parse_transform_curves,
                         prefab_animators, quaternion_z_degrees)
from .classify import (bundle_reach, feature_from_project_dir,
                       feature_from_container_path, match_vocabulary,
                       mechanics_from_holders)
from .core import connect, infer_type, make_thumbnail
from .index import build_index
from .mesh import parse_mesh
from .objects import group_objects, name_tokens
from .spine import build as spine_build
from .spine import cut, fit_to_page, footprint, on_page, parse_atlas
from .models import parse_material, parse_prefab_models
from .profile import FLAT_SHADER_RE, LIT_SHADER_RE, _curve
from .doctor import (diagnose_export, diagnose_levels, diagnose_outcome,
                     diagnose_staging)
from .levels import corpus_candidates, looks_like_tiled, parse_level
from .ripper import Ripper
from .classify import load_bundle_map
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


def mesh_layout(vertices: int, stride_channels: str, blob: bytes,
                data_size: int) -> str:
    """A Mesh asset with just the fields the vertex reader looks at."""
    return f"""%YAML 1.1
--- !u!43 &4300000
Mesh:
  m_Name: probe
  m_SubMeshes:
  - firstByte: 0
    indexCount: 3
    topology: 0
    firstVertex: 0
    vertexCount: {vertices}
  m_MeshCompression: 0
  m_IndexFormat: 0
  m_IndexBuffer: 000001000200
  m_VertexData:
    m_VertexCount: {vertices}
    m_Channels:
{stride_channels}
    m_DataSize: {data_size}
    _typelessdata: {blob.hex()}
"""


def channel(stream: int, offset: int, fmt: int, dimension: int) -> str:
    return (f"    - stream: {stream}\n      offset: {offset}\n"
            f"      format: {fmt}\n      dimension: {dimension}\n")


def mesh_checks() -> None:
    """The vertex buffer's own arithmetic, where a mistake reads as noise."""
    import struct

    # One stream: position float32 x3 (12) + two float16 x4 (8+8) + unorm8 x4 (4)
    # + float16 x2 (4) = 36 bytes a row. Five rows is 180, which is deliberately
    # not a multiple of 16 - the case the reader used to reject.
    channels = (channel(0, 0, 0, 3) + channel(0, 12, 1, 4) + channel(0, 20, 1, 4)
                + channel(0, 28, 2, 4) + channel(0, 32, 1, 2))
    rows = b""
    for index in range(5):
        rows += struct.pack("<fff", float(index), float(index) * 2, 0.0)
        rows += b"\x00" * 24
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "probe.asset"

        path.write_text(mesh_layout(5, channels, rows, len(rows)), encoding="utf-8")
        parsed = parse_mesh(path)
        check("a single stream is not padded past its own rows",
              parsed is not None and len(parsed.vertices), 5)
        if parsed is not None:
            check("and the positions survive the walk",
                  [round(float(v[0]), 3) for v in parsed.vertices], [0.0, 1.0, 2.0, 3.0, 4.0])

        # The high nibble of `dimension` carries flags, not components: 0x34 is
        # four components, and reading 52 of them would blow the stride apart.
        flagged = (channel(0, 0, 0, 3) + channel(0, 12, 1, 0x34) + channel(0, 20, 1, 4)
                   + channel(0, 28, 2, 4) + channel(0, 32, 1, 2))
        path.write_text(mesh_layout(5, flagged, rows, len(rows)), encoding="utf-8")
        check("dimension flags are masked off",
              parse_mesh(path) is not None, True)

        # A declared size the strides cannot reproduce means the layout is not
        # understood, and reading it anyway would return convincing nonsense.
        path.write_text(mesh_layout(5, channels, rows, len(rows) + 64), encoding="utf-8")
        check("a size that does not add up is still refused",
              parse_mesh(path), None)


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


def desktop_staging_checks() -> None:
    """A desktop build keeps its player data in <Game>_Data; the gate must know it."""
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / "PEAK"
        (staging / "PEAK_Data" / "Managed").mkdir(parents=True)
        # A SerializedFile states its version in bytes 8-12; the gate reads the head
        # rather than trusting the name, so the fixture has to carry a real one.
        # Over the 64-byte floor the gate uses to ignore stubs.
        serialized = bytes(8) + (22).to_bytes(4, "big") + bytes(256)
        (staging / "PEAK_Data" / "level0").write_bytes(serialized)
        (staging / "PEAK_Data" / "sharedassets0.assets").write_bytes(serialized)
        (staging / "PEAK_Data" / "Managed" / "Assembly-CSharp.dll").write_bytes(b"MZ")
        (staging / "GameAssembly.dll").write_bytes(b"MZ")
        report = diagnose_staging(staging)
    check("a desktop build's player data is found, not refused",
          [c.message for c in report.blockers], [])
    check("and the directory it found is named",
          any("PEAK_Data" in c.message for c in report.checks), True)


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


def scriptable_corpus_checks() -> None:
    """Design data serialised as ScriptableObjects, and the art it must not catch.

    A `.asset` file is a sprite or a material far more often than it is data, so
    the two are told apart by the class the YAML declares rather than by the folder
    the exporter happened to put them in.
    """
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Assets"
        waves = root / "MonoBehaviour"
        waves.mkdir(parents=True)
        for number in range(7):
            (waves / f"Aftermath_Wave{number:02d}.asset").write_text(
                "--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: wave\n")

        checks = diagnose_levels(root)
        check("a small indexed set is named rather than called nothing",
              "no single corpus" in checks[0].message, True)
        check("and it is not promoted to a corpus verdict",
              "DETECTED BUT UNSUPPORTED" in checks[0].message, False)
        check("the group itself is found",
              [e["count"] for e in corpus_candidates(root)], [7])

        # Twelve or more of them is a corpus, and then the verdict changes.
        for number in range(7, 14):
            (waves / f"Aftermath_Wave{number:02d}.asset").write_text(
                "--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: wave\n")
        check("past the threshold it becomes an unreadable corpus",
              "DETECTED BUT UNSUPPORTED" in diagnose_levels(root)[0].message, True)

        # The same shape in art must never read as data.
        art = root / "Sprite"
        art.mkdir()
        for number in range(9):
            (art / f"icon_{number}.asset").write_text(
                "--- !u!213 &21300000\nSprite:\n  m_Name: icon\n")
        check("sprites serialised as .asset are not mistaken for data",
              [e for e in corpus_candidates(root)
               if e["directory"].endswith("Sprite")], [])


def object_checks() -> None:
    """Grouping sprites into objects, on the shapes real builds actually ship."""
    def sprite(name, w=32, h=32, page=None, mechanic=None):
        return {"n": name, "img": f"sprites/{name}.png", "kind": "image", "at": None,
                "w": w, "h": h, "ax": page, "mechanic": mechanic, "feature": None}

    # A whole and the parts whose names extend it, with a chain to flatten: a nose
    # ball belongs to the dog, not to the nose.
    dog = [sprite("blueTB_dog", 78, 80), sprite("blueTB_dogEar_1"),
           sprite("blueTB_dogEar_2"), sprite("blueTB_dogNose"),
           sprite("blueTB_dogNoseBall"), sprite("blueTB_dogTail"),
           sprite("blueTB_Balloon_base", 72, 80)]
    found = group_objects(dog, [])
    whole = next(o for o in found if o["n"] == "blueTB_dog")
    check("the whole owns every part below it", len(whole["p"]), 5)
    check("a chain is flattened to its root",
          sorted(dog[i]["n"] for i in whole["p"]),
          ["blueTB_dogEar_1", "blueTB_dogEar_2", "blueTB_dogNose",
           "blueTB_dogNoseBall", "blueTB_dogTail"])
    check("an unrelated stem stays its own object",
          [len(o["p"]) for o in found if o["n"] == "blueTB_Balloon_base"], [0])

    # A numbered run of one thing is a set of variants, not a decomposition.
    coins = [sprite("coin")] + [sprite(f"coin_{n}") for n in range(1, 10)]
    check("a numbered run is not a decomposition",
          max(len(o["p"]) for o in group_objects(coins, [])), 0)

    # Several names, but each of them many times over: one cube in five colours
    # across many states, not 40 pieces of one puzzle.
    matrix = [sprite("puzzle", 147, 141)]
    for colour in ("blue", "green", "red", "orange"):
        for number in range(1, 11):
            matrix.append(sprite(f"puzzle_cube_{colour}_{number:02d}"))
    check("a variant matrix is not a decomposition",
          max(len(o["p"]) for o in group_objects(matrix, [])), 0)

    # No sprite of the assembled thing exists, so the clip that draws the pieces is
    # what says they are one object.
    dragon = [sprite("Dragon_body"), sprite("Dragon_head"), sprite("Dragon_wing"),
              sprite("unrelated_button")]
    dragon.append({"n": "idle_1_0", "img": None, "kind": "animation", "at": None,
                   "w": None, "h": None, "ax": None, "mechanic": None, "feature": None})
    rigged = group_objects(dragon, [(4, ["sprites/Dragon_body.png",
                                         "sprites/Dragon_head.png",
                                         "sprites/Dragon_wing.png"])])
    headless = next(o for o in rigged if o["c"] == 4)
    check("a clip defines an object the build ships no whole for",
          (headless["w"], len(headless["p"])), (None, 3))
    check("the headless label is the name its members share",
          headless["n"], "Dragon")

    # Where the members share no name worth printing, the clip names the thing.
    numbered = [sprite("02"), sprite("07"), sprite("012")]
    numbered.append({"n": "ChestOpenAnimation", "img": None, "kind": "animation",
                     "at": None, "w": None, "h": None, "ax": None, "mechanic": None,
                     "feature": None})
    check("a set that shares no name takes the clip's",
          group_objects(numbered, [(3, [d["img"] for d in numbered[:3]])])[0]["n"],
          "ChestOpenAnimation")
    check("a sprite the clip does not draw stays outside it",
          all(dragon[i]["n"] != "unrelated_button" for i in headless["p"]), True)

    # A clip that draws a whole room is depicting a place, not a thing.
    room = [sprite(f"0{n}_B_prop_{n}") for n in range(9000, 9120)]
    room.append({"n": "AreaSketchAnimation", "img": None, "kind": "animation",
                 "at": None, "w": None, "h": None, "ax": None, "mechanic": None,
                 "feature": None})
    check("a clip drawing a place defines no object",
          any(o["c"] is not None
              for o in group_objects(room, [(120, [d["img"] for d in room[:120]])])),
          False)

    # One outlier must not drag the shared label back to the folder name.
    leaves = [sprite("Koala-LeafAssets-Stage_2B-Stage2B_1"),
              sprite("Koala-LeafAssets-Stage_2B-Stage2B_2"),
              sprite("Koala-LeafAssets-Stage_2B-Stage2B_3"),
              sprite("Koala-bush2_v02")]
    leaves.append({"n": "Bush_2B", "img": None, "kind": "animation", "at": None,
                   "w": None, "h": None, "ax": None, "mechanic": None, "feature": None})
    named = group_objects(leaves, [(4, [d["img"] for d in leaves[:4]])])[0]
    check("the label comes from most of the set, not all of it",
          named["n"], "Koala-LeafAssets-Stage_2B-Stage2B")

    # The sheet an object came from is the one holding most of its sprites.
    pages = [sprite("gun", 40, 40, page=3), sprite("gunGrip", 8, 8, page=3),
             sprite("gunSight", 6, 6, page=4),
             {"n": "GamePlay", "img": "x", "kind": "image", "at": 1, "w": 512,
              "h": 512, "ax": None, "mechanic": None, "feature": None},
             {"n": "Other", "img": "y", "kind": "image", "at": 1, "w": 512, "h": 512,
              "ax": None, "mechanic": None, "feature": None}]
    check("the atlas is where most of the object sits",
          next(o for o in group_objects(pages, []) if o["n"] == "gun")["a"], 3)
    check("an atlas sheet is never a member of an object",
          any(o["n"] == "GamePlay" for o in group_objects(pages, [])), False)

    # A one-letter sprite is not a name that others extend.
    letters = [sprite("D", 75, 70), sprite("d1_E_c", 40, 40),
               sprite("d2_D_k", 40, 40), sprite("d1_B_b", 40, 40)]
    check("a stem too short to be a name owns nothing",
          max(len(o["p"]) for o in group_objects(letters, [])), 0)

    # A whole is assembled from its parts, so it cannot be smaller than one.
    prefix_hub = [sprite("Gem", 57, 56), sprite("gem_item_AO", 256, 256),
                  sprite("gem_shine", 128, 128), sprite("gem_glow_ring", 200, 90)]
    check("a whole that does not contain its parts is not their whole",
          max(len(o["p"]) for o in group_objects(prefix_hub, [])), 0)

    # Every part the same size as the whole: copies of it, not pieces of it.
    swatches = [sprite("1x1_white", 1, 1)] + [
        sprite(f"1x1_white_{tail}", 1, 1)
        for tail in ("0", "150", "bundled", "no_atlas", "resources")]
    check("parts that are all the size of the whole are copies of it",
          max(len(o["p"]) for o in group_objects(swatches, [])), 0)

    # A set the build declares outranks the names, which here say nothing.
    skeleton = [sprite("01_Alt", 40, 20), sprite("01_Ust", 40, 30),
                sprite("02_Alt", 40, 20), sprite("unrelated_button", 30, 30)]
    for index in range(3):
        skeleton[index]["set"] = "TextAsset/Disco_280px.atlas.txt"
    grouped = group_objects(skeleton, [])
    declared = next(o for o in grouped if len(o["p"]) > 1)
    check("an atlas descriptor's regions are one object",
          (declared["n"], len(declared["p"]), declared["w"]), ("Disco_280px", 3, None))
    check("a sprite outside the descriptor stays outside the object",
          all(skeleton[i]["n"] != "unrelated_button" for i in declared["p"]), True)

    check("token split follows the seams an artist types",
          name_tokens("Blocks-Balloon-blueTB_dogEar_1"),
          ("blocks", "balloon", "blue", "tb", "dog", "ear", "1"))


def thumbnail_checks() -> None:
    """A thumbnail has to fill its frame, or a grid of real art reads as empty."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "t.jpg"
        make_thumbnail(Image.new("RGBA", (24, 24), (255, 0, 0, 255)), target, 224)
        with Image.open(target) as made:
            check("a small sprite is enlarged, not left as a speck", made.size,
                  (224, 224))
        # Measured away from the centre: a 24x24 sprite left at 1:1 still covers the
        # middle pixel, so only a point outside that square tells the two apart.
        # 24 px enlarged eight times covers 192 of the 224, reaching well past it.
        with Image.open(target) as made:
            off_centre = made.convert("RGB").getpixel((40, 112))
        check("the art reaches past where a 1:1 paste would end",
              off_centre[0] > 200, True)

        wide = Path(tmp) / "w.jpg"
        make_thumbnail(Image.new("RGBA", (1024, 8), (0, 255, 0, 255)), wide, 224)
        with Image.open(wide) as made:
            check("a strip is not stretched into a square",
                  made.convert("RGB").getpixel((112, 112))[1] > 200, True)
        with Image.open(wide) as made:
            check("and keeps its proportions",
                  made.convert("RGB").getpixel((112, 40))[1] > 200, False)


def spine_checks() -> None:
    """Reading the two atlas dialects a build's skeletal art is described in."""
    classic = """
adam.png
size: 256,64
format: RGBA8888
filter: Linear,Linear
repeat: none
agiz01
  rotate: false
  xy: 147, 2
  size: 25, 16
  orig: 25, 16
  offset: 0, 0
  index: -1
burun
  rotate: true
  xy: 164, 20
  size: 10, 11
  orig: 10, 11
  offset: 0, 0
  index: -1
"""
    regions = parse_atlas(classic)
    check("both regions read from the old dialect", len(regions), 2)
    check("its rect is xy plus size",
          [regions[0][key] for key in ("x", "y", "w", "h")], [147, 2, 25, 16])
    check("a rotate flag is a quarter turn", regions[1]["turn"], 90)
    check("the page carries its own size",
          (regions[0]["page"], regions[0]["page_w"], regions[0]["page_h"]),
          ("adam.png", 256, 64))

    modern = """
Super_Thunder.png
size:1906,586
filter:Linear,Linear
pma:true
01_Alt
bounds:702,297,142,63
offsets:2,2,146,67
rotate:90
plain
bounds:10,20,30,40
"""
    regions = parse_atlas(modern)
    check("both regions read from the new dialect", len(regions), 2)
    check("its rect is one bounds line",
          [regions[0][key] for key in ("x", "y", "w", "h", "turn")],
          [702, 297, 142, 63, 90])
    check("a region with no rotate line is not turned", regions[1]["turn"], 0)

    # A page line names an image and is followed by a size; a region named like one
    # would otherwise swallow the regions after it.
    check("only a line naming an image opens a page",
          [r["name"] for r in parse_atlas(
              "sheet.png\nsize:64,64\nicon.png\nbounds:1,2,3,4\n")],
          ["icon.png"])

    # A page exported at half the size the descriptor was written for.
    scaled = fit_to_page(parse_atlas(classic), (128, 32))
    # Half of 25 is 12.5, and a rect that rounds up would reach past the page it was
    # just fitted to, so it rounds the way Python rounds a tie: down to even.
    check("rects follow the page as it was actually exported",
          [scaled[0][key] for key in ("x", "y", "w", "h")], [74, 1, 12, 8])
    check("and a page at its declared size is left alone",
          fit_to_page(parse_atlas(classic), (256, 64))[0]["x"], 147)

    # A turned region occupies the transposed footprint, so a bounds check has to ask
    # about the footprint and not about the region.
    turned = {"x": 250, "y": 10, "w": 20, "h": 60, "turn": 90}
    check("a turned region's footprint is transposed", footprint(turned), (60, 20))
    check("it fits a page wide enough for the footprint",
          on_page(turned, (320, 100)), True)
    check("and not one only wide enough for the region",
          on_page(turned, (280, 100)), False)

    # A turned region occupies the transposed footprint on the page, and comes back
    # the way it is drawn.
    sheet = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    sheet.paste(Image.new("RGBA", (20, 8), (255, 0, 0, 255)), (5, 7))
    piece = cut(sheet, {"x": 5, "y": 7, "w": 8, "h": 20, "turn": 90})
    check("a turned region is cut transposed and set back", piece.size, (8, 20))
    upright = cut(sheet, {"x": 5, "y": 7, "w": 20, "h": 8, "turn": 0})
    check("an unturned region is cut as it lies", upright.size, (20, 8))


def index_rerun_checks() -> None:
    """Indexing the same export twice keeps every asset's id, so its labels hold."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Assets"
        (root / "TextAsset").mkdir(parents=True)
        # Two assets, so a replaced row could not happen to get its old id back.
        for name, guid in (("notes", "a" * 32), ("other", "b" * 32)):
            (root / "TextAsset" / f"{name}.txt").write_text(name, encoding="utf-8")
            (root / "TextAsset" / f"{name}.txt.meta").write_text(f"guid: {guid}\n")
        conn = connect(Path(tmp) / "out" / "assetlab.db")
        build_index(root, conn)
        ids = dict(conn.execute("SELECT rel_path, id FROM assets"))
        conn.execute("INSERT INTO tags VALUES (?, 'role', 'doc', 'high', 'filename')",
                     (ids["TextAsset/notes.txt"],))
        (root / "TextAsset" / "notes.txt").write_text("notes, edited", encoding="utf-8")
        build_index(root, conn)
        after = dict(conn.execute("SELECT rel_path, id FROM assets"))
        labelled = [row[0] for row in conn.execute(
            "SELECT a.rel_path FROM tags t JOIN assets a ON a.id = t.asset_id")]
        size = conn.execute("SELECT size_bytes FROM assets WHERE rel_path = "
                            "'TextAsset/notes.txt'").fetchone()[0]
        # A file renamed between exports keeps its guid and arrives under a new path.
        (root / "TextAsset" / "other.txt").rename(root / "TextAsset" / "other_0.txt")
        (root / "TextAsset" / "other.txt.meta").rename(
            root / "TextAsset" / "other_0.txt.meta")
        try:
            build_index(root, conn)
            moved = dict(conn.execute("SELECT rel_path, id FROM assets"))
        except sqlite3.IntegrityError as error:
            moved = str(error)
        # Two files still in the export trade guids; each path keeps its own row.
        (root / "TextAsset" / "notes.txt.meta").write_text(f"guid: {'b' * 32}\n")
        (root / "TextAsset" / "other_0.txt.meta").write_text(f"guid: {'a' * 32}\n")
        try:
            build_index(root, conn)
            swapped = [tuple(row) for row in conn.execute(
                "SELECT rel_path, id, guid FROM assets ORDER BY rel_path")]
        except sqlite3.IntegrityError as error:
            swapped = str(error)
        conn.close()          # Windows will not remove a directory it still holds
    check("a second index keeps every asset's id", after, ids)
    check("so a label written against it still finds it", labelled,
          ["TextAsset/notes.txt"])
    check("and the row itself is brought up to date", size, len("notes, edited"))
    check("a file that moved keeps its id under its new path", moved,
          {"TextAsset/notes.txt": ids["TextAsset/notes.txt"],
           "TextAsset/other_0.txt": ids["TextAsset/other.txt"]})
    check("two files that trade guids each keep their own id", swapped,
          [("TextAsset/notes.txt", ids["TextAsset/notes.txt"], "b" * 32),
           ("TextAsset/other_0.txt", ids["TextAsset/other.txt"], "a" * 32)])


def spine_catalogue_checks() -> None:
    """Cutting a descriptor into a catalogue, and cutting it again."""
    with tempfile.TemporaryDirectory() as tmp:
        root, out = Path(tmp) / "Assets", Path(tmp) / "out"
        (root / "TextAsset").mkdir(parents=True)
        (root / "Texture2D").mkdir(parents=True)
        Image.new("RGBA", (64, 32), (255, 0, 0, 255)).save(
            root / "Texture2D" / "sheet.png")
        (root / "TextAsset" / "hero.atlas.txt").write_text(
            "sheet.png\nsize:64,32\nhead\nbounds:0,0,10,8\nbody\nbounds:20,4,12,16\n", encoding="utf-8")
        conn = connect(out / "assetlab.db")
        # The page is in the catalogue, as it is once `index` has run; without that
        # the regions are still cut but have no atlas to point at.
        conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type) "
                     "VALUES ('a' * 32, 'Texture2D/sheet.png', 'sheet', 'Texture2D')")
        stats = spine_build(root, out, conn)
        check("both regions cut, both tied to their page",
              (stats["cut"], stats["out_of_bounds"], stats["no_atlas"]), (2, 0, 0))
        rows = dict(conn.execute("SELECT name, id FROM assets WHERE ext='atlas'"))
        check("each region is a sprite in the catalogue", sorted(rows), ["body", "head"])

        # A descriptor measures down from the top and Unity measures up from the
        # bottom, so an 8-tall region at the top of a 32-tall page lands at y=24.
        placed = dict(conn.execute(
            "SELECT a.name, s.y FROM sprites s JOIN assets a ON a.id = s.asset_id"))
        check("the origin is turned over for Unity", placed["head"], 24)

        again = spine_build(root, out, conn)
        after = dict(conn.execute("SELECT name, id FROM assets WHERE ext='atlas'"))
        check("a second run reuses what it already cut", again["reused"], 2)
        check("and leaves the ids alone, so labels stay attached", after, rows)
        conn.close()          # Windows will not remove a directory it still holds


ANIMATOR_PREFAB = """%YAML 1.1
--- !u!1 &100
GameObject:
  m_Name: Page
--- !u!4 &400
Transform:
  m_GameObject: {fileID: 100}
  m_LocalPosition: {x: -28, y: -12, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Children:
  - {fileID: 401}
  - {fileID: 402}
  m_Father: {fileID: 0}
--- !u!111 &111000
Animation:
  m_GameObject: {fileID: 100}
  m_Animation: {fileID: 7400000, guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee, type: 2}
--- !u!1 &101
GameObject:
  m_Name: Elephant
--- !u!4 &401
Transform:
  m_GameObject: {fileID: 101}
  m_LocalPosition: {x: 2, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 400}
--- !u!95 &95001
Animator:
  m_GameObject: {fileID: 101}
  m_Controller: {fileID: 9100000, guid: cccccccccccccccccccccccccccccccc, type: 2}
--- !u!212 &212001
SpriteRenderer:
  m_GameObject: {fileID: 101}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}
--- !u!1 &102
GameObject:
  m_Name: Patch
--- !u!4 &402
Transform:
  m_GameObject: {fileID: 102}
  m_LocalPosition: {x: 0, y: -5, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 400}
--- !u!212 &212002
SpriteRenderer:
  m_GameObject: {fileID: 102}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, type: 3}
"""

# A clip that sways the object its Animator is on: its curves have the empty path.
ANIMATOR_CLIP = """%YAML 1.1
--- !u!74 &7400000
AnimationClip:
  m_Name: Sway
  m_EulerCurves:
  - curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: {x: 0, y: 0, z: 0}
      - serializedVersion: 3
        time: 1
        value: {x: 0, y: 0, z: 10}
    path:
  m_PositionCurves: []
  m_SampleRate: 60
"""


def animator_binding_checks() -> None:
    """A clip moves the object its Animator is on, and draws only what is under it."""
    import json
    from . import animations
    check("each animating component is found where it sits",
          sorted(prefab_animators(ANIMATOR_PREFAB)),
          [("", {"e" * 32}), ("Elephant", {"c" * 32})])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Page.prefab").write_text(ANIMATOR_PREFAB, encoding="utf-8")
        (root / "Sway.anim").write_text(ANIMATOR_CLIP, encoding="utf-8")
        conn = connect(root / "t.db")
        try:
            for guid, path, kind, ext, size in (
                    ("a" * 32, "elephant.asset", "Sprite", "asset", (62, 88)),
                    ("b" * 32, "patch.asset", "Sprite", "asset", (1024, 8)),
                    ("f" * 32, "Sway.anim", "AnimationClip", "anim", (None, None)),
                    ("c" * 32, "Sway.controller", "AnimatorController", "controller",
                     (None, None)),
                    ("9" * 32, "Page.prefab", "Prefab", "prefab", (None, None))):
                image = f"sprites/{path}.png" if kind == "Sprite" else None
                conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type, ext, "
                             "width, height, image_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (guid, path, path.split(".")[0], kind, ext, *size, image))
            ids = dict(conn.execute("SELECT guid, id FROM assets").fetchall())
            for guid in ("a" * 32, "b" * 32):
                conn.execute("INSERT INTO sprites (asset_id, ppu, anchor_x, anchor_y) "
                             "VALUES (?, 100, 0.5, 0.5)", (ids[guid],))
            conn.execute("INSERT INTO refs VALUES (?, ?)", ("c" * 32, "f" * 32))
            conn.execute("INSERT INTO used_by VALUES (?, ?, 'Page', 'Prefab')",
                         (ids["f" * 32], "9" * 32))
            conn.commit()
            stats = animations.build(root, conn)
            layers, nodes = (json.loads(v) for v in conn.execute(
                "SELECT layers, nodes FROM animations WHERE asset_id = ?",
                (ids["f" * 32],)).fetchone())
        finally:
            conn.close()          # Windows will not remove a directory it still holds
    check("a clip draws only the art under the Animator that plays it",
          [layer["name"] for layer in layers], ["Elephant"])
    check("curves on the Animator's own object move that object",
          "rot" in nodes[layers[0]["chain"][-1]], True)
    check("and leave the prefab root where it is",
          "rot" in nodes[layers[0]["chain"][0]], False)
    check("the clip was bound through its Animator, not guessed",
          (stats["bound"], stats["guessed"]), (1, 0))


def rotation_unwrap_checks() -> None:
    """A quaternion turn past half a turn keeps going instead of spinning back."""
    text = chr(10).join([
        "  m_RotationCurves:",
        "  - curve:",
        "      serializedVersion: 2",
        "      m_Curve:",
        "      - serializedVersion: 3",
        "        time: 0",
        "        value: {x: 0, y: 0, z: 0.9961947, w: 0.0871557}",
        "      - serializedVersion: 3",
        "        time: 1",
        "        value: {x: 0, y: 0, z: 0.9961947, w: -0.0871557}",
        "    path: Arm",
        "  m_CompressedRotationCurves: []", ""])
    keys = parse_transform_curves(text)["Arm"]["euler"]
    check("a turn from 170 to 190 degrees is not read as a turn back to -170",
          [round(key[3]) for key in keys], [170, 190])


PAIR_PREFAB = """%YAML 1.1
--- !u!1 &100
GameObject:
  m_Name: Pair
  m_IsActive: 1
--- !u!4 &400
Transform:
  m_GameObject: {fileID: 100}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Children:
  - {fileID: 401}
  - {fileID: 402}
  - {fileID: 403}
  m_Father: {fileID: 0}
--- !u!1 &101
GameObject:
  m_Name: Left
  m_IsActive: 1
--- !u!4 &401
Transform:
  m_GameObject: {fileID: 101}
  m_LocalPosition: {x: -1, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 400}
--- !u!212 &212001
SpriteRenderer:
  m_GameObject: {fileID: 101}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}
--- !u!1 &102
GameObject:
  m_Name: Right
  m_IsActive: 1
--- !u!4 &402
Transform:
  m_GameObject: {fileID: 102}
  m_LocalPosition: {x: 1, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 400}
--- !u!212 &212002
SpriteRenderer:
  m_GameObject: {fileID: 102}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, type: 3}
--- !u!1 &103
GameObject:
  m_Name: Hidden
  m_IsActive: 0
--- !u!4 &403
Transform:
  m_GameObject: {fileID: 103}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 400}
--- !u!212 &212003
SpriteRenderer:
  m_GameObject: {fileID: 103}
  m_Enabled: 1
  m_Sprite: {fileID: 21300000, guid: dddddddddddddddddddddddddddddddd, type: 3}
"""


def prefab_pose_checks() -> None:
    """A prefab is drawn as it assembles: parts where it puts them, hidden ones left out."""
    import json
    from . import prefabs
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sprites").mkdir()
        for name, colour in (("red", (255, 0, 0, 255)), ("blue", (0, 0, 255, 255)),
                             ("green", (0, 255, 0, 255))):
            Image.new("RGBA", (100, 100), colour).save(root / "sprites" / f"{name}.png")
        (root / "Pair.prefab").write_text(PAIR_PREFAB, encoding="utf-8")
        conn = connect(root / "t.db")
        try:
            for guid, name in (("a" * 32, "red"), ("b" * 32, "blue"), ("d" * 32, "green")):
                conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type, ext, "
                             "width, height, image_path) VALUES (?, ?, ?, 'Sprite', 'asset', "
                             "100, 100, ?)", (guid, f"{name}.asset", name, f"sprites/{name}.png"))
            conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type, ext) "
                         "VALUES (?, 'Pair.prefab', 'Pair', 'Prefab', 'prefab')", ("9" * 32,))
            for (asset_id,) in conn.execute(
                    "SELECT id FROM assets WHERE unity_type='Sprite'").fetchall():
                conn.execute("INSERT INTO sprites (asset_id, ppu, anchor_x, anchor_y) "
                             "VALUES (?, 100, 0.5, 0.5)", (asset_id,))
            conn.commit()
            stats = prefabs.build(root, root, conn)
            row = conn.execute(
                "SELECT sprites, pose, figures, main, fill FROM prefab_poses").fetchone()
            with Image.open(root / row["pose"]) as pose:
                pose = pose.convert("RGBA")
                w, h = pose.size
                left = pose.getpixel((round(w * 0.2), h // 2))
                right = pose.getpixel((round(w * 0.8), h // 2))
                middle = pose.getpixel((w // 2, h // 2))
            pieces = len(json.loads(row["sprites"]))
            # The blue part turned fully transparent: its quad is still there, and the
            # picture is framed by what is drawn, not by the quads.
            Image.new("RGBA", (100, 100), (0, 0, 255, 0)).save(root / "sprites" / "blue.png")
            prefabs.build(root, root, conn)
            with Image.open(root / conn.execute(
                    "SELECT pose FROM prefab_poses").fetchone()[0]) as framed:
                framed_w, framed_h = framed.size
        finally:
            conn.close()          # Windows will not remove a directory it still holds
    check("a prefab with two sprites on show is drawn once", stats["drawn"], 1)
    check("its picture knows how much of its frame it fills", 0.5 < row["fill"] < 0.8, True)
    check("and is framed by the pixels drawn, not by the quads",
          abs(framed_w / framed_h - 1) < 0.15, True)
    check("each part lands where the prefab puts it",
          (left[0] > 200 and left[2] < 60, right[2] > 200 and right[0] < 60), (True, True))
    check("a part the prefab ships switched off is left off", middle[3], 0)
    check("its pieces are the two sprites it shows", pieces, 2)
    check("two parts standing apart are two figures, neither one most of it",
          (row["figures"], row["main"]), (2, 0.5))


UI_PREFAB = """%YAML 1.1
--- !u!1 &100
GameObject:
  m_Name: Card
  m_IsActive: 1
--- !u!224 &400
RectTransform:
  m_GameObject: {fileID: 100}
  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Children:
  - {fileID: 401}
  - {fileID: 402}
  m_Father: {fileID: 0}
  m_AnchorMin: {x: 0.5, y: 0.5}
  m_AnchorMax: {x: 0.5, y: 0.5}
  m_AnchoredPosition: {x: 0, y: 0}
  m_SizeDelta: {x: 200, y: 100}
  m_Pivot: {x: 0.5, y: 0.5}
--- !u!95 &95001
Animator:
  m_GameObject: {fileID: 100}
  m_Controller: {fileID: 9100000, guid: cccccccccccccccccccccccccccccccc, type: 2}
--- !u!1 &101
GameObject:
  m_Name: Left
  m_IsActive: 1
--- !u!224 &401
RectTransform:
  m_GameObject: {fileID: 101}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 400}
  m_AnchorMin: {x: 0, y: 0.5}
  m_AnchorMax: {x: 0, y: 0.5}
  m_AnchoredPosition: {x: 50, y: 0}
  m_SizeDelta: {x: 100, y: 100}
  m_Pivot: {x: 0.5, y: 0.5}
--- !u!114 &114001
MonoBehaviour:
  m_GameObject: {fileID: 101}
  m_Enabled: 1
  m_Color: {r: 1, g: 1, b: 1, a: 1}
  m_Sprite: {fileID: 21300000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}
  m_Type: 0
  m_PreserveAspect: 0
  m_FillMethod: 4
  m_PixelsPerUnitMultiplier: 1
--- !u!1 &102
GameObject:
  m_Name: Right
  m_IsActive: 1
--- !u!224 &402
RectTransform:
  m_GameObject: {fileID: 102}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 400}
  m_AnchorMin: {x: 1, y: 0.5}
  m_AnchorMax: {x: 1, y: 0.5}
  m_AnchoredPosition: {x: -50, y: 0}
  m_SizeDelta: {x: 100, y: 100}
  m_Pivot: {x: 0.5, y: 0.5}
--- !u!114 &114002
MonoBehaviour:
  m_GameObject: {fileID: 102}
  m_Enabled: 1
  m_Color: {r: 1, g: 1, b: 1, a: 1}
  m_Sprite: {fileID: 21300000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, type: 3}
  m_Type: 0
  m_PreserveAspect: 0
  m_FillMethod: 4
  m_PixelsPerUnitMultiplier: 1
"""

# A clip that lifts the left card by 30 canvas pixels, the way uGUI animates.
UI_CLIP = """%YAML 1.1
--- !u!74 &7400000
AnimationClip:
  m_Name: Lift
  m_PositionCurves: []
  m_FloatCurves:
  - curve:
      serializedVersion: 2
      m_Curve:
      - serializedVersion: 3
        time: 0
        value: 0
      - serializedVersion: 3
        time: 1
        value: 30
    attribute: m_AnchoredPosition.y
    path: Left
    classID: 224
  m_PPtrCurves: []
  m_SampleRate: 60
"""


def ui_rig_checks() -> None:
    """A uGUI tree is laid out by its anchors, drawn, and moved by its anchored position."""
    import json
    from . import animations, prefabs
    records = parse_prefab_rig(UI_PREFAB)
    check("a uGUI Image is a layer like a sprite renderer", [r["path"] for r in records],
          ["Left", "Right"])
    check("each is placed by its anchors and anchored position",
          [r["chain"][-1]["base"][:2] for r in records], [[-0.5, 0.0], [0.5, 0.0]])
    check("and fills its rect", records[0]["draw"], [1.0, 1.0])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sprites").mkdir()
        for name, colour in (("red", (255, 0, 0, 255)), ("blue", (0, 0, 255, 255))):
            Image.new("RGBA", (40, 40), colour).save(root / "sprites" / f"{name}.png")
        (root / "Card.prefab").write_text(UI_PREFAB, encoding="utf-8")
        (root / "Lift.anim").write_text(UI_CLIP, encoding="utf-8")
        conn = connect(root / "t.db")
        try:
            for guid, path, kind, ext, image in (
                    ("a" * 32, "red.asset", "Sprite", "asset", "sprites/red.png"),
                    ("b" * 32, "blue.asset", "Sprite", "asset", "sprites/blue.png"),
                    ("f" * 32, "Lift.anim", "AnimationClip", "anim", None),
                    ("c" * 32, "Lift.controller", "AnimatorController", "controller", None),
                    ("9" * 32, "Card.prefab", "Prefab", "prefab", None)):
                conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type, ext, "
                             "width, height, image_path) VALUES (?, ?, ?, ?, ?, 40, 40, ?)",
                             (guid, path, path.split(".")[0], kind, ext, image))
            ids = dict(conn.execute("SELECT guid, id FROM assets").fetchall())
            for guid in ("a" * 32, "b" * 32):
                conn.execute("INSERT INTO sprites (asset_id, ppu, anchor_x, anchor_y) "
                             "VALUES (?, 100, 0.5, 0.5)", (ids[guid],))
            conn.execute("INSERT INTO refs VALUES (?, ?)", ("c" * 32, "f" * 32))
            conn.execute("INSERT INTO used_by VALUES (?, ?, 'Card', 'Prefab')",
                         (ids["f" * 32], "9" * 32))
            conn.commit()
            prefabs.build(root, root, conn)
            pose_path = conn.execute("SELECT pose FROM prefab_poses").fetchone()[0]
            with Image.open(root / pose_path) as pose:
                pose = pose.convert("RGBA")
                w, h = pose.size
                left, right = pose.getpixel((w // 4, h // 2)), pose.getpixel((3 * w // 4, h // 2))
            animations.build(root, conn)
            layers, nodes = (json.loads(v) for v in conn.execute(
                "SELECT layers, nodes FROM animations WHERE asset_id = ?",
                (ids["f" * 32],)).fetchone())
        finally:
            conn.close()          # Windows will not remove a directory it still holds
    check("its still has each sprite filling its own half",
          (left[0] > 200 and left[2] < 60, right[2] > 200 and right[0] < 60), (True, True))
    lifted = next(layer for layer in layers if layer["name"] == "Left")
    check("an anchored-position curve moves the part from where its anchors put it",
          nodes[lifted["chain"][-1]]["pos"], [[0.0, -0.5, 0.0, 0.0], [1.0, -0.5, 0.3, 0.0]])


def record_checks() -> None:
    """A re-run mirrors its export; bundle records and the run itself are recorded."""
    import json
    from .classify import bundle_records
    from .core import prune_dependents, run_record
    from .doctor import WARN, diagnose_outcome
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Assets"
        (root / "Sprite").mkdir(parents=True)
        (root / "Spine").mkdir()
        for name in ("keep", "gone"):
            (root / "Sprite" / f"{name}.asset").write_text("%YAML 1.1", encoding="utf-8")
        (root / "Spine" / "hero.atlas").write_text("hero.png", encoding="utf-8")
        conn = connect(Path(tmp) / "t.db")
        try:
            build_index(root, conn)
            ids = dict(conn.execute("SELECT name, id FROM assets").fetchall())
            conn.execute("INSERT INTO assets (rel_path, name, unity_type) "
                         "VALUES ('Spine/hero.atlas#arm', 'arm', 'Sprite')")
            conn.execute("INSERT INTO tags VALUES (?, 'role', 'UI', 'high', 'graph')",
                         (ids["gone"],))
            conn.commit()
            (root / "Sprite" / "gone.asset").unlink()
            build_index(root, conn)
            names = {row[0] for row in conn.execute("SELECT name FROM assets")}
            tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
            left = prune_dependents(conn)
        finally:
            conn.close()          # Windows will not remove a directory it still holds
        check("a file gone from the export takes its row with it",
              ("gone" in names, "keep" in names), (False, True))
        check("and every row that named it", (tags, left), (0, {}))
        check("a region cut from a file that is still there stays", "arm" in names, True)

        records = Path(tmp) / "Primary" / "AssetBundle"
        records.mkdir(parents=True)
        (records / "ui_shop.json").write_text(json.dumps(
            {"m_AssetBundleName": "ui_shop",
             "m_Container": {"Assets/UI/Shop/ShopDialog.prefab": {},
                             "Assets/UI/Shop/ShopAtlas.spriteatlas": {}}}), encoding="utf-8")
        check("every entry of every bundle record is kept as a record",
              sorted(bundle_records(Path(tmp) / "Primary")),
              [("ui_shop", "Assets/UI/Shop/ShopAtlas.spriteatlas"),
               ("ui_shop", "Assets/UI/Shop/ShopDialog.prefab")])

        out = Path(tmp) / "out"
        conn = connect(out / "assetlab.db")
        try:
            conn.execute("INSERT INTO assets (rel_path, name) VALUES ('a.png', 'a')")
            conn.executemany("INSERT INTO animations (asset_id, curve_summary, layer_count, "
                             "frame_count) VALUES (?, 'position', 0, 0)",
                             [(1000 + i,) for i in range(30)])
            conn.commit()
        finally:
            conn.close()
        report = diagnose_outcome(out, peers=[])
    check("a build whose clips carry curves and draw nothing is warned about",
          any(c.status == WARN and "clips with curves are drawn" in c.message
              for c in report.checks), True)
    record = run_record(["assetlab", "--test"])
    check("a run records the code, the interpreter and the command",
          (set(record) >= {"assetlab_commit", "assetlab_dirty", "python", "argv"},
           record["argv"]), (True, ["assetlab", "--test"]))


def skeleton_checks() -> None:
    """A Spine skeleton is read in either encoding and posed from its cut regions."""
    import json
    import struct
    from .skeleton import draw_pose, read_skeleton
    from .spine import parse_atlas

    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            low, n = n & 0x7F, n >> 7
            out.append(low | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def text(value: str) -> bytes:
        data = value.encode()
        return varint(len(data) + 1) + data

    def floats(*values: float) -> bytes:
        return struct.pack(f">{len(values)}f", *values)

    white = struct.pack(">i", -1)

    def body(sequence: bool) -> bytes:
        seq = b"\x00" if sequence else b""

        def region(key: int, x: float) -> bytes:
            # key, name (none: the key), type 0, path (none: the name), transform, colour
            return (varint(key) + varint(0) + bytes([0]) + varint(0)
                    + floats(0, x, 0, 1, 1, 100, 100) + white + seq)

        mesh = (varint(3) + varint(0) + bytes([2]) + varint(0) + white + varint(4)
                + floats(0, 0, 1, 0, 1, 1, 0, 1) + varint(6)
                + struct.pack(">6H", 0, 1, 2, 2, 3, 0)
                + b"\x00" + floats(-50, 180, 50, 180, 50, 80, -50, 80) + varint(4) + seq)
        return (floats(0, 0, 0, 0) + b"\x00"
                + varint(3) + text("red") + text("blue") + text("green")
                + varint(1) + text("root") + floats(0, 0, 0, 1, 1, 0, 0, 0) + varint(0) + b"\x00"
                + varint(3)
                + text("a") + varint(0) + white + white + varint(1) + varint(0)
                + text("b") + varint(0) + white + white + varint(2) + varint(0)
                + text("c") + varint(0) + white + white + varint(3) + varint(0)
                + varint(0) + varint(0) + varint(0)
                + varint(3)
                + varint(0) + varint(1) + region(1, -60)
                + varint(1) + varint(1) + region(2, 60)
                + varint(2) + varint(1) + mesh
                + varint(0))

    modern = bytes(8) + text("4.1.24") + body(True)
    older = text("abc") + text("3.8.99") + body(False)
    for data, version in ((modern, "4.1.24"), (older, "3.8.99")):
        skeleton = read_skeleton(data)
        check(f"a Spine {version[:3]} binary skeleton is read through its skins",
              (skeleton["version"], [slot["name"] for slot in skeleton["slots"]],
               sorted(a["type"] for a in skeleton["skins"][0]["attachments"].values())),
              (version, ["a", "b", "c"], ["mesh", "region", "region"]))
    from_json = read_skeleton(json.dumps({
        "skeleton": {"spine": "2.1.27"}, "bones": [{"name": "root"}],
        "slots": [{"name": "a", "bone": "root", "attachment": "red"}],
        "skins": {"default": {"a": {"red": {"width": 100, "height": 100}}}}}).encode())
    check("and a JSON one, of any version",
          [a["type"] for a in from_json["skins"][0]["attachments"].values()], ["region"])

    atlas = chr(10).join(["page.png", "size: 256,256", "format: RGBA8888",
                          "filter: Linear,Linear", "repeat: none", "eye", "  rotate: false",
                          "  xy: 2, 2", "  size: 20, 10", "  orig: 40, 30", "  offset: 5, 6",
                          "  index: -1", ""])
    stripped = parse_atlas(atlas)[0]
    check("a stripped region keeps its original size and where it sat in it",
          (stripped["orig"], stripped["offset"]), ((40, 30), (5, 6)))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        regions = {}
        for name, colour in (("red", (255, 0, 0, 255)), ("blue", (0, 0, 255, 255)),
                             ("green", (0, 255, 0, 255))):
            Image.new("RGBA", (100, 100), colour).save(root / f"{name}.png")
            regions[name] = {"image": root / f"{name}.png", "w": 100, "h": 100}
        drawn = draw_pose(read_skeleton(modern), regions, root / "pose.png")
        with Image.open(root / "pose.png") as pose:
            pose = pose.convert("RGBA")
            w, h = pose.size
            left = pose.getpixel((round(w * 0.25), round(h * 0.75)))
            right = pose.getpixel((round(w * 0.75), round(h * 0.75)))
            top = pose.getpixel((w // 2, round(h * 0.25)))
        from .skeleton import _paint
        canvas, _ = _paint([{"slot": 0, "image": root / "red.png", "colour": (1.0, 1.0, 1.0, 1.0),
                             "triangles": [(((0, 0), (100, 0), (0, 100)),
                                            ((0, 0), (400, 0.3), (200, 0)))]}],
                           (0, -10, 400, 10))
        sliver = canvas.getchannel("A").getbbox()
    check("its setup pose puts each region where its bone and offset say",
          (left[0] > 200 and left[2] < 60, right[2] > 200 and right[0] < 60), (True, True))
    check("and textures a mesh across its triangles", top[1] > 200 and top[0] < 60, True)
    check("three pieces drawn", drawn["pieces"], 3)
    check("a triangle thinner than a pixel draws nothing", sliver, None)


def small_unit_checks() -> None:
    """A UI laid out in tiny units is still drawn at the size its sprites can hold."""
    from . import prefabs
    tiny = (UI_PREFAB.replace("{x: 200, y: 100}", "{x: 2, y: 1}")
            .replace("{x: 100, y: 100}", "{x: 1, y: 1}")
            .replace("{x: 50, y: 0}", "{x: 0.5, y: 0}").replace("{x: -50, y: 0}", "{x: -0.5, y: 0}"))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sprites").mkdir()
        for name, colour in (("red", (255, 0, 0, 255)), ("blue", (0, 0, 255, 255))):
            Image.new("RGBA", (200, 200), colour).save(root / "sprites" / f"{name}.png")
        (root / "Tiny.prefab").write_text(tiny, encoding="utf-8")
        conn = connect(root / "t.db")
        try:
            for guid, name in (("a" * 32, "red"), ("b" * 32, "blue")):
                conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type, ext, width, "
                             "height, image_path) VALUES (?, ?, ?, 'Sprite', 'asset', 200, 200, ?)",
                             (guid, f"{name}.asset", name, f"sprites/{name}.png"))
                conn.execute("INSERT INTO sprites (asset_id, ppu, anchor_x, anchor_y) "
                             "VALUES (last_insert_rowid(), 100, 0.5, 0.5)")
            conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type, ext) "
                         "VALUES (?, 'Tiny.prefab', 'Tiny', 'Prefab', 'prefab')", ("9" * 32,))
            conn.commit()
            prefabs.build(root, root, conn)
            width, height = conn.execute("SELECT width, height FROM prefab_poses").fetchone()
        finally:
            conn.close()          # Windows will not remove a directory it still holds
    check("a UI laid out in hundredths is drawn as large as its sprites allow",
          (width > 400, abs(width / height - 2) < 0.2), (True, True))


def level_report_checks() -> None:
    """A level format with no config does not stop the report."""
    from . import levels
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "t.db")
        try:
            conn.executemany(
                "INSERT INTO levels (levelset, level_no, rel_path, properties, obstacle_layers, "
                "object_layers) VALUES (?, ?, ?, ?, '', '')",
                [("main", 1, "a.json", ""), ("main", 2, "b.json", '{"moves": 20}')])
            conn.commit()
            try:
                text = levels.report(conn, Path(tmp))
            except Exception as problem:           # noqa: BLE001 - the failure is the finding
                text = f"raised {type(problem).__name__}"
        finally:
            conn.close()
    check("levels whose config is an empty string are reported, not fatal",
          "`moves`: set on 1 levels" in text, True)


def addressables_checks() -> None:
    """What a catalogue declares, against what the package it ships in holds."""
    import base64
    import json
    import struct
    runtime = "{UnityEngine.AddressableAssets.Addressables.RuntimePath}"
    ids = [runtime + "/Android/ui_shop_assets_all_aaa.bundle",
           "https://cdn.example.invalid/Android/remotegroup_assets_bg_bbb.bundle",
           "Assets/UI/Shop.prefab", "Assets/Art/Bg.png"]
    # internal id, provider, dependency key, dependency hash, data, primary key, type
    entries = [(0, 0, -1, 0, -1, 0, 0), (1, 0, -1, 0, -1, 1, 0),
               (2, 1, 0, 0, -1, 2, 1), (3, 1, 1, 0, -1, 3, 1)]
    entry_blob = struct.pack("<i", len(entries)) + b"".join(
        struct.pack("<7i", *entry) for entry in entries)
    # key 0 resolves to the shipped bundle, key 1 to the remote one
    bucket_blob = (struct.pack("<i", 2) + struct.pack("<2i", 0, 1) + struct.pack("<i", 0)
                   + struct.pack("<2i", 0, 1) + struct.pack("<i", 1))
    catalog = {"m_InternalIds": ids, "m_InternalIdPrefixes": [],
               "m_EntryDataString": base64.b64encode(entry_blob).decode(),
               "m_BucketDataString": base64.b64encode(bucket_blob).decode()}
    with tempfile.TemporaryDirectory() as tmp:
        aa = Path(tmp) / "assets" / "aa"
        (aa / "Android").mkdir(parents=True)
        (aa / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        (aa / "Android" / "ui_shop_assets_all_aaa.bundle").write_bytes(b"UnityFS")
        found = read_catalog(aa / "catalog.json", Path(tmp))
    bundles = {bundle["group"]: bundle for bundle in found["bundles"]}
    check("a bundle under the runtime path is local and in the package",
          (bundles["ui_shop"]["location"], bundles["ui_shop"]["shipped"]), ("local", True))
    check("a bundle behind an address is remote and not in the package",
          (bundles["remotegroup"]["location"], bundles["remotegroup"]["shipped"]),
          ("remote", False))
    check("an entry that needs a missing bundle is named, one that does not is not",
          found["unreachable"], ["Assets/Art/Bg.png"])
    check("no address is written into the report",
          "cdn.example" in json.dumps(found), False)


def type_checks() -> None:
    """A serialized object's own header types it; its folder does not."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lighting = root / "Scenes" / "Game" / "LightingData.asset"
        lighting.parent.mkdir(parents=True)
        lighting.write_bytes(bytes(16) + b"binary serialized file")
        check("an object the header cannot place is not typed by its folder",
              infer_type(Path("Scenes/Game/LightingData.asset"), lighting),
              "SerializedAsset")
        icon = root / "Sprite" / "icon.asset"
        icon.parent.mkdir()
        icon.write_text(chr(10).join(["%YAML 1.1", "--- !u!213 &21300000", "Sprite:", ""]),
                        encoding="utf-8")
        check("its header still types it", infer_type(Path("Sprite/icon.asset"), icon),
              "Sprite")


def bundle_reach_checks() -> None:
    """A bundle's label reaches the contents of what its manifest names."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "t.db")
        for guid, name, kind, ext in (
                ("g_dialog", "ShopDialog", "Prefab", "prefab"),
                ("g_tex", "shop_bg", "Texture2D", "png"),
                ("g_page", "sactx-0-1024x1024-ASTC 6x6-ShopAtlas-abc12345", "Texture2D",
                 "png"),
                ("g_sprite", "shop_button", "Sprite", "asset"),
                ("g_page2", "sactx-0-2048x2048-ASTC 6x6-board atlas-def67890", "Texture2D",
                 "png"),
                ("g_tile", "board_tile", "Sprite", "asset"),
                ("g_other", "unrelated", "Sprite", "asset")):
            conn.execute("INSERT INTO assets (guid, rel_path, name, unity_type, ext) "
                         "VALUES (?, ?, ?, ?, ?)", (guid, f"{name}.{ext}", name, kind, ext))
        conn.execute("INSERT INTO refs VALUES ('g_dialog', 'g_tex')")
        sprite_id = conn.execute(
            "SELECT id FROM assets WHERE guid = 'g_sprite'").fetchone()[0]
        conn.execute("INSERT INTO sprites (asset_id, atlas_guid, x, y, w, h) "
                     "VALUES (?, 'g_page', 0, 0, 10, 10)", (sprite_id,))
        tile_id = conn.execute("SELECT id FROM assets WHERE guid = 'g_tile'").fetchone()[0]
        conn.execute("INSERT INTO sprites (asset_id, atlas_guid, x, y, w, h) "
                     "VALUES (?, 'g_page2', 0, 0, 10, 10)", (tile_id,))
        reached = bundle_reach(conn, {
            "shopdialog": ("b1", "Assets/Game/UI/Shop/ShopDialog.prefab"),
            "shopatlas": ("b2", "Assets/Game/UI/Shop/ShopAtlas.spriteatlas"),
            "board atlas": ("b3", "assets/common_atlas/board atlas.spriteatlasv2")})
        conn.close()
    check("a bundle's label reaches what its container references",
          reached.get("g_tex", (None,))[0], "b1")
    check("and every sprite on the pages of an atlas it names",
          reached.get("g_sprite", (None,))[0], "b2")
    check("the V2 packer's atlas reaches its pages' sprites too",
          reached.get("g_tile", (None,))[0], "b3")
    check("but not what it does not reach", "g_other" in reached, False)
    check("and nothing at all without a bundle map", bundle_reach(None, {}), {})


def ripper_checks() -> None:
    """The primary content pass asks AssetRipper for that export, and its records are read."""
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "AssetBundle"
        records.mkdir()
        # Named after the bundle, as AssetRipper writes it; not every build's bundles
        # end in `.bundle`.
        (records / "common_atlas.unity3d.json").write_text(
            '{"m_AssetBundleName": "common_atlas", "m_Container": '
            '{"assets/common_atlas/board atlas.spriteatlasv2": {}}}', encoding="utf-8")
        check("a bundle record is read whatever its bundle is named",
              load_bundle_map(Path(tmp)).get("board atlas"),
              ("common_atlas", "assets/common_atlas/board atlas.spriteatlasv2"))

    posted: list[tuple[str, dict]] = []

    class Stub(http.server.BaseHTTPRequestHandler):
        # Answers the way AssetRipper does: a form POST redirects to the home page.
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            fields = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
            posted.append((self.path, fields))
            (Path(fields["path"]) / "Assets").mkdir(parents=True, exist_ok=True)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = (Path(tmp) / "game_primary").resolve()
            stub = Ripper(base_url=f"http://127.0.0.1:{server.server_port}")
            exported = stub.export_primary_content(target)
            check("the primary content pass calls AssetRipper's own export for it",
                  [path for path, _ in posted], ["/Export/PrimaryContent"])
            check("into the folder it was given", posted[0][1].get("path"), str(target))
            check("and hands back that export's Assets", exported, target / "Assets")
    finally:
        server.shutdown()
        server.server_close()


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

    mesh_checks()
    gate_checks()
    absent_tree_checks()
    desktop_staging_checks()
    corpus_checks()
    scriptable_corpus_checks()
    object_checks()
    animator_binding_checks()
    rotation_unwrap_checks()
    prefab_pose_checks()
    ui_rig_checks()
    record_checks()
    skeleton_checks()
    small_unit_checks()
    level_report_checks()
    spine_checks()
    addressables_checks()
    type_checks()
    bundle_reach_checks()
    spine_catalogue_checks()
    index_rerun_checks()
    thumbnail_checks()
    ripper_checks()
    outcome_checks()

    for line in FAILED:
        print("FAIL", line)
    print(f"analysis: {len(PASSED)} passed, {len(FAILED)} failed")

    from .ingest.selftest import main as ingest_main
    ingest_failed = ingest_main()

    # The page's own JavaScript, run in a browser. Reported separately because it can
    # be skipped for want of one, and a skipped check must not read as a passing one.
    from .jstest import run as run_browser_checks
    browser = run_browser_checks()
    for line in browser["failed"]:
        print("FAIL", line)
    if browser["skipped"]:
        print(f"browser: SKIPPED - {browser['skipped']}")
    else:
        print(f"browser: {browser['passed']} passed, "
              f"{len(browser['failed'])} failed")
    raise SystemExit(1 if (FAILED or ingest_failed or browser["failed"]) else 0)


if __name__ == "__main__":
    main()
