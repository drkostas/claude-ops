"""kernel_memory: wired memory from vm_stat, kernel zones from zprint and queued Mach messages per process
from lsmp, each read through a runner that may fail or time out. All text invented in the tools' shapes."""
import sys

from claude_ops import kernel_memory as km

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     3776.
Pages active:                                 289615.
Pages wired down:                             131072.
Pages purgeable:                                  26.
"""

ZPRINT = """                            elem         cur         max        cur         max         cur  alloc  alloc                total
zone name                   size        size        size      #elts       #elts       inuse   size  count               allocs
-------------------------------------------------------------------------------------------------------------------------------
vm.permanent                   1        176K        176K     180224      180224      174450    16K  16384                 170K
ipc.kmsgs                    256      91360K      91728K     365440      366912      364936    16K     64  C      48622114278K
vm.pages.array                48      94672K      94672K    2019669     2019669     2019495    16K    341 XC            94663K
data.kalloc.1024            1024    2097152K    2097152K    2097152     2097152     2097000    16K     16  C      34790009164K
VM.map.entries                64      94512K     313264K    1512192     5012224     1311775    16K    256  C       3645497790K
NFS.mount                   1400          0K          0K          0           0           0    32K     23  C                0B
dtrace.fasttrap_probe_t[$    112        256K      20736K       2340      189586        1868    16K    146  C        196870506K
-------------------------------------------------------------------------------------------------------------
                                                               kmod          vm        peak               cur
wired memory                                                     id         tag        size  waste       size
-------------------------------------------------------------------------------------------------------------
VM_KERN_COUNT_BOOT_STOLEN                                                   266                       902528K
zones                                                                                                9269890K
total                                                                                  13794.96M of 13788.27M
data.kalloc.1024[raw]                                                                                2097000K
"""

LSMP = """Ignoring failure of mach_port_space_info() for task 1234 for '-all'
Process (0) : kernel_task
  name      ipc-object    rights     flags   boost  reqs  recv  send sonce oref  qlimit  msgcount  context            identifier  type
---------   ----------  ----------  -------- -----  ---- ----- ----- ----- ----  ------  --------  ------------------ ----------- ------------
0x00000103  0x291f3850  send        --------        ---            1                                                  0x00000000  (-) Unknown Process
Process (501) : example-watcher
  name      ipc-object    rights     flags   boost  reqs  recv  send sonce oref  qlimit  msgcount  context            identifier  type
0x00000103  0x9327afcf  send        --------        ---            4                                                  0x00000000  THREAD-CONTROL (0xe08aad9)
0x0000030b  0xbb0051db  recv        --------     0  ---      1               N        5         0  0x0000000000000000
0x00002223  0x88efe404  recv,send   --------     0  ---      1     1         Y     1024      1024  0x0000000000000000
0x0000275b  0x17751283  recv,send   --------     0  ---      1     1         Y     1024       300  0x0000000000000000
Process (502) : example-daemon
0x00000c03  0xd26bacf1  recv        ---G----     0  -N-      1           1   Y        5         2  0x0000000af8de37a0
 -          0xf9de385c  recv,send   ---G----     0  DN-      1     1     1   Y        5         4  0x0000000af8c6c900 0x0000401b  (437) other
Process (503) : quiet-app
0x00000c03  0xd26bacf2  recv        --------     0  ---      1               N        5         0  0x0000000000000000
"""


def fake(outputs):
    """A runner that answers each command by its program's name, and records what it was asked."""
    asked = []

    def run(cmd, timeout, env):
        asked.append((list(cmd), timeout, env))
        for name, out in outputs.items():
            if any(part.endswith("/" + name) for part in cmd):
                return out
        return None
    return run, asked


def test_wired_from_vm_stat():
    assert km.parse_vm_stat(VM_STAT) == 131072 * 16384 / km.GB == 2.0
    assert km.parse_vm_stat("") is None
    assert km.parse_vm_stat("Pages wired down: 5.") is None   # no page size, no guess


def test_zones_largest_first_and_only_the_zone_table():
    zones = km.parse_zprint(ZPRINT)
    assert [z.name for z in zones][:3] == ["data.kalloc.1024", "vm.pages.array", "VM.map.entries"]
    top = zones[0]
    assert top.size == top.peak == 2097152 * 1024 and top.elements == 2097152 and top.at_peak
    entries = next(z for z in zones if z.name == "VM.map.entries")
    assert entries.size == 94512 * 1024 and entries.peak == 313264 * 1024 and not entries.at_peak
    names = {z.name for z in zones}
    # a row with flags, one without, a truncated name and an empty zone all read
    assert {"ipc.kmsgs", "vm.permanent", "dtrace.fasttrap_probe_t[$", "NFS.mount"} <= names
    # the wired-memory table's rows are not zones
    assert not names & {"zones", "total", "VM_KERN_COUNT_BOOT_STOLEN", "data.kalloc.1024[raw]"}
    assert km.parse_zprint("") == []


def test_queued_per_process_counts_each_port_once():
    holders = km.parse_lsmp(LSMP)
    assert [(h.pid, h.name, h.queued, h.ports, h.full) for h in holders] == [
        (501, "example-watcher", 1324, 2, 1),
        (502, "example-daemon", 2, 1, 0),      # the "-" row is a port set's member, counted on its own row
    ]
    assert km.parse_lsmp("") == []


def test_readers_use_sudo_n_a_timeout_and_the_callers_env():
    run, asked = fake({"vm_stat": VM_STAT, "zprint": ZPRINT, "lsmp": LSMP})
    env = {"PATH": "/usr/bin:/bin"}
    assert km.wired_gb(run=run, env=env) == 2.0
    assert km.read_zones(run=run, env=env, timeout=7)[0].name == "data.kalloc.1024"
    assert km.read_queued(run=run, env=env)[0].pid == 501
    assert asked[0] == (["/usr/bin/vm_stat"], km.TIMEOUT_S, env)
    assert asked[1] == (["/usr/bin/sudo", "-n", "/usr/bin/zprint", "-t"], 7, env)
    assert asked[2][0] == ["/usr/bin/sudo", "-n", "/usr/bin/lsmp", "-a"]


def test_a_tool_that_cannot_run_is_unknown_never_empty():
    run, _ = fake({})
    assert km.wired_gb(run=run) is None
    assert km.read_zones(run=run) is None
    assert km.read_queued(run=run) is None
    # it ran and printed something that is not its table: still unknown
    run, _ = fake({"zprint": "sudo: a password is required\n", "lsmp": "usage: lsmp\n"})
    assert km.read_zones(run=run) is None and km.read_queued(run=run) is None
    # it ran and found no message waiting anywhere: an empty list, which is an answer
    run, _ = fake({"lsmp": "Process (503) : quiet-app\n0x00000c03  0xd26bacf2  recv        --------     0  ---"
                           "      1               N        5         0  0x0000000000000000 \n"})
    assert km.read_queued(run=run) == []


def test_the_default_runner_gives_none_on_failure_and_on_timeout():
    assert km._run([sys.executable, "-c", "print('ok')"], 10, None) == "ok\n"
    assert km._run([sys.executable, "-c", "import sys; sys.exit(3)"], 10, None) is None
    assert km._run(["/nonexistent/tool"], 10, None) is None
    assert km._run([sys.executable, "-c", "import time; time.sleep(5)"], 0.2, None) is None
    # the environment passed is the one the command gets
    out = km._run([sys.executable, "-c", "import os; print(os.environ.get('KM_PROBE'))"], 10,
                  {"KM_PROBE": "seen", "PATH": "/usr/bin:/bin"})
    assert out == "seen\n"
