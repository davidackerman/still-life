// Sound for the live page, off until the visitor turns it on: the same sound
// design as the Shorts (explainer.soundtrack in the repo), synthesized live with
// the Web Audio API.
//   - cells born play soft high notes, cells that die soft low ones, pooled every
//     0.3 s; more cells, more notes and a little louder. Pitch: height on the
//     board. Pan: left-right.
//   - a soft pulse that follows the phase (ALIVE every 1.5 s, CONTESTED faster,
//     FLATLINING low and slow); dying is one long fading tone
//   - a chime when a comment lands, a warm chord when the universe is saved
//   - underneath, the live stream's bed of low, slow chords (stream_audio.py):
//     alive, it drifts through a calm progression, a chord every 80 s, each
//     easing into the next; stuck or contested, it moves within seconds to one
//     darker chord and holds it; silent once it has died
// The notes go through a soft reverb; the pulse's monitor and the bed don't.
"use strict";

const Sound = (() => {
  const NOTES = [0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24].map((s) => 220 * 2 ** (s / 12));  // A pentatonic, 2 octaves
  const LOW = NOTES.slice(0, 6).map((f) => f / 2);  // for deaths, an octave down
  const PULSE = { ALIVE: [880, 1.5, 0.035], CONTESTED: [880, 0.9, 0.04], FLATLINING: [659.25, 2.2, 0.04] };  // Hz, every s, level
  // The bed: [root, the notes above it], in A like everything else (as stream_audio.py).
  const PROGRESSION = [  // alive: A maj9, F#m9, D maj9, E sus, and round again
    [110.00, [164.81, 207.65, 246.94, 277.18]], [92.50, [164.81, 207.65, 220.00, 277.18]],
    [73.42, [185.00, 220.00, 277.18, 329.63]], [82.41, [185.00, 220.00, 246.94, 277.18]]];
  const HOLD = { FLATLINING: [110.00, [164.81, 220.00, 261.63]],  // A minor: stuck in a loop
                 CONTESTED: [110.00, [164.81, 220.00, 246.94]] };  // A sus2: a comment broke the loop; undecided
  const CHORD_S = 80, BED = { root: 0.0053, note: 0.0034 };  // per voice
  const EASE_SLOW = 8.3, EASE_FAST = 1.1;  // s: chords blend over ~25 s; a new phase gets its chord in ~2 s
  const LEVEL = 0.8;  // about the Shorts' loudness
  const TICK_S = 0.3, AHEAD_S = 1.1;  // notes pool per tick; the pulse is scheduled this far ahead (background tabs tick slowly)
  const MAX_STEP = 60;  // a jump of more generations than this is a cut, not music
  const PREF = "still-life-sound";

  let ctx = null, master = null, dry = null, music = null, bed = new Map(), bedChord = null;
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
    for (const [root, notes] of PROGRESSION.concat(Object.values(HOLD))) {
      voice("root", root);
      notes.forEach((f) => voice("note", f));
    }
    setInterval(schedule, 100);
  }

  // One voice of the bed, silent until a chord wants it: the note and a slightly
  // detuned twin (a slow, warm beat), the root with its octave too (so small
  // speakers carry it), each swelling slowly at its own rate.
  function voice(kind, f) {
    const key = `${kind}:${f}`;
    if (bed.has(key)) return;
    const g = ctx.createGain(), swell = ctx.createGain(), lfo = ctx.createOscillator(), depth = ctx.createGain();
    g.gain.value = 0;
    swell.gain.value = 0.825;  // 0.65 + 0.35 sin^2(...), as a steady part and a wobble
    lfo.frequency.value = 2 * (0.025 + (f % 7) * 0.007);
    depth.gain.value = -0.175;
    lfo.connect(depth).connect(swell.gain);
    lfo.start();
    for (const [mult, amp] of [[1, 1], [1.0025, 0.5], ...(kind === "root" ? [[2, 0.35]] : [])]) {
      const o = ctx.createOscillator(), a = ctx.createGain();
      o.frequency.value = f * mult; a.gain.value = amp;
      o.connect(a).connect(swell);
      o.start();
    }
    swell.connect(g).connect(dry);
    bed.set(key, { g, level: BED[kind] });
  }

  // The chord the bed is heading for: the phase's, or the progression's (null: silent).
  function chordNow() {
    if (phase === "DIED") return null;
    return HOLD[phase] || PROGRESSION[Math.floor(Date.now() / 1000 / CHORD_S) % PROGRESSION.length];
  }

  function bedTo(chord, ease) {
    if (chord === bedChord) return;
    bedChord = chord;
    const want = new Set(chord ? [`root:${chord[0]}`, ...chord[1].map((f) => `note:${f}`)] : []);
    for (const [key, v] of bed) v.g.gain.setTargetAtTime(want.has(key) ? v.level : 0, ctx.currentTime, ease);
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
    if (phase) bedTo(chordNow(), EASE_SLOW);  // the progression moves on every CHORD_S
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
    const was = phase, now = ctx.currentTime;
    phase = p;
    bedTo(chordNow(), was ? EASE_FAST : EASE_SLOW);  // a new phase: its chord within seconds
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
      bedTo(null, 0.05); bedChord = undefined;  // back on, the bed fades in again
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
