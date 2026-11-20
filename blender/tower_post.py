"""Finish a tower render: words over the frames, a soundtrack timed to the moves, an MP4.

python blender/tower_post.py FRAMES_DIR TIMING_JSON OUT.mp4 STATE_DIR VOXELS.npz
"""
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the repo
sys.path.insert(0, ROOT)
import explainer as ex  # noqa: E402
import look3d  # noqa: E402
import short  # noqa: E402

frames_dir, timing_path, out = sys.argv[1:4]
state = sys.argv[4] if len(sys.argv) > 4 else os.path.join(ROOT, "state")
FPS = 30
CHIME_GAP = 5  # frames between move chimes at the most (moves land faster than that in the busy days)
tm = json.load(open(timing_path))
N = tm["frames"]
files = sorted(f for f in os.listdir(frames_dir) if f.endswith((".png", ".jpg")))
assert len(files) == N, (len(files), N)
w_in, h_in = Image.open(os.path.join(frames_dir, files[0])).size
w0, h0 = w_in - w_in % 2, h_in - h_in % 2  # the encoder wants even sizes
k = w0 / short.PW  # the frames' scale (a test renders small)
ex.SCALE = 1

# ---- the words: how many moves, by how many people; then what the colors are
moves = short.comments_made(short.made_count(short.History(state)))
people = short.players(state)
who = "Hundreds of strangers" if people >= 200 else f"{people} strangers"


def fit(lines, width=980):  # each line's size, cut down until it fits across the screen
    out = []
    for txt, weight, size in lines:
        while size > 40 and ex.font(weight, size).getlength(txt) > width:
            size -= 2
        out.append((txt, weight, size))
    return out


WORDS = os.environ.get("TOWER_WORDS", "orbit")  # orbit: the noon tower's; fly: the fly-through's
def dies_day():  # the day the stream says it dies if nobody plays ("DIES SAT NOV 21" -> "Saturday"), from the state
    import engine, render, store
    world = store.Store(state).load(b"words-only")
    f = world.forecast or engine.refresh_forecast(world)
    big, _ = render.headline(world.universe, world.gen_seconds, f and f["stuck_gen"], f and engine.FORECAST_GENS,
                             world.launch_time)
    names = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday", "FRI": "Friday",
             "SAT": "Saturday", "SUN": "Sunday"}
    if not big.startswith("DIES ") or not f or not f.get("stuck_gen"):
        return None, big
    import time as _t
    days = (world.launch_time + f["stuck_gen"] * world.gen_seconds + 86400 - _t.time()) / 86400  # dies a day after it sticks
    _, wd, mon, dd = big.split()[:4]
    return (names.get(wd) if days <= 6 else f"{mon.title()} {int(dd)}"), big  # "Friday" when close, else "Nov 29"


if WORDS == "future":  # the tower to now, then the future if nobody comments, then one comment saving it
    day_name, big = dies_day()
    print("forecast:", big, flush=True)
    hook = fit([("Hundreds of YouTubers", "bold", 104), ("are building this.", "bold", 104)])
    end = fit([("Comment to save it.", "bold", 104)])
elif WORDS == "tree":  # the tree of life to now, withering if nobody comments, one comment bringing it back
    day_name, big = dies_day()
    print("forecast:", big, flush=True)
    hook = fit([("Hundreds of YouTubers", "bold", 104), ("are building a tree of life.", "bold", 104)])
    end = fit([("Comment to save it.", "bold", 104)])
elif WORDS == "fly":
    day_name, big = dies_day()
    print("forecast:", big, flush=True)
    hook = fit([(who, "bold", 104), ("are building this.", "bold", 104)])
    end = fit(([(f"It dies {day_name}", "bold", 104), ("unless people play.", "bold", 104)] if day_name else
               [("It dies", "bold", 104), ("unless people play.", "bold", 104)])
              + [("Comment, or join the live chat.", "demi", 62)])
else:
    hook = fit([(who, "bold", 104), ("built this.", "bold", 104)])
    end = fit([("Comment anything.", "bold", 104), ("Watch it appear live.", "demi", 70)])
n_hook = max(int(3.0 * FPS), short.rf(*[t for t, _, _ in hook]))
n_end = max(int(2.5 * FPS), short.rf(*[t for t, _, _ in end]))


seven = fit([("Every glow is a comment.", "bold", 84)] if WORDS == "fly" else [("It started with just 7 cells.", "bold", 84)])
n_seven = max(int(2.2 * FPS), short.rf(*[t for t, _, _ in seven]))
if os.environ.get("TOWER_NO_SECOND", "") == "1":  # the 12-second cut: straight from the hook to the counter
    n_seven = 0
GEN_S = short.History(state).gen_seconds


def texts_for(i):
    if WORDS in ("future", "tree"):
        if i < n_hook:
            return short.block(hook, short.TOP_Y, 1.0 if i < n_hook - short.FADE else short.fade(i, n_hook))
        if i >= N - n_end:
            return short.block(end, short.TOP_Y, min(1.0, (i - (N - n_end) + 1) / short.FADE))
        for a, b, lines in beats:
            if a <= i < b:
                return short.block(lines, short.TOP_Y, short.fade(i - a, b - a))
        return []
    if i < n_hook:  # the hook, over the whole tower and on into the dive
        return short.block(hook, short.TOP_Y, 1.0 if i < n_hook - short.FADE else short.fade(i, n_hook))
    if i >= N - n_end:  # and what you can do, as it finishes
        return short.block(end, short.TOP_Y, min(1.0, (i - (N - n_end) + 1) / short.FADE))
    if i < n_hook + n_seven:  # as the acorn bursts
        return short.block(seven, short.TOP_Y, short.fade(i - n_hook, n_seven))
    if voxels:  # then the day it has reached, counting up as it grows
        a = min(1.0, (i - n_hook - n_seven + 1) / short.FADE, (N - n_end - i) / short.FADE)
        day = int(vz["gens"][layer_at(i)]) * GEN_S // 86400 + 1
        return short.block([(f"Day {day}", "demi", 52)], short.TOP_Y, a * 0.8)  # quiet: it hints at time, the tower leads
    return []


# ---- the sound: the Shorts' own soundtrack (explainer.soundtrack), played from the layer the growth has reached:
# cells popping into existence play notes (high, soft), dying ones low; a chime as each move lands
voxels = sys.argv[5] if len(sys.argv) > 5 else None
SR = ex.SR
spf = SR // FPS
G0, GF, full, layer = tm["G0"], tm["GF"], tm["full"], tm["layer"]


class F:
    def __init__(self, grid, cue=None):
        self.grid, self.cue, self.grey, self.phase = grid, cue, 0.0, "ALIVE"


if voxels:
    vz = np.load(voxels)
    H, W = (int(v) for v in vz["shape"])
    grids = np.unpackbits(vz["grids"], axis=-1)[:, :, :W].astype(np.uint8)
    nl = len(grids)
    move_layer = {}
    for li, xx, mv in zip(vz["mt"], vz["mx"], vz["mv"]):
        move_layer.setdefault(int(mv), (int(li), []))[1].append(int(xx))

    FROM_L = tm.get("from", 0.0) / layer

    KEYS = tm.get("keys")

    def layer_at(i):
        if i < G0:
            return int(FROM_L) - 1 if FROM_L else nl - 1
        if KEYS and tm.get("G1", G0) > G0:  # regrown from the seed first: follow the growth's own keys
            kf, kh = zip(*KEYS)
            return int(np.clip(np.interp(i, kf, kh) / layer - 1e-6, 0, nl - 1))
        return int(np.clip(FROM_L + (i - G0) / max(1, GF - G0) * (full / layer - FROM_L), 0, nl - 1))
    if WORDS in ("future", "tree"):
        first = lambda L: next(i for i in range(N) if i >= G0 and layer_at(i) >= L)
        f_frozen, f_inject = first(int(vz["frozen"])), first(int(vz["inject"]))
        G1 = tm.get("G1", G0)
        a0 = max(n_hook, G1)
        dies = f"dies {day_name}." if day_name else "dies."
        revive = (f_inject, N - n_end, fit([("But one comment", "bold", 96), ("brings it back.", "bold", 96)]))
        if WORDS == "tree":  # the seed sprouting (after the hook); then one line for the withering, started early
            wither = fit([("If nobody comments,", "bold", 96), (f"it withers and {dies}", "bold", 96)])  # enough to read it
            w_at = max(n_hook, min(G1, f_inject - short.rf(*[t for t, _, _ in wither])))
            s0 = max(G0, n_hook)
            beats = ([(s0, min(s0 + max(n_seven, int(2.4 * FPS)), w_at), seven)] if G1 > G0 else []) + [
                (w_at, f_inject, wither), revive]
        else:
            beats = [(a0, f_frozen, fit([("If nobody comments,", "bold", 96), ("it freezes", "bold", 96)])),
                     (f_frozen, f_inject, fit([(f"and {dies}", "bold", 96)])), revive]
        for a, b, lines in beats:
            need = short.rf(*[t for t, _, _ in lines])
            print(f"beat {a}-{b} ({b - a} frames, reading needs {need}):", " / ".join(t for t, _, _ in lines),
                  "" if b - a >= need else "  <-- TOO SHORT", flush=True)
    by_layer = {}
    for mv, (li, xs) in move_layer.items():
        by_layer.setdefault(li, []).append(float(np.mean(xs)))
    frames_s, last_cue, prev = [], -99, None
    for i in range(N):
        L = layer_at(i)
        if WORDS in ("future", "tree") and i < G0 and FROM_L:  # the opening's sound: the whole history, swept through in the hold
            L = int(i / G0 * (FROM_L - 1))
        cue = None
        if prev is not None and L > prev and i - last_cue >= CHIME_GAP:
            landed = [x for l in range(prev + 1, L + 1) for x in by_layer.get(l, [])]
            if landed:
                cue, last_cue = ("inject", int(np.mean(landed))), i
        frames_s.append(F(grids[L], cue))
        prev = L
    tail_frames = int(2.5 * FPS)  # what rings past the end plays at the start: it loops
    audio = ex.soundtrack(frames_s + [F(frames_s[-1].grid)] * tail_frames)
    n = N * spf
    audio[:, :tail_frames * spf] += audio[:, n:n + tail_frames * spf]
    audio = audio[:, :n]
else:
    audio = np.zeros((2, N * spf), np.float32)
wav = os.path.splitext(out)[0] + ".wav"
ex.write_wav(wav, audio)

# ---- frames with words -> MP4
ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w0}x{h0}",
                       "-r", str(FPS), "-i", "-", "-i", wav, "-shortest", "-c:v", "libx264", "-preset", "medium",
                       "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out],
                      stdin=subprocess.PIPE)
for i, name in enumerate(files):
    im = Image.open(os.path.join(frames_dir, name)).convert("RGB").crop((0, 0, w0, h0))
    ts = texts_for(i)
    if ts:
        big = im.resize((short.PW, short.PH), Image.BILINEAR) if k != 1 else im
        back = look3d._backing(ts)  # a soft dark patch behind the words, as the 3D Shorts
        if back is not None:
            big = Image.alpha_composite(big.convert("RGBA"), back).convert("RGB")
        layer_img = Image.new("RGBA", big.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer_img)
        for s_, weight, size, xy, anchor, alpha in ts:
            ex.text(d, xy, s_, weight, size, alpha, anchor=anchor)
        big = Image.alpha_composite(big.convert("RGBA"), layer_img).convert("RGB")
        im = big.resize((w0, h0), Image.LANCZOS) if k != 1 else big
    ff.stdin.write(im.tobytes())
ff.stdin.close()
assert ff.wait() == 0
print("wrote", out, f"({os.path.getsize(out) / 1e6:.1f} MB)", "| hook", moves, people)
