// Game of Life on the still-life torus, matching life.py exactly.
// Grids are Uint8Array(H * W) of 0/1, row-major, origin top-left.
"use strict";

const LifeCore = (() => {
  // state.json stores the grid as base64 of numpy.packbits (row-major, MSB first).
  function decode(b64, h, w) {
    const bin = atob(b64);
    const g = new Uint8Array(h * w);
    for (let i = 0; i < h * w; i++) g[i] = (bin.charCodeAt(i >> 3) >> (7 - (i & 7))) & 1;
    return g;
  }

  // One B3/S23 generation with wrapping edges.
  function step(g, h, w) {
    const out = new Uint8Array(h * w);
    for (let y = 0; y < h; y++) {
      const up = ((y + h - 1) % h) * w, row = y * w, down = ((y + 1) % h) * w;
      for (let x = 0; x < w; x++) {
        const l = (x + w - 1) % w, r = (x + 1) % w;
        const n = g[up + l] + g[up + x] + g[up + r] + g[row + l] + g[row + r]
                + g[down + l] + g[down + x] + g[down + r];
        out[row + x] = n === 3 || (n === 2 && g[row + x]) ? 1 : 0;
      }
    }
    return out;
  }

  // 64-bit key from two independent 32-bit FNV-1a hashes (only for this page's
  // forecast; the bot itself uses BLAKE2b).
  function key(g) {
    let a = 0x811c9dc5, b = 0x01000193 ^ 0x5bd1e995;
    for (let i = 0; i < g.length; i += 8) {
      let byte = 0;
      for (let j = 0; j < 8; j++) byte = (byte << 1) | g[i + j];
      a = Math.imul(a ^ byte, 0x01000193) >>> 0;
      b = Math.imul(b ^ byte ^ (i & 0xff), 0x5bd1e995) >>> 0;
    }
    return a.toString(36) + ":" + b.toString(36);
  }

  // Evolve undisturbed from `g` (at generation `gen`) until a state repeats.
  // Works in chunks so a page stays responsive; resolves to the generation at
  // which the repeat is detected, or null if none within `maxGens`.
  function findRepeat(g, h, w, gen, maxGens, onProgress) {
    return new Promise((resolve) => {
      const seen = new Map([[key(g), gen]]);
      let cur = g, n = 0;
      function chunk() {
        const stop = Math.min(n + 400, maxGens);
        for (; n < stop; n++) {
          cur = step(cur, h, w);
          const k = key(cur);
          if (seen.has(k)) return resolve(gen + n + 1);
          seen.set(k, gen + n + 1);
        }
        if (onProgress) onProgress(n / maxGens);
        if (n >= maxGens) return resolve(null);
        setTimeout(chunk, 0);
      }
      chunk();
    });
  }

  return { decode, step, key, findRepeat };
})();

if (typeof module !== "undefined") module.exports = LifeCore;
