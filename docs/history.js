// Rebuild the universe's past from the public log (state/log.jsonl), in the
// browser. Same rules and order as the bot: at each generation, logged
// injections are OR-ed in first, then the grid steps to the next generation.
// Keeps a snapshot every SNAP generations so scrubbing is instant.
"use strict";

const History = (() => {
  const SNAP = 50;

  function parse(text) {
    const lines = text.split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
    const g = lines[0];
    if (!g || g.type !== "genesis") throw new Error("log does not start with a genesis line");
    const injections = new Map(); // gen -> [{cells, command, changed}]
    const events = []; // {gen, kind: "comment"|"phase", ...}
    let lastGen = 0;
    for (const e of lines) {
      if (e.type === "injection") {
        if (!injections.has(e.gen)) injections.set(e.gen, []);
        injections.get(e.gen).push(e);
        events.push({ gen: e.gen, kind: "comment", command: e.command, changed: e.changed });
      } else if (e.type === "phase") {
        events.push({ gen: e.gen, kind: "phase", to: e.to, reason: e.reason, deadline: e.deadline_gen });
      } else if (e.type === "tick") {
        lastGen = Math.max(lastGen, e.gen_end);
      }
    }
    return { genesis: g, injections, events, lastGen };
  }

  // Seed grid: the named pattern centred, exactly like life.centered() in Python.
  const PATTERNS = {
    block: [[0, 0], [1, 0], [0, 1], [1, 1]],
    blinker: [[0, 0], [1, 0], [2, 0]],
    rpent: [[1, 0], [2, 0], [0, 1], [1, 1], [1, 2]],
    acorn: [[1, 0], [3, 1], [0, 2], [1, 2], [4, 2], [5, 2], [6, 2]],
    glider_se: [[1, 0], [2, 1], [0, 2], [1, 2], [2, 2]],
    glider_sw: [[1, 0], [0, 1], [0, 2], [1, 2], [2, 2]],
    glider_ne: [[0, 0], [1, 0], [2, 0], [2, 1], [1, 2]],
    glider_nw: [[0, 0], [1, 0], [2, 0], [0, 1], [1, 2]],
  };
  function seedGrid(name, h, w) {
    const pts = PATTERNS[name];
    const pw = Math.max(...pts.map((p) => p[0])) + 1, ph = Math.max(...pts.map((p) => p[1])) + 1;
    const x0 = Math.floor((w - pw) / 2), y0 = Math.floor((h - ph) / 2);
    const g = new Uint8Array(h * w);
    for (const [dx, dy] of pts) g[((y0 + dy) % h) * w + ((x0 + dx) % w)] = 1;
    return g;
  }

  class Timeline {
    constructor(parsed) {
      const g = parsed.genesis;
      [this.h, this.w] = g.shape;
      this.launch = g.launch_time;
      this.genSec = g.gen_seconds;
      this.injections = parsed.injections;
      this.events = parsed.events;
      this.lastGen = parsed.lastGen;
      this.snaps = new Map(); // gen -> grid after that gen's injections
      this.cur = { gen: 0, grid: this.inject(0, seedGrid(g.seed, this.h, this.w)) };
      this.snaps.set(0, this.cur.grid);
    }

    inject(gen, grid) {
      const batch = this.injections.get(gen);
      if (!batch) return grid;
      const out = grid.slice();
      for (const inj of batch) for (const [x, y] of inj.cells) out[(y % this.h) * this.w + (x % this.w)] = 1;
      return out;
    }

    cellsAt(gen) {
      const s = new Set();
      for (const inj of this.injections.get(gen) || []) for (const [x, y] of inj.cells) s.add(y * this.w + x);
      return s;
    }

    // The grid as it stood at `gen` (after that generation's injections).
    gridAt(gen) {
      gen = Math.max(0, Math.min(gen, this.lastGen));
      let start = this.cur.gen <= gen ? this.cur : null;
      const snapGen = Math.floor(gen / SNAP) * SNAP;
      if (!start || (this.snaps.has(snapGen) && snapGen > start.gen)) {
        let k = snapGen;
        while (k > 0 && !this.snaps.has(k)) k -= SNAP;
        start = { gen: k, grid: this.snaps.get(k) };
      }
      let { gen: at, grid } = start;
      while (at < gen) {
        grid = this.inject(at + 1, LifeCore.step(grid, this.h, this.w));
        at++;
        if (at % SNAP === 0 && !this.snaps.has(at)) this.snaps.set(at, grid);
      }
      this.cur = { gen: at, grid };
      return grid;
    }

    // Phase in effect at `gen`, and the deadline if any.
    phaseAt(gen) {
      let phase = "ALIVE", deadline = null, died = null;
      for (const e of this.events) {
        if (e.kind !== "phase" || e.gen > gen) continue;
        phase = e.to;
        if (e.to === "FLATLINING" && e.deadline != null) deadline = e.deadline;
        if (e.to === "ALIVE") deadline = null;
        if (e.to === "DIED") died = e.gen;
      }
      return { phase, deadline, died };
    }
  }

  return { parse, Timeline, seedGrid };
})();

if (typeof module !== "undefined") module.exports = History;
