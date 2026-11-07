"""T-1633: scene.json, a 3D scene as plain data. fmscene validates it (every default filled in) and compiles it to an
ES module that builds the scene with three.js and keeps templates/scene.html's survival clauses."""
import copy
import json
import math
import os
import re
import shutil
import subprocess
import unittest

from helpers import ForemanTestCase

from sampo import scene as sscene

SCENE = {
    "background": "#14161C",
    "camera": {"fov": 28, "position": [0, 1.2, 6], "target": [0, 0, 0], "near": 1, "far": 20},
    "lights": [{"type": "hemisphere", "sky": "#f2e4d2", "ground": "#34425e", "intensity": 1.5},
               {"type": "directional", "color": "#fff0dc", "intensity": 2, "position": [-3, 4, 2]}],
    "materials": {"clay": {"color": "#c99c74", "roughness": 0.8}, "glow": {"emissive": "#86b6ff", "emissiveIntensity": 2}},
    "meshes": [{"name": "pebble", "geometry": {"type": "sphere", "radius": 1}, "material": "clay"},
               {"geometry": {"type": "vertices", "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]},
                "material": "glow", "rotation": [0, 90, 0]},
               {"geometry": {"type": "plane", "size": [8, 8]}, "material": "clay", "rotation": [-90, 0, 0]}],
    "animations": {"spin": {"target": "pebble", "property": "rotation", "times": [0, 4],
                            "values": [[0, 0, 0], [0, 360, 0]]}},
}


# Before <fm-scene> is defined the page's own CSS gives it its box, so the module loading shifts nothing.
PREDEFINED = ("fm-scene:not(:defined){display:block;position:relative;aspect-ratio:16/10}",
              "fm-scene:not(:defined)>[slot=poster]{width:100%;height:100%;object-fit:cover}")


def embedded(module):
    """The SCENE object the module carries, read back as JSON."""
    m = re.search(r"^export const SCENE = (\{.*?\});$", module, re.M | re.S)
    return json.loads(m.group(1))


# three.js and the browser, stubbed: enough for mount() to run and say what it did.
THREE_STUB = """
export const made = [];
class V { constructor() { this.v = [0, 0, 0]; } set(...v) { this.v = v; return this; } }
class O {
  constructor(...args) { this.args = args; this.position = new V(); this.rotation = new V(); this.scale = new V();
    this.kids = []; made.push(this); }
  add(o) { this.kids.push(o); } lookAt(...v) { this.look = v; } updateProjectionMatrix() { this.updated = true; }
  dispose() { this.disposed = true; } setAttribute(k, a) { this[k] = a; } setIndex(i) { this.index = i; }
  computeVertexNormals() { this.normals = true; }
}
export class Scene extends O {}
export class PerspectiveCamera extends O {}
export class Mesh extends O {}
export class BoxGeometry extends O {}
export class SphereGeometry extends O {}
export class PlaneGeometry extends O {}
export class BufferGeometry extends O {}
export class Float32BufferAttribute extends O {}
export class MeshStandardMaterial extends O {}
export class AmbientLight extends O {}
export class HemisphereLight extends O {}
export class DirectionalLight extends O {}
export class PointLight extends O {}
export const ACESFilmicToneMapping = 4;
export class WebGLRenderer extends O {
  debug = {}; frames = 0;
  setPixelRatio(d) { this.dpr = d; } setSize(w, h, css) { this.size = [w, h, css]; }
  setClearColor(c, a) { this.clear = [c, a]; } render() { this.frames++; }
  forceContextLoss() { this.released = true; this.args[0].canvas.lost = true; }
  compile() { if (globalThis.shaderFails) this.debug.onShaderError?.(this.args[0].context, "program"); }
}
"""
DRIVER = """
import { made } from "three";
const mode = process.argv[2], errors = [];
console.error = (...a) => errors.push(a.map(String).join(" "));
const events = () => { const on = {}; return {
  addEventListener(t, f, o) { (on[t] ??= new Set()).add(f); o?.signal?.addEventListener("abort", () => on[t].delete(f)); },
  fire(t, e = {}) { for (const f of on[t] ?? []) f(e); },
  live: () => Object.values(on).reduce((n, s) => n + s.size, 0) }; };
const reduce = Object.assign(events(), { matches: mode === "reduce" });
globalThis.matchMedia = q => q.includes("reduced-motion") ? reduce : Object.assign(events(), { matches: false });
globalThis.document = Object.assign(events(), { hidden: false });
globalThis.window = { devicePixelRatio: 3 };
globalThis.shaderFails = mode === "shader";
const queued = new Map(); let ids = 0;
globalThis.requestAnimationFrame = f => { queued.set(++ids, f); return ids; };
globalThis.cancelAnimationFrame = id => queued.delete(id);
const tick = now => { const fs = [...queued.values()]; queued.clear(); fs.forEach(f => f(now)); };
let ro, io;
globalThis.ResizeObserver = class { constructor(cb) { ro = this; this.cb = cb; } observe() {} disconnect() { this.off = true; } };
globalThis.IntersectionObserver = class { constructor(cb) { io = this; this.cb = cb; } observe() {} disconnect() { this.off = true; } };
// <fm-scene>'s platform, stubbed: an element whose open shadow root's markup makes the one canvas. A second
// attachShadow throws, as a browser's does once a root is there.
const defined = new Map(), shadow = mode => ({ mode, querySelector: () => fig.canvas ?? null,
                                              set innerHTML(h) { fig.canvas = makeCanvas(); } });
globalThis.customElements = { get: n => defined.get(n), define: (n, c) => defined.set(n, c) };
globalThis.HTMLElement = class { dataset = {}; attrs = {}; getAttribute(k) { return this.attrs[k] ?? null; }
  attachShadow(o) { if (this.shadowRoot) throw new Error("a shadow root is there already");
                    return this.shadowRoot = shadow(o.mode); } };
// A canvas keeps its one context for life, as a browser's does: once lost for good, only a new canvas draws again.
let fig = { dataset: { state: "poster" }, querySelector: () => fig.canvas };
const makeCanvas = (attrs = {}) => { const cv = Object.assign(events(), { clientWidth: 800, clientHeight: 500, lost: false,
  attrs, setAttribute(k, v) { cv.attrs[k] = v; }, removeAttribute(k) { delete cv.attrs[k]; },
  getContext(kind, attrs) { cv.asked = [kind, attrs]; return mode === "nogl" ? null : cv.gl ??= {
    canvas: cv, isContextLost: () => cv.lost, getProgramInfoLog: () => "ERROR: 0:1: oops",
    getExtension: () => ({ loseContext() { cv.lost = true; } }) }; },
  cloneNode: () => makeCanvas({ ...cv.attrs }), replaceWith(n) { fig.canvas = n; } }); return cv; };
fig.canvas = makeCanvas();
const { mount } = await import("./scene.mjs");
let start = () => mount(fig);
const element = mode === "component" || mode === "declarative";
if (element) {  // the element mounts on connect and frees on disconnect; the rest of the run is the same
  const el = fig = new (customElements.get("fm-scene"))();
  el.attrs.label = "A clay pebble";
  if (mode === "declarative") el.shadowRoot = shadow("open");  // the page's <template shadowrootmode>, no canvas
  start = () => { el.connectedCallback(); return () => el.disconnectedCallback(); };
}
const dispose = start(), canvas = fig.canvas, find = name => made.find(o => o.constructor.name === name);
const r = find("WebGLRenderer"), cam = find("PerspectiveCamera"), pebble = find("Mesh");
const out = { asked: canvas.asked, renderer: !!r };
await 0;  // what fail() queued runs now
out.early = { freed: made.filter(o => o.disposed).length, released: !!r?.released };
ro?.cb([]);
out.first = fig.dataset.state;
if (r) Object.assign(out, { dpr: r.dpr, size: r.size, clear: r.clear, camera: cam.args, aspect: cam.aspect,
                            at: cam.position.v, look: cam.look, tone: r.toneMapping });
tick(1000); tick(2000); tick(2500);
out.yaw = pebble?.rotation.v[1]; out.queued = queued.size;
let prevented = false;
canvas.lost = true; canvas.fire("webglcontextlost", { preventDefault() { prevented = true; } });
out.lost = [fig.dataset.state, prevented, queued.size];
canvas.lost = false; canvas.fire("webglcontextrestored");
out.restored = fig.dataset.state;
document.hidden = true; document.fire("visibilitychange"); out.hidden = queued.size;
document.hidden = false; document.fire("visibilitychange"); out.shown = queued.size;
io?.cb([{ isIntersecting: true }, { isIntersecting: false }]); out.offscreen = queued.size;  // a batch: the last counts
io?.cb([{ isIntersecting: false }, { isIntersecting: true }]); out.onscreen = queued.size;
dispose();
out.after = { state: fig.dataset.state, released: r ? r.released : canvas.lost, freed: made.filter(o => o.disposed).length,
              listeners: canvas.live() + document.live() + reduce.live(), observers: !ro || (ro.off && io.off) };
if (mode === "live" || element) {  // a second mount, after dispose() lost the first canvas's context for good
  const again = start(); ro.cb([]);
  out.remount = [fig.dataset.state, fig.canvas !== canvas, fig.canvas.lost]; again();
}
if (fig.shadowRoot) out.element = [fig.shadowRoot.mode, canvas.attrs["aria-label"], fig.canvas.attrs["aria-label"]];
if (element) {  // T-1706: label is watched: a new one reaches the canvas; taken away, the canvas hides and the twin speaks
  out.observed = customElements.get("fm-scene").observedAttributes;
  fig.attrs.label = "A wet pebble"; fig.attributeChangedCallback("label", "A clay pebble", "A wet pebble");
  const named = fig.canvas.attrs["aria-label"];
  delete fig.attrs.label; fig.attributeChangedCallback("label", "A wet pebble", null);
  out.relabel = [named, fig.canvas.attrs["aria-label"] ?? null, fig.canvas.attrs["aria-hidden"] ?? null];
  const bare = new (customElements.get("fm-scene"))();  // before it is connected (an upgrade sets attributes first)
  bare.attributeChangedCallback("label", null, "x"); out.early_label = "ok";
}
out.errors = errors;
console.log(JSON.stringify(out));
"""


class Three(ForemanTestCase):
    def test_the_module_holds_the_camera_it_was_given(self):
        module = sscene.compile_three(sscene.validate(copy.deepcopy(SCENE)))
        self.assertIn('import * as THREE from "three";', module)
        self.assertIn("export function mount(fig)", module)
        got = embedded(module)
        self.assertEqual(got["camera"], SCENE["camera"])
        self.assertEqual(got["background"], "#14161c", "colours come out as lowercase #rrggbb")
        self.assertEqual(got["materials"]["clay"]["color"], "#c99c74")
        for clause in ("failIfMajorPerformanceCaveat", "webglcontextlost", "preventDefault", "prefers-reduced-motion",
                       "IntersectionObserver", "visibilitychange", "forceContextLoss", "? 1.5 : 2"):
            self.assertIn(clause, module)
        self.assertEqual(module, sscene.compile_three(sscene.validate(copy.deepcopy(SCENE))), "the same bytes twice")
        self.assertNotIn("</", module, "pasted into a <script>, nothing in it closes the tag")
        self.assertTrue(module.isascii(), "no character an encoding mix-up could mangle")

    def test_the_latest_intersection_entry_counts(self):
        """A batch of entries comes oldest first: reading the first one leaves the loop running offscreen."""
        template = os.path.join(os.path.dirname(sscene.__file__), "..", "..", "skills", "playbooks", "references", "ui",
                                "templates", "scene.html")
        with open(template) as f:
            for name, text in (("scene.html", f.read()), ("the module", sscene.RUNTIME)):
                self.assertIn("IntersectionObserver(entries => { onscreen = entries.at(-1).isIntersecting;", text, name)

    def run_module(self, mode):
        d = os.path.join(self.tmp, mode)
        os.makedirs(os.path.join(d, "node_modules", "three"))
        emit = sscene.compile_component if mode in ("component", "declarative") else sscene.compile_three
        files = {"scene.mjs": emit(sscene.validate(copy.deepcopy(SCENE))), "drive.mjs": DRIVER,
                 "node_modules/three/package.json": '{"name": "three", "type": "module", "main": "index.js"}',
                 "node_modules/three/index.js": THREE_STUB}
        for name, text in files.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        p = subprocess.run(["node", "drive.mjs", mode], cwd=d, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    @unittest.skipUnless(shutil.which("node"), "no node here to run the module against a stubbed three.js")
    def test_the_module_survives_in_a_stubbed_browser(self):
        o = self.run_module("live")
        self.assertEqual(o["asked"][0], "webgl2")
        self.assertTrue(o["asked"][1]["failIfMajorPerformanceCaveat"], "a software renderer gets the poster")
        self.assertEqual((o["camera"], o["at"], o["look"]), ([28, 1, 1, 20], [0, 1.2, 6], [0, 0, 0]))
        self.assertEqual((o["dpr"], o["size"], o["aspect"]), (2, [800, 500, False], 1.6), "DPR capped at 2")
        self.assertEqual((o["clear"], o["tone"], o["first"]), (["#14161c", 1], 4, "live"))
        self.assertAlmostEqual(o["yaw"], 0.2 / 4 * 360 * 3.141592653589793 / 180, 6, "dt capped at 0.1 s a frame")
        self.assertEqual(o["queued"], 1, "an animated scene keeps its loop")
        self.assertEqual((o["lost"], o["restored"]), (["lost", True, 0], "live"))
        self.assertEqual((o["hidden"], o["shown"]), (0, 1), "no frames in a hidden tab")
        self.assertEqual((o["offscreen"], o["onscreen"]), (0, 1), "the latest entry of a batch decides")
        self.assertEqual(o["early"], {"freed": 0, "released": False})
        self.assertEqual(o["after"], {"state": "poster", "released": True, "freed": 6, "listeners": 0, "observers": True},
                         "3 geometries, 2 materials and the renderer freed, the context released")
        self.assertEqual(o["remount"], ["live", True, False], "a second mount draws on a fresh canvas")
        self.assertEqual(o["errors"], [])

    @unittest.skipUnless(shutil.which("node"), "no node here to run the module against a stubbed three.js")
    def test_reduced_motion_no_webgl_and_a_shader_error(self):
        o = self.run_module("reduce")
        self.assertEqual((o["first"], o["yaw"], o["queued"]), ("live", 0, 0), "one still frame, no loop")
        o = self.run_module("nogl")
        self.assertEqual((o["renderer"], o["first"], o["errors"]), (False, "poster", []), "the poster stays, no throw")
        o = self.run_module("shader")
        self.assertEqual((o["first"], o["restored"], o["queued"]), ("failed", "failed", 0))
        self.assertEqual(o["early"], {"freed": 6, "released": True}, "a failed scene frees all it made at once")
        self.assertEqual(len(o["errors"]), 1, "logged once")
        self.assertIn("oops", o["errors"][0])

    def test_fm_scene_compile(self):
        src, js = os.path.join(self.tmp, "scene.json"), os.path.join(self.tmp, "scene.js")
        with open(src, "w") as f:
            json.dump(SCENE, f)
        p = self.fm("scene", "compile", src, "--to", "three")
        self.assertEqual(embedded(p.stdout)["camera"]["fov"], 28)
        self.fm("scene", "compile", src, "--out", "scene.js", cwd=self.tmp)  # a bare name lands in the cwd
        with open(js) as f:
            self.assertEqual(f.read(), p.stdout)
        p = self.fm("scene", "compile", src, "--out", src, check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("is the scene it compiles", p.stderr)
        link = os.path.join(self.tmp, "link.js")
        os.symlink(src, link)
        p = self.fm("scene", "compile", src, "--out", link, check=False)  # a link to the source is the source
        self.assertEqual(p.returncode, 2)
        self.assertIn("is the scene it compiles", p.stderr)
        self.assertTrue(os.path.islink(link))
        with open(src) as f:
            self.assertEqual(json.load(f), SCENE, "the source is never overwritten")
        with open(src, "w") as f:
            f.write('{"camera": {"fov": 28, "position": [0, 0, 5], "fov": 30}}')
        p = self.fm("scene", "compile", src, check=False)
        self.assertEqual((p.returncode, p.stdout), (2, ""))
        self.assertIn("'fov' appears twice", p.stderr)
        fifo = os.path.join(self.tmp, "fifo.json")
        os.mkfifo(fifo)
        p = self.fm("scene", "compile", fifo, check=False)  # open() alone would wait for a writer forever
        self.assertEqual(p.returncode, 2)
        self.assertIn("not a regular file", p.stderr)


class Component(ForemanTestCase):
    """T-1634: --to component wraps the same module in <fm-scene>, a custom element mounting it in its shadow DOM."""
    def test_the_component_holds_the_same_camera_and_materials(self):
        scene = sscene.validate(copy.deepcopy(SCENE))
        three, component = sscene.compile_three(scene), sscene.compile_component(scene)
        self.assertEqual(embedded(component), embedded(three), "one SCENE: camera, materials and all")
        self.assertIn(sscene.RUNTIME, component, "mount() and its survival code whole, not a fork")
        for part in ('attachShadow({ mode: "open" })', '<slot name="poster">', "<canvas role=\"img\">",
                     ":host([data-state=\"live\"]) canvas", "prefers-reduced-motion: reduce",
                     'customElements.define("fm-scene", FmScene)', '::slotted([slot="poster"]) { object-fit: cover; }',
                     *PREDEFINED, "::part(stage)"):
            self.assertIn(part, component)
        playbook = os.path.join(os.path.dirname(sscene.__file__), "..", "..", "skills", "playbooks", "references", "ui",
                                "scene-3d.md")
        with open(playbook) as f:
            text = f.read()
        for part in ("--to component", *PREDEFINED, "::part(stage)", 'define("fm-scene-2", FmScene)'):
            self.assertIn(part, text, "the playbook says how to place the element without a layout shift")
        self.assertEqual(component, sscene.compile_component(sscene.validate(copy.deepcopy(SCENE))), "same bytes")
        self.assertNotIn("</", component, "pasted into a <script>, nothing in it closes the tag")
        self.assertTrue(component.isascii())
        src = os.path.join(self.tmp, "scene.json")
        with open(src, "w") as f:
            json.dump(SCENE, f)
        self.assertEqual(self.fm("scene", "compile", src, "--to", "component").stdout.rstrip("\n"),
                         component.rstrip("\n"))

    run_module = Three.run_module

    @unittest.skipUnless(shutil.which("node"), "no node here to run the element against a stubbed three.js")
    def test_the_element_survives_as_the_module_does(self):
        live, o = self.run_module("live"), self.run_module("component")
        self.assertEqual(o.pop("element"), ["open", "A clay pebble", "A clay pebble"],
                         "a shadow root; the label reaches the canvas, and the fresh one a remount makes")
        self.assertEqual((o.pop("observed"), o.pop("relabel"), o.pop("early_label")),
                         (["label"], ["A wet pebble", None, "true"], "ok"), "label is watched (T-1706)")
        self.assertEqual(o, live, "the same camera, frames, survival and freeing, the state on the element itself")
        o = self.run_module("declarative")
        self.assertEqual(o.pop("element"), ["open", "A clay pebble", "A clay pebble"])
        for k in ("observed", "relabel", "early_label"):
            o.pop(k)
        self.assertEqual(o, live, "a declarative shadow root with no canvas is filled in, not attached again")


class Tag(ForemanTestCase):  # T-1706
    def test_a_tag_names_the_element_so_two_scenes_share_a_page(self):
        scene = sscene.validate(copy.deepcopy(SCENE))
        two = sscene.compile_component(scene, tag="pebble-scene")
        self.assertIn('customElements.define("pebble-scene", FmScene)', two)
        self.assertIn("pebble-scene:not(:defined){display:block", two, "the page's box rule names it too")
        self.assertNotIn("fm-scene", two.replace(sscene.RUNTIME, ""), "nothing outside the shared runtime says fm-scene")
        self.assertEqual(sscene.compile_component(scene), sscene.compile_component(scene, tag="fm-scene"))
        for bad in ("pebble", "Pebble-scene", "1-scene", "font-face", "pebble-scène", 'a-"b', "a-b</script"):
            with self.assertRaises(ValueError, msg=bad):
                sscene.compile_component(scene, tag=bad)
        src = os.path.join(self.tmp, "scene.json")
        with open(src, "w") as f:
            json.dump(SCENE, f)
        self.assertEqual(self.fm("scene", "compile", src, "--to", "component", "--tag", "pebble-scene").stdout.rstrip("\n"),
                         two.rstrip("\n"))
        p = self.fm("scene", "compile", src, "--tag", "pebble-scene", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--tag names the element: use it with --to component", p.stderr)


class Schema(ForemanTestCase):
    def test_the_schema_states_what_a_second_renderer_must_not_guess(self):
        for fact in ("Rx * Ry * Rz", "T * R * S", "up is +Y", "looks down -Z", "negates view-space z",
                     "and reverses each triangle's winding",  # T-1693: or three's front faces are culled
                     "max(2, segments >> 1)", "decay 2", "smooth normals in three.js, flat"):
            self.assertIn(fact, " ".join(sscene.SCHEMA.split()))

    def test_defaults_are_filled_in(self):
        s = sscene.validate({"camera": {"fov": 30, "position": [0, 0, 5]}, "materials": {"m": {}},
                              "meshes": [{"geometry": {"type": "box"}, "material": "m"}]})
        self.assertEqual(s["camera"], {"fov": 30, "position": [0, 0, 5], "target": [0, 0, 0], "near": 0.1, "far": 100})
        self.assertEqual(s["background"], None)
        self.assertEqual(s["materials"]["m"], {"color": "#cccccc", "roughness": 0.8, "metalness": 0,
                                               "emissive": "#000000", "emissiveIntensity": 1})
        self.assertEqual(s["meshes"], [{"name": None, "geometry": {"type": "box", "size": [1, 1, 1]}, "material": "m",
                                        "position": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}])
        self.assertEqual((s["lights"], s["animations"]), ([], {}))
        s = sscene.validate(copy.deepcopy(SCENE))
        self.assertEqual(s["lights"][0], {"type": "hemisphere", "intensity": 1.5, "sky": "#f2e4d2", "ground": "#34425e"})
        self.assertEqual(s["meshes"][0]["geometry"], {"type": "sphere", "radius": 1, "segments": 32})
        self.assertTrue(s["animations"]["spin"]["loop"])

    def test_what_it_refuses_names_the_place(self):
        def bad(path, value):
            s = copy.deepcopy(SCENE)
            *head, last = path
            node = s
            for k in head:
                node = node[k]
            if value is KeyError:
                del node[last]
            else:
                node[last] = value
            return s
        cases = [
            (bad(["camera"], KeyError), "scene: camera is missing"),
            (bad(["camera", "fov"], 0), "camera.fov: wants a number at least 1 and at most 179, not 0"),
            (bad(["camera", "fov"], True), "camera.fov"),
            (bad(["camera", "fov"], 10 ** 400), "camera.fov"),
            (bad(["camera", "position"], [0, 0]), "camera.position: wants a list of 3 numbers"),
            (bad(["camera", "near"], 0.001), "camera.far: wants a number above near and at most 10,000 x near"),
            (bad(["camera", "target"], [0, 1.2, 6]), "camera.target"),
            (bad(["camera", "position"], [0, 6, 0]), "camera.target: straight below"),
            (bad(["camera", "target"], [0, 9, 6]), "camera.target: straight above"),
            (bad(["camera", "zoom"], 2), "camera: unknown key 'zoom'"),
            (bad(["background"], "red"), "background: wants a colour"),
            (bad(["lights", 0, "type"], ["spot"]), "lights[0]: wants a type"),
            (bad(["lights", 0, "color"], "#fff"), "lights[0]: unknown key 'color'"),
            (bad(["lights", 1, "position"], [0, 0, 0]), "lights[1].position"),
            (bad(["lights"], [{"type": "point"}] * 4), "lights: at most 3 directional and point lights"),
            (bad(["materials", "clay", "roughness"], 0), "materials.clay.roughness: wants a number above 0"),
            (bad(["materials", "__proto__"], {}), "materials: wants a name"),
            (bad(["meshes", 0, "material"], "stone"), "meshes[0].material: no material named stone"),
            (bad(["meshes", 0, "material"], ["clay"]), "meshes[0].material: wants a name"),
            (bad(["meshes", 0, "geometry", "type"], "torus"), "meshes[0].geometry: wants a type"),
            (bad(["meshes", 0, "geometry", "segments"], 2.5), "meshes[0].geometry.segments: wants an integer"),
            (bad(["meshes", 1, "geometry", "faces"], [[0, 1, 3]]), "meshes[1].geometry.faces[0]"),
            (bad(["meshes", 1, "geometry", "vertices"], [[0, 0, 0]]), "meshes[1].geometry.vertices: wants at least 3"),
            (bad(["meshes", 1, "name"], "pebble"), "meshes[1].name: pebble names an earlier mesh too"),
            (bad(["animations", "spin", "target"], "rock"), "animations.spin.target: no mesh named rock"),
            (bad(["animations", "spin", "times"], [0, 0]), "animations.spin.times: must rise"),
            (bad(["animations", "spin", "values"], [[0, 0, 0]]), "animations.spin.values: one per time"),
            (bad(["animations", "twice"], dict(SCENE["animations"]["spin"])), "animations.twice: spin moves"),
        ]
        for scene, want in cases:
            with self.subTest(want=want), self.assertRaises(ValueError) as e:
                sscene.validate(scene)
            self.assertIn(want, str(e.exception))

    def test_loads_refuses_what_json_lets_through(self):
        for text, want in [('{"camera": {"fov": NaN, "position": [0, 0, 5]}}', "NaN"),
                           ('{"camera": {"fov": 1e400, "position": [0, 0, 5]}}', "camera.fov"),
                           ("[" * 100000 + "]" * 100000, "nested too deeply")]:
            with self.subTest(want=want), self.assertRaises(ValueError) as e:
                sscene.loads(text)
            self.assertIn(want, str(e.exception))
        self.assertEqual(sscene.loads('﻿{"camera": {"fov": 30, "position": [0, 0, 5]}}')["camera"]["fov"], 30)


CARDS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sscene.__file__)))), "skills", "library", "cards",
                     "looks")


def plate(**geometry):
    """One red mesh in front of a camera on +Z, on black."""
    return sscene.validate({"camera": {"fov": 40, "position": [0, 0, 5]}, "background": "#000000",
                             "materials": {"red": {"color": "#ff0000"}},
                             "meshes": [{"geometry": geometry, "material": "red"}]})


def lit(w, h, rgb, cols, k):
    """How many pixels of tile k of a contact sheet aren't the black background."""
    tw, x0, y0 = w // cols, (k % cols) * (w // cols), 0
    return sum(rgb[3 * (y * w + x):3 * (y * w + x) + 3] != b"\0\0\0" for y in range(y0, y0 + h)
               for x in range(x0, x0 + tw))


class Offline(ForemanTestCase):
    """T-1530: 3D checks with no browser or GPU: a camera path, the --hud dev build, a turntable contact sheet drawn by
    fmraster, and GLSL checks of the shader cards."""

    def test_a_camera_path_moves_the_camera_and_keeps_its_aim(self):
        scene = copy.deepcopy(SCENE)
        fly = {"target": "camera", "property": "position", "times": [0, 4], "values": [[0, 1.2, 6], [6, 1.2, 0]]}
        scene["animations"]["fly"] = fly
        module = sscene.compile_three(sscene.validate(scene))
        self.assertIn('a.target === "camera" ? camera', module)
        self.assertIn("camera.lookAt(...c.target); };", module)  # every pose keeps the aim
        for bad, said in [({"fly": dict(fly, property="rotation")}, "a camera path moves the camera's position"),
                          ({"fly": dict(fly, target="drone")}, "no mesh named drone (or camera")]:
            with self.subTest(said=said), self.assertRaises(ValueError) as e:
                sscene.validate(dict(scene, animations=bad))
            self.assertIn(said, str(e.exception))
        named = copy.deepcopy(SCENE)
        named["meshes"][0]["name"] = "camera"
        named["animations"] = {}
        with self.assertRaises(ValueError) as e:
            sscene.validate(named)
        self.assertIn("camera names the camera", str(e.exception))

    def test_the_hud_is_only_in_a_hud_build(self):
        scene = sscene.validate(SCENE)
        plain, hud = sscene.compile_three(scene), sscene.compile_three(scene, hud=True)
        self.assertNotIn("__fmScene", plain)
        self.assertIn("hud(renderer, fig, performance.now() - t0); }", hud)
        self.assertIn("globalThis.__fmScene", hud)
        self.assertIn("globalThis.__fmScene", sscene.compile_component(scene, hud=True))
        if shutil.which("node"):  # the dev build still parses as a module
            path = os.path.join(self.tmp, "hud.mjs")
            with open(path, "w", encoding="utf-8") as f:
                f.write(hud)
            p = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_turntable_shows_front_faces_and_culls_back_ones(self):
        w, h, rgb = sscene.turntable(plate(type="plane", size=[2, 1]), frames=2, size=(40, 30), cols=2)
        self.assertEqual((w, h, len(rgb)), (80, 30, 80 * 30 * 3))
        self.assertGreater(lit(w, h, rgb, 2, 0), 100)  # facing the camera: drawn
        self.assertEqual(lit(w, h, rgb, 2, 1), 0)  # turned half round: its back, culled as three.js culls it
        w, h, rgb = sscene.turntable(plate(type="box", size=[2, 1, 0.2]), frames=4, size=(40, 30), cols=4)
        side, front, back = lit(w, h, rgb, 4, 1), lit(w, h, rgb, 4, 0), lit(w, h, rgb, 4, 2)
        self.assertGreater(front, 3 * side)  # a slab face-on, then edge-on
        self.assertLess(abs(front - back), front // 10)
        ball = sscene.turntable(plate(type="sphere", radius=1, segments=12), frames=1, size=(40, 30), cols=1)
        self.assertGreater(lit(*ball, 1, 0), 100)  # outward faces, whatever order the grid made them in

    def test_fm_scene_check_and_turntable(self):
        path = os.path.join(self.tmp, "scene.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(SCENE, f)
        got = json.loads(self.fm("scene", "check", path, "--json").stdout)
        self.assertEqual((got["meshes"], got["lights"], got["animations"]), (3, 2, 1))
        self.assertEqual(got["triangles"], 32 * 16 * 2 - 2 * 32 + 1 + 2)  # sphere (no empty pole faces), 1, plane
        out = os.path.join(self.tmp, "sheet.png")
        self.fm("scene", "turntable", path, "--out", out, "--frames", "2")
        from sampo import img
        info = img.info(out)
        self.assertEqual((info["width"], info["height"]), (320, 100))
        p = self.fm("scene", "turntable", path, check=False)
        self.assertNotEqual(p.returncode, 0, "it writes a PNG: --out")

    def test_cells_draw_the_scene_as_half_blocks(self):  # T-1635
        lines = sscene.cells(plate(type="plane", size=[2, 1]), cols=40)
        self.assertEqual(len(lines), 12)  # 40 x 25 pixels, rounded down to even: 24 rows of pixels, 12 of cells
        for ln in lines:
            self.assertEqual(ln.count("▀"), 40)
            self.assertTrue(ln.endswith("\x1b[0m"))
        text = "".join(lines)
        self.assertIn("\x1b[38;2;", text)
        self.assertNotEqual(text.count("48;2;0;0;0m"), 40 * 12, "the plate is drawn, not only background")
        self.assertEqual(lines, sscene.cells(plate(type="plane", size=[2, 1]), cols=40))  # integer raster: same bytes
        back = "".join(sscene.cells(plate(type="plane", size=[2, 1]), cols=40, turn=180))
        self.assertEqual(set(re.findall(r"[34]8;2;([0-9;]+)m", back)), {"0;0;0"})  # its back, culled
        self.assertEqual(len(sscene.cells(plate(type="plane", size=[2, 1]), cols=2)), 1)  # never under one row
        with self.assertRaises(ValueError):
            sscene.cells(plate(type="plane", size=[2, 1]), cols=1)

    def test_fm_scene_cells(self):  # T-1635
        path = os.path.join(self.tmp, "scene.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(SCENE, f)
        out = self.fm("scene", "cells", path, "--cols", "30").stdout
        self.assertEqual(out.count("▀"), 30 * 9)
        self.assertNotEqual(self.fm("scene", "cells", path, "--cols", "500", check=False).returncode, 0)

    def test_the_shipped_shader_cards_pass(self):
        paths = sscene.shader_paths(CARDS)
        self.assertGreaterEqual(len(paths), 8)
        validator = shutil.which("glslangValidator")
        self.assertEqual([x for p in paths for x in sscene.shader_problems(p, validator)], [])

    def test_a_card_whose_code_and_table_disagree(self):
        folder = os.path.join(self.tmp, "shaders")
        os.makedirs(folder)
        card = os.path.join(folder, "shader-x.md")
        with open(card, "w", encoding="utf-8") as f:
            f.write("## Shader\n```glsl\n#version 300 es\nprecision highp float;\nuniform float uA;\nout vec4 o;\n"
                    "void main() { o = vec4(uA); }\n```\n\n## Uniforms\n| Uniform | Type |\n|---|---|\n| `uB` | float |\n")
        found = sscene.shader_problems(card)
        self.assertIn("shader-x.md: the Uniforms table lists uB, which no block declares", found)
        self.assertIn("shader-x.md: uA is declared but not in the Uniforms table", found)
        bad = os.path.join(folder, "broken.frag")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("#version 300 es\nprecision highp float;\nout vec4 o;\nvoid main() { o = vec4(1.0) }\n")
        validator = shutil.which("glslangValidator")
        if validator:
            self.assertTrue(any("doesn't compile as GLSL (frag)" in x for x in sscene.shader_problems(bad, validator)))
        p = self.fm("scene", "shader", folder, check=False)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("2 shader file(s)", p.stdout)



def m4(pos=(0, 0, 0), rot=(0, 0, 0), scale=(1, 1, 1)):
    """three.js's Matrix4.compose: T * Rx * Ry * Rz * S, column-major as toJSON writes it."""
    import math
    x, y, z = (math.radians(a) for a in rot)
    rx = [[1, 0, 0], [0, math.cos(x), -math.sin(x)], [0, math.sin(x), math.cos(x)]]
    ry = [[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]]
    rz = [[math.cos(z), -math.sin(z), 0], [math.sin(z), math.cos(z), 0], [0, 0, 1]]
    mul = lambda a, b: [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    r = mul(mul(rx, ry), rz)
    return [v for j in range(3) for v in [r[i][j] * scale[j] for i in range(3)] + [0]] + [*pos, 1]


def three_doc():
    """A scene as three.js's Object3D.toJSON() writes it (format 4)."""
    tri = {"itemSize": 3, "type": "Float32Array", "array": [0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0], "normalized": False}
    return {
        "metadata": {"version": 4.6, "type": "Object", "generator": "Object3D.toJSON"},
        "geometries": [
            {"uuid": "g-box", "type": "BoxGeometry", "width": 2, "height": 1, "depth": 0.5},
            {"uuid": "g-ball", "type": "SphereGeometry", "radius": 0.5, "widthSegments": 16, "heightSegments": 8},
            {"uuid": "g-floor", "type": "PlaneGeometry", "width": 10, "height": 6},
            {"uuid": "g-quad", "type": "BufferGeometry",
             "data": {"attributes": {"position": tri}, "index": {"type": "Uint16Array", "array": [0, 1, 2, 2, 1, 3]}}},
            {"uuid": "g-soup", "type": "BufferGeometry", "data": {"attributes": {"position": dict(tri, array=tri["array"][:9])}}},
            {"uuid": "g-knot", "type": "TorusKnotGeometry", "radius": 1},
        ],
        "materials": [
            {"uuid": "m-red", "type": "MeshStandardMaterial", "name": "red", "color": 0xFF0000, "roughness": 0.4,
             "metalness": 0.2, "emissive": 0x110000, "emissiveIntensity": 2},
            {"uuid": "m-flat", "type": "MeshBasicMaterial", "color": 0x336699},
        ],
        "object": {"uuid": "s", "type": "Scene", "background": 0x101418, "matrix": m4(), "children": [
            {"uuid": "grp", "type": "Group", "matrix": m4((1, 0, 0)), "children": [
                {"uuid": "crate", "type": "Mesh", "name": "crate", "geometry": "g-box", "material": "m-red",
                 "matrix": m4((1, 0, 0), (0, 90, 0))}]},
            {"uuid": "ball", "type": "Mesh", "name": "crate", "geometry": "g-ball", "material": "m-flat",
             "matrix": m4((0, 1, 0), scale=(2, 2, 2))},  # a second "crate": renamed, names are unique
            {"uuid": "floor", "type": "Mesh", "geometry": "g-floor", "material": "m-flat", "matrix": m4(rot=(-90, 0, 0))},
            {"uuid": "quad", "type": "Mesh", "geometry": "g-quad", "material": "m-red", "matrix": m4()},
            {"uuid": "soup", "type": "Mesh", "geometry": "g-soup", "material": ["m-red", "m-flat"], "matrix": m4()},
            {"uuid": "knot", "type": "Mesh", "geometry": "g-knot", "material": "m-red", "matrix": m4()},
            {"uuid": "wire", "type": "Line", "geometry": "g-soup", "material": "m-red", "matrix": m4()},
            {"uuid": "amb", "type": "AmbientLight", "color": 0xFFFFFF, "intensity": 0.5, "matrix": m4()},
            {"uuid": "sun", "type": "DirectionalLight", "color": 0xFFEEDD, "intensity": 2, "matrix": m4((3, 4, 5)),
             "target": "t"},
            {"uuid": "cam", "type": "PerspectiveCamera", "fov": 40, "zoom": 1, "near": 0.1, "far": 50, "aspect": 1.5,
             "matrix": m4((0, 0, 8))},
        ]}}


class Import(ForemanTestCase):
    """T-1847: fm scene import, a three.js Object3D.toJSON() scene to scene.json."""

    def near(self, a, b):
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, places=4)

    def test_from_three_carries_what_scene_json_holds_and_names_the_rest(self):
        scene, left = sscene.from_three(three_doc())
        self.assertEqual(sscene.validate(scene), scene, "the import is a valid scene.json")
        m = {x["name"]: x for x in scene["meshes"]}
        crate = m["crate"]
        self.near(crate["position"], [2, 0, 0])  # the group's move composed with its own
        self.near(crate["rotation"], [0, 90, 0])
        self.assertEqual(crate["geometry"], {"type": "box", "size": [2, 1, 0.5]})
        self.assertEqual(scene["materials"][crate["material"]],
                         {"color": "#ff0000", "roughness": 0.4, "metalness": 0.2, "emissive": "#110000",
                          "emissiveIntensity": 2})
        ball = next(x for x in scene["meshes"] if x["geometry"]["type"] == "sphere")
        self.assertNotEqual(ball["name"], "crate")
        self.near(ball["scale"], [2, 2, 2])
        self.assertEqual(ball["geometry"], {"type": "sphere", "radius": 0.5, "segments": 16})
        floor = next(x for x in scene["meshes"] if x["geometry"]["type"] == "plane")
        self.near(floor["rotation"], [-90, 0, 0])
        self.assertEqual(floor["geometry"]["size"], [10, 6])
        quad, soup = [x["geometry"] for x in scene["meshes"] if x["geometry"]["type"] == "vertices"]
        self.assertEqual(quad["faces"], [[0, 1, 2], [2, 1, 3]])
        self.assertEqual(soup["faces"], [[0, 1, 2]], "no index: each three vertices a triangle")
        self.assertEqual(scene["background"], "#101418")
        self.assertEqual([lt["type"] for lt in scene["lights"]], ["ambient", "directional"])
        self.near(scene["lights"][1]["position"], [3, 4, 5])
        self.assertEqual(scene["camera"]["fov"], 40)
        self.near(scene["camera"]["position"], [0, 0, 8])
        self.near(scene["camera"]["target"], [0, 0, 0])
        joined = " ".join(left)
        for word in ("TorusKnotGeometry", "Line", "second material"):
            self.assertIn(word, joined)

    def test_rotations_survive_the_matrix(self):
        for rot in ((30, 45, 60), (-120, 10, 170), (0, -90, 0)):
            doc = {"metadata": {"version": 4.6}, "geometries": [{"uuid": "g", "type": "BoxGeometry"}],
                   "materials": [{"uuid": "m", "type": "MeshStandardMaterial", "color": 0}],
                   "object": {"type": "Scene", "matrix": m4(), "children": [
                       {"type": "Mesh", "geometry": "g", "material": "m", "matrix": m4((1, 2, 3), rot, (1, 2, 3))}]}}
            scene, _ = sscene.from_three(doc)
            got = sscene.triangles(scene)
            want = sscene.triangles(dict(scene, meshes=[dict(scene["meshes"][0], rotation=list(rot),
                                                               position=[1, 2, 3], scale=[1, 2, 3])]))
            self.assertEqual(got, want, rot)
            self.near(scene["camera"]["position"], [0, 0, 5])  # no camera in the file: three's default

    def test_fm_scene_import_writes_a_scene_fm_scene_checks(self):
        src, out = os.path.join(self.tmp, "three.json"), os.path.join(self.tmp, "scene.json")
        with open(src, "w") as f:
            json.dump(three_doc(), f)
        r = self.fm("scene", "import", src, "--out", out)
        self.assertIn("left out", r.stdout)
        self.assertEqual(self.fm("scene", "check", out).returncode, 0)
        printed = self.fm("scene", "import", src).stdout
        self.assertEqual(sscene.loads(printed), sscene.load(out))
        with open(src, "w") as f:
            f.write('{"metadata": {"version": 4.6}, "object": {"type": "Mesh"}}')
        self.assertNotEqual(self.fm("scene", "import", src, check=False).returncode, 0)

    def test_a_top_down_camera_and_a_mesh_named_camera_still_validate(self):
        doc = three_doc()
        kids = doc["object"]["children"]
        kids[1]["name"] = "camera"  # scene.json keeps that name for a camera path
        next(k for k in kids if k["type"] == "PerspectiveCamera")["matrix"] = m4((0, 8, 0), (-90, 0, 0))
        scene, left = sscene.from_three(doc)
        self.assertNotIn("camera", [x["name"] for x in scene["meshes"]])
        self.near(scene["camera"]["position"], [0, 8, 0])
        self.assertLess(scene["camera"]["target"][1], 1)
        self.assertIn("straight down", " ".join(left))


    def test_odd_input_is_a_note_or_a_value_error_never_a_crash(self):
        doc = three_doc()
        kids = doc["object"]["children"]
        kids.append({"type": "\x1b[2J", "matrix": m4()})  # a terminal escape as a type: shown escaped
        kids.append({"type": "Mesh", "geometry": "g-box", "material": "m-red", "visible": False, "matrix": m4()})
        kids.append({"type": "Mesh", "name": "mirror", "geometry": "g-box", "material": "m-red",
                     "matrix": m4((0, 0, 1), (0, 30, 0), (-1, 1, 1))})
        kids.append({"type": "Mesh", "name": "Cube.001", "geometry": "g-box", "material": "m-red", "matrix": m4()})
        kids += [{"type": "DirectionalLight", "matrix": m4((i + 1, 1, 0))} for i in range(3)]  # a 4th direct light
        kids.append({"type": "DirectionalLight", "matrix": m4()})  # at the origin: refused, so a note
        cam = next(k for k in kids if k["type"] == "PerspectiveCamera")
        cam.update(zoom=2, far=5000, matrix=m4((0, 0, 8), (0, 0, 20)))  # rolled 20 degrees
        scene, left = sscene.from_three(doc)
        joined = " ".join(left)
        self.assertNotIn("\x1b", joined)
        self.assertIn("\\x1b[2J", joined)
        for word in ("hidden", "past the third", "roll", "MeshBasicMaterial", "renamed", "far 5000"):
            self.assertIn(word, joined)
        m = {x["name"]: x for x in scene["meshes"]}
        self.assertEqual(len(scene["meshes"]), 7, "the hidden mesh isn't drawn")
        got = sscene.triangles(dict(scene, meshes=[m["mirror"]]))
        want = sscene.triangles(dict(scene, meshes=[dict(m["mirror"], position=[0, 0, 1], rotation=[0, 30, 0],
                                                          scale=[-1, 1, 1])]))
        self.assertEqual(got, want, "a mirrored matrix round-trips")
        self.assertAlmostEqual(scene["camera"]["fov"], 2 * math.degrees(math.atan(math.tan(math.radians(20)) / 2)),
                               places=4)
        self.assertEqual(scene["camera"]["far"], 1000)
        for bad in ({"fov": "50"}, {"color": None}):  # a field of the wrong kind: ValueError, not a traceback
            d = three_doc()
            target = next(k for k in d["object"]["children"] if k["type"] == "PerspectiveCamera") \
                if "fov" in bad else d["materials"][0]
            target.update(bad)
            with self.assertRaises(ValueError):
                sscene.from_three(d)
        d = three_doc()  # one big geometry shared by many meshes: bounded, not multiplied
        d["geometries"][3]["data"]["attributes"]["position"]["array"] = [0.0] * 3 * 30000
        d["object"]["children"] += [{"type": "Mesh", "geometry": "g-quad", "material": "m-red", "matrix": m4()}] * 40
        with self.assertRaises(ValueError) as e:
            sscene.from_three(d)
        self.assertIn("vertices", str(e.exception))


def vol6(v, f):
    return sum(sscene._cross3(v[j], v[k])[0] * v[i][0] + sscene._cross3(v[j], v[k])[1] * v[i][1]
               + sscene._cross3(v[j], v[k])[2] * v[i][2] for i, j, k in f)


class Metaballs(ForemanTestCase):
    """T-1848: fm scene metaballs, a seeded blob polygonised by marching tetrahedra."""

    def blob(self, scene):
        g = next(m for m in scene["meshes"] if m["name"] == "blob")["geometry"]
        return g["vertices"], g["faces"]

    def test_the_blob_is_closed_wound_outward_and_seeded(self):
        import collections
        a = sscene.metaballs(balls=4, grid=16, seed=3)
        self.assertEqual(sscene.validate(a), a)
        v, f = self.blob(a)
        directed = collections.Counter((t[i], t[(i + 1) % 3]) for t in f for i in range(3))
        self.assertEqual(max(directed.values()), 1)  # no edge twice the same way: wound alike
        self.assertTrue(all((y, x) in directed for x, y in directed))  # every edge has its twin: closed
        self.assertGreater(vol6(v, f), 0)  # counter-clockwise seen from outside
        self.assertEqual(sscene.metaballs(balls=4, grid=16, seed=3), a)
        self.assertNotEqual(sscene.metaballs(balls=4, grid=16, seed=4), a)
        self.assertGreater(len(sscene.metaballs(4, 32, 3)["meshes"][0]["geometry"]["faces"]), len(f))
        for bad in ({"balls": 0}, {"balls": 13}, {"grid": 7}, {"grid": 49}):
            with self.assertRaises(ValueError):
                sscene.metaballs(**bad)

    def test_one_ball_is_a_sphere_of_its_radius(self):
        v, f = self.blob(sscene.metaballs(balls=1, grid=32, seed=5))
        mid = [(min(p[i] for p in v) + max(p[i] for p in v)) / 2 for i in range(3)]
        d = [math.dist(p, mid) for p in v]
        self.assertLess(max(d) / min(d), 1.04)
        self.assertAlmostEqual(vol6(v, f) / 6, 4 / 3 * math.pi * (sum(d) / len(d)) ** 3, delta=0.05 * vol6(v, f) / 6)

    def test_fm_scene_metaballs_prints_or_writes_a_scene_fm_scene_checks(self):
        out = os.path.join(self.tmp, "blob.json")
        flags = ("--balls", "3", "--grid", "12", "--seed", "1")
        self.assertIn("wrote", self.fm("scene", "metaballs", *flags, "--out", out).stdout)
        self.assertEqual(self.fm("scene", "check", out).returncode, 0)
        self.assertEqual(sscene.loads(self.fm("scene", "metaballs", *flags).stdout), sscene.load(out))
        self.assertNotEqual(self.fm("scene", "metaballs", "--grid", "4", check=False).returncode, 0)
        for action in ("check", "shader", "soak", "compile", "metaballs"):  # the others need FILE; metaballs takes none
            r = self.fm("scene", action, *(["x.json"] if action == "metaballs" else []), check=False)
            self.assertEqual((r.returncode, "Traceback" in r.stderr), (2, False), r.stderr)

    def test_a_flag_the_action_does_not_read_is_refused_naming_who_reads_it(self):
        # T-1855: metaballs --days used to run and drop --days without a word
        for args, said in ((["metaballs", "--days", "5"], "terrain, airlock"), (["terrain", "--balls=3"], "metaballs"),
                           (["compile", "x.json", "--size", "2x2"], "shade")):
            r = self.fm("scene", *args, check=False)
            self.assertEqual((r.returncode, said in r.stderr), (2, True), r.stderr)


class Airlock(ForemanTestCase):
    """T-1849: fm scene airlock, what the guard held and let through as boxes either side of a gate."""

    def test_airlock_stands_a_box_per_held_category_beside_a_gate(self):
        s = sscene.airlock({"core": 100, "brief": 1, "rm-outside": 0, "Bad name!": 3, "floor": 2}, 50)
        self.assertEqual(sscene.validate(s), s)
        box = {m["name"]: m for m in s["meshes"] if m["geometry"]["type"] == "box"}
        self.assertEqual(set(box), {"pillar-l", "pillar-r", "lintel", "through", "core", "brief", "other"})
        h = {n: m["geometry"]["size"][1] for n, m in box.items()}
        self.assertAlmostEqual(h["core"], 2.1)  # the most held stands tallest; nothing held, no box
        self.assertTrue(0.1 < h["brief"] < h["other"] < h["through"] < h["core"])  # other: Bad name! and floor
        self.assertTrue(all(box[n]["position"][0] < 0 < box["through"]["position"][0] for n in ("core", "brief")))
        self.assertAlmostEqual(box["core"]["position"][1], h["core"] / 2)  # standing on the floor
        for bad in (({"core": -1}, 0), ({}, -1)):
            with self.assertRaises(ValueError):
                sscene.airlock(*bad)

    def test_guard_counts_reads_this_projects_blocks_and_tool_calls(self):
        from sampo import core as c
        p = c.find_project(self.repo, create=True)
        for cat in ("core", "core", "brief"):
            c.log_event(p, "guard_block", data={"category": cat})
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for ts, kind, proj in ((c.now(), "tool", p.slug), (c.now(), "tool_fail", p.slug),
                                   (c.now(), "tool", "elsewhere"), ("2020-01-01T00:00:00Z", "tool", p.slug),
                                   (c.now(), "stop", p.slug)):
                f.write(json.dumps({"ts": ts, "kind": kind, "project": proj}) + "\n")
        self.assertEqual(sscene.guard_counts(p, 30), ({"core": 2, "brief": 1}, 2))

    def test_fm_scene_airlock_prints_a_scene_fm_scene_checks(self):
        from sampo import core as c
        c.find_project(self.repo, create=True)
        out = os.path.join(self.tmp, "airlock.json")
        self.assertIn("wrote", self.fm("scene", "airlock", "--days", "7", "--out", out).stdout)
        self.assertEqual(self.fm("scene", "check", out).returncode, 0)
        r = self.fm("scene", "airlock", "--days", "0", check=False)  # a refusal, not a traceback
        self.assertEqual((r.returncode, "1 to 3650" in r.stderr), (2, True), r.stderr)
        other = os.path.join(self.tmp, "plain")
        os.makedirs(other)
        r = self.fm("scene", "airlock", cwd=other, check=False)
        self.assertEqual((r.returncode, "not in a Sampo project" in r.stderr), (2, True), r.stderr)


class BenchScene(ForemanTestCase):
    """T-1850: fm scene bench, fm code bench's runs as a diorama."""

    def test_bench_stands_a_box_per_run_and_seed(self):
        runs = [{"results": [{"seed": "operator", "closed": True, "seconds": 20}]},
                {"results": [{"seed": "operator", "closed": False, "seconds": 400},
                             {"seed": "fix-bug", "closed": True, "seconds": 0}]}]
        s = sscene.bench(runs)
        self.assertEqual(sscene.validate(s), s)
        box = {m["name"]: m for m in s["meshes"] if m["name"] != "floor"}
        self.assertEqual(set(box), {"operator-1", "operator-2", "fix-bug-2"})
        colour = {n: s["materials"][m["material"]]["color"] for n, m in box.items()}
        self.assertEqual(colour["operator-1"], colour["fix-bug-2"])  # closed
        self.assertNotEqual(colour["operator-1"], colour["operator-2"])  # not closed
        h = {n: m["geometry"]["size"][1] for n, m in box.items()}
        self.assertAlmostEqual(h["operator-2"], 2.1)  # the slowest stands tallest
        self.assertAlmostEqual(h["fix-bug-2"], 0.1)
        self.assertLess(box["operator-1"]["position"][0], box["operator-2"]["position"][0])  # runs left to right
        self.assertNotEqual(box["operator-2"]["position"][2], box["fix-bug-2"]["position"][2])  # seeds front to back
        self.assertEqual(sum(m["name"].startswith("operator-") for m in sscene.bench(runs * 10)["meshes"]), 12)
        skip = [{"results": [{"seed": "rm-rf", "closed": False, "skipped": True}]}]
        self.assertNotIn("rm-rf-3", {m["name"] for m in sscene.bench(runs + skip)["meshes"]})  # skipped: no box
        for empty in ([], skip):
            with self.assertRaises(ValueError):
                sscene.bench(empty)
        odd = [{"results": [{"seed": "a b", "closed": True, "seconds": 10 ** 400},
                            {"seed": "a-b", "closed": True, "seconds": [1]},
                            {"seed": "9lives", "closed": True, "seconds": float("inf")}, "x"]}, {"results": 5}]
        self.assertEqual({m["name"] for m in sscene.bench(odd)["meshes"]},  # a hand-edited history: drawn, no crash
                         {"a-b-1", "a-b-1_2", "s9lives-1", "floor"})

    def test_fm_scene_bench_reads_the_bench_history(self):
        from sampo import core as c
        r = self.fm("scene", "bench", check=False)  # no runs yet: a refusal, not a traceback
        self.assertEqual((r.returncode, "no fm code bench results" in r.stderr), (2, True), r.stderr)
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), "code-bench.jsonl"), "w") as f:
            f.write(json.dumps({"score": 1.0, "results": [{"seed": "operator", "closed": True, "seconds": 22.2}]}) + "\n")
        out = os.path.join(self.tmp, "bench.json")
        self.assertIn("wrote", self.fm("scene", "bench", "--out", out).stdout)
        self.assertEqual(self.fm("scene", "check", out).returncode, 0)


class Lookbook(ForemanTestCase):
    """T-1852: fm scene lookbook, the look cards hung as colour-field panels in a 3D gallery."""

    def test_lookbook_hangs_a_frame_per_look_banded_in_its_palette(self):
        known = {"look-a": {"palette": [(255, 0, 0), (0, 0, 255)]}, "look-b": {"palette": [(0, 255, 0)]},
                 "look-c": {"palette": []}}
        s = sscene.lookbook(["look-b", "look-a", "look-c"], known)
        self.assertEqual(sscene.validate(s), s)
        mesh = {m["name"]: m for m in s["meshes"]}
        self.assertLessEqual({"look-a", "look-b", "look-c", "look-a-1", "look-a-2", "look-b-1", "floor"}, set(mesh))
        self.assertNotIn("look-c-1", mesh)  # no palette, an empty frame
        colour = {n: s["materials"][m["material"]]["color"] for n, m in mesh.items()}
        self.assertEqual([colour[n] for n in ("look-a-1", "look-a-2", "look-b-1")], ["#ff0000", "#0000ff", "#00ff00"])
        self.assertGreater(mesh["look-a-1"]["position"][1], mesh["look-a-2"]["position"][1])  # bands top to bottom
        self.assertLess(abs(mesh["look-b"]["position"][0]), abs(mesh["look-c"]["position"][0]))  # best ranked: centre
        self.assertEqual(len([n for n in mesh if n.count("-") == 1 and n.startswith("look-")]), 3)
        with self.assertRaises(ValueError):
            sscene.lookbook([], known)
        with self.assertRaises(ValueError):
            sscene.lookbook(["look-a"] * 13, known)

    def test_fm_scene_lookbook_hangs_the_cards(self):
        out = os.path.join(self.tmp, "lookbook.json")
        self.assertIn("3 look(s)", self.fm("scene", "lookbook", "--count", "3", "--out", out).stdout)
        self.assertEqual(self.fm("scene", "check", out).returncode, 0)
        for n in ("13", "0"):
            r = self.fm("scene", "lookbook", "--count", n, check=False)
            self.assertEqual((r.returncode, "1 to 12" in r.stderr), (2, True), r.stderr)

    def test_lookbook_frames_hang_flush_on_the_walls_all_in_view(self):
        known = {f"look-{i}": {"palette": [(9, 9, 9)]} for i in range(12)}
        s = sscene.lookbook(list(known), known)
        cx, _, cz = s["camera"]["position"]
        for n in known:
            m = next(m for m in s["meshes"] if m["name"] == n)
            (x, _, z), (sx, _, sz) = m["position"], m["geometry"]["size"]
            self.assertAlmostEqual(max(abs(x), abs(z)), 5.95, msg=n)  # its back on the wall's inner face, 6.0
            for ex in (-sx / 2, sx / 2):  # every corner inside the 8:5 view at fov 60, 42.7 degrees each side
                for ez in (-sz / 2, sz / 2):
                    self.assertLess(math.degrees(math.atan2(abs(x + ex - cx), cz - z - ez)), 42, n)


class Terrain(ForemanTestCase):
    """T-1851: fm scene terrain, the repo's churn as a ribbon per busiest path."""

    def test_terrain_draws_a_ribbon_per_row_as_tall_as_its_churn(self):
        s = sscene.terrain([[0, 0, 0], [1, 100, 0], [3, 0, 0]], ["plugin", ".github", "README.md"])
        self.assertEqual(sscene.validate(s), s)
        ribbons = [m for m in s["meshes"] if m["name"] != "floor"]
        self.assertEqual([m["name"] for m in ribbons], ["plugin", "p--github", "README-md"])
        top = [max(v[1] for v in m["geometry"]["vertices"]) for m in ribbons]
        self.assertAlmostEqual(top[0], 0.02)  # no churn: flat on the floor
        self.assertAlmostEqual(top[1], 1.52)  # the busiest day is the tallest
        self.assertTrue(0.02 < top[2] < top[1])
        self.assertEqual(len({s["materials"][m["material"]]["color"] for m in ribbons}), 3)
        for bad in (([], []), ([[1]], ["a"]), ([[1, 2], [1]], ["a", "b"]), ([[1, -1]], ["a"]),
                    ([[1, 2]] * 11, [f"p{i}" for i in range(11)]), ([[1] * 41], ["a"])):
            with self.assertRaises(ValueError):
                sscene.terrain(*bad)
        for m in ribbons:  # tops face up, fronts face the camera
            vs = m["geometry"]["vertices"]
            for f in m["geometry"]["faces"]:
                a, b, c = (vs[k] for k in f)
                u, w = [b[k] - a[k] for k in range(3)], [c[k] - a[k] for k in range(3)]
                ny, nz = u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]
                self.assertTrue(ny >= 0 and nz >= 0 and ny + nz > 0, (m["name"], f))
        names = [m["name"] for m in sscene.terrain([[1, 2]] * 5, ["floor", "camera", "a-b", "a-b-5", "a.b"])["meshes"]]
        self.assertEqual(len(set(names)), 6, names)  # renamed clear of the reserved and of each other

    def test_churn_reads_lines_changed_per_top_level_path(self):
        for path, n in (("a/x.py", 10), ("b/y.py", 2), ("café/z.py", 1)):
            os.makedirs(os.path.join(self.repo, os.path.dirname(path)), exist_ok=True)
            with open(os.path.join(self.repo, path), "w") as f:
                f.write("x\n" * n)
        subprocess.run(["git", "-C", self.repo, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "two"], check=True)
        rows, names = sscene.churn(self.repo, 30)
        self.assertEqual(names, ["a", "b", "README.md", "café"])  # not git's quoted "caf\303\251"
        self.assertEqual([sum(r) for r in rows], [10, 2, 1, 1])
        self.assertEqual(len(rows[0]), 2)  # columns run from the oldest commit in range, not from 30 days back
        with self.assertRaises(ValueError):
            sscene.churn(self.tmp, 30)  # not a repository

    def test_fm_scene_terrain_prints_a_scene_fm_scene_checks(self):
        out = os.path.join(self.tmp, "terrain.json")
        self.assertIn("wrote", self.fm("scene", "terrain", "--days", "7", "--out", out).stdout)
        self.assertEqual(self.fm("scene", "check", out).returncode, 0)
        self.assertEqual(sscene.loads(self.fm("scene", "terrain", "--days", "7").stdout), sscene.load(out))
        self.assertNotEqual(self.fm("scene", "terrain", cwd=self.tmp, check=False).returncode, 0)
        self.assertIn("wrote", self.fm("scene", "terrain", "--days", "1", "--out", out).stdout)
        self.assertNotEqual(self.fm("scene", "terrain", "--days", "0", check=False).returncode, 0)


class Gallery(ForemanTestCase):
    """T-1873: fm scene gallery, every built-in scene on one sheet, a caption each."""

    def test_each_built_in_builds_and_draws_small_inside_a_time_budget(self):
        import time
        self.assertEqual(set(sscene.SAMPLES) | {"metaballs"}, set(sscene.GALLERY))  # every tile has a fallback
        for action in sscene.GALLERY:
            t = time.process_time()
            scene = sscene.SAMPLES[action]() if action in sscene.SAMPLES else sscene.metaballs(5, 16, 0)
            rgb = sscene._frame(sscene.validate(scene), 0, 160, 100)
            self.assertLess(time.process_time() - t, 3.0, action)  # CPU, ponytail: a hang guard, not a bench
            self.assertGreater(len({rgb[i:i + 3] for i in range(0, len(rgb), 3)}), 2, action)  # drew something
        full = {"terrain": sscene.terrain([[(r * 7 + t * 3) % 11 for t in range(40)] for r in range(10)],
                                           [f"p{r}" for r in range(10)]),  # the largest churn draws: 10 by 40
                "lookbook": sscene.lookbook([f"l{k}" for k in range(8)],
                                             {f"l{k}": {"palette": [(200, 30 * k, 90)] * 4} for k in range(8)}),
                "bench": sscene.bench([{"results": [{"seed": s, "closed": k % 2 == 0, "seconds": 10 + k}
                                                     for s in "abcd"]} for k in range(12)])}
        for action, scene in full.items():  # review: the gallery draws real data, larger than the samples
            t = time.process_time()
            sscene._frame(sscene.validate(scene), 0, 160, 100)
            self.assertLess(time.process_time() - t, 3.0, action)  # CPU: in-process render

    def test_a_tile_falls_back_to_its_sample_when_its_data_is_empty_or_unreadable(self):
        import types
        from unittest import mock
        real = sscene._builtin

        def broken(action, a):
            if action == "bench":
                raise KeyError("runs")  # a corrupt record: one tile, not the whole sheet
            if action == "airlock":
                return real(action, types.SimpleNamespace(**{**vars(a), "days": 1}))
            return real(action, a)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)  # the gallery reads the project it runs in: this test's, as fm scene gallery's tests do
        with mock.patch.object(sscene, "guard_counts", return_value=({}, 0)), \
                mock.patch.object(sscene, "_builtin", side_effect=broken), \
                mock.patch.object(sscene, "churn", wraps=sscene.churn) as churn, \
                mock.patch("sampo.core.find_project", return_value=object()):
            w, h, rgb, said = sscene.gallery()
        # T-3678: the churn tile reads the test's own repo, never the checkout the run started in (5 CPU-s of git)
        self.assertEqual(churn.call_args.args[0], self.repo)
        self.assertEqual((w, h, len(said)), (480, 200, 5))
        self.assertIn("(a sample: unreadable (KeyError))", said[3])
        self.assertIn("(a sample: no guard record over 1 days)", said[2])  # an empty record isn't a bare gate

    def test_fm_scene_gallery_writes_one_sheet_and_a_caption_per_scene(self):
        import struct
        out = os.path.join(self.tmp, "gallery.png")
        lines = self.fm("scene", "gallery", "--out", out).stdout.splitlines()
        self.assertIn("5 scenes, 480x200", lines[0])
        self.assertEqual([ln.split(":")[0] for ln in lines[1:]], [f"{k}. {a}" for k, a in enumerate(sscene.GALLERY, 1)])
        self.assertIn("(a sample: no fm code bench results", lines[4])  # a fresh project has no bench runs
        with open(out, "rb") as f:
            png = f.read(24)
        self.assertEqual((png[:8], struct.unpack(">II", png[16:24])), (b"\x89PNG\r\n\x1a\n", (480, 200)))
        said = json.loads(self.fm("scene", "gallery", "--out", out, "--json").stdout)
        self.assertEqual((said["size"], len(said["scenes"])), ([480, 200], 5))
        for bad, why in ((["x.json", "--out", out], "takes no FILE"), ([], "give --out"), (["--out", out, "--balls", "3"],
                                                                                           "only metaballs")):
            r = self.fm("scene", "gallery", *bad, check=False)
            self.assertEqual((r.returncode, why in r.stderr, "Traceback" in r.stderr), (2, True, False), r.stderr)


if __name__ == "__main__":
    unittest.main()
