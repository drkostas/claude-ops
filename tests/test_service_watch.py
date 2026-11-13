"""The decision to restart a service. Nothing here starts, signals or connects to anything."""
import subprocess

import pytest

from claude_ops import service_check as sc
from claude_ops import service_watch as w

DOWN, UP = False, True


def rd(*pairs):
    return [w.Reading(t, a) for t, a in pairs]


def test_an_answering_service_is_never_restarted():
    assert w.decide(rd((0, DOWN), (60, DOWN), (120, UP)), 120, "on").action == w.NONE


def test_no_reading_and_nothing_to_ask_do_nothing():
    assert w.decide([], 0, "on").action == w.NONE
    assert w.decide(rd((0, DOWN), (60, None)), 60, "on").action == w.NONE


def test_one_down_reading_is_a_recheck_not_a_restart():
    d = w.decide(rd((100, DOWN)), 100, "on")
    assert d.action == w.RECHECK and d.wait_until == 160


def test_two_down_readings_too_close_are_still_a_recheck():
    assert w.decide(rd((100, DOWN), (130, DOWN)), 130, "on").action == w.RECHECK


def test_two_down_readings_a_minute_apart_restart_a_switched_on_service():
    d = w.decide(rd((100, DOWN), (160, DOWN)), 160, "on")
    assert d.action == w.RESTART


def test_a_down_between_two_answers_does_not_count():
    assert w.decide(rd((0, DOWN), (60, UP), (120, DOWN)), 120, "on").action == w.RECHECK


def test_a_switched_off_service_is_never_restarted():
    assert w.decide(rd((0, DOWN), (60, DOWN)), 60, "off").action == w.OFF


def test_an_unreadable_setting_never_restarts():
    assert w.decide(rd((0, DOWN), (60, DOWN)), 60, None).action == w.UNKNOWN


def test_a_restart_just_tried_is_given_time():
    d = w.decide(rd((0, DOWN), (60, DOWN)), 70, "on", [w.Restart(60, False)])
    assert d.action == w.WAIT and d.wait_until == 90


def test_three_failed_restarts_in_the_window_give_up_and_stay_given_up():
    tried = [w.Restart(100, False), w.Restart(200, False), w.Restart(300, False)]
    readings = rd((0, DOWN), (60, DOWN), (400, DOWN), (460, DOWN))
    assert w.decide(readings, 500, "on", tried).action == w.GIVE_UP
    assert w.decide(readings, 520, "on", tried).action == w.GIVE_UP


def test_old_failed_restarts_do_not_count():
    tried = [w.Restart(0, False), w.Restart(10, False), w.Restart(20, False)]
    assert w.decide(rd((900, DOWN), (960, DOWN)), 960, "on", tried).action == w.RESTART


def test_a_restart_that_recovered_does_not_count():
    tried = [w.Restart(100, True), w.Restart(200, True), w.Restart(300, True)]
    assert w.decide(rd((400, DOWN), (460, DOWN)), 460, "on", tried).action == w.RESTART


def _launchctl(disabled="", print_code=0, print_out="state = running\npid = 5\n", disabled_code=0):
    def run(cmd, **kw):
        if cmd[1] == "print-disabled":
            return subprocess.CompletedProcess(cmd, disabled_code, disabled, "")
        return subprocess.CompletedProcess(cmd, print_code, print_out, "")
    return run


SPEC = sc.Spec("x", label="com.example.x", domain="system", port=1)


def test_setting_on_when_enabled_and_loaded():
    out = '\t"com.example.x" => enabled\n'
    assert w.setting(SPEC, _launchctl(out)) == "on"


def test_setting_off_when_disabled():
    assert w.setting(SPEC, _launchctl('\t"com.example.x" => disabled\n')) == "off"


def test_setting_off_when_not_loaded():
    assert w.setting(SPEC, _launchctl("", print_code=113, print_out="")) == "off"


def test_setting_unreadable_when_launchd_fails():
    assert w.setting(SPEC, _launchctl("", disabled_code=1)) is None
    assert w.setting(SPEC, _launchctl("", print_code=5)) is None


def test_a_service_without_a_label_has_no_switch():
    assert w.setting(sc.Spec("n", port=1)) == "on"
