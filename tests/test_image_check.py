"""Readings of a container's image against its tag. Only fake command output is used."""
import subprocess

from claude_ops import image_check as ic

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


def _docker(answers):
    """A runner that answers each docker call by its subcommand words."""
    def run(argv, **k):
        key = " ".join(argv[1:3])
        for pattern, (code, out) in answers.items():
            if pattern in " ".join(argv[1:]):
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 1, "", "no answer")
    return run


def test_an_image_behind_its_tag_is_behind():
    run = _docker({
        "inspect --format {{.Config.Image}}": (0, "example/app:1.10|sha256:img\n"),
        "image inspect": (0, f'["example/app@{OLD}"]|2026-10-01T10:00:00.123456789Z\n'),
        "buildx imagetools": (0, NEW + "\n"),
    })
    r = ic.read("web", run)
    assert (r.reference, r.running, r.current, r.behind) == ("example/app:1.10", (OLD,), NEW, True)
    assert r.created is not None and r.age_days > 0


def test_a_current_image_is_not_behind():
    run = _docker({
        "inspect --format {{.Config.Image}}": (0, "example/app:1.10|sha256:img\n"),
        "image inspect": (0, f'["example/app@{NEW}"]|2026-11-18T10:00:00Z\n'),
        "buildx imagetools": (0, NEW + "\n"),
    })
    assert ic.read("web", run).behind is False


def test_a_registry_that_does_not_answer_is_not_a_current_image():
    run = _docker({
        "inspect --format {{.Config.Image}}": (0, "example/app:1.10|sha256:img\n"),
        "image inspect": (0, f'["example/app@{OLD}"]|2026-10-01T10:00:00Z\n'),
    })
    r = ic.read("web", run)
    assert (r.current, r.behind) == (None, None)


def test_the_manifest_fallback_is_used_when_buildx_is_missing():
    manifest = '{"Descriptor": {"digest": "%s"}}' % NEW
    run = _docker({
        "inspect --format {{.Config.Image}}": (0, "example/app:1.10|sha256:img\n"),
        "image inspect": (0, f'["example/app@{OLD}"]|2026-10-01T10:00:00Z\n'),
        "manifest inspect": (0, manifest),
    })
    assert ic.read("web", run).current == NEW


def test_a_multi_platform_manifest_list_gives_no_digest():
    listing = '[{"Descriptor": {"digest": "%s"}}, {"Descriptor": {"digest": "%s"}}]' % (NEW, OLD)
    run = _docker({
        "inspect --format {{.Config.Image}}": (0, "example/app:1.10|sha256:img\n"),
        "image inspect": (0, f'["example/app@{OLD}"]|2026-10-01T10:00:00Z\n'),
        "manifest inspect": (0, listing),
    })
    r = ic.read("web", run)
    assert (r.current, r.behind) == (None, None)


def test_a_container_that_cannot_be_read_says_so():
    r = ic.read("gone", _docker({}))
    assert (r.reference, r.behind) == (None, None) and r.detail


def test_a_locally_built_image_has_no_digest_and_so_no_answer():
    run = _docker({
        "inspect --format {{.Config.Image}}": (0, "local-build|sha256:img\n"),
        "image inspect": (0, "[]|2026-10-01T10:00:00Z\n"),
        "buildx imagetools": (1, ""),
    })
    assert ic.read("web", run).behind is None


def test_a_pinned_reference_is_its_own_current_digest():
    assert ic.current_digest(f"example/app@{OLD}", _docker({})) == OLD


def test_image_times_with_nine_fraction_digits_and_zones():
    assert ic.parse_time("2026-10-01T10:00:00.123456789Z").microsecond == 123456
    assert ic.parse_time("2026-10-01T10:00:00+02:00").utcoffset().total_seconds() == 7200
    assert ic.parse_time("garbage") is None and ic.parse_time(None) is None
