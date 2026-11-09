"""What the video shows: the status title and the 1280x720 thumbnail."""

from __future__ import annotations

import math
import os

import numpy as np

import life
from universe import ALIVE, CONTESTED, DIED, FLATLINING, GEN_SECONDS, Universe

TITLE_BASE = "This video dies when its universe gets stuck in a loop"
CELL = 10  # px per cell: 128x72 cells -> 1280x720
THUMB_W, THUMB_H = 1280, 720  # YouTube's recommended thumbnail size; bigger boards are scaled to fit

BG = (10, 12, 16)
GRID = (22, 25, 32)
HIGHLIGHT = (255, 255, 255)  # cells injected since the last render
PHASE_COLOR = {
    ALIVE: (120, 230, 160),
    FLATLINING: (240, 90, 90),
    CONTESTED: (250, 200, 80),
    DIED: (120, 122, 135),
}


def time_left(gens: int, gen_seconds: int = GEN_SECONDS) -> str:
    """Short form for the thumbnail: '14h left', '45m left'."""
    secs = gens * gen_seconds
    if secs >= 3600:
        return f"{math.ceil(secs / 3600)}h left"
    return f"{max(1, math.ceil(secs / 60))}m left"


def duration(secs: float) -> str:
    """Countdown clock for titles, rounded down to the quarter hour (the title
    changes at most every 15 minutes): '3d 13h 45m', '14h 30m', '45m', 'under 15m'."""
    q = max(0, int(secs)) // 900 * 900
    d, h, m = q // 86400, q % 86400 // 3600, q % 3600 // 60
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else "under 15m"


VERB = "dies"  # one place to change the title's verb (e.g. "self-destructs")


def title(u: Universe, gen_seconds: int = GEN_SECONDS, stuck_gen: int | None = None,
          horizon_gens: int | None = None, saves: int = 0) -> str:
    """The video title: a countdown to death, plus " · saved N×" once comments
    have saved it from a countdown (last, so a phone cutting it off loses nothing).

    ALIVE needs the forecast (`stuck_gen`: when it would get stuck if nobody
    comments; None with `horizon_gens` = not within that many generations)."""
    base = _title(u, gen_seconds, stuck_gen, horizon_gens)
    return f"{base} · saved {saves}×" if saves else base


def _title(u: Universe, gen_seconds: int, stuck_gen: int | None, horizon_gens: int | None) -> str:
    if u.phase == DIED:
        return f"This video died: its universe got stuck in a loop"
    if u.phase == FLATLINING:
        return f"This video {VERB} in {duration(u.gens_left() * gen_seconds)}: its universe is stuck in a loop"
    if u.phase == CONTESTED:
        return f"This video might die in {duration(u.gens_left() * gen_seconds)}: a comment broke the loop"
    if stuck_gen is not None:
        when = duration((stuck_gen + u.flatline_gens - u.generation) * gen_seconds)
    elif horizon_gens is not None:
        when = f"{(horizon_gens + u.flatline_gens) * gen_seconds // 86400}+ days"  # beyond the forecast
    else:
        return f"{TITLE_BASE}"
    return f"This Game of Life video {VERB} in {when} if nobody comments"


_BRAILLE_BITS = ((0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08),
                 (1, 1, 0x10), (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80))


def braille(grid: np.ndarray) -> str:
    """The grid as Unicode braille, 2x4 cells per character (128x72 -> 64x18).

    About 3.5 KB of UTF-8, small enough for a video description (5000 bytes)."""
    h, w = grid.shape
    ph, pw = -h % 4, -w % 2
    g = np.pad(grid, ((0, ph), (0, pw)))
    code = np.zeros((g.shape[0] // 4, g.shape[1] // 2), dtype=np.int32)
    for dx, dy, bit in _BRAILLE_BITS:
        code += g[dy::4, dx::2].astype(np.int32) * bit
    return "\n".join("".join(chr(0x2800 + c) for c in row) for row in code)


def _font(size: int):
    from PIL import ImageFont

    return ImageFont.load_default(size=size)


def big_left(secs: float) -> str:
    """Thumbnail countdown, rounded down: '9 DAYS', '1 DAY' (24-48 h), '14 HOURS', '45 MINUTES'.
    Hours only in the last day, so hourly thumbnail refreshes happen only in the crisis."""
    secs = max(0, int(secs))
    if secs >= 2 * 86400:
        return f"{secs // 86400} DAYS"
    if secs >= 86400:
        return "1 DAY"
    if secs >= 2 * 3600:
        return f"{secs // 3600} HOURS"
    if secs >= 3600:
        return "1 HOUR"
    m = max(1, secs // 60)
    return f"{m} MINUTE" + ("" if m == 1 else "S")


def headline(u: Universe, gen_seconds: int = GEN_SECONDS, stuck_gen: int | None = None,
             horizon_gens: int | None = None) -> tuple[str, str]:
    """(big countdown, small context line) for the thumbnail."""
    if u.phase == DIED:
        return "DIED", f"its universe got stuck in a loop · gen {u.died_gen:,}"
    if u.phase in (FLATLINING, CONTESTED):
        left = big_left(u.gens_left() * gen_seconds)
        if u.phase == FLATLINING:
            return f"DIES IN {left}", f"stuck in a loop · gen {u.generation:,}"
        return f"MIGHT DIE IN {left}", f"a comment broke the loop · gen {u.generation:,}"
    if stuck_gen is not None:
        big = "DIES IN " + big_left((stuck_gen + u.flatline_gens - u.generation) * gen_seconds)
    elif horizon_gens is not None:
        big = f"DIES IN {(horizon_gens + u.flatline_gens) * gen_seconds // 86400}+ DAYS"
    else:
        big = "ALIVE"
    return big, f"if nobody comments · gen {u.generation:,}"


def thumbnail(u: Universe, path: str, highlight=(), gen_seconds: int = GEN_SECONDS,
              stuck_gen: int | None = None, horizon_gens: int | None = None) -> int:
    """Write the thumbnail PNG. `highlight` is an iterable of (x, y) cells to
    emphasize if alive (cells injected since the last render). The text is the
    countdown from headline(). Returns bytes."""
    from PIL import Image, ImageDraw

    h, w = u.grid.shape
    color = np.array(PHASE_COLOR[u.phase], np.uint8)
    hl = life.mask(highlight, (h, w)) if highlight else np.zeros((h, w), np.uint8)

    cell = max(4, min(CELL, THUMB_W // w))  # 10 px for 128x72; bigger boards get smaller cells
    inner = np.zeros((cell, cell), bool)
    inner[1:-1, 1:-1] = True
    tile = np.tile(inner, (h, w))
    alive = np.kron(u.grid, np.ones((cell, cell), np.uint8)).astype(bool) & tile
    lit = np.kron(u.grid & hl, np.ones((cell, cell), np.uint8)).astype(bool) & tile

    img = np.empty((h * cell, w * cell, 3), np.uint8)
    img[:] = GRID
    img[tile] = BG
    img[alive] = color
    img[lit] = HIGHLIGHT

    im = Image.fromarray(img)
    if im.size != (THUMB_W, THUMB_H):
        im = im.resize((THUMB_W, THUMB_H), Image.LANCZOS)
    im = im.convert("RGBA")
    band = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(band)
    W, H = im.size
    # Everything sits bottom-LEFT: YouTube overlays the video length on the
    # bottom-right corner (and hover icons on the top-right).
    for i in range(70):  # soft fade into the band
        d.line([(0, H - 210 - i), (W, H - 210 - i)], fill=(0, 0, 0, int(200 * (1 - i / 70))))
    d.rectangle([0, H - 210, W, H], fill=(0, 0, 0, 200))
    big, small = headline(u, gen_seconds, stuck_gen, horizon_gens)
    size = 110
    while size > 60 and d.textlength(big, font=_font(size)) > W * 0.76:  # stay clear of the duration badge
        size -= 4
    # Reads top to bottom: "DIES IN 9 DAYS" / "if nobody comments · gen 385"
    d.text((36, H - 92), big, font=_font(size), fill=PHASE_COLOR[u.phase] + (255,), anchor="ls")
    d.text((40, H - 34), small, font=_font(50), fill=(235, 235, 240, 255), anchor="ls")
    im = Image.alpha_composite(im, band).convert("RGB")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    im.save(path, "PNG", optimize=True)
    size = os.path.getsize(path)
    if size >= 2 * 1024 * 1024:
        raise RuntimeError(f"thumbnail {path} is {size} bytes; must be under 2 MB")
    return size


# ---------------------------------------------------------------- pinned comment
PINNED_MARKERS = ("Latest changes to the universe", "This video is alive", "This Short is alive")  # the owner's status comment
LIVE_ADDRESS = "davidackerman.github.io/still-life"
PHASE_WORDS = {
    "cycle_detected": "got stuck in a loop: the 24-hour countdown started",
    "new_cycle": "fell into a new loop: the countdown restarted",
    "reentered_cycle": "fell back into its loop: the countdown kept running",
    "injection": "a comment broke the loop",
    "survived_deadline": "saved: still changing when the countdown ran out",
    "deadline": "died",
}
EMOJI = {ALIVE: "🟢", FLATLINING: "🔴", CONTESTED: "🟡", DIED: "⚫"}


def coarse_left(secs: float) -> str:
    """Pinned-comment countdown, coarse so the comment isn't edited constantly:
    '~4 days', '~1 day', then hours only in the last day."""
    secs = max(0, int(secs))
    if secs >= 2 * 86400:
        return f"~{secs // 86400} days"  # rounded down, like the title and thumbnail
    if secs >= 86400:
        return "~1 day"
    if secs >= 2 * 3600:
        return f"~{secs // 3600} hours"
    return "under 2 hours"


def event_line(ev: dict) -> str | None:
    """One line of the pinned comment for a logged event (game data only)."""
    if ev["type"] == "injection":
        return f"gen {ev['gen']:,}: {ev['command']}" + ("" if ev["changed"] else " (changed nothing)")
    if ev["type"] == "phase":
        return f"gen {ev['gen']:,}: {PHASE_WORDS.get(ev['reason'], ev['to'].lower())}"
    return None


def short_span(secs: float) -> str:
    """'3d 5h', '14h', '2h 05m', '45m'."""
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, m = rem // 3600, rem % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h >= 3:
        return f"{h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"


def life_left(stuck: int | None, gen: int, flatline: int, gen_seconds: int, horizon: int) -> str:
    """How long a universe at `gen` has left if nobody comments: '8d 21h', '14h'."""
    if stuck is None:
        return f"{(horizon + flatline) * gen_seconds // 86400}d+"
    return short_span((stuck + flatline - gen) * gen_seconds)


def effect_suffix(effect: dict, flatline: int, gen_seconds: int) -> str:
    """' · 8 days → 2 days': the time it had left before and after this comment."""
    h = effect.get("horizon", 30000)
    b = life_left(effect["before"], effect["gen"], flatline, gen_seconds, h)
    a = life_left(effect["after"], effect["gen"], flatline, gen_seconds, h)
    return f" · {b} → {a}" if a != b else f" · still {a}"


def lines_from_log(entries: list[dict], flatline: int, gen_seconds: int, burst: int = 3) -> list[str]:
    """Pinned-comment lines for a whole log, oldest first (effects matched by gen
    and command). A run with more than `burst` comments gets one summary line."""
    effects = {}
    for e in entries:
        if e["type"] == "effect":
            effects.setdefault((e["gen"], e["command"]), e)
    out, run = [], []

    def flush():
        injs = [e for e in run if e["type"] == "injection"]
        effs = [effects[(e["gen"], e["command"])] for e in injs if (e["gen"], e["command"]) in effects]
        grouped = len(injs) > burst
        for e in run:
            if grouped and e["type"] == "injection":
                if e is injs[0]:
                    line = f"gen {e['gen']:,}: {len(injs)} comments"
                    if effs:
                        line += effect_suffix({**effs[0], "after": effs[-1]["after"]}, flatline, gen_seconds)
                    out.append(line)
                continue
            line = event_line(e)
            if line and e["type"] == "injection" and e["changed"] and (e["gen"], e["command"]) in effects:
                line += effect_suffix(effects[(e["gen"], e["command"])], flatline, gen_seconds)
            if line:
                out.append(line)
        run.clear()

    for e in entries:
        if e["type"] == "tick":
            flush()
        elif e["type"] in ("injection", "phase"):
            run.append(e)
    flush()
    return out


def pinned_status(u: Universe, gen_seconds: int = GEN_SECONDS, stuck_gen: int | None = None,
                  horizon_gens: int | None = None) -> str:
    if u.phase == DIED:
        return f"{EMOJI[DIED]} DIED at generation {u.died_gen:,}: its universe got stuck in a loop"
    if u.phase == FLATLINING:
        return (f"{EMOJI[FLATLINING]} FLATLINING · stuck in a loop: this video dies in "
                f"{coarse_left(u.gens_left() * gen_seconds)} unless a comment breaks it")
    if u.phase == CONTESTED:
        return (f"{EMOJI[CONTESTED]} CONTESTED · a comment broke the loop. If it's still changing in "
                f"{coarse_left(u.gens_left() * gen_seconds)}, it lives")
    if stuck_gen is not None:
        when = coarse_left((stuck_gen + u.flatline_gens - u.generation) * gen_seconds)
    elif horizon_gens is not None:
        when = f"{(horizon_gens + u.flatline_gens) * gen_seconds // 86400}+ days"
    else:
        return f"{EMOJI[ALIVE]} ALIVE"
    return f"{EMOJI[ALIVE]} ALIVE · this video dies in {when} if nobody comments"


def quiet_spot(grid, win_w: int = 24, win_h: int = 14) -> tuple[int, int]:
    """Top-left (x, y) of the emptiest win_w x win_h window (wrapping), for suggestions."""
    h, w = grid.shape
    padded = np.pad(grid, ((0, win_h), (0, win_w)), mode="wrap").astype(np.int32)
    c = padded.cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    sums = c[win_h:win_h + h, win_w:win_w + w] - c[:h, win_w:win_w + w] - c[win_h:win_h + h, :w] + c[:h, :w]
    y, x = np.unravel_index(np.argmin(sums), sums.shape)  # first minimum: deterministic
    return int((x + win_w // 2 - 3) % w), int((y + win_h // 2 - 1) % h)


def suggestion(u: Universe) -> str:
    x, y = quiet_spot(u.grid)
    pattern = ("acorn", "rpent", "glider")[(u.generation // 180) % 3]
    return f"{pattern} {x},{y}"


INTRO_MAIN = ("This video is alive: the thumbnail is its universe, the title counts down to its death, "
              "and every comment adds cells (as many as you like; replies count too).")
INTRO_EXTRA = ("This Short is alive: every comment here adds cells to the same universe as the linked video, "
               "whose thumbnail shows it live (as many as you like; replies count too). "
               "If the universe dies, both videos die.")


def pinned_text(status: str, recent: list[str], suggest: str | None = None, link: bool = True,
                main: bool = True) -> str:
    """The self-updating status comment. `main` is the video whose title and
    thumbnail show the universe; other videos (a Short) only feed it comments."""
    lines = [status, INTRO_MAIN if main else INTRO_EXTRA, "", "Latest moves (time it had left: before → after):"]
    lines += [f"• {r}" for r in recent] or ["• none yet. Be the first!"]
    lines.append("")
    if suggest:
        lines.append(f"Try one: {suggest} (a quiet corner right now) · or just say anything")
    lines.append(f"Rewind every move: {LIVE_ADDRESS}" if link else "Rewind every move: the address is at the end of the video.")
    return "\n".join(lines)
