"""Run the page's own JavaScript against fixtures, in a browser that is already here.

Which clip is a picture of an object, what a prefab's inactive flag means, whether a
clip draws one figure or a crowd - all of it decides what the reader sees, and all of
it runs in the browser, because the curve sampler it depends on does. That left it as
the one part of the pipeline with no test, and the gap was not theoretical: a one-line
fix to `layerStyle` landed in one page and not the other, and only a measurement on a
real build caught it.

No JavaScript runtime is installed here and the project takes no new dependencies, so
the checks run in headless Edge, which ships with Windows; Chrome works too. Where
neither is found the suite says so and reports them skipped rather than passing - a
test that could not run is not a test that passed.

Run: python -m assetlab.jstest
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .browser import MODEL_VIEW, RIG_ENGINE, SHARED_VIEWS

#: Where a Chromium sits on a machine that never installed one on purpose.
BROWSER_HINTS = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
RESULT_RE = re.compile(r"ASSETLAB_RESULT:(\{.*?\})</", re.S)


def find_browser() -> Path | None:
    """A Chromium to run the page in. `CHROME` overrides, then the usual places."""
    named = os.environ.get("CHROME")
    if named and Path(named).is_file():
        return Path(named)
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for hint in BROWSER_HINTS:
        candidate = Path(os.path.expandvars(hint))
        if candidate.is_file():
            return candidate
    return None


#: The fixtures. They go in ahead of the shared code, which reads OBJECTS as it
#: loads, and they are plain data: everything between them and the checks is the
#: shipped code pasted in unchanged, so a check fails exactly when a reader's page
#: would.
FIXTURES = r"""
// ---- fixtures --------------------------------------------------------------
// A sprite as the catalogue holds one, and a clip that draws some of them. Piece
// positions are in pixels; a node's base is in Unity units, which the rig scales.
const DATA = [];
function sprite(name, w, h, extra){
  const record = Object.assign({n: name, img: name + ".png", kind: "image",
                                w: w, h: h, at: null}, extra || {});
  record._i = DATA.length; DATA.push(record); return record._i;
}
function clip(name, pieces, extra){
  const nodes = [], layers = [];
  for (const piece of pieces){
    nodes.push({base: [(piece.x || 0) / 100, (piece.y || 0) / 100, 1, 1]});
    layers.push({name: piece.img, img: piece.img + ".png",
                 size: [piece.w || 40, piece.h || 40], chain: [nodes.length - 1],
                 off: piece.off || undefined});
  }
  const record = Object.assign({n: name, img: null, kind: "animation", layers: layers,
                                nodes: nodes, masks: null, clipdur: 1, at: null},
                               extra || {});
  record._i = DATA.length; DATA.push(record); return record._i;
}
const FIXTURE_OBJECTS = [];
function object(name, whole, parts, clipIndex, atlas){
  FIXTURE_OBJECTS.push({n: name, w: whole, p: parts, c: clipIndex,
                        a: atlas === undefined ? null : atlas});
  return FIXTURE_OBJECTS.length - 1;
}
const el = () => ({checked: false, value: ""});
const match = () => true;

// A. Two clips draw the object equally. One is its idle, one its destruction.
const a1 = sprite("head", 40, 40), a2 = sprite("body", 40, 40);
clip("collect", [{img: "head"}, {img: "body"}]);
clip("idle_1", [{img: "head"}, {img: "body"}]);
const objIdle = object("dragon", a1, [a2], 2);

// B. Both draw all of it, but one is mostly about something else - and it is the
//    bigger clip, so without the share term it wins.
const b1 = sprite("eye_l", 20, 20), b2 = sprite("eye_r", 20, 20);
const bLamp = clip("lamp_animation", [{img: "eye_l"}, {img: "eye_r"}, {img: "shade"},
                                      {img: "stand"}, {img: "bulb"}, {img: "cord"}]);
clip("elephant_animation", [{img: "eye_l"}, {img: "eye_r"}]);
const objShare = object("elephant_eyes", b1, [b2], bLamp);

// C. A prefab that ships its whole rig switched off.
const c1 = sprite("koala_body", 40, 40), c2 = sprite("koala_head", 40, 40);
const cOff = clip("Koala_Idle", [{img: "koala_body", off: true},
                                 {img: "koala_head", off: true},
                                 {img: "koala_ear", off: true},
                                 {img: "koala_foot", off: true}]);
const objWoken = object("koala", c1, [c2], cOff);

// D. A rig that switches between three eyelids on purpose: most of it is on.
const dOn = clip("Blink", [{img: "face"}, {img: "brow"}, {img: "cheek"}, {img: "chin"},
                           {img: "mouth"}, {img: "nose"}, {img: "pupil"}, {img: "iris"},
                           {img: "lid1", off: true}, {img: "lid2", off: true}]);

// E. One figure's parts overlap; three elves standing in a row do not.
const eOne = clip("one_figure", [{img: "p1"}, {img: "p2", x: 5}, {img: "p3", y: 5}]);
const eThree = clip("three_elves", [{img: "e1a"}, {img: "e1b", x: 4},
                                    {img: "e2a", x: 400}, {img: "e2b", x: 404},
                                    {img: "e3a", x: 800}, {img: "e3b", x: 804}]);

// F. An object cut across two sheets. Only the cuts on the sheet being drawn belong
//    on it, and Unity measures a rect up from the bottom while the page draws down.
const sheetA = sprite("SheetA", 100, 100, {at: 1, ac: 9, sheet: "atlas/1.jpg"});
const sheetB = sprite("SheetB", 100, 100, {at: 1, ac: 4, sheet: "atlas/2.jpg"});
const f1 = sprite("gem", 10, 10, {ax: sheetA, r: [20, 0, 10, 10, 0]});
const f2 = sprite("gem_spark", 10, 10, {ax: sheetB, r: [0, 0, 10, 10, 0]});
const objSheets = object("gem", f1, [f2], null, sheetA);

// H. The same rocket as a fall-start and a fall-end. As a depiction the start ranks
//    higher; as a picture of the rocket it has almost none of it on screen.
const h1 = sprite("nose", 30, 30), h2 = sprite("fin", 20, 20), h3 = sprite("body", 30, 40);
clip("VerticalFallStartAnimation", [{img: "nose", off: true}, {img: "fin", off: true},
                                    {img: "body"}, {img: "smoke"}]);
clip("VerticalFallEndAnimation", [{img: "nose"}, {img: "fin"}, {img: "body"},
                                  {img: "smoke"}]);
const objRocket = object("rocket", h3, [h1, h2], DATA.length - 2);

// I. A clip that shares one sprite with an object is not a picture of it.
const i1 = sprite("bowtie", 20, 10), i2 = sprite("bowtie_knot", 8, 8);
clip("BirdIdleAnimation", [{img: "bowtie"}, {img: "wing"}, {img: "beak"}]);
const objBowtie = object("bowtie", i1, [i2], DATA.length - 1);

// J. No clip in the build shows the whole of it: one state at a time.
const j1 = sprite("box_lid", 30, 20), j2 = sprite("box_body", 30, 30);
const j3 = sprite("box_ribbon", 20, 20);
clip("GiftBoxState4Tap", [{img: "box_lid", off: true}, {img: "box_body"},
                          {img: "box_ribbon", off: true}, {img: "sparkle"}]);
const objPartial = object("box", j2, [j1, j3], DATA.length - 1);

// K. The model view, which both pages share. The hub names the build a model came
//    from and a per-build page has no builds to name, so the same code has to do both.
const FIXTURE_MODELS = [{g: "Goliath", prefab_id: "p1", prefab_name: "Chair",
                         path: "Chair", object_name: "Seat", mesh_name: "SM_Seat",
                         mesh_bytes: 2048, skinned: false, render: null, tris: 900,
                         materials: [{name: "Wood", colour: {hex: "#8b5a2b"},
                                      textures: [{slot: "_BaseMap", name: "wood",
                                                  img: "wood.png"}]}]}];
// Two objects out of one scene file. They share `prefab_id`, because that names the
// file, and differ in `key`, which names the object. Matching on the file is what the
// panel used to do and would now gather the whole level.
FIXTURE_MODELS[0].key = 7;
FIXTURE_MODELS.push({g: "Goliath", prefab_id: "s9", prefab_name: "Level",
                     path: "Lamp", object_name: "Lamp", mesh_name: "SM_Lamp",
                     mesh_bytes: 512, skinned: false, render: null, tris: 40,
                     placements: 12, key: 8, materials: []});
FIXTURE_MODELS.push({g: "Goliath", prefab_id: "s9", prefab_name: "Level",
                     path: "Bench", object_name: "Bench", mesh_name: "SM_Bench",
                     mesh_bytes: 700, skinned: false, render: null, tris: 90,
                     placements: 3, key: 9, materials: []});
const FIXTURE_SCENES = [{g: "Goliath", id: "p1", name: "Chair", key: 7, render: "r.png",
                         parts: 1, tris: 900},
                        {g: "Goliath", id: "s9", name: "Lamp", from: "Level", key: 8,
                         placements: 12, render: "r.png", parts: 1, tris: 40}];

// G. A mechanic's art is mostly loose sprites that belong to no object.
const g1 = sprite("snowball_01", 30, 30);
object("snowball_01", g1, [], null);

"""

#: The assertions, once the page's own code is in scope.
CHECKS = r"""
// ---- checks ----------------------------------------------------------------
const PASSED = [], FAILED = [];
function check(label, got, want){
  const same = JSON.stringify(got) === JSON.stringify(want);
  (same ? PASSED : FAILED).push(label + ": got " + JSON.stringify(got) +
                                ", want " + JSON.stringify(want));
}

check("the idle wins over the destruction that draws the same art",
      objectClip(FIXTURE_OBJECTS[objIdle]).n, "idle_1");
check("a clip that is mostly this object beats a bigger one that includes it",
      objectClip(FIXTURE_OBJECTS[objShare]).n, "elephant_animation");

const woken = objectClip(FIXTURE_OBJECTS[objWoken]);
check("a rig the prefab ships switched off is drawn as authored",
      [woken.woken === true, clipShows(woken)], [true, 4]);
check("a rig that switches parts on purpose is left alone",
      wakeRig(DATA[dOn]), false);
check("and keeps the parts it deliberately hides hidden",
      clipShows(DATA[dOn]), 8);

check("overlapping parts read as one figure", figureCount(DATA[eOne]), 1);
check("figures standing apart are counted", figureCount(DATA[eThree]), 3);

// A body that bounces half a unit while a hat is parked fifty units away to hide it.
const camLayers = [{name: "body", size: [100, 100], chain: [0]},
                   {name: "eye", size: [10, 10], chain: [1]},
                   {name: "hat", size: [20, 20], chain: [2]}];
const camNodes = [{base: [0, 0, 1, 1], pos: [[0, 0, 0, 0], [1, 0, 0.5, 0]]},
                  {base: [0.2, 0.2, 1, 1]},
                  {base: [0, 0.6, 1, 1], pos: [[0, 0, 0.6, 0], [0.5, 50, 0.6, 0], [1, 50, 0.6, 0]]}];
const camFit = rigFit(camLayers, camNodes, 1);
check("the camera holds still while a part bounces and another is parked away",
      stageTransform(camLayers, camNodes, 0, 1, undefined, camFit.cam),
      stageTransform(camLayers, camNodes, 1, 1, undefined, camFit.cam));
check("and so does not follow", camFit.cam.follow, false);
// The same rig driven twenty units across as a whole.
const tripNodes = [{base: [0, 0, 1, 1], pos: [[0, 0, 0, 0], [1, 20, 0, 0]]},
                   {base: [0, 0, 1, 1]}, {base: [0.2, 0.2, 1, 1]}, {base: [0, 0.6, 1, 1]}];
const tripLayers = camLayers.map((layer, i) => ({...layer, chain: [0, i + 1]}));
const tripFit = rigFit(tripLayers, tripNodes, 1);
check("a rig that travels as a whole is followed", tripFit.cam.follow, true);
check("and stays in frame at the end of its trip",
      stageTransform(tripLayers, tripNodes, 1, 1, undefined, tripFit.cam)
        .includes("translate(-2"), true);

const poseObj = {n: "BeePrefab", w: null, p: [], c: null, a: null,
                 pose: "poses/7.png", cs: []};
check("a prefab object's card shows the prefab assembled",
      objectCard([poseObj, 0]).includes('src="poses/7.png"'), true);
const findBy = document.createElement("input");
findBy.id = "q"; findBy.value = "beeprefab"; document.body.appendChild(findBy);
check("a prefab object is found by its own name", objVisible(poseObj), true);
findBy.remove();
check("and its panel shows it as the final form",
      /final form[\s\S]*poses\/7\.png/.test(objectPanel(poseObj)), true);

const sheets = atlasSection(FIXTURE_OBJECTS[objSheets]);
check("only the cuts on the sheet being drawn are outlined",
      (sheets.match(/<i /g) || []).length, 1);
check("a rect is measured up from the bottom of the sheet",
      /top:90\.000%/.test(sheets.replace(/\s+/g, "")), true);
check("the caption counts the sheet's own sprites and this object's",
      /9 sprites packed, 1 of them/.test(sheets.replace(/\s+/g, " ")), true);

check("an object's members are its whole and its parts",
      objMembers(FIXTURE_OBJECTS[objSheets]).length, 2);
check("a clip with the object on screen beats one that ranks higher and hides it",
      objectClip(FIXTURE_OBJECTS[objRocket]).n, "VerticalFallEndAnimation");
check("one sprite in common is not a portrait",
      objectClip(FIXTURE_OBJECTS[objBowtie]), null);

check("a panel that can only show part of an object says which part",
      /holds 1 of the 3 sprites on screen/.test(
        objectPanel(FIXTURE_OBJECTS[objPartial]).replace(/\s+/g, " ")), true);

check("a model card names its mesh and its materials",
      /SM_Seat/.test(modelCard(FIXTURE_MODELS[0], 0)) &&
      /1 material/.test(modelCard(FIXTURE_MODELS[0], 0)), true);
check("with no builds to name, no build is named",
      /undefined/.test(modelCard(FIXTURE_MODELS[0], 0) +
                       modelPanel(FIXTURE_MODELS[0]) +
                       sceneCard(FIXTURE_SCENES[0], 0)), false);
check("an albedo stands for the surface where there is one",
      /wood\.png/.test(modelPanel(FIXTURE_MODELS[0])), true);
check("a scene's parts are the models of that scene in that build",
      /Chair/.test(scenePanel(FIXTURE_SCENES[0])), true);
check("an object out of a scene shows its own parts, not the whole file's",
      /SM_Lamp/.test(scenePanel(FIXTURE_SCENES[1])) &&
      /SM_Bench/.test(scenePanel(FIXTURE_SCENES[1])), false);
check("and says how often its scene places it",
      /placed 12/.test(sceneCard(FIXTURE_SCENES[1], 1)), true);
check("a prefab, placed once by definition, is not counted at the reader",
      /placed/.test(sceneCard(FIXTURE_SCENES[0], 0)), false);

const split = familyObjects([DATA[a1], DATA[a2], DATA[g1]]);
check("a family separates what comes apart from what does not",
      [split.sets.length, split.loose.map(d => d.n)], [1, ["snowball_01"]]);

check("a headless object's face is its largest piece",
      objHero({n: "x", w: null, p: [f1, sheetA], c: null, a: null}).n, "SheetA");

document.getElementById("out").textContent = "ASSETLAB_RESULT:" +
  JSON.stringify({passed: PASSED.length, failed: FAILED});
"""

PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<pre id="out">pending</pre>
<script>
// Installed in its own block so that whatever the block below does - throw at run
// time or fail to parse at all - the runner is told what happened rather than being
// left to guess from an empty page.
addEventListener("error", event => {
  document.getElementById("out").textContent = "ASSETLAB_RESULT:" + JSON.stringify({
    passed: 0, failed: ["the page threw: " + (event.message || event) +
                        " at line " + (event.lineno || "?")]});
});
</script>
<script>
__FIXTURES__
__RIG_ENGINE__
__SHARED_VIEWS__
__MODEL_VIEW__
__CHECKS__
</script></body></html>
"""


def run(browser: Path | None = None) -> dict:
    """Run the checks. -> {"passed": int, "failed": [str], "skipped": str | None}"""
    browser = browser or find_browser()
    if browser is None:
        return {"passed": 0, "failed": [],
                "skipped": "no Chromium found; point CHROME at one"}
    # The shared blocks go in exactly as the pages receive them, so these checks
    # exercise the code that ships rather than a copy of it.
    page = (PAGE.replace("__FIXTURES__", FIXTURES)
                .replace("__RIG_ENGINE__", RIG_ENGINE)
                .replace("__MODEL_VIEW__",
                         MODEL_VIEW.replace("__MODELS__", "FIXTURE_MODELS")
                                   .replace("__SCENES__", "FIXTURE_SCENES"))
                .replace("__SHARED_VIEWS__",
                         SHARED_VIEWS.replace("__OBJECTS__", "FIXTURE_OBJECTS"))
                .replace("__CHECKS__", CHECKS))
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "jstest.html"
        target.write_text(page, encoding="utf-8")
        try:
            done = subprocess.run(
                [str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
                 "--virtual-time-budget=8000", f"--user-data-dir={tmp}/profile",
                 "--dump-dom", target.as_uri()],
                capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as problem:
            return {"passed": 0, "failed": [], "skipped": f"{browser.name}: {problem}"}
    found = RESULT_RE.search(done.stdout)
    if not found:
        # A page that threw before reporting is a failure, not a skip: what threw is
        # the code under test.
        return {"passed": 0, "skipped": None,
                "failed": ["the page reported nothing; it threw before it could"]}
    result = json.loads(found.group(1))
    result["skipped"] = None
    return result


def main() -> int:
    outcome = run()
    for line in outcome["failed"]:
        print("FAIL", line)
    if outcome["skipped"]:
        print(f"browser: skipped - {outcome['skipped']}")
    else:
        print(f"browser: {outcome['passed']} passed, {len(outcome['failed'])} failed")
    return len(outcome["failed"])


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
