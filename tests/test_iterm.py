"""Telling chats apart in a shared iTerm2 window by tab color."""
from claude_ops import iterm

RED, YELLOW, PURPLE, ORANGE = iterm.PALETTE[:4]


def test_a_window_with_one_chat_is_left_alone():
    assert iterm.assign([[("claude-a", None)]]) == {}


def test_colors_the_user_set_are_kept_when_they_differ():
    assert iterm.assign([[("claude-a", RED), ("claude-b", YELLOW)]]) == {}


def test_uncolored_tabs_take_the_first_free_colors():
    assert iterm.assign([[("claude-a", None), ("claude-b", None)]]) == {"claude-a": RED, "claude-b": YELLOW}


def test_a_repeated_color_is_replaced_in_the_later_tab():
    assert iterm.assign([[("claude-a", RED), ("claude-b", RED)]]) == {"claude-b": YELLOW}


def test_a_chat_takes_back_its_old_color_when_it_is_free():
    assert iterm.assign([[("claude-a", RED), ("claude-b", None)]], {"claude-b": PURPLE}) == {"claude-b": PURPLE}
    assert iterm.assign([[("claude-a", RED), ("claude-b", None)]], {"claude-b": RED}) == {"claude-b": YELLOW}


def test_float_colors_compare_rounded_and_windows_are_separate():
    assert iterm.assign([[("claude-a", (234.4, 116.1, 103.6)), ("claude-b", RED)]]) == {"claude-b": YELLOW}
    two = iterm.assign([[("claude-a", None), ("claude-b", None)], [("claude-c", None), ("claude-d", None)]])
    assert two == {"claude-a": RED, "claude-b": YELLOW, "claude-c": RED, "claude-d": YELLOW}


def test_more_chats_than_colors_leaves_the_rest():
    tabs = [(f"claude-{i}", None) for i in range(3)]
    assert iterm.assign([tabs], palette=(RED, YELLOW)) == {"claude-0": RED, "claude-1": YELLOW}


def test_tmux_clients_are_read_by_terminal(monkeypatch):
    class R:
        stdout = "/dev/ttys001 claude-a\n/dev/ttys002 other\n"
    monkeypatch.setattr(iterm.subprocess, "run", lambda *a, **k: R())
    assert iterm.tmux_by_tty() == {"/dev/ttys001": "claude-a", "/dev/ttys002": "other"}
