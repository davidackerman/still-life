// The board in 3D, as on the live stream (board3d.py in the repo): lit blocks on a
// floor tipped 25 degrees away. The camera looks at the board's centre with its top
// edge tipped away, so up is still up and column 0 is on the left; the whole board
// shows once, fitted inside the canvas with room for the numbers on all four edges.
// A glow goes under the blocks (they stay sharp). View.at() is where a board point
// lands on the canvas: the page puts its labels and ripples there.
"use strict";
const Board3D = (() => {
  const TILT = 25 * Math.PI / 180, DIST = 1.3;  // the tilt; the camera's distance in board widths
  const HEIGHT = 0.8, MARGIN = 0.12;  // a block's height and its gap to the next, in cells
  const FRONT = 0.55, SIDE = 0.38;  // the front and side faces' shade (the top is lit)
  const GLOW = 0.42, TRAIL_INSET = 0.14;  // (as the stream's: more glow reads as blur)
  const EYES = 0.25;  // a face on every block: each eye's radius, a fraction of its top's width (0: none), as the stream's
  const SCREEN = "#050609", FLOOR = "#0a0c10", GRID = "#16191f", EDGE = "#2a2e3a", MUTED = "#7d8391";
  const XS = 16, YS = 8;  // a number every this many columns / rows
  const ease = (t) => { t = Math.min(1, Math.max(0, t)); return t * t * (3 - 2 * t); };

  class View {
    // A board of h x w cells on a canvas of cw x ch px; `unit`: px per stream px (sizes of text and pads);
    // `top`, `bottom`: px kept clear above and below it (full screen: the status and the controls).
    constructor(h, w, cw, ch, unit, top = 0, bottom = 0) {
      this.h = h; this.w = w; this.cw = cw; this.ch = ch; this.unit = unit; this.inTop = top; this.inBottom = bottom;
      const s = Math.sin(TILT), c = Math.cos(TILT), D = DIST * w;
      this.c = [w / 2, h / 2 + D * s, D * c];
      this.fwd = [0, -s, -c];
      this.up = [0, -c, s];
      this.f = 1; this.ox = 0; this.oy = 0;
      const pts = [];
      for (const x of [0, w]) for (const y of [0, h]) for (const z of [0, HEIGHT]) pts.push(this.raw(x, y, z));
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
      const pad = 30 * unit;
      const room = Math.max(ch * 0.3, ch - top - bottom);
      this.f = Math.min((cw - 2 * pad) / (x1 - x0), (room - 2 * pad) / (y1 - y0));
      this.ox = cw / 2 - this.f * (x0 + x1) / 2;
      this.oy = top + room / 2 - this.f * (y0 + y1) / 2;
      this.edge = [this.at(0, 0), this.at(w, 0), this.at(w, h), this.at(0, h)];
      this.top = Math.min(...this.edge.map((p) => p[1]));
      this.bottom = Math.max(...this.edge.map((p) => p[1]));
      this.floor = null;
    }
    raw(x, y, z) {
      const vx = x - this.c[0], vy = y - this.c[1], vz = z - this.c[2];
      const d = vy * this.fwd[1] + vz * this.fwd[2];
      return [vx / d, -(vy * this.up[1] + vz * this.up[2]) / d];
    }
    at(x, y, z = 0) {  // a board point, in cells (a cell's centre is +0.5), on the canvas
      const r = this.raw(x, y, z);
      return [this.ox + this.f * r[0], this.oy + this.f * r[1]];
    }
    depth(x, y, z) { return (y - this.c[1]) * this.fwd[1] + (z - this.c[2]) * this.fwd[2]; }

    // The floor, its grid and the numbers along all four edges: drawn once per size.
    floorImage() {
      if (this.floor) return this.floor;
      const { h, w, cw, ch, unit } = this, img = document.createElement("canvas");
      img.width = cw; img.height = ch;
      const c = img.getContext("2d");
      c.fillStyle = SCREEN; c.fillRect(0, 0, cw, ch);
      const quad = (pts) => { c.beginPath(); c.moveTo(...pts[0]); for (const p of pts.slice(1)) c.lineTo(...p); c.closePath(); };
      quad(this.edge); c.fillStyle = FLOOR; c.fill();
      c.strokeStyle = GRID; c.lineWidth = Math.max(1, unit);
      c.beginPath();
      for (let y = 0; y <= h; y++) { c.moveTo(...this.at(0, y)); c.lineTo(...this.at(w, y)); }
      for (let x = 0; x <= w; x++) { c.moveTo(...this.at(x, 0)); c.lineTo(...this.at(x, h)); }
      c.stroke();
      quad(this.edge); c.strokeStyle = EDGE; c.stroke();
      const size = Math.round(15 * unit), font = (px) => `${px}px system-ui, -apple-system, "Segoe UI", sans-serif`;
      c.fillStyle = MUTED;
      for (let x = 0; x < w; x += XS) {  // x along the far (top) edge and the near (bottom) edge
        c.font = font(size); c.textAlign = "center"; c.textBaseline = "alphabetic";
        c.fillText(String(x), ...this.at(x + 0.5, -1.0));
        c.font = font(Math.round(17 * unit)); c.textBaseline = "top";
        c.fillText(String(x), ...this.at(x + 0.5, h + 1.0));
      }
      c.font = font(size); c.textBaseline = "middle";
      for (let y = 0; y < h; y += YS) {  // y down both sides
        c.textAlign = "right"; c.fillText(String(y), ...this.at(-0.9, y + 0.5));
        c.textAlign = "left"; c.fillText(String(y), ...this.at(w + 0.9, y + 0.5));
      }
      c.textAlign = "left"; c.textBaseline = "alphabetic"; c.fillText("x", ...this.at(w, -1.0));
      this.floor = img;
      return img;
    }

    // A cell's square on the floor (or `z` up), inset by `inset`, as a path on `c`.
    square(c, x, y, inset, z = 0) {
      c.beginPath();
      c.moveTo(...this.at(x + inset, y + inset, z)); c.lineTo(...this.at(x + 1 - inset, y + inset, z));
      c.lineTo(...this.at(x + 1 - inset, y + 1 - inset, z)); c.lineTo(...this.at(x + inset, y + 1 - inset, z));
      c.closePath();
    }

    // The board on `c`. o: {grid, before (or null), rise 0..1, color(i) -> [r, g, b] for a living cell,
    // tails: [[i, [r, g, b], alpha]], glow}.
    draw(c, o) {
      const { h, w } = this, g = o.grid, was = o.before || g, e = ease(o.rise ?? 1);
      c.drawImage(this.floorImage(), 0, 0);
      for (const [i, rgb, a] of o.tails || []) {  // the tails: floor tiles fading into the floor
        if (g[i] || was[i]) continue;
        this.square(c, i % w, Math.floor(i / w), TRAIL_INSET);
        c.fillStyle = `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${a.toFixed(3)})`; c.fill();
      }
      const blocks = [];
      for (let i = 0; i < g.length; i++) {
        const hgt = g[i] && was[i] ? HEIGHT : g[i] ? HEIGHT * e : was[i] ? HEIGHT * (1 - e) : 0;
        if (hgt <= 0.02) continue;
        const x = i % w, y = Math.floor(i / w);
        blocks.push({ i, x, y, hgt, rgb: o.color(i), d: this.depth(x + 0.5, y + 0.5, hgt / 2) });
      }
      blocks.sort((a, b) => b.d - a.d);  // far to near
      if (o.glow !== false) this.glow(c, blocks);
      const poly = (pts, rgb, k) => {
        c.beginPath(); c.moveTo(...pts[0]); for (let n = 1; n < pts.length; n++) c.lineTo(...pts[n]); c.closePath();
        c.fillStyle = `rgb(${Math.round(rgb[0] * k)}, ${Math.round(rgb[1] * k)}, ${Math.round(rgb[2] * k)})`; c.fill();
      };
      for (const b of blocks) {
        const X0 = b.x + MARGIN, X1 = b.x + 1 - MARGIN, Y0 = b.y + MARGIN, Y1 = b.y + 1 - MARGIN, z = b.hgt;
        const sx = b.x + 0.5 < w / 2 ? X1 : X0;  // the side facing the camera
        poly([this.at(sx, Y0, 0), this.at(sx, Y1, 0), this.at(sx, Y1, z), this.at(sx, Y0, z)], b.rgb, SIDE);
        poly([this.at(X0, Y1, 0), this.at(X1, Y1, 0), this.at(X1, Y1, z), this.at(X0, Y1, z)], b.rgb, FRONT);
        const top = b.rgb.map((v) => Math.min(255, v * 1.06 + 10));
        poly([this.at(X0, Y0, z), this.at(X1, Y0, z), this.at(X1, Y1, z), this.at(X0, Y1, z)], top, 1);
        if (EYES && z > 0.5 * HEIGHT) {  // two eyes on the top, toward its front edge, if big enough to read
          const p0 = this.at(X0, Y0, z), p1 = this.at(X1, Y0, z);
          const r = EYES * Math.hypot(p1[0] - p0[0], p1[1] - p0[1]);
          if (r > 0.75) for (const u of [0.27, 0.73]) {
            const [ex, ey] = this.at(X0 + u * (X1 - X0), Y0 + 0.6 * (Y1 - Y0), z);
            c.beginPath(); c.ellipse(ex, ey, r, r * 1.15, 0, 0, 2 * Math.PI);
            c.fillStyle = "#fff"; c.fill();
            c.lineWidth = Math.max(0.5, r * 0.18); c.strokeStyle = "rgb(20, 40, 30)"; c.stroke();
            c.beginPath(); c.arc(ex, ey + r * 0.25, r * 0.62, 0, 2 * Math.PI);  // the pupil, a little low: looking up at you
            c.fillStyle = "rgb(12, 12, 16)"; c.fill();
          }
        }
      }
    }

    // The tops' light, blurred, added to the floor before the blocks go on: drawn small and
    // brought back up (a blur that costs almost nothing, in every browser).
    glow(c, blocks) {
      const k = 4, sw = Math.max(1, Math.round(this.cw / k)), sh = Math.max(1, Math.round(this.ch / k));
      if (!this.g1 || this.g1.width !== sw) {
        this.g1 = Object.assign(document.createElement("canvas"), { width: sw, height: sh });
        this.g2 = Object.assign(document.createElement("canvas"), { width: Math.max(1, sw >> 1), height: Math.max(1, sh >> 1) });
      }
      const a = this.g1.getContext("2d"), b = this.g2.getContext("2d");
      a.setTransform(1, 0, 0, 1, 0, 0); a.clearRect(0, 0, sw, sh);
      a.setTransform(1 / k, 0, 0, 1 / k, 0, 0);
      for (const bl of blocks) {
        const z = bl.hgt, m = GLOW * Math.min(1, z / HEIGHT);
        this.square(a, bl.x, bl.y, MARGIN, z);
        a.fillStyle = `rgba(${bl.rgb[0]}, ${bl.rgb[1]}, ${bl.rgb[2]}, ${m.toFixed(3)})`; a.fill();
      }
      b.clearRect(0, 0, this.g2.width, this.g2.height);
      b.imageSmoothingEnabled = true; b.drawImage(this.g1, 0, 0, this.g2.width, this.g2.height);
      c.save();
      c.globalCompositeOperation = "lighter"; c.imageSmoothingEnabled = true;
      c.drawImage(this.g1, 0, 0, this.cw, this.ch);  // a tight glow
      c.globalAlpha = 0.8; c.drawImage(this.g2, 0, 0, this.cw, this.ch);  // and a wider one
      c.restore();
    }
  }

  // A legend film's frame as little blocks: a lit top standing up off a darker front.
  function film(c, f, n, px, rgb) {
    const s = px - 1, lift = Math.max(1, Math.round(s * 0.35));
    const css = (k) => `rgb(${Math.min(255, Math.round(rgb[0] * k))}, ${Math.min(255, Math.round(rgb[1] * k))}, ${Math.min(255, Math.round(rgb[2] * k))})`;
    for (let i = 0; i < f.length; i++) {
      if (!f[i]) continue;
      const x = (i % n) * px, y = Math.floor(i / n) * px;
      c.fillStyle = css(1.06); c.fillRect(x, y, s, s - lift);  // the top
      c.fillStyle = css(FRONT); c.fillRect(x, y + s - lift, s, lift);  // the front, below it
    }
  }

  return { View, HEIGHT, film };
})();
