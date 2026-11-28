import inspect

import pytest

from claude_ops.stand_in import missing, stand_in


def build(commit, *, git=None, run=None, log_dir="/path/to/logs", runtime=None):
    return commit


def test_a_fake_without_a_new_keyword_is_refused_and_named():
    # the real function gained `runtime`, and the fake written before it never took it
    def build_waits(commit, *, git, run, log_dir="/path/to/logs"):
        return commit

    with pytest.raises(TypeError) as e:
        stand_in(build, build_waits)
    assert "cannot take runtime, which build takes" in str(e.value)
    assert "build_waits" in str(e.value)
    assert missing(build, build_waits) == ["runtime"]


def test_every_missing_keyword_is_named_in_order():
    fake = lambda commit, git=None, run=None: commit  # noqa: E731
    assert missing(build, fake) == ["log_dir", "runtime"]


def test_a_fake_with_kwargs_takes_every_keyword_and_is_returned_itself():
    def build_any(commit, **kw):
        return commit

    assert stand_in(build, build_any) is build_any
    assert missing(build, build_any) == []


def test_a_parameter_callers_may_pass_by_name_needs_its_own_name():
    # build(commit=...) is a call a caller may make, and a fake that names it c cannot take it
    def fake(c, *, git, run, log_dir, runtime):
        return c

    assert missing(build, fake) == ["commit"]

    def real(a, b, *, c=None):
        return a

    assert missing(real, lambda a, *, c=None: a) == ["b"]
    assert missing(real, lambda *args, c=None: None) == ["a", "b"]
    assert missing(real, lambda a, **kw: None) == []


def test_positional_only_parameters_are_compared_by_place():
    def real(a, b, /, c=None):
        return a

    assert missing(real, lambda x, y, c=None: x) == []
    assert missing(real, lambda x, c=None: x) == ["b"]


def test_a_function_whose_signature_cannot_be_read_is_refused():
    class NoSignature:
        __signature__ = "not a signature"

        def __call__(self, *a, **k):
            return None

    with pytest.raises(TypeError, match="has no signature to compare"):
        stand_in(NoSignature(), build)
    with pytest.raises(TypeError, match="the fake function"):
        stand_in(build, NoSignature())


def test_a_bound_method_is_compared_without_self():
    class Phone:
        def install(self, path, *, serial):
            return path

    def fake_install(path, *, serial):
        return path

    assert stand_in(Phone().install, fake_install) is fake_install
    assert "self" not in inspect.signature(Phone().install).parameters
