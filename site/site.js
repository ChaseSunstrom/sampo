(function () {
  var root = document.documentElement;
  try { var t = localStorage.getItem("fm-theme"); if (t) root.setAttribute("data-theme", t); } catch (e) {}
  var toggle = document.getElementById("theme");  /* T-3996: the one header's switch, on every page */
  if (toggle) toggle.addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("fm-theme", next); } catch (e) {}
  });

  document.querySelectorAll(".menu nav a").forEach(function (a) { a.addEventListener("click", function () { a.closest("details").open = false; }); });
  document.addEventListener("click", function (e) { var m = document.querySelector(".menu[open]"); if (m && !m.contains(e.target)) m.open = false; });

  /* T-3996: the side list scrolls on its own (down on a wide screen, sideways on a phone): open it at this page's entry */
  var side = document.querySelector(".side"), cur = side && side.querySelector("[aria-current]");
  if (cur) {  // rects, not offsets: the sticky list is its links' offsetParent on a wide screen, the body on a phone
    var at = cur.getBoundingClientRect(), box = side.getBoundingClientRect();
    if (side.scrollHeight > side.clientHeight) side.scrollTop += at.top - box.top - (side.clientHeight - at.height) / 2;
    if (side.scrollWidth > side.clientWidth) side.scrollLeft += at.left - box.left - (side.clientWidth - at.width) / 2;
  }

  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var text = document.getElementById(b.getAttribute("data-copy")).textContent;
      var done = function () { b.textContent = "Copied"; b.setAttribute("data-done", ""); setTimeout(function () { b.textContent = "Copy"; b.removeAttribute("data-done"); }, 1800); };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {}); else done();
    });
  });

  var still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var stations = document.body.classList.contains("forge") ? [] : document.querySelectorAll("#stations li");
  if (still || !("IntersectionObserver" in window)) {
    stations.forEach(function (li) { li.classList.add("on"); });
    return;
  }
  // the one orchestrated moment: the stations tick through in order when the loop comes into view
  var ticked = false;
  var io = new IntersectionObserver(function (es) {
    es.forEach(function (e) {
      if (e.target.id === "stations" && e.isIntersecting && !ticked) {
        ticked = true;
        stations.forEach(function (li, i) { setTimeout(function () { li.classList.add("on"); }, 220 * i + 150); });
      }
    });
  }, { threshold: 0.35 });
  // T-2040: the hero's session plays once, a line at a time, the pane's stations following, from its first view on
  var sess = document.getElementById("session");
  if (sess) {
    var lines = [].slice.call(sess.querySelectorAll(".ln")), marks = sess.querySelectorAll(".stage li"), at = 0;
    var mark = function (s) { marks.forEach(function (li, k) { li.className = k < s ? "done" : k === s ? "now" : ""; }); };
    var step = function () {
      var ln = lines[at];
      setTimeout(function () {
        if (at) lines[at - 1].classList.remove("cur");
        ln.classList.add("on"); mark(+ln.getAttribute("data-s"));
        if (++at < lines.length) { ln.classList.add("cur"); step(); }
      }, +ln.getAttribute("data-w") || 700);
    };
    sess.querySelectorAll(".tx").forEach(function (t) { t.style.setProperty("--n", t.textContent.length); });
    var sio = new IntersectionObserver(function (es) {
      if (es[es.length - 1].isIntersecting) { sio.disconnect(); step(); }
    }, { threshold: 0.1 });
    sess.classList.add("live"); mark(-1); sio.observe(sess);
  }
  // T-1615: each chart draws itself once, the first time it comes into view (CSS does the motion; seen still, it's whole)
  var charts = new IntersectionObserver(function (es) {
    es.forEach(function (e) {
      if (e.isIntersecting) { e.target.classList.add("drawn"); charts.unobserve(e.target); }
    });
  }, { threshold: 0.3 });
  document.querySelectorAll(".viz.grow-x, .viz.grow-y, .viz.draw").forEach(function (v) {
    v.querySelectorAll(".m").forEach(function (m, i) { m.style.transitionDelay = Math.min(i, 24) * 40 + "ms"; });
    v.classList.add("armed");
    charts.observe(v);
  });
  var st = document.getElementById("stations");
  if (st) io.observe(st);
  // T-2063: the proof figures count up once as the hero rises, ending on the exact text the build wrote
  document.querySelectorAll(".proof dd").forEach(function (dd) {
    var end = dd.textContent, n = +end.replace(/,/g, ""), t0 = performance.now() + 300;
    if (!/^[\d,]+$/.test(end)) return;
    var tick = function (now) {
      var k = Math.min(Math.max((now - t0) / 1200, 0), 1);
      dd.textContent = k < 1 ? Math.round(n * (1 - Math.pow(1 - k, 3))).toLocaleString("en-US") : end;
      if (k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    setTimeout(function () { t0 = -1e9; dd.textContent = end; }, 1700);  // frames can stall (a hidden tab, a renderer); the text can't stay wrong
  });
  // T-2063: a feature card's light follows the pointer (style.css draws it on hover)
  document.addEventListener("pointermove", function (e) {
    var c = e.target.closest && e.target.closest(".fcard");
    if (!c) return;
    var r = c.getBoundingClientRect();
    c.style.setProperty("--mx", e.clientX - r.left + "px");
    c.style.setProperty("--my", e.clientY - r.top + "px");
  }, { passive: true });
  // T-2759: the forge's fire, heat rising in the hearth under the mill. Motion only (the still return above) and bounded:
  // it slows to rest by --autoplay-limit, burns again only under the pointer, and stops offscreen and in a hidden tab.
  // Any failure takes the canvas away, leaving the CSS glow the hearth already has.
  // T-2767: the same fire lights the tower pages' skies, drifting its own way per data-sky: the aurora sideways, the
  // stars barely, the river downstream; the hearth's heat rises
  var drift = { aurora: [.3, 0], stars: [0, .05], river: [-.5, 0] };
  document.querySelectorAll(".hearth, .sky").forEach(function (box) {
    try { fire(box); } catch (e) { var dead = box.querySelector("canvas"); if (dead) dead.remove(); }
  });
  function fire(box) {
    var cv = document.createElement("canvas"), gl = cv.getContext("webgl");
    if (!gl) return;
    var prog = gl.createProgram();
    var shader = function (type, src) { var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); gl.attachShader(prog, s); };
    shader(gl.VERTEX_SHADER, "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}");
    shader(gl.FRAGMENT_SHADER, "precision mediump float;uniform vec2 r,d;uniform float t;uniform vec3 a,b,c;" +
      "float h(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}" +
      "float n(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);" +
      "return mix(mix(h(i),h(i+vec2(1.,0.)),f.x),mix(h(i+vec2(0.,1.)),h(i+vec2(1.,1.)),f.x),f.y);}" +
      "float fbm(vec2 p){float s=0.,w=.5;for(int i=0;i<5;i++){s+=w*n(p);p*=2.03;w*=.5;}return s;}" +
      "void main(){vec2 uv=gl_FragCoord.xy/r;float f=fbm(vec2(uv.x*r.x/r.y,uv.y)*3.+d*t);" +
      "float heat=(1.-smoothstep(0.,.9,uv.y*1.25+(f-.5)*.6-.05))*(1.-smoothstep(.3,1.1,abs(uv.x-.5)*2.));" +
      "vec3 col=mix(a,b,smoothstep(.1,.6,heat));gl_FragColor=vec4(mix(col,c,smoothstep(.55,1.,heat)),1.);}");
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("the forge shader didn't link");
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    var at = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(at); gl.vertexAttribPointer(at, 2, gl.FLOAT, false, 0, 0);
    var u = {}; ["r", "d", "t", "a", "b", "c"].forEach(function (k) { u[k] = gl.getUniformLocation(prog, k); });
    gl.uniform2fv(u.d, drift[box.getAttribute("data-sky")] || [0, -.7]);
    // the theme's own stone, ember and glow, read through a 2D context so any CSS colour comes back as #rrggbb
    var probe = document.createElement("canvas").getContext("2d"), css = getComputedStyle(box);
    var rgb = function (name) {
      probe.fillStyle = "#000"; probe.fillStyle = css.getPropertyValue(name).trim();
      return (probe.fillStyle.match(/[\da-f]{2}/gi) || ["0", "0", "0"]).slice(0, 3).map(function (x) { return parseInt(x, 16) / 255; });
    };
    var tint = function () { gl.uniform3fv(u.a, rgb("--stone")); gl.uniform3fv(u.b, rgb("--ember")); gl.uniform3fv(u.c, rgb("--glow")); };
    var limit = parseFloat(css.getPropertyValue("--autoplay-limit")) || 5000;
    var t = 0, ran = 0, last = 0, raf = 0, hover = false, seen = false;
    var paint = function () {
      var w = Math.ceil(box.clientWidth / 2), h = Math.ceil(box.clientHeight / 2);  // half resolution: fire is soft
      if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; gl.viewport(0, 0, w, h); }
      gl.uniform2f(u.r, w, h); gl.uniform1f(u.t, t); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    };
    var frame = function (now) {
      raf = 0;
      if (now - last >= 33) {  // about 30 frames a second is plenty for embers
        var dt = Math.min(now - last, 100), speed = hover ? 1 : Math.max(0, 1 - ran / limit);
        last = now; ran += dt; t += dt / 1000 * speed;
        paint();
        if (!speed) return;  // at rest: the last frame stays on the canvas
      }
      if (seen && !document.hidden) raf = requestAnimationFrame(frame);
    };
    var go = function () { if (!raf && seen && !document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); } };
    tint(); paint();
    box.appendChild(cv);
    if (cv.animate) cv.animate([{ opacity: 0 }, { opacity: 1 }], parseFloat(css.getPropertyValue("--dur-stage")) || 500);
    cv.addEventListener("webglcontextlost", function () { cv.remove(); seen = false; });
    new IntersectionObserver(function (es) { seen = es[es.length - 1].isIntersecting; go(); }).observe(box);
    document.addEventListener("visibilitychange", go);
    box.addEventListener("pointerenter", function () { hover = true; go(); });
    box.addEventListener("pointerleave", function () { hover = false; });
    var retint = function () { tint(); paint(); };  // a theme switch repaints the resting frame in the new colours
    if (toggle) toggle.addEventListener("click", retint);
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", retint);
  }
})();
