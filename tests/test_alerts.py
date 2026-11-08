import alerts

LAUNCH = 1_794_114_283.0


def test_stuck_saved_and_died_alert_but_contested_and_reentry_do_not():
    ev = [{"type": "phase", "gen": 1855, "to": "FLATLINING", "reason": "cycle_detected", "deadline_gen": 2575},
          {"type": "phase", "gen": 1900, "to": "CONTESTED", "reason": "injection"},
          {"type": "phase", "gen": 1950, "to": "FLATLINING", "reason": "reentered_cycle", "deadline_gen": 2575},
          {"type": "phase", "gen": 2575, "to": "ALIVE", "reason": "survived_deadline"},
          {"type": "phase", "gen": 4000, "to": "DIED", "reason": "deadline"},
          {"type": "injection", "gen": 5, "command": "glider 1,1 se", "changed": True}]
    out = alerts.alerts_for(ev, LAUNCH, 120, "https://www.youtube.com/watch?v=x")
    assert [a["title"].split()[0] for a in out] == ["🔴", "🫀", "⚫"]
    assert "dies Wed Nov 11, 1:54 PM ET unless saved" in out[0]["title"]
    assert all("davidackerman.github.io/still-life" in a["body"] and "watch?v=x" in a["body"] for a in out)


def test_quiet_runs_have_no_alerts():
    assert alerts.alerts_for([{"type": "tick"}], LAUNCH, 120) == []
