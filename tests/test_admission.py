import json

from admission import RETENTION_S, Admission, Comment, admit, keyed, parse_time, take_batch

KEY = b"test-key-0123456789"
T0 = parse_time("2026-11-08T00:00:00Z")


def C(cid, author, minute, text="block 1,1"):
    hh, mm = divmod(minute, 60)
    return Comment(cid, author, f"2026-11-08T{hh:02d}:{mm:02d}:00Z", text)


def run(s, comments, minute_now=120, dead=False, cooldown_s=3600):
    """The optional per-author cooldown (off in production) is on here, to test it."""
    return admit(s, comments, T0 + minute_now * 60, KEY, dead=dead, cooldown_s=cooldown_s)


def queued(s):
    return [q["comment"] for q in s.queue]


def k(cid):
    return keyed(KEY, "comment", cid)


def test_dedupe_by_comment_id():
    s = Admission()
    run(s, [C("a", "u1", 0)])
    run(s, [C("a", "u1", 0)])
    run(s, [C("a", "u1", 0, "edited text")])
    assert queued(s) == [k("a")]


def test_processing_order_is_published_at_then_id():
    s = Admission()
    run(s, [C("z", "u1", 5), C("b", "u2", 1), C("a", "u3", 1)])
    assert queued(s) == [k("a"), k("b"), k("z")]


def test_one_injection_per_author_per_hour():
    s = Admission()
    c = run(s, [C("a", "u1", 0), C("b", "u1", 30), C("c", "u1", 59), C("d", "u1", 60)])
    assert queued(s) == [k("a"), k("d")]
    assert c == {"new": 4, "accepted": 2, "rejected": {"author_rate_limit": 2}}


def test_cooldown_counts_from_the_last_accepted_comment():
    s = Admission()
    run(s, [C("a", "u1", 0), C("b", "u1", 50), C("c", "u1", 61)])
    assert queued(s) == [k("a"), k("c")]


def test_rate_limit_spans_runs_and_late_arrivals():
    s = Admission()
    run(s, [C("a", "u1", 30)])
    assert run(s, [C("b", "u1", 40)])["rejected"] == {"author_rate_limit": 1}
    # published *before* the accepted one but fetched later (e.g. approved after review)
    assert run(s, [C("c", "u1", 0)])["rejected"] == {"author_rate_limit": 1}
    assert len(s.queue) == 1


def test_different_authors_are_independent():
    s = Admission()
    run(s, [C("a", "u1", 0), C("b", "u2", 0)])
    assert len(s.queue) == 2


def test_after_death_comments_are_rejected_but_marked_seen():
    s = Admission()
    c = run(s, [C("a", "u1", 0)], dead=True)
    assert c["rejected"] == {"universe_dead": 1} and not s.queue and k("a") in s.seen


def test_comments_outside_the_48h_window_are_ignored():
    s = Admission()
    c = admit(s, [C("old", "u1", 0)], T0 + 49 * 3600, KEY)
    assert c["new"] == 0 and not s.queue and not s.seen


def test_global_cap_queues_the_excess():
    s = Admission()
    run(s, [C(f"c{i:02d}", f"u{i}", 0) for i in range(25)])
    first, second = take_batch(s), take_batch(s)
    assert len(first) == 20 and len(second) == 5
    assert [q["comment"] for q in first + second] == [k(f"c{i:02d}") for i in range(25)]


def test_queued_injection_has_cells_but_no_raw_comment_data():
    s = Admission()
    run(s, [C("Ugx-secret-id", "UC-secret-author", 0, "glider 3,4 ne please")])
    q = s.queue[0]
    assert q["command"] == "glider 3,4 ne" and len(q["cells"]) == 5
    blob = json.dumps(s.to_dict())
    for raw in ("Ugx-secret-id", "UC-secret-author", "please"):
        assert raw not in blob


def test_keyed_ids_depend_on_the_key():
    assert keyed(b"k1" * 8, "comment", "a") != keyed(b"k2" * 8, "comment", "a")
    assert keyed(KEY, "comment", "a") != keyed(KEY, "author", "a")


def test_old_hashes_are_pruned():
    s = Admission()
    run(s, [C("a", "u1", 0)])
    admit(s, [], T0 + RETENTION_S + 3600, KEY)
    assert not s.seen and not s.last_accepted


def test_roundtrip_dict():
    s = Admission()
    run(s, [C(f"c{i:02d}", f"u{i}", i) for i in range(25)])
    take_batch(s)
    s2 = Admission.from_dict(json.loads(json.dumps(s.to_dict())))
    assert s2.seen == s.seen and s2.last_accepted == s.last_accepted and s2.queue == s.queue


def test_by_default_every_comment_counts():
    s = Admission()
    c = admit(s, [C(f"c{i}", "u1", i) for i in range(24)], T0 + 120 * 60, KEY)
    assert c["accepted"] == 24 and c["rejected"] == {}


def test_a_flood_takes_turns_with_everyone_else():
    s = Admission()
    flood = [C(f"f{i:02d}", "spammer", 0) for i in range(30)]
    others = [C("x", "u2", 1), C("y", "u3", 2)]
    admit(s, flood + others, T0 + 120 * 60, KEY)
    first = take_batch(s)
    got = [q["comment"] for q in first]
    assert len(first) == 20 and k("x") in got and k("y") in got  # not stuck behind 30 spam comments
    assert [q["comment"] for q in first if q["comment"] not in (k("x"), k("y"))] == [k(f"f{i:02d}") for i in range(18)]
    assert len(take_batch(s)) == 12 and not s.queue
