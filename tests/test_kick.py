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


def test_it_starts_a_run_only_when_the_last_one_is_over_three_minutes_old(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http, calls = fake([run(2), run(12)])
    kick.main(NOW, http)
    assert [c[0] for c in calls] == ["GET"]
    http, calls = fake([run(4), run(12)])
    kick.main(NOW, http)
    assert calls[-1] == ("POST", "dispatches", {"ref": "main"})


def test_it_never_piles_up_runs_and_does_nothing_without_a_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http, calls = fake([run(9, "queued"), run(20)])
    kick.main(NOW, http)
    assert [c[0] for c in calls] == ["GET"]
    monkeypatch.delenv("GITHUB_TOKEN")
    http, calls = fake([run(60)])
    assert kick.main(NOW, http) == 0 and calls == []
