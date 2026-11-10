"""The live stream's sound: the Shorts' sound design (explainer.soundtrack),
made while the stream runs, one video frame's worth at a time.

- when a generation turns (every 2 minutes): born cells play soft high notes and
  dying cells soft low ones, a few real cells each (pitch: height on the board,
  pan: left-right); more cells, more notes and a little louder
- a chime where a comment lands, a warm chord when a comment saves it
- a soft pulse that follows the phase, quieter than in the Shorts because it
  plays around the clock; dying is one long fading tone
- a faint airy chord that follows the phase
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
PULSE_LEVEL = 0.3  # with OUT below, a bit softer than the Shorts' pulse: it never stops
PAD = [440.0, 493.88, 523.25, 554.37, 659.25]
CHORDS = {ALIVE: {440.0, 554.37, 659.25}, FLATLINING: {440.0, 523.25, 659.25},
          CONTESTED: {440.0, 493.88, 659.25}, DIED: set()}
CHIME = (880.0, 1108.73, 1318.51)
SAVED = (220.0, 277.18, 329.63, 440.0, 554.37)
OUT = 1.5  # about 9 dB above the Shorts' per-event level (their master is 0.56 * tanh(1.2 x)): the stream is sparse
MAX_STEP = 6  # more generations than this at once (a restart, a pause) play no notes
AHEAD_S = 14.0  # how far ahead notes can be mixed in


class Soundtrack:
    def __init__(self, seed: int = 3):
        self.rng = np.random.default_rng(seed)
        self.buf = np.zeros((2, int(AHEAD_S * SR)), np.float32)  # what is still to be played
        self.t = 0  # samples made so far
        self.next_pulse = int(0.8 * SR)
        self.phase: str | None = None
        self.pad_gain = np.zeros(len(PAD))
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
    def generation(self, before: np.ndarray, after: np.ndarray, steps: int = 1) -> None:
        """A generation turned (or a comment's cells appeared): its births and deaths play."""
        if before is None or before.shape != after.shape or steps > MAX_STEP or self.phase == DIED:
            return
        h, w = after.shape
        born = np.argwhere((after == 1) & (before == 0))
        died = np.argwhere((after == 0) & (before == 1))
        self._voices(born, "born", ex.NOTES, 5, 0.035, 0.075, 0.05, h, w)
        self._voices(died, "died", LOW, 3, 0.03, 0.06, 0.19, h, w)  # a beat after the births

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

    def landed(self, x: float, w: int) -> None:
        """A comment landed at column x: a soft rising arpeggio (several: one after another)."""
        start = max(self.t + int(0.05 * SR), self.last_chime + int(0.35 * SR))
        if start - self.t > 3 * SR:  # a burst: don't pile them up
            return
        self.last_chime = start
        pan = x / max(1, w - 1) * 1.4 - 0.7
        for j, f in enumerate(CHIME):
            self._mix(self._note(("chime", f), f, 2.5, 0.8), 0.04, (start - self.t) / SR + j * 0.09, pan)

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
        # the faint chord, gains easing toward the phase's chord
        target = np.array([1.0 if v in CHORDS[self.phase or ALIVE] else 0.0 for v in PAD])
        new = self.pad_gain + (target - self.pad_gain) * 0.09
        t = (t0 + np.arange(n)) / SR
        ramp = np.linspace(0, 1, n, endpoint=False)
        for v, g0, g1 in zip(PAD, self.pad_gain, new):
            if g0 < 1e-4 and g1 < 1e-4:
                continue
            swell = 0.6 + 0.4 * np.sin(2 * np.pi * 0.07 * t + v) ** 2
            out += (0.0022 * (g0 + (g1 - g0) * ramp) * swell * np.sin(2 * np.pi * v * t)).astype(np.float32)
        self.pad_gain = new
        self.t += n
        pcm = np.clip(OUT * np.tanh(1.2 * out), -1, 1)
        return (pcm.T * 32767).astype("<i2").tobytes()
