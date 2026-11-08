"""The death sequence: what happens after a universe dies.

Runs one stage after another, as far as it can get in one scheduled run, and
records where it got to in state.json ("death"), so a crashed or timed-out run
picks up where it stopped. It starts on the first run *after* the death (the
workflow installs ffmpeg when it sees a dead universe).

    verify      replay the whole log (replay.verify). On any mismatch: HALT and
                alert; nothing is uploaded or changed until a human looks.
    render      the obituary (obituary.py): the dead universe's life, its death,
                and the next universe starting. Credits the last comment that
                changed the board by the commenter's display name, looked up
                fresh by matching the HMAC (never stored).
                DRY_RUN: stop here ("dry_run_done"), log what would happen, alert.
    upload      the obituary, public (resumable). Recorded before anything else,
                and found again by a marker in its description if a run dies
                mid-way, so it is never uploaded twice.
    processing  wait until YouTube has processed it.
    launch      archive the dead universe (archive/universe-N/), start universe
                N+1 in state/ on the obituary video (bigger board), post its
                status comment for the owner to pin.
    retire      make the dead universe's videos private (the main video and every
                Short): fetch each one's status and send every writable field back.

Nothing here runs while the universe is alive.
"""

from __future__ import annotations

import hashlib
import os
import shutil

import life
from universe import Universe

VERIFY, RENDER, DRY, UPLOAD, PROCESSING, LAUNCH, HALTED = (
    "verify", "render", "dry_run_done", "upload", "processing", "launch", "halted")
OUT = os.path.join("out", "obituary.mp4")
ARCHIVE_ROOT = "archive"


# ---------------------------------------------------------------- the next universe
def lifetime(seed: str, shape: tuple[int, int], flatline: int, cap: int = 60000) -> int | None:
    """Generations until `seed` alone on a board of `shape` gets stuck (None: not within cap)."""
    return Universe(life.mask(life.centered(seed, shape), shape), flatline).forecast_stuck(cap)


def next_shape(shape: tuple[int, int], seed: str, flatline: int) -> tuple[int, int]:
    """PLACEHOLDER, to confirm with the owner before DRY_RUN goes off: the
    smallest 16:9 board (width a multiple of 16) bigger than `shape` on which
    `seed`, left alone, lasts longer than on `shape`. Bigger alone isn't enough:
    on a torus the acorn lasts 2.6 days at 128x72 but 1.9 at 160x90."""
    h, w = shape
    base = lifetime(seed, shape, flatline)
    width = (w // 16 + 1) * 16
    while width <= 1024:
        cand = (width * 9 // 16, width)
        t = lifetime(seed, cand, flatline)
        if t is None or (base is not None and t > base):
            return cand
        width += 16
    return (h * 2, w * 2)


# ---------------------------------------------------------------- helpers
def _event(stage: str, **kw) -> dict:
    return {"type": "death", "stage": stage, **kw}


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def marker(world, genesis_hash: str) -> str:
    """Put in the obituary's description so a later run can find the upload."""
    return f"still-life universe {world.universe_no} obituary {genesis_hash[:12]}"


def obituary_snippet(template: dict, world, fx: dict, shape: tuple[int, int], mark: str) -> dict:
    """Title/description for the obituary, built on the dead video's own snippet."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    n, (h, w) = world.universe_no, shape
    span = (f"{dt.datetime.fromtimestamp(fx['launch'], et):%b %-d} – "
            f"{dt.datetime.fromtimestamp(fx['died_at'], et):%b %-d, %Y}")
    desc = template.get("description", "")
    for old, new in (("0–127", f"0–{w - 1}"), ("0–71", f"0–{h - 1}"), ("128×72", f"{w}×{h}"),
                     ("128x72", f"{w}x{h}")):
        desc = desc.replace(old, new)
    head = (f"Universe #{n} died: it lived {span} ({fx['died_gen']:,} generations, {fx['comments']:,} "
            f"comments). This video now hosts universe #{n + 1}, bigger: {w}×{h} cells.")
    desc = f"{head}\n\n{desc}".strip() + f"\n\n({mark})"
    while len(desc.encode("utf-8")) > 5000 and "\n\n" in desc[:-len(mark) - 4]:
        desc = desc.rsplit("\n\n", 2)[0] + f"\n\n({mark})"  # trim from the end, keep the marker
    desc = desc.replace("<", "‹").replace(">", "›")
    out = {k: v for k, v in template.items() if k in ("tags", "categoryId", "defaultLanguage",
                                                       "defaultAudioLanguage")}
    out.setdefault("categoryId", "28")
    out.update(title=f"Universe #{n} died. Universe #{n + 1} lives here.", description=desc)
    return out


# ---------------------------------------------------------------- the sequence
def advance(world, store, yt, now: float, dry_run: bool, videos: list[str], key: bytes,
            render=None, out: str | None = None, archive_root: str | None = None):
    """Move the death sequence forward and save its progress. Returns (world,
    events) for reporting (already saved); the world is a new one once the next
    universe has launched. `videos` = [main, *extras] of the dead universe.
    Events are game data plus our own video IDs."""
    import obituary
    import replay

    render = render or (lambda sd, shape, path, credit: obituary.render(sd, shape, path, credit))
    out, archive_root = out or OUT, archive_root or ARCHIVE_ROOT
    d = world.death or {"stage": VERIFY}
    world.death = d
    events: list[dict] = []
    report: list[dict] = []
    genesis = _genesis(store)

    def done():
        store.save(world, events)
        return world, report + events

    while True:
        st = d["stage"]
        if st == HALTED:
            return done()
        if st == VERIFY:
            try:
                replay.verify(store.dir)
            except Exception as e:  # noqa: BLE001 - any mismatch halts
                d.update(stage=HALTED, error=str(e)[:500])
                events.append(_event(HALTED, error=str(e)[:500],
                                     alert=f"⚠️ Death sequence HALTED for universe #{world.universe_no}: "
                                           "the log failed verification",
                                     detail=f"replay.py says:\n\n    {e}\n\nNothing was uploaded or changed. "
                                            "Investigate, then set state.json death.stage back to \"verify\"."))
                return done()
            d["stage"] = RENDER
            events.append(_event("verified"))
        elif st in (RENDER, DRY):
            if st == DRY and dry_run:
                return done()  # done until the owner turns DRY_RUN off
            if not d.get("next_shape"):
                d["next_shape"] = list(next_shape(world.universe.grid.shape, world.seed, world.universe.flatline_gens))
            shape = tuple(d["next_shape"])
            credit = None
            if world.last_changer and hasattr(yt, "display_name_of"):
                try:
                    credit = yt.display_name_of(world.last_changer["comment"])
                except Exception:  # noqa: BLE001 - the credit is optional
                    credit = None
            render(store.dir, shape, out, credit)
            d["rendered"] = _sha(out)[:16]
            events.append(_event("rendered", next_shape=list(shape), credited=credit is not None))
            if dry_run:
                d["stage"] = DRY
                h, w = shape
                would = [f"upload the obituary publicly; universe #{world.universe_no + 1} ({w}×{h}) starts on it",
                         *[f"make video {v} private" for v in videos]]
                events.append(_event(DRY, would=would,
                                     alert=f"⚫ Dry run: universe #{world.universe_no}'s obituary is ready to watch",
                                     detail="Download it from this run's artifacts (Actions → the latest tick run → "
                                            "Artifacts → obituary).\n\nWith DRY_RUN off, the bot would:\n" +
                                            "\n".join(f"- {x}" for x in would) +
                                            "\n\nNothing was uploaded or changed. To go live, set DRY_RUN to "
                                            "\"false\" in .github/workflows/tick.yml."))
                return done()
            d["stage"] = UPLOAD
        elif st == UPLOAD:
            mark = marker(world, genesis["seed_hash"] if genesis else "")
            vid = yt.find_upload(mark)
            if not vid:
                if not os.path.exists(out) or (d.get("rendered") and _sha(out)[:16] != d["rendered"]):
                    d["stage"] = RENDER  # a new runner: render again, then upload
                    continue
                import obituary as ob
                from short import History
                h = History(store.dir)
                h.path = store.path("log.jsonl")
                template = yt.snippet_of(videos[0]) if videos else {}
                vid = yt.upload_video(out, obituary_snippet(template, world, ob.facts(h),
                                                            tuple(d["next_shape"]), mark))
            d.update(stage=PROCESSING, video=vid)
            events.append(_event("uploaded", video=vid))
            store.save(world, events)  # the ID is safe on disk before anything else can fail
            report += events
            events = []
        elif st == PROCESSING:
            s = yt.video_state(d["video"])
            bad = (not s.get("exists") or s.get("upload") in ("failed", "rejected", "deleted")
                   or s.get("processing") in ("failed", "terminated"))
            if bad:
                d.update(stage=HALTED, error=f"upload problem: {s}")
                events.append(_event(HALTED, error=str(s)[:300],
                                     alert=f"⚠️ Death sequence HALTED: the obituary upload {d['video']} failed",
                                     detail=f"YouTube says: {s}\n\nDelete it in Studio, then set state.json "
                                            "death.stage back to \"render\"."))
                return done()
            if s.get("upload") == "processed" or s.get("processing") == "succeeded":
                d["stage"] = LAUNCH
                events.append(_event("processed", video=d["video"]))
            else:
                return done()  # still processing: check again next run
        elif st == LAUNCH:
            new, out = _launch(world, store, yt, now, videos, key, archive_root, events)
            return new, report + events + out
        else:
            raise ValueError(f"unknown death stage {st!r}")


def _genesis(store) -> dict | None:
    path = store.path("log.jsonl")
    if not os.path.exists(path):
        return None
    import json
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


def _launch(world, store, yt, now, videos, key, archive_root, events):
    """Archive the dead universe and start the next one on the obituary video."""
    import tick
    from store import Store

    n, d = world.universe_no, world.death
    events.append(_event("launch", next_universe=n + 1, video=d["video"]))
    store.save(world, events)  # the dead universe's last words
    dest = os.path.join(archive_root, f"universe-{n}")
    if os.path.exists(dest):
        raise FileExistsError(f"{dest} already exists; refusing to overwrite an archive")
    shutil.copytree(store.dir, dest)
    from replay import verify
    verify(dest)  # the archive is a faithful copy before the live files go
    for name in ("log.jsonl", "state.json", "history.bin"):
        os.remove(store.path(name))

    shape = tuple(d["next_shape"])
    flatline_hours = world.universe.flatline_gens * world.gen_seconds / 3600
    new, genesis = tick.create_world(now, key, shape, seed=world.seed, gen_seconds=world.gen_seconds,
                                     hours=flatline_hours, launch=float(int(now)),
                                     extra={"universe": n + 1, "previous": {
                                         "archive": dest.replace(os.sep, "/"), "died_gen": world.universe.died_gen,
                                         "final_state_hash": world.universe.hash.hex()}})
    new.universe_no, new.video_id = n + 1, d["video"]
    new.retired = sorted(set(world.retired) | set(videos))
    new.retire_pending = [v for v in videos if v]
    new.quota = world.quota  # same project, same Pacific day
    fresh = Store(store.dir)
    fresh.create(new, genesis)
    out = []
    try:
        cid = yt.post_comment(new.video_id, "This video is alive. Latest changes to the universe will appear "
                                            "here in a few minutes.")
        new.pinned = {"0": {"id": cid, "text": None, "at": None}}
    except Exception as e:  # noqa: BLE001 - the owner can post it by hand
        out.append(_event("status_comment_failed", error=repr(e)[:300]))
    h, w = shape
    out.append(_event("launched", universe=n + 1, video=new.video_id, shape=[h, w],
                      alert=f"🌱 Universe #{n + 1} is live ({w}×{h}): pin its status comment",
                      detail=f"https://www.youtube.com/watch?v={new.video_id}\n\nOpen the video, find the "
                             "channel's comment \"This video is alive…\" and pin it (⋮ → Pin). The previous "
                             f"universe is archived in {dest}/."))
    fresh.save(new, out)
    return new, out  # `events` went into the archived log, `out` into the new one


def retire(world, yt, store) -> list[dict]:
    """Make the dead universe's videos private, one at a time; retried next run on failure."""
    events = []
    for vid in list(world.retire_pending):
        try:
            yt.set_private(vid)
        except Exception as e:  # noqa: BLE001
            events.append(_event("retire_failed", video=vid, error=repr(e)[:300]))
            break
        world.retire_pending.remove(vid)
        events.append(_event("retired", video=vid))
    if events and not world.retire_pending and any(e["stage"] == "retired" for e in events):
        events[-1]["alert"] = f"🔒 Universe #{world.universe_no - 1}'s videos are now private"
        events[-1]["detail"] = "The main video and every Short of the dead universe are private."
    return events
