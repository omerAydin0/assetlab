// ---- model view -----------------------------------------------------------
// Shared by both pages. What a 3D build has instead of sprites: a mesh drawn
// through materials, each of which is either a set of textures or a flat colour.
// Both are shown, because in the 3D build here half the models are colour and no
// texture at all.
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
    <b>${label}</b><s>${fromBuild(model)}${model.skinned ? "rigged · " : ""}${tris}${
      model.materials.length} material${model.materials.length === 1 ? "" : "s"}</s></div>`;
}
function placedTimes(entry){
  // Only a scene object has this: a prefab is a file, and a file is placed once.
  return entry.placements > 1 ? `placed ${entry.placements}× · ` : "";
}
function sceneCard(scene, i){
  return `<div class="card" data-scene="${i}">
    <img loading="lazy" class="render" src="${scene.render}" alt="">
    <b>${scene.name}</b><s>${fromBuild(scene)}${placedTimes(scene)}${scene.parts} parts ·
    ${scene.tris.toLocaleString()} tris</s></div>`;
}
function scenePanel(scene){
  // Keyed on the object, not the file it came from: one scene file holds
  // thousands of objects, and matching on the file would gather the whole level.
  const parts = MODELS.filter(m => m.g === scene.g && m.key === scene.key);
  const strip = parts.map(m => `<figure>${m.render ? `<img src="${m.render}">` : ""}
    <figcaption>${m.mesh_name || m.object_name || "?"}</figcaption></figure>`).join("");
  return `<button class="close"
      onclick="document.getElementById('panel').classList.remove('open')">close</button>
    <h2>${scene.name}</h2>
    <img class="render" style="max-width:420px" src="${scene.render}">
    <p style="color:var(--dim)">${scene.parts} parts, ${scene.tris.toLocaleString()}
      triangles, assembled from the object's own transforms.${
      scene.placements > 1 ? ` Its scene places this same shape ${scene.placements}
      times; it is shown once.` : ""}</p>
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
    ${typeof GAMEBADGE === "undefined" ? "" :
      `<div><span class="tag">${GAMEBADGE[model.g]}</span></div>`}
    <h3>mesh <span>${model.skinned ? "rigged geometry" : "static geometry"}${
      model.mesh_bytes ? " · " + Math.round(model.mesh_bytes / 1024) + " KB" : ""}</span></h3>
    <p style="color:var(--dim);font-size:13px;margin:4px 0">Geometry is not rendered
      here. What is shown is the surface it is drawn with.</p>
    <h3>materials <span>${model.materials.length}</span></h3>${rows}
    <dl><dt>from</dt><dd>${model.prefab_name}</dd>
        <dt>path</dt><dd>${model.path || "(root)"}</dd></dl>`;
}

