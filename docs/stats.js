// Anonymous visit and button counting with GoatCounter (goatcounter.com): no
// cookies, no personal data, no consent banner. Its script sends only the page
// path and query (?gen=...), never anything typed or any #fragment. Events are
// bare names, namespaced "still-life/<name>" because the GoatCounter site is
// shared with David's other projects, counted at most once per visit.
"use strict";

const Stats = (() => {
  const GOATCOUNTER = "davidackerman";  // the "xyz" in xyz.goatcounter.com; "" = counting off
  const queue = [], sent = new Set();

  function send(name) {
    window.goatcounter.count({ path: `still-life/${name}`, title: `still-life: ${name}`, event: true });
  }
  function ready() { return window.goatcounter && typeof window.goatcounter.count === "function"; }
  function flush() { while (queue.length && ready()) send(queue.shift()); }

  // Counts the page view (count.js ignores localhost by itself).
  function start() {
    if (!GOATCOUNTER) return false;
    const s = document.createElement("script");
    s.async = true;
    s.src = "https://gc.zgo.at/count.js";
    s.dataset.goatcounter = `https://${GOATCOUNTER}.goatcounter.com/count`;
    s.addEventListener("load", flush);
    document.head.appendChild(s);
    return true;
  }

  function track(name) {
    if (!GOATCOUNTER || sent.has(name)) return;
    sent.add(name);
    if (ready()) send(name); else queue.push(name);
  }

  return { start, track, on: () => Boolean(GOATCOUNTER) };
})();

if (typeof module !== "undefined") module.exports = Stats;
