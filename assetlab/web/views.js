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

// ---- object view ----------------------------------------------------------
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
      return note(cuts.length === 1
        ? "This piece is its own texture rather than a cut from a shared sheet."
        : "These pieces are each the whole of one texture rather than cuts from a shared sheet.");
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
  // Sometimes no clip in the build shows the thing whole - a gift box animates one
  // state at a time, and five of its fifty sprites are on screen at once. The best
  // available is still shown, with what it is saying about itself.
  const onScreen = clip && clip.layers
    ? objectOnScreen(clip, new Set(members.map(d => d.img))) : 0;
  const partial = clip && clip.layers && onScreen < members.length * 0.4
    ? `<p style="color:var(--dim);font-size:13px;margin:4px 0">This is the fullest
       clip the build has for it, and it holds ${onScreen} of the ${members.length}
       sprites on screen at once — the rest belong to states it does not show.</p>`
    : "";
  const woken = clip && clip.woken
    ? `<p style="color:var(--dim);font-size:13px;margin:4px 0">The prefab ships these
       parts switched off — the build turns them on at run time — so they are
       drawn here as authored rather than as an empty stage.</p>` : "";
  const final = clip && clip.layers
    ? crowd + woken + partial + rigStage(clip) + clipControls(clip)
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
// How much of one object a clip has on screen at the moment it would be shown.
function objectOnScreen(clip, mine){
  const {poseAt} = rigFit(clip.layers, clip.nodes, clip.clipdur, clip.masks);
  const seen = new Set();
  for (const layer of clip.layers)
    if (mine.has(layer.img) && layerStyle(layer, poseAt) !== "none") seen.add(layer.img);
  return seen.size;
}
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
  const able = [];
  for (const d of DATA){
    if (!d.layers || !d.layers.length) continue;
    if (build !== undefined && d.g !== build) continue;
    const drawn = new Set(d.layers.map(layer => layer.img));
    let covers = 0;
    for (const image of drawn) if (mine.has(image)) covers++;
    // One sprite in common is a coincidence, not a portrait. A bird's clip that
    // happens to draw a bowtie is not a picture of the bowtie.
    if (covers < 2) continue;
    // How much of the clip is this object. Two clips can draw all four of an
    // object's sprites while one of them is a lamp animation that happens to
    // include them; the one that is mostly about this object is the picture of it.
    // Bucketed so a rounding difference cannot outrank what the clip depicts.
    able.push({d, covers, share: Math.round(covers / drawn.size * 20),
               visible: objectOnScreen(d, mine), shown: clipShows(d)});
  }
  if (!able.length) return null;
  // Visibility gates before anything else ranks. A build ships the same rocket as a
  // fall-start and a fall-end; the start has two of the rocket's sixteen sprites on
  // screen and the end has fifteen, and ranking a start above an end - which it is,
  // as a depiction - put two sprites in front of the reader. What counts is how much
  // of *the object* is drawn, not how much of the clip: a two-layer clip showing both
  // of its layers is not beaten by a six-layer one showing all six. Measured against
  // the best any candidate manages, so an object whose every clip is sparse still
  // gets one.
  const most = Math.max(...able.map(entry => entry.visible));
  const usable = able.filter(entry => entry.visible >= most * 0.5);
  let best = null, bestKey = null;
  for (const entry of usable){
    // How much of the object it draws, then how much of it is this object, then what
    // it depicts - an idle or a tap over an explosion - then how much it shows.
    const key = [-entry.covers, -entry.share, clipRank(entry.d.n), -entry.shown,
                 -entry.d.layers.length];
    if (!bestKey || before(key, bestKey)) { best = entry.d; bestKey = key; }
  }
  if (best && best.layers) wakeRig(best);
  return best;
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
