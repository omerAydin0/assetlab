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
    layer_count   INTEGER
);

CREATE TABLE IF NOT EXISTS piece_groups (
    group_key TEXT NOT NULL,   -- mechanic + shared name stem
    asset_id  INTEGER NOT NULL,
    position  TEXT,            -- tl t tr | l c r | bl b br
    PRIMARY KEY (group_key, asset_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_piece_asset ON piece_groups(asset_id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Additive migrations so an existing database keeps working across versions.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
    for column, decl in (("primary_role", "TEXT"), ("primary_feature", "TEXT"),
                         ("primary_mechanic", "TEXT")):
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
    sprite_columns = {row[1] for row in conn.execute("PRAGMA table_info(sprites)")}
    for column, decl in (("ppu", "REAL"), ("anchor_x", "REAL"), ("anchor_y", "REAL"),
                         ("border", "TEXT")):
        if sprite_columns and column not in sprite_columns:
            conn.execute(f"ALTER TABLE sprites ADD COLUMN {column} {decl}")
    conn.commit()
    return conn


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


def make_thumbnail(image: Image.Image, target: Path, size: int = 224) -> None:
    thumb = image.copy()
    thumb.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (30, 32, 36, 255))
    canvas.alpha_composite(thumb, ((size - thumb.width) // 2, (size - thumb.height) // 2))
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(target, "JPEG", quality=88, optimize=True)
