"""file_patch — put a small hand-made change back into a file that an update replaced.

Software that updates itself replaces its own files, and a change you made in an installed copy
(a setting turned off, a flag added to a command) goes with them. `apply` re-makes one such change
from a recipe of three strings and says what happened, so a program can do it again after every
update and report the result instead of leaving the change forgotten.

    result = file_patch.apply(path, find="run(a);", replace="run(a, {quiet: true});",
                              contains="quiet: true", backup_suffix=".bak-quiet")

THE RECIPE.
  find      the exact text the update leaves, which must appear exactly once in the file
  replace   the text that takes its place
  contains  the text that is in the file only while the change is, and so the proof it holds

THE RESULT is a dict with `status` and `detail`.
  present    `contains` is already in the file, so nothing was changed
  applied    the file was changed and `contains` was read back from it
  would_apply  (only with dry_run=True) the change could be made, and nothing was written
  not_found  `find` is not in the file, so the update changed the text the recipe knew; nothing was changed
  ambiguous  `find` appears more than once, so the recipe cannot say where; nothing was changed
  failed     the file could not be read or written, or the written file lacks `contains`; the original
             is put back whenever the write went wrong

A recipe that cannot be applied never guesses: it changes nothing and says why.

THE WRITE keeps the file's permission bits, goes through a temporary file in the same folder and a
rename, so a program reading the file never sees half of it. With `backup_suffix` the original is
copied to <path><backup_suffix> first, unless a copy is already there (the first one is the
original, and a later re-apply must not overwrite it with an already changed file).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def apply(path, find: str, replace: str, contains: str, *, backup_suffix: str | None = None,
          dry_run: bool = False) -> dict:
    """Make one recipe's change in `path`, once. See the module text for the statuses. With `dry_run`
    nothing is written and a change that could be made answers `would_apply`."""
    p = Path(path)
    if not find or contains not in replace:
        return {"status": "failed", "path": str(p),
                "detail": "the recipe is wrong: `find` is empty or `replace` does not hold `contains`"}
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {"status": "failed", "path": str(p), "detail": f"cannot read it ({e.__class__.__name__})"}
    if contains in text:
        return {"status": "present", "path": str(p), "detail": "already holds it"}
    n = text.count(find)
    if n == 0:
        return {"status": "not_found", "path": str(p), "detail": "the text to replace is not in the file"}
    if n > 1:
        return {"status": "ambiguous", "path": str(p), "detail": f"the text to replace appears {n} times"}
    if dry_run:
        return {"status": "would_apply", "path": str(p), "detail": "the text to replace appears once"}
    backup = None
    try:
        if backup_suffix:
            backup = Path(str(p) + backup_suffix)
            if not backup.exists():
                shutil.copy2(p, backup)
        mode = p.stat().st_mode
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".new")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text.replace(find, replace, 1))
            os.chmod(tmp, mode & 0o7777)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        if contains not in p.read_text(encoding="utf-8"):
            raise OSError("the file does not hold the change after the write")
    except OSError as e:
        try:                                          # put the original back whatever went wrong
            p.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return {"status": "failed", "path": str(p), "detail": f"could not make the change ({e})"}
    return {"status": "applied", "path": str(p), "detail": "changed and read back",
            "backup": str(backup) if backup else None}
