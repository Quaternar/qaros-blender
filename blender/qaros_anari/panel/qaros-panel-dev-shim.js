// SPDX-License-Identifier: MIT
// Only for opening the page in a desktop browser: in the mixer qaros.panel exists and this file
// does nothing. Try a state from the console:
// __qarosDevReceive({t: "state", key: "scene", value: {object: {name: "Cube", location: [0, 0, 0],
//   rotation: [0, 0, 0], scale: [1, 1, 1]}, material: null, frame: {current: 1, start: 1, end: 250,
//   playing: false}}})
if (!window.qaros?.panel) {
  const listeners = [];
  window.qaros = Object.freeze({
    ...(window.qaros || {}),
    panel: Object.freeze({
      send: async (message) => console.log("qaros.panel.send", message),
      onMessage: (callback) => {
        listeners.push(callback);
        return () => {};
      },
    }),
  });
  window.__qarosDevReceive = (message) => listeners.forEach((callback) => callback(message));
}
