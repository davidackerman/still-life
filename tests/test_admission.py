from admission import Admission, Comment, admit, take_batch


def C(cid, author, minute, text="block 1,1"):
    hh, mm = divmod(minute, 60)
    return Comment(cid, author, f"2026-11-08T{hh:02d}:{mm:02d}:00Z", text)


def test_dedupe_by_comment_id_forever():
    s = Admission()
    admit(s, [C("a", "u1", 0)])
    admit(s, [C("a", "u1", 0)])
    admit(s, [C("a", "u1", 0, "edited text")])
    assert [q["comment_id"] for q in s.queue] == ["a"]


def test_processing_order_is_published_at_then_id():
    s = Admission()
    admit(s, [C("z", "u1", 5), C("b", "u2", 1), C("a", "u3", 1)])
    assert [q["comment_id"] for q in s.queue] == ["a", "b", "z"]


def test_one_injection_per_author_per_hour():
    s = Admission()
    ev = admit(s, [C("a", "u1", 0), C("b", "u1", 30), C("c", "u1", 59), C("d", "u1", 60)])
    assert [q["comment_id"] for q in s.queue] == ["a", "d"]
    assert [(e["comment_id"], e["reason"]) for e in ev] == [("b", "author_rate_limit"), ("c", "author_rate_limit")]


def test_cooldown_counts_from_the_last_accepted_comment():
    s = Admission()
    admit(s, [C("a", "u1", 0), C("b", "u1", 50), C("c", "u1", 61)])
    assert [q["comment_id"] for q in s.queue] == ["a", "c"]


def test_rate_limit_spans_runs():
    s = Admission()
    admit(s, [C("a", "u1", 0)])
    ev = admit(s, [C("b", "u1", 10)])
    assert ev[0]["reason"] == "author_rate_limit" and len(s.queue) == 1


def test_different_authors_are_independent():
    s = Admission()
    admit(s, [C("a", "u1", 0), C("b", "u2", 0)])
    assert len(s.queue) == 2


def test_after_death_comments_are_rejected_but_marked_processed():
    s = Admission()
    ev = admit(s, [C("a", "u1", 0)], dead=True)
    assert ev[0]["reason"] == "universe_dead" and not s.queue and "a" in s.processed_ids


def test_global_cap_queues_the_excess():
    s = Admission()
    admit(s, [C(f"c{i:02d}", f"u{i}", 0) for i in range(25)])
    first, second = take_batch(s), take_batch(s)
    assert len(first) == 20 and len(second) == 5
    assert [q["comment_id"] for q in first + second] == [f"c{i:02d}" for i in range(25)]


def test_queued_injection_records_parsed_cells():
    s = Admission()
    admit(s, [C("a", "u1", 0, "glider 3,4 ne")])
    q = s.queue[0]
    assert q["command"] == "glider 3,4 ne" and len(q["cells"]) == 5 and q["text"] == "glider 3,4 ne"


def test_roundtrip_dict():
    s = Admission()
    admit(s, [C(f"c{i:02d}", f"u{i}", i) for i in range(25)])
    take_batch(s)
    s2 = Admission.from_dict(s.to_dict())
    assert s2.processed_ids == s.processed_ids and s2.last_accepted == s.last_accepted and s2.queue == s.queue
