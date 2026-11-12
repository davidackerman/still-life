"""deploy/kick.py: start the bot's run when GitHub's schedule is late."""
import datetime as dt
import importlib.util
import os

spec = importlib.util.spec_from_file_location("kick", os.path.join(os.path.dirname(__file__), "..", "deploy", "kick.py"))
kick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kick)

NOW = dt.datetime(2026, 11, 11, 2, 30, tzinfo=dt.timezone.utc)


def run(minutes_ago, status="completed"):
    return {"status": status, "created_at": (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def fake(runs):
    calls = []

    def http(method, url, token, body=None):
        calls.append((method, url.rsplit("/", 1)[-1], body))
        return {"workflow_runs": runs} if method == "GET" else None
    return http, calls


def log_with_comment(tmp_path, minutes_ago):
    import json
    p = tmp_path / "log.jsonl"
    lines = [{"type": "tick", "now": (NOW - dt.timedelta(minutes=m)).timestamp(), "comments_new": n}
             for m, n in ((minutes_ago, 2), (1, 0))]  # the newest run read nothing new
    p.write_text("\n".join(json.dumps(x) for x in [{"type": "genesis"}] + lines) + "\n")
    return str(p)


def test_runs_come_every_minute_while_people_play_and_every_five_when_quiet(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    for commented, last_run, starts in ((4, 1.2, True), (4, 0.5, False),     # active: every minute
                                        (30, 2, False), (30, 3.2, True),     # this hour: every 3
                                        (120, 4, False), (120, 5.2, True)):  # quiet: every 5
        http, calls = fake([run(last_run), run(12)])
        kick.main(NOW, http, log=log_with_comment(tmp_path, commented))
        assert (calls[-1] == ("POST", "dispatches", {"ref": "main"})) == starts, (commented, last_run)
    http, calls = fake([run(5.2)])
    kick.main(NOW, http, log=str(tmp_path / "missing.jsonl"))  # no log to read: every 5
    assert calls[-1][0] == "POST"


def test_it_never_piles_up_runs_and_does_nothing_without_a_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http, calls = fake([run(9, "queued"), run(20)])
    kick.main(NOW, http, log="/nonexistent")
    assert [c[0] for c in calls] == ["GET"]
    monkeypatch.delenv("GITHUB_TOKEN")
    http, calls = fake([run(60)])
    assert kick.main(NOW, http) == 0 and calls == []
