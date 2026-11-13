// SPDX-License-Identifier: MIT
// The Blender controls page. Protocol: qaros_anari/protocol.py. Every user has a copy of this
// page; Blender holds the truth and posts it back, so the page only shows state and asks for edits.
"use strict";

const panel = window.qaros?.panel;
const SEND_INTERVAL_MS = 33; // a drag sends at most ~30 values a second; Blender applies 20

// The hand-written tabs: one spec per slider row (key and index on the wire, range, step).
const GROUPS = {
  location: { label: ["X", "Y", "Z"], step: 0.1, digits: 2, span: 5 },
  rotation: { label: ["X", "Y", "Z"], step: 15, digits: 0, min: -180, max: 180, unit: "°" },
  scale: { label: ["X", "Y", "Z"], step: 0.1, digits: 2, min: 0.01, max: 5 },
};
const MATERIAL_ROWS = [
  { key: "color", index: 0, label: "R", min: 0, max: 1, step: 0.05, digits: 2 },
  { key: "color", index: 1, label: "G", min: 0, max: 1, step: 0.05, digits: 2 },
  { key: "color", index: 2, label: "B", min: 0, max: 1, step: 0.05, digits: 2 },
  { key: "roughness", index: 0, label: "Ro", min: 0, max: 1, step: 0.05, digits: 2 },
  { key: "metallic", index: 0, label: "Me", min: 0, max: 1, step: 0.05, digits: 2 },
];

let state = null; // {object, material, frame}
let panels = []; // [{name, title, items}]: Blender's panels, replayed
let panelName = null;
let panelSignature = "";
let group = "location";
let callId = 0;
const rows = new Map(); // row id -> slider row (hand-written and generated alike)
const updaters = new Map(); // generated control id -> function(item) showing a new value
let dragging = null; // id of the slider under a finger: incoming state does not move it
let lastSent = 0;
let pending = null;

function send(message) {
  panel?.send(message);
}

// Sends at most one message per interval while a value moves; the released value always goes.
function sendThrottled(message, final) {
  const now = performance.now();
  if (final || now - lastSent >= SEND_INTERVAL_MS) {
    lastSent = now;
    pending = null;
    send(message);
    return;
  }
  if (!pending) {
    setTimeout(() => {
      if (pending) {
        lastSent = performance.now();
        send(pending);
        pending = null;
      }
    }, SEND_INTERVAL_MS);
  }
  pending = message;
}

function format(value, digits, unit) {
  return value === null || value === undefined ? "-" : Number(value).toFixed(digits) + (unit || "");
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// A slider row: label, slider, value, - and +. `spec.send(value, final)` says where edits go.
function makeRow(container, spec) {
  const row = element("div", "row");
  const label = element("label", "", spec.label);
  const slider = document.createElement("input");
  slider.type = "range";
  slider.step = "any";
  slider.id = `${spec.domId}-slider`;
  const value = element("span", "value");
  const minus = element("button", "", "−");
  minus.id = `${spec.domId}-minus`;
  const plus = element("button", "", "+");
  plus.id = `${spec.domId}-plus`;
  row.append(label, slider, value, minus, plus);
  container.append(row);

  const entry = { id: spec.id, row, slider, value, spec, current: null };
  rows.set(spec.id, entry);
  slider.addEventListener("pointerdown", () => (dragging = spec.id));
  slider.addEventListener("input", () => {
    dragging = spec.id;
    entry.value.textContent = format(slider.valueAsNumber, spec.digits, spec.unit);
    spec.send(slider.valueAsNumber, false);
  });
  slider.addEventListener("change", () => {
    spec.send(slider.valueAsNumber, true);
    dragging = null;
  });
  const stepBy = (sign) => {
    if (entry.current === null) return;
    spec.send(entry.current + sign * spec.step, true);
  };
  minus.addEventListener("click", () => stepBy(-1));
  plus.addEventListener("click", () => stepBy(1));
  return entry;
}

function setSpec(spec) {
  return {
    ...spec,
    id: `${spec.key}/${spec.index}`,
    domId: `${spec.key}-${spec.index}`,
    send: (value, final) => sendThrottled({ t: "set", key: spec.key, index: spec.index, value, final }, final),
  };
}

function showRow(entry, value, enabled) {
  const { spec, slider } = entry;
  entry.current = value;
  entry.row.classList.toggle("off", !enabled);
  for (const control of entry.row.querySelectorAll("input, button")) control.disabled = !enabled;
  if (!enabled) {
    entry.value.textContent = "-";
    return;
  }
  if (dragging === entry.id) return; // the finger owns this slider until it lets go
  // Without a fixed range the slider spans the value +- span, re-centred on every update.
  const min = spec.span !== undefined ? Math.floor(value) - spec.span : spec.min;
  const max = spec.span !== undefined ? Math.floor(value) + spec.span : spec.max;
  slider.min = String(min);
  slider.max = String(max);
  slider.value = String(value);
  entry.value.textContent = format(value, spec.digits, spec.unit);
}

// ---- Hand-written tabs: Object, Material, Frame ----

function buildObjectRows() {
  const container = document.getElementById("rows-object");
  container.replaceChildren();
  const spec = GROUPS[group];
  for (let index = 0; index < 3; index++) {
    makeRow(container, setSpec({ ...spec, key: group, index, label: spec.label[index] }));
  }
}

// Only the tabs the add-on's preferences switch on: the add-on refuses edits from the others.
function showTabs(tabs) {
  if (!Array.isArray(tabs)) return;
  for (const button of document.querySelectorAll("#tabs button")) button.hidden = !tabs.includes(button.dataset.tab);
  document.getElementById("no-tabs").hidden = tabs.length > 0;
  const active = document.querySelector("#tabs button.active");
  if (tabs.length && (!active || active.hidden)) selectTab(tabs[0]);
  if (!tabs.length) for (const tab of document.querySelectorAll(".tab")) tab.classList.remove("active");
}

function render() {
  showTabs(state?.tabs);
  const object = state?.object;
  document.getElementById("object-name").textContent = object ? object.name : "No object";
  const values = object ? object[group] : null;
  for (let index = 0; index < 3; index++) {
    const entry = rows.get(`${group}/${index}`);
    if (entry) showRow(entry, values ? values[index] : null, !!values);
  }
  document.getElementById("object-note").textContent = !object
    ? "Select an object in Blender."
    : group === "rotation" && !object.rotation
      ? "Rotation mode is not Euler: edit it in Blender."
      : "";

  const material = state?.material;
  document.getElementById("material-name").textContent = material?.name || "No material";
  const swatch = document.getElementById("swatch");
  const color = material?.color;
  swatch.style.background = color
    ? `rgb(${color.map((c) => Math.round(Math.pow(Math.min(Math.max(c, 0), 1), 1 / 2.2) * 255)).join(",")})`
    : "transparent";
  for (const spec of MATERIAL_ROWS) {
    const entry = rows.get(`${spec.key}/${spec.index}`);
    const value = spec.key === "color" ? (color ? color[spec.index] : null) : material?.[spec.key];
    showRow(entry, value ?? null, value !== null && value !== undefined);
  }
  document.getElementById("material-note").textContent =
    material?.reason || (material && !color ? "Linked inputs are edited in Blender." : "");

  const frame = state?.frame;
  document.getElementById("frame-number").textContent = frame ? String(frame.current) : "-";
  const frameEntry = rows.get("frame/0");
  if (frame) {
    frameEntry.spec.min = frame.start;
    frameEntry.spec.max = frame.end;
  }
  showRow(frameEntry, frame ? frame.current : null, !!frame);
  document.getElementById("play").textContent = frame?.playing ? "Pause" : "Play";
}

// ---- The Panels tab: Blender's own panels, replayed (blender_panels.py) ----

function sendProp(item, index, value, final) {
  sendThrottled({ t: "prop", id: item.id, index, value, final }, final);
}

function numberRows(container, item) {
  const components = item.length ? (item.index >= 0 ? [item.index] : [...Array(item.length).keys()]) : [-1];
  container.append(element("div", "field-label", item.label));
  const entries = components.map((component) => {
    const letter = component >= 0 ? (item.components || "")[component] || String(component) : "";
    const soft = item.soft;
    const spec = {
      id: `${item.id}/${component}`,
      domId: `${item.id}-${component}`.replace(/[^A-Za-z0-9_-]/g, "_"),
      label: letter,
      step: item.step || 1,
      digits: item.type === "int" ? 0 : item.unit === "deg" ? 0 : 2,
      unit: item.unit === "deg" ? "°" : "",
      ...(soft ? { min: soft[0], max: soft[1] } : { span: item.unit === "deg" ? 180 : 5 }),
      send: (value, final) => sendProp(item, component, item.type === "int" ? Math.round(value) : value, final),
    };
    return { component, entry: makeRow(container, spec) };
  });
  return (next) => {
    for (const { component, entry } of entries) {
      const value = Array.isArray(next.value) ? next.value[component] : next.value;
      showRow(entry, value, !next.readonly);
    }
  };
}

function toggleRow(container, item) {
  const row = element("div", "toggles");
  row.append(element("span", "", item.label));
  const components = item.length ? [...Array(item.length).keys()] : [-1];
  const buttons = components.map((component) => {
    const button = element("button", "", component >= 0 ? (item.components || "XYZW")[component] : "");
    button.id = `${item.id}-${component}-toggle`.replace(/[^A-Za-z0-9_-]/g, "_");
    button.addEventListener("click", () => {
      const shown = button.classList.contains("active");
      // An inverted checkbox shows the opposite of the property; send what the property becomes.
      const next = item.invert ? shown : !shown;
      sendProp(item, component, next, true);
    });
    row.append(button);
    return { component, button };
  });
  container.append(row);
  return (next) => {
    for (const { component, button } of buttons) {
      const raw = component >= 0 ? next.value[component] : next.value;
      const shown = next.invert ? !raw : !!raw;
      button.classList.toggle("active", shown);
      if (component < 0) button.textContent = shown ? "On" : "Off";
      button.disabled = !!next.readonly;
    }
  };
}

function enumRow(container, item) {
  container.append(element("div", "field-label", item.label));
  const select = document.createElement("select");
  select.id = `${item.id}-select`.replace(/[^A-Za-z0-9_-]/g, "_");
  for (const [identifier, name] of item.items) {
    const option = document.createElement("option");
    option.value = identifier;
    option.textContent = name;
    select.append(option);
  }
  select.addEventListener("change", () => sendProp(item, -1, select.value, true));
  container.append(select);
  return (next) => {
    if (document.activeElement !== select) select.value = next.value;
    select.disabled = !!next.readonly;
  };
}

function textRow(container, item) {
  const row = element("div", "text-line");
  const value = element("span", "value");
  row.append(element("span", "field-label", item.label), value);
  container.append(row);
  return (next) => (value.textContent = next.value || "-");
}

const SUGGESTIONS_SHOWN = 6;

// A text or link field, typed on the mixer's keyboard (it shows while the input has focus). Enter or
// leaving the field sends it, Escape puts Blender's value back. A field with `choices` lists the
// names that match what was typed as buttons: CEF draws no datalist popup into an off-screen page.
function textField(container, item) {
  container.append(element("div", "field-label", item.label));
  const row = element("div", "text-field");
  const input = document.createElement("input");
  input.type = "text";
  input.id = `${item.id}-text`.replace(/[^A-Za-z0-9_-]/g, "_");
  input.autocomplete = "off";
  input.spellcheck = false;
  input.enterKeyHint = "done";
  if (item.type === "pointer") input.placeholder = "None";
  row.append(input);
  let current = item; // the latest state of this field
  let shown = item.value ?? ""; // Blender's value, what Escape restores
  const suggestions = element("div", "suggestions");
  const pick = (value) => {
    input.value = value;
    sendProp(current, -1, value, true);
    // A touch may have moved focus already, so blur alone does not always clear the list.
    suggestions.replaceChildren();
    input.blur();
  };
  if (item.type === "pointer") {
    const clear = element("button", "", "×");
    clear.title = "Unlink";
    clear.addEventListener("click", () => pick(""));
    row.append(clear);
  }
  container.append(row, suggestions);
  const suggest = () => {
    suggestions.replaceChildren();
    if (!current.choices || document.activeElement !== input) return;
    const typed = input.value.toLowerCase();
    for (const name of current.choices.filter((n) => n.toLowerCase().includes(typed)).slice(0, SUGGESTIONS_SHOWN)) {
      const button = element("button", "", name);
      // pointerdown, not click: the input's blur would hide the list before a click lands.
      button.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        pick(name);
      });
      suggestions.append(button);
    }
  };
  const accepts = (value) => !value || !current.choices || current.more || current.choices.includes(value);
  input.addEventListener("focus", suggest);
  input.addEventListener("input", suggest);
  input.addEventListener("blur", () => suggestions.replaceChildren());
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") input.blur();
    if (event.key === "Escape") {
      input.value = shown;
      input.blur();
    }
  });
  input.addEventListener("change", () => {
    if (input.value === shown) return;
    if (accepts(input.value)) sendProp(current, -1, input.value, true);
    else input.value = shown; // not a name Blender has: back to its value
  });
  return (next) => {
    current = next;
    shown = next.value ?? "";
    if (document.activeElement !== input) input.value = shown; // never under a typing finger
    input.disabled = !!next.readonly;
    input.maxLength = next.maxlen || 1024;
  };
}

function buildPanel(current) {
  const body = document.getElementById("panel-body");
  body.replaceChildren();
  for (const key of [...rows.keys()]) if (key.includes(":")) rows.delete(key);
  updaters.clear();
  if (!current) {
    body.append(element("p", "note", "Select an object in Blender."));
    return;
  }
  current.items.forEach((item, position) => {
    let update = null;
    if (item.kind === "heading") body.append(element("div", "heading", item.text));
    else if (item.kind === "note") body.append(element("p", "note", item.text));
    else if (item.kind === "text") update = textRow(body, item);
    else if (item.type === "float" || item.type === "int") update = numberRows(body, item);
    else if (item.type === "boolean") update = toggleRow(body, item);
    else if (item.type === "enum") update = enumRow(body, item);
    else if (item.type === "string" || item.type === "pointer") update = textField(body, item);
    if (update) updaters.set(position, update);
  });
}

// What a panel looks like without its values: a rebuild only when this changes, so a value update
// never replaces the control under a finger.
function signatureOf(list) {
  return JSON.stringify(list.map((p) => [p.name, p.items.map((i) => [i.kind, i.id, i.label, i.type, i.length, i.text])]));
}

function renderPanels() {
  const names = document.getElementById("panel-names");
  if (!panels.some((p) => p.name === panelName)) panelName = panels[0]?.name ?? null;
  const signature = signatureOf(panels) + panelName;
  if (signature !== panelSignature) {
    panelSignature = signature;
    names.replaceChildren();
    for (const p of panels) {
      const button = element("button", p.name === panelName ? "active" : "", p.title);
      button.dataset.panel = p.name;
      button.addEventListener("click", () => {
        panelName = p.name;
        renderPanels();
      });
      names.append(button);
    }
    buildPanel(panels.find((p) => p.name === panelName));
  }
  const current = panels.find((p) => p.name === panelName);
  current?.items.forEach((item, position) => updaters.get(position)?.(item));
}

// ---- Start ----

function selectTab(name) {
  for (const button of document.querySelectorAll("#tabs button")) button.classList.toggle("active", button.dataset.tab === name);
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("active", tab.id === `tab-${name}`);
}

function selectGroup(name) {
  group = name;
  for (const button of document.querySelectorAll("#groups button")) button.classList.toggle("active", button.dataset.group === name);
  buildObjectRows();
  render();
}

function call(name) {
  send({ t: "call", id: ++callId, name });
}

function start() {
  for (const button of document.querySelectorAll("#tabs button")) button.addEventListener("click", () => selectTab(button.dataset.tab));
  for (const button of document.querySelectorAll("#groups button")) button.addEventListener("click", () => selectGroup(button.dataset.group));
  document.getElementById("undo").addEventListener("click", () => call("undo"));
  document.getElementById("redo").addEventListener("click", () => call("redo"));
  document.getElementById("play").addEventListener("click", () => call("play"));
  const materialRows = document.getElementById("rows-material");
  for (const spec of MATERIAL_ROWS) makeRow(materialRows, setSpec(spec));
  makeRow(document.getElementById("rows-frame"), setSpec({ key: "frame", index: 0, label: "F", min: 0, max: 250, step: 1, digits: 0 }));
  buildObjectRows();
  render();
  renderPanels();
  panel?.onMessage((message) => {
    if (message?.t !== "state") return;
    if (message.key === "scene") {
      state = message.value;
      render();
    } else if (message.key === "panels") {
      panels = Array.isArray(message.value) ? message.value : [];
      renderPanels();
    }
  });
  // Every copy of the page asks for the state when it loads: a reloaded page starts empty.
  send({ t: "hello" });
}

start();
