"""tick.run + store + replay.py end to end, with a fake YouTube and a fake clock."""
import json
import os

import pytest

import replay
import tick
from admission import Comment

KEY = b"test-state-key-0123456789"
LAUNCH = 1_800_000_000.0


class FakeYT:
    def __init__(self):
        self.comments, self.titles, self.thumbs = [], [], 0

    def fetch_comments(self, since, watch=(), now=None):
        return list(self.comments)

    def set_title(self, t, grid_text=""):
        self.titles.append(t)

    def set_thumbnail(self, path):
        self.thumbs += 1


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(monkeypatch):
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))


def live(state_dir, yt, until_s, step_s=300, comments_at=()):
    """Run tick every step_s seconds; post each (t, text) comment shortly before t."""
    pending = sorted(comments_at)
    for i, t in enumerate(range(0, until_s, step_s)):
        while pending and pending[0][0] <= t:
            ct, text = pending.pop(0)
            yt.comments.append(Comment(f"Ugx{ct}", f"UC{ct % 7}", iso(LAUNCH + ct - 30), text))
        tick.run(state_dir, LAUNCH + t, lambda w: yt, KEY, thumb_path=None)


def log_entries(d):
    return [json.loads(x) for x in open(os.path.join(d, "log.jsonl"))]


def test_replay_roundtrip_through_flatline_contest_and_death(tmp_path, env):
    d = str(tmp_path / "state")
    yt = FakeYT()
    # The acorn alone flatlines at gen 1855 (61.8 h). Comments then contest it.
    live(d, yt, 8 * 86400, comments_at=[(63 * 3600, "rpent 100,10"), (64 * 3600, "cells 1,1 2,2 3,3"),
                                        (66 * 3600, "glider 10,10")])
    u = replay.verify(d)
    phases = [e["to"] for e in log_entries(d) if e["type"] == "phase"]
    assert phases[0] == "FLATLINING" and "CONTESTED" in phases and phases[-1] == u.phase


def test_an_early_comment_can_kill_it_and_later_ones_are_refused(tmp_path, env):
    d = str(tmp_path / "state")
    yt = FakeYT()
    live(d, yt, 4 * 86400, comments_at=[(3600, "glider 10,10"), (7200, "hello"),
                                        (63 * 3600, "rpent 100,10"), (64 * 3600, "block 1,1")])
    u = replay.verify(d)
    entries = log_entries(d)
    assert u.phase == "DIED" and u.died_gen < 2575  # sooner than the undisturbed acorn
    assert sum(e["type"] == "injection" for e in entries) == 2
    assert sum(e.get("rejected", {}).get("universe_dead", 0) for e in entries if e["type"] == "tick") == 2


def test_log_contains_no_raw_comment_data(tmp_path, env):
    d = str(tmp_path / "state")
    yt = FakeYT()
    live(d, yt, 3600, comments_at=[(600, "glider 10,10 my secret words")])
    blob = open(os.path.join(d, "log.jsonl")).read() + open(os.path.join(d, "state.json")).read()
    assert "Ugx600" not in blob and "UC5" not in blob and "secret words" not in blob
    assert "glider 10,10 se" in blob  # the parsed command is game data


def test_running_twice_at_the_same_time_changes_nothing(tmp_path, env):
    d = str(tmp_path / "state")
    yt = FakeYT()
    live(d, yt, 3600, comments_at=[(600, "block 5,5")])
    files = {n: open(os.path.join(d, n), "rb").read() for n in ("log.jsonl", "state.json", "history.bin")}
    yt.comments.append(Comment("Ugx-late", "UC9", iso(LAUNCH + 3300 - 60), "acorn 50,50"))
    tick.run(d, LAUNCH + 3300, lambda w: yt, KEY, thumb_path=None)  # applies the new comment
    after_one = {n: open(os.path.join(d, n), "rb").read() for n in files}
    assert after_one != files
    tick.run(d, LAUNCH + 3300, lambda w: yt, KEY, thumb_path=None)  # same moment again
    after_two = {n: open(os.path.join(d, n), "rb").read() for n in files}
    assert after_two == after_one
    replay.verify(d)


def test_tampered_log_fails_replay(tmp_path, env):
    d = str(tmp_path / "state")
    live(d, FakeYT(), 3600, comments_at=[(600, "block 5,5")])
    p = os.path.join(d, "log.jsonl")
    lines = open(p).read().splitlines()
    i = next(i for i, ln in enumerate(lines) if '"injection"' in ln)
    lines[i] = lines[i].replace('"block 5,5"', '"block 6,6"')
    open(p, "w").write("\n".join(lines) + "\n")
    with pytest.raises(replay.ReplayError, match="hash chain"):
        replay.verify(d)


def test_rehashed_forgery_still_fails_replay(tmp_path, env):
    """Rewrite an injection's cells and re-hash the whole chain: the chain checks
    out, but the replayed life no longer matches the logged phases/states."""
    import store
    d = str(tmp_path / "state")
    live(d, FakeYT(), 7200, comments_at=[(600, "block 5,5")])
    p = os.path.join(d, "log.jsonl")
    entries = [json.loads(x) for x in open(p)]
    for e in entries:
        if e["type"] == "injection":
            e["cells"] = [[60, 30], [61, 30], [60, 31], [61, 31]]
    prev = store.GENESIS_PREV
    for e in entries:
        e["prev_hash"] = prev
        e["hash"] = store.line_hash(e)
        prev = e["hash"]
    open(p, "w").write("".join(json.dumps(e) + "\n" for e in entries))
    st = json.load(open(os.path.join(d, "state.json")))
    st["log"]["head"] = prev
    json.dump(st, open(os.path.join(d, "state.json"), "w"))
    with pytest.raises(replay.ReplayError):
        replay.verify(d)


def test_tampered_state_fails_replay(tmp_path, env):
    d = str(tmp_path / "state")
    live(d, FakeYT(), 3600)
    p = os.path.join(d, "state.json")
    st = json.load(open(p))
    st["universe"]["phase"] = "DIED"
    json.dump(st, open(p, "w"))
    with pytest.raises(replay.ReplayError, match="phase"):
        replay.verify(d)


def test_store_refuses_a_log_that_does_not_match_state(tmp_path, env):
    d = str(tmp_path / "state")
    live(d, FakeYT(), 1800)
    with open(os.path.join(d, "log.jsonl"), "a") as f:
        f.write('{"type":"bogus"}\n')
    with pytest.raises(ValueError, match="does not end where"):
        tick.run(d, LAUNCH + 1800, lambda w: FakeYT(), KEY, thumb_path=None)


def test_genesis_fixes_the_config(tmp_path, env, monkeypatch):
    d = str(tmp_path / "state")
    live(d, FakeYT(), 600)
    monkeypatch.setenv("GEN_SECONDS", "30")  # ignored after creation
    tick.run(d, LAUNCH + 1200, lambda w: FakeYT(), KEY, thumb_path=None)
    assert json.load(open(os.path.join(d, "state.json")))["universe"]["generation"] == 10


def test_recent_events_are_backfilled_from_the_log(tmp_path, env):
    d = str(tmp_path / "state")
    live(d, FakeYT(), 3600, comments_at=[(600, "block 5,5")])
    p = os.path.join(d, "state.json")
    st = json.load(open(p))
    st["display"].pop("recent", None)  # as written by the bot before this feature
    json.dump(st, open(p, "w"))
    tick.run(d, LAUNCH + 3600, lambda w: FakeYT(), KEY, thumb_path=None)
    recent = json.load(open(p))["display"]["recent"]
    assert len(recent) == 1 and recent[0].startswith("gen 4: block 5,5 · ")  # posted at 570 s: gen 4


def test_painted_moves_are_logged_with_their_color_and_replay(tmp_path, env):
    d = str(tmp_path / "state")
    yt = FakeYT()
    live(d, yt, 2 * 3600, comments_at=[(600, "glider 10,10 BLUE my secret words"), (1200, "hi pink")])
    replay.verify(d)  # the color rides along; the game is the same
    inj = [e for e in log_entries(d) if e["type"] == "injection"]
    assert [e.get("color") for e in inj] == ["blue", "pink"]
    assert inj[0]["command"] == "glider 10,10 se"  # the move itself, unchanged
    blob = open(os.path.join(d, "log.jsonl")).read()
    assert "secret words" not in blob and "hi pink" not in blob  # only the parsed color, never the text
    import numpy as np
    import short
    h = short.History(d)
    u = h.fresh()
    h.goto(u, h.now_gen)
    assert {int(v) for v in np.unique(u.paint) if v} <= {4, 6}  # blue and pink (whatever survived)
    assert not (u.paint > 0)[u.grid == 0].any()  # paint only on live cells
