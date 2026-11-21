"""turns: who goes next, by class and place, with short jobs first, and which free place each takes."""
from claude_ops import turns

CLASSES = ("urgent", "gate", "hand", "heavy")


def w(id, cls="hand", place=0, size=None):
    return {"id": id, "cls": cls, "place": place, "size": size}


def ids(ws):
    return [x["id"] for x in ws]


def test_a_higher_class_goes_first_whatever_its_place():
    ws = [w("old-hand", "hand", 10), w("new-gate", "gate", 500), w("heavy", "heavy", 1), w("urgent", "urgent", 900)]
    assert ids(turns.serve_order(ws, CLASSES, now=1000)) == ["urgent", "new-gate", "old-hand", "heavy"]


def test_a_short_waiter_goes_ahead_of_long_ones_that_have_not_waited_long():
    ws = [w("long", place=100, size=40), w("unsure", place=150), w("short", place=200, size=2)]
    assert ids(turns.serve_order(ws, CLASSES, now=300)) == ["short", "long", "unsure"]


def test_a_long_waiter_that_waited_long_enough_is_not_passed():
    ws = [w("long", place=0, size=40), w("short", place=200, size=2)]
    assert ids(turns.serve_order(ws, CLASSES, jump_s=600, now=600)) == ["long", "short"]
    assert ids(turns.serve_order(ws, CLASSES, jump_s=600, now=599)) == ["short", "long"]


def test_only_a_size_from_one_to_short_is_short():
    assert turns.is_short(w(1, size=3), 3) and turns.is_short(w(1, size=1), 3)
    for size in (0, 4, None, "2", True, 2.0):
        assert not turns.is_short(w(1, size=size), 3)
    assert not turns.is_short(w(1, size=2), 0)


def test_short_zero_orders_by_place_alone_and_ties_go_by_id():
    ws = [w("b", place=5, size=1), w("a", place=5, size=50), w("c", place=1, size=1)]
    assert ids(turns.serve_order(ws, CLASSES, short=0, now=10)) == ["c", "a", "b"]


def test_a_class_not_listed_comes_last():
    ws = [w("odd", "lunch", 1), w("hand", "hand", 9)]
    assert ids(turns.serve_order(ws, CLASSES, now=10)) == ["hand", "odd"]


def test_each_free_place_goes_to_the_next_served():
    ws = [w("a", place=1, size=40), w("b", place=2, size=40), w("c", place=3, size=40)]
    assert turns.assign(ws, ["2", "3"], CLASSES, now=10) == {"a": "2", "b": "3"}
    assert turns.assign(ws, [], CLASSES, now=10) == {}
    assert turns.assign([], ["1"], CLASSES, now=10) == {}


def test_the_short_place_goes_only_to_a_short_waiter_that_got_nothing_else():
    ws = [w("long", place=1, size=40), w("s1", place=2, size=2), w("s2", place=3, size=1)]
    # s1 is served first and takes the shared place, so the short place goes to s2
    assert turns.assign(ws, ["1", "short"], CLASSES, now=10) == {"s1": "1", "s2": "short"}
    assert turns.assign(ws, ["short"], CLASSES, now=10) == {"s1": "short"}
    assert turns.assign([w("long", size=40)], ["short"], CLASSES, now=10) == {}


def test_the_short_place_follows_class_then_place():
    ws = [w("hand-short", "hand", 1, size=2), w("gate-short", "gate", 9, size=2)]
    assert turns.assign(ws, ["short"], CLASSES, now=10) == {"gate-short": "short"}


def test_with_small_the_short_place_takes_a_small_waiter_before_an_older_bigger_short_one():
    ws = [w("fifteen", place=100, size=15), w("one", place=200, size=1)]
    assert turns.assign(ws, ["short"], CLASSES, short=20, small=3, now=300) == {"one": "short"}
    # small=0, the default, keeps the order by place
    assert turns.assign(ws, ["short"], CLASSES, short=20, now=300) == {"fifteen": "short"}


def test_with_small_each_size_goes_oldest_first():
    ws = [w("ten", place=100, size=10), w("two", place=150, size=2), w("one", place=200, size=1),
          w("four", place=50, size=4)]
    assert ids(turns.short_order(ws, CLASSES, short=20, small=3, now=300)) == ["two", "one", "four", "ten"]


def test_a_bigger_short_waiter_that_waited_jump_s_goes_next_whatever_smaller_one_comes_after():
    ws = [w("ten", place=0, size=10), w("one", place=550, size=1)]
    assert turns.assign(ws, ["short"], CLASSES, short=20, small=3, jump_s=600, now=599) == {"one": "short"}
    assert turns.assign(ws, ["short"], CLASSES, short=20, small=3, jump_s=600, now=600) == {"ten": "short"}
    ws.append(w("newer-one", place=650, size=1))
    assert turns.assign(ws, ["short"], CLASSES, short=20, small=3, jump_s=600, now=700) == {"ten": "short"}


def test_the_short_line_holds_no_waiter_that_cannot_say_its_size_and_keeps_class_first():
    ws = [w("unsure", place=1), w("hand-ten", "hand", 2, size=10), w("gate-ten", "gate", 9, size=10),
          w("hand-one", "hand", 5, size=1)]
    assert ids(turns.short_order(ws, CLASSES, short=20, small=3, now=10)) == ["gate-ten", "hand-one", "hand-ten"]
    assert turns.assign([w("unsure", place=1)], ["short"], CLASSES, short=20, small=3, now=10) == {}


def test_a_small_of_short_or_more_or_below_one_orders_by_place():
    ws = [w("ten", place=1, size=10), w("one", place=2, size=1)]
    for small in (0, -1, 20, 30):
        assert ids(turns.short_order(ws, CLASSES, short=20, small=small, now=10)) == ["ten", "one"]
