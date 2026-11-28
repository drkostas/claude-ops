"""stand_in: check that a test's fake of a function takes every keyword the real function takes.

A test often replaces a real function with a fake one (a stand-in), so the code under test runs
without the network, a device or a slow program:

    from claude_ops.stand_in import stand_in
    module.build = stand_in(module.build, fake_build)

When the real function gains a keyword and the code under test starts to pass it, a fake that does
not accept it raises TypeError inside the code under test. Code that turns an error into an outcome
then reports a wrong result, far from the cause, and the test fails on that result. ``stand_in``
compares the two signatures when the fake is set and raises ``TypeError`` at once, naming each
argument the fake cannot take. It returns the fake unchanged, so it can wrap any assignment.

What is compared, with ``inspect.signature`` on both:

- every parameter of the real function that a caller may pass by name (keyword-only or
  positional-or-keyword) must be a parameter of the fake with the same name that can be passed by
  name, unless the fake has ``**kwargs``;
- a positional-only parameter of the real function needs a place in the fake: a positional
  parameter whose name the real function does not take by name, or ``*args``.

The order of positional parameters is not compared.

A real or fake function whose signature cannot be read (some built-in functions) is refused, since
nothing could be compared. ``missing(real, fake)`` returns the list of what the fake cannot take,
without raising, for a check that wants to report several fakes at once.

It compares names and places only. A fake that takes a keyword and ignores it passes, and a function
the code under test calls that the test never replaced is not seen, because no stand-in exists for it.
"""
from __future__ import annotations

import inspect
from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)

_P = inspect.Parameter
#: the kinds of parameter that take an argument in a place
_IN_PLACE = (_P.POSITIONAL_ONLY, _P.POSITIONAL_OR_KEYWORD)
#: the kinds of parameter that take an argument by name
_BY_NAME = (_P.POSITIONAL_OR_KEYWORD, _P.KEYWORD_ONLY)


def missing(real: Callable, fake: Callable) -> list[str]:
    """The parameters of `real` that `fake` cannot take, in `real`'s order. Empty when it takes them all."""
    want = list(_signature(real, "real").parameters.values())
    have = list(_signature(fake, "fake").parameters.values())
    any_name = any(p.kind is _P.VAR_KEYWORD for p in have)
    any_place = any(p.kind is _P.VAR_POSITIONAL for p in have)
    names = {p.name for p in have if p.kind in _BY_NAME}
    named = {p.name for p in want if p.kind in _BY_NAME}
    # a fake's positional parameter that answers to one of the real names is taken by that name, so it is
    # no free place for a positional-only argument
    places = sum(1 for p in have if p.kind in _IN_PLACE and p.name not in named)
    out: list[str] = []
    place = 0
    for p in want:
        if p.kind in _BY_NAME:
            if not (any_name or p.name in names):
                out.append(p.name)
        elif p.kind is _P.POSITIONAL_ONLY:
            if not (any_place or place < places):
                out.append(p.name)
            place += 1
    return out


def stand_in(real: Callable, fake: F) -> F:
    """`fake` itself, once it takes every argument `real` takes (see the module's docstring), or TypeError
    naming what it cannot take."""
    lacks = missing(real, fake)
    if lacks:
        raise TypeError(f"the stand-in {_name(fake)} for {_name(real)} cannot take {', '.join(lacks)}, "
                        f"which {_name(real)} takes")
    return fake


def _signature(fn: Callable, which: str) -> inspect.Signature:
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError) as e:
        raise TypeError(f"the {which} function {_name(fn)} has no signature to compare ({e})") from None


def _name(fn: Callable) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)
