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
  stage.style.transform = stageTransform(layers, nodes, t, +stage.dataset.fit, masks,
                                         stageCamera(stage));
}
function playRig(layers, nodes, duration, masks){
  stopClip();
  const stage = document.getElementById("rigstage");
  const label = document.getElementById("clippos");
  if (!stage) return;
  const parts = [...stage.querySelectorAll("[data-layer]")].map(root => ({
    root, chain: [...root.querySelectorAll("[data-node]")] }));
  const started = performance.now(), fit = +stage.dataset.fit, cam = stageCamera(stage);
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
    stage.style.transform = stageTransform(layers, nodes, t, fit, masks, cam);
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
// Median centre of the parts on show. It moves with a rig that travels as a whole
// and ignores the one part that flies off, or is parked far away to hide it.
function partsCentre(layers, nodes, t, masks){
  const xs = [], ys = [];
  for (const layer of layers){
    if (layerStyle(layer, t) === "none") continue;
    const b = boxAt(layer, nodes, t);
    xs.push((b[0] + b[2]) / 2); ys.push((b[1] + b[3]) / 2);
  }
  if (!xs.length) return [0, 0];
  xs.sort((a, b) => a - b); ys.sort((a, b) => a - b);
  return [xs[xs.length >> 1], ys[ys.length >> 1]];
}
function rigFit(layers, nodes, duration, masks){
  const boxes = [];
  for (let k = 0; k <= 12; k++){
    const t = duration ? duration * k / 12 : 0;
    const b = rigBounds(layers, nodes, t, masks);
    boxes.push({t, b, w: Math.max(1, b[2]-b[0]), h: Math.max(1, b[3]-b[1]),
                c: partsCentre(layers, nodes, t, masks)});
  }
  const byArea = [...boxes].sort((a, b) => a.w*a.h - b.w*b.h);
  const median = byArea[Math.floor(byArea.length / 2)];
  // The camera holds still. Re-centring on the rig's bounds every frame cancelled
  // the motion it was there to show - a bounce became a wobble in place - and let
  // one part flung off or parked out of sight swing the whole picture: across five
  // builds the frame moved by more than 5% of the rig's size in 907 of 2,009 clips
  // and by more than half of it in 156. Only a rig whose parts travel further than
  // its own size, a cart driving across, is followed - and then by where most of
  // its parts are, not by the box around all of them.
  const xs = boxes.map(e => e.c[0]), ys = boxes.map(e => e.c[1]);
  const travel = Math.max(Math.max(...xs) - Math.min(...xs),
                          Math.max(...ys) - Math.min(...ys));
  const centre = [(median.b[0] + median.b[2]) / 2, (median.b[1] + median.b[3]) / 2];
  // Enlarged too where the rig is small: one build lays its UI out in units a
  // hundredth of a pixel, and capping at 1 drew a dialog eleven pixels tall. The
  // browser scales the sprites from their own pixels, so they stay sharp.
  return {fit: Math.max(0.15, Math.min(330 / (median.w * 1.15), 290 / (median.h * 1.15), 48)),
          poseAt: median.t,
          cam: {centre, shift: [centre[0] - median.c[0], centre[1] - median.c[1]],
                follow: travel > Math.max(median.w, median.h)}};
}
function stageCamera(stage){
  const v = (stage.dataset.cam || "").split(",").map(Number);
  return v.length === 5 ? {centre: [v[0], v[1]], shift: [v[2], v[3]], follow: v[4] === 1}
                        : null;
}
function stageTransform(layers, nodes, t, fit, masks, cam){
  let cx, cy;
  if (cam && !cam.follow) [cx, cy] = cam.centre;
  else if (cam){
    const c = partsCentre(layers, nodes, t, masks);
    cx = c[0] + cam.shift[0]; cy = c[1] + cam.shift[1];
  } else {
    const b = rigBounds(layers, nodes, t, masks);
    cx = (b[0] + b[2]) / 2; cy = (b[1] + b[3]) / 2;
  }
  return `scale(${fit}) translate(${-cx}px, ${-cy}px)`;
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
  const {fit, poseAt, cam} = rigFit(d.layers, d.nodes, d.clipdur, d.masks);
  const camera = [...cam.centre, ...cam.shift, cam.follow ? 1 : 0]
    .map(v => +v.toFixed(3)).join(",");
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
    border-radius:8px">${filters}<div id="rigstage" data-fit="${fit.toFixed(5)}" data-cam="${camera}"
    data-pose-at="${poseAt.toFixed(4)}"
    style="position:absolute;left:50%;top:50%;transform-origin:0 0">${html}</div></div>`;
}
