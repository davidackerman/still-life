"""The death sequence (death.py) through tick.run, with a fake YouTube and clock."""
import json
import os

import pytest

import death
import replay
import tick
from admission import Comment

KEY = b"test-state-key-0123456789"
LAUNCH = 1_800_000_000.0


class DeathYT:
    """Fake YouTube with the calls the death sequence makes."""

    def __init__(self):
        self.videos = ["MAIN", "SHORT"]
        self.comments, self.uploads, self.private, self.posted = [], [], [], []
        self.upload_status, self.existing = "processed", {}

    def fetch_comments(self, since, watch=(), now=None):
        return list(self.comments)

    def set_title(self, t, grid_text=""):
        pass

    def set_thumbnail(self, path):
        pass

    def find_upload(self, marker):
        return self.existing.get(marker)

    def upload_video(self, path, snippet):
        assert os.path.getsize(path) > 0
        self.uploads.append(snippet)
        vid = f"OBIT{len(self.uploads)}"
        self.existing[snippet["description"].rsplit("(", 1)[1].rstrip(")")] = vid
        return vid

    def video_state(self, vid):
        return {"exists": True, "upload": self.upload_status, "processing": None, "privacy": "public"}

    def set_private(self, vid):
        self.private.append(vid)

    def snippet_of(self, vid):
        return {"title": "old", "description": "Comment glider x,y (x 0–127, y 0–71).", "tags": ["life"],
                "categoryId": "28", "defaultLanguage": "en"}

    def post_comment(self, vid, text):
        self.posted.append((vid, text))
        return "UgSTATUS"

    def display_name_of(self, keyed_comment):
        return "@saver"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SEED", "blinker")  # loops at once
    monkeypatch.setenv("FLATLINE_HOURS", "1")  # ... and dies an hour later
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    monkeypatch.delenv("GEN_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    return str(tmp_path / "state")


def until_dead(d, yt, step=600):
    t = 0
    while True:
        ev = tick.run(d, LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
        if any(e["type"] == "phase" and e["to"] == "DIED" for e in ev):
            return t
        t += step


def state(d):
    with open(os.path.join(d, "state.json")) as f:
        return json.load(f)


def test_dry_run_renders_then_stops_and_changes_nothing(env):
    yt = DeathYT()
    t = until_dead(env, yt)
    assert state(env)["death"] is None  # it starts on the run after the death
    ev = tick.run(env, LAUNCH + t + 300, lambda w: yt, KEY, thumb_path=None, dry_run=True)
    stages = [e["stage"] for e in ev if e["type"] == "death"]
    assert stages == ["verified", "rendered", "dry_run_done"]
    dry = [e for e in ev if e.get("stage") == "dry_run_done"][0]
    assert dry["alert"].startswith("⚫ Dry run") and "make video MAIN private" in dry["would"]
    assert yt.uploads == [] and yt.private == [] and yt.posted == []
    again = tick.run(env, LAUNCH + t + 600, lambda w: yt, KEY, thumb_path=None, dry_run=True)
    assert not [e for e in again if e["type"] == "death"]  # nothing more until DRY_RUN is off
    replay.verify(env)  # death events don't disturb the log


def test_live_death_uploads_launches_the_next_universe_and_retires_the_old_videos(env, tmp_path):
    yt = DeathYT()
    t = until_dead(env, yt)
    ev = tick.run(env, LAUNCH + t + 300, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    stages = [e["stage"] for e in ev if e["type"] == "death"]
    assert stages == ["verified", "rendered", "uploaded", "processed", "launch", "launched", "retired", "retired"]
    assert len(yt.uploads) == 1 and yt.uploads[0]["title"] == "Universe #1 died. Universe #2 lives here."
    desc = yt.uploads[0]["description"]
    assert "x 0–255, y 0–143" in desc and "still-life universe 1 obituary" in desc
    assert yt.private == ["MAIN", "SHORT"] and yt.posted[0][0] == "OBIT1"
    # the dead universe is archived and still verifies; state/ now holds universe #2
    replay.verify(str(tmp_path / "archive" / "universe-1"))
    s = state(env)
    assert s["config"]["universe"] == 2 and s["config"]["video_id"] == "OBIT1"
    assert s["config"]["retired"] == ["MAIN", "SHORT"] and s["retire_pending"] == []
    assert s["universe"]["shape"] == [144, 256] and s["universe"]["generation"] == 0
    assert s["display"]["pinned"]["0"]["id"] == "UgSTATUS"
    with open(os.path.join(env, "log.jsonl")) as f:
        g = json.loads(f.readline())
    assert g["universe"] == 2 and g["previous"]["archive"].endswith("archive/universe-1")
    alerts = [e["alert"] for e in ev if e.get("alert")]
    assert alerts[0].startswith("🌱 Universe #2 is live") and alerts[1].startswith("🔒")
    replay.verify(env)
    # universe #2 just lives on
    tick.run(env, LAUNCH + t + 900, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    assert state(env)["universe"]["generation"] > 0


def test_it_waits_while_youtube_processes_the_upload(env):
    yt = DeathYT()
    yt.upload_status = "uploaded"
    t = until_dead(env, yt)
    tick.run(env, LAUNCH + t + 300, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    assert state(env)["death"]["stage"] == "processing" and yt.private == []
    yt.upload_status = "processed"
    tick.run(env, LAUNCH + t + 600, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    assert state(env)["config"]["universe"] == 2 and len(yt.uploads) == 1


def test_an_upload_is_never_repeated(env, monkeypatch):
    yt = DeathYT()
    t = until_dead(env, yt)
    real = death._launch

    def crash(*a, **kw):
        raise RuntimeError("runner died after the upload")
    monkeypatch.setattr(death, "_launch", crash)
    tick.run(env, LAUNCH + t + 300, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    assert state(env)["death"]["stage"] == "launch" and len(yt.uploads) == 1
    monkeypatch.setattr(death, "_launch", real)
    tick.run(env, LAUNCH + t + 600, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    assert len(yt.uploads) == 1 and state(env)["config"]["video_id"] == "OBIT1"


def test_a_crash_between_upload_and_saving_finds_the_upload_again(env):
    yt = DeathYT()
    t = until_dead(env, yt)
    yt.existing[f"still-life universe 1 obituary "] = None  # nothing yet
    # pretend an earlier run uploaded but never recorded it
    g = json.loads(open(os.path.join(env, "log.jsonl")).readline())
    yt.existing[f"still-life universe 1 obituary {g['seed_hash'][:12]}"] = "OBIT-EARLIER"
    tick.run(env, LAUNCH + t + 300, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    assert yt.uploads == [] and state(env)["config"]["video_id"] == "OBIT-EARLIER"


def test_a_log_that_fails_verification_halts_everything(env):
    yt = DeathYT()
    t = until_dead(env, yt)
    path = os.path.join(env, "log.jsonl")
    lines = open(path).read().splitlines()
    i = next(i for i, ln in enumerate(lines) if json.loads(ln)["type"] == "tick" and json.loads(ln)["gen_end"])
    e = json.loads(lines[i])
    e["state_hash"] = "0" * 64  # tamper with a run's recorded board
    lines[i] = json.dumps(e)
    open(path, "w").write("\n".join(lines) + "\n")
    ev = tick.run(env, LAUNCH + t + 300, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    halted = [e for e in ev if e.get("stage") == "halted"]
    assert halted and halted[0]["alert"].startswith("⚠️ Death sequence HALTED")
    assert yt.uploads == [] and yt.private == []


def test_errors_are_alerted_once_and_retried(env, monkeypatch):
    yt = DeathYT()
    t = until_dead(env, yt)
    import obituary

    def broken(*a, **kw):
        raise RuntimeError("ffmpeg missing")
    monkeypatch.setattr(obituary, "render", broken)
    ev1 = tick.run(env, LAUNCH + t + 300, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    ev2 = tick.run(env, LAUNCH + t + 600, lambda w: yt, KEY, thumb_path=None, dry_run=False)
    assert [e.get("alert", "")[:2] for e in ev1 if "error" in e] == ["⚠️"[:2]]
    assert all("alert" not in e for e in ev2 if "error" in e)
    assert yt.uploads == []


def test_next_shape_is_bigger_and_lasts_longer():
    assert death.next_shape((72, 128), "acorn", 720) == (99, 176)
    assert death.lifetime("acorn", (99, 176), 720) > death.lifetime("acorn", (72, 128), 720)
