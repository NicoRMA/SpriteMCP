(() => {
  "use strict";

  const MAX_HISTORY = 60;

  const EYE_OPEN =
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 5c-5.5 0-9.5 4.5-10.5 6.2a1.2 1.2 0 0 0 0 1.2C2.5 14.5 6.5 19 12 19s9.5-4.5 10.5-6.2a1.2 1.2 0 0 0 0-1.2C21.5 9.5 17.5 5 12 5zm0 12c-3.9 0-7.1-3.2-8.4-5C4.9 10.2 8.1 7 12 7s7.1 3.2 8.4 5c-1.3 1.8-4.5 5-8.4 5zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/></svg>';
  const EYE_CLOSED =
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3.28 2.22 2.22 3.28l3.1 3.1C3.5 7.7 1.9 9.6 1.5 10.2a1.2 1.2 0 0 0 0 1.2C2.5 13.1 6.5 17.5 12 17.5c1.6 0 3.1-.4 4.4-1l3.3 3.3 1.06-1.06L3.28 2.22zM12 15.5c-3.5 0-6.4-2.7-7.7-4.3.6-.8 1.8-2.1 3.4-3.1l1.6 1.6A3 3 0 0 0 12 14.5c.4 0 .8-.1 1.1-.2l1.5 1.5c-.8.4-1.7.7-2.6.7zm8.5-4.1c-.4.6-1.1 1.5-2.1 2.4l-1.2-1.2c.8-.7 1.4-1.4 1.8-1.9C17.7 9.1 15 7 12 7c-.5 0-1 .1-1.5.2L8.9 5.6C9.9 5.2 10.9 5 12 5c5.5 0 9.5 4.4 10.5 6.1a1.2 1.2 0 0 1 0 1.3z"/></svg>';

  const state = {
    info: null,
    layer: null,
    tool: "pencil",
    color: [201, 162, 39, 255],
    colorTransparent: false,
    brushSize: 1,
    zoom: 6,
    showUnderlay: true,
    dirty: false,
    buffers: {},
    underlays: {},
    visible: {},
    painting: false,
    strokeActive: false,
    undoStack: [],
    redoStack: [],
  };

  const el = {
    charName: document.getElementById("char-name"),
    layerList: document.getElementById("layer-list"),
    view: document.getElementById("view"),
    color: document.getElementById("color"),
    colorSwatch: document.getElementById("color-swatch"),
    colorTransparent: document.getElementById("color-transparent"),
    brushSize: document.getElementById("brush-size"),
    zoom: document.getElementById("zoom"),
    zoomLabel: document.getElementById("zoom-label"),
    underlay: document.getElementById("show-underlay"),
    undo: document.getElementById("btn-undo"),
    redo: document.getElementById("btn-redo"),
    apply: document.getElementById("btn-apply"),
    status: document.getElementById("status"),
  };

  const viewCtx = el.view.getContext("2d", { willReadFrequently: true });

  function hexToRgb(hex) {
    const h = hex.replace("#", "");
    return [
      parseInt(h.slice(0, 2), 16),
      parseInt(h.slice(2, 4), 16),
      parseInt(h.slice(4, 6), 16),
    ];
  }

  function rgbaToHex(r, g, b) {
    return (
      "#" +
      [r, g, b]
        .map((v) => v.toString(16).padStart(2, "0"))
        .join("")
    );
  }

  function paintColor() {
    if (state.colorTransparent || state.color[3] === 0) {
      return [0, 0, 0, 0];
    }
    return [state.color[0], state.color[1], state.color[2], 255];
  }

  function syncColorUi() {
    const transparent = state.colorTransparent || state.color[3] === 0;
    el.colorTransparent.checked = transparent;
    el.colorSwatch.classList.toggle("is-transparent", transparent);
    if (transparent) {
      el.colorSwatch.style.setProperty("--swatch", "transparent");
    } else {
      const hex = rgbaToHex(state.color[0], state.color[1], state.color[2]);
      el.color.value = hex;
      el.colorSwatch.style.setProperty("--swatch", hex);
    }
  }

  function syncHistoryUi() {
    el.undo.disabled = state.undoStack.length === 0;
    el.redo.disabled = state.redoStack.length === 0;
  }

  function setStatus(msg, kind) {
    el.status.textContent = msg || "";
    el.status.className = "status" + (kind ? " " + kind : "");
  }

  function cloneImageData(imageData) {
    return new ImageData(
      new Uint8ClampedArray(imageData.data),
      imageData.width,
      imageData.height
    );
  }

  function snapshotLayer(layer) {
    const buf = state.buffers[layer];
    if (!buf) return null;
    return { layer, data: cloneImageData(buf) };
  }

  function restoreSnapshot(snap) {
    if (!snap) return;
    state.buffers[snap.layer] = cloneImageData(snap.data);
    if (state.layer !== snap.layer) {
      selectLayer(snap.layer);
    } else {
      redraw();
    }
  }

  function pushHistoryBeforeEdit() {
    const snap = snapshotLayer(state.layer);
    if (!snap) return;
    state.undoStack.push(snap);
    if (state.undoStack.length > MAX_HISTORY) {
      state.undoStack.shift();
    }
    state.redoStack.length = 0;
    syncHistoryUi();
  }

  function undo() {
    if (!state.undoStack.length) return;
    const current = snapshotLayer(state.layer);
    const prev = state.undoStack.pop();
    // Current may be a different layer than prev — snapshot the edited layer.
    const beforeRestore = snapshotLayer(prev.layer);
    if (beforeRestore) state.redoStack.push(beforeRestore);
    else if (current) state.redoStack.push(current);
    restoreSnapshot(prev);
    state.dirty = true;
    syncHistoryUi();
  }

  function redo() {
    if (!state.redoStack.length) return;
    const next = state.redoStack.pop();
    const beforeRestore = snapshotLayer(next.layer);
    if (beforeRestore) state.undoStack.push(beforeRestore);
    restoreSnapshot(next);
    state.dirty = true;
    syncHistoryUi();
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("Failed to load " + url));
      img.src = url + "?t=" + Date.now();
    });
  }

  function imageToImageData(img, w, h) {
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0);
    return ctx.getImageData(0, 0, w, h);
  }

  function imageDataToDataUrl(imageData) {
    const c = document.createElement("canvas");
    c.width = imageData.width;
    c.height = imageData.height;
    c.getContext("2d").putImageData(imageData, 0, 0);
    return c.toDataURL("image/png");
  }

  function setPixel(imageData, x, y, rgba) {
    const { width, height, data } = imageData;
    if (x < 0 || y < 0 || x >= width || y >= height) return;
    const i = (y * width + x) * 4;
    data[i] = rgba[0];
    data[i + 1] = rgba[1];
    data[i + 2] = rgba[2];
    data[i + 3] = rgba[3];
  }

  function getPixel(imageData, x, y) {
    const { width, height, data } = imageData;
    if (x < 0 || y < 0 || x >= width || y >= height) {
      return [0, 0, 0, 0];
    }
    const i = (y * width + x) * 4;
    return [data[i], data[i + 1], data[i + 2], data[i + 3]];
  }

  function sameRgba(a, b) {
    return a[0] === b[0] && a[1] === b[1] && a[2] === b[2] && a[3] === b[3];
  }

  function stampBrush(imageData, cx, cy, rgba) {
    const size = Math.max(1, Math.min(16, state.brushSize | 0));
    const half = Math.floor((size - 1) / 2);
    for (let dy = 0; dy < size; dy++) {
      for (let dx = 0; dx < size; dx++) {
        setPixel(imageData, cx - half + dx, cy - half + dy, rgba);
      }
    }
  }

  function floodFill(imageData, sx, sy, fill) {
    const target = getPixel(imageData, sx, sy);
    if (sameRgba(target, fill)) return false;
    const { width, height, data } = imageData;
    const stack = [[sx, sy]];
    const seen = new Uint8Array(width * height);
    let painted = false;
    while (stack.length) {
      const [x, y] = stack.pop();
      if (x < 0 || y < 0 || x >= width || y >= height) continue;
      const idx = y * width + x;
      if (seen[idx]) continue;
      seen[idx] = 1;
      const i = idx * 4;
      const cur = [data[i], data[i + 1], data[i + 2], data[i + 3]];
      if (!sameRgba(cur, target)) continue;
      data[i] = fill[0];
      data[i + 1] = fill[1];
      data[i + 2] = fill[2];
      data[i + 3] = fill[3];
      painted = true;
      stack.push([x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]);
    }
    return painted;
  }

  function sampleComposite(x, y) {
    const layers = state.info.layers;
    for (let i = layers.length - 1; i >= 0; i--) {
      const name = layers[i];
      if (!state.visible[name]) continue;
      const px = getPixel(state.buffers[name], x, y);
      if (px[3] > 0) return px;
    }
    if (state.showUnderlay) {
      for (let i = layers.length - 1; i >= 0; i--) {
        const name = layers[i];
        if (!state.visible[name] || !state.underlays[name]) continue;
        const px = getPixel(state.underlays[name], x, y);
        if (px[3] > 0) return [px[0], px[1], px[2], 255];
      }
    }
    return [0, 0, 0, 0];
  }

  function canvasCoords(evt) {
    const rect = el.view.getBoundingClientRect();
    const w = state.info.canvas.w;
    const h = state.info.canvas.h;
    const x = Math.floor(((evt.clientX - rect.left) / rect.width) * w);
    const y = Math.floor(((evt.clientY - rect.top) / rect.height) * h);
    return { x, y };
  }

  function blitImageData(imageData, w, h, z) {
    const tmp = document.createElement("canvas");
    tmp.width = w;
    tmp.height = h;
    tmp.getContext("2d").putImageData(imageData, 0, 0);
    viewCtx.drawImage(tmp, 0, 0, w * z, h * z);
  }

  function redraw() {
    const { w, h } = state.info.canvas;
    const z = state.zoom;
    el.view.width = w * z;
    el.view.height = h * z;
    viewCtx.imageSmoothingEnabled = false;
    viewCtx.clearRect(0, 0, el.view.width, el.view.height);

    for (const layer of state.info.layers) {
      if (!state.visible[layer]) continue;
      if (state.showUnderlay && state.underlays[layer]) {
        blitImageData(state.underlays[layer], w, h, z);
      }
    }
    for (const layer of state.info.layers) {
      if (!state.visible[layer]) continue;
      if (state.buffers[layer]) {
        blitImageData(state.buffers[layer], w, h, z);
      }
    }
  }

  function updateVisButton(btn, layer) {
    const visible = !!state.visible[layer];
    btn.classList.toggle("is-hidden", !visible);
    btn.setAttribute("aria-pressed", visible ? "true" : "false");
    btn.title = visible ? "Hide layer" : "Show layer";
    btn.innerHTML = visible ? EYE_OPEN : EYE_CLOSED;
  }

  function selectLayer(layer) {
    state.layer = layer;
    for (const btn of el.layerList.querySelectorAll(".layer-btn")) {
      btn.classList.toggle("active", btn.dataset.layer === layer);
    }
    redraw();
  }

  function paintAt(x, y) {
    const buf = state.buffers[state.layer];
    if (!buf) return;
    if (state.tool === "pencil") {
      stampBrush(buf, x, y, paintColor());
      state.dirty = true;
      redraw();
    } else if (state.tool === "eraser") {
      stampBrush(buf, x, y, [0, 0, 0, 0]);
      state.dirty = true;
      redraw();
    } else if (state.tool === "bucket") {
      pushHistoryBeforeEdit();
      if (floodFill(buf, x, y, paintColor())) {
        state.dirty = true;
        redraw();
      } else {
        state.undoStack.pop();
        syncHistoryUi();
      }
    } else if (state.tool === "eyedropper") {
      const px = sampleComposite(x, y);
      if (px[3] === 0) {
        state.colorTransparent = true;
        state.color = [0, 0, 0, 0];
      } else {
        state.colorTransparent = false;
        state.color = [px[0], px[1], px[2], 255];
      }
      syncColorUi();
    }
  }

  async function init() {
    setStatus("Loading…");
    const infoRes = await fetch("/api/info");
    if (!infoRes.ok) throw new Error("Failed to load /api/info");
    state.info = await infoRes.json();
    el.charName.textContent = state.info.name;

    const { w, h } = state.info.canvas;
    for (const layer of state.info.layers) {
      state.visible[layer] = true;

      const li = document.createElement("li");

      const vis = document.createElement("button");
      vis.type = "button";
      vis.className = "vis-btn";
      vis.dataset.layer = layer;
      vis.setAttribute("aria-label", "Toggle visibility " + layer);
      updateVisButton(vis, layer);
      vis.addEventListener("click", (evt) => {
        evt.stopPropagation();
        state.visible[layer] = !state.visible[layer];
        updateVisButton(vis, layer);
        redraw();
      });

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "layer-btn";
      btn.dataset.layer = layer;
      btn.textContent = layer;
      btn.addEventListener("click", () => selectLayer(layer));

      li.appendChild(vis);
      li.appendChild(btn);
      el.layerList.appendChild(li);

      const img = await loadImage("/api/layer/" + layer + ".png");
      state.buffers[layer] = imageToImageData(img, w, h);
      try {
        const under = await loadImage("/api/underlay/" + layer + ".png");
        state.underlays[layer] = imageToImageData(under, w, h);
      } catch {
        state.underlays[layer] = null;
      }
    }

    syncColorUi();
    syncHistoryUi();
    selectLayer(state.info.layers[0]);
    setStatus("Ready");
  }

  document.querySelectorAll(".tools .tool").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.tool = btn.dataset.tool;
      document.querySelectorAll(".tools .tool").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });

  el.color.addEventListener("input", () => {
    const [r, g, b] = hexToRgb(el.color.value);
    state.colorTransparent = false;
    state.color = [r, g, b, 255];
    syncColorUi();
  });

  el.colorSwatch.addEventListener("click", () => {
    el.color.click();
  });

  el.colorTransparent.addEventListener("change", () => {
    state.colorTransparent = el.colorTransparent.checked;
    if (state.colorTransparent) {
      state.color = [0, 0, 0, 0];
    } else if (state.color[3] === 0) {
      const [r, g, b] = hexToRgb(el.color.value);
      state.color = [r, g, b, 255];
    }
    syncColorUi();
  });

  el.brushSize.addEventListener("change", () => {
    let n = Number(el.brushSize.value) || 1;
    n = Math.max(1, Math.min(16, Math.round(n)));
    state.brushSize = n;
    el.brushSize.value = String(n);
  });
  el.brushSize.addEventListener("input", () => {
    let n = Number(el.brushSize.value) || 1;
    n = Math.max(1, Math.min(16, Math.round(n)));
    state.brushSize = n;
  });

  el.zoom.addEventListener("input", () => {
    state.zoom = Number(el.zoom.value);
    el.zoomLabel.textContent = state.zoom + "×";
    redraw();
  });

  el.underlay.addEventListener("change", () => {
    state.showUnderlay = el.underlay.checked;
    redraw();
  });

  el.undo.addEventListener("click", () => undo());
  el.redo.addEventListener("click", () => redo());

  el.view.addEventListener("mousedown", (evt) => {
    if (evt.button !== 0) return;
    const { x, y } = canvasCoords(evt);
    if (state.tool === "pencil" || state.tool === "eraser") {
      pushHistoryBeforeEdit();
      state.strokeActive = true;
      state.painting = true;
      paintAt(x, y);
    } else if (state.tool === "bucket") {
      paintAt(x, y);
    } else if (state.tool === "eyedropper") {
      paintAt(x, y);
    }
  });

  el.view.addEventListener("mousemove", (evt) => {
    if (!state.painting) return;
    const { x, y } = canvasCoords(evt);
    paintAt(x, y);
  });

  window.addEventListener("mouseup", () => {
    state.painting = false;
    state.strokeActive = false;
  });

  window.addEventListener("keydown", (evt) => {
    if (evt.target && /INPUT|TEXTAREA|SELECT/.test(evt.target.tagName)) return;
    const key = evt.key.toLowerCase();
    const mod = evt.ctrlKey || evt.metaKey;
    if (mod && key === "z" && !evt.shiftKey) {
      evt.preventDefault();
      undo();
      return;
    }
    if (mod && (key === "y" || (key === "z" && evt.shiftKey))) {
      evt.preventDefault();
      redo();
      return;
    }
    if (key === "p") document.querySelector('[data-tool="pencil"]').click();
    if (key === "g") document.querySelector('[data-tool="bucket"]').click();
    if (key === "i") document.querySelector('[data-tool="eyedropper"]').click();
    if (key === "e") document.querySelector('[data-tool="eraser"]').click();
  });

  el.apply.addEventListener("click", async () => {
    el.apply.disabled = true;
    setStatus("Saving…");
    try {
      const layers = {};
      for (const name of state.info.layers) {
        layers[name] = imageDataToDataUrl(state.buffers[name]);
      }
      const res = await fetch("/api/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ layers }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Apply failed");
      }
      state.dirty = false;
      setStatus(
        "Saved " + data.written.length + " layers. Ask agent to compose_character.",
        "ok"
      );
    } catch (err) {
      setStatus(String(err.message || err), "err");
    } finally {
      el.apply.disabled = false;
    }
  });

  init().catch((err) => setStatus(String(err.message || err), "err"));
})();
