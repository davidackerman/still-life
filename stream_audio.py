"""The live stream's sound: the Shorts' sound design (explainer.soundtrack),
made while the stream runs, one video frame's worth at a time.

- when a generation turns (every 2 minutes): born cells play soft high notes and
  dying cells soft low ones, a few real cells each (pitch: height on the board,
  pan: left-right); more cells, more notes and a little louder
- a chime where a comment lands, a warm chord when a comment saves it
- a soft pulse that follows the phase, quieter than in the Shorts because it
  plays around the clock; dying is one long fading tone
- underneath, a low bed of slow chords. While it's alive it drifts through a
  calm progression (a chord every CHORD_S, each fading into the next); stuck or
  contested, it moves within seconds to one darker chord and holds it
The notes go through the same soft reverb as the Shorts'. Notes are rendered
once (with their reverb tails) and mixed in ahead of time.
"""

from __future__ import annotations

import numpy as np

import explainer as ex
from universe import ALIVE, CONTESTED, DIED, FLATLINING

SR = ex.SR  # 48 kHz, as the Shorts
LOW = [f / 2 for f in ex.NOTES[:6]]  # A2..A3, for deaths
PULSE = {ALIVE: (880.0, 1.5, 0.035), CONTESTED: (880.0, 0.9, 0.04), FLATLINING: (659.25, 2.2, 0.04)}
PULSE_LEVEL = 0.5  # with DRIVE below: it never stops, but the owner wanted it a little more present
# The bed: (root, the notes above it), in A like everything else.
PROGRESSION = [  # alive: A maj9, F#m9, D maj9, E sus, and round again
    (110.00, (164.81, 207.65, 246.94, 277.18)),
    (92.50, (164.81, 207.65, 220.00, 277.18)),
    (73.42, (185.00, 220.00, 277.18, 329.63)),
    (82.41, (185.00, 220.00, 246.94, 277.18)),
]
HOLD = {FLATLINING: (110.00, (164.81, 220.00, 261.63)),  # A minor: stuck in a loop
        CONTESTED: (110.00, (164.81, 220.00, 246.94))}  # A sus2: a comment broke the loop; undecided
CHORD_S = 80.0  # each chord of the progression
EASE_SLOW, EASE_FAST, FAST_S = 0.012, 0.09, 6.0  # per 0.1 s: chords blend over ~25 s; a new phase in ~2 s
BED = {"root": 0.0053, "note": 0.0034}  # per voice (the root a little more: low notes sound quieter)
CHIME = (880.0, 1108.73, 1318.51)
SAVED = (220.0, 277.18, 329.63, 440.0, 554.37)
# The master: a soft limiter, tanh(DRIVE x). Below about -6 dBFS it is a plain
# gain of DRIVE; louder peaks are rounded off instead of clipped. 5.7 is the
# owner's "louder overall": 10 dB over the old 1.5 tanh(1.2 x).
DRIVE = 5.7
MAX_STEP = 6  # more generations than this at once (a restart, a pause) play no notes
AHEAD_S = 14.0  # how far ahead notes can be mixed in


class Soundtrack:
    def __init__(self, seed: int = 3):
        self.rng = np.random.default_rng(seed)
        self.buf = np.zeros((2, int(AHEAD_S * SR)), np.float32)  # what is still to be played
        self.t = 0  # samples made so far
        self.next_pulse = int(0.8 * SR)
        self.phase: str | None = None
        self.bed: dict = {}  # (kind, freq) -> gain now
        self.fast_until = 0  # sample: until then the bed follows the phase quickly
        self.last_chime = -SR
        self._notes: dict = {}

    # -------------------------------------------------------------- notes
    def _note(self, key, freq, dur, decay, attack=0.008, harmonics=((1, 1.0), (2, 0.25), (4, 0.06))):
        """(dry, wet) at unit level, the wet with its whole reverb tail. Cached."""
        if key not in self._notes:
            dry = ex._note(freq, dur, 1.0, decay, attack=attack, harmonics=harmonics)
            pad = np.concatenate([dry, np.zeros(int(2.5 * SR), np.float32)])
            wet = ex._reverb(np.stack([pad, pad]))
            self._notes[key] = (pad, wet)
        return self._notes[key]

    def warm(self) -> None:
        """Render every note once up front, so the first generation doesn't stutter."""
        for f in ex.NOTES:
            self._note(("born", f), f, 2.0, 0.55)
        for f in LOW:
            self._note(("died", f), f, 1.0, 0.25)
        for f in CHIME:
            self._note(("chime", f), f, 2.5, 0.8)
        for f in SAVED:
            self._note(("saved", f), f, 4.0, 1.6, attack=0.15, harmonics=((1, 1.0), (2, 0.15)))

    def _mix(self, note, amp: float, delay_s: float, pan: float = 0.0, reverb: bool = True) -> None:
        dry, wet = note
        at = int(delay_s * SR)
        n = min(len(dry), self.buf.shape[1] - at)
        if n <= 0:
            return
        a = (pan + 1) * np.pi / 4
        gains = (np.cos(a), np.sin(a))
        for ch in (0, 1):
            sig = 0.75 * dry[:n] + (0.55 * wet[ch, :n] if reverb else 0)
            self.buf[ch, at:at + n] += (amp * gains[ch] * sig).astype(np.float32)

    # -------------------------------------------------------------- events
    def generation(self, before: np.ndarray, after: np.ndarray, steps: int = 1, gain: float = 1.0) -> None:
        """A generation turned (or a comment's cells appeared): its births and deaths play.
        `gain`: softer for the rewind and the replay, which play many at once."""
        if before is None or before.shape != after.shape or steps > MAX_STEP or self.phase == DIED:
            return
        h, w = after.shape
        born = np.argwhere((after == 1) & (before == 0))
        died = np.argwhere((after == 0) & (before == 1))
        self._voices(born, "born", ex.NOTES, 5, 0.035 * gain, 0.075 * gain, 0.05, h, w)
        self._voices(died, "died", LOW, 3, 0.03 * gain, 0.06 * gain, 0.19, h, w)  # a beat after the births

    def _voices(self, cells, kind, notes, max_k, lo, hi, delay, h, w) -> None:
        count = len(cells)
        if not count:
            return
        k = int(np.clip(np.ceil(np.log2(count + 1) / 1.6), 1, max_k))  # 1 cell: 1 note ... ~250: 5
        loud = lo + (hi - lo) * min(1.0, np.log2(count + 1) / 8)
        picks = cells[self.rng.choice(count, size=k, replace=False)]
        for j, (y, x) in enumerate(sorted(picks.tolist(), key=lambda p: -p[0])):  # lowest on the board first
            f = notes[min(len(notes) - 1, int((h - 1 - y) / h * len(notes)))]
            note = self._note((kind, f), f, *((2.0, 0.55) if kind == "born" else (1.0, 0.25)))
            self._mix(note, loud / np.sqrt(k), delay + j * 0.055, pan=x / max(1, w - 1) * 1.4 - 0.7)

    def landed(self, x: float, w: int, gain: float = 1.0) -> None:
        """A comment landed at column x: a soft rising arpeggio (several: one after another)."""
        start = max(self.t + int(0.05 * SR), self.last_chime + int(0.35 * SR))
        if start - self.t > 3 * SR:  # a burst: don't pile them up
            return
        self.last_chime = start
        pan = x / max(1, w - 1) * 1.4 - 0.7
        for j, f in enumerate(CHIME):
            self._mix(self._note(("chime", f), f, 2.5, 0.8), 0.04 * gain, (start - self.t) / SR + j * 0.09, pan)

    def set_phase(self, phase: str) -> None:
        was, self.phase = self.phase, phase
        if was is None or was == phase:
            return
        if was == CONTESTED and phase == ALIVE:  # saved
            for j, f in enumerate(SAVED):
                note = self._note(("saved", f), f, 4.0, 1.6, attack=0.15, harmonics=((1, 1.0), (2, 0.15)))
                self._mix(note, 0.035, 0.05 + j * 0.05)
        if phase == DIED:  # the monitor flatlines
            tt = np.arange(int(6 * SR)) / SR
            tone = 0.03 * np.minimum(1, tt / 0.05) * np.clip((6 - tt) / 3, 0, 1) * np.sin(2 * np.pi * 880 * tt)
            self._mix((tone.astype(np.float32), None), 1 / 0.75, 0.05, reverb=False)
        self.next_pulse = max(self.next_pulse, self.t + int(0.4 * SR))
        self.fast_until = self.t + int(FAST_S * SR)

    def chord(self, t: int | None = None) -> tuple[float, tuple]:
        """(root, notes) the bed is heading for at sample t."""
        t = self.t if t is None else t
        if self.phase in HOLD:
            return HOLD[self.phase]
        return PROGRESSION[int(t / SR / CHORD_S) % len(PROGRESSION)]

    # -------------------------------------------------------------- the bed
    def _bed(self, out: np.ndarray, t0: int, n: int) -> None:
        """Mix the next n samples of the low chords into `out`, every voice easing
        toward the chord of the moment (silent once it has died)."""
        target = {}
        if self.phase != DIED:
            root, notes = self.chord(t0)
            target = {("root", root): 1.0, **{("note", f): 1.0 for f in notes}}
        k = (EASE_FAST if t0 < self.fast_until else EASE_SLOW) * n / (SR / 10)
        t = (t0 + np.arange(n)) / SR
        ramp = np.linspace(0, 1, n, endpoint=False)
        for key in set(self.bed) | set(target):
            kind, f = key
            g0 = self.bed.get(key, 0.0)
            g1 = g0 + (target.get(key, 0.0) - g0) * min(1.0, k)
            self.bed[key] = g1
            if g0 < 1e-4 and g1 < 1e-4:
                self.bed.pop(key)
                continue
            w = 2 * np.pi * f * t
            tone = np.sin(w) + 0.5 * np.sin(w * 1.0025)  # a slightly detuned twin: a slow, warm beat
            if kind == "root":
                tone += 0.35 * np.sin(2 * w)  # its octave, so small speakers carry it
            swell = 0.65 + 0.35 * np.sin(2 * np.pi * (0.025 + (f % 7) * 0.007) * t + f) ** 2
            out += (BED[kind] * (g0 + (g1 - g0) * ramp) * swell * tone).astype(np.float32)

    # -------------------------------------------------------------- output
    def chunk(self, n: int) -> bytes:
        """The next n samples, as 16-bit stereo PCM (interleaved)."""
        t0 = self.t
        p = PULSE.get(self.phase or ALIVE) if self.phase != DIED else None
        while p and self.next_pulse < t0 + n:
            freq, gap, amp = p
            beep = self._note(("pulse", freq), freq, 0.6, 0.12, attack=0.01, harmonics=((1, 1.0),))
            at = (self.next_pulse - t0) / SR
            self._mix((beep[0], beep[1]), amp * PULSE_LEVEL * 0.5, at, reverb=True)  # half into the music
            self._mix((beep[0], None), amp * PULSE_LEVEL / 0.75, at, reverb=False)  # and the dry monitor
            self.next_pulse += int(gap * SR)
        out = self.buf[:, :n].copy()
        self.buf[:, :-n] = self.buf[:, n:]
        self.buf[:, -n:] = 0
        self._bed(out, t0, n)
        self.t += n
        pcm = np.tanh(DRIVE * out)
        return (pcm.T * 32767).astype("<i2").tobytes()
