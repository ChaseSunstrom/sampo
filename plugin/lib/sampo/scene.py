"""fm scene (T-1633): scene.json, a 3D scene described once as plain data, checked and compiled to a three.js module.

validate() returns the scene with every default filled in: one shape for each renderer that reads it (three.js here;
a rasteriser or a web component later reads the same dict). compile_three() emits an ES module modelled on
references/ui/templates/scene.html: no WebGL keeps the poster, context loss is survived, the DPR is capped, reduced
motion holds one still frame, nothing draws offscreen or hidden, a failure ends at the poster, dispose() frees it all.
compile_component() (T-1634) is that module whole plus <fm-scene>, a custom element mounting it in its shadow DOM.
"""
import base64
import json
import math
import os
import re
import stat
import struct

SCHEMA = """scene.json (only camera is required; numbers are finite, colours "#rgb" or "#rrggbb" in sRGB, angles in
degrees, names a letter then letters, digits, - or _; an unknown key is refused, so a typo never passes silently):
  background  the page's colour, so the canvas edge has no seam; null (the default): a transparent canvas
  camera      {fov: vertical, 1-179; position: [x,y,z]; target: [0,0,0]; near: 0.1; far: 100, at most 10,000 x near}
  lights      [{type: ambient | hemisphere | directional | point; color: "#ffffff"; intensity: 1 (three.js r155+
              units); position: [0,1,0] (directional: it shines from there toward the origin; point)}], a hemisphere
              taking sky: "#ffffff" and ground: "#444444" in place of color; at most 3 directional and point lights
  materials   {name: {color: "#cccccc"; roughness: 0.8, above 0, at most 1; metalness: 0, 0-1; emissive: "#000000";
              emissiveIntensity: 1}}
  meshes      [{name; material: a materials name; geometry; position: [0,0,0]; rotation: [0,0,0] (about X, Y, Z);
              scale: [1,1,1]}], geometry one of {type: box, size: [1,1,1]}, {type: sphere, radius: 1, segments:
              32}, {type: plane, size: [1,1]} (in XY, facing +Z), {type: vertices, vertices: [[x,y,z], ...], faces:
              [[a,b,c], ...] (indices, counter-clockwise seen from the front)}
  animations  {name: {target: a mesh's name, or camera for a camera path (position only: it keeps looking at
              camera.target, and no mesh may be named camera); property: position | rotation | scale; times: [seconds, rising];
              values: [[x,y,z], ...], one per time; loop: true}}: linear between keys, held outside them; a loop
              restarts at its last time, so its last value should equal its first (or turn a whole 360)
What a second renderer must not guess (three.js's conventions; world space is right-handed with +Y up):
  rotation    the matrix is Rx * Ry * Rz (three.js's Euler order "XYZ"), so on a point Z turns first
  transform   a mesh's model matrix is T * R * S: scale, then rotate, then move
  camera      its up is +Y and it looks down -Z of its own frame, toward target (one straight above or below is
              refused); a renderer whose view space looks along +z, as fmraster's does, negates view-space z
              and reverses each triangle's winding (the mirror keeps the screen winding, so three's front
              faces would be culled)
  sphere      width segments are segments, height segments max(2, segments >> 1)
  point       a point light falls off as intensity / distance^2: decay 2, no cutoff distance
  vertices    smooth normals in three.js, flat per face in the rasteriser (three sums the faces' normals at
              each vertex, area-weighted)"""

MAX_FILE = 16 << 20  # a scene with a million vertices fits; the module carries all of it
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")  # never __proto__: the module's object literal would eat it
HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
LIGHTS = {"ambient": {"color": "#ffffff"}, "hemisphere": {"sky": "#ffffff", "ground": "#444444"},
          "directional": {"color": "#ffffff", "position": [0, 1, 0]}, "point": {"color": "#ffffff", "position": [0, 1, 0]}}
DIRECT = 3  # scene-3d.md: at most three direct lights
GEOMETRIES = ("box", "sphere", "plane", "vertices")
PROPERTIES = ("position", "rotation", "scale")


def _fail(where, what):
    raise ValueError(f"{where}: {what}")


def _show(v):
    s = repr(v)  # repr escapes control characters: nothing from the file reaches the terminal raw
    return s if len(s) <= 40 else s[:39] + "…"


def _obj(v, where, required=(), optional=()):
    if not isinstance(v, dict):
        _fail(where, f"wants an object, not {_show(v)}")
    for k in v:
        if k not in required and k not in optional:
            _fail(where, f"unknown key {_show(k)} (known: {', '.join([*required, *optional])})")
    for k in required:
        if k not in v:
            _fail(where, f"{k} is missing")
    return v


def _list(v, where):
    if not isinstance(v, list):
        _fail(where, f"wants a list, not {_show(v)}")
    return v


def _num(v, where, lo=-math.inf, hi=math.inf, above=False, integer=False):
    ok = not isinstance(v, bool) and isinstance(v, int if integer else (int, float))
    try:
        ok = ok and math.isfinite(v) and (v > lo if above else v >= lo) and v <= hi
    except OverflowError:  # an int past a float's range
        ok = False
    if not ok:
        bounds = [f"{'above' if above else 'at least'} {lo:g}"] * (lo > -math.inf) + [f"at most {hi:g}"] * (hi < math.inf)
        _fail(where, f"wants {'an integer' if integer else 'a number'}{' ' if bounds else ''}{' and '.join(bounds)}, "
                     f"not {_show(v)}")
    return v


def _vec(v, where, n=3, **bounds):
    if not isinstance(v, list) or len(v) != n:
        _fail(where, f"wants a list of {n} numbers, not {_show(v)}")
    return [_num(x, f"{where}[{i}]", **bounds) for i, x in enumerate(v)]


def _colour(v, where):
    if not isinstance(v, str) or not HEX.fullmatch(v):
        _fail(where, f'wants a colour, "#rgb" or "#rrggbb", not {_show(v)}')
    v = v.lower()
    return v if len(v) == 7 else "#" + "".join(ch * 2 for ch in v[1:])


def _name(v, where):
    if not isinstance(v, str) or not NAME.fullmatch(v):
        _fail(where, f"wants a name (a letter, then up to 63 letters, digits, - or _), not {_show(v)}")
    return v


def _geometry(g, w):
    if not isinstance(g, dict) or g.get("type") not in GEOMETRIES:
        _fail(w, f"wants a type: {' | '.join(GEOMETRIES)}")
    t = g["type"]
    if t == "box":
        _obj(g, w, ["type"], ["size"])
        return {"type": t, "size": _vec(g.get("size", [1, 1, 1]), f"{w}.size", lo=0, above=True)}
    if t == "sphere":
        _obj(g, w, ["type"], ["radius", "segments"])
        return {"type": t, "radius": _num(g.get("radius", 1), f"{w}.radius", 0, above=True),
                "segments": _num(g.get("segments", 32), f"{w}.segments", 3, 256, integer=True)}
    if t == "plane":
        _obj(g, w, ["type"], ["size"])
        return {"type": t, "size": _vec(g.get("size", [1, 1]), f"{w}.size", 2, lo=0, above=True)}
    _obj(g, w, ["type", "vertices", "faces"])
    vs = [_vec(v, f"{w}.vertices[{i}]") for i, v in enumerate(_list(g["vertices"], f"{w}.vertices"))]
    if len(vs) < 3:
        _fail(f"{w}.vertices", f"wants at least 3, not {len(vs)}")
    faces = [_vec(f, f"{w}.faces[{i}]", lo=0, hi=len(vs) - 1, integer=True)
             for i, f in enumerate(_list(g["faces"], f"{w}.faces"))]
    if not faces:
        _fail(f"{w}.faces", "wants at least one face")
    return {"type": t, "vertices": vs, "faces": faces}


def validate(scene):
    """A parsed scene.json → the same scene with every default filled in; ValueError names the first bad place."""
    _obj(scene, "scene", ["camera"], ["background", "lights", "materials", "meshes", "animations"])
    bg = scene.get("background")
    c = _obj(scene["camera"], "camera", ["fov", "position"], ["target", "near", "far"])
    cam = {"fov": _num(c["fov"], "camera.fov", 1, 179), "position": _vec(c["position"], "camera.position"),
           "target": _vec(c.get("target", [0, 0, 0]), "camera.target"),
           "near": _num(c.get("near", 0.1), "camera.near", 0, above=True),
           "far": _num(c.get("far", 100), "camera.far", 0, above=True)}
    if not cam["near"] < cam["far"] <= 10000 * cam["near"]:  # scene-3d.md: clip planes as tight as the scene allows
        _fail("camera.far", f"wants a number above near and at most 10,000 x near, not {_show(cam['far'])}")
    if cam["position"] == cam["target"]:
        _fail("camera.target", "is where the camera stands, so it has no direction to look")
    dx, dy, dz = (t - p for t, p in zip(cam["target"], cam["position"]))
    if math.hypot(dx, dz) <= 1e-9 * abs(dy):  # along up (+Y): the view has no sideways axis to build
        _fail("camera.target", f"straight {'above' if dy > 0 else 'below'} the camera, along its up (+Y): move one "
                               "of them aside a little")

    lights, direct = [], 0
    for i, l in enumerate(_list(scene.get("lights", []), "lights")):
        w = f"lights[{i}]"
        if not isinstance(l, dict) or not isinstance(l.get("type"), str) or l["type"] not in LIGHTS:
            _fail(w, f"wants a type: {' | '.join(LIGHTS)}")
        t, extra = l["type"], LIGHTS[l["type"]]
        _obj(l, w, ["type"], ["intensity", *extra])
        out = {"type": t, "intensity": _num(l.get("intensity", 1), f"{w}.intensity", 0)}
        for k, d in extra.items():
            out[k] = _vec(l.get(k, d), f"{w}.{k}") if k == "position" else _colour(l.get(k, d), f"{w}.{k}")
        if t == "directional" and not any(out["position"]):  # its direction would be the zero vector: NaN light
            _fail(f"{w}.position", "is where it shines from toward the origin, so not the origin itself")
        direct += t in ("directional", "point")
        lights.append(out)
    if direct > DIRECT:
        _fail("lights", f"at most {DIRECT} directional and point lights (scene-3d.md), not {direct}")

    materials = {}
    if not isinstance(scene.get("materials", {}), dict):
        _fail("materials", "wants an object of named materials")
    for name, m in scene.get("materials", {}).items():
        w = f"materials.{_name(name, 'materials')}"
        _obj(m, w, (), ["color", "roughness", "metalness", "emissive", "emissiveIntensity"])
        materials[name] = {"color": _colour(m.get("color", "#cccccc"), f"{w}.color"),
                           "roughness": _num(m.get("roughness", 0.8), f"{w}.roughness", 0, 1, above=True),
                           "metalness": _num(m.get("metalness", 0), f"{w}.metalness", 0, 1),
                           "emissive": _colour(m.get("emissive", "#000000"), f"{w}.emissive"),
                           "emissiveIntensity": _num(m.get("emissiveIntensity", 1), f"{w}.emissiveIntensity", 0)}

    meshes, names = [], set()
    for i, m in enumerate(_list(scene.get("meshes", []), "meshes")):
        w = f"meshes[{i}]"
        _obj(m, w, ["geometry", "material"], ["name", "position", "rotation", "scale"])
        name = None if m.get("name") is None else _name(m["name"], f"{w}.name")
        if name and name in names:
            _fail(f"{w}.name", f"{name} names an earlier mesh too")
        if name == "camera":  # T-1530: an animation's target camera is the camera's path
            _fail(f"{w}.name", "camera names the camera, which an animation can move: call the mesh something else")
        names.add(name)
        mat = _name(m["material"], f"{w}.material")
        if mat not in materials:
            _fail(f"{w}.material", f"no material named {mat}")
        meshes.append({"name": name, "geometry": _geometry(m["geometry"], f"{w}.geometry"), "material": mat,
                       "position": _vec(m.get("position", [0, 0, 0]), f"{w}.position"),
                       "rotation": _vec(m.get("rotation", [0, 0, 0]), f"{w}.rotation"),
                       "scale": _vec(m.get("scale", [1, 1, 1]), f"{w}.scale")})
    names.discard(None)

    animations, moved = {}, {}
    if not isinstance(scene.get("animations", {}), dict):
        _fail("animations", "wants an object of named animations")
    for name, a in scene.get("animations", {}).items():
        w = f"animations.{_name(name, 'animations')}"
        _obj(a, w, ["target", "property", "times", "values"], ["loop"])
        target = _name(a["target"], f"{w}.target")
        if target not in names and target != "camera":
            _fail(f"{w}.target", f"no mesh named {target} (or camera, for a camera path)")
        prop = a["property"]
        if prop not in PROPERTIES:
            _fail(f"{w}.property", f"wants {' | '.join(PROPERTIES)}, not {_show(prop)}")
        if target == "camera" and prop != "position":  # T-1530: it looks at camera.target, so only its place moves
            _fail(f"{w}.property", f"a camera path moves the camera's position (it keeps looking at camera.target), "
                                   f"not its {prop}")
        if (target, prop) in moved:  # two would fight over it, the last one silently winning
            _fail(w, f"{moved[target, prop]} moves {target}'s {prop} already")
        moved[target, prop] = name
        times = [_num(t, f"{w}.times[{j}]", 0) for j, t in enumerate(_list(a["times"], f"{w}.times"))]
        if not times or any(b <= t for t, b in zip(times, times[1:])):
            _fail(f"{w}.times", "must rise, from at least one time")
        values = _list(a["values"], f"{w}.values")
        if len(values) != len(times):
            _fail(f"{w}.values", f"one per time: {len(times)} wanted, not {len(values)}")
        loop = a.get("loop", True)
        if not isinstance(loop, bool):
            _fail(f"{w}.loop", f"wants true or false, not {_show(loop)}")
        animations[name] = {"target": target, "property": prop, "times": times,
                            "values": [_vec(v, f"{w}.values[{j}]") for j, v in enumerate(values)], "loop": loop}

    return {"background": None if bg is None else _colour(bg, "background"), "camera": cam, "lights": lights,
            "materials": materials, "meshes": meshes, "animations": animations}


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:  # json keeps the last silently: a second "camera" would hide the first
            raise ValueError(f"key {_show(k)} appears twice in one object")
        out[k] = v
    return out


def _constant(name):
    raise ValueError(f"{name} is not a number JSON allows")


def _json(text):
    """JSON text → its value: a leading byte-order mark is fine; a repeated key, NaN or deep nesting isn't."""
    text = text.removeprefix("﻿")
    if _depth(text) > MAX_DEPTH:  # T-1799: json's own limit is the C stack's, so it differs by platform and Python
        raise ValueError("nested too deeply")
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except RecursionError:
        raise ValueError("nested too deeply") from None


def loads(text):
    """scene.json's text → the validated scene."""
    return validate(_json(text))


MAX_DEPTH = 256


def _depth(text):
    """The deepest [ or { nesting outside strings."""
    d = top = 0
    for m in re.finditer(r'"(?:[^"\\]|\\.)*"|[\[{\]}]', text):
        t = m.group()
        if t in ("[", "{"):
            d += 1
            top = max(top, d)
        elif t in ("]", "}"):
            d -= 1
    return top


def load(path, parse=loads):
    """A scene.json file → the validated scene (or parse(its text)); ValueError for a file that isn't regular, is too
    big or is wrong."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)  # a plain open() of a FIFO waits for a writer
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("not a regular file")
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError(f"too large a file (over {MAX_FILE >> 20} MB)")
        return parse(data.decode("utf-8"))
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None


HEAD = """// Built by fm scene compile (T-1633) from a scene.json: edit that and compile again. Needs three.js r155 or later,
// resolved as "three" by an import map or a bundler. Modelled on Sampo's templates/scene.html (references/ui/
// scene-3d.md): the page holds <figure class="scene" data-state="poster"> with the poster, a <canvas role="img"
// aria-label="..."> and the text twin, and its CSS fades the canvas in at data-state="live". Load it after first paint:
//   import("./scene.js").then(m => m.mount(figure)).catch(err => console.error("scene: showing the poster", err));
// mount() returns dispose(), which frees everything it made, the WebGL context included.
import * as THREE from "three";

"""

COMPONENT_HEAD = """// Built by fm scene compile --to component (T-1634) from a scene.json: edit that and compile again. Needs three.js
// r155 or later, resolved as "three" by an import map or a bundler. This is the three.js module (SCENE, mount()) plus
// <fm-scene>, a custom element mounting it in its own shadow DOM. The page holds <fm-scene label="what it shows">
// with the poster inside as <img slot="poster" alt=""> and the text twin after it, and loads this after first paint:
//   import("./scene.js").catch(err => console.error("scene: showing the poster", err));
// Until then, or with no WebGL, the poster and the twin show. The element's data-state (poster | live | lost |
// failed) is the first-frame flag; taking it out of the page frees all mount() made, the WebGL context included.
// No layout shift: the page's CSS gives it its box before it is defined (once defined, fm-scene::part(stage) sets the
// ratio):  fm-scene:not(:defined){display:block;position:relative;aspect-ratio:16/10}
//          fm-scene:not(:defined)>[slot=poster]{width:100%;height:100%;object-fit:cover}
import * as THREE from "three";

"""

RUNTIME = """
const RAD = Math.PI / 180;

function geometry(g) {
  if (g.type === "box") return new THREE.BoxGeometry(...g.size);
  if (g.type === "sphere") return new THREE.SphereGeometry(g.radius, g.segments, Math.max(2, g.segments >> 1));
  if (g.type === "plane") return new THREE.PlaneGeometry(...g.size);
  const geo = new THREE.BufferGeometry();  // vertices: smooth normals from the faces
  geo.setAttribute("position", new THREE.Float32BufferAttribute(g.vertices.flat(), 3));
  geo.setIndex(g.faces.flat());
  geo.computeVertexNormals();
  return geo;
}

function light(l) {
  if (l.type === "ambient") return new THREE.AmbientLight(l.color, l.intensity);
  if (l.type === "hemisphere") return new THREE.HemisphereLight(l.sky, l.ground, l.intensity);
  const o = new (l.type === "point" ? THREE.PointLight : THREE.DirectionalLight)(l.color, l.intensity);
  o.position.set(...l.position);  // a directional light shines from here toward the origin
  return o;
}

// Linear between keys, held outside them; a loop restarts at its last time.
function sample(a, t) {
  const ts = a.times, end = ts[ts.length - 1];
  if (a.loop && end > 0) t %= end;
  let i = 0;
  while (i < ts.length - 1 && ts[i + 1] <= t) i++;
  if (i === ts.length - 1 || t <= ts[i]) return a.values[i];
  const k = (t - ts[i]) / (ts[i + 1] - ts[i]);
  return a.values[i].map((v, j) => v + (a.values[i + 1][j] - v) * k);
}

export function mount(fig) {
  // A fresh canvas each mount: a context released by dispose() never comes back on its old canvas. fig: the page's
  // figure, or an <fm-scene> holding its canvas in its shadow root.
  const old = (fig.shadowRoot ?? fig).querySelector("canvas"), canvas = old.cloneNode(false), setState = s => { fig.dataset.state = s; };
  old.replaceWith(canvas);
  const off = new AbortController(), on = (target, type, fn) => target.addEventListener(type, fn, { signal: off.signal });
  // Survival 1: no WebGL2 (or only a software renderer) -> the poster and the text twin stay, nothing throws.
  const gl = canvas.getContext("webgl2", { antialias: true, alpha: true, powerPreference: "low-power", failIfMajorPerformanceCaveat: true });
  if (!gl) return () => {};
  const reduce = matchMedia("(prefers-reduced-motion: reduce)"), c = SCENE.camera, owned = [];
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(c.fov, 1, c.near, c.far);
  let renderer = null, anims = [], raf = 0, last = 0, time = 0, stopped = false, freed = false, visible = !document.hidden, onscreen = true;

  // Once, from fail() or dispose(): free what was made, then release the context (survival 4).
  const free = () => {
    if (freed) return; freed = true;
    for (const o of owned) o.dispose();
    if (renderer) { renderer.dispose(); renderer.forceContextLoss(); renderer = null; } else gl.getExtension("WEBGL_lose_context")?.loseContext();
  };
  // Survival 8: any failure ends at the poster, logged once; the freeing waits until three.js's own call has returned.
  const fail = err => { if (stopped) return; stopped = true; cancelAnimationFrame(raf); raf = 0; console.error("scene: showing the poster", err); setState("failed"); queueMicrotask(free); };
  const pose = t => { for (const a of anims) { const v = sample(a, t); a.mesh[a.property].set(...(a.property === "rotation" ? v.map(d => d * RAD) : v)); } camera.lookAt(...c.target); };
  try {
    renderer = new THREE.WebGLRenderer({ canvas, context: gl });
    renderer.debug.onShaderError = (g, p) => { if (!g.isContextLost()) fail(new Error(g.getProgramInfoLog(p) || "a shader failed to compile")); };
    renderer.toneMapping = THREE.ACESFilmicToneMapping;  // one tone mapper; the output stays sRGB, three's default
    renderer.setClearColor(SCENE.background ?? "#000000", SCENE.background ? 1 : 0);  // no seam: the page's colour, or none
    camera.position.set(...c.position); camera.lookAt(...c.target);
    const mats = new Map(Object.entries(SCENE.materials).map(([k, m]) => [k, new THREE.MeshStandardMaterial(m)]));
    const named = new Map();
    owned.push(...mats.values());
    for (const m of SCENE.meshes) {
      const geo = geometry(m.geometry), mesh = new THREE.Mesh(geo, mats.get(m.material));
      owned.push(geo);
      mesh.position.set(...m.position); mesh.rotation.set(...m.rotation.map(d => d * RAD)); mesh.scale.set(...m.scale);
      if (m.name) named.set(m.name, mesh);
      scene.add(mesh);
    }
    for (const l of SCENE.lights) scene.add(light(l));
    anims = Object.values(SCENE.animations).map(a => ({ ...a, mesh: a.target === "camera" ? camera : named.get(a.target) }));
    pose(0);
    renderer.compile(scene, camera);  // shaders compile before the reveal: no hitch once it shows
  } catch (err) { fail(err); }
  const moving = anims.length > 0;

  // Draw now; the first good frame fades the canvas in over the poster (survival 2).
  function render() {
    if (stopped || !renderer || gl.isContextLost()) return;
    try { renderer.render(scene, camera); } catch (err) { return fail(err); }
    if (!stopped && fig.dataset.state !== "live") setState("live");
  }
  // Survival 5: reduced motion holds the first pose and draws only when something changes.
  function frame(now) {
    raf = 0;
    const dt = Math.min((now - (last || now)) / 1000, 0.1); last = now;
    pose(reduce.matches ? 0 : (time += dt));
    render();
    if (moving && !reduce.matches) wake();
  }
  // Survival 9: no frames offscreen, in a hidden tab, or with nothing moving.
  function wake() { if (!raf && !stopped && visible && onscreen && renderer && !gl.isContextLost()) raf = requestAnimationFrame(frame); }
  function sleep() { cancelAnimationFrame(raf); raf = 0; last = 0; }

  // Budget: DPR capped at 2 (1.5 on touch screens); a resize redraws in the same callback, so no black frame.
  const ro = new ResizeObserver(() => {
    if (stopped || !renderer) return;
    const dpr = Math.min(window.devicePixelRatio || 1, matchMedia("(pointer: coarse)").matches ? 1.5 : 2);
    const w = Math.max(1, canvas.clientWidth), h = Math.max(1, canvas.clientHeight);
    renderer.setPixelRatio(dpr); renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    render();
  });
  const io = new IntersectionObserver(entries => { onscreen = entries.at(-1).isIntersecting; onscreen ? wake() : sleep(); });  // oldest first
  on(document, "visibilitychange", () => { visible = !document.hidden; visible ? wake() : sleep(); });
  on(reduce, "change", wake);
  // Survival 3: preventDefault or the browser never restores; the poster covers the gap; three.js rebuilds its own state.
  on(canvas, "webglcontextlost", e => { e.preventDefault(); sleep(); if (!stopped) setState("lost"); });
  on(canvas, "webglcontextrestored", () => { render(); wake(); });
  ro.observe(canvas); io.observe(canvas); wake();
  // Survival 4 and 9: cancel the loop, remove the listeners, free what was made, release the context.
  return () => { off.abort(); ro.disconnect(); io.disconnect(); sleep(); stopped = true; free(); setState("poster"); };
}
"""


# <\/ in the markup: the module pasted into a <script> has nothing in it that closes the tag.
ELEMENT = r"""
// <fm-scene> (T-1634): mount() in a shadow DOM. The child with slot="poster" sits under the canvas; any other child
// (the text twin) shows below. The element itself carries data-state, so the page's CSS and tests read it there.
const SHADOW = `<style>
  :host { display: block; }
  .stage { position: relative; aspect-ratio: 16 / 10; }
  @media (max-aspect-ratio: 3/4) { .stage { aspect-ratio: 4 / 5; } }
  ::slotted([slot="poster"]), canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
  ::slotted([slot="poster"]) { object-fit: cover; }
  canvas { opacity: 0; touch-action: pan-y; pointer-events: none; }
  :host([data-state="live"]) canvas { opacity: 1; pointer-events: auto; transition: opacity 400ms ease-out; }
  @media (prefers-reduced-motion: reduce) { :host([data-state="live"]) canvas { transition: none; } }
<\/style><div class="stage" part="stage"><slot name="poster"><\/slot><canvas role="img"><\/canvas><\/div><slot><\/slot>`;

export class FmScene extends HTMLElement {
  static observedAttributes = ["label"];
  #dispose = null;
  connectedCallback() {
    // A root the page declared (<template shadowrootmode>) is kept, filled in if it has no canvas.
    const root = this.shadowRoot ?? this.attachShadow({ mode: "open" });
    if (!root.querySelector("canvas")) root.innerHTML = SHADOW;
    this.#label();
    this.dataset.state = "poster";
    this.#dispose = mount(this);
  }
  // label="..." names the canvas (an id reference can't cross the shadow boundary); with none, the twin speaks. It is
  // watched (T-1706): a page that changes it, or takes it away, is heard.
  attributeChangedCallback() { this.#label(); }
  #label() {
    const canvas = this.shadowRoot?.querySelector("canvas"), label = this.getAttribute("label");
    if (!canvas) return;  // not connected yet: connectedCallback names it
    if (label) { canvas.setAttribute("aria-label", label); canvas.removeAttribute("aria-hidden"); }
    else { canvas.removeAttribute("aria-label"); canvas.setAttribute("aria-hidden", "true"); }
  }
  // Out of the page: all mount() made is freed and the context released; put back, it mounts on a fresh canvas.
  disconnectedCallback() { this.#dispose?.(); this.#dispose = null; }
}
// Two scenes on one page: the second finds fm-scene taken and needs a name of its own, in its markup too.
if (!customElements.get("fm-scene")) customElements.define("fm-scene", FmScene);
else console.warn("scene: <fm-scene> is taken; compile this one with fm scene compile --to component --tag NAME");
"""
# T-1706: a valid custom element name (ASCII: a lowercase letter first, a hyphen in it), none the HTML spec reserves
TAG = re.compile(r"[a-z][a-z0-9._]*-[a-z0-9._-]*")
RESERVED_TAGS = {"annotation-xml", "color-profile", "font-face", "font-face-src", "font-face-uri", "font-face-format",
                 "font-face-name", "missing-glyph"}


# T-1530: fm scene compile --hud adds this dev overlay: the frame rate and what three.js holds, over the canvas, and the
# same numbers as globalThis.__fmScene for a test or a soak to read. A development build only.
HUD = """
// --hud (T-1530): a dev overlay, not for production: the frame rate twice a second, draw calls, triangles, geometries
// and textures; globalThis.__fmScene holds the same and each render's milliseconds (the last 2,000), for a soak to read.
function hud(renderer, fig, ms) {
  const el = hud.el ??= Object.assign(document.createElement("output"), { className: "fm-scene-hud" });
  if (!el.isConnected) {
    el.style.cssText = "position:absolute;top:4px;left:4px;z-index:9;font:11px/1.3 ui-monospace,monospace;color:#fff;background:#000b;padding:2px 6px;pointer-events:none;white-space:pre";
    (fig.shadowRoot?.querySelector(".stage") ?? fig).append(el);
  }
  const now = performance.now(), info = renderer.info, times = hud.ms ??= [];
  times.push(ms); if (times.length > 2000) times.shift();
  hud.n = (hud.n ?? 0) + 1; hud.t ??= now; hud.frames = (hud.frames ?? 0) + 1;
  const s = globalThis.__fmScene = { fps: hud.fps ?? 0, frames: hud.frames, calls: info.render.calls,
    triangles: info.render.triangles, geometries: info.memory.geometries, textures: info.memory.textures, frame_ms: times };
  if (now - hud.t < 500) return;
  s.fps = hud.fps = Math.round(hud.n * 1000 / (now - hud.t)); hud.n = 0; hud.t = now;
  el.textContent = `${s.fps} fps  ${s.calls} calls  ${s.triangles} triangles\\n${s.geometries} geometries  ${s.textures} textures`;
}
"""
RENDER_CALL = "try { renderer.render(scene, camera); }"
TIMED_CALL = "try { const t0 = performance.now(); renderer.render(scene, camera); hud(renderer, fig, performance.now() - t0); }"


def compile_three(scene, head=HEAD, tail="", hud=False):
    """A validated scene → the ES module's text: SCENE (one top-level key a line), then the runtime that builds it.
    The same scene gives the same bytes, so a second compile changes nothing. hud (T-1530): with the dev overlay."""
    body = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, separators=(',', ':'))}" for k, v in scene.items())
    runtime = RUNTIME.replace(RENDER_CALL, TIMED_CALL) + HUD \
        if hud else RUNTIME
    return head + "export const SCENE = {\n" + body + "\n};\n" + runtime + tail


def compile_component(scene, tag="fm-scene", hud=False):
    """A validated scene → compile_three's module, the same SCENE and mount(), with <fm-scene> defined after it, or
    the custom element name tag (T-1706: two scenes on one page, each its own)."""
    if not TAG.fullmatch(tag) or tag in RESERVED_TAGS:
        raise ValueError(f"--tag {tag!r} isn't a custom element name: lowercase ASCII, a letter first, a hyphen in it "
                         "(pebble-scene), and none the HTML spec reserves (font-face, …)")
    return compile_three(scene, COMPONENT_HEAD.replace("fm-scene", tag), ELEMENT.replace("fm-scene", tag), hud)


# T-1530: offline checks: no browser, no GPU
def _rot(p, deg):
    """p turned by Rx * Ry * Rz (three.js's Euler order XYZ: on a point, Z turns first), angles in degrees."""
    x, y, z = p
    ax, ay, az = (math.radians(d) for d in deg)
    x, y = x * math.cos(az) - y * math.sin(az), x * math.sin(az) + y * math.cos(az)
    x, z = x * math.cos(ay) + z * math.sin(ay), -x * math.sin(ay) + z * math.cos(ay)
    y, z = y * math.cos(ax) - z * math.sin(ax), y * math.sin(ax) + z * math.cos(ax)
    return x, y, z


def _cross3(a, b):
    return a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]


def _unit(v):
    n = math.sqrt(sum(a * a for a in v))
    return tuple(a / n for a in v)


def faces(g):
    """A geometry's triangles in its own space, front faces counter-clockwise seen from outside, as three.js builds
    them (a sphere's poles drop their empty triangles)."""
    if g["type"] == "vertices":
        return [tuple(tuple(g["vertices"][i]) for i in f) for f in g["faces"]]
    if g["type"] == "plane":
        w, h = (s / 2 for s in g["size"])
        q = [(-w, -h, 0), (w, -h, 0), (w, h, 0), (-w, h, 0)]
        return [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
    if g["type"] == "box":
        half, tris = [s / 2 for s in g["size"]], []
        for i in range(3):
            j, k = (i + 1) % 3, (i + 2) % 3
            for s in (1, -1):  # the face whose outward normal is s along axis i: u along j, v along s * k
                q = []
                for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    p = [0.0, 0.0, 0.0]
                    p[i], p[j], p[k] = s * half[i], a * half[j], s * b * half[k]
                    q.append(tuple(p))
                tris += [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
        return tris
    r, ws = g["radius"], g["segments"]
    hs = max(2, ws >> 1)
    grid = [[(-r * math.cos(2 * math.pi * u / ws) * math.sin(math.pi * v / hs), r * math.cos(math.pi * v / hs),
              r * math.sin(2 * math.pi * u / ws) * math.sin(math.pi * v / hs)) for u in range(ws + 1)]
            for v in range(hs + 1)]
    tris = []
    for v in range(hs):
        for u in range(ws):
            for t in ((grid[v][u], grid[v + 1][u], grid[v + 1][u + 1]), (grid[v][u], grid[v + 1][u + 1], grid[v][u + 1])):
                n = _cross3([b - a for a, b in zip(t[0], t[1])], [c - a for a, c in zip(t[0], t[2])])
                if any(abs(x) > 1e-12 for x in n):
                    out = sum(x * y for x, y in zip(n, (sum(p[i] for p in t) for i in range(3)))) > 0
                    tris.append(t if out else (t[0], t[2], t[1]))
    return tris


def triangles(scene, turn=0.0, unit=1000):
    """T-1530: the scene's meshes as fmraster triangles: each mesh's T * R * S, then the world turned `turn` degrees
    about +Y (a turntable), seen from its camera, in integer units of 1/unit. From three.js's view space the depth is
    negated and two vertices swapped, as fmraster's docstring says, so its front faces stay front faces."""
    cam = scene["camera"]
    eye = cam["position"]
    f = _unit([t - e for t, e in zip(cam["target"], eye)])
    r = _unit(_cross3(f, (0, 1, 0)))
    up = _cross3(r, f)
    out = []
    for m in scene["meshes"]:
        hexc = scene["materials"][m["material"]]["color"]
        colour = tuple(int(hexc[i:i + 2], 16) for i in (1, 3, 5))
        for tri in faces(m["geometry"]):
            pts = []
            for p in tri:
                p = _rot([a * s for a, s in zip(p, m["scale"])], m["rotation"])
                p = _rot([a + b for a, b in zip(p, m["position"])], (0, turn, 0))
                d = [a - e for a, e in zip(p, eye)]
                pts.append(tuple(round(sum(x * y for x, y in zip(d, axis)) * unit) for axis in (r, up, f)))
            out.append((pts[0], pts[2], pts[1], colour))
    return out


def turntable(scene, frames=8, size=(160, 100), cols=4):
    """T-1530: a contact sheet of the scene turned about +Y in `frames` even steps, drawn by fmraster (flat shading,
    no browser or GPU): (width, height, RGB bytes), frames left to right then down. ponytail: pure Python, about a
    second a megapixel; a scene of many thousand triangles takes a while."""
    w, h = size
    return _sheet([_frame(scene, 360 * k / frames, w, h) for k in range(frames)], w, h, cols, _background(scene))


def _sheet(tiles, w, h, cols, bg):
    """w x h RGB tiles laid left to right then down, cols wide, on bg: (width, height, RGB bytes)."""
    cols = min(cols, len(tiles))
    rows = -(-len(tiles) // cols)
    sheet = bytearray(bytes(bg) * (w * cols * h * rows))
    for k, tile in enumerate(tiles):
        x0, y0 = (k % cols) * w, (k // cols) * h
        for y in range(h):
            at = ((y0 + y) * w * cols + x0) * 3
            sheet[at:at + w * 3] = tile[y * w * 3:(y + 1) * w * 3]
    return w * cols, h * rows, bytes(sheet)


def _background(scene):
    return tuple(int(scene["background"][i:i + 2], 16) for i in (1, 3, 5)) if scene["background"] else (0, 0, 0)


def _frame(scene, turn, w, h):
    """One w x h RGB frame of the scene turned `turn` degrees about +Y, drawn by fmraster from its camera."""
    from sampo import raster
    focal = max(1, round(h / 2 / math.tan(math.radians(scene["camera"]["fov"]) / 2)))
    return raster.render(triangles(scene, turn), w, h, focal=focal, background=_background(scene))


def cells(scene, cols=64, turn=0.0):
    """T-1635: the scene as terminal text: cols x (cols * 5/8, down to even) pixels from fmraster, two to a cell as
    an upper half block, the top pixel its 24-bit foreground and the bottom its background; one string per row of
    cells, each ending in a reset. ponytail: every cell carries both colours, about 40 bytes; truecolor only."""
    if not 2 <= cols <= 400:
        raise ValueError(f"--cols {cols}: 2 to 400")
    h = max(2, cols * 5 // 8 // 2 * 2)
    rgb = _frame(scene, turn, cols, h)
    lines = []
    for y in range(0, h, 2):
        row = []
        for x in range(cols):
            a, b = 3 * (y * cols + x), 3 * ((y + 1) * cols + x)
            row.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm\u2580" % (*rgb[a:a + 3], *rgb[b:b + 3]))
        lines.append("".join(row) + "\x1b[0m")
    return lines


THREE_LIGHTS = {"AmbientLight": "ambient", "HemisphereLight": "hemisphere", "DirectionalLight": "directional",
                "PointLight": "point"}
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
IMPORT_VERTICES = 1 << 20  # MAX_FILE's million vertices: meshes sharing one geometry can't multiply past it
MATERIAL_DROPS = ("opacity", "transparent", "wireframe", "side", "flatShading", "vertexColors", "alphaTest")


def _hex(n):
    return "#%06x" % (int(n) & 0xFFFFFF)


def _tidy(vs):
    return [round(v, 6) + 0.0 for v in vs]  # + 0.0: no -0.0 in the file


def _mat4mul(a, b):
    """Column-major 4x4 matrices, as three.js keeps them: a * b."""
    return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]


def _decompose(m):
    """A column-major matrix → position, rotation (degrees, Euler XYZ), scale and whether it shears, as three's
    Matrix4.decompose and Euler.setFromRotationMatrix: a mirrored matrix negates scale x; a shear is lost."""
    cols = [m[0:3], m[4:7], m[8:11]]
    s = [math.sqrt(sum(v * v for v in col)) for col in cols]
    if sum(a * b for a, b in zip(cols[0], _cross3(cols[1], cols[2]))) < 0:
        s[0] = -s[0]
    r = [[cols[j][i] / s[j] if s[j] else 0.0 for j in range(3)] for i in range(3)]  # r[row][col]
    shear = any(abs(sum(r[k][i] * r[k][j] for k in range(3))) > 1e-6 for i, j in ((0, 1), (0, 2), (1, 2)))
    y = math.asin(max(-1.0, min(1.0, r[0][2])))
    if abs(r[0][2]) < 0.9999999:
        x, z = math.atan2(-r[1][2], r[2][2]), math.atan2(-r[0][1], r[0][0])
    else:
        x, z = math.atan2(r[2][1], r[1][1]), 0.0
    return _tidy(m[12:15]), _tidy(math.degrees(a) for a in (x, y, z)), _tidy(s), shear


def from_three(doc):
    """T-1847: three.js JSON (Object3D.toJSON / ObjectLoader, format 4) → (the scene.json dict, validated; notes on
    what it couldn't carry). Each object's matrix is composed with its parents' (Groups, Object3Ds), then split into
    position, rotation and scale. Box, Sphere, Plane and BufferGeometry (position, optional index) become box,
    sphere, plane and vertices; a mesh material's color, roughness, metalness and emissive carry, textures don't;
    Ambient, Hemisphere, Directional and Point lights (at most 3 of the last two); the first PerspectiveCamera looks
    along its -Z, its target as far off as it stands from the origin (none: three's usual fov 50 at [0, 0, 5]).
    Whatever scene.json refuses is left out with a note, never a failed import; ValueError when the document isn't
    three.js JSON or holds no mesh scene.json can draw."""
    try:
        return _from_three(doc)
    except (TypeError, AttributeError, ArithmeticError) as e:  # a field of the wrong kind: "50" for a fov, a null
        raise ValueError(f"not three.js JSON as Object3D.toJSON writes it ({type(e).__name__}: {e})") from None


def _from_three(doc):
    import collections
    if not isinstance(doc, dict) or not isinstance(doc.get("object"), dict):
        raise ValueError("not three.js JSON: no object (Object3D.toJSON writes metadata, geometries, materials, object)")
    geos = {g.get("uuid"): g for g in doc.get("geometries") or [] if isinstance(g, dict)}
    mats = {m.get("uuid"): m for m in doc.get("materials") or [] if isinstance(m, dict)}
    notes, meshes, lights, materials, matname, taken = [], [], [], {}, {}, {"camera"}  # camera: a path's target
    camera, seen, targets, count = None, set(), [], [0]
    fallback = {"fov": 50, "position": [0, 0, 5]}

    def refused(part, what):
        try:
            validate(dict({"camera": fallback}, **part))
            return False
        except ValueError as e:
            notes.append(f"{what} scene.json refuses ({e})")
            return True

    def name(want, prefix, used):
        n, i = want if isinstance(want, str) and NAME.fullmatch(want) and want not in used else None, 0
        if n is None and want not in (None, ""):
            notes.append(f"a {prefix} name scene.json can't take or a repeat (renamed {prefix}N)")
        while n is None or n in used:
            i += 1
            n = f"{prefix}{i}"
        used.add(n)
        return n

    def geometry(g):
        t = str(g.get("type", "")).removesuffix("BufferGeometry").removesuffix("Geometry") or "Buffer"
        if t == "Box":
            return {"type": "box", "size": [g.get("width", 1), g.get("height", 1), g.get("depth", 1)]}
        if t == "Sphere":
            return {"type": "sphere", "radius": g.get("radius", 1), "segments": g.get("widthSegments", 32)}
        if t == "Plane":
            return {"type": "plane", "size": [g.get("width", 1), g.get("height", 1)]}
        if t == "Buffer":
            data = g.get("data") or {}
            pos = (data.get("attributes") or {}).get("position") or {}
            a = pos.get("array") or []
            if pos.get("itemSize", 3) != 3 or len(a) % 3 or not a:
                return None
            verts = [a[i:i + 3] for i in range(0, len(a), 3)]
            idx = (data.get("index") or {}).get("array") or list(range(len(verts) // 3 * 3))
            return {"type": "vertices", "vertices": verts, "faces": [idx[i:i + 3] for i in range(0, len(idx) // 3 * 3, 3)]}
        return None

    def material(ref):
        if isinstance(ref, list):
            if len(ref) > 1:
                notes.append("a second material on a mesh (its first is kept)")
            ref = ref[0] if ref else None
        m = mats.get(ref) if isinstance(ref, str) else None
        if m is None:
            return None
        if ref not in matname:
            out = {"color": _hex(m.get("color", 0xFFFFFF))}
            for k in ("roughness", "metalness", "emissiveIntensity"):
                if isinstance(m.get(k), (int, float)):
                    out[k] = m[k]
            if "roughness" in out:
                out["roughness"] = min(1, max(out["roughness"], 0.001))  # scene.json's roughness is above 0
            if "metalness" in out:
                out["metalness"] = min(1, max(out["metalness"], 0))
            if "emissive" in m:
                out["emissive"] = _hex(m["emissive"])
            if m.get("type") not in ("MeshStandardMaterial", "MeshPhysicalMaterial", None):
                notes.append(f"a {_show(m.get('type'))} (drawn as a lit standard material)")
            notes.extend(f"a material's {k}" for k in MATERIAL_DROPS if k in m)
            if any(k.lower().endswith("map") for k in m):
                notes.append("a texture")
            matname[ref] = name(m.get("name"), "material", set(materials))
            materials[matname[ref]] = out
        return matname[ref]

    def walk(o, parent):
        nonlocal camera
        t, m = o.get("type"), o.get("matrix", IDENTITY)
        if not (isinstance(m, list) and len(m) == 16 and all(isinstance(v, (int, float)) for v in m)):
            raise ValueError(f"{_show(t)} {_show(o.get('uuid', ''))}: matrix isn't 16 numbers")
        seen.add(o.get("uuid"))
        if o.get("visible") is False:
            notes.append("a hidden object (with its children)")
            return
        world = _mat4mul(parent, m)
        if t == "Mesh":
            g = geos.get(o.get("geometry"))
            geo, mat = geometry(g) if g else None, material(o.get("material"))
            if geo is None or mat is None:
                notes.append(f"a {_show(g.get('type')) if g else 'geometry-less'} mesh" if geo is None
                             else "a mesh with no material")
            else:
                p, r, s, shear = _decompose(world)
                mesh = {"name": name(o.get("name"), "mesh", taken), "material": mat, "geometry": geo,
                        "position": p, "rotation": r, "scale": s}
                if shear:
                    notes.append("a sheared transform (the shear is lost)")
                if not refused({"materials": {mat: materials[mat]}, "meshes": [mesh]}, f"a mesh {mesh['name']}"):
                    count[0] += len(geo.get("vertices", ()))
                    if count[0] > IMPORT_VERTICES:
                        raise ValueError(f"over {IMPORT_VERTICES:,} vertices (a geometry shared by many meshes?)")
                    meshes.append(mesh)
        elif t in THREE_LIGHTS:
            lt = {"type": THREE_LIGHTS[t], "intensity": o.get("intensity", 1)}
            if t == "HemisphereLight":
                lt.update(sky=_hex(o.get("color", 0xFFFFFF)), ground=_hex(o.get("groundColor", 0x444444)))
            else:
                lt["color"] = _hex(o.get("color", 0xFFFFFF))
            if t in ("DirectionalLight", "PointLight"):
                lt["position"] = _tidy(world[12:15])
            if t == "DirectionalLight" and o.get("target"):
                targets.append(o["target"])
            if t == "PointLight" and (o.get("distance", 0) or o.get("decay", 2) != 2):
                notes.append("a point light's distance or decay (scene.json's falls off as 1/d^2)")
            if t in ("DirectionalLight", "PointLight") and sum(x["type"] in ("directional", "point") for x in lights) >= 3:
                notes.append(f"a {t} past the third")
            elif not refused({"lights": [lt]}, f"a {t}"):
                lights.append(lt)
        elif t == "PerspectiveCamera" and camera is None:
            p, fwd = world[12:15], _unit([-world[8], -world[9], -world[10]])
            d = max(1.0, math.sqrt(sum(v * v for v in p)))
            if math.hypot(fwd[0], fwd[2]) < 1e-6:  # straight down or up, along scene.json's fixed up: lean it a hair
                fwd = _unit([0, fwd[1], -1e-3])
                notes.append("a camera looking straight down or up (tilted 0.06 degrees)")
            elif abs(world[1]) > 1e-6 * math.hypot(*world[0:3]):  # its x axis tilts out of level: rolled
                notes.append("a camera's roll (scene.json's camera keeps +Y up)")
            fov = math.degrees(2 * math.atan(math.tan(math.radians(o.get("fov", 50)) / 2) / (o.get("zoom") or 1)))
            near, far = o.get("near", 0.1), o.get("far", 2000)
            if far > near * 10000:
                notes.append(f"the camera's far {far} (clipped to 10,000 x near)")
            cam = {"fov": min(179, max(1, round(fov, 6))), "near": near, "far": min(far, near * 10000),
                   "position": _tidy(p), "target": _tidy(a + b * d for a, b in zip(p, fwd))}
            camera = None if refused({"camera": cam}, "the camera") else cam
        elif t not in ("Scene", "Group", "Object3D"):
            notes.append(f"a {_show(t)}")
        for ch in o.get("children") or []:
            if isinstance(ch, dict):
                walk(ch, world)

    root = doc["object"]
    walk(root, IDENTITY)
    if not meshes:
        raise ValueError("no mesh scene.json can draw" + (f" (left out: {', '.join(notes[:20])})" if notes else ""))
    scene = {"camera": camera or fallback, "materials": {k: v for k, v in materials.items() if k in {m["material"] for m in meshes}},
             "meshes": meshes, "lights": lights}
    if isinstance(root.get("background"), int):
        scene["background"] = _hex(root["background"])
    elif root.get("background") is not None:
        notes.append("a background texture")
    notes += ["fog"] * bool(root.get("fog")) + ["animations"] * bool(doc.get("animations"))
    notes += ["a directional light's target (scene.json's shines toward the origin)"] * any(u in seen for u in targets)
    if camera is None:
        notes.append("the camera (none in the file: three's usual fov 50 at [0, 0, 5])")
    counts = collections.Counter(notes)
    left = [w if n == 1 else f"{w} (x{n})" for w, n in counts.items()]
    return validate(scene), left if len(left) <= 20 else left[:20] + [f"{len(left) - 20} more kinds"]


GLSL = re.compile(r"```glsl\n(.*?)```", re.S)
SHADER_FILES = (".frag", ".vert", ".glsl")


def shader_problems(path, validator=None):
    """T-1530: offline checks of a shader card (looks/shader-*.md) or a .frag/.vert/.glsl file: each GLSL block has a
    main and compiles under glslangValidator when one is given (its stage from gl_Position: vertex, else fragment),
    and a card declares exactly the uniforms its ## Uniforms table lists. [problem], empty when it passes."""
    import subprocess
    import tempfile
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    blocks = GLSL.findall(text) if path.endswith(".md") else [text]
    name = os.path.basename(path)
    if not blocks:  # a guidance card (shader-performance) or an empty file: nothing to compile
        return [] if path.endswith(".md") else [f"{name}: empty"]
    problems, declared = [], set()
    for n, block in enumerate(blocks, 1):
        where = f"{name} block {n}" if len(blocks) > 1 else name
        if not re.search(r"\bvoid\s+main\s*\(", block):
            problems.append(f"{where}: no void main()")
        for decl in re.findall(r"\buniform\s+\w+\s+([^;{]+);", block):
            declared |= {re.sub(r"\[.*", "", x).strip() for x in decl.split(",")}
        if validator:
            from sampo import core
            stage = "vert" if "gl_Position" in block else "frag"
            with tempfile.TemporaryDirectory(prefix="fm-shader-") as tmp:
                src = os.path.join(tmp, f"shader.{stage}")
                with open(src, "w", encoding="utf-8") as fh:
                    fh.write(block)
                try:
                    r = subprocess.run([validator, src], capture_output=True, text=True, timeout=30,
                                       stdin=subprocess.DEVNULL)
                except (OSError, subprocess.SubprocessError) as e:
                    problems.append(f"{where}: {validator} didn't run ({e})")
                    continue
            if r.returncode:
                said = [x.strip() for x in (r.stdout + r.stderr).splitlines() if "ERROR" in x]
                problems.append(f"{where}: doesn't compile as GLSL ({stage}): "
                                + core.joinmore([x.replace(src, "shader") for x in said], 3, "; "))
    table = re.search(r"(?ms)^## Uniforms\n(.*?)(?=^## |\Z)", text) if path.endswith(".md") else None
    if table:
        listed = {x for row in table.group(1).splitlines() if row.startswith("| `")
                  for x in re.findall(r"`(\w+)`", row.split("|")[1])}
        problems += [f"{name}: the Uniforms table lists {u}, which no block declares" for u in sorted(listed - declared)]
        problems += [f"{name}: {u} is declared but not in the Uniforms table" for u in sorted(declared - listed)]
    return problems


def shader_paths(path):
    """The shader cards and files a path names: itself, or the shader-*.md and GLSL files under a folder."""
    if not os.path.isdir(path):
        return [path]
    return sorted(os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs
                  if f.endswith(SHADER_FILES) or f.startswith("shader-") and f.endswith(".md"))


GLTF_MAX = 256 << 20  # bytes of a .gltf or .glb read at most
COMPONENT = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


def gltf_problems(path):
    """T-1530: offline structural checks of a .gltf or .glb: the GLB header and chunks, asset.version 2.x, every index
    in range (scene nodes, node children and meshes, primitive accessors and materials, accessor views, view buffers),
    each accessor inside its view, each view inside its buffer, and each buffer as long as it says (a .glb's BIN chunk,
    a base64 data URI, or a file beside it: a URI leaving the asset's folder is a problem, never read). [problem]"""
    name = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read(GLTF_MAX + 1)
    if len(data) > GLTF_MAX:
        return [f"{name}: over {GLTF_MAX >> 20} MB"]
    problems, chunk = [], None
    if data[:4] == b"glTF" or path.lower().endswith(".glb"):
        if len(data) < 20 or data[:4] != b"glTF":
            return [f"{name}: not a GLB (no glTF magic)"]
        version, length = struct.unpack_from("<II", data, 4)
        if version != 2:
            problems.append(f"{name}: GLB version {version}, not 2")
        if length != len(data):
            problems.append(f"{name}: the header says {length} bytes; the file has {len(data)}")
        size, kind = struct.unpack_from("<II", data, 12)
        if kind != 0x4E4F534A:
            return problems + [f"{name}: the first chunk isn't JSON"]
        text, at = data[20:20 + size], 20 + size
        if at + 8 <= len(data) and struct.unpack_from("<II", data, at)[1] == 0x004E4942:
            chunk = len(data[at + 8:at + 8 + struct.unpack_from("<I", data, at)[0]])
    else:
        text = data
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as e:
        return problems + [f"{name}: not JSON ({str(e)[:80]})"]
    if not isinstance(doc, dict):
        return problems + [f"{name}: not a glTF object"]
    version = str((doc.get("asset") or {}).get("version", "")) if isinstance(doc.get("asset"), dict) else ""
    if not version.startswith("2."):
        problems.append(f"{name}: asset.version is {version!r}, not 2.x")

    def rows(key):
        got = doc.get(key, [])
        return [x if isinstance(x, dict) else {} for x in got] if isinstance(got, list) else []

    def index(where, i, key):
        n = len(rows(key))
        if not (isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n):
            problems.append(f"{name}: {where}: {key} index {i!r} out of range ({f'0-{n - 1}' if n else 'there are none'})")
            return False
        return True

    for s, sc in enumerate(rows("scenes")):
        for i in sc.get("nodes", []):
            index(f"scenes[{s}].nodes", i, "nodes")
    for n, node in enumerate(rows("nodes")):
        for i in node.get("children", []):
            index(f"nodes[{n}].children", i, "nodes")
        if "mesh" in node:
            index(f"nodes[{n}].mesh", node["mesh"], "meshes")
    for m, mesh in enumerate(rows("meshes")):
        for k, prim in enumerate(mesh.get("primitives", [])):
            prim = prim if isinstance(prim, dict) else {}
            for attr, i in (prim.get("attributes") or {}).items():
                index(f"meshes[{m}].primitives[{k}].{attr}", i, "accessors")
            for key, rows_of in (("indices", "accessors"), ("material", "materials")):
                if key in prim:
                    index(f"meshes[{m}].primitives[{k}].{key}", prim[key], rows_of)
    folder, lengths = os.path.dirname(os.path.abspath(path)), []
    for b, buf in enumerate(rows("buffers")):
        uri, has = buf.get("uri"), None
        if uri is None:
            has = chunk if b == 0 else None
        elif isinstance(uri, str) and uri.startswith("data:"):
            try:
                has = len(base64.b64decode(uri.split(",", 1)[1], validate=True))
            except (IndexError, ValueError):
                problems.append(f"{name}: buffers[{b}]: its data URI isn't base64")
        elif isinstance(uri, str):
            target = os.path.realpath(os.path.join(folder, uri))
            if os.path.isabs(uri) or not target.startswith(os.path.realpath(folder) + os.sep):
                problems.append(f"{name}: buffers[{b}]: {uri} is outside the asset's folder")
            elif not os.path.isfile(target):
                problems.append(f"{name}: buffers[{b}]: {uri} isn't there")
            else:
                has = os.path.getsize(target)
        want = buf.get("byteLength")
        if has is not None and isinstance(want, int) and has < want:
            problems.append(f"{name}: buffers[{b}] declares {want} bytes, has {has}")
        lengths.append(want if isinstance(want, int) else 0)
    views = rows("bufferViews")
    for v, view in enumerate(views):
        if index(f"bufferViews[{v}].buffer", view.get("buffer"), "buffers"):
            end = (view.get("byteOffset") or 0) + (view.get("byteLength") or 0)
            if end > lengths[view["buffer"]]:
                problems.append(f"{name}: bufferViews[{v}] ends at byte {end} of buffers[{view['buffer']}], which "
                                f"has {lengths[view['buffer']]}")
    for a, acc in enumerate(rows("accessors")):
        if "bufferView" not in acc or not index(f"accessors[{a}].bufferView", acc["bufferView"], "bufferViews"):
            continue
        size = COMPONENT.get(acc.get("componentType"), 0) * WIDTH.get(acc.get("type"), 0)
        view, count = views[acc["bufferView"]], acc.get("count") or 0
        if not size:
            problems.append(f"{name}: accessors[{a}]: componentType {acc.get('componentType')!r} or type "
                            f"{acc.get('type')!r} isn't glTF's")
        elif count > 0:
            need = (acc.get("byteOffset") or 0) + (view.get("byteStride") or size) * (count - 1) + size
            if need > (view.get("byteLength") or 0):
                problems.append(f"{name}: accessors[{a}] needs {need} bytes of bufferViews[{acc['bufferView']}], "
                                f"which has {view.get('byteLength') or 0}")
    return problems


def find_three(*starts):
    """T-1530: a three.js build for the soak: FOREMAN_THREE (its three.module.js), else node_modules/three/build/ in a
    start folder or above it; None when there's none."""
    env = os.environ.get("FOREMAN_THREE")
    if env:
        return env if os.path.isfile(env) else None
    for start in starts:
        d = os.path.abspath(start)
        while True:
            hit = os.path.join(d, "node_modules", "three", "build", "three.module.js")
            if os.path.isfile(hit):
                return hit
            if os.path.dirname(d) == d:
                break
            d = os.path.dirname(d)
    return None


SOAK_PAGE = """<!doctype html><meta charset="utf-8"><title>fm scene soak</title>
<style>html,body{margin:0;background:#000}figure{margin:0;position:relative;width:640px;height:400px}
canvas{position:absolute;inset:0;width:100%;height:100%}</style>
<script type="importmap">{"imports": {"three": "./three/three.module.js"}}</script>
<figure class="scene" data-state="poster"><canvas role="img" aria-label="soak"></canvas></figure>
<script>
// The soak's own shim, never the module's: headless Chrome draws WebGL with SwiftShader, which the module's
// failIfMajorPerformanceCaveat refuses on purpose (a real page keeps its poster rather than draw in software).
const getContext = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function (kind, attrs) {
  return getContext.call(this, kind, attrs && { ...attrs, failIfMajorPerformanceCaveat: false });
};
import("./scene.js").then(m => { window.__dispose = m.mount(document.querySelector("figure")); },
                          e => { window.__error = String(e && e.stack || e); });
</script>
"""
SAMPLE = ("JSON.stringify({s: globalThis.__fmScene ? {...globalThis.__fmScene, frame_ms: undefined, "
          "ms: globalThis.__fmScene.frame_ms.slice()} : null, state: document.querySelector('figure').dataset.state, "
          "error: window.__error ?? null, heap: performance.memory ? performance.memory.usedJSHeapSize : null})")


def _p95(times):
    return sorted(times)[max(0, -(-len(times) * 95 // 100) - 1)] if times else None


def soak_problems(samples, after, budget_ms=None):
    """T-1530: the soak's verdict from its samples ({state, s: __fmScene or None, error}) and the state after
    dispose(): it drew, its geometries and textures held steady (growth is a leak), the p95 frame time is within
    budget_ms when one is given, and dispose() put the poster back."""
    drawn = [x for x in samples if x.get("s")]
    if not drawn:
        last = samples[-1] if samples else {}
        return [f"it never drew (state {last.get('state')!r}{': ' + str(last.get('error'))[:200] if last.get('error') else ''})"]
    first, last, problems = drawn[0]["s"], drawn[-1]["s"], []
    for key in ("geometries", "textures"):
        if last.get(key, 0) > first.get(key, 0):
            problems.append(f"{key} grew from {first.get(key, 0)} to {last.get(key, 0)} while it ran: a leak")
    p95 = _p95(last.get("frame_ms") or last.get("ms") or [])
    if budget_ms is not None and p95 is not None and p95 > budget_ms:
        problems.append(f"p95 frame time {p95:g} ms is over the {budget_ms:g} ms budget")
    if after != "poster":
        problems.append(f"still {after} after dispose(): it should be back at the poster")
    return problems


def soak(path, three, seconds=10, budget_ms=None):
    """T-1530: the scene compiled with --hud, mounted in headless Chrome (SwiftShader, through fmshot) from a local
    server on 127.0.0.1, sampled once a second for `seconds`, then disposed: {frames, p95_ms, geometries, textures,
    heap, after, problems}, or {skipped: why} with no browser or no three.js build. Optional: nothing is installed."""
    import functools
    import http.server
    import shutil
    import tempfile
    import threading
    import time
    from sampo import shot
    scene = load(path)
    browser, why = shot.find_browser("fm scene soak")
    if not browser:
        return {"skipped": why}
    if not three or not os.path.isfile(three):
        return {"skipped": "no three.js build: set FOREMAN_THREE to a three.module.js, or install three in "
                           "node_modules beside the scene"}
    with tempfile.TemporaryDirectory(prefix="fm-soak-") as tmp:
        os.makedirs(os.path.join(tmp, "three"))
        for part in ("three.module.js", "three.core.js"):  # r170+ splits the build in two
            src = os.path.join(os.path.dirname(three), part)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(tmp, "three", part))
        with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as f:
            f.write(SOAK_PAGE)
        with open(os.path.join(tmp, "scene.js"), "w", encoding="utf-8") as f:
            f.write(compile_three(scene, hud=True))

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=tmp))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        b, samples, after = None, [], None
        try:
            b = shot.Browser(browser, (640, 400))
            shot.load(b, f"http://127.0.0.1:{server.server_address[1]}/index.html")

            def ask(expr):
                got = b.call("Runtime.evaluate", b.sid, expression=expr, returnByValue=True)
                return (got.get("result") or {}).get("value")
            end = time.monotonic() + seconds
            while True:
                samples.append(json.loads(ask(SAMPLE) or "{}"))
                if time.monotonic() >= end:
                    break
                time.sleep(1)
            after = ask("window.__dispose && window.__dispose(), document.querySelector('figure').dataset.state")
        finally:
            if b:
                b.close()
            server.shutdown()
            server.server_close()
    for x in samples:
        if x.get("s"):
            x["s"]["frame_ms"] = x["s"].pop("ms", [])
    drawn = [x["s"] for x in samples if x.get("s")]
    p95 = _p95(drawn[-1]["frame_ms"]) if drawn else None
    return {"frames": drawn[-1]["frames"] if drawn else 0, "p95_ms": None if p95 is None else round(p95, 2),
            "geometries": [x["geometries"] for x in drawn], "textures": [x["textures"] for x in drawn],
            "heap": [x.get("heap") for x in samples], "after": after,
            "problems": soak_problems(samples, after, budget_ms)}


# ---------------------------------------------------------------- the shader sketchbook (T-1784)

LOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__)))), "skills", "library", "cards", "looks")
UNIFORM_DECL = re.compile(r"(?m)^\s*uniform\s+(?:(?:highp|mediump|lowp)\s+)?(float|vec2|vec3|vec4)\s+([^;]+);")
COMPONENTS = {"float": 1, "vec2": 2, "vec3": 3, "vec4": 4}


def uniforms(glsl):
    """{name: components} of a shader's float and vector uniforms."""
    return {n.strip(): COMPONENTS[t] for t, names in UNIFORM_DECL.findall(glsl) for n in names.split(",")}


def sketch(text):
    """A card's ## Sketch line, its preset in its last backticks (`uA=1 uB=0.5,0.25 uP=sweep`), as {uniform: [floats] or
    "sweep"}: sweep runs a float from 0 to 1 and back, for a uniform no clock drives."""
    spans = re.findall(r"`([^`]*)`", text)  # the last span: the line may name a command first
    preset = spans[-1].strip() if spans else ""
    if not preset:
        raise ValueError("a Sketch line holds its preset in backticks: `uName=1 uColour=0.1,0.2,0.3`")
    out = {}
    for word in preset.split():
        name, _, value = word.partition("=")
        if not re.fullmatch(r"u[A-Za-z0-9]+", name) or not value:
            raise ValueError(f"{word!r}: uName=VALUE")
        nums = None if value == "sweep" else [float(v) for v in value.split(",")]  # a ValueError on a non-number
        if nums is not None and not all(math.isfinite(v) for v in nums):
            raise ValueError(f"{word!r}: finite numbers only")
        out[name] = nums if nums is not None else "sweep"
    return out


def sketch_cards(folder=None):
    """Every shader look card with a Sketch line: [{name, title, about, glsl, sketch}], by name."""
    folder = folder or LOOKS
    out = []
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("shader-") and name.endswith(".md")):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            text = f.read()
        title = re.search(r'(?m)^title:\s*"?([^"\n]+)"?\s*$', text)
        about = re.search(r"(?s)^---\n.*?\n---\n+(.+?)\n\n", text)
        glsl = GLSL.search(text)
        line = re.search(r"(?ms)^## Sketch\n(.*?)(?=^## |\Z)", text)
        if glsl and line:
            out.append({"name": name[:-3], "title": title.group(1) if title else name[:-3],
                        "about": about.group(1).strip() if about else "", "glsl": glsl.group(1),
                        "sketch": sketch(line.group(1))})
    return out


SKETCH_CSS = (".sketchbook{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:20px}"
              ".sketch [hidden]{display:none}.sketch{margin:0}.sketch canvas{display:block;width:100%;height:auto;aspect-ratio:8/5;border-radius:8px}"
              ".sketch .fallback{margin:0;padding:12px;border-radius:8px;outline:1px dashed currentColor;opacity:.8}"
              ".sketch figcaption{margin-top:6px}.sketch code{opacity:.6;font-size:.8em}")
SKETCH_JS = r"""// T-1784: each card's fragment shader on its canvas, drawn only while on screen; one still frame under reduced
// motion; the card's words in place of a canvas when WebGL2 is missing or a shader won't compile.
const VS = "#version 300 es\nout vec2 vUv;\nvoid main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2); " +
  "vUv = p; gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }";
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
const live = [];
for (const fig of document.querySelectorAll("figure.sketch")) {
  const canvas = fig.querySelector("canvas"), note = fig.querySelector(".fallback");
  const gl = canvas.getContext("webgl2");
  const fail = () => { canvas.hidden = true; note.hidden = false; };
  if (!gl) { fail(); continue; }
  const shader = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
  };
  const vs = shader(gl.VERTEX_SHADER, VS);
  const fs = shader(gl.FRAGMENT_SHADER, document.getElementById(fig.dataset.shader).textContent.trim());
  const prog = gl.createProgram();
  if (vs && fs) { gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog); }
  if (!vs || !fs || !gl.getProgramParameter(prog, gl.LINK_STATUS)) { fail(); continue; }
  note.hidden = true;
  gl.useProgram(prog);
  gl.bindVertexArray(gl.createVertexArray());
  const set = (name, v) => { const at = gl.getUniformLocation(prog, name); if (at !== null) gl["uniform" + v.length + "fv"](at, v); };
  const sweep = [];
  for (const [k, v] of Object.entries(JSON.parse(fig.dataset.uniforms))) v === "sweep" ? sweep.push(k) : set(k, v);
  set("uRes", [canvas.width, canvas.height]);
  const draw = (t, s) => {
    set("uTime", [t]);
    for (const k of sweep) set(k, [s ?? 0.5 - 0.5 * Math.cos(t * 0.9)]);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  };
  draw(0, 1);  // the cards' own still (uTime 0, a swept uniform at its end) for a reader who asked for less motion
  live.push({ fig, draw, seen: false });
}
if (!still && live.length) {
  const io = new IntersectionObserver((es) => es.forEach((e) => {
    for (const s of live) if (s.fig === e.target) s.seen = e.isIntersecting;
  }));
  live.forEach((s) => io.observe(s.fig));
  const t0 = performance.now();
  const tick = (now) => { const t = 2.0 + (now - t0) / 1000; for (const s of live) if (s.seen) s.draw(t); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}
"""


def sketchbook_html(cards, page=True):
    """One thumbnail per card: a canvas running its fragment shader with the card's Sketch preset, the card's
    description as the fallback (shown until a shader runs, so it stays without JavaScript), and the shader sources
    as non-executed script elements. page=False: the fragment alone, for the site."""
    import html
    figs, sources = [], []
    for c in cards:
        if re.search(r"(?i)</script", c["glsl"]):  # it would end its element early: left out
            continue
        sid = "sh-" + c["name"]
        figs.append(f'<figure class="sketch" data-shader="{sid}" data-uniforms="{html.escape(json.dumps(c["sketch"]))}">'
                    f'<canvas width="320" height="200" role="img" aria-label="{html.escape(c["title"])}"></canvas>'
                    f'<p class="fallback">{html.escape(c["about"])}</p><figcaption><b>'
                    f'{html.escape(c["title"].removeprefix("Shader: "))}</b> <code>{html.escape(c["name"])}</code>'
                    f'</figcaption></figure>')
        sources.append(f'<script type="x-shader/x-fragment" id="{sid}">\n{c["glsl"]}</script>')
    body = (f'<style>{SKETCH_CSS}</style><div class="sketchbook">{"".join(figs)}</div>{"".join(sources)}'
            f'<script type="module">\n{SKETCH_JS}</script>')
    if not page:
        return body
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-'
            'width,initial-scale=1"><title>Shader sketchbook</title><style>body{font:15px/1.5 system-ui,sans-serif;'
            'margin:24px;background:#fafaf7;color:#1d1d1f}@media (prefers-color-scheme:dark){body{background:#15161a;'
            f'color:#e8e8ea}}}}</style></head><body><h1>Shader sketchbook</h1>{body}</body></html>\n')


TETS = ((1, 3), (1, 5), (2, 3), (2, 6), (4, 5), (4, 6))  # corner 0 → a → b → 7: six tets that agree on shared faces


def metaballs(balls=5, grid=32, seed=0):
    """T-1848: a scene.json of a metaball blob: the field sum r²/d² of `balls` seeded balls, polygonised where it is 1
    by marching tetrahedra on a grid³ box (each cube cut into six tets round its diagonal, so neighbours share their
    cuts and the surface closes), one vertices mesh wound counter-clockwise seen from outside, over a floor, turning.
    The box grows until the field is under 0.9 on all of it, so the blob never touches its sides. ValueError for
    balls outside 1-12 or grid outside 8-48."""
    import random
    if not 1 <= balls <= 12:
        raise ValueError(f"--balls {balls}: 1 to 12")
    if not 8 <= grid <= 48:
        raise ValueError(f"--grid {grid}: 8 to 48")
    rnd = random.Random(seed)
    bs = [([rnd.uniform(-0.8, 0.8) for _ in range(3)], rnd.uniform(0.35, 0.6)) for _ in range(balls)]
    half = max(max(map(abs, c)) + r for c, r in bs)
    while sum(r * r / (half - max(map(abs, c))) ** 2 for c, r in bs) >= 0.9:  # a box point is that far from a ball
        half *= 1.1
    n, step = grid + 1, 2 * half / grid
    pts = [(-half + i * step, -half + j * step, -half + k * step) for k in range(n) for j in range(n) for i in range(n)]
    val = [sum(r * r / ((x - c[0]) ** 2 + (y - c[1]) ** 2 + (z - c[2]) ** 2 + 1e-12) for c, r in bs) for x, y, z in pts]
    verts, index, tris = [], {}, []

    def vertex(a, b):  # where the surface crosses grid edge a-b, made once and shared by every tet on that edge
        key = (a, b) if a < b else (b, a)
        if key not in index:
            t = (1 - val[a]) / (val[b] - val[a])
            index[key] = len(verts)
            verts.append([p + t * (q - p) for p, q in zip(pts[a], pts[b])])
        return index[key]

    def det(o, p, q, s):  # the sign of the tet's volume: which way round its faces go
        u, v, w = ([pts[x][i] - pts[o][i] for i in range(3)] for x in (p, q, s))
        return sum(a * b for a, b in zip(u, _cross3(v, w)))

    corner = [0, 1, n, n + 1, n * n, n * n + 1, n * n + n, n * n + n + 1]  # bit 1: +x, 2: +y, 4: +z
    for k in range(grid):
        for j in range(grid):
            for i in range(grid):
                cube = [i + j * n + k * n * n + d for d in corner]
                inside = sum(val[p] > 1 for p in cube)
                if inside in (0, 8):
                    continue
                for a, b in TETS:
                    tet = (cube[0], cube[a], cube[b], cube[7])
                    inn, out = [p for p in tet if val[p] > 1], [p for p in tet if val[p] <= 1]
                    if len(inn) in (1, 3):  # one corner apart: a triangle round it, facing away from the inside
                        one, rest = (inn[0], out) if len(inn) == 1 else (out[0], inn)
                        t = [vertex(one, p) for p in rest]
                        if (det(one, *rest) > 0) != (len(inn) == 1):
                            t.reverse()
                        tris.append(t)
                    elif len(inn) == 2:  # two and two: a quad between the inside edge and the outside one
                        (p, q), (r, s) = inn, out
                        quad = [vertex(p, r), vertex(p, s), vertex(q, s), vertex(q, r)]
                        if det(p, q, r, s) < 0:
                            quad.reverse()
                        tris += [quad[:3], [quad[0], quad[2], quad[3]]]
    if not verts:
        raise ValueError(f"--grid {grid} is too coarse to catch any ball: raise it")
    floor = min(v[1] for v in verts) - 0.02
    return validate({
        "background": "#101218",
        "camera": {"fov": 40, "position": [0, round(half, 3), round(half * 3.2, 3)], "target": [0, 0, 0], "far": 100},
        "lights": [{"type": "hemisphere", "sky": "#dfe8ff", "ground": "#2a221a", "intensity": 0.6},
                   {"type": "directional", "position": [3, 5, 4], "intensity": 1.6},
                   {"type": "directional", "position": [-4, 2, -3], "color": "#7aa2ff", "intensity": 0.5}],
        "materials": {"blob": {"color": "#e0569a", "roughness": 0.25, "metalness": 0.1},
                      "floor": {"color": "#22252e", "roughness": 0.9}},
        "meshes": [{"name": "blob", "material": "blob",
                    "geometry": {"type": "vertices", "vertices": [_tidy(v) for v in verts], "faces": tris}},
                   {"name": "floor", "material": "floor", "geometry": {"type": "plane", "size": [half * 6, half * 6]},
                    "position": [0, round(floor, 6), 0], "rotation": [-90, 0, 0]}],
        "animations": {"spin": {"target": "blob", "property": "rotation", "times": [0, 12],
                                "values": [[0, 0, 0], [0, 360, 0]]}}})


TERRAIN = ("#e0569a", "#5aa9e6", "#f2c14e", "#6cc58a", "#b07cf0", "#f08a5d", "#4fd1c5", "#e8e8e8", "#9aa5b1", "#d4a373")


def terrain(rows, names):
    """T-1851: churn as terrain: rows[i][t], the lines changed under names[i] in column t → a scene.json with a ribbon
    per row (its own colour, a mesh named after its path), log1p(lines) high with the tallest at 1.5, its front
    dropped to the floor so it reads as a ridge. ValueError for no rows, over 10 rows, under 2 or over 40 columns,
    rows of unequal length or a negative count."""
    if not 1 <= len(rows) <= 10 or len(rows) != len(names):
        raise ValueError(f"1 to 10 rows, one name each, not {len(rows)} rows and {len(names)} names")
    cols = len(rows[0])
    if not 2 <= cols <= 40 or any(len(r) != cols for r in rows) or any(v < 0 for r in rows for v in r):
        raise ValueError("rows of 2 to 40 counts each, all the same length, none negative")
    top = max(math.log1p(v) for r in rows for v in r) or 1
    meshes, materials, taken = [], {"floor": {"color": "#1b1e26", "roughness": 0.9}}, {"floor", "camera"}
    for i, (r, path) in enumerate(zip(rows, names)):
        n = re.sub(r"[^A-Za-z0-9_-]", "-", path)[:60]
        n = n if n[:1].isalpha() else f"p-{n}"
        base, k = n[:56], i + 1
        while n in taken:  # T-1851 review: the renamed one may be taken too
            n, k = f"{base}-{k}", k + 1
        taken.add(n)
        z, verts = (i - (len(rows) - 1) / 2) * 1.2, []
        for t, v in enumerate(r):  # per column: the top's back and front edge, the front's foot
            x, y = round(16 * t / (cols - 1) - 8, 6), round(0.02 + 1.5 * math.log1p(v) / top, 6)
            verts += [[x, y, z - 0.4], [x, y, z + 0.4], [x, 0, z + 0.4]]
        tris = [f for t in range(cols - 1) for a in (3 * t,) for f in
                ([a, a + 1, a + 4], [a, a + 4, a + 3], [a + 2, a + 5, a + 4], [a + 2, a + 4, a + 1])]  # up, toward +z
        materials[f"row{i}"] = {"color": TERRAIN[i], "roughness": 0.6}
        meshes.append({"name": n, "material": f"row{i}", "geometry": {"type": "vertices", "vertices": verts,
                                                                      "faces": tris}})
    meshes.append({"name": "floor", "material": "floor", "geometry": {"type": "plane", "size": [20, 16]},
                   "rotation": [-90, 0, 0]})
    return validate({
        "background": "#101218", "materials": materials, "meshes": meshes,
        "camera": {"fov": 40, "position": [0, 9, 15], "target": [0, 0.4, 0], "far": 100},
        "lights": [{"type": "hemisphere", "sky": "#dfe8ff", "ground": "#2a221a", "intensity": 0.7},
                   {"type": "directional", "position": [-4, 8, 6], "intensity": 1.4},
                   {"type": "directional", "position": [6, 3, -4], "color": "#7aa2ff", "intensity": 0.4}]})


def churn(root, days):
    """T-1851: (rows, names) for terrain: lines added and deleted per top-level path in root's git log over the last
    `days` days, a column per day from the oldest commit in range (at most 40), the 10 busiest paths first.
    ValueError outside git or with nothing in range."""
    import collections
    import subprocess
    import time
    try:  # quotepath off: café/ arrives as café/, not "caf\303\251/"
        r = subprocess.run(["git", "-C", root, "-c", "core.quotepath=false", "log", f"--since={days}.days", "--numstat",
                            "--no-renames", "--format=@%ct"], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise ValueError("git log took over 60 s: try fewer --days") from None
    if r.returncode:
        raise ValueError(f"git log: {r.stderr.strip() or 'failed'}")
    rec, when = [], None
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if line.startswith("@"):
            when = int(line[1:])
        elif len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and when:  # "-": a binary file
            rec.append((when, parts[2].split("/")[0], int(parts[0]) + int(parts[1])))
    if not rec:
        raise ValueError(f"no lines changed in the last {days} days")
    start = min(w for w, _, _ in rec)  # from the oldest commit in range: a young repo fills the floor
    span = max(time.time() - start, 1)
    cols = min(40, max(2, math.ceil(span / 86400)))
    per = collections.defaultdict(lambda: [0] * cols)
    for w, path, n in rec:
        per[path][min(cols - 1, int((w - start) / span * cols))] += n
    names = sorted(per, key=lambda k: (-sum(per[k]), k))[:10]
    return [per[k] for k in names], names


AIRLOCK = ("#e0569a", "#f08a5d", "#f2c14e", "#e8685a", "#d4a373", "#c9708f")


def airlock(held, through):
    """T-1849: the guard as a gate. Before it a box per category it held, past it one green box for the tool calls it
    let through; heights log-scaled to the largest, so 3 blocks still show beside 3,000 calls."""
    import collections
    if through < 0 or any(v < 0 for v in held.values()):
        raise ValueError("counts can't be negative")
    gate, named = {"pillar-l", "pillar-r", "lintel", "through", "floor", "camera"}, collections.Counter()
    for k, v in held.items():  # a name a mesh can't carry, or one the gate uses, joins other
        if v:
            named[k if re.fullmatch(r"[a-z][a-z0-9-]{0,40}", str(k)) and k not in gate else "other"] += v
    top = max([math.log1p(v) for v in named.values()] + [math.log1p(through)]) or 1

    def box(name, material, size, x, z, y=None):
        return {"name": name, "material": material, "geometry": {"type": "box", "size": size},
                "position": [x, size[1] / 2 if y is None else y, z]}

    def h(v):
        return round(0.1 + 2 * math.log1p(v) / top, 6)
    materials = {"gate": {"color": "#9aa5b1", "roughness": 0.5}, "through": {"color": "#6cc58a", "roughness": 0.6},
                 "floor": {"color": "#1b1e26", "roughness": 0.9}}
    meshes = [box("pillar-l", "gate", [0.5, 3, 0.5], 0, -2.2), box("pillar-r", "gate", [0.5, 3, 0.5], 0, 2.2),
              box("lintel", "gate", [0.7, 0.4, 4.9], 0, 0, 3.2), box("through", "through", [1.6, h(through), 1.6], 2.4, 0)]
    for j, (k, v) in enumerate(sorted(named.items(), key=lambda kv: (-kv[1], kv[0]))):  # rows of 3 back from the gate
        materials[f"held{j}"] = {"color": AIRLOCK[j % len(AIRLOCK)], "roughness": 0.6}
        meshes.append(box(k, f"held{j}", [1, h(v), 1], -2 - (j // 3) * 1.3, (j % 3 - 1) * 1.3))
    meshes.append({"name": "floor", "material": "floor", "geometry": {"type": "plane", "size": [20, 12]},
                   "rotation": [-90, 0, 0]})
    return validate({
        "background": "#101218", "materials": materials, "meshes": meshes,
        "camera": {"fov": 40, "position": [5, 6, 11], "target": [-1, 1, 0], "far": 100},
        "lights": [{"type": "hemisphere", "sky": "#dfe8ff", "ground": "#2a221a", "intensity": 0.7},
                   {"type": "directional", "position": [-4, 8, 6], "intensity": 1.4},
                   {"type": "directional", "position": [6, 3, -4], "color": "#7aa2ff", "intensity": 0.4}]})


def bench(runs):
    """T-1850: fm code bench's last 12 runs as a diorama, a box per seed per run: runs left to right, seeds front to
    back, height log-scaled to the slowest, green when the seed closed, red when it didn't. Skipped seeds have no box."""
    runs = runs[-12:]

    def secs(v):  # a hand-edited history: a list, inf, nan or 10**400 reads as 0, not a crash
        return v if isinstance(v, (int, float)) and 0 <= v <= 1e6 else 0
    cells = [(i, str(r.get("seed")), bool(r.get("closed")), secs(r.get("seconds")))
             for i, run in enumerate(runs) for r in (run.get("results") if isinstance(run.get("results"), list) else ())
             if isinstance(r, dict) and not r.get("skipped")]
    if not cells:
        raise ValueError("no fm code bench results to draw: fm code bench makes them")
    seeds = sorted({c[1] for c in cells})
    top = max(math.log1p(c[3]) for c in cells) or 1
    meshes, taken = [], {"floor", "camera"}
    for i, seed, closed, secs in cells:
        base = re.sub(r"[^A-Za-z0-9_-]", "-", seed)[:48]
        n = first = (base if base[:1].isalpha() else "s" + base) + f"-{i + 1}"
        k = 2
        while n in taken:  # a seed twice in one run, or two seeds that clean to the same name
            n, k = f"{first[:58]}_{k}", k + 1
        taken.add(n)
        h = round(0.1 + 2 * math.log1p(secs) / top, 6)
        meshes.append({"name": n, "material": "closed" if closed else "open", "geometry": {"type": "box",
                       "size": [0.8, h, 0.8]}, "position": [round((i - (len(runs) - 1) / 2) * 1.2, 6), h / 2,
                                                           round((seeds.index(seed) - (len(seeds) - 1) / 2) * 1.2, 6)]})
    w, d = len(runs) * 1.2 + 4, len(seeds) * 1.2 + 4
    meshes.append({"name": "floor", "material": "floor", "geometry": {"type": "plane", "size": [w, d]},
                   "rotation": [-90, 0, 0]})
    return validate({
        "background": "#101218", "meshes": meshes,
        "materials": {"closed": {"color": "#6cc58a", "roughness": 0.6}, "open": {"color": "#e8685a", "roughness": 0.6},
                      "floor": {"color": "#1b1e26", "roughness": 0.9}},
        "camera": {"fov": 40, "position": [round(w * 0.3, 3), round(3 + w * 0.3, 3), round(d / 2 + w * 0.7, 3)],
                   "target": [0, 0.8, 0], "far": 200},
        "lights": [{"type": "hemisphere", "sky": "#dfe8ff", "ground": "#2a221a", "intensity": 0.7},
                   {"type": "directional", "position": [-4, 8, 6], "intensity": 1.4},
                   {"type": "directional", "position": [6, 3, -4], "color": "#7aa2ff", "intensity": 0.4}]})


def lookbook(names, known):
    """T-1852: the look cards hung in a gallery, best first: a dark frame per look on a U of walls (four along the
    back wall's lower row, centre out, then the side walls' back halves, then a row above the first), its palette as
    colour-field bands top to bottom. Every frame is in the camera's view. known: fmtaste.looks()."""
    if not 1 <= len(names) <= 12:
        raise ValueError(f"{len(names)} looks: 1 to 12 hang")

    def back(y):  # (centre, along, out of the wall); 5.95: a frame's back on the wall's inner face
        return [((x, y, -5.95), (1, 0, 0), (0, 0, 1)) for x in (-1.4, 1.4, -4.2, 4.2)]
    sides = [s for z in (-4.2, -1.4) for s in (((-5.95, 1.6, z), (0, 0, 1), (1, 0, 0)),
                                              ((5.95, 1.6, z), (0, 0, -1), (-1, 0, 0)))]
    slots = back(1.6) + sides + back(3.7)

    def box(name, material, size, at):
        return {"name": name, "material": material, "geometry": {"type": "box", "size": [round(v, 6) for v in size]},
                "position": [round(v, 6) for v in at]}
    materials = {"frame": {"color": "#2a2d36", "roughness": 0.5}, "wall": {"color": "#e9e4da", "roughness": 0.95},
                 "floor": {"color": "#6b5a48", "roughness": 0.8}}
    meshes = [box("wall-back", "wall", [12.4, 5, 0.2], [0, 2.5, -6.1]),
              box("wall-left", "wall", [0.2, 5, 12.2], [-6.1, 2.5, 0]),
              box("wall-right", "wall", [0.2, 5, 12.2], [6.1, 2.5, 0]),
              {"name": "floor", "material": "floor", "geometry": {"type": "plane", "size": [12.2, 12.2]},
               "rotation": [-90, 0, 0]}]
    for n, ((cx, cy, cz), a, o) in zip(names, slots):
        def at(along, up, out):
            return [cx + a[0] * along + o[0] * out, cy + up, cz + a[2] * along + o[2] * out]

        def size(along, up, out):
            return [abs(a[0]) * along + abs(o[0]) * out, up, abs(a[2]) * along + abs(o[2]) * out]
        meshes.append(box(n, "frame", size(2.4, 1.8, 0.1), at(0, 0, 0)))
        pal = known[n]["palette"]
        for j, (r, g, b) in enumerate(pal):
            c = f"{r:02x}{g:02x}{b:02x}"
            materials.setdefault("c" + c, {"color": "#" + c, "roughness": 0.7})
            meshes.append(box(f"{n}-{j + 1}", "c" + c, size(2.1, 1.5 / len(pal), 0.04),
                              at(0, 0.75 - (j + 0.5) * 1.5 / len(pal), 0.07)))
    return validate({
        "background": "#101218", "materials": materials, "meshes": meshes,
        "camera": {"fov": 60, "position": [0, 2.6, 7.5], "target": [0, 2.6, -6], "far": 100},
        "lights": [{"type": "hemisphere", "sky": "#fff6e8", "ground": "#3a2e22", "intensity": 0.8},
                   {"type": "directional", "position": [2, 8, 10], "intensity": 1.2},
                   {"type": "point", "position": [0, 4, 0], "color": "#ffd9a0", "intensity": 0.6}]})


def guard_counts(p, days):
    """T-1849: ({category: blocks}, tool calls run) for project p over the last `days` days: blocks from its ledger,
    calls (clean or failed, both got past the guard) from the global events."""
    import collections
    import time
    from sampo import core
    since = core.iso(time.time() - days * 86400)
    held = collections.Counter(str((e.get("data") or {}).get("category"))
                               for e in core.ledger_events(p, ["guard_block"], since))
    through = sum(1 for e in core.global_events(since)
                  if e.get("kind") in ("tool", "tool_fail") and e.get("project") == p.slug)
    return dict(held), through


def _write_out(args, text, what="FILE"):
    """--out's absolute path with text written there, never over args.path; None when there's no --out."""
    from sampo import core
    out = os.path.abspath(args.out) if args.out else None  # write_atomic makes dirname(path): "" for a bare name
    if out and args.path and os.path.exists(out) and os.path.samefile(out, args.path):
        raise ValueError(f"--out {args.out} is {what}")
    if out:
        core.write_atomic(out, text)  # through a temp file: a symlink at out is replaced, never written through
    return out


_READS = {"--balls": "metaballs", "--grid": "metaballs", "--seed": "metaballs", "--count": "lookbook",
          "--days": "terrain airlock", "--hud": "compile", "--to": "compile", "--tag": "compile", "--frames": "turntable",
          "--cols": "cells", "--turn": "cells", "--seconds": "soak", "--budget-ms": "soak", "--size": "shade",
          "--time": "shade", "--set": "shade"}  # T-1855: which actions read each flag; --out is everyone's


GALLERY = {"metaballs": "a seeded blob, polygonised by marching tetrahedra: the smooth-surface path",
           "terrain": "where the repo churns: a ridge per busiest path, taller is more lines changed",
           "airlock": "what the guard held, a box per category, beside the calls it let through",
           "bench": "fm code bench by run and seed: green closed, red didn't, taller was slower",
           "lookbook": "the look cards, best ranked in the middle, each palette as bands"}  # T-1873: tile order
SAMPLES = {"terrain": lambda: terrain([[1, 4, 2, 6, 3], [2, 1, 5, 2, 4]], ["plugin", "docs"]),
           "airlock": lambda: airlock({"destructive": 3, "core": 1}, 120),
           "bench": lambda: bench([{"results": [{"seed": "operator", "closed": True, "seconds": 22},
                                                {"seed": "fixer", "closed": False, "seconds": 40}]}]),
           "lookbook": lambda: lookbook(["sample"], {"sample": {"palette": [(230, 90, 60), (40, 120, 200)]}})}


def _builtin(action, a):
    """(scene, what) for a built-in scene from this repo's data; ValueError when the flags or the data won't do."""
    if action == "metaballs":
        return metaballs(a.balls, a.grid, a.seed), f"a blob of {a.balls} ball(s)"
    if action == "lookbook":
        from sampo import taste
        if not 1 <= a.count <= 12:
            raise ValueError(f"--count {a.count}: 1 to 12")
        names = [r["look"] for r in taste.rank()][:a.count]
        if not names:
            raise ValueError("no look cards yet")
        return lookbook(names, taste.looks()), f"{len(names)} look(s), best ranked first"
    if action == "bench":
        from sampo import smithbench
        runs = smithbench.history()
        return bench(runs), f"the last {min(len(runs), 12)} of {len(runs)} bench run(s)"
    days = a.days if a.days is not None else 90 if action == "terrain" else 30
    if not 1 <= days <= 3650:
        raise ValueError(f"--days {days}: 1 to 3650")
    if action == "terrain":
        rows, names = churn(os.getcwd(), days)
        return terrain(rows, names), f"{len(names)} path(s) over {days} days"
    from sampo import core
    p = core.find_project(os.getcwd())
    if not p:
        raise ValueError("not in a Sampo project: the guard's record is per project")
    held, through = guard_counts(p, days)
    if not held and not through and getattr(a, "need_data", False):  # T-1873 review: the gallery's sample instead
        raise ValueError(f"no guard record over {days} days")
    return airlock(held, through), f"{sum(held.values())} held in {len(held)} categories, {through} through over {days} days"


def gallery(size=(160, 100), cols=3):
    """T-1873: every built-in scene drawn once, small and unturned, on one sheet left to right then down; each built
    from this repo's data, or its sample (the reason said) when there's none. (width, height, RGB bytes, captions).
    The blob is drawn on a 16-cube grid: half the default's detail, an eighth of its work."""
    import types
    a, tiles, said = types.SimpleNamespace(balls=5, grid=16, seed=0, count=8, days=None, need_data=True), [], []
    for k, action in enumerate(GALLERY, 1):
        try:
            scene, what = _builtin(action, a)
        except (OSError, ValueError) as e:
            scene, what = SAMPLES[action](), f"a sample: {e}"
        except Exception as e:  # noqa: BLE001 — review: a corrupt record costs its tile, not the sheet
            scene, what = SAMPLES[action](), f"a sample: unreadable ({type(e).__name__})"
        tiles.append(_frame(scene, 0, *size))
        said.append(f"{k}. {action}: {GALLERY[action]} ({what})")
    return (*_sheet(tiles, *size, cols, (17, 17, 17)), said)


def cmd_scene(args):
    import sys
    from sampo import cli
    from sampo import core
    for flag in sorted({t.partition("=")[0] for t in cli._ARGV} & set(_READS)):
        if args.action not in _READS[flag].split():
            print(f"sampo: scene: {args.action} doesn't read {flag} (only {', '.join(_READS[flag].split())} do)",
                  file=sys.stderr)
            return 2
    if args.action == "sketchbook":  # T-1784
        if args.path:
            print(f"sampo: scene: sketchbook takes no FILE (got {args.path}); write it with --out", file=sys.stderr)
            return 2
        try:
            text = sketchbook_html(sketch_cards())
        except ValueError as e:
            raise cli.UsageError(f"a shader card's Sketch line: {e}")
        out = args.out or "sketchbook.html"
        try:
            core.write_atomic(out, text)
        except OSError as e:
            print(f"sampo: scene: can't write {out}: {e}", file=sys.stderr)
            return 1
        print(f"wrote {out}: {text.count('<figure class=')} shaders; open it in a browser with WebGL2")
        return 0
    if args.action in ("metaballs", "terrain", "airlock", "bench", "lookbook", "gallery"):  # T-1848..T-1852, T-1873
        if args.path:
            print(f"sampo: scene: {args.action} takes no FILE (got {args.path}); write it with --out", file=sys.stderr)
            return 2
        try:
            if args.action == "gallery":
                if not args.out:
                    raise ValueError("gallery writes a PNG: give --out PATH")
                from sampo import img
                w, h, rgb, said = gallery()
                img.save(args.out, w, h, rgb, "rgb")
            else:
                scene, what = _builtin(args.action, args)
                text = json.dumps(scene, indent=2) + "\n"
                out = _write_out(args, text)
        except (OSError, ValueError) as e:
            print(f"sampo: scene: {args.action}: {e}", file=sys.stderr)
            return 2
        if args.action == "gallery":
            return cli.out(args, {"out": args.out, "size": [w, h], "scenes": said}, "\n".join(
                [f"wrote {args.out}: {len(said)} scenes, {w}x{h}, left to right then down"] + said)) or 0
        if not out:
            return cli.out(args, {"scene": scene}, text.rstrip("\n")) or 0
        gs = [m["geometry"] for m in scene["meshes"] if m["name"] != "floor"]
        tris = sum(len(g.get("faces", ())) for g in gs)  # boxes count none: three.js makes their triangles
        return cli.out(args, {"out": args.out, "vertices": sum(len(g.get("vertices", ())) for g in gs),
                                "triangles": tris}, f"wrote {args.out}: {what}" + f", {tris} triangles" * bool(tris)) or 0
    if not args.path:
        print(f"sampo: scene: {args.action} needs FILE", file=sys.stderr)
        return 2
    if args.action == "shade":  # T-1846: a fragment shader run per pixel on the CPU, no browser or GPU
        import re
        from sampo import img
        from sampo import shade
        try:
            m = re.fullmatch(r"(\d+)x(\d+)", args.size)
            w, h = (int(m[1]), int(m[2])) if m else (0, 0)
            if not (0 < w <= 512 and 0 < h <= 512):
                raise ValueError(f"--size WxH: 1 to 512 each (got {args.size})")
            sets = {}
            for s in args.set or ():
                name, eq, vals = s.partition("=")
                if not eq or not name:
                    raise ValueError(f"--set NAME=V[,V…] (got {s})")
                sets[name.strip()] = [float(v) for v in vals.split(",")]
            if not args.out:
                raise ValueError("shade writes a PNG: give --out PATH")
            if os.path.exists(args.out) and os.path.samefile(args.out, args.path):
                raise ValueError(f"--out {args.out} is FILE")
            src, first = shade.source(args.path)
            rgb, notes = shade.render(src, w, h, sets, args.time, first)
            img.save(args.out, w, h, rgb, "rgb")
        except (OSError, ValueError) as e:
            print(f"sampo: scene: {e}", file=sys.stderr)
            return 2
        said = f"wrote {args.out}: {w}x{h} of {args.path}"
        said += f"; preview defaults for {', '.join(notes['defaulted'])}" * bool(notes["defaulted"])
        said += f"; {notes['undefined']} pixel(s) undefined, drawn black" * bool(notes["undefined"])
        return cli.out(args, {"out": args.out, "size": [w, h], **notes}, said) or 0
    if args.action == "shader":  # T-1530: GLSL checks with no browser; glslangValidator when it's installed
        import shutil
        validator = shutil.which("glslangValidator")
        paths = shader_paths(args.path)
        try:
            found = [p for path in paths for p in shader_problems(path, validator)]
        except (OSError, UnicodeDecodeError) as e:
            print(f"sampo: scene: {e}", file=sys.stderr)
            return 2
        cli.out(args, {"checked": paths, "problems": found, "compiled": bool(validator)},
                  "\n".join([f"✗ {x}" for x in found] + [
                      f"{'✗' if found else '✓'} {len(paths)} shader file(s), {len(found)} problem(s)"
                      + ("" if validator else " (glslangValidator not found: compiling skipped, the rest checked)")]))
        return 1 if found else 0
    if args.action == "check" and args.path.lower().endswith((".gltf", ".glb")):  # T-1530: a glTF asset, offline
        try:
            found = gltf_problems(args.path)
        except OSError as e:
            print(f"sampo: scene: {e}", file=sys.stderr)
            return 2
        cli.out(args, {"problems": found}, "\n".join([f"✗ {x}" for x in found] + [
            f"{'✗' if found else '✓'} {args.path}: {len(found)} problem(s)"]))
        return 1 if found else 0
    if args.action == "soak":  # T-1530: a real browser when there is one; skipped with the reason when not
        if not 1 <= args.seconds <= 600:
            print(f"sampo: scene: soak: --seconds {args.seconds}: 1 to 600", file=sys.stderr)
            return 2
        try:
            got = soak(args.path, find_three(os.path.dirname(os.path.abspath(args.path)), os.getcwd()),
                       args.seconds, args.budget_ms)
        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            print(f"sampo: scene: soak: {e}", file=sys.stderr)
            return 2
        if "skipped" in got:
            cli.out(args, got, f"skipped: {got['skipped']}")
            return 0
        cli.out(args, got, "\n".join([f"✗ {x}" for x in got["problems"]] + [
            f"{'✗' if got['problems'] else '✓'} soak {args.seconds} s: {got['frames']} frames, p95 "
            f"{got['p95_ms'] if got['p95_ms'] is not None else '?'} ms, geometries "
            f"{'→'.join(map(str, sorted(set(got['geometries']), key=got['geometries'].index)))}, after dispose "
            f"{got['after']}"]))
        return 1 if got["problems"] else 0
    if args.action == "import":  # T-1847: three.js JSON in, a checked scene.json out; what didn't carry, named
        try:
            scene, left = load(args.path, lambda text: from_three(_json(text)))
            text = json.dumps(scene, indent=2) + "\n"
            out = _write_out(args, text, "the file it imports")
        except (OSError, ValueError) as e:
            print(f"sampo: scene: import: {e}", file=sys.stderr)
            return 2
        note = f"left out: {'; '.join(left)}" if left else "nothing left out"
        if not out:  # stdout is the scene, so the note goes to stderr
            print(note, file=sys.stderr)
            return cli.out(args, {"scene": scene, "left_out": left}, text.rstrip("\n")) or 0
        return cli.out(args, {"out": args.out, "meshes": len(scene["meshes"]), "left_out": left},
                         f"wrote {args.out}: {len(scene['meshes'])} mesh(es); {note}") or 0
    if args.action in ("check", "turntable", "cells"):  # T-1530, T-1635
        try:
            scene = load(args.path)
            if args.action == "cells":
                lines = cells(scene, args.cols, args.turn)
                return cli.out(args, {"cols": args.cols, "rows": len(lines), "lines": lines}, "\n".join(lines)) or 0
            if args.action == "check":
                tris = sum(len(faces(m["geometry"])) for m in scene["meshes"])
                return cli.out(args, {"meshes": len(scene["meshes"]), "triangles": tris,
                                        "lights": len(scene["lights"]), "animations": len(scene["animations"])},
                                 f"✓ {args.path}: {len(scene['meshes'])} mesh(es), {tris} triangles, "
                                 f"{len(scene['lights'])} light(s), {len(scene['animations'])} animation(s)") or 0
            if not args.out:
                raise ValueError("turntable writes a PNG: give --out PATH")
            if not 1 <= args.frames <= 36:
                raise ValueError(f"--frames {args.frames}: 1 to 36")
            w, h, rgb = turntable(scene, args.frames)
            from sampo import img
            img.save(args.out, w, h, rgb, "rgb")
        except (OSError, ValueError) as e:
            print(f"sampo: scene: {e}", file=sys.stderr)
            return 2
        return cli.out(args, {"out": args.out, "frames": args.frames, "size": [w, h]},
                         f"wrote {args.out}: {args.frames} turns of {args.path}, {w}x{h}") or 0
    try:
        tag = getattr(args, "tag", None)
        if tag and args.to != "component":
            raise ValueError("--tag names the element: use it with --to component")
        scene = load(args.path)
        hud = getattr(args, "hud", False)
        text = compile_component(scene, tag or "fm-scene", hud) if args.to == "component" else \
            compile_three(scene, hud=hud)
        out = _write_out(args, text, "the scene it compiles")
    except (OSError, ValueError) as e:
        print(f"sampo: scene: {e}", file=sys.stderr)
        return 2
    if out:
        cli.out(args, {"out": args.out, "bytes": len(text.encode())}, f"wrote {args.out}")
    else:
        cli.out(args, {"module": text}, text.rstrip("\n"))
    return 0
