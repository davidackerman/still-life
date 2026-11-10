// Sound for the live page, off until the visitor turns it on: the same sound
// design as the Shorts (explainer.soundtrack in the repo), synthesized live with
// the Web Audio API.
//   - cells born play soft high notes, cells that die soft low ones, pooled every
//     0.3 s; more cells, more notes and a little louder. Pitch: height on the
//     board. Pan: left-right.
//   - a soft pulse that follows the phase (ALIVE every 1.5 s, CONTESTED faster,
//     FLATLINING low and slow); dying is one long fading tone
//   - a chime when a comment lands, a warm chord when the universe is saved
//   - a faint airy chord that follows the phase
// Everything but the pulse goes through a soft reverb.
"use strict";

const Sound = (() => {
  const NOTES = [0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24].map((s) => 220 * 2 ** (s / 12));  // A pentatonic, 2 octaves
  const LOW = NOTES.slice(0, 6).map((f) => f / 2);  // for deaths, an octave down
  const PULSE = { ALIVE: [880, 1.5, 0.035], CONTESTED: [880, 0.9, 0.04], FLATLINING: [659.25, 2.2, 0.04] };  // Hz, every s, level
  const PAD = [440, 493.88, 523.25, 554.37, 659.25];
  const CHORD = { ALIVE: [440, 554.37, 659.25], FLATLINING: [440, 523.25, 659.25], CONTESTED: [440, 493.88, 659.25] };
  const LEVEL = 0.8;  // about the Shorts' loudness
  const TICK_S = 0.3, AHEAD_S = 1.1;  // notes pool per tick; the pulse is scheduled this far ahead (background tabs tick slowly)
  const MAX_STEP = 60;  // a jump of more generations than this is a cut, not music
  const PREF = "still-life-sound";

  let ctx = null, master = null, dry = null, music = null, padGains = [];
  let on = false, phase = null, nextPulse = 0, nextTick = 0, lastChime = 0;
  let last = null, w = 128, h = 72;
  const born = new Set(), died = new Set();

  function wanted() { try { return localStorage.getItem(PREF) === "on"; } catch (e) { return false; } }
  function remember(v) { try { localStorage.setItem(PREF, v ? "on" : "off"); } catch (e) { /* private mode: not remembered */ } }

  function reverb() {  // a darkened noise tail, like the Shorts'
    const sr = ctx.sampleRate, rt = 2.4, pre = Math.floor(0.02 * sr), n = Math.floor(rt * sr);
    const buf = ctx.createBuffer(2, pre + n, sr), a = 1 - Math.exp(-2 * Math.PI * 2500 / sr);
    for (let c = 0; c < 2; c++) {
      const d = buf.getChannelData(c);
      let y = 0;
      for (let i = 0; i < n; i++) {
        y += a * ((Math.random() * 2 - 1) * Math.exp(-6.9 * i / sr / rt) - y);
        d[pre + i] = y;
      }
    }
    const conv = ctx.createConvolver();
    conv.buffer = buf;  // normalized by the node
    return conv;
  }

  function start() {
    if (ctx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (navigator.audioSession) navigator.audioSession.type = "playback";  // iPhone: play on silent mode, like a video does
    ctx = new AC();
    const limit = ctx.createDynamicsCompressor();  // a safety net, like the Shorts' soft clip
    limit.threshold.value = -6; limit.knee.value = 6; limit.ratio.value = 8;
    limit.attack.value = 0.003; limit.release.value = 0.25;
    master = ctx.createGain();
    master.gain.value = 0;
    master.connect(limit).connect(ctx.destination);
    dry = ctx.createGain();
    dry.connect(master);
    music = ctx.createGain();
    const direct = ctx.createGain(), send = ctx.createGain();
    direct.gain.value = 0.75; send.gain.value = 0.55;
    music.connect(direct).connect(master);
    music.connect(send).connect(reverb()).connect(master);
    const pad = ctx.createGain(), lfo = ctx.createOscillator(), depth = ctx.createGain();
    pad.gain.value = 0.0022 * 0.8;  // the airy chord, slowly swelling
    lfo.frequency.value = 0.07; depth.gain.value = 0.0022 * 0.2;
    lfo.connect(depth).connect(pad.gain);
    lfo.start();
    pad.connect(music);
    padGains = PAD.map((f) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = f; g.gain.value = 0;
      o.connect(g).connect(pad);
      o.start();
      return g;
    });
    setInterval(schedule, 100);
  }

  // A soft plucked tone (kalimba-like): quick attack, exponential decay.
  function note(freq, at, { dur, amp, decay, attack = 0.008, harm = [[1, 1], [2, 0.25], [4, 0.06]], pan = 0, bus = music }) {
    const out = ctx.createStereoPanner ? ctx.createStereoPanner() : ctx.createGain();
    if (out.pan) out.pan.value = Math.max(-1, Math.min(1, pan));
    out.connect(bus);
    for (const [k, weight] of harm) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = freq * k;
      g.gain.setValueAtTime(0, at);
      g.gain.linearRampToValueAtTime(amp * weight, at + attack);
      g.gain.setTargetAtTime(0, at + attack, 1 / (1 / decay + 3 * (k - 1)));  // higher harmonics fade faster
      o.connect(g).connect(out);
      o.start(at);
      o.stop(at + dur);
    }
  }

  // A few real cells from `cells` play; pitch by row (higher up, higher), pan by column.
  function voices(cells, notes, maxK, lo, hi, decay, dur, at) {
    const count = cells.length;
    if (!count) return;
    const k = Math.max(1, Math.min(maxK, Math.ceil(Math.log2(count + 1) / 1.6)));  // 1 cell: 1 note ... ~250: 5
    const loud = lo + (hi - lo) * Math.min(1, Math.log2(count + 1) / 8);
    for (let j = 0; j < k; j++) {  // k distinct picks
      const r = j + Math.floor(Math.random() * (count - j));
      [cells[j], cells[r]] = [cells[r], cells[j]];
    }
    cells.slice(0, k).sort((a, b) => b - a).forEach((i, j) => {  // lowest on the board first
      const x = i % w, y = Math.floor(i / w);
      note(notes[Math.min(notes.length - 1, Math.floor((h - 1 - y) / h * notes.length))], at + j * 0.055,
           { dur, amp: loud / Math.sqrt(k), decay, pan: x / (w - 1) * 1.4 - 0.7 });
    });
  }

  function schedule() {
    if (!on || ctx.state !== "running") return;
    const now = ctx.currentTime;
    if (now >= nextTick && (born.size || died.size)) {
      voices([...born], NOTES, 5, 0.035, 0.075, 0.55, 2.0, now + 0.05);
      voices([...died], LOW, 3, 0.03, 0.06, 0.25, 1.0, now + 0.19);  // a beat after the births
      born.clear(); died.clear();
      nextTick = now + TICK_S;
    }
    const p = PULSE[phase];
    if (!p) return;
    if (nextPulse < now) nextPulse = now + 0.05;  // the first beat, or after a pause: no backlog
    for (; nextPulse < now + AHEAD_S; nextPulse += p[1]) {
      const beat = { dur: 0.6, amp: p[2], decay: 0.12, attack: 0.01, harm: [[1, 1]] };
      note(p[0], nextPulse, { ...beat, bus: dry });
      note(p[0], nextPulse, { ...beat, amp: p[2] * 0.5 });
    }
  }

  function setPhase(p) {
    if (p === phase) return;
    const was = phase, now = ctx.currentTime, chord = CHORD[p] || [];
    phase = p;
    PAD.forEach((f, i) => padGains[i].gain.setTargetAtTime(chord.includes(f) ? 1 : 0, now, 1.0));
    if (p === "DIED" && was) {  // the monitor flatlines: one long tone that fades
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880;
      g.gain.setValueAtTime(0, now);
      g.gain.linearRampToValueAtTime(0.03, now + 0.05);
      g.gain.setValueAtTime(0.03, now + 3);
      g.gain.linearRampToValueAtTime(0, now + 6);
      o.connect(g).connect(dry);
      o.start(now);
      o.stop(now + 6.1);
    }
    if (was === "DIED" || PULSE[was]?.[1] !== PULSE[p]?.[1]) nextPulse = now + 0.4;  // the new rhythm starts cleanly
  }

  // Each drawn frame: the generation shown, its grid (Uint8Array h*w) and its phase.
  function frame(gen, grid, width, height, p) {
    if (!on || !ctx) return;
    w = width; h = height;
    setPhase(p);
    if (p !== "DIED" && last && grid !== last.grid && grid.length === last.grid.length
        && gen >= last.gen && gen - last.gen <= MAX_STEP) {
      const a = last.grid;
      for (let i = 0; i < grid.length; i++) {
        if (grid[i] && !a[i]) born.add(i);
        else if (!grid[i] && a[i]) died.add(i);
      }
    }
    last = { gen, grid };
  }

  function playing() { return on && ctx && ctx.state === "running"; }

  // A comment landed at column x: a soft rising arpeggio. Several at once play
  // one after another, but never pile up more than a second ahead.
  function chime(x) {
    if (!playing()) return;
    const now = ctx.currentTime;
    if (lastChime > now + 1) return;
    const at = Math.max(now + 0.05, lastChime + 0.35), pan = x / (w - 1) * 1.4 - 0.7;
    lastChime = at;
    [880, 1108.73, 1318.51].forEach((f, j) => note(f, at + j * 0.09, { dur: 2.5, amp: 0.04, decay: 0.8, pan }));
  }

  function saved() {  // a warm chord
    if (!playing()) return;
    const at = ctx.currentTime + 0.05;
    [220, 277.18, 329.63, 440, 554.37].forEach((f, j) =>
      note(f, at + j * 0.05, { dur: 4, amp: 0.035, decay: 1.6, attack: 0.15, harm: [[1, 1], [2, 0.15]] }));
  }

  function setOn(v) {
    on = v;
    remember(v);
    if (v) {
      start();
      if (!ctx) return (on = false);
      ctx.resume().catch(() => {});
      master.gain.setTargetAtTime(LEVEL, ctx.currentTime, 0.05);
    } else if (ctx) {
      master.gain.setTargetAtTime(0, ctx.currentTime, 0.05);
      last = null; phase = null; born.clear(); died.clear();
      setTimeout(() => { if (!on) ctx.suspend(); }, 400);
    }
    return on;
  }

  // The button. A remembered "on" can't play until the visitor clicks
  // something; if that click is the button itself, it starts the sound.
  function toggle() { return setOn(!playing()); }
  function unlock() { if (on && ctx && ctx.state !== "running") ctx.resume().catch(() => {}); }  // any click or key
  function restore() { return wanted() ? setOn(true) : false; }

  return { frame, chime, saved, toggle, unlock, restore };
})();

if (typeof module !== "undefined") module.exports = Sound;
