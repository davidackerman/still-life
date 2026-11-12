// Rebuild the universe's past from the public log (state/log.jsonl), in the
// browser. Same rules and order as the bot: at each generation, logged
// injections are OR-ed in first, then the grid steps to the next generation.
// Keeps a snapshot every SNAP generations so scrubbing is instant. It also keeps
// the paint (the colors people gave their moves) exactly as life.paint_step does.
"use strict";

const History = (() => {
  const SNAP = 50;
  const COLORS = ["red", "orange", "yellow", "blue", "purple", "pink", "cyan"];  // paint 1..7 (0: none), as commands.COLORS
  const DY = [-1, -1, -1, 0, 0, 1, 1, 1], DX = [-1, 0, 1, -1, 1, -1, 0, 1];  // the 8 neighbours, read from the top left

  // The paint one generation on, from `before` to `after` = step(before). A cell
  // that lives on keeps its paint; a cell born takes its three parents' paint (the
  // majority's, or if all three differ, the first one's). Dead cells have none.
  function paintStep(before, after, paint, h, w) {
    const out = new Uint8Array(h * w);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = y * w + x;
      if (!after[i]) continue;
      if (before[i]) { out[i] = paint[i]; continue; }
      const p = [];
      for (let k = 0; k < 8; k++) {
        const j = ((y + DY[k] + h) % h) * w + ((x + DX[k] + w) % w);
        if (before[j]) p.push(paint[j]);
      }
      out[i] = p[0] === p[1] || p[0] === p[2] ? p[0] : p[1] === p[2] ? p[1] : p[0];
    }
    return out;
  }

  function parse(text) {
    const lines = text.split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
    const g = lines[0];
    if (!g || g.type !== "genesis") throw new Error("log does not start with a genesis line");
    const injections = new Map(); // gen -> [{cells, command, changed}]
    const events = []; // {gen, kind: "comment"|"phase", ...}
    const effects = new Map(); // "gen|command" -> {before, after, horizon}: its lifespan before -> after
    let lastGen = 0, checked = null;  // checked: when the bot last read the comments
    for (const e of lines) {
      if (e.type === "effect") effects.set(`${e.gen}|${e.command}`, e);
    }
    for (const e of lines) {
      if (e.type === "injection") {
        if (!injections.has(e.gen)) injections.set(e.gen, []);
        const effect = effects.get(`${e.gen}|${e.command}`) || null;
        injections.get(e.gen).push({ ...e, effect });
        events.push({ gen: e.gen, kind: "comment", command: e.command, changed: e.changed, effect, via: e.via });
      } else if (e.type === "phase") {
        events.push({ gen: e.gen, kind: "phase", to: e.to, reason: e.reason, deadline: e.deadline_gen });
      } else if (e.type === "tick") {
        lastGen = Math.max(lastGen, e.gen_end);
        if (e.now) checked = Math.max(checked || 0, e.now);
      }
    }
    return { genesis: g, injections, events, lastGen, checked };
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
  // What a move does, as a little film of box x box grids, exactly as render.demo
  // draws it on the stream: its first cells for FILM_HOLD frames, `gens`
  // generations, then its last for FILM_HOLD, every film the same length so they
  // start again together. Gliders, blocks and blinkers wrap on their own box;
  // the bursts grow out of it into a world 4x the box.
  const FILM_HOLD = 6;
  const FILM_SEED = {  // render.demo's "random" (numpy seed 2) and "cells" (a T of four)
    random: [[0, 0], [1, 0], [3, 0], [1, 1], [2, 1], [3, 1], [1, 2], [2, 2], [4, 2], [3, 3], [4, 3], [0, 4], [4, 4]],
    cells: [[0, 0], [1, 0], [2, 0], [1, 1]],
  };
  const films = new Map();
  function film(name, box = 9, gens = 28) {
    const key = `${name}|${box}|${gens}`;
    if (films.has(key)) return films.get(key);
    const pts = FILM_SEED[name] || PATTERNS[name];
    const small = name.startsWith("glider") || name === "block" || name === "blinker";
    const world = small ? box : 4 * box;
    const pw = Math.max(...pts.map((p) => p[0])) + 1, ph = Math.max(...pts.map((p) => p[1])) + 1;
    const x0 = Math.floor((world - pw) / 2), y0 = Math.floor((world - ph) / 2), o = Math.floor((world - box) / 2);
    let g = new Uint8Array(world * world);
    for (const [x, y] of pts) g[((y0 + y) % world) * world + ((x0 + x) % world)] = 1;
    const crop = (grid) => {
      const c = new Uint8Array(box * box);
      for (let y = 0; y < box; y++) for (let x = 0; x < box; x++) c[y * box + x] = grid[(o + y) * world + (o + x)];
      return c;
    };
    const frames = [];
    for (let i = 0; i <= gens; i++) { frames.push(crop(g)); g = LifeCore.step(g, world, world); }
    const out = [...Array(FILM_HOLD).fill(frames[0]), ...frames.slice(1), ...Array(FILM_HOLD).fill(frames[frames.length - 1])];
    films.set(key, out);
    return out;
  }

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
      this.checked = parsed.checked;
      this.snaps = new Map(); // gen -> {grid, paint} after that gen's injections (paint: null until a color)
      this.seed0 = seedGrid(g.seed, this.h, this.w);  // before any comment at gen 0
      this.cur = { gen: 0, ...this.inject(0, this.seed0, null) };
      this.snaps.set(0, this.cur);
    }

    // That generation's moves, OR-ed in; a move with a color paints its cells.
    inject(gen, grid, paint) {
      const batch = this.injections.get(gen);
      if (!batch) return { grid, paint };
      const out = grid.slice();
      for (const inj of batch) {
        const k = COLORS.indexOf(inj.color) + 1;
        if (k && !paint) paint = new Uint8Array(this.h * this.w);  // (a paint passed in is always a fresh copy)
        for (const [x, y] of inj.cells) {
          const i = (y % this.h) * this.w + (x % this.w);
          out[i] = 1;
          if (k) paint[i] = k;
        }
      }
      return { grid: out, paint };
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
        start = { gen: k, ...this.snaps.get(k) };
      }
      let { gen: at, grid, paint } = start;
      while (at < gen) {
        const next = LifeCore.step(grid, this.h, this.w);
        ({ grid, paint } = this.inject(at + 1, next, paint && paintStep(grid, next, paint, this.h, this.w)));
        at++;
        if (at % SNAP === 0 && !this.snaps.has(at)) this.snaps.set(at, { grid, paint });
      }
      this.cur = { gen: at, grid, paint };
      return grid;
    }

    // The grid at `gen` as if the moves that landed at `g0` never had: what the
    // page compares with to show what they did. Kept per g0, so following it is cheap.
    without(g0, gen) {
      if (!this.cf) this.cf = new Map();  // g0 -> {gen, grid}
      let c = this.cf.get(g0);
      if (!c || c.gen > gen) {
        const before = g0 === 0 ? this.seed0 : LifeCore.step(this.gridAt(g0 - 1), this.h, this.w);
        c = { gen: g0, grid: before };  // the moves at g0 never landed
      }
      while (c.gen < gen) c = { gen: c.gen + 1, grid: this.inject(c.gen + 1, LifeCore.step(c.grid, this.h, this.w), null).grid };
      this.cf.set(g0, c);
      return c.grid;
    }

    // The paint at `gen` (index per cell: 0 none, 1.. COLORS), or null if nobody had painted yet.
    paintAt(gen) {
      this.gridAt(gen);
      return this.cur.paint;
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

  return { parse, Timeline, seedGrid, paintStep, COLORS, film };
})();

if (typeof module !== "undefined") module.exports = History;
