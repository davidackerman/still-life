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


def write_log(tmp_path, comment_ago=None, chat_ago=None):
    import json
    ticks = [{"type": "tick", "now": (NOW - dt.timedelta(minutes=m)).timestamp(), "comments_new": n, "chat_new": c}
             for m, n, c in ((comment_ago, 2, 0), (chat_ago, 1, 1)) if m is not None]
    ticks.sort(key=lambda e: e["now"])
    ticks.append({"type": "tick", "now": (NOW - dt.timedelta(minutes=1)).timestamp(), "comments_new": 0, "chat_new": 0})
    p = tmp_path / "log.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [{"type": "genesis"}] + ticks) + "\n")
    return str(p)


def write_state(tmp_path, used):
    import json
    from zoneinfo import ZoneInfo
    day = NOW.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"quota": {"day": day, "used": used}}))
    return str(p)


def starts(tmp_path, last_run, used=0, **ago):
    http, calls = fake([run(last_run), run(12)])
    kick.main(NOW, http, log=write_log(tmp_path, **ago), state=write_state(tmp_path, used))
    return calls[-1] == ("POST", "dispatches", {"ref": "main"})


def test_runs_come_every_minute_while_the_chat_talks_and_every_five_when_quiet(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert starts(tmp_path, 1.2, chat_ago=4) and not starts(tmp_path, 0.5, chat_ago=4)  # chatting: every minute
    assert not starts(tmp_path, 2, comment_ago=4) and starts(tmp_path, 3.2, comment_ago=4)  # video comments: every 3
    assert not starts(tmp_path, 2, chat_ago=30) and starts(tmp_path, 3.2, chat_ago=30)  # this hour: every 3
    assert not starts(tmp_path, 4, comment_ago=120) and starts(tmp_path, 5.2, comment_ago=120)  # quiet: every 5
    http, calls = fake([run(5.2)])
    kick.main(NOW, http, log=str(tmp_path / "missing.jsonl"), state=str(tmp_path / "missing.json"))
    assert calls[-1][0] == "POST"  # no log to read: every 5


def test_it_backs_off_as_the_days_quota_fills(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    limit = kick.write_limit(NOW)  # 6:30 PM Pacific: 9,700 less 5.5 hours of reads
    assert limit == 9700 - 120 * 5.5
    assert starts(tmp_path, 1.2, used=limit - 2000, chat_ago=4)
    assert not starts(tmp_path, 1.2, used=limit - 1400, chat_ago=4) and starts(tmp_path, 3.2, used=limit - 1400, chat_ago=4)
    assert not starts(tmp_path, 3.2, used=limit - 400, chat_ago=4) and starts(tmp_path, 5.2, used=limit - 400, chat_ago=4)


def test_it_never_piles_up_runs_and_does_nothing_without_a_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http, calls = fake([run(9, "queued"), run(20)])
    kick.main(NOW, http, log="/nonexistent", state="/nonexistent")
    assert [c[0] for c in calls] == ["GET"]
    monkeypatch.delenv("GITHUB_TOKEN")
    http, calls = fake([run(60)])
    assert kick.main(NOW, http) == 0 and calls == []
