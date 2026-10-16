// ---- Tone curve ----------------------------------------------------------
// Point curve on a canvas (RGB master + R, G, B) and four parametric sliders.
// Points are 0-255 like the XMP. The server owns the rendering; this only
// edits values.tone_curve, and the interpolation drawn here mirrors tonecurve.py.
const CURVE_COLOURS = { rgb: "#e8e8ea", red: "#e0685f", green: "#6fcf7f", blue: "#6a9cf0" };
let curveChannel = "rgb";
let curveDrag = -1;

function defaultCurve() {
  return {
    parametric: { highlights: 0, lights: 0, darks: 0, shadows: 0, split1: 25, split2: 50, split3: 75 },
    points: { rgb: [[0,0],[255,255]], red: [[0,0],[255,255]], green: [[0,0],[255,255]], blue: [[0,0],[255,255]] },
  };
}
function curve() {
  if (!values.tone_curve) values.tone_curve = defaultCurve();
  return values.tone_curve;
}
// Monotone cubic (Fritsch-Carlson), same as the server.
function curveValue(pts, x) {
  const n = pts.length, X = pts.map(p => p[0]), Y = pts.map(p => p[1]);
  const h = [], d = [];
  for (let i = 0; i < n - 1; i++) { h.push(X[i+1] - X[i]); d.push((Y[i+1] - Y[i]) / h[i]); }
  const m = new Array(n).fill(0);
  if (n === 2) { m[0] = m[1] = d[0]; }
  else {
    m[0] = d[0]; m[n-1] = d[n-2];
    for (let i = 1; i < n - 1; i++) {
      if (d[i-1] * d[i] <= 0) m[i] = 0;
      else {
        const w1 = 2*h[i] + h[i-1], w2 = h[i] + 2*h[i-1];
        m[i] = (w1 + w2) / (w1 / d[i-1] + w2 / d[i]);
      }
    }
    if (Math.abs(m[0]) > 3*Math.abs(d[0])) m[0] = 3*d[0];
    if (Math.abs(m[n-1]) > 3*Math.abs(d[n-2])) m[n-1] = 3*d[n-2];
  }
  let i = 0;
  while (i < n - 2 && x >= X[i+1]) i++;
  const t = (x - X[i]) / h[i], t2 = t*t, t3 = t2*t;
  const v = (2*t3 - 3*t2 + 1)*Y[i] + (t3 - 2*t2 + t)*h[i]*m[i] + (-2*t3 + 3*t2)*Y[i+1] + (t3 - t2)*h[i]*m[i+1];
  return Math.min(255, Math.max(0, v));
}

(function buildCurvePanel() {
  const panel = document.createElement("div");
  panel.id = "curve-panel";
  panel.innerHTML = `
    <h3>Tone curve</h3>
    <div class="curve-row" id="curve-channels" style="margin:0 0 7px">
      <button data-ch="rgb" class="active">RGB</button>
      <button data-ch="red">R</button><button data-ch="green">G</button><button data-ch="blue">B</button>
      <button id="curve-reset" style="margin-left:auto" title="Reset the whole curve">Reset</button>
    </div>
    <canvas id="curve-canvas" width="480" height="480"></canvas>
    <div class="curve-row">
      <select id="curve-preset"><option value="">Curve preset…</option>
        ${Object.keys(CURVE_PRESETS).map(n => `<option>${n}</option>`).join("")}</select>
    </div>
    <div id="curve-para"></div>`;
  $("sliders").appendChild(panel);

  for (const c of CURVE_PARAMETRIC) {
    const wrap = document.createElement("div");
    wrap.className = "ctrl";
    wrap.innerHTML = `
      <div class="ctrl-head"><label for="cp-${c.key}">${c.label}</label>
        <span class="val" id="cpv-${c.key}" title="Click to reset">0</span></div>
      <input type="range" id="cp-${c.key}" min="-100" max="100" step="1" value="0">`;
    $("curve-para").appendChild(wrap);
    const slider = wrap.querySelector("input");
    slider.addEventListener("input", () => {
      curve().parametric[c.key] = parseFloat(slider.value);
      paintCurve(); schedulePreview();
    });
    $(`cpv-${c.key}`).addEventListener("click", () => {
      curve().parametric[c.key] = 0; paintCurve(); schedulePreview();
    });
  }

  panel.querySelectorAll("#curve-channels [data-ch]").forEach(b => b.addEventListener("click", () => {
    curveChannel = b.dataset.ch; paintCurve();
  }));
  $("curve-reset").addEventListener("click", () => {
    values.tone_curve = defaultCurve(); paintCurve(); schedulePreview();
    commit("Reset tone curve");
  });
  $("curve-preset").addEventListener("change", (e) => {
    const pts = CURVE_PRESETS[e.target.value];
    if (pts) {
      curve().points.rgb = pts.map(p => [...p]); paintCurve(); schedulePreview();
      commit(`Curve preset: ${e.target.value}`);
    }
    e.target.value = "";
  });

  const cv = $("curve-canvas");
  const toXY = (ev) => {
    const r = cv.getBoundingClientRect();
    return [Math.min(255, Math.max(0, (ev.clientX - r.left) / r.width * 255)),
            Math.min(255, Math.max(0, 255 - (ev.clientY - r.top) / r.height * 255))];
  };
  const hit = (pts, x, y) => {
    const r = cv.getBoundingClientRect(), k = 255 / r.width;
    return pts.findIndex(p => Math.hypot(p[0] - x, p[1] - y) < 9 * k);
  };
  cv.addEventListener("pointerdown", (ev) => {
    if (!current) return;
    const pts = curve().points[curveChannel], [x, y] = toXY(ev);
    let i = hit(pts, x, y);
    if (i < 0) {
      pts.push([Math.round(x), Math.round(y)]);
      pts.sort((a, b) => a[0] - b[0]);
      i = pts.findIndex(p => p[0] === Math.round(x) && p[1] === Math.round(y));
    }
    curveDrag = i;
    cv.setPointerCapture(ev.pointerId);
    clampPoint(pts, i, x, y);
  });
  cv.addEventListener("pointermove", (ev) => {
    if (curveDrag < 0) return;
    const [x, y] = toXY(ev);
    clampPoint(curve().points[curveChannel], curveDrag, x, y);
  });
  const end = () => { if (curveDrag >= 0) commit(`Tone curve (${curveChannel.toUpperCase()})`); curveDrag = -1; };
  cv.addEventListener("pointerup", end);
  cv.addEventListener("pointercancel", end);
  // Double-click removes an interior point.
  cv.addEventListener("dblclick", (ev) => {
    const pts = curve().points[curveChannel], [x, y] = toXY(ev), i = hit(pts, x, y);
    if (i > 0 && i < pts.length - 1) { pts.splice(i, 1); paintCurve(); schedulePreview(); commit("Remove curve point"); }
  });
  // Keep points ordered: an interior point stays between its neighbours in
  // x and y, the end points keep their x and only move vertically.
  function clampPoint(pts, i, x, y) {
    const last = pts.length - 1;
    const lo = i > 0 ? pts[i-1] : [0, 0], hi = i < last ? pts[i+1] : [255, 255];
    if (i === 0) x = 0; else if (i === last) x = 255;
    else x = Math.min(hi[0] - 1, Math.max(lo[0] + 1, x));
    y = Math.min(i < last ? hi[1] : 255, Math.max(i > 0 ? lo[1] : 0, y));
    pts[i] = [Math.round(x), Math.round(y)];
    paintCurve(); schedulePreview();
  }
})();

function paintCurve() {
  const cv = $("curve-canvas");
  if (!cv) return;
  const g = cv.getContext("2d"), S = cv.width, tc = curve();
  g.clearRect(0, 0, S, S);
  g.strokeStyle = "rgba(128,128,128,.22)"; g.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    g.beginPath(); g.moveTo(i*S/4, 0); g.lineTo(i*S/4, S); g.moveTo(0, i*S/4); g.lineTo(S, i*S/4); g.stroke();
  }
  g.strokeStyle = "rgba(128,128,128,.45)";
  g.beginPath(); g.moveTo(0, S); g.lineTo(S, 0); g.stroke();
  const draw = (ch, active) => {
    const pts = tc.points[ch];
    g.strokeStyle = CURVE_COLOURS[ch]; g.globalAlpha = active ? 1 : 0.35; g.lineWidth = active ? 2.5 : 1.5;
    g.beginPath();
    for (let px = 0; px <= S; px += 2) {
      const y = S - curveValue(pts, px / S * 255) / 255 * S;
      px ? g.lineTo(px, y) : g.moveTo(px, y);
    }
    g.stroke(); g.globalAlpha = 1;
    if (active) {
      g.fillStyle = CURVE_COLOURS[ch];
      for (const p of pts) { g.beginPath(); g.arc(p[0]/255*S, S - p[1]/255*S, 6, 0, 7); g.fill(); }
    }
  };
  for (const ch of ["rgb", "red", "green", "blue"]) if (ch !== curveChannel) draw(ch, false);
  draw(curveChannel, true);
  document.querySelectorAll("#curve-channels [data-ch]").forEach(b =>
    b.classList.toggle("active", b.dataset.ch === curveChannel));
  for (const c of CURVE_PARAMETRIC) {
    const v = tc.parametric[c.key] || 0;
    $(`cp-${c.key}`).value = v;
    $(`cpv-${c.key}`).textContent = (v > 0 ? "+" : "") + Math.round(v);
    $(`cp-${c.key}`).closest(".ctrl").classList.toggle("changed", v !== 0);
  }
}

// ---- Copy / paste settings -----------------------------------------------
// Temperature is stored as a ratio of the source photo's own white balance,
// so pasting onto a frame shot in different light keeps the intent ("a bit
// warmer than neutral") rather than forcing an absolute Kelvin value.
$("copy").addEventListener("click", () => {
  if (!current) return;
  const baseline = baselineTemp || values.color_temp || 5500;
  clipboard = {
    values: JSON.parse(JSON.stringify(values)),   // deep: the curve is nested
    tempRatio: (values.color_temp || baseline) / baseline,
    from: current.split("/").pop(),
  };
  $("paste").disabled = false;
  updateSelectionUI();
  setStatus(`Copied settings from ${clipboard.from}`);
});

$("paste").addEventListener("click", () => {
  if (!clipboard || !current) return;
  const baseline = baselineTemp || 5500;
  // Spot positions belong to the photo they were placed on, so they stay put.
  values = { ...JSON.parse(JSON.stringify(clipboard.values)), color_temp: Math.round(baseline * clipboard.tempRatio),
             spots: values.spots || [] };
  paintAll();
  schedulePreview();
  commit("Paste settings");
  setStatus(`Pasted settings from ${clipboard.from} — not saved yet`);
});
