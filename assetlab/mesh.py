"""Read a Unity Mesh and draw it, so a 3D build can be looked at rather than listed.

A 3D build's art is geometry, and geometry has no thumbnail. What the catalogue can
show instead - a UV atlas - is unreadable: the flower texture is petals scattered
across a 2048 square and the penguin texture is 63% empty space. Neither tells you
what the game looks like. So the meshes are rasterised here.

Three things about the format are worth writing down, because each cost a wrong
turn to find:

* **`dimension` is a bitfield in recent Unity.** Read raw it returns values like 52
  for a 4-component channel; only the low nibble is the count.
* **Streams are laid out end to end, each padded to 16 bytes.** A channel's `offset`
  is within its own stream, not within the blob.
* **Vertex layout varies between meshes in the same build.** One mesh here stores
  normals as float32 in a 40-byte stride, another as float16 in 32 bytes.

Rasterising is done with numpy over a triangle at a time: pure Python is too slow
for a 50,000-triangle mesh, and a real GPU is not available or wanted here.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

CHANNEL_RE = re.compile(
    r"- stream: (\d+)\s*\n\s*offset: (\d+)\s*\n\s*format: (\d+)\s*\n\s*dimension: (\d+)")
VERTEX_COUNT_RE = re.compile(r"m_VertexCount: (\d+)")
BINDPOSE_RE = re.compile(
    r"  - e00: ([-\d.eE+]+)\s*\n(?:\s+e\d\d: [-\d.eE+]+\s*\n){15}")
BINDPOSE_VALUES_RE = re.compile(r"e\d\d: ([-\d.eE+]+)")
BLOB_RE = re.compile(r"_typelessdata:\s*([0-9a-f]+)")
INDEX_RE = re.compile(r"m_IndexBuffer:\s*([0-9a-f]+)")
INDEX_FORMAT_RE = re.compile(r"m_IndexFormat: (\d+)")
DATA_SIZE_RE = re.compile(r"m_DataSize: (\d+)")

#: Unity's VertexAttributeFormat, as (bytes, numpy dtype, divisor). A divisor turns
#: a normalised integer back into the float it stands for; 1 leaves it alone.
FORMATS = {
    0:  (4, "<f4", 1.0),        # Float32
    1:  (2, "<f2", 1.0),        # Float16
    2:  (1, "u1", 255.0),       # UNorm8
    3:  (1, "i1", 127.0),       # SNorm8
    4:  (2, "<u2", 65535.0),    # UNorm16
    5:  (2, "<i2", 32767.0),    # SNorm16
    6:  (1, "u1", 1.0),         # UInt8
    7:  (1, "i1", 1.0),         # SInt8
    8:  (2, "<u2", 1.0),        # UInt16
    9:  (2, "<i2", 1.0),        # SInt16
    10: (4, "<u4", 1.0),        # UInt32
    11: (4, "<i4", 1.0),        # SInt32
}
DEFAULT_FORMAT = (4, "<f4", 1.0)
POSITION, NORMAL, TANGENT, COLOUR, UV0 = 0, 1, 2, 3, 4
#: Where a rigged mesh keeps its skin: four weights and four bone indices per vertex.
BLEND_WEIGHT, BLEND_INDICES = 12, 13


@dataclass
class Mesh:
    name: str
    vertices: np.ndarray            # (n, 3) float32
    normals: np.ndarray | None      # (n, 3) float32
    uvs: np.ndarray | None          # (n, 2) float32
    triangles: np.ndarray           # (m, 3) int32
    submesh_ranges: list[tuple[int, int]]   # (first triangle, count) per submesh
    #: The first bind pose, when the mesh is rigged. Together with the first bone's
    #: world matrix it is what actually places a skinned mesh - the renderer's own
    #: Transform does not.
    bindpose: np.ndarray | None = None
    #: Every bind pose, in bone order, as (bones, 4, 4).
    bindposes: np.ndarray | None = None
    #: (vertices, 4) weights and the bone indices they apply to.
    skin_weights: np.ndarray | None = None
    skin_bones: np.ndarray | None = None

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)


PACKED_RE_CACHE: dict[str, re.Pattern] = {}


def _packed(text: str, field: str) -> dict | None:
    """One PackedBitVector: `m_NumItems`, optional range/start, hex data, bit size."""
    if field not in PACKED_RE_CACHE:
        PACKED_RE_CACHE[field] = re.compile(
            r"    " + field + r":\s*\n"
            r"      m_NumItems: (\d+)\s*\n"
            r"(?:      m_Range: ([-\d.eE+]+)\s*\n      m_Start: ([-\d.eE+]+)\s*\n)?"
            r"      m_Data:\s*([0-9a-f]*)\s*\n"
            r"      m_BitSize: (\d+)")
    found = PACKED_RE_CACHE[field].search(text)
    if not found:
        return None
    count, span, start, payload, bits = found.groups()
    return {"count": int(count), "range": float(span) if span else None,
            "start": float(start) if start else None,
            "data": payload, "bits": int(bits)}


def _unpack(vector: dict | None) -> np.ndarray | None:
    """Read `count` little-endian values of `bits` width out of the bit stream."""
    if not vector or not vector["count"] or not vector["bits"] or not vector["data"]:
        return None
    count, width = vector["count"], vector["bits"]
    raw = np.frombuffer(bytes.fromhex(vector["data"]), dtype=np.uint8)
    bits = np.unpackbits(raw, bitorder="little")
    needed = count * width
    if len(bits) < needed:
        return None
    grid = bits[:needed].reshape(count, width).astype(np.uint64)
    weights = (np.uint64(1) << np.arange(width, dtype=np.uint64))
    return (grid * weights).sum(axis=1)


def _unpack_float(vector: dict | None) -> np.ndarray | None:
    """The same, scaled back out of its quantised range."""
    values = _unpack(vector)
    if values is None or vector["range"] is None:
        return values.astype(np.float32) if values is not None else None
    largest = float((1 << vector["bits"]) - 1) or 1.0
    return (vector["start"] + vector["range"] * (values / largest)).astype(np.float32)


def parse_compressed(text: str, name: str) -> "Mesh | None":
    """Geometry from `m_CompressedMesh`, used when there is no vertex buffer."""
    block_start = text.find("m_CompressedMesh")
    if block_start < 0:
        return None
    block = text[block_start:]

    flat = _unpack_float(_packed(block, "m_Vertices"))
    indices = _unpack(_packed(block, "m_Triangles"))
    if flat is None or indices is None or len(flat) < 3:
        return None
    vertices = flat[:len(flat) // 3 * 3].reshape(-1, 3)
    triangles = indices[:len(indices) // 3 * 3].reshape(-1, 3).astype(np.int32)
    triangles = triangles[(triangles < len(vertices)).all(axis=1)]
    if not len(triangles):
        return None

    # Normals keep only x and y; z is recovered from unit length, and its sign is
    # carried in a separate one-bit vector.
    normals = None
    pair = _unpack_float(_packed(block, "m_Normals"))
    signs = _unpack(_packed(block, "m_NormalSigns"))
    if pair is not None and len(pair) >= 2 * len(vertices):
        xy = pair[:len(vertices) * 2].reshape(-1, 2)
        z = np.sqrt(np.clip(1.0 - (xy ** 2).sum(axis=1), 0.0, 1.0))
        if signs is not None and len(signs) >= len(vertices):
            z = np.where(signs[:len(vertices)] > 0, z, -z)
        normals = np.column_stack([xy, z]).astype(np.float32)

    uvs = None
    flat_uv = _unpack_float(_packed(block, "m_UV"))
    if flat_uv is not None and len(flat_uv) >= 2 * len(vertices):
        uvs = flat_uv[:len(vertices) * 2].reshape(-1, 2)

    return Mesh(name, vertices, normals, uvs, triangles,
                [(0, len(triangles))], first_bindpose(text))


def all_bindposes(text: str) -> np.ndarray | None:
    """Every bind pose, in bone order, or None when the mesh is not rigged."""
    block_start = text.find("m_BindPose:")
    if block_start < 0:
        return None
    # The list ends where the next top-level key begins.
    end = len(text)
    for key in ("m_BoneNameHashes:", "m_RootBoneNameHash:", "m_BonesAABB:",
                "m_VariableBoneCountWeights:", "m_MeshCompression:"):
        found = text.find("\n  " + key, block_start)
        if found >= 0:
            end = min(end, found)
    # `end` is the index of the newline that closes the last entry, and finditer
    # treats endpos as a hard end of string - so without the extra character the
    # final bind pose cannot match its own line ending and is silently dropped. That
    # left a fourteen-bone rig with thirteen poses, and every vertex weighted to the
    # missing bone flew off on its own.
    poses = []
    for match in BINDPOSE_RE.finditer(text, block_start, end + 1):
        values = [float(v) for v in BINDPOSE_VALUES_RE.findall(match.group(0))]
        if len(values) == 16:
            poses.append(values)
    if not poses:
        return None
    return np.array(poses, dtype=np.float32).reshape(-1, 4, 4)


def first_bindpose(text: str) -> np.ndarray | None:
    """The mesh's first bind pose as a 4x4, or None when the mesh is not rigged."""
    poses = all_bindposes(text)
    return poses[0] if poses is not None else None


def _channel_table(text: str) -> list[dict]:
    block = text[text.index("m_Channels"):text.index("m_DataSize")]
    table = []
    for stream, offset, fmt, dimension in CHANNEL_RE.findall(block):
        table.append({
            "stream": int(stream), "offset": int(offset), "format": int(fmt),
            # Only the low nibble is the component count; the rest are flags.
            "dimension": int(dimension) & 0x0F,
        })
    return table


def parse_mesh(path: Path) -> Mesh | None:
    """Read one exported Mesh asset. Returns None when it carries no geometry."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if "m_Channels" not in text or "m_DataSize" not in text:
        return None

    channels = _channel_table(text)
    count_match = VERTEX_COUNT_RE.search(text)
    blob_match = BLOB_RE.search(text)
    index_match = INDEX_RE.search(text)
    if not (count_match and blob_match and index_match):
        return parse_compressed(text, path.stem)

    count = int(count_match.group(1))
    data = bytes.fromhex(blob_match.group(1))
    if not count or not data:
        # No vertex buffer: either a quantised mesh, or an empty node.
        return parse_compressed(text, path.stem)

    # Stream layout: each stream's rows are contiguous, and the next stream starts
    # on a 16-byte boundary after it.
    used = [c for c in channels if c["dimension"]]
    strides: dict[int, int] = {}
    starts: dict[int, int] = {}
    cursor = 0
    streams = sorted({c["stream"] for c in used})
    for index, stream in enumerate(streams):
        items = [c for c in used if c["stream"] == stream]
        stride = max(c["offset"] + c["dimension"]
                     * FORMATS.get(c["format"], DEFAULT_FORMAT)[0]
                     for c in items)
        stride = (stride + 3) & ~3
        strides[stream], starts[stream] = stride, cursor
        cursor += count * stride
        if index + 1 < len(streams):
            # The padding exists so the *next* stream starts on a 16-byte
            # boundary. Nothing follows the last one, and m_DataSize ends exactly
            # where its rows end - so padding it too makes every single-stream
            # mesh whose rows are not a multiple of 16 fail the size check and be
            # thrown away as unreadable.
            cursor = (cursor + 15) & ~15
    declared = DATA_SIZE_RE.search(text)
    if cursor > len(data) or (declared and cursor != int(declared.group(1))
                              and int(declared.group(1)) > 0):
        # The strides do not reproduce the size Unity wrote, so the layout is not
        # understood and reading it would return plausible-looking noise.
        return parse_compressed(text, path.stem)

    buffer = np.frombuffer(data, dtype=np.uint8)

    def read(index: int) -> np.ndarray | None:
        if index >= len(channels):
            return None
        channel = channels[index]
        width = channel["dimension"]
        if not width:
            return None
        size, dtype, divisor = FORMATS.get(channel["format"], DEFAULT_FORMAT)
        stride = strides[channel["stream"]]
        base = starts[channel["stream"]] + channel["offset"]
        rows = np.lib.stride_tricks.as_strided(
            buffer[base:], shape=(count, width * size), strides=(stride, 1))
        values = np.frombuffer(rows.tobytes(), dtype=dtype).reshape(count, width)
        values = values.astype(np.float32)
        return values / divisor if divisor != 1.0 else values

    vertices = read(POSITION)
    if vertices is None or vertices.shape[1] < 3:
        return None
    vertices = vertices[:, :3]

    normals = read(NORMAL)
    if normals is not None and normals.shape[1] >= 3:
        normals = normals[:, :3]
    else:
        normals = None

    uvs = read(UV0)
    uvs = uvs[:, :2] if uvs is not None and uvs.shape[1] >= 2 else None

    index_size = 2 if int(INDEX_FORMAT_RE.search(text).group(1)) == 0 else 4
    raw = bytes.fromhex(index_match.group(1))
    indices = np.frombuffer(raw, dtype="<u2" if index_size == 2 else "<u4")
    usable = (len(indices) // 3) * 3
    triangles = indices[:usable].reshape(-1, 3).astype(np.int32)
    triangles = triangles[(triangles < count).all(axis=1)]
    if not len(triangles):
        return None

    ranges = []
    for first_byte, index_count in re.findall(
            r"firstByte: (\d+)\s*\n\s*indexCount: (\d+)", text):
        start = int(first_byte) // index_size // 3
        ranges.append((start, int(index_count) // 3))

    weights = read(BLEND_WEIGHT)
    bones = None
    if weights is not None and weights.shape[1] == 4:
        raw_bones = read(BLEND_INDICES)
        bones = raw_bones.astype(np.int32) if raw_bones is not None \
            and raw_bones.shape[1] == 4 else None
    if bones is None:
        weights = None

    poses = all_bindposes(text)
    return Mesh(path.stem, vertices, normals, uvs, triangles, ranges,
                poses[0] if poses is not None else None, poses, weights, bones)


# --------------------------------------------------------------------- rendering


#: Below this fraction of the largest extent, an axis is a slab's thickness rather
#: than one of its dimensions.
SLAB_RATIO = 0.18


def up_axis(points: np.ndarray) -> tuple[int, float]:
    """Which axis the subject stands on, as (index, +1 or -1). See module note."""
    low, high = points.min(axis=0), points.max(axis=0)
    size = high - low
    largest = float(size.max()) or 1.0

    thin = [i for i in range(3) if size[i] / largest < SLAB_RATIO]
    if len(thin) == 1:
        axis = thin[0]
    else:
        # Y and Z are the candidates; X is a game object's width by convention.
        axis = 1 if size[1] >= size[2] else 2

    horizontal = [i for i in range(3) if i != axis]
    span = float(size[axis])
    if span <= 0:
        return axis, 1.0

    def width(mask) -> float:
        """Horizontal footprint of one slab, ignoring stray vertices."""
        slab = points[mask]
        if len(slab) < 8:
            return 0.0
        # Percentiles rather than min/max: one antenna should not widen a slab.
        low_edge = np.percentile(slab[:, horizontal], 8, axis=0)
        high_edge = np.percentile(slab[:, horizontal], 92, axis=0)
        return float(np.prod(np.maximum(high_edge - low_edge, 1e-6)))

    along = points[:, axis]
    bottom = width(along <= low[axis] + span * 0.28)
    top = width(along >= high[axis] - span * 0.28)
    # The wider quarter is the base, and up points away from it.
    return axis, (1.0 if bottom >= top else -1.0)


def orient(points: np.ndarray, axis: int, sign: float) -> np.ndarray:
    """Rotate the subject so its own up axis becomes the camera's Y.

    Negating one row to flip the up axis also flips the frame's handedness, which
    turns the camera through the subject and shows its back. A second negation on a
    horizontal row restores a determinant of +1, so this stays a rotation.
    """
    matrix = np.zeros((3, 3), dtype=np.float32)
    horizontal = [i for i in range(3) if i != axis]
    matrix[0, horizontal[0]] = 1.0
    matrix[1, axis] = sign
    matrix[2, horizontal[1]] = sign
    return points @ matrix.T


def look_matrix(yaw: float = -30.0, pitch: float = 22.0) -> np.ndarray:
    """A fixed three-quarter view. An axis-aligned one flattens most props."""
    y, p = math.radians(yaw), math.radians(pitch)
    rot_y = np.array([[math.cos(y), 0, -math.sin(y)],
                      [0, 1, 0],
                      [math.sin(y), 0, math.cos(y)]], dtype=np.float32)
    rot_x = np.array([[1, 0, 0],
                      [0, math.cos(p), -math.sin(p)],
                      [0, math.sin(p), math.cos(p)]], dtype=np.float32)
    return rot_x @ rot_y


@dataclass
class Piece:
    """One mesh placed in the world, with the colour or texture it is drawn in."""

    mesh: Mesh
    transform: np.ndarray | None = None       # 4x4, world placement
    colour: tuple[int, int, int] = (150, 155, 165)
    texture: Image.Image | None = None
    #: Alpha of the mask a cutout shader keys on, as a 2D array. Foliage is built
    #: from quads with the leaf shape in the alpha channel; drawn opaque, a bush
    #: comes out as a stack of plates.
    cutout: object | None = None
    submesh: int | None = None                # which submesh this piece covers


#: Where a cutout shader puts its edge. Unity's own default is 0.5 of 255.
CUTOUT_ALPHA = 127

LIGHT = np.array([0.35, 0.75, 0.55], dtype=np.float32)
LIGHT /= np.linalg.norm(LIGHT)


#: Quarter turns the search tries. Half-steps would double the cost to separate views
#: that a three-quarter camera already distinguishes.
FACINGS = (-30.0, 60.0, 150.0, 240.0)
PROBE_SIZE = 112
#: How many triangles a probe render draws at most. A 112-square image cannot
#: show more silhouette than this, and the probe only has to rank four angles.
PROBE_TRIANGLES = 1200


def interest(image: Image.Image) -> float:
    """How much there is to look at, over the pixels the subject actually covers.

    Two terms, both measured against the same four quarter turns of one penguin whose
    front and back are known:

    * spread of colour, which is high where a design is and low on a plain surface;
    * how much light the visible side catches, since a front is modelled and lit and
      a back is usually a smooth shadowed field.

    A warm-vs-cool term was tried first and was exactly backwards: the penguin's white
    front is neutral while its black back carries the orange feet and yellow hat, so
    it scored the back highest and turned every character around.
    """
    pixels = np.asarray(image, dtype=np.float32).reshape(-1, 3)
    lit = pixels[pixels.sum(axis=1) > 60]        # ignore the background
    if len(lit) < 40:
        return 0.0
    return float(lit.std(axis=0).mean() + 0.25 * lit.mean())


def render(pieces: list[Piece], size: int = 384,
           background: tuple[int, int, int] = (14, 16, 20),
           yaw: float | None = None, stride: int = 1) -> Image.Image | None:
    """Draw the pieces together, orthographic, z-buffered, lit from one direction."""
    pieces = [p for p in pieces if p.mesh is not None and len(p.mesh.triangles)]
    if not pieces:
        return None

    if yaw is None:
        # Four probe renders decide which way round to draw the thing, and they
        # were costing four times what the picture itself costs: the per-triangle
        # work is the same at 112 square as at 320, because it is loop overhead
        # rather than pixels. The probe only has to say which angle shows the most,
        # and that judgement does not need every triangle - so a dense mesh is
        # probed on a regular sample of its triangles, which keeps the silhouette
        # it is judging while cutting the count to something the size can show.
        total = sum(len(p.mesh.triangles) for p in pieces)
        # Its own name: assigning to `stride` here overwrote the parameter, and the
        # picture this call went on to draw was sampled too, which is what turned a
        # smooth surface into a lattice of loose triangles.
        probe_stride = max(1, total // PROBE_TRIANGLES)
        best, best_score = FACINGS[0], -1.0
        for candidate in FACINGS:
            probe = render(pieces, size=PROBE_SIZE, background=background,
                           yaw=candidate, stride=probe_stride)
            score = interest(probe) if probe else 0.0
            if score > best_score:
                best, best_score = candidate, score
        yaw = best

    view = look_matrix(yaw=yaw)

    world = []
    for piece in pieces:
        points = piece.mesh.vertices
        normals = piece.mesh.normals
        if piece.transform is not None:
            homogeneous = np.concatenate(
                [points, np.ones((len(points), 1), np.float32)], axis=1)
            points = (homogeneous @ piece.transform.T)[:, :3]
            if normals is not None:
                normals = normals @ piece.transform[:3, :3].T
        world.append((piece, points, normals))

    # One reading of "up" for the whole assembly, so a hat stays on the head.
    if all(piece.transform is not None for piece in pieces):
        # Placed by a world transform, and Unity's world up is +Y, so there is
        # nothing to guess. The guess decides which end is the base by which end is
        # wider, which is right for a statue and upside down for a tree: a willow
        # was drawn hanging, its canopy reading as a bowl and its trunk as a stalk
        # coming out of the top.
        axis, sign = 1, 1.0
    else:
        axis, sign = up_axis(np.concatenate([p for _, p, _ in world], axis=0))

    placed = []
    for piece, points, normals in world:
        camera = orient(points, axis, sign) @ view.T
        if normals is not None:
            normals = orient(normals, axis, sign) @ view.T
        placed.append((piece, camera, normals))

    everything = np.concatenate([c for _, c, _ in placed], axis=0)
    low, high = everything.min(axis=0), everything.max(axis=0)
    span = float(max(high[0] - low[0], high[1] - low[1])) or 1.0
    scale = (size - 28) / span
    centre = (high + low) / 2

    colour_buffer = np.zeros((size, size, 3), dtype=np.float32)
    colour_buffer[:] = np.array(background, dtype=np.float32)
    depth_buffer = np.full((size, size), np.inf, dtype=np.float32)

    for piece, camera, normals in placed:
        screen = np.empty_like(camera)
        screen[:, 0] = (camera[:, 0] - centre[0]) * scale + size / 2
        screen[:, 1] = size / 2 - (camera[:, 1] - centre[1]) * scale
        screen[:, 2] = camera[:, 2]

        triangles = piece.mesh.triangles
        if piece.submesh is not None and piece.submesh < len(piece.mesh.submesh_ranges):
            first, run = piece.mesh.submesh_ranges[piece.submesh]
            triangles = triangles[first:first + run]

        if stride > 1:
            triangles = triangles[::stride]
        base = np.array(piece.colour, dtype=np.float32)
        _rasterise(triangles, screen, normals, piece, base,
                   colour_buffer, depth_buffer, size)

    _expose(colour_buffer, depth_buffer, background)
    return Image.fromarray(np.clip(colour_buffer, 0, 255).astype(np.uint8))


#: What the brightest part of a subject should reach, and how far the picture may
#: be lifted to get it there. A cap matters: without one, a genuinely black object
#: is amplified into grey noise and claims detail it does not have.
#: Only a picture darker than the floor is lifted, and only as far as the floor.
#: Lifting everything to one target was worse than the problem: it pushed every
#: flat-coloured rock to near-white and threw away the difference between a pale
#: one and a dark one.
EXPOSURE_FLOOR = 70.0
EXPOSURE_TARGET = 120.0
#: Measured, not chosen: the darkest rock in a real build sat at a 95th-percentile
#: brightness of 22 against a target of 190, so it needs 8.6. Ten leaves a little
#: room and still refuses to make something out of a subject that is truly black.
EXPOSURE_MAX_GAIN = 10.0


def _expose(colour_buffer, depth_buffer, background) -> None:
    """Lift a subject that came out too dark to tell from the page behind it.

    A build lights its world with a sky; this draws with one lamp and no ambient,
    so a dark albedo stays dark. On a near-black page that is a black square where
    a rock should be - measured on a real catalogue, the most-placed rock in the
    build was drawn at a brightness the page could not separate from its own
    background. The lift is measured from the subject alone, not the background,
    and applied to the subject alone, so the page keeps its own contrast.
    """
    drawn = np.isfinite(depth_buffer)
    if not drawn.any():
        return
    subject = colour_buffer[drawn]
    # The 95th percentile rather than the maximum: one specular pixel should not
    # decide the exposure for the whole thing.
    brightest = float(np.percentile(subject.max(axis=1), 95))
    if brightest <= 1.0 or brightest >= EXPOSURE_FLOOR:
        return
    gain = min(EXPOSURE_TARGET / brightest, EXPOSURE_MAX_GAIN)
    colour_buffer[drawn] = np.clip(subject * gain, 0, 255)


def _rasterise(triangles, screen, normals, piece, base,
               colour_buffer, depth_buffer, size) -> None:
    corners = screen[triangles]                       # (m, 3, 3)
    minimum = np.floor(corners[:, :, :2].min(axis=1)).astype(np.int32)
    maximum = np.ceil(corners[:, :, :2].max(axis=1)).astype(np.int32)
    np.clip(minimum, 0, size - 1, out=minimum)
    np.clip(maximum, 0, size - 1, out=maximum)

    # Back-face cull in screen space: a closed mesh hides half its triangles.
    edge1 = corners[:, 1, :2] - corners[:, 0, :2]
    edge2 = corners[:, 2, :2] - corners[:, 0, :2]
    area = edge1[:, 0] * edge2[:, 1] - edge1[:, 1] * edge2[:, 0]
    keep = (np.abs(area) > 1e-6) & (maximum[:, 0] > minimum[:, 0]) \
        & (maximum[:, 1] > minimum[:, 1])

    uvs = piece.mesh.uvs
    texture = None
    if piece.texture is not None and uvs is not None:
        texture = np.asarray(piece.texture.convert("RGB"), dtype=np.float32)

    for index in np.nonzero(keep)[0]:
        tri = triangles[index]
        p0, p1, p2 = corners[index]
        x0, x1 = minimum[index, 0], maximum[index, 0]
        y0, y1 = minimum[index, 1], maximum[index, 1]

        ys, xs = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        xs = xs.astype(np.float32) + 0.5
        ys = ys.astype(np.float32) + 0.5
        denominator = area[index]
        w0 = ((p1[1] - p2[1]) * (xs - p2[0]) + (p2[0] - p1[0]) * (ys - p2[1])) / denominator
        w1 = ((p2[1] - p0[1]) * (xs - p2[0]) + (p0[0] - p2[0]) * (ys - p2[1])) / denominator
        w2 = 1.0 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue

        if piece.cutout is not None and uvs is not None:
            uv = (w0[..., None] * uvs[tri[0]] + w1[..., None] * uvs[tri[1]]
                  + w2[..., None] * uvs[tri[2]])
            height, width = piece.cutout.shape[:2]
            mx = np.clip((uv[..., 0] % 1.0) * (width - 1), 0, width - 1).astype(np.int32)
            my = np.clip((1.0 - uv[..., 1] % 1.0) * (height - 1), 0,
                         height - 1).astype(np.int32)
            inside = inside & (piece.cutout[my, mx] > CUTOUT_ALPHA)
            if not inside.any():
                continue

        depth = w0 * p0[2] + w1 * p1[2] + w2 * p2[2]
        region = depth_buffer[y0:y1 + 1, x0:x1 + 1]
        nearer = inside & (depth < region)
        if not nearer.any():
            continue

        # Lighting from the interpolated normal, so a curved surface reads curved
        # instead of faceted.
        if normals is not None:
            n = (w0[..., None] * normals[tri[0]] + w1[..., None] * normals[tri[1]]
                 + w2[..., None] * normals[tri[2]])
            length = np.linalg.norm(n, axis=-1, keepdims=True)
            n = n / np.where(length == 0, 1, length)
            lambert = np.abs(n @ LIGHT)
        else:
            lambert = np.full(w0.shape, 0.75, dtype=np.float32)
        shade = (0.30 + 0.70 * np.clip(lambert, 0, 1))[..., None]

        if texture is not None:
            uv = (w0[..., None] * uvs[tri[0]] + w1[..., None] * uvs[tri[1]]
                  + w2[..., None] * uvs[tri[2]])
            height, width = texture.shape[:2]
            # Unity's V axis points up, an image's down.
            tx = np.clip((uv[..., 0] % 1.0) * (width - 1), 0, width - 1).astype(np.int32)
            ty = np.clip((1.0 - uv[..., 1] % 1.0) * (height - 1), 0, height - 1).astype(np.int32)
            source = texture[ty, tx]
        else:
            source = np.broadcast_to(base, w0.shape + (3,))

        painted = source * shade
        target = colour_buffer[y0:y1 + 1, x0:x1 + 1]
        target[nearer] = painted[nearer]
        region[nearer] = depth[nearer]
