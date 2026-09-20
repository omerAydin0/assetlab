"""Shared helpers: SQLite schema, path/type inference, image and hash utilities.

AssetLab never writes into the AssetRipper export tree. Every output goes to the
directory passed as --out.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None

GUID_RE = re.compile(rb"guid: ([0-9a-f]{32})")
META_GUID_RE = re.compile(r"^guid:\s*([0-9a-f]{32})\s*$", re.MULTILINE)

VISUAL_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".gif", ".webp", ".tif", ".tiff"}
AUDIO_EXT = {".ogg", ".wav", ".mp3", ".flac", ".m4a", ".aac"}

# Files worth scanning for outgoing GUID references.
REFERENCING_EXT = {".prefab", ".unity", ".mat", ".asset", ".anim", ".controller",
                   ".spriteatlas", ".playable", ".overrideController", ".physicMaterial"}

# Top-level export folder -> Unity type. Falls back to extension.
FOLDER_TYPE = {
    "animationclip": "AnimationClip",
    "animatorcontroller": "AnimatorController",
    "audioclip": "AudioClip",
    "font": "Font",
    "gameobject": "Prefab",
    "material": "Material",
    "mesh": "Mesh",
    "monobehaviour": "MonoBehaviour",
    "scenes": "Scene",
    "shader": "Shader",
    "shadervariantcollection": "ShaderVariantCollection",
    "sprite": "Sprite",
    "texture2d": "Texture2D",
}

EXT_TYPE = {
    ".prefab": "Prefab",
    ".unity": "Scene",
    ".mat": "Material",
    ".anim": "AnimationClip",
    ".controller": "AnimatorController",
    ".shader": "Shader",
    ".spriteatlas": "SpriteAtlas",
    ".cs": "Script",
    ".dll": "Assembly",
    ".ttf": "Font",
    ".otf": "Font",
    ".bytes": "TextAsset",
    ".txt": "TextAsset",
}

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY,
    guid         TEXT UNIQUE,
    rel_path     TEXT NOT NULL UNIQUE,
    name         TEXT,
    unity_type   TEXT,
    ext          TEXT,
    size_bytes   INTEGER,
    width        INTEGER,
    height       INTEGER,
    has_alpha    INTEGER,
    alpha_ratio  REAL,
    is_grayscale INTEGER,
    dominant_hex TEXT,
    sha256       TEXT,
    dhash        TEXT,
    duration_seconds REAL,
    sample_rate  INTEGER,
    channels     INTEGER,
    audio_codec  TEXT,
    image_path   TEXT,   -- viewable image (atlas png, or sliced sprite png)
    thumbnail    TEXT,
    duplicate_group TEXT,
    primary_role     TEXT,
    primary_feature  TEXT,
    primary_mechanic TEXT   -- gameplay mechanic, e.g. Paper / Safe / MatchItem
);
CREATE INDEX IF NOT EXISTS idx_assets_guid ON assets(guid);
CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(unity_type);

CREATE TABLE IF NOT EXISTS refs (
    src_guid TEXT NOT NULL,
    dst_guid TEXT NOT NULL,
    PRIMARY KEY (src_guid, dst_guid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_refs_dst ON refs(dst_guid);

CREATE TABLE IF NOT EXISTS tags (
    asset_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,   -- role | feature | subcategory
    value      TEXT NOT NULL,
    confidence TEXT NOT NULL,   -- high | medium | low
    source     TEXT NOT NULL,   -- bundle | classid | graph | filename | project_dir
    PRIMARY KEY (asset_id, kind, value, source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_tags_asset ON tags(asset_id);

CREATE TABLE IF NOT EXISTS used_by (
    asset_id    INTEGER NOT NULL,
    holder_guid TEXT NOT NULL,
    holder_name TEXT,
    holder_type TEXT,
    PRIMARY KEY (asset_id, holder_guid)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bundles (
    bundle_name  TEXT NOT NULL,
    project_path TEXT NOT NULL,
    collection   TEXT,
    path_id      TEXT,
    PRIMARY KEY (bundle_name, project_path)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sprites (
    asset_id    INTEGER PRIMARY KEY,
    atlas_guid  TEXT,
    x INTEGER, y INTEGER, w INTEGER, h INTEGER,
    rotated INTEGER,   -- packed at 90 degrees: its box on the sheet is h by w
    sliced_path TEXT,
    ppu         REAL,   -- m_PixelsToUnits; 140 here, not the 100 default
    anchor_x    REAL,   -- pivot as a fraction of the sliced image, left to right
    anchor_y    REAL,   -- ... and top to bottom, so it can drive a CSS translate
    border      TEXT    -- JSON [left, bottom, right, top] nine-slice margins, px
);

CREATE TABLE IF NOT EXISTS levels (
    levelset       TEXT NOT NULL,
    level_no       INTEGER NOT NULL,
    rel_path       TEXT,
    grid_w         INTEGER,
    grid_h         INTEGER,
    layer_count    INTEGER,
    depth_layers   INTEGER,   -- stacked board layers: how deeply items are buried
    obstacle_layers TEXT,     -- comma separated names (Paper1, bubble, cage, ...)
    obstacle_tiles INTEGER,
    shelf_tiles    INTEGER,
    shelf_types    INTEGER,
    item_tiles     INTEGER,
    distinct_items INTEGER,
    time_limit     INTEGER,
    move_limit     INTEGER,   -- move-based games use this instead of a timer
    object_groups  INTEGER,   -- Tiled objectgroup layers (spawners, zones)
    properties     TEXT,      -- raw level config as JSON
    PRIMARY KEY (levelset, level_no)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS animations (
    asset_id      INTEGER PRIMARY KEY,
    duration      REAL,
    sample_rate   REAL,
    frame_count   INTEGER,
    track_count   INTEGER,
    track_path    TEXT,
    frames        TEXT,   -- JSON [[time, sprite image path], ...]
    curve_summary TEXT,   -- which transform/float curves the clip animates
    layers        TEXT,   -- JSON: sprite layers; chain entries index into `nodes`
    nodes         TEXT,   -- JSON: de-duplicated transform nodes shared by the layers
    masks         TEXT,   -- JSON: SpriteMask rectangles the layers are clipped to
    layer_count   INTEGER,
    holder_id     INTEGER   -- the prefab whose Animator plays it
);

CREATE TABLE IF NOT EXISTS prefab_poses (
    prefab_id   INTEGER PRIMARY KEY,
    sprites     TEXT,      -- JSON asset ids of the sprites it shows, back to front
    layer_count INTEGER,
    width       INTEGER,
    height      INTEGER,
    pose        TEXT,      -- the picture, relative to the catalogue folder
    same_as     INTEGER,   -- an earlier prefab that draws the identical picture
    figures     INTEGER,   -- separate clusters of overlapping parts it draws
    main        REAL,      -- the largest cluster's share of its parts
    fill        REAL       -- the share of its picture's frame that is opaque
);

CREATE TABLE IF NOT EXISTS spine_poses (
    descriptor TEXT PRIMARY KEY,   -- the atlas descriptor, relative to the export
    pose       TEXT,               -- the setup-pose picture, relative to the catalogue
    width      INTEGER,
    height     INTEGER,
    version    TEXT,               -- the Spine version the skeleton was written by
    pieces     INTEGER,
    fill       REAL
);

CREATE TABLE IF NOT EXISTS piece_groups (
    group_key TEXT NOT NULL,   -- mechanic + shared name stem
    asset_id  INTEGER NOT NULL,
    position  TEXT,            -- tl t tr | l c r | bl b br
    PRIMARY KEY (group_key, asset_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_piece_asset ON piece_groups(asset_id);

-- What a 3D build draws with. Stays empty for a 2D build, which is the point:
-- the profile decides whether the stage that fills it runs at all.
CREATE TABLE IF NOT EXISTS models (
    id           INTEGER PRIMARY KEY,
    prefab_id    INTEGER,
    prefab_name  TEXT,
    path         TEXT,      -- object path inside the prefab
    object_name  TEXT,
    mesh_guid    TEXT,
    mesh_name    TEXT,
    mesh_bytes   INTEGER,
    skinned      INTEGER,   -- 1 for a SkinnedMeshRenderer, i.e. rigged geometry
    materials    TEXT,      -- JSON: [{name, colour, textures:[{slot, name, img}]}]
    matrix       TEXT,      -- JSON: 16 floats, the object's place inside its prefab
    render_path  TEXT,      -- this one mesh, drawn on its own
    tri_count    INTEGER,
    vert_count   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_models_prefab ON models(prefab_id);
CREATE INDEX IF NOT EXISTS idx_models_mesh ON models(mesh_guid);

-- A prefab drawn whole: every mesh it owns, each in its own place. A chair on its
-- own is a shape; a chair under a table with a penguin on it is the game.
CREATE TABLE IF NOT EXISTS scenes (
    id          INTEGER PRIMARY KEY,
    prefab_id   INTEGER,     -- the prefab, or the scene file, the object came from
    prefab_name TEXT,
    object_name TEXT,        -- null for a prefab: the file already names it
    placements  INTEGER,     -- how many times a scene places this same shape
    render_path TEXT,
    part_count  INTEGER,
    tri_count   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_scenes_prefab ON scenes(prefab_id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Additive migrations so an existing database keeps working across versions.
    level_columns = {row[1] for row in conn.execute("PRAGMA table_info(levels)")}
    if "object_layers" not in level_columns:
        conn.execute("ALTER TABLE levels ADD COLUMN object_layers TEXT")

    # `scenes` is purely derived, and its key changed when a scene - which holds
    # many objects rather than being one - became a source.
    scene_columns = {row[1] for row in conn.execute("PRAGMA table_info(scenes)")}
    if scene_columns and "object_name" not in scene_columns:
        conn.execute("DROP TABLE scenes")
        conn.executescript(SCHEMA)

    model_columns = {row[1] for row in conn.execute("PRAGMA table_info(models)")}
    for column, decl in (("matrix", "TEXT"), ("render_path", "TEXT"),
                         ("tri_count", "INTEGER"), ("vert_count", "INTEGER"),
                         ("placements", "INTEGER")):
        if column not in model_columns:
            conn.execute(f"ALTER TABLE models ADD COLUMN {column} {decl}")

    existing = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
    for column, decl in (("primary_role", "TEXT"), ("primary_feature", "TEXT"),
                         ("primary_mechanic", "TEXT"),
                         # 'engine' for what Unity and its packages ship, 'game' for
                         # what the studio authored. See classify.mark_origin.
                         ("origin", "TEXT"),
                         ("hold_count", "INTEGER")):
        if column not in existing:
            conn.execute(f"ALTER TABLE assets ADD COLUMN {column} {decl}")
    # `levels` is purely derived, so an outdated shape is rebuilt rather than patched.
    level_columns = {row[1] for row in conn.execute("PRAGMA table_info(levels)")}
    if level_columns and "move_limit" not in level_columns:
        conn.execute("DROP TABLE levels")
        conn.executescript(SCHEMA)
    animation_columns = {row[1] for row in conn.execute("PRAGMA table_info(animations)")}
    if animation_columns and "masks" not in animation_columns:
        conn.execute("DROP TABLE animations")
        conn.executescript(SCHEMA)
    animation_columns = {row[1] for row in conn.execute("PRAGMA table_info(animations)")}
    if animation_columns and "holder_id" not in animation_columns:
        conn.execute("ALTER TABLE animations ADD COLUMN holder_id INTEGER")
    # Purely derived, so an outdated shape is rebuilt by the prefabs stage.
    pose_columns = {row[1] for row in conn.execute("PRAGMA table_info(prefab_poses)")}
    if pose_columns and not {"figures", "fill"} <= pose_columns:
        conn.execute("DROP TABLE prefab_poses")
        conn.executescript(SCHEMA)
    sprite_columns = {row[1] for row in conn.execute("PRAGMA table_info(sprites)")}
    for column, decl in (("ppu", "REAL"), ("anchor_x", "REAL"), ("anchor_y", "REAL"),
                         ("border", "TEXT"), ("rotated", "INTEGER")):
        if sprite_columns and column not in sprite_columns:
            conn.execute(f"ALTER TABLE sprites ADD COLUMN {column} {decl}")
    conn.commit()
    return conn


#: How much of a large file is held at once while scanning it. The overlap has to
#: exceed the longest pattern read across the boundary; a guid reference is 38 bytes.
SCAN_BLOCK = 1 << 22
SCAN_OVERLAP = 64

#: A file this size or larger is streamed rather than read whole. A prefab never
#: reaches it; a generated scene is a thousand times over.
STREAM_ABOVE = 1 << 24


def scan_matches(path: Path, pattern: re.Pattern[bytes]):
    """First group of every match in a file, without holding the file.

    A hand-authored scene is a few megabytes. A scene a build assembles from a
    prop library is several hundred, and `read_bytes` on twenty of them asks for
    more memory than the machine has. Blocks overlap so a match that straddles a
    boundary is still read exactly once: a match is kept only when it begins at
    or after the point the previous block stopped reporting from.
    """
    try:
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        tail = b""
        while True:
            block = handle.read(SCAN_BLOCK)
            if not block:
                break
            window = tail + block
            floor = max(0, len(tail) - SCAN_OVERLAP)
            for match in pattern.finditer(window):
                if match.start() >= floor:
                    yield match.group(1).decode("ascii")
            tail = window[-SCAN_OVERLAP:]


def scan_guids(path: Path):
    """Every guid in a file, streamed."""
    return scan_matches(path, GUID_RE)


def scan_class_ids(path: Path):
    """Every Unity class id declared by a document header, streamed."""
    return scan_matches(path, CLASS_ID_HEAD_RE)


CLASS_ID_HEAD_RE = re.compile(rb"--- !u!(\d+) &")


def stream_documents(path: Path):
    """Yield ``(class_id, file_id, body)`` for each document in a Unity YAML file.

    Every stage that reads a prefab splits the whole text on the document header.
    That is the right thing for a file measured in kilobytes and the wrong thing
    for one measured in hundreds of megabytes, where the split alone costs several
    gigabytes. Line-at-a-time the cost is one document.
    """
    class_id: int | None = None
    file_id = ""
    body: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("--- !u!"):
                if class_id is not None:
                    yield class_id, file_id, "".join(body)
                found = DOC_HEAD_RE.match(line)
                if found:
                    class_id, file_id, body = int(found.group(1)), found.group(2), []
                else:                       # a header we cannot read ends the run
                    class_id, file_id, body = None, "", []
            elif class_id is not None:
                body.append(line)
    if class_id is not None:
        yield class_id, file_id, "".join(body)


DOC_HEAD_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)")


# A YAML asset declares its own Unity class id, which beats any folder guess.
# `_Studio/Sprites/Atlas/Gameplay/MatchItems/BS_147.asset` is a real Sprite.
CLASS_ID_TYPE = {
    21: "Material", 28: "Texture2D", 43: "Mesh", 48: "Shader", 74: "AnimationClip",
    83: "AudioClip", 91: "AnimatorController", 114: "MonoBehaviour", 128: "Font",
    213: "Sprite", 687078895: "SpriteAtlas",
}
FIRST_CLASS_ID_RE = re.compile(rb"--- !u!(\d+) &")


def class_id_type(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(256)
    except OSError:
        return None
    match = FIRST_CLASS_ID_RE.search(head)
    return CLASS_ID_TYPE.get(int(match.group(1))) if match else None


def infer_type(rel: Path, path: Path | None = None) -> str:
    ext = rel.suffix.lower()
    if ext in EXT_TYPE:
        return EXT_TYPE[ext]
    if ext in VISUAL_EXT:
        return "Texture2D"
    if ext in AUDIO_EXT:
        return "AudioClip"
    if ext == ".asset" and path is not None:
        declared = class_id_type(path)
        if declared:
            return declared
        # A serialized object says what it is in its own header, and the folder it sits
        # in cannot overrule that. Scene folders hold LightingData.asset and
        # LightProbes.asset beside the scenes; taking the folder's word typed twenty of
        # them "Scene" across six builds. One the header read cannot place stays
        # unplaced, which is what it is.
        if path.is_file():
            return "SerializedAsset"
    # Fall back to the folder the asset sits in.
    for part in rel.parts[:-1]:
        mapped = FOLDER_TYPE.get(part.lower())
        if mapped:
            return mapped
    if ext == ".asset":
        return "SerializedAsset"
    # Checked after the folder map so a `Sprite/x.json` stays a Sprite; games that
    # ship configuration and level data as loose JSON land here.
    if ext in {".json", ".xml", ".csv", ".tsv"}:
        return "TextAsset"
    return "Other"


def read_meta_guid(meta_path: Path) -> str | None:
    try:
        text = meta_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = META_GUID_RE.search(text)
    return match.group(1) if match else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_rgba(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGBA")


def image_stats(image: Image.Image) -> dict[str, Any]:
    """Dimensions, alpha coverage, grayscale test and mean visible colour."""
    array = np.asarray(image, dtype=np.uint8)
    height, width = array.shape[0], array.shape[1]
    alpha = array[..., 3]
    visible = alpha > 8
    alpha_ratio = float(visible.mean()) if visible.size else 0.0
    rgb = array[..., :3].astype(np.int16)
    if visible.any():
        shown = rgb[visible]
        mean = shown.mean(axis=0).astype(int)
        dominant = "#{:02x}{:02x}{:02x}".format(*mean)
        spread = (shown.max(axis=1) - shown.min(axis=1))
        grayscale = bool((spread <= 8).mean() > 0.95)
    else:
        dominant, grayscale = None, False
    return {
        "width": width,
        "height": height,
        "has_alpha": int(bool((alpha < 255).any())),
        "alpha_ratio": round(alpha_ratio, 5),
        "is_grayscale": int(grayscale),
        "dominant_hex": dominant,
    }


def dhash(image: Image.Image) -> str:
    """Difference hash over a white-composited grayscale 9x8 reduction."""
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    background.alpha_composite(image)
    small = background.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = np.asarray(small, dtype=np.int16)
    bits = (pixels[:, :8] > pixels[:, 1:]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


#: How far a small sprite may be blown up to fill its card. Past this the art is more
#: blur than information, and a 12-pixel icon shown at 96 is already unmistakable.
MAX_ENLARGE = 8


def make_thumbnail(image: Image.Image, target: Path, size: int = 224) -> None:
    """Fit the art to the frame, in both directions.

    ``Image.thumbnail`` only ever shrinks. Most of a mobile build's sprites are far
    smaller than the frame - a 71x73 blocker occupied a tenth of a 224x224 canvas and
    then the card scaled that canvas down again, so a screen of real artwork read as
    a screen of empty boxes. Aspect ratio is kept, so a 1024x8 gradient strip stays a
    strip: it genuinely is one.
    """
    thumb = image.copy()
    scale = min(size / max(1, thumb.width), size / max(1, thumb.height), MAX_ENLARGE)
    if abs(scale - 1) > 0.01:
        wide = max(1, round(thumb.width * scale))
        tall = max(1, round(thumb.height * scale))
        thumb = thumb.resize((wide, tall), Image.Resampling.LANCZOS if scale < 1
                             else Image.Resampling.BICUBIC)
    canvas = Image.new("RGBA", (size, size), (30, 32, 36, 255))
    canvas.alpha_composite(thumb, ((size - thumb.width) // 2, (size - thumb.height) // 2))
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(target, "JPEG", quality=88, optimize=True)


def prune_dependents(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete every row that names an asset the catalogue no longer holds.

    Read from the schema rather than listed, so a table added later is covered without
    anyone remembering to add it here. A clip whose holder prefab is gone keeps the
    clip and forgets the holder. -> {table.column: rows}
    """
    removed: dict[str, int] = {}
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'assets'").fetchall():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info([{table}])")}
        for column in ("asset_id", "prefab_id", "holder_id"):
            if column not in columns:
                continue
            verb = (f"UPDATE [{table}] SET {column} = NULL" if column == "holder_id"
                    else f"DELETE FROM [{table}]")
            cursor = conn.execute(f"{verb} WHERE {column} IS NOT NULL "
                                  f"AND {column} NOT IN (SELECT id FROM assets)")
            if cursor.rowcount:
                removed[f"{table}.{column}"] = cursor.rowcount
    return removed


def run_record(argv: list[str] | None = None) -> dict:
    """What produced a catalogue: the code, the interpreter and the command line.

    Two months on, a folder of results is only worth as much as the answer to "made
    from what, by which version". The package's own git commit answers the second,
    with a flag when the working tree held changes the commit does not.
    """
    import platform
    import subprocess
    import sys
    from datetime import datetime, timezone
    package = Path(__file__).resolve().parent.parent

    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(package), *args], capture_output=True,
                                  text=True, timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "assetlab_commit": git("rev-parse", "HEAD") or None,
            "assetlab_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
            "python": platform.python_version(),
            "argv": list(sys.argv if argv is None else argv)}
