"""Readings of a process's runtime. Only fake command output and a throwaway folder are used."""
import os
import subprocess
import tempfile
from datetime import date

from claude_ops import runtime_check as rc


def _out(text="", code=0):
    return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=text, stderr="")


def test_mapped_files_lists_the_program_first():
    lsof = "p123\nn/usr/local/bin/node\nn/usr/lib/libz.1.dylib\nn/usr/lib/libc.dylib\n"
    assert rc.mapped_files(123, _out(lsof)) == ["/usr/local/bin/node", "/usr/lib/libz.1.dylib", "/usr/lib/libc.dylib"]


def test_a_process_lsof_cannot_read_has_no_mapping():
    assert rc.mapped_files(1, _out("", 1)) is None


def test_a_missing_tool_is_not_a_crash():
    def boom(*a, **k):
        raise FileNotFoundError("lsof")
    assert rc.mapped_files(1, boom) is None


def test_a_library_upgraded_on_disk_is_still_mapped_by_an_old_process():
    d = tempfile.mkdtemp()
    new = os.path.join(d, "libbrotli.1.2.1.dylib")
    open(new, "w").close()
    old = os.path.join(d, "libbrotli.1.2.0.dylib")        # the file an upgrade removed
    stale = rc.stale_mappings([new, old, "[anon]"])
    assert stale == [old]


def test_linux_marks_a_replaced_file_as_deleted():
    cache = "/Library/Preferences/Logging/.plist-cache.dM7MVDfq"
    assert rc.stale_mappings([cache, "/tmp/x/.hidden.dylib", "/Library/Preferences/a.plist"], exists=lambda p: False) == []
    assert rc.stale_mappings(["/usr/lib/libz.so.1 (deleted)"]) == ["/usr/lib/libz.so.1 (deleted)"]


def test_runtime_names():
    assert rc.runtime_name("/opt/homebrew/bin/node") == "node"
    assert rc.runtime_name("/usr/bin/python3.14") == "python"
    assert rc.runtime_name("/usr/sbin/php-fpm8.3") == "php"
    assert rc.runtime_name("/home/u/.bun/bin/bun") == "bun"
    assert rc.runtime_name("/bin/sh") is None
    for other in ("node_exporter", "nodemon", "rubocop", "pythonista", "phpstorm"):
        assert rc.runtime_name("/usr/bin/" + other) is None
    assert rc.runtime_name(None) is None


def test_versions_are_read_from_what_each_program_prints():
    assert rc.parse_version("v25.8.1\n") == "25.8.1"
    assert rc.parse_version("Python 3.14.0") == "3.14.0"
    assert rc.parse_version("PHP 8.3.1 (cli) (built: Jan 1 2026)") == "8.3.1"
    assert rc.parse_version("1.3.6") == "1.3.6"
    assert rc.parse_version("nothing here") is None


def test_a_failed_version_call_is_none():
    assert rc.version_of("/x/node", _out("", 127)) is None


def test_read_puts_the_four_facts_together():
    d = tempfile.mkdtemp()
    live = os.path.join(d, "libok.dylib")
    open(live, "w").close()
    gone = os.path.join(d, "libold.dylib")
    calls = []

    def run(argv, **k):
        calls.append(argv)
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0, f"p9\nn/opt/x/bin/node\nn{live}\nn{gone}\n", "")
        return subprocess.CompletedProcess(argv, 0, "v25.8.1\n", "")
    r = rc.read(9, run)
    assert (r.name, r.version, r.executable, r.stale, r.mapped) == ("node", "25.8.1", "/opt/x/bin/node", (gone,), 2)
    assert ["/opt/x/bin/node", "--version"] in calls


def test_a_program_that_is_not_a_runtime_is_never_run():
    calls = []

    def run(argv, **k):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "p9\nn/usr/bin/screencapture\n" if argv[0] == "lsof" else "x 1.2.3", "")
    r = rc.read(9, run)
    assert (r.name, r.version) == (None, None)
    assert [c[0] for c in calls] == ["lsof"]


def test_read_of_an_unreadable_process_is_none():
    assert rc.read(1, _out("", 1)) is None


def test_release_lines():
    assert rc.release_line("node", "25.8.1") == "25"
    assert rc.release_line("python", "3.14.2") == "3.14"
    assert rc.release_line("php", "8.3.1") == "8.3"
    assert rc.release_line("bun", "1.3.6") == "1"
    assert rc.release_line("php", "8") is None


TABLE = {"node": {"22": "2027-04-30", "25": "2026-06-01"}, "python": {"3.14": "2030-10-31"}}


def test_a_line_past_its_date_is_ended():
    s = rc.support(TABLE, "node", "25.8.1", date(2026, 11, 20))
    assert (s.state, s.line, s.ends) == (rc.ENDED, "25", date(2026, 6, 1))


def test_a_line_inside_its_date_is_supported():
    assert rc.support(TABLE, "node", "22.1.0", date(2026, 11, 20)).state == rc.SUPPORTED
    assert rc.support(TABLE, "python", "3.14.0", date(2026, 11, 20)).state == rc.SUPPORTED


def test_a_runtime_or_line_the_table_lacks_is_unknown_never_supported():
    assert rc.support(TABLE, "node", "18.0.0", date(2026, 11, 20)).state == rc.UNKNOWN
    assert rc.support(TABLE, "ruby", "3.3.0", date(2026, 11, 20)).state == rc.UNKNOWN
    assert rc.support(TABLE, None, "1.0", date(2026, 11, 20)).state == rc.UNKNOWN
    assert rc.support({"node": {"25": "soon"}}, "node", "25.1.0", date(2026, 11, 20)).state == rc.UNKNOWN
