"""Combined browser: every catalogued game in one page, filtered by tick box.

Embedding five full catalogues would be ~50 MB of JSON and slow to parse, so the
hub carries only the previewable assets (images, audio, animations, fonts) and
drops text excerpts and usage lists. Those stay in each game's own browser.html,
which remains the complete view.

Media paths are rewritten to point at each game's output folder, so the hub must
sit next to them - `out/hub.html` alongside `out/<game>/`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .core import connect

MEDIA_KINDS = {"image", "audio", "animation", "font"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AssetLab hub</title>
<style>
:root{--bg:#14161a;--panel:#1c2027;--line:#2c323d;--text:#eef1f5;--dim:#98a2b1;--accent:#6cb6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:12px 16px}
h1{margin:0 0 10px;font-size:16px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.games{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:9px}
input,select{background:#12151a;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:inherit}
input[type=search]{min-width:230px}
label.chk{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px}
label.game{display:flex;align-items:center;gap:6px;background:#232833;border:1px solid var(--line);
 border-radius:6px;padding:5px 10px;font-size:13px;cursor:pointer}
#count{color:var(--dim);font-size:13px;margin-left:auto}
#filtertoggle{display:none}
@media (max-width:760px){
  header{padding:10px 12px;max-height:80vh;overflow-y:auto}
  h1{display:flex;align-items:center;justify-content:space-between;gap:10px;
     margin-bottom:8px;font-size:15px}
  #filtertoggle{display:inline-block;flex:none;background:#232833;color:var(--text);
    border:1px solid var(--line);border-radius:6px;padding:6px 12px;font:inherit;
    cursor:pointer}
  /* Collapsed by default: the tabs and the count are the only things worth a
     permanent row, and everything else is one tap away. */
  header:not(.open) .games{display:none}
  header:not(.open) .controls>:not(.vtabs):not(#count){display:none}
  .controls{gap:6px}
  .vtabs{display:flex;width:100%}
  .vtabs .vtab{flex:1;text-align:center}
  #count{margin-left:0;width:100%}
  .games{gap:6px}
  label.game{font-size:12px;padding:4px 8px;gap:5px}
  input[type=search]{min-width:0;width:100%}
  /* Two per row rather than one, which is what made the panel four screens tall. */
  .controls select{flex:1 1 45%;min-width:0}
  .grid{grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
  main{padding:12px 12px 60px}
}
main{padding:14px 16px 60px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px;cursor:pointer;overflow:hidden}
.card:hover{border-color:var(--accent)}
.card img{width:100%;height:118px;object-fit:contain;background:#0e1014;border-radius:5px;display:block}
.card .ph{height:118px;background:#0e1014;border-radius:5px;display:flex;align-items:center;justify-content:center;color:#5c6675;font-size:30px}
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
button.close{background:none;border:1px solid var(--line);color:var(--dim);border-radius:6px;padding:5px 10px;cursor:pointer}
.tag{display:inline-block;background:#252b34;border-radius:4px;padding:1px 7px;font-size:11px;color:var(--dim);margin:2px 3px 0 0}
.vtab{background:#12151a;color:var(--dim);border:1px solid var(--line);padding:7px 14px;
 cursor:pointer;font:inherit}
.vtab:first-child{border-radius:6px 0 0 6px}
.vtab:last-child{border-radius:0 6px 6px 0;border-left:0}
.vtab.on{background:var(--accent);color:#0d1117;border-color:var(--accent)}
aside h3{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);
 margin:16px 0 6px;font-weight:600}
/* A model has no picture of itself; the surface it is drawn with stands in. */
.swatch{display:inline-block;width:26px;height:26px;border-radius:5px;
 border:1px solid var(--line);vertical-align:middle;margin-right:8px}
.matrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:7px 0;
 border-bottom:1px solid var(--line)}
.matrow:last-child{border-bottom:0}
.matrow b{font-weight:500;font-size:12px}
.render{width:100%;aspect-ratio:1;background:#0e1014;border-radius:5px;
  object-fit:contain;display:block}
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
.pbadge{font-size:10px;padding:0 5px;border-radius:3px;margin-left:5px;
 background:#252b34;color:var(--dim);letter-spacing:.4px}
.pbadge.is3d{background:var(--accent);color:#0d1117}
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
</style></head><body>
<header>
  <h1><span>AssetLab <span style="color:var(--dim);font-weight:400">— all games</span></span>
    <button id="filtertoggle" aria-expanded="false">filters</button></h1>
  <div class="games" id="games"></div>
  <div class="controls">
    <span class="vtabs"><button class="vtab on" data-view="assets">assets</button><button
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
    <label class="chk"><input type="checkbox" id="obst"> obstacles only</label>
    <label class="chk"><input type="checkbox" id="atlas" checked> hide atlas sheets</label>
    <span id="count"></span>
  </div>
</header>
<main><div class="grid" id="grid"></div></main>
<aside id="panel"></aside>
<script>
const DATA = __DATA__, GAMES = __GAMES__;
const F = ["role","feature","mechanic","type"];
const el = id => document.getElementById(id);
DATA.forEach((d, i) => { d._i = i; });

// Each build carries the verdict of its own profile stage, so a thin-looking
// catalogue can be read as "this one is 3D" rather than as a broken import.
el("games").innerHTML = GAMES.map(g => {
  const p = g.profile;
  const badge = p ? `<span class="pbadge ${p.verdict === "3d" ? "is3d" : ""}"
    title="${p.note} (score ${p.score})">${p.verdict.toUpperCase()}</span>` : "";
  return `<label class="game"><input type="checkbox" class="gamebox" value="${g.id}" checked>
   ${g.title} <span style="color:var(--dim)">${g.count.toLocaleString()}</span>${badge}</label>`;
}).join("");

function activeGames(){
  return new Set([...document.querySelectorAll(".gamebox")].filter(b => b.checked)
                 .map(b => b.value));
}
let games = activeGames();

function passes(d, skipKey){
  if (!games.has(d.g)) return false;
  // An atlas sheet is the page its sprites were cut from, not a piece of art.
  if (el("atlas").checked && d.at) return false;
  if (el("obst").checked && !d.obstacle) return false;
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

function refreshFacets(){
  for (const key of F){
    const select = el(key), current = select.value, counts = {};
    for (const d of DATA) { if (passes(d, key)) { const v = d[key];
                            if (v) counts[v] = (counts[v]||0)+1; } }
    if (current && !(current in counts)) counts[current] = 0;
    select.innerHTML = `<option value="">all ${key}s</option>` +
      Object.entries(counts).sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0]))
        .map(([v,n]) => `<option value="${v}">${v} (${n})</option>`).join("");
    select.value = current;
  }
}

let clipTimer = null, rigFrame = null;
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
  if (layer.off) return "none";
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
function playClip(frames, duration){
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
    if (d.fr) return `<img id="clipframe" src="${d.fr[0][1]}">` +
      controls(`playClip(DATA[${d._i}].fr, DATA[${d._i}].clipdur)`,
               `${d.fr.length} frames · ${d.clipdur ?? "?"}s`);
    return rigStage(d) + controls(`playRig(DATA[${d._i}].layers, DATA[${d._i}].nodes, DATA[${d._i}].clipdur, DATA[${d._i}].masks)`,
      `${d.layers.length} layers · ${(d.clipdur ?? 0).toFixed(2)}s`);
  }
  if (d.pg && d.pg.length > 1) return pieceSet(d);
  if (d.kind === "image") return `<img src="${d.img}">`;
  if (d.kind === "audio")
    return `<audio controls preload="none" style="width:100%" src="${d.href}"></audio>`;
  if (d.kind === "font")
    return `<style>@font-face{font-family:"pv${d._i}";src:url("${d.href}")}</style>
      <div style="font-family:'pv${d._i}',serif;font-size:24px;background:#0e1014;
                  border-radius:8px;padding:12px">ABCDEFG abcdefg 0123456789<br>
                  Şeker Ğüzel İçki Öykü</div>`;
  return "";
}

let shown = 0, filtered = [];
const GLYPH = {audio:"♪", font:"A", animation:"▶"};
function card(d, i){
  const thumb = d.t ? `<img loading="lazy" src="${d.t}">`
                    : `<div class="ph">${GLYPH[d.kind] || "·"}</div>`;
  const sub = d.kind === "audio" ? (d.dur ? d.dur.toFixed(2)+"s" : "audio")
            : d.kind === "animation"
              ? (d.fr ? `▶ ${d.fr.length} frames` : `▶ ${d.layers.length} layers`)
            : d.w ? d.w+"×"+d.h : d.type;
  return `<div class="card" data-i="${i}">${thumb}<b>${d.n}</b>
          <s>${GAMEBADGE[d.g]} · ${sub}</s></div>`;
}
const GAMEBADGE = Object.fromEntries(GAMES.map(g => [g.id, g.short]));


// ---- model view -----------------------------------------------------------
// What a 3D build has instead of sprites: a mesh drawn through materials, each of
// which is either a set of textures or a flat colour. Both are shown, because in
// the 3D build here half the models are colour and no texture at all.
const MODELS = __MODELS__;
const SCENES = __SCENES__;

function swatch(colour){
  return colour ? `<span class="swatch" style="background:${colour.hex}"
    title="${colour.hex}"></span>` : "";
}
// A drawn model beats the atlas it is painted with: a UV sheet is a scattering of
// unplaced parts, and no reader has ever recognised a character from one.
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
    <b>${label}</b><s>${GAMEBADGE[model.g]} · ${model.skinned ? "rigged · " : ""}${tris}${
      model.materials.length} material${model.materials.length === 1 ? "" : "s"}</s></div>`;
}
function sceneCard(scene, i){
  return `<div class="card" data-scene="${i}">
    <img loading="lazy" class="render" src="${scene.render}" alt="">
    <b>${scene.name}</b><s>${GAMEBADGE[scene.g]} · ${scene.parts} parts ·
    ${scene.tris.toLocaleString()} tris</s></div>`;
}
function scenePanel(scene){
  const parts = MODELS.filter(m => m.g === scene.g && m.prefab_id === scene.id);
  const strip = parts.map(m => `<figure>${m.render ? `<img src="${m.render}">` : ""}
    <figcaption>${m.mesh_name || m.object_name || "?"}</figcaption></figure>`).join("");
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
    <div><span class="tag">${GAMEBADGE[model.g]}</span></div>
    <h3>mesh <span>${model.skinned ? "rigged geometry" : "static geometry"}${
      model.mesh_bytes ? " · " + Math.round(model.mesh_bytes / 1024) + " KB" : ""}</span></h3>
    <p style="color:var(--dim);font-size:13px;margin:4px 0">Geometry is not rendered
      here. What is shown is the surface it is drawn with.</p>
    <h3>materials <span>${model.materials.length}</span></h3>${rows}
    <dl><dt>prefab</dt><dd>${model.prefab_name}</dd>
        <dt>path</dt><dd>${model.path || "(root)"}</dd></dl>`;
}

// ---- obstacle view --------------------------------------------------------
// The design enums name each blocker, so its art is exactly the tagged assets that
// carry that name; grouping on it is what turns 800 loose sprites into ~40 obstacles.
let VIEW = "assets";
function familyKey(d){ return (GAMEBADGE[d.g] + " · " + (d.mechanic || d.feature || "unlabelled")); }
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
// Fragments, sparks and shadows are what an obstacle is built from and what it
// leaves behind; the rest is what sits on the board. Splitting on the words the
// artists themselves used beats any size threshold - a big blast sprite is not the
// obstacle and a small goal icon is.
const PIECE_WORDS = new Set(["part","parts","piece","pieces","frag","fragment",
  "fragments","debris","shard","shards","chunk","particle","particles","blast",
  "glow","spark","sparkle","sparkles","additive","add","fx","vfx","smoke","dust",
  "trail","flash","shadow","crack","cracked","broken","explode","explosion","dot",
  "ray","rays","confetti","splash","splinter","dirt","puff","burst","mask"]);
function wordsOf(text){
  return (text || "").split(/[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])/)
                     .filter(Boolean).map(w => w.toLowerCase());
}
function isPiece(d){
  if (d.sub === "Particle") return true;
  return wordsOf(d.n).concat(wordsOf(d.p)).some(w => PIECE_WORDS.has(w));
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
function splitFamily(parts){
  // The rig is the obstacle assembled, so the sprites it draws are by definition the
  // pieces it is made of. That beats guessing from names, which reads a shopping
  // cart's wheel and its body as equally whole.
  const rig = familyRig(parts);
  const drawn = new Set(rig ? rig.layers.map(layer => layer.img) : []);
  const whole = [], pieces = [], seen = new Set();
  // A raw atlas sheet is not artwork - it is the page the artwork was cut from,
  // and being the largest image in the set it would headline every card.
  for (const d of parts.filter(d => !d.at)){
    seen.add(d._i);
    ((drawn.has(d.img) || isPiece(d)) ? pieces : whole).push(d);
  }
  // Sometimes the classifier files the rig's art under a neighbouring name and this
  // family keeps none of it. The rig is still the authority on what the obstacle is
  // made of, so the sprites it draws are fetched back.
  if (drawn.size && !pieces.some(d => drawn.has(d.img)))
    for (const d of DATA)
      if (d.img && drawn.has(d.img) && !seen.has(d._i)) pieces.push(d);
  const bySize = (a, b) => (b.w||0)*(b.h||0) - (a.w||0)*(a.h||0);
  return [whole.sort(bySize), pieces.sort(bySize)];
}
function familyCard(name, parts, i){
  const [whole, pieces] = splitFamily(parts);
  const shots = (whole.length ? whole : pieces).slice(0, 4)
    .map(p => `<img loading="lazy" src="${p.t || p.img}">`).join("");
  const total = whole.length + pieces.length;
  return `<div class="card famcard" data-fam="${i}"><div class="famshots">${shots}</div>
          <b>${name}</b>
          <s>${whole.length} whole · ${pieces.length} piece${pieces.length === 1 ? "" : "s"}</s>
          </div>`;
}
function strip(list){
  return `<div class="partstrip">` + list.map(d =>
    `<figure><img src="${d.img}" title="${d.n}">
     <figcaption>${d.n}<br>${d.w||"?"}×${d.h||"?"}</figcaption></figure>`).join("")
    + `</div>`;
}
function familyPanel(name, parts){
  const [whole, pieces] = splitFamily(parts);
  const rig = familyRig(parts);
  const assembled = rig
    ? `${rigStage(rig)}
       <div style="display:flex;gap:8px;align-items:center;margin:6px 0 4px">
         <button class="close" onclick="playRig(DATA[${rig._i}].layers, DATA[${rig._i}].nodes,
           DATA[${rig._i}].clipdur, DATA[${rig._i}].masks)">play</button>
         <button class="close" onclick="stopClip()">stop</button>
         <small id="clippos">${rig.layers.length} layers · ${
           (rig.clipdur ?? 0).toFixed(2)}s · ${rig.n}</small></div>`
    : `<p style="color:var(--dim);font-size:13px;margin:4px 0">This one ships no rigged
       clip, so there is no assembled view — only the art below.</p>`;
  return `<button class="close"
      onclick="stopClip();document.getElementById('panel').classList.remove('open')">close</button>
    <h2>${name}</h2>
    <h3>final form <span>the obstacle as it sits on the board</span></h3>
    ${assembled}
    ${whole.length ? strip(whole) : ""}
    ${pieces.length ? `<h3>pieces <span>${pieces.length} sprite${
        pieces.length === 1 ? "" : "s"} it is built from, plus its effects</span></h3>`
      + strip(pieces) : ""}`;
}
let FAMILIES = [];
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
      const shown = MODELS.map((m, i) => [m, i]).filter(([m]) => games.has(m.g) &&
        (!query || (m.mesh_name || "").toLowerCase().includes(query) ||
                   (m.object_name || "").toLowerCase().includes(query) ||
                   (m.prefab_name || "").toLowerCase().includes(query)));
      // One row per placement suits the database; a gallery wants one card per
      // distinct mesh, or a chair leg used four times fills the screen.
      const seen = new Set();
      const unique = shown.filter(([m]) => {
        const key = m.g + "/" + (m.mesh_name || m.object_name);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      const assembled = SCENES.map((s, i) => [s, i]).filter(([s]) => games.has(s.g) &&
        (!query || s.name.toLowerCase().includes(query)));
      const lead = assembled.length
        ? `<div class="scenehead">assembled prefabs</div>
           <div class="scenestrip">${assembled.map(([s, i]) => sceneCard(s, i)).join("")}</div>
           <div class="scenehead">individual meshes</div>` : "";
      el("grid").innerHTML = lead +
        `<div class="scenestrip">${unique.map(([m, i]) => modelCard(m, i)).join("")}</div>`;
      el("count").textContent = `${unique.length} models, ` +
        `${unique.filter(([m]) => m.skinned).length} rigged, ` +
        `${assembled.length} assembled`;
      return;
    }
    filtered = DATA.filter(match);
  }
  const slice = filtered.slice(shown, shown+300);
  el("grid").insertAdjacentHTML("beforeend", slice.map(d => card(d, d._i)).join(""));
  shown += slice.length;
  el("count").textContent = `${filtered.length.toLocaleString()} assets` +
    (shown < filtered.length ? ` (showing ${shown})` : "");
}
["q","role","feature","mechanic","type","size","obst","atlas"].forEach(id =>
  el(id).addEventListener(id === "q" ? "input" : "change", () => render(true)));
document.querySelectorAll(".gamebox").forEach(box =>
  box.addEventListener("change", () => {
    games = activeGames(); syncModelsTab(); render(true); }));
addEventListener("scroll", () => {
  if (shown < filtered.length && innerHeight + scrollY > document.body.offsetHeight - 700)
    render(false);
});
document.querySelectorAll(".vtab").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".vtab").forEach(b => b.classList.toggle("on", b === tab));
  VIEW = tab.dataset.view; render(true);
}));
el("filtertoggle").addEventListener("click", event => {
  const open = document.querySelector("header").classList.toggle("open");
  event.currentTarget.setAttribute("aria-expanded", String(open));
  event.currentTarget.textContent = open ? "close" : "filters";
});

el("grid").addEventListener("click", e => {
  const scene = e.target.closest("[data-scene]");
  if (scene){
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
  panel.innerHTML = `<button class="close"
      onclick="stopClip();document.getElementById('panel').classList.remove('open')">close</button>
    ${preview(d)}<h2>${d.n}</h2>
    <div>${[GAMEBADGE[d.g], d.role, d.feature, d.mechanic, d.obstacle?"OBSTACLE":""]
             .filter(Boolean).map(t => `<span class="tag">${t}</span>`).join("")}</div>
    <dl><dt>type</dt><dd>${d.type}</dd><dt>size</dt><dd>${d.w?d.w+" × "+d.h:"—"}</dd>
        <dt>path</dt><dd>${d.p}</dd></dl>`;
  if (d.kind === "animation" && d.layers) poseRig(d.layers, d.nodes, d.masks);
  panel.classList.add("open");
});
// The tab exists only when a selected build has a mesh chain behind it.
function syncModelsTab(){
  const any = MODELS.some(m => games.has(m.g)) || SCENES.some(s => games.has(s.g));
  el("modelstab").style.display = any ? "" : "none";
  if (!any && VIEW === "models"){
    VIEW = "assets";
    document.querySelectorAll(".vtab").forEach(b =>
      b.classList.toggle("on", b.dataset.view === "assets"));
  }
}
syncModelsTab();
render(true);
</script></body></html>
"""


def collect(out_dir: Path, name: str) -> tuple[list[dict], dict] | None:
    """Pull the previewable rows out of one game's catalogue."""
    database = out_dir / name / "assetlab.db"
    if not database.is_file():
        return None
    conn = connect(database)
    conn.row_factory = sqlite3.Row

    obstacles = {row[0] for row in conn.execute(
        "SELECT asset_id FROM tags WHERE kind='category' AND value='Obstacle'")}
    # The pages sprites were cut from. They are the largest images in the catalogue,
    # so an obstacle set that keeps them shows its atlas sheet instead of its art.
    atlas_pages = {row[0] for row in conn.execute(
        "SELECT DISTINCT atlas_guid FROM sprites WHERE atlas_guid IS NOT NULL")}
    clips: dict[int, dict] = {}
    try:
        for row in conn.execute(
            "SELECT asset_id, frames, layers, nodes, masks, duration FROM animations"):
            if row["frames"] or row["layers"]:
                clips[row["asset_id"]] = {
                    "fr": json.loads(row["frames"]) if row["frames"] else None,
                    "layers": json.loads(row["layers"]) if row["layers"] else None,
                    "nodes": json.loads(row["nodes"]) if row["nodes"] else None,
                    "masks": json.loads(row["masks"]) if row["masks"] else None,
                    "dur": row["duration"]}
    except sqlite3.OperationalError:
        pass

    prefix = f"{name}/"
    # Multi-part obstacle art carries its whole set, so it can be shown assembled.
    piece_sets: dict[str, list[dict]] = {}
    piece_key: dict[int, str] = {}
    try:
        for row in conn.execute(
            """SELECT p.group_key, p.position, a.id, a.name, a.image_path, a.width, a.height
                 FROM piece_groups p JOIN assets a ON a.id = p.asset_id
                WHERE a.image_path LIKE 'sprites/%'
                ORDER BY p.group_key, p.position, a.name"""):
            piece_key[row["id"]] = row["group_key"]
            piece_sets.setdefault(row["group_key"], []).append({
                "n": row["name"], "pos": row["position"], "w": row["width"],
                "h": row["height"], "img": prefix + row["image_path"]})
    except sqlite3.OperationalError:
        pass

    records = []
    for row in conn.execute(
        """SELECT id, guid, name, rel_path, unity_type, ext, width, height, image_path,
                  primary_role, primary_feature, primary_mechanic,
                  duration_seconds, thumbnail
             FROM assets ORDER BY unity_type, name"""):
        ext = (row["ext"] or "").lower()
        clip = clips.get(row["id"])
        if row["image_path"]:
            kind = "image"
        elif clip:
            kind = "animation"
        elif ext in {"ogg", "wav", "mp3", "m4a", "aac", "flac"}:
            kind = "audio"
        elif ext in {"ttf", "otf", "woff", "woff2"}:
            kind = "font"
        else:
            continue                      # text and everything else stays per-game
        if kind not in MEDIA_KINDS:
            continue

        thumb = (out_dir / name / "thumbs" / f"{row['id']}.jpg")
        record = {
            "g": name, "n": row["name"], "p": row["rel_path"], "type": row["unity_type"],
            "w": row["width"], "h": row["height"], "kind": kind,
            "role": row["primary_role"], "feature": row["primary_feature"],
            "mechanic": row["primary_mechanic"], "obstacle": row["id"] in obstacles,
            "at": 1 if row["guid"] in atlas_pages else None,
            "pg": piece_sets.get(piece_key.get(row["id"])),
            "t": f"{prefix}thumbs/{row['id']}.jpg" if thumb.is_file() else None,
        }
        if kind == "image" and row["image_path"]:
            record["img"] = (prefix + row["image_path"]
                             if row["image_path"].startswith("sprites/") else None)
            if not record["img"]:
                record["img"] = record["t"]
        elif kind == "audio":
            record["href"] = f"{prefix}media/{row['id']}.{ext}"
            record["dur"] = row["duration_seconds"]
        elif kind == "font":
            record["href"] = f"{prefix}media/{row['id']}.{ext}"
        elif kind == "animation":
            record["fr"] = ([[t, prefix + p] for t, p in clip["fr"]] if clip["fr"] else None)
            record["layers"] = ([dict(layer, img=prefix + layer["img"])
                                 for layer in clip["layers"]] if clip["layers"] else None)
            record["nodes"] = clip["nodes"]
            record["masks"] = clip["masks"]
            record["clipdur"] = clip["dur"]
        records.append(record)
    # The mesh chain, for a build that has one. Texture paths get the same game
    # prefix as sprites, so a model's surface loads from that build's folder.
    models: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, path, object_name, mesh_name,
                      mesh_bytes, skinned, materials, render_path, tri_count
                 FROM models ORDER BY skinned DESC, tri_count DESC, mesh_name"""):
            entry = {
                "g": name, "prefab_id": row["prefab_id"],
                "prefab_name": row["prefab_name"], "path": row["path"],
                "object_name": row["object_name"], "mesh_name": row["mesh_name"],
                "mesh_bytes": row["mesh_bytes"], "skinned": bool(row["skinned"]),
                "render": prefix + row["render_path"] if row["render_path"] else None,
                "tris": row["tri_count"],
                "materials": json.loads(row["materials"]) if row["materials"] else [],
            }
            for material in entry["materials"]:
                kept = []
                for texture in material.get("textures", []):
                    image = texture.get("img") or ""
                    if image.startswith("sprites/"):
                        texture["img"] = prefix + image
                    else:
                        # A loose texture lives in the export tree, which the hub
                        # cannot reach. Its thumbnail is already next to the page,
                        # so the surface is shown at reduced size rather than lost.
                        thumb = out_dir / name / "thumbs" / f"{texture.get('id')}.jpg"
                        if not thumb.is_file():
                            continue
                        texture["img"] = f"{prefix}thumbs/{texture['id']}.jpg"
                        texture["reduced"] = True
                    kept.append(texture)
                material["textures"] = kept
            models.append(entry)
    except sqlite3.OperationalError:
        pass                       # models stage not run for this catalogue

    scenes: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, render_path, part_count, tri_count
                 FROM scenes ORDER BY tri_count DESC"""):
            scenes.append({"g": name, "id": row["prefab_id"],
                           "name": row["prefab_name"],
                           "render": prefix + row["render_path"],
                           "parts": row["part_count"], "tris": row["tri_count"]})
    except sqlite3.OperationalError:
        pass

    stored = conn.execute("SELECT value FROM meta WHERE key = 'profile'").fetchone()
    profile = json.loads(stored["value"]) if stored else None

    conn.close()
    return records, {"id": name, "title": name, "short": name,
                     "count": len(records), "profile": profile}, models, scenes


def build(out_dir: Path, names: list[str]) -> dict:
    data, games, models, scenes = [], [], [], []
    for name in names:
        result = collect(out_dir, name)
        if result is None:
            print(f"  skipped {name}: no catalogue")
            continue
        records, meta, found, assembled = result
        data.extend(records)
        games.append(meta)
        models.extend(found)
        scenes.extend(assembled)
        verdict = (meta.get("profile") or {}).get("verdict", "?").upper()
        extra = f", {len(found)} models" if found else ""
        print(f"  {name:<14} {len(records):>7} previewable assets  [{verdict}]{extra}")

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    page = (PAGE.replace("__DATA__", payload)
                .replace("__MODELS__", json.dumps(models, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__SCENES__", json.dumps(scenes, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__GAMES__", json.dumps(games, ensure_ascii=False)))
    target = out_dir / "hub.html"
    target.write_text(page, encoding="utf-8")
    return {"games": len(games), "assets": len(data), "models": len(models),
            "bytes": len(page), "path": target}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a combined multi-game browser.")
    parser.add_argument("--out", type=Path, default=Path("out"),
                        help="directory holding the per-game catalogues")
    parser.add_argument("--games", nargs="*", default=None,
                        help="catalogue folder names; defaults to every folder in --out")
    args = parser.parse_args()

    names = args.games or sorted(
        p.name for p in args.out.iterdir() if (p / "assetlab.db").is_file())
    stats = build(args.out.resolve(), names)
    print(f"\nhub.html: {stats['games']} games, {stats['assets']} assets, "
          f"{stats['bytes']/1e6:.1f} MB")
    print(f"open: {stats['path']}")


if __name__ == "__main__":
    main()
