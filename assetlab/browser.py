"""Stage 7 - thumbnails plus a self-contained research browser.

The data is embedded directly into the HTML because browsers block fetch() from
file:// URLs; images stay as separate files, which load fine from disk. Open
``browser.html`` directly, no server needed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from PIL import Image

from .core import connect, load_rgba, make_thumbnail
from .objects import group_objects

AUDIO_EXT = {"ogg", "wav", "mp3", "m4a", "aac", "flac"}
FONT_EXT = {"ttf", "otf", "woff", "woff2"}
TEXT_EXT = {"json", "txt", "bytes", "xml", "csv", "cs", "shader", "cginc", "hlsl",
            "asset", "mat", "prefab", "unity", "anim", "controller", "spriteatlas",
            "shadervariants", "config"}
# Excerpts are inlined into the page, so both the per-file slice and the total are
# bounded; anything larger stays a link.
EXCERPT_CHARS = 1400
EXCERPT_MAX_BYTES = 64 * 1024
EXCERPT_BUDGET = 1_500_000
#: Width of the atlas copy kept beside the page. The outlines are drawn in
#: percentages, so scale costs detail and nothing else, and a sheet is looked at
#: to see where a sprite sits on it rather than to read the sprite.
SHEET_MAX = 1024


def save_sheet(source: Path, target: Path, size: int = SHEET_MAX) -> None:
    """A fitted copy of an atlas page, with no padding.

    The outlines over it are positioned in percentages of the image box, so the copy
    has to keep the sheet's own proportions exactly - a square letterboxed canvas
    would put every cut in the wrong place.
    """
    with load_rgba(source) as image:
        scale = min(1.0, size / max(1, image.width, image.height))
        view = image if scale == 1 else image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS)
        # Most of a sheet is transparent; JPEG has no alpha, so it is laid on the same
        # ground the panel draws behind it.
        canvas = Image.new("RGB", view.size, (14, 16, 20))
        canvas.paste(view, mask=view.getchannel("A"))
        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target, "JPEG", quality=82, optimize=True)


def media_kind(ext: str, has_image: bool) -> str | None:
    if has_image:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in FONT_EXT:
        return "font"
    if ext in TEXT_EXT:
        return "text"
    return None

#: The rig: sampling a clip's curves, composing a sprite onto its transform chain,
#: framing what it reaches and drawing it. Both pages had their own copy, 247 lines
#: identical to the line, and a one-line fix to one of them was silently absent from
#: the other until a measurement caught it. One copy now.
RIG_ENGINE = r"""let clipTimer = null, rigFrame = null;
function stopClip(){
  if (clipTimer) { clearTimeout(clipTimer); clipTimer = null; }
  if (rigFrame) { cancelAnimationFrame(rigFrame); rigFrame = null; }
}

// Transform curves are sampled linearly between keyframes. Unity eases with bezier
// tangents, so motion is approximated, not reproduced exactly.
function sampleCurve(keys, t){
  if (!keys || !keys.length) return null;
  if (t <= keys[0][0]) return keys[0].slice(1);
  for (let i = 1; i < keys.length; i++){
    if (t <= keys[i][0]){
      const a = keys[i-1], b = keys[i], span = b[0] - a[0];
      const f = span ? (t - a[0]) / span : 0;
      return [a[1] + (b[1]-a[1])*f, a[2] + (b[2]-a[2])*f, a[3] + (b[3]-a[3])*f];
    }
  }
  return keys[keys.length-1].slice(1);
}
function sampleScalar(keys, t){
  if (!keys || !keys.length) return null;
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++){
    if (t <= keys[i][0]){
      const a = keys[i-1], b = keys[i], span = b[0]-a[0], f = span ? (t-a[0])/span : 0;
      return a[1] + (b[1]-a[1])*f;
    }
  }
  return keys[keys.length-1][1];
}
// A rig carries alternate parts (three eyelids, a smoke puff) that the clip fades or
// toggles; without this they would all be drawn at once, at full opacity.
function layerStyle(layer, t){
  if (layer.off && !layer.wake) return "none";
  // Switching off a parent hides its whole subtree, so every toggle on the chain
  // counts, not just the one on the part itself.
  for (const curve of layer.acts || []) if (sampleScalar(curve, t) < 0.5) return "none";
  let a = layer.tint ?? 1;                     // renderer colour set in the prefab
  const alpha = sampleScalar(layer.alpha, t);
  if (alpha !== null) a *= Math.max(0, Math.min(1, alpha));
  return a >= 0.999 ? "" : String(a);
}
// Half-extents of a layer around its pivot. An off-centre pivot moves the art off
// the transform, and a flipped renderer mirrors it about that same pivot.
function layerBox(layer){
  const w = layer.size?.[0] || 64, h = layer.size?.[1] || 64;
  const ax = layer.anch ? layer.anch[0] : 0.5, ay = layer.anch ? layer.anch[1] : 0.5;
  let x0 = -ax*w, x1 = (1-ax)*w, y0 = -ay*h, y1 = (1-ay)*h;
  if (layer.flip & 1) { const s = x0; x0 = -x1; x1 = -s; }
  if (layer.flip & 2) { const s = y0; y0 = -y1; y1 = -s; }
  return [x0, y0, x1, y1];
}
// Sprite sizes are pre-scaled to this in the catalogue, so a build authored at 140
// pixels per unit still lines up with its unit-based transform offsets.
const PPU = 100;
function nodeTransform(node, t){
  const base = node.base, p = sampleCurve(node.pos, t), r = sampleCurve(node.rot, t),
        s = sampleCurve(node.scale, t);
  const x = (p ? p[0] : base[0]) * PPU, y = (p ? p[1] : base[1]) * PPU;
  // rot0 is the resting tilt baked into the prefab; a curve replaces it outright.
  const rot = r ? r[2] : (node.rot0 || 0);
  const sx = s ? s[0] : base[2], sy = s ? s[1] : base[3];
  // Unity's Y axis points up, the page's points down.
  return `translate(${x}px, ${-y}px) rotate(${-rot}deg) scale(${sx}, ${sy})`;
}
function poseRig(layers, nodes, masks){
  // Without this the layers sit unstyled on top of each other until play is pressed,
  // which looks like one meaningless still image.
  const stage = document.getElementById("rigstage");
  if (!stage) return;
  const t = +stage.dataset.poseAt || 0;
  [...stage.querySelectorAll("[data-layer]")].forEach((root, i) => {
    const style = layerStyle(layers[i], t);
    root.style.display = style === "none" ? "none" : "";
    root.style.opacity = style === "none" ? "" : style;
    [...root.querySelectorAll("[data-node]")].forEach((e, depth) => {
      e.style.transform = nodeTransform(nodes[layers[i].chain[depth]], t); });
  });
  applyMasks(stage, layers, nodes, masks, t);
  stage.style.transform = stageTransform(layers, nodes, t, +stage.dataset.fit, masks);
}
function playRig(layers, nodes, duration, masks){
  stopClip();
  const stage = document.getElementById("rigstage");
  const label = document.getElementById("clippos");
  if (!stage) return;
  const parts = [...stage.querySelectorAll("[data-layer]")].map(root => ({
    root, chain: [...root.querySelectorAll("[data-node]")] }));
  const started = performance.now(), fit = +stage.dataset.fit;
  const loop = () => {
    const t = duration ? ((performance.now() - started) / 1000) % duration : 0;
    parts.forEach((entry, i) => {
      const style = layerStyle(layers[i], t);
      entry.root.style.display = style === "none" ? "none" : "";
      entry.root.style.opacity = style === "none" ? "" : style;
      entry.chain.forEach((el, depth) => {
        el.style.transform = nodeTransform(nodes[layers[i].chain[depth]], t); });
    });
    applyMasks(stage, layers, nodes, masks, t);
    stage.style.transform = stageTransform(layers, nodes, t, fit, masks);
    if (label) label.textContent = `t=${t.toFixed(2)}s / ${(duration||0).toFixed(2)}s`;
    rigFrame = requestAnimationFrame(loop);
  };
  loop();
}
// 2D affine [a,b,c,d,e,f]; needed to measure where a rig actually reaches, since a
// rig's sprites and offsets are in the hundreds of pixels and would otherwise be
// cropped by the preview box.
function matMul(m, n){
  return [m[0]*n[0]+m[2]*n[1], m[1]*n[0]+m[3]*n[1],
          m[0]*n[2]+m[2]*n[3], m[1]*n[2]+m[3]*n[3],
          m[0]*n[4]+m[2]*n[5]+m[4], m[1]*n[4]+m[3]*n[5]+m[5]];
}
function nodeMatrix(node, t){
  const b = node.base, p = sampleCurve(node.pos, t), r = sampleCurve(node.rot, t),
        s = sampleCurve(node.scale, t);
  const x = (p ? p[0] : b[0]) * PPU, y = -(p ? p[1] : b[1]) * PPU;
  const rad = -(r ? r[2] : (node.rot0 || 0)) * Math.PI / 180;
  const sx = s ? s[0] : b[2], sy = s ? s[1] : b[3];
  const cos = Math.cos(rad), sin = Math.sin(rad);
  return [cos*sx, sin*sx, -sin*sy, cos*sy, x, y];
}
// Where the rig reaches at one instant.
// Axis-aligned box a chain+sprite occupies at one instant, in stage pixels.
function boxAt(entry, nodes, t){
  let m = [1,0,0,1,0,0];
  for (const i of entry.chain) m = matMul(m, nodeMatrix(nodes[i], t));
  const [x0, y0, x1, y1] = layerBox(entry);
  let a = 1e9, b = 1e9, c = -1e9, e = -1e9;
  for (const p of [[x0,y0],[x1,y0],[x0,y1],[x1,y1]]){
    const x = m[0]*p[0] + m[2]*p[1] + m[4], y = m[1]*p[0] + m[3]*p[1] + m[5];
    if (x < a) a = x; if (x > c) c = x;
    if (y < b) b = y; if (y > e) e = y;
  }
  return [a, b, c, e];
}
function rigBounds(layers, nodes, t, masks){
  let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9, any = false;
  for (const layer of layers){
    // A part that is hidden at this instant must not stretch the frame around it.
    if (layerStyle(layer, t) === "none") continue;
    let box = boxAt(layer, nodes, t);
    // Nor may one that is clipped: a slot machine's reel is far larger than the
    // window it shows through, and framing to the reel shrinks the machine away.
    if (layer.mk !== undefined && masks){
      const w = boxAt(masks[layer.mk], nodes, t);
      box = [Math.max(box[0], w[0]), Math.max(box[1], w[1]),
             Math.min(box[2], w[2]), Math.min(box[3], w[3])];
      if (box[2] <= box[0] || box[3] <= box[1]) continue;
    }
    any = true;
    if (box[0] < minX) minX = box[0]; if (box[2] > maxX) maxX = box[2];
    if (box[1] < minY) minY = box[1]; if (box[3] > maxY) maxY = box[3];
  }
  return any ? [minX, minY, maxX, maxY] : [0, 0, 1, 1];
}
// A SpriteMask shows its subtree only where the mask sprite is. The clip is applied
// as an axis-aligned window in stage space, with the contents shifted back by the
// same amount so the layers inside keep their own absolute placement.
function applyMasks(stage, layers, nodes, masks, t){
  if (!masks) return;
  for (const wrapper of stage.querySelectorAll("[data-mk]")){
    const box = boxAt(masks[+wrapper.dataset.mk], nodes, t);
    wrapper.style.left = box[0] + "px";
    wrapper.style.top = box[1] + "px";
    wrapper.style.width = Math.max(0, box[2] - box[0]) + "px";
    wrapper.style.height = Math.max(0, box[3] - box[1]) + "px";
    wrapper.firstElementChild.style.transform =
      `translate(${-box[0]}px, ${-box[1]}px)`;
  }
}
// Scale to the rig's own size, not to the distance it travels: a clip like a cart
// driving across the screen spans thousands of pixels while the cart itself is ~250,
// and fitting the journey would shrink it to a speck. The median instant is used so
// a moment where everything is collapsed or one piece is off-screen cannot set it.
// Also picks the moment to show when paused: a clip like "bee appears" has scale 0
// at t=0, so posing there would show an empty box.
function rigFit(layers, nodes, duration, masks){
  const boxes = [];
  for (let k = 0; k <= 12; k++){
    const t = duration ? duration * k / 12 : 0;
    const b = rigBounds(layers, nodes, t, masks);
    boxes.push({t, w: Math.max(1, b[2]-b[0]), h: Math.max(1, b[3]-b[1])});
  }
  boxes.sort((a, b) => a.w*a.h - b.w*b.h);
  const median = boxes[Math.floor(boxes.length / 2)];
  return {fit: Math.max(0.15, Math.min(330 / (median.w * 1.15), 290 / (median.h * 1.15), 1)),
          poseAt: median.t};
}
// The frame follows the subject, so a travelling rig stays visible throughout.
function stageTransform(layers, nodes, t, fit, masks){
  const b = rigBounds(layers, nodes, t, masks);
  return `scale(${fit}) translate(${-(b[0]+b[2])/2}px, ${-(b[1]+b[3])/2}px)`;
}
// A renderer multiplies its sprite by m_Color, and only a colour matrix reproduces
// that; a plain opacity would leave a black scrim white.
function tintFilters(layers){
  const seen = new Map();
  for (const layer of layers){
    if (!layer.rgb) continue;
    const key = layer.rgb.join(",");
    if (!seen.has(key)) seen.set(key, `rt${seen.size}`);
    layer._f = seen.get(key);
  }
  if (!seen.size) return "";
  const defs = [...seen].map(([key, id]) => {
    const [r, g, b] = key.split(",");
    return `<filter id="${id}" color-interpolation-filters="sRGB">
      <feColorMatrix type="matrix" values="${r} 0 0 0 0  0 ${g} 0 0 0
        0 0 ${b} 0 0  0 0 0 1 0"/></filter>`;
  }).join("");
  return `<svg width="0" height="0" style="position:absolute">${defs}</svg>`;
}
function rigStage(d){
  const {fit, poseAt} = rigFit(d.layers, d.nodes, d.clipdur, d.masks);
  const filters = tintFilters(d.layers);
  // Each layer nests one div per chain step so parent transforms compose naturally.
  const html = d.layers.map((layer, i) => {
    const open = layer.chain.map((_, depth) =>
      `<div data-node="${depth}" style="position:absolute;transform-origin:0 0">`).join("");
    const [w, h] = layer.size || [64, 64];
    const ax = layer.anch ? layer.anch[0] : 0.5, ay = layer.anch ? layer.anch[1] : 0.5;
    // transform-origin 0 0 makes the percentage translate land the sprite's pivot on
    // the transform, and puts the mirror axis through that pivot too.
    const flip = `scale(${layer.flip & 1 ? -1 : 1}, ${layer.flip & 2 ? -1 : 1})`;
    const box = `position:absolute;transform-origin:0 0;width:${w}px;height:${h}px;
      transform:${flip} translate(${-ax*100}%, ${-ay*100}%)`
      + (layer._f ? `;filter:url(#${layer._f})` : "");
    // A nine-sliced renderer keeps its corners at source size and stretches only the
    // middle, which is exactly what border-image does.
    const art = layer.bord
      ? `<div style="${box};border-style:solid;border-width:0;border-image:
           url(${layer.img}) ${layer.bord[0].join(" ")} fill /
           ${layer.bord[1].map(v => v + "px").join(" ")} / 0 stretch"></div>`
      : `<img src="${layer.img}" style="${box}">`;
    const body = `<div data-layer="${i}" style="position:absolute">` + open + art +
      "</div>".repeat(layer.chain.length + 1);
    return layer.mk === undefined ? body
      : `<div data-mk="${layer.mk}" style="position:absolute;overflow:hidden">
           <div style="position:absolute">${body}</div></div>`;
  }).join("");
  return `<div style="position:relative;height:300px;overflow:hidden;background:#0e1014;
    border-radius:8px">${filters}<div id="rigstage" data-fit="${fit.toFixed(5)}"
    data-pose-at="${poseAt.toFixed(4)}"
    style="position:absolute;left:50%;top:50%;transform-origin:0 0">${html}</div></div>`;
}
"""

#: The object, animation and obstacle-panel views, shared verbatim with the hub.
#: Both pages carry the same DATA shape and the same helpers around it, and the
#: two copies of this code had already drifted - the hub was still splitting an
#: obstacle with a word list after this file stopped. One copy keeps them together.
SHARED_VIEWS = r"""// ---- object view ----------------------------------------------------------
// One object is one thing on screen: the sprite of it whole, every sprite it is cut
// into, and the sheet all of those were packed on - shown together, because none of
// the three explains the art on its own. Which sprites belong to which object is
// decided in the catalogue, from the artists' own naming and from what the clips
// actually draw; see assetlab/objects.py for why those two and not a word list.
const OBJECTS = __OBJECTS__;
const OBJ_OF = new Map();
OBJECTS.forEach((o, i) => {
  if (o.w != null) OBJ_OF.set(o.w, i);
  for (const part of o.p) OBJ_OF.set(part, i);
});
function objMembers(o){
  const list = o.w != null ? [DATA[o.w]] : [];
  for (const part of o.p) list.push(DATA[part]);
  return list.filter(Boolean);
}
// A headless set - thirteen limbs and no sprite of the dragon - still needs a face
// for its card, and the largest piece is the least misleading one available.
function objHero(o){
  if (o.w != null) return DATA[o.w];
  return o.p.map(i => DATA[i]).filter(Boolean)
    .sort((a, b) => (b.w||0)*(b.h||0) - (a.w||0)*(a.h||0))[0] || null;
}
function objVisible(o){ return objMembers(o).some(match); }
// The hub shows several builds at once and a per-build page shows one, so the badge
// exists on the one page and not the other. Two builds ship a `coin`; without this
// their cards are indistinguishable on the page where that matters.
function fromBuild(d){
  return (typeof GAMEBADGE === "undefined" || !d) ? "" : GAMEBADGE[d.g] + " · ";
}
function objectCard(entry){
  const [o, i] = entry;
  const hero = objHero(o), members = objMembers(o);
  const rest = members.filter(d => d !== hero).slice(0, 6);
  const bits = rest.map(d =>
    `<img loading="lazy" src="${d.t || d.img}" title="${d.n}">`).join("");
  const parts = members.length - (o.w != null ? 1 : 0);
  return `<div class="card objcard" data-obj="${i}">
    <div class="objshots">${hero
      ? `<img loading="lazy" class="hero" src="${hero.t || hero.img}">` : ""}
      <div class="objbits">${bits}</div></div>
    <b>${o.n}</b><s>${fromBuild(hero)}${o.w != null ? "whole + " : ""}${parts} part${
      parts === 1 ? "" : "s"}${o.c != null ? " · animated" : ""}</s></div>`;
}
// The sheets, with this object's own cuts outlined on them. Usually one, but a build
// is free to split an object across pages and hiding the second would misstate where
// its art lives.
function atlasSection(o){
  const byPage = new Map();
  for (const d of objMembers(o)){
    if (!d.r || d.ax == null) continue;
    if (!byPage.has(d.ax)) byPage.set(d.ax, []);
    byPage.get(d.ax).push(d);
  }
  const note = text => `<p style="color:var(--dim);font-size:13px;margin:4px 0">${
    text}</p>`;
  if (!byPage.size)
    return `<h3>sprite atlas</h3>` +
      note("The catalogue records no sheet for these sprites.");
  // A sprite rotated into its slot occupies the transposed footprint.
  const box = d => { const [x, y, w, h, rot] = d.r;
                     return [x, y, rot ? h : w, rot ? w : h]; };
  // One build packs a helmet across seven sheets, six of which hold a single spark.
  // Drawing all seven turns the panel into a wall of atlases, so the sheets that
  // actually carry the object are drawn and the tail is counted rather than dropped.
  const ordered = [...byPage].sort((a, b) => b[1].length - a[1].length);
  const drawn = ordered.filter(([, cuts], rank) => rank < 4 && cuts.length > 1);
  const tail = ordered.length - drawn.length;
  const blocks = (drawn.length ? drawn : ordered.slice(0, 1)).map(([index, cuts]) => {
    const page = DATA[index];
    if (!page || !page.w || !page.h) return note("One sheet is missing from the catalogue.");
    // A page that cannot contain its own cuts is not the sheet they came from - some
    // exports point every sprite in a bundle at one texture id, and drawing the
    // outlines anyway would invent a layout that is not in the build.
    if (!cuts.every(d => { const [x, y, w, h] = box(d);
          return x >= 0 && y >= 0 && x + w <= page.w && y + h <= page.h; }))
      return note(`The export points ${cuts.length} of these sprites at
        <b>${page.n}</b> (${page.w}×${page.h}), which is too small to hold them,
        so there is no layout to draw.`);
    if (page.ac <= cuts.length && cuts.every(d => { const [x, y, w, h] = box(d);
          return x === 0 && y === 0 && w === page.w && h === page.h; }))
      return note(`${cuts.length === 1 ? "This piece is" : "These pieces are"} shipped
        as their own texture rather than packed onto a shared sheet.`);
    // Only the copy made beside the catalogue is drawn on. The full texture is a
    // file:// path a served page cannot load, and the square thumbnail next to it is
    // padded, so outlines over either would land somewhere they are not.
    if (!page.sheet)
      return note(`<b>${page.n}</b> (${page.w}×${page.h}) holds ${page.ac} sprites
        including ${cuts.length} of these, but no copy of the sheet was made beside
        this catalogue, so its layout cannot be drawn here.`);
    const marks = cuts.map(d => { const [x, y, w, h] = box(d);
      return `<i title="${d.n}" style="left:${(x/page.w*100).toFixed(3)}%;top:${
        ((page.h-y-h)/page.h*100).toFixed(3)}%;width:${(w/page.w*100).toFixed(3)}%;
        height:${(h/page.h*100).toFixed(3)}%"></i>`; }).join("");
    return `<p style="color:var(--dim);font-size:12px;margin:10px 0 4px">${page.n}
      · ${page.w}×${page.h} · ${page.ac} sprites packed, ${cuts.length} of
      them this object's</p>
      <div class="atlas"><img loading="lazy" src="${page.sheet}">${marks}</div>`;
  }).join("");
  const rest = tail > 0 && drawn.length
    ? `<p style="color:var(--dim);font-size:12px;margin:8px 0 0">${tail} further sheet${
        tail === 1 ? "" : "s"} hold${tail === 1 ? "s" : ""} the remaining ${
        ordered.slice(drawn.length).reduce((sum, [, cuts]) => sum + cuts.length, 0)}
        piece${ordered.slice(drawn.length).reduce((sum, [, cuts]) => sum + cuts.length, 0)
        === 1 ? "" : "s"}, one or two at a time.</p>` : "";
  return `<h3>sprite atlas <span>${byPage.size === 1 ? "the sheet it was cut from"
    : byPage.size + " sheets hold its art"}</span></h3>${blocks}${rest}`;
}
function clipControls(clip){
  return `<div style="display:flex;gap:8px;align-items:center;margin:6px 0 4px">
      <button class="close" onclick="playRig(DATA[${clip._i}].layers, DATA[${clip._i}].nodes,
        DATA[${clip._i}].clipdur, DATA[${clip._i}].masks)">play</button>
      <button class="close" onclick="stopClip()">stop</button>
      <small id="clippos">${clip.layers.length} layers · ${
        (clip.clipdur ?? 0).toFixed(2)}s · ${clip.n}</small></div>`;
}
function objectPanel(o){
  const members = objMembers(o), hero = objHero(o);
  const clip = objectClip(o);
  const figures = clip && clip.layers ? figureCount(clip) : 1;
  const how = clip ? "assembled from the clip that draws it"
            : o.w != null ? "the sprite the build ships whole"
            : "no assembled form in the build — largest piece shown";
  // Said rather than hidden: the clip is still the only assembled view there is, and
  // knowing it holds a crowd is what stops the picture from being read wrongly.
  const crowd = figures > 1
    ? `<p style="color:var(--dim);font-size:13px;margin:4px 0">This clip draws
       ${figures} separate figures; this object is one of them.</p>` : "";
  const woken = clip && clip.woken
    ? `<p style="color:var(--dim);font-size:13px;margin:4px 0">The prefab ships these
       parts switched off — the build turns them on at run time — so they are
       drawn here as authored rather than as an empty stage.</p>` : "";
  const final = clip && clip.layers ? crowd + woken + rigStage(clip) + clipControls(clip)
    : hero ? `<img src="${hero.img}" style="max-height:260px">`
           : `<p style="color:var(--dim)">nothing to show</p>`;
  return `<button class="close"
      onclick="stopClip();document.getElementById('panel').classList.remove('open')">close</button>
    <h2>${o.n}</h2>
    <h3>final form <span>${how}</span></h3>${final}
    ${atlasSection(o)}
    <h3>pieces <span>${members.length} sprite${
      members.length === 1 ? "" : "s"} cut for this object</span></h3>
    ${strip(members)}`;
}
// Which clip is a picture of this object. The catalogue notes one that draws it, but
// a build ships several of the same thing - appear, idle, tap, explode - and it noted
// whichever came first. That put `Koala_Appear` in front of the koala: it draws all
// twenty-six of its sprites and has four of them on screen at any instant, so the
// panel showed an almost empty stage where the idle would have shown the animal.
// Ranked here rather than in the catalogue because the curve sampler that decides
// what is actually on screen lives on this page.
// How much of itself a clip has on screen at the moment it would be shown.
function clipShows(clip){
  const {poseAt} = rigFit(clip.layers, clip.nodes, clip.clipdur, clip.masks);
  return clip.layers.filter(layer => layerStyle(layer, poseAt) !== "none").length;
}
// A prefab may ship its whole rig switched off. One build authors the koala with its
// body, head, ears and feet all inactive and turns them on from code when the block
// is placed, and honouring that flag faithfully renders an empty box for an animal
// the build plainly draws. Where a clip would show nothing at all, its parts are
// drawn as authored instead and the panel says which reading it is showing.
function wakeRig(clip){
  if (clip.woken !== undefined) return clip.woken;
  if (!clip.layers.some(layer => layer.off)) return (clip.woken = false);
  const asAuthored = clipShows(clip);
  for (const layer of clip.layers) layer.wake = true;
  const awake = clipShows(clip);
  // Only where the flag is what was hiding the thing. A clip that keeps most of its
  // parts off screen through its own curves - three eyelids, one blink - is doing
  // that on purpose, and overriding it would draw all three eyelids at once.
  if (asAuthored >= awake * 0.4){
    for (const layer of clip.layers) layer.wake = false;
    return (clip.woken = false);
  }
  return (clip.woken = true);
}
function before(a, b){
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return false;
}
function objectClip(o){
  if (o.c == null) return null;
  const mine = new Set(objMembers(o).map(d => d.img));
  const build = (objHero(o) || {}).g;
  let best = null, bestKey = null, fallback = null;
  for (const d of DATA){
    if (!d.layers || !d.layers.length) continue;
    if (build !== undefined && d.g !== build) continue;
    const drawn = new Set(d.layers.map(layer => layer.img));
    let covers = 0;
    for (const image of drawn) if (mine.has(image)) covers++;
    if (!covers) continue;
    // How much of the clip is this object. Two clips can draw all four of an
    // object's sprites while one of them is a lamp animation that happens to include
    // them; the one that is mostly about this object is the picture of it. Bucketed
    // so a rounding difference cannot outrank what the clip depicts.
    const share = Math.round(covers / drawn.size * 20);
    // A clip that has almost nothing on screen at rest is not a picture of anything,
    // whatever it is named and however much art it names - unless nothing it could
    // have chosen would show either, which is what wakeRig settles below.
    const shown = clipShows(d);
    if (shown < 2) { fallback = fallback && fallback.layers.length >= d.layers.length
                                ? fallback : d; continue; }
    // How much of the object it draws first, then what it depicts - an idle or a tap
    // over an explosion - then how much of itself it actually shows.
    const key = [-covers, -share, clipRank(d.n), -shown, -d.layers.length];
    if (!bestKey || before(key, bestKey)) { best = d; bestKey = key; }
  }
  const chosen = best || fallback || DATA[o.c];
  if (chosen && chosen.layers) wakeRig(chosen);
  return chosen;
}
// Whether the clip draws one thing or several. Sprites that belong to one object
// overlap - a dragon's head sits on its body - while three elves standing in a row
// share no pixels, and neither do a mouse and the rhino animated beside it.
function figureCount(clip){
  const t = rigFit(clip.layers, clip.nodes, clip.clipdur, clip.masks).poseAt;
  const drawn = clip.layers.filter(l => layerStyle(l, t) !== "none");
  const boxes = drawn.map(l => boxAt(l, clip.nodes, t));
  const owner = boxes.map((_, i) => i);
  const find = a => owner[a] === a ? a : (owner[a] = find(owner[a]));
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++){
      const a = boxes[i], b = boxes[j];
      if (a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]) owner[find(i)] = find(j);
    }
  const tally = new Map();
  for (let i = 0; i < boxes.length; i++){
    const root = find(i);
    tally.set(root, (tally.get(root) || 0) + 1);
  }
  const sizes = [...tally.values()].sort((a, b) => b - a);
  // A stray spark is not a second figure; a quarter of the art is.
  return sizes.filter(size => size >= Math.max(2, drawn.length * 0.25)).length;
}
function openObject(i){
  stopClip();
  const o = OBJECTS[i], panel = el("panel");
  panel.innerHTML = objectPanel(o);
  panel.classList.add("open", "wide");
  const clip = objectClip(o);
  if (clip && clip.layers) poseRig(clip.layers, clip.nodes, clip.masks);
}

// ---- animation view -------------------------------------------------------
// Controllers are the state machines that drive the clips. They have no picture
// either, so they belong here with them and not in front of the art.
const CLIP_TYPES = new Set(["AnimationClip", "AnimatorController",
                            "AnimatorOverrideController"]);
function isClip(d){ return CLIP_TYPES.has(d.type) || d.kind === "animation"; }
// The layer carrying the most art stands for a clip far better than its first layer,
// which is as often a shadow or a spark as it is the subject.
function clipThumb(d){
  if (d.fr) return d.t || d.fr[0][1];
  if (d.layers && d.layers.length)
    return d.layers.reduce((a, b) => ((b.size?.[0]||0)*(b.size?.[1]||0) >
      (a.size?.[0]||0)*(a.size?.[1]||0)) ? b : a).img;
  return d.t;
}
function clipCard(d){
  const src = clipThumb(d);
  const how = d.layers ? `${d.layers.length} layers`
            : d.fr ? `${d.fr.length} frames`
            : (d.curves || "no preview in this export");
  return `<div class="card" data-i="${d._i}">${src
      ? `<img loading="lazy" src="${src}">` : `<div class="ph">▶</div>`}
    <b>${d.n}</b><s>${fromBuild(d)}${how}${
      d.clipdur ? " · " + d.clipdur.toFixed(2) + "s" : ""}</s></div>`;
}

// The composed animation is the obstacle actually assembled - every sprite in place,
// which no single image in the set shows. Which clip matters: the obstacle at rest on
// the board, not mid-explosion, so clips are ranked by what they depict.
const CLIP_RANK = [/idle|loop/i, /tap|click|touch|press/i,
                   /appear|spawn|intro|create|enter/i, /hit|damage|shake|bounce/i];
function clipRank(name){
  const found = CLIP_RANK.findIndex(re => re.test(name));
  if (found >= 0) return found;
  return /explode|destroy|die|death|collect|disappear|leave|exit|end|win|fail/i
    .test(name) ? 9 : 5;
}
function familyRig(parts){
  const game = parts[0].g, mechanic = parts[0].mechanic;
  const clips = DATA.filter(d => d.layers && d.layers.length
                                 && d.mechanic === mechanic && d.g === game);
  if (!clips.length) return null;
  // A clip named "idle" is the obstacle at rest, but some builds ship idle variants
  // that animate one detached part. A clip carrying a small fraction of the art the
  // obstacle has is not a picture of the obstacle, whatever it is called.
  const most = Math.max(...clips.map(d => d.layers.length));
  const usable = clips.filter(d => d.layers.length >= most * 0.4);
  return usable.reduce((a, b) => {
    const ra = clipRank(a.n), rb = clipRank(b.n);
    return rb < ra || (rb === ra && b.layers.length > a.layers.length) ? b : a;
  });
}
// A family is a mechanic's whole vocabulary - Balloon is six colours of dog and six
// balloons, 85 sprites in all. Reporting that as "72 whole, 13 pieces" said nothing
// true about any of it; the objects inside are the unit worth counting.
function familyObjects(parts){
  const seen = new Set(), found = [];
  for (const d of parts){
    const i = OBJ_OF.get(d._i);
    if (i !== undefined && !seen.has(i)){ seen.add(i); found.push([OBJECTS[i], i]); }
  }
  found.sort((a, b) => b[0].p.length - a[0].p.length || a[0].n.localeCompare(b[0].n));
  // A mechanic's vocabulary is mostly loose art. One build tags 201 sprites `Items`
  // and 126 of them belong to nothing - twelve numbered snowballs and a blurred copy
  // of each. Listing those as 126 objects, every one with its own heading above a
  // strip holding a single picture, buries the fifteen things that actually come
  // apart and reads as a pile of unrelated art, which is what it is.
  const sets = found.filter(([o]) => objMembers(o).length > 1);
  const loose = found.filter(([o]) => objMembers(o).length === 1)
                     .map(([o]) => objMembers(o)[0]);
  return {sets, loose};
}
function familyCard(name, parts, i){
  const {sets, loose} = familyObjects(parts);
  const shots = (sets.length ? sets.map(([o]) => objHero(o)) : loose)
    .slice(0, 4).filter(Boolean)
    .map(hero => `<img loading="lazy" src="${hero.t || hero.img}">`).join("");
  return `<div class="card famcard" data-fam="${i}"><div class="famshots">${shots}</div>
          <b>${name}</b><s>${sets.length} object${sets.length === 1 ? "" : "s"}
          · ${parts.length} sprites</s></div>`;
}
function strip(list){
  return `<div class="partstrip">` + list.map(d =>
    `<figure><img src="${d.img}" title="${d.n}">
     <figcaption>${d.n}<br>${d.w||"?"}×${d.h||"?"}</figcaption></figure>`).join("")
    + `</div>`;
}
function familyPanel(name, parts){
  const {sets, loose} = familyObjects(parts);
  const mechanic = name.split(" · ").pop();
  const rig = familyRig(parts);
  if (rig && rig.layers) wakeRig(rig);
  const assembled = rig
    ? rigStage(rig) + clipControls(rig)
    : `<p style="color:var(--dim);font-size:13px;margin:4px 0">This one ships no rigged
       clip, so there is no assembled view — only the art below.</p>`;
  const blocks = sets.map(([o, i]) => {
    const members = objMembers(o);
    return `<div class="objblock" data-obj="${i}">
      <div class="objblockhead"><b>${o.n}</b><small>${members.length} sprites${
        o.w != null ? ", shipped whole" : ", pieces only"}${
        o.c != null ? ", animated" : ""} — click for its atlas</small></div>
      ${strip(members)}</div>`;
  }).join("");
  return `<button class="close"
      onclick="stopClip();document.getElementById('panel').classList.remove('open')">close</button>
    <h2>${name}</h2>
    <h3>final form <span>the obstacle as it sits on the board</span></h3>
    ${assembled}
    ${sets.length ? `<h3>objects <span>${sets.length} that come apart, of ${
        parts.length} sprites tagged ${mechanic}</span></h3>${blocks}` : ""}
    ${loose.length ? `<h3>loose sprites <span>${loose.length} tagged ${mechanic},
        belonging to no object here</span></h3>${strip(loose)}` : ""}`;
}
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#14161a;--panel:#1c2027;--line:#2c323d;--text:#eef1f5;--dim:#98a2b1;--accent:#6cb6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:12px 16px}
h1{margin:0 0 10px;font-size:16px;letter-spacing:.2px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select{background:#12151a;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:inherit}
input[type=search]{min-width:230px}
label.chk{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px}
#count{color:var(--dim);font-size:13px;margin-left:auto}
main{padding:14px 16px 60px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px;cursor:pointer;overflow:hidden}
.card:hover{border-color:var(--accent)}
.card img{width:100%;height:118px;object-fit:contain;background:#0e1014;border-radius:5px;display:block}
.card .ph{height:118px;background:#0e1014;border-radius:5px;display:flex;align-items:center;
 justify-content:center;color:#5c6675;font-size:30px}
.card b{display:block;font-weight:500;font-size:12px;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card s{display:block;text-decoration:none;color:var(--dim);font-size:11px}
aside{position:fixed;top:0;right:0;width:390px;max-width:92vw;height:100vh;background:var(--panel);
 border-left:1px solid var(--line);padding:18px;overflow:auto;transform:translateX(100%);transition:transform .16s;z-index:9}
aside.open{transform:none}
aside img{width:100%;object-fit:contain;background:#0e1014;border-radius:8px;max-height:340px}
/* Composed layers and piece sets must not inherit the single-preview framing: a
   per-image dark background paints a black box behind every sprite in the rig. */
#rigstage img{max-width:none;max-height:none;background:none;
 border-radius:0;object-fit:fill}
.pieces img{width:auto;max-width:100%;max-height:64px;background:none;border-radius:0;
 object-fit:contain}
aside h2{font-size:15px;margin:12px 0 4px;word-break:break-all}
aside dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px;margin:12px 0}
aside dt{color:var(--dim)}
aside dd{margin:0;word-break:break-all}
aside ul{margin:6px 0 0;padding-left:18px;font-size:13px;color:var(--dim)}
button.close{background:none;border:1px solid var(--line);color:var(--dim);border-radius:6px;padding:5px 10px;cursor:pointer}
.tag{display:inline-block;background:#252b34;border-radius:4px;padding:1px 7px;font-size:11px;color:var(--dim);margin:2px 3px 0 0}
.vtab{background:#12151a;color:var(--dim);border:1px solid var(--line);padding:7px 14px;
 cursor:pointer;font:inherit}
.vtab:first-child{border-radius:6px 0 0 6px}
.vtab:last-child{border-radius:0 6px 6px 0;border-left:0}
.vtab.on{background:var(--accent);color:#0d1117;border-color:var(--accent)}
aside h3{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);
 margin:16px 0 6px;font-weight:600}
/* A model has no picture of itself - what can be shown is the surface it is drawn
   with, so the swatch and the texture set stand in for a render. */
.swatch{display:inline-block;width:26px;height:26px;border-radius:5px;
 border:1px solid var(--line);vertical-align:middle;margin-right:8px}
.matrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:7px 0;
 border-bottom:1px solid var(--line)}
.matrow:last-child{border-bottom:0}
.matrow b{font-weight:500;font-size:12px}
.render{width:100%;aspect-ratio:1;background:#0e1014;border-radius:5px;
  object-fit:contain;display:block}
/* The grid these sit in is itself a grid, so both must span every column or they
   line up beside the cards instead of above them. */
.scenestrip{grid-column:1/-1;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin:0 0 22px}
.scenehead{grid-column:1/-1;font:600 13px/1.5 inherit;color:var(--dim);
  letter-spacing:.04em;text-transform:uppercase;margin:0 0 10px}
.maptex{margin:0;position:relative;max-width:46%;display:flex;flex-direction:column;
  align-items:center;justify-content:center}
.maptex img{max-width:100%;max-height:100%;opacity:.65}
.maptex figcaption{position:absolute;bottom:2px;font:600 9px/1.4 inherit;
  letter-spacing:.03em;color:#cfd6e2;background:#0009;border-radius:3px;padding:0 4px}
.modelshots{height:118px;background:#0e1014;border-radius:5px;display:flex;
 align-items:center;justify-content:center;gap:4px;padding:5px;overflow:hidden}
.modelshots img{width:auto;height:auto;max-width:46%;max-height:100%;
 object-fit:contain;background:none;border-radius:0}
.modelshots .flat{width:44px;height:44px;border-radius:8px;border:1px solid var(--line)}
aside h3 span{text-transform:none;letter-spacing:0;color:var(--dim);font-weight:400;
 margin-left:8px;font-size:12px}
/* An obstacle card shows several of its parts, so a set is recognisable as a set
   from the grid; a single hero image is indistinguishable from a plain asset. */
.famcard .famshots{height:118px;background:#0e1014;border-radius:5px;display:flex;
 flex-wrap:wrap;gap:3px;padding:4px;align-items:center;justify-content:center}
.famcard .famshots img{width:auto;height:auto;max-width:calc(50% - 4px);
 max-height:calc(50% - 4px);object-fit:contain;background:none;border-radius:0}
.famcard .famshots img:only-child{max-width:100%;max-height:100%}
/* Obstacle parts are laid out side by side at their own proportions - a corner strip
   and its body only read as one blocker when they sit next to each other. */
/* The obstacle panel opens wider than the asset panel: two parts per row is not a
   comparison, and these sets run to a few dozen pieces. */
aside.wide{width:min(900px,96vw)}
.partstrip{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;background:#0e1014;
 border-radius:8px;padding:12px}
.partstrip figure{margin:0;text-align:center;max-width:104px}
.partstrip img{display:block;width:auto;max-width:102px;max-height:84px;background:none;
 border-radius:0;object-fit:contain;margin:0 auto}
.partstrip figcaption{color:var(--dim);font-size:10px;margin-top:3px;word-break:break-all}
/* An object is a whole plus the parts it is cut into, so its card has to show both
   at once - one hero image alone is indistinguishable from a plain sprite. */
.objcard .objshots{height:118px;background:#0e1014;border-radius:5px;display:flex;
 align-items:center;gap:5px;padding:5px;overflow:hidden}
.objcard .objshots .hero{max-width:58%;max-height:100%;width:auto;height:auto;
 object-fit:contain;background:none;border-radius:0}
.objcard .objbits{display:flex;flex-wrap:wrap;gap:3px;align-items:center;
 justify-content:center;flex:1;max-height:100%;overflow:hidden}
.objcard .objbits img{width:auto;height:auto;max-width:44px;max-height:34px;
 object-fit:contain;background:none;border-radius:0}
/* The sheet with this object's own cuts drawn on it. It is the one view that says
   which art the studio chose to pack together, and where this object sits in it. */
.atlas{position:relative;display:inline-block;max-width:100%;line-height:0;
 background:#0e1014;border-radius:8px}
.atlas img{display:block;width:auto;max-width:100%;max-height:420px;background:none;
 border-radius:8px}
/* A cut is a few percent of a sheet, so at preview size the mark has to carry
   further than the art around it. */
.atlas i{position:absolute;border:2px solid var(--accent);border-radius:2px;
 box-shadow:0 0 7px 2px #6cb6ffcc,0 0 0 1px #000a inset;pointer-events:none}
.objblock{border:1px solid var(--line);border-radius:8px;padding:10px;margin:0 0 10px;
 cursor:pointer}
.objblock:hover{border-color:var(--accent)}
.objblockhead{display:flex;align-items:baseline;gap:10px;margin:0 0 8px}
.objblockhead small{color:var(--dim);font-size:11px}
</style></head><body>
<header>
  <h1>__TITLE__ <span style="color:var(--dim);font-weight:400">— asset research library</span></h1>
  <div class="controls">
    <span><button class="vtab on" data-view="assets">assets</button><button
      class="vtab" data-view="objects" id="objectstab">objects</button><button
      class="vtab" data-view="animations" id="animtab">animations</button><button
      class="vtab" data-view="obstacles">obstacles</button><button
      class="vtab" data-view="models" id="modelstab">models</button></span>
    <input type="search" id="q" placeholder="search name / path…">
    <select id="role"></select><select id="feature"></select>
    <select id="mechanic"></select><select id="type"></select>
    <select id="size">
      <option value="">any size</option><option value="0-63">&lt; 64 px</option>
      <option value="64-159">64–159 px</option><option value="160-511">160–511 px</option>
      <option value="512-99999">512 px +</option>
    </select>
    <label class="chk"><input type="checkbox" id="dups"> duplicates only</label>
    <label class="chk"><input type="checkbox" id="imgs" checked> previewable only</label>
    <label class="chk"><input type="checkbox" id="atlas" checked> hide atlas sheets</label>
    <label class="chk"><input type="checkbox" id="eng" checked> hide engine assets</label>
    <span id="count"></span>
  </div>
  <div id="profile" style="color:var(--dim);font-size:12px;margin-top:7px"></div>
</header>
<main><div class="grid" id="grid"></div></main>
<aside id="panel"></aside>
<script>
const DATA = __DATA__;
const F = ["role","feature","mechanic","type"];
const el = id => document.getElementById(id);

// A facet count must describe the rows the grid would actually show, so it is
// measured against every other active filter - "images only" above all, which is on
// by default and hides materials, scripts and other imageless assets.
function passes(d, skipKey){
  // Clips have a tab of their own. Keeping them in the asset grid as well buries the
  // art behind several hundred cards that are not pictures of anything, and makes
  // every facet count promise rows the grid does not show.
  if (VIEW === "assets" && isClip(d)) return false;
  if (VIEW === "animations" && !isClip(d)) return false;
  // An atlas sheet is the page its sprites were cut from, not a piece of art. In a
  // build with thousands of sprites they vanish into the grid; in one with a few
  // hundred they are a third of what you see.
  if (el("atlas").checked && d.at) return false;
  // Unity and its packages ship art of their own - dither tables, the Rendering
  // Debugger's widgets - which lands in the same folders as the studio's and sorts
  // to the front because it is tiny. It is in the catalogue, just not in the way.
  if (el("eng").checked && d.eng) return false;
  if (el("imgs").checked && !d.kind) return false;
  if (el("dups").checked && !d.dup) return false;
  for (const k of F) { if (k === skipKey) continue;
                       const v = el(k).value; if (v && d[k] !== v) return false; }
  const s = el("size").value;
  if (s){ const [lo,hi] = s.split("-").map(Number); const m = Math.max(d.w||0, d.h||0);
          if (!(m >= lo && m <= hi)) return false; }
  const q = el("q").value.trim().toLowerCase();
  if (q && !(d.n.toLowerCase().includes(q) || d.p.toLowerCase().includes(q))) return false;
  return true;
}
const match = d => passes(d, null);
DATA.forEach((d, i) => { d._i = i; });   // stable id; indexOf per card is O(n)

function refreshFacets(){
  for (const key of F){
    const select = el(key), current = select.value, counts = {};
    for (const d of DATA) { if (passes(d, key)) { const v = d[key];
                            if (v) counts[v] = (counts[v]||0)+1; } }
    if (current && !(current in counts)) counts[current] = 0;   // keep the selection
    const opts = Object.entries(counts).sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0]))
      .map(([v,n]) => `<option value="${v}">${v} (${n})</option>`).join("");
    select.innerHTML = `<option value="">all ${key}s</option>` + opts;
    select.value = current;
  }
}

let shown = 0, filtered = [];
const GLYPH = {audio:"♪", font:"A", text:"{ }", animation:"▶"};
function card(d, i){
  const thumb = d.t ? `<img loading="lazy" src="${d.t}">`
    : `<div class="ph">${GLYPH[d.kind] || "·"}</div>`;
  const sub = d.kind === "audio" ? (d.dur ? d.dur.toFixed(2) + "s" : "audio")
            : d.kind === "animation"
              ? (d.fr ? `▶ ${d.fr.length} frames` : `▶ ${d.layers.length} layers`)
            : d.w ? d.w + "×" + d.h : d.type;
  return `<div class="card" data-i="${i}">${thumb}<b>${d.n}</b><s>${sub}</s></div>`;
}

__RIG_ENGINE__function playClip(frames, duration){
  stopClip();
  const img = document.getElementById("clipframe");
  const label = document.getElementById("clippos");
  if (!img) return;
  let i = 0;
  const step = () => {
    img.src = frames[i][1];
    if (label) label.textContent = `${i + 1}/${frames.length}  t=${frames[i][0]}s`;
    const next = (i + 1) % frames.length;
    // Hold each frame for its own gap; wrap using the clip's total duration.
    const gap = next === 0 ? Math.max(0.03, (duration || 0) - frames[i][0])
                           : frames[next][0] - frames[i][0];
    i = next;
    clipTimer = setTimeout(step, Math.max(16, gap * 1000));
  };
  step();
}

// Edge and corner pieces laid out where they belong, so a 1217x60 strip is readable
// as the top of an obstacle instead of an unexplained sliver.
const CELL_ORDER = ["tl","t","tr","l","c","r","bl","b","br"];
function pieceSet(d){
  const byPos = {};
  for (const piece of d.pg) (byPos[piece.pos] ||= []).push(piece);
  const cells = CELL_ORDER.map(pos => {
    const list = byPos[pos];
    if (!list) return `<div></div>`;
    const imgs = list.map(p =>
      `<img src="${p.img}" title="${p.n} (${p.w}×${p.h})">`).join("");
    return `<div style="display:flex;gap:3px;flex-wrap:wrap;align-items:center;
      justify-content:center;min-height:24px">${imgs}</div>`;
  }).join("");
  const used = CELL_ORDER.filter(pos => byPos[pos]).join(" ");
  return `<div class="pieces" style="display:grid;grid-template-columns:1fr 1fr 1fr;
      gap:4px;background:#0e1014;border-radius:8px;padding:8px">${cells}</div>
    <small style="color:var(--dim)">${d.pg.length} pieces assembled by position
      (${used})</small>`;
}
function preview(d){
  if (d.kind === "animation"){
    const controls = (call, info) => `
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
        <button class="close" onclick="${call}">play</button>
        <button class="close" onclick="stopClip()">stop</button>
        <small id="clippos">${info}</small></div>`;
    if (d.fr)
      return `<img id="clipframe" src="${d.fr[0][1]}">` +
        controls(`playClip(DATA[${d._i}].fr, DATA[${d._i}].clipdur)`,
                 `${d.fr.length} frames · ${d.clipdur ?? "?"}s`);
    return rigStage(d) +
      controls(`playRig(DATA[${d._i}].layers, DATA[${d._i}].nodes, DATA[${d._i}].clipdur, DATA[${d._i}].masks)`,
               `${d.layers.length} layers · ${(d.clipdur ?? 0).toFixed(2)}s`);
  }
  if (d.pg && d.pg.length > 1) return pieceSet(d);
  if (d.kind === "image") return `<img src="${d.img}">`;
  if (d.kind === "audio")
    return `<audio controls preload="none" style="width:100%" src="${d.href}"></audio>`;
  if (d.kind === "font")
    // Unity ships display type as TextMeshPro SDF assets, not .ttf, so a font file
    // here is often an engine fallback shared by many games - compare the byte size.
    return `<style>@font-face{font-family:"pv${d._i}";src:url("${d.href}")}</style>
            <div style="font-family:'pv${d._i}',serif;font-size:24px;line-height:1.35;
                        background:#0e1014;border-radius:8px;padding:12px">
              ABCDEFG abcdefg 0123456789<br>Şeker Ğüzel İçki Öykü</div>
            <small style="color:var(--dim)">${(d.b||0).toLocaleString()} bytes — identical
            sizes across games mean the same fallback file, not the game's own type</small>`;
  if (d.kind === "text" && d.ex)
    return `<pre style="white-space:pre-wrap;word-break:break-word;max-height:340px;
                        overflow:auto;background:#0e1014;border-radius:8px;padding:10px;
                        font-size:12px;margin:0">${d.ex.replace(/[<&]/g,
                        c => c === "<" ? "&lt;" : "&amp;")}</pre>`;
  return "";
}

// ---- model view -----------------------------------------------------------
// A 3D build has no sprite to show for its art. What it does have is a mesh drawn
// through materials, and a material is either a set of textures or a flat colour -
// in the build this was written for, half the models are colour and no texture at
// all. Both are rendered here, because showing only textures would show half.
const MODELS = __MODELS__;
const SCENES = __SCENES__;
const PROFILE = __PROFILE__;

function swatch(colour){
  return colour ? `<span class="swatch" style="background:${colour.hex}"
    title="${colour.hex}"></span>` : "";
}
// A model's own picture beats anything its materials can show. The texture strip
// stays as the fallback for geometry the renderer could not read - a mesh whose data
// lives in an external .resS, say - because half a card is better than none.
// BaseMap/MainTex is what a surface looks like; a normal, mask or roughness map
// describes how it reacts to light and reads as a flat wash of colour. Showing one
// as a model's face is how a lavender square comes to stand for a building.
const ALBEDO_SLOTS = ["MainTex", "BaseMap", "BaseColorMap", "MainTexture", "Albedo"];
function isAlbedo(texture){
  return ALBEDO_SLOTS.includes(String(texture.slot || "").replace(/^_/, ""));
}
function materialStrip(model){
  const shots = [];
  for (const material of model.materials){
    const maps = (material.textures || []).slice();
    maps.sort((a, b) => (isAlbedo(b) ? 1 : 0) - (isAlbedo(a) ? 1 : 0));
    // Without an albedo the material's own colour is the honest answer, so it
    // leads and the maps follow it as what they are.
    if (!maps.some(isAlbedo) && material.colour)
      shots.push(`<div class="flat" style="background:${material.colour.hex}"
        title="${material.name} base colour"></div>`);
    for (const texture of maps.slice(0, 2))
      shots.push(isAlbedo(texture)
        ? `<img loading="lazy" src="${texture.img}" title="${texture.name}">`
        : `<figure class="maptex"><img loading="lazy" src="${texture.img}"
             title="${texture.name}"><figcaption>${
             String(texture.slot).replace(/^_/, "")}</figcaption></figure>`);
    if (!maps.length && material.colour)
      shots.push(`<div class="flat" style="background:${material.colour.hex}"></div>`);
  }
  return `<div class="modelshots">${shots.slice(0, 4).join("") ||
    `<span style="color:#5c6675">no material</span>`}</div>`;
}
function modelCard(model, i){
  const body = model.render
    ? `<img loading="lazy" class="render" src="${model.render}" alt="">`
    : materialStrip(model);
  const label = model.mesh_name || model.object_name || "(unnamed)";
  const tris = model.tris ? `${model.tris.toLocaleString()} tris · ` : "";
  return `<div class="card" data-model="${i}">${body}
    <b>${label}</b><s>${model.skinned ? "rigged · " : ""}${tris}${model.materials.length}
    material${model.materials.length === 1 ? "" : "s"}</s></div>`;
}
function sceneCard(scene, i){
  return `<div class="card" data-scene="${i}">
    <img loading="lazy" class="render" src="${scene.render}" alt="">
    <b>${scene.name}</b><s>${scene.parts} parts · ${scene.tris.toLocaleString()} tris</s></div>`;
}
function scenePanel(scene){
  const parts = MODELS.filter(m => m.prefab_id === scene.id);
  const strip = parts.map(m => `<figure>${m.render
      ? `<img src="${m.render}">` : ""}<figcaption>${m.mesh_name
      || m.object_name || "?"}</figcaption></figure>`).join("");
  return `<button class="close"
      onclick="document.getElementById('panel').classList.remove('open')">close</button>
    <h2>${scene.name}</h2>
    <img class="render" style="max-width:420px" src="${scene.render}">
    <p style="color:var(--dim)">${scene.parts} parts, ${scene.tris.toLocaleString()}
      triangles, assembled from the prefab's own transforms.</p>
    <div class="partstrip">${strip}</div>`;
}
function modelPanel(model){
  const rows = model.materials.map(material => {
    const textures = material.textures.map(texture =>
      `<figure><img src="${texture.img}" title="${texture.name}">
       <figcaption>${texture.slot}<br>${texture.name}</figcaption></figure>`).join("");
    return `<div class="matrow">${swatch(material.colour)}<b>${material.name}</b>
      <small style="color:var(--dim)">${material.textures.length
        ? material.textures.length + " texture" + (material.textures.length === 1 ? "" : "s")
        : "flat colour, no texture"}</small></div>
      ${textures ? `<div class="partstrip">${textures}</div>` : ""}`;
  }).join("");
  return `<button class="close"
      onclick="stopClip();document.getElementById('panel').classList.remove('open')">close</button>
    <h2>${model.mesh_name || model.object_name || "(unnamed)"}</h2>
    ${model.render ? `<img class="render" style="max-width:340px"
      src="${model.render}">` : ""}
    <h3>mesh <span>${model.skinned ? "rigged geometry" : "static geometry"}${
      model.mesh_bytes ? " · " + Math.round(model.mesh_bytes / 1024) + " KB" : ""}</span></h3>
    <p style="color:var(--dim);font-size:13px;margin:4px 0">Geometry is not rendered
      here. What is shown is the surface it is drawn with.</p>
    <h3>materials <span>${model.materials.length}</span></h3>${rows}
    <dl><dt>prefab</dt><dd>${model.prefab_name}</dd>
        <dt>path</dt><dd>${model.path || "(root)"}</dd></dl>`;
}

// ---- obstacle grouping ----------------------------------------------------
// The design enums name each blocker, so its art is exactly the tagged assets that
// carry that name; grouping on it is what turns 800 loose sprites into ~40 obstacles.
// Only the key differs between this page and the other, so only the key lives here -
// everything drawn from it comes from SHARED_VIEWS below.
let VIEW = "assets";
function familyKey(d){ return (d.mechanic || d.feature || "unlabelled"); }
function families(){
  const groups = new Map();
  for (const d of DATA){
    if (!d.obstacle || !d.img || !match(d)) continue;
    const key = familyKey(d);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(d);
  }
  for (const list of groups.values()) list.sort((a, b) => a.n.localeCompare(b.n));
  return [...groups].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
}
__SHARED_VIEWS__let FAMILIES = [], PAGED = "asset";
// Three of the five views scroll a flat list, and only the card differs.
const CARD = {asset: d => card(d, d._i), animation: clipCard, object: objectCard};
const COUNTED = {asset: "assets", animation: "clips", object: "objects"};
function render(reset){
  if (!reset) { if (VIEW === "obstacles") return; }
  else {
    refreshFacets();
    el("grid").innerHTML = "";
    shown = 0;
    if (VIEW === "obstacles"){
      FAMILIES = families();
      el("grid").innerHTML = FAMILIES.map(([n, p], i) => familyCard(n, p, i)).join("");
      el("count").textContent = `${FAMILIES.length} obstacles, ` +
        `${FAMILIES.reduce((s, f) => s + f[1].length, 0)} parts`;
      return;
    }
    if (VIEW === "models"){
      const query = el("q").value.trim().toLowerCase();
      // One row per placement is the right shape for the database and the wrong one
      // for a gallery: a chair leg used four times is one mesh to look at.
      const seen = new Set();
      const shown = MODELS.map((m, i) => [m, i]).filter(([m]) => {
        if (query && !((m.mesh_name || "").toLowerCase().includes(query) ||
            (m.object_name || "").toLowerCase().includes(query) ||
            (m.prefab_name || "").toLowerCase().includes(query))) return false;
        const key = m.mesh_name || m.object_name;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      const scenes = SCENES.filter(s => !query ||
        s.name.toLowerCase().includes(query));
      const assembled = scenes.length && !query
        ? `<div class="scenehead">assembled prefabs</div>
           <div class="scenestrip">${scenes.map(sceneCard).join("")}</div>
           <div class="scenehead">individual meshes</div>` : "";
      el("grid").innerHTML = assembled +
        `<div class="scenestrip">${shown.map(([m, i]) => modelCard(m, i)).join("")}</div>`;
      el("count").textContent = `${shown.length} models, ` +
        `${shown.filter(([m]) => m.skinned).length} rigged, ${scenes.length} assembled`;
      return;
    }
    if (VIEW === "objects"){
      PAGED = "object";
      // Most objects are one sprite that owns nothing, which the assets tab already
      // shows. What this tab is for is the ones that come apart, so they lead - and
      // ranking across builds rather than within each keeps the comparison.
      filtered = OBJECTS.map((o, i) => [o, i]).filter(([o]) => objVisible(o))
        .sort((a, b) => b[0].p.length - a[0].p.length ||
              (b[0].c != null) - (a[0].c != null) || a[0].n.localeCompare(b[0].n));
    } else if (VIEW === "animations"){
      PAGED = "animation";
      // Rigs first and the fullest of them at the front: a clip that composes thirty
      // sprites is the one worth watching, and a curve-only clip has nothing to show.
      filtered = DATA.filter(match).sort((a, b) =>
        (b.layers ? b.layers.length : b.fr ? 1 : 0) -
        (a.layers ? a.layers.length : a.fr ? 1 : 0) || a.n.localeCompare(b.n));
    } else {
      PAGED = "asset";
      filtered = DATA.filter(match);
    }
  }
  const slice = filtered.slice(shown, shown + 300);
  el("grid").insertAdjacentHTML("beforeend", slice.map(CARD[PAGED]).join(""));
  shown += slice.length;
  el("count").textContent = `${filtered.length} ${COUNTED[PAGED]}` +
    (shown < filtered.length ? ` (showing ${shown})` : "");
}
["q","role","feature","mechanic","type","size","dups","imgs","atlas","eng"].forEach(id =>
  el(id).addEventListener(id === "q" ? "input" : "change", () => render(true)));
addEventListener("scroll", () => {
  if (shown < filtered.length && innerHeight + scrollY > document.body.offsetHeight - 700) render(false);
});
document.querySelectorAll(".vtab").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".vtab").forEach(b => b.classList.toggle("on", b === tab));
  VIEW = tab.dataset.view; render(true);
}));
el("grid").addEventListener("click", e => {
  const scene = e.target.closest("[data-scene]");
  if (scene){
    stopClip();
    const panel = el("panel");
    panel.classList.remove("wide");
    panel.innerHTML = scenePanel(SCENES[+scene.dataset.scene]);
    panel.classList.add("open");
    return;
  }
  const model = e.target.closest("[data-model]");
  if (model){
    stopClip();
    const panel = el("panel");
    panel.classList.remove("wide");
    panel.innerHTML = modelPanel(MODELS[+model.dataset.model]);
    panel.classList.add("open");
    return;
  }
  const object = e.target.closest("[data-obj]");
  if (object){ openObject(+object.dataset.obj); return; }
  const group = e.target.closest("[data-fam]");
  if (group){
    stopClip();
    const [name, parts] = FAMILIES[+group.dataset.fam];
    const panel = el("panel");
    panel.innerHTML = familyPanel(name, parts);
    panel.classList.add("open", "wide");
    const rig = familyRig(parts);
    if (rig) poseRig(rig.layers, rig.nodes, rig.masks);
    return;
  }
  const node = e.target.closest(".card"); if (!node) return;
  stopClip();
  el("panel").classList.remove("wide");
  const d = DATA[+node.dataset.i], panel = el("panel");
  panel.innerHTML = `<button class="close" onclick="document.getElementById('panel').classList.remove('open')">close</button>
    ${preview(d)}<h2>${d.n}</h2>
    <div>${[d.role,d.feature,d.mechanic,d.obstacle?"OBSTACLE":"",d.sub,d.dup?"duplicate":""]
              .filter(Boolean).map(t => `<span class="tag">${t}</span>`).join("")}</div>
    <dl><dt>type</dt><dd>${d.type}</dd><dt>size</dt><dd>${d.w?d.w+" × "+d.h:"—"}</dd>
        ${d.kind === "audio" ? `<dt>audio</dt><dd>${d.dur?d.dur+"s ":""}${d.rate?d.rate+" Hz ":""}${d.ch?d.ch+"ch":""}</dd>` : ""}
        ${d.curves ? `<dt>curves</dt><dd>${d.curves}</dd>` : ""}
        <dt>bytes</dt><dd>${(d.b||0).toLocaleString()}</dd><dt>path</dt><dd>${d.p}</dd></dl>
    ${d.href && d.kind !== "image" ? `<a href="${d.href}" target="_blank" class="tag">open file</a>` : ""}
    ${d.u && d.u.length ? `<div style="color:var(--dim);font-size:13px">used by ${d.u.length} prefab/scene:</div>
      <ul>${d.u.map(u => `<li>${u}</li>`).join("")}</ul>` : ""}`;
  panel.classList.add("open");
  if (d.kind === "animation" && d.layers) poseRig(d.layers, d.nodes, d.masks);
});
el("panel").addEventListener("click", e => {
  const block = e.target.closest("[data-obj]");
  if (block) openObject(+block.dataset.obj);
});
// A tab only exists when there is something behind it: a 2D build has no mesh chain
// and a build with no clips has no rigs, and an empty view is worse than an absent one.
if (!MODELS.length) el("modelstab").style.display = "none";
if (!OBJECTS.length) el("objectstab").style.display = "none";
if (!DATA.some(isClip)) el("animtab").style.display = "none";
if (PROFILE) el("profile").textContent =
  `${PROFILE.verdict.toUpperCase()} build - ${PROFILE.note}`;
render(true);
</script></body></html>
"""


def build(out_dir: Path, assets_root: Path, title: str, conn: sqlite3.Connection) -> dict[str, int]:
    thumbs_dir = out_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    usage: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(
        "SELECT asset_id, holder_name FROM used_by WHERE holder_name IS NOT NULL"):
        if len(usage[row["asset_id"]]) < 25:
            usage[row["asset_id"]].append(row["holder_name"])

    subcategory = {
        row["asset_id"]: row["value"]
        for row in conn.execute("SELECT asset_id, value FROM tags WHERE kind='subcategory'")
    }
    obstacles = {
        row[0] for row in conn.execute(
            "SELECT asset_id FROM tags WHERE kind='category' AND value='Obstacle'")
    }
    # The pages sprites were cut from. They are the largest images in the catalogue,
    # so an obstacle set that keeps them shows its atlas sheet instead of its art.
    atlas_pages: set[str] = set()
    # Where each sprite was cut from. The page is worth showing beside the pieces -
    # it is the one view that says which art the studio chose to pack together.
    sprite_rect: dict[int, tuple] = {}
    packed: dict[str, int] = {}
    rect_columns = {row[1] for row in conn.execute("PRAGMA table_info(sprites)")}
    rotated = "rotated" if "rotated" in rect_columns else "0 AS rotated"
    for row in conn.execute(
        f"SELECT asset_id, atlas_guid, x, y, w, h, {rotated} FROM sprites "
        "WHERE atlas_guid IS NOT NULL"):
        atlas_pages.add(row["atlas_guid"])
        packed[row["atlas_guid"]] = packed.get(row["atlas_guid"], 0) + 1
        sprite_rect[row["asset_id"]] = (row["atlas_guid"], row["x"], row["y"],
                                        row["w"], row["h"], row["rotated"] or 0)
    # Multi-part obstacle art only reads assembled, so every member of a piece set
    # carries the whole set with it.
    piece_sets: dict[str, list[dict]] = defaultdict(list)
    piece_key: dict[int, str] = {}
    try:
        for row in conn.execute(
            """SELECT p.group_key, p.position, a.id, a.name, a.image_path, a.width, a.height
                 FROM piece_groups p JOIN assets a ON a.id = p.asset_id
                WHERE a.image_path IS NOT NULL
                ORDER BY p.group_key, p.position, a.name"""):
            piece_key[row["id"]] = row["group_key"]
            piece_sets[row["group_key"]].append({
                "n": row["name"], "pos": row["position"],
                "img": (row["image_path"] if row["image_path"].startswith("sprites/")
                        else (assets_root / row["image_path"]).as_uri()),
                "w": row["width"], "h": row["height"]})
    except sqlite3.OperationalError:
        pass

    animations: dict[int, dict] = {}
    try:
        for row in conn.execute(
            """SELECT asset_id, frames, layers, nodes, masks, duration, frame_count,
                      curve_summary FROM animations"""):
            animations[row["asset_id"]] = {
                "frames": json.loads(row["frames"]) if row["frames"] else None,
                "layers": json.loads(row["layers"]) if row["layers"] else None,
                "nodes": json.loads(row["nodes"]) if row["nodes"] else None,
                "masks": json.loads(row["masks"]) if row["masks"] else None,
                "duration": row["duration"], "count": row["frame_count"],
                "curves": row["curve_summary"],
            }
    except sqlite3.OperationalError:
        pass   # animations stage not run for this catalog

    # Art first. Sorted by type alone the grid opens on AnimationClip and
    # AnimatorController - a screen of empty placeholders - and a build's 5,000
    # pictures sit behind 9,000 rows that have nothing to show.
    rows = conn.execute(
        """SELECT id, guid, name, rel_path, unity_type, ext, width, height, size_bytes,
                  image_path, duplicate_group, primary_role, primary_feature,
                  primary_mechanic, duration_seconds, sample_rate, channels, origin
             FROM assets
            ORDER BY (image_path IS NULL), unity_type, name"""
    ).fetchall()

    records, made = [], 0
    row_guid: list[str | None] = []
    row_id: list[int] = []
    row_image: list[str | None] = []
    excerpt_budget = EXCERPT_BUDGET
    for row in rows:
        thumb = None
        if row["image_path"]:
            source = out_dir / row["image_path"] if row["image_path"].startswith("sprites/") \
                else assets_root / row["image_path"]
            target = thumbs_dir / f"{row['id']}.jpg"
            if not target.exists():
                try:
                    with load_rgba(source) as image:
                        make_thumbnail(image, target)
                    made += 1
                except (OSError, ValueError):
                    target = None
            if target and target.exists():
                thumb = f"thumbs/{row['id']}.jpg"
        image_href = None
        if row["image_path"]:
            image_href = (row["image_path"] if row["image_path"].startswith("sprites/")
                          else (assets_root / row["image_path"]).as_uri())

        ext = (row["ext"] or "").lower()
        kind = media_kind(ext, bool(row["image_path"]))
        clip = animations.get(row["id"])
        if clip and (clip["frames"] or clip["layers"]):
            # Watchable either as a frame sequence or as a transform-driven rig; the
            # first sprite stands in as the thumbnail.
            kind = "animation"
            thumb = thumb or (clip["frames"][0][1] if clip["frames"]
                              else clip["layers"][0]["img"])
        source = assets_root / row["rel_path"]
        href = image_href
        if kind in {"audio", "font"}:
            # Copied next to the page so playback works both from file:// and from a
            # local server; a file:// src is blocked when the page is served over http.
            target = media_dir / f"{row['id']}.{ext}"
            if not target.exists():
                try:
                    shutil.copyfile(source, target)
                except OSError:
                    target = None
            href = f"media/{target.name}" if target and target.exists() else source.as_uri()
        elif kind == "text":
            href = source.as_uri()
        excerpt = None
        if kind == "text" and (row["size_bytes"] or 0) <= EXCERPT_MAX_BYTES \
                and excerpt_budget > 0:
            try:
                excerpt = source.read_text(encoding="utf-8", errors="replace")[:EXCERPT_CHARS]
                excerpt_budget -= len(excerpt)
            except OSError:
                excerpt = None

        records.append({
            "n": row["name"], "p": row["rel_path"], "type": row["unity_type"],
            "w": row["width"], "h": row["height"], "b": row["size_bytes"],
            "img": image_href, "t": thumb, "kind": kind, "href": href, "ex": excerpt,
            "dup": row["duplicate_group"], "role": row["primary_role"],
            "feature": row["primary_feature"], "mechanic": row["primary_mechanic"],
            "obstacle": row["id"] in obstacles, "sub": subcategory.get(row["id"]),
            "at": 1 if row["guid"] in atlas_pages else None,
            "ac": packed.get(row["guid"]),
            "eng": 1 if row["origin"] == "engine" else None,
            "u": usage.get(row["id"], []),
            "dur": row["duration_seconds"], "rate": row["sample_rate"],
            "ch": row["channels"],
            "fr": clip["frames"] if clip else None,
            "layers": clip["layers"] if clip else None,
            "nodes": clip["nodes"] if clip else None,
            "masks": clip["masks"] if clip else None,
            "pg": piece_sets.get(piece_key.get(row["id"])) if row["id"] in piece_key else None,
            "clipdur": clip["duration"] if clip else None,
            "curves": clip["curves"] if clip else None,
        })
        row_guid.append(row["guid"])
        row_id.append(row["id"])
        row_image.append(row["image_path"])
        if made and made % 400 == 0:
            print(f"  {made} thumbnails", flush=True)

    # The atlas is addressed the way everything else on the page is - by its position
    # in DATA - so the browser can open the sheet a sprite came from without a lookup
    # table of its own.
    place_of_guid = {guid: position for position, guid in enumerate(row_guid) if guid}
    for position, asset_id in enumerate(row_id):
        rect = sprite_rect.get(asset_id)
        if not rect:
            continue
        page = place_of_guid.get(rect[0])
        if page is None:
            continue
        records[position]["ax"] = page
        records[position]["r"] = list(rect[1:])

    grouped = group_objects(records, [
        (position, [layer["img"] for layer in record["layers"]])
        for position, record in enumerate(records) if record.get("layers")])

    # The sheet, as the page can actually show it. A Texture2D is addressed by a
    # file:// URI, which a browser refuses to load once the catalogue is served over
    # HTTP - and being able to read it from a phone is why it is served at all. Only
    # pages that hold more than one sprite are copied: everything else is a sprite's
    # own texture, which the pieces strip already shows.
    sheet_dir = out_dir / "atlas"
    sheets = 0
    for page in {entry["a"] for entry in grouped if entry["a"] is not None}:
        record = records[page]
        if not record.get("ac") or record["ac"] < 2 or not row_image[page]:
            continue
        target = sheet_dir / f"{row_id[page]}.jpg"
        if not target.exists():
            stored = row_image[page]
            source = (out_dir / stored if stored.startswith("sprites/")
                      else assets_root / stored)
            try:
                save_sheet(source, target)
            except (OSError, ValueError):
                continue
            sheets += 1
        record["sheet"] = f"atlas/{target.name}"

    # The mesh chain, for builds that have one. Textures are addressed the same way
    # sprites are, so a model's surface loads from the same folders as everything
    # else on the page.
    models: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, path, object_name, mesh_name,
                      mesh_bytes, skinned, materials, render_path, tri_count
                 FROM models ORDER BY skinned DESC, tri_count DESC, mesh_name"""):
            entry = {
                "prefab_id": row["prefab_id"],
                "prefab_name": row["prefab_name"], "path": row["path"],
                "object_name": row["object_name"], "mesh_name": row["mesh_name"],
                "mesh_bytes": row["mesh_bytes"], "skinned": bool(row["skinned"]),
                "render": row["render_path"], "tris": row["tri_count"],
                "materials": json.loads(row["materials"]) if row["materials"] else [],
            }
            for material in entry["materials"]:
                for texture in material.get("textures", []):
                    path = texture.get("img")
                    if path and not path.startswith("sprites/"):
                        texture["img"] = (assets_root / path).as_uri()
            models.append(entry)
    except sqlite3.OperationalError:
        pass                      # models stage not run for this catalogue

    # An assembled prefab, drawn whole. This is the one view that shows the game
    # rather than its parts, so it leads the 3D tab.
    scenes: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, render_path, part_count, tri_count
                 FROM scenes ORDER BY tri_count DESC"""):
            scenes.append({"id": row["prefab_id"], "name": row["prefab_name"],
                           "render": row["render_path"], "parts": row["part_count"],
                           "tris": row["tri_count"]})
    except sqlite3.OperationalError:
        pass

    stored = conn.execute(
        "SELECT value FROM meta WHERE key = 'profile'").fetchone()
    profile = json.loads(stored["value"]) if stored else None

    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    page = (PAGE.replace("__RIG_ENGINE__", RIG_ENGINE)
                .replace("__SHARED_VIEWS__", SHARED_VIEWS)
                .replace("__DATA__", payload)
                .replace("__MODELS__", json.dumps(models, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__SCENES__", json.dumps(scenes, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__OBJECTS__", json.dumps(grouped, ensure_ascii=False,
                                                   separators=(",", ":")))
                .replace("__PROFILE__", json.dumps(profile))
                .replace("__TITLE__", title))
    (out_dir / "browser.html").write_text(page, encoding="utf-8")
    return {"assets": len(records), "thumbnails_created": made,
            "with_thumb": sum(1 for r in records if r["t"]), "models": len(models),
            "objects": len(grouped), "sheets": sheets}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the research browser.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--title", default="AssetLab")
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    stats = build(args.out.resolve(), args.export.resolve(), args.title, conn)
    print(f"browser.html: {stats['assets']} assets, {stats['with_thumb']} with thumbnails "
          f"({stats['thumbnails_created']} new)")
    print(f"open: {(args.out.resolve() / 'browser.html')}")
    conn.close()


if __name__ == "__main__":
    main()
