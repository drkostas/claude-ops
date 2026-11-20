"""Map a folder tree and find the files that answer a need, from paths and names alone.

`walk` turns a folder into one row per file (path, size, modified time, extension).
`signature` is a cheap reading that changes when a file is added, removed, renamed or
edited, so a caller rebuilds its map only when it must. `find` ranks the rows for a
need written in plain words ("a copy of my ID card"). It reads no file content, only the
words of each folder and file name, so it is safe to run over a folder of private papers.

A caller passes its own `rules`, each saying which need words it answers and which folder
and name words then count for more:

    {"when": ["id", "passport"], "folders": ["identity"], "names": ["card", "passport"]}

Among files that match equally well, the format a person can send is preferred (a PDF,
then a picture, then a word processor file, then raw camera files), and then the newest.
"""
import os
import re

FORMATS = {"pdf": 5, "jpg": 4, "jpeg": 4, "png": 4, "heic": 3, "docx": 2, "doc": 2, "odt": 2,
           "txt": 1, "xlsx": 1, "csv": 1}
RAW = {"dng", "cr2", "cr3", "nef", "arw", "raw"}
STOP = {"a", "an", "the", "of", "my", "me", "for", "and", "or", "to", "with", "from", "in", "on", "is",
        "copy", "copies", "document", "documents", "file", "files", "showing", "show", "current",
        "latest", "need", "needs", "proof", "please", "scan", "scanned"}
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def words(text):
    """Lower case words of a text, split on anything that is not a letter or digit."""
    return [w for w in re.split(r"[^0-9a-zͰ-Ͽ]+", (text or "").lower()) if w]


def stem(word):
    """A word without a plural ending, so "statements" meets "statement"."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def stems(text):
    return {stem(w) for w in words(text)}


def walk(root, skip=()):
    """Rows for every file under `root`: path (relative, with /), size, mtime, ext.

    A folder whose name is in `skip` is not entered. Unreadable folders and files that
    vanish during the walk are left out. Links are not followed.
    """
    root = os.path.abspath(os.path.expanduser(root))
    rows = []
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            entries = list(os.scandir(os.path.join(root, rel) if rel else root))
        except OSError:
            continue
        for e in entries:
            if e.name in SKIP_NAMES or e.name.startswith("~$"):
                continue
            path = f"{rel}/{e.name}" if rel else e.name
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if e.name not in skip:
                        stack.append(path)
                    continue
                st = e.stat()
            except OSError:
                continue
            ext = e.name.rsplit(".", 1)[1].lower() if "." in e.name else ""
            rows.append({"path": path, "size": st.st_size, "mtime": int(st.st_mtime), "ext": ext})
    rows.sort(key=lambda r: r["path"])
    return rows


def signature(rows):
    """What changed between two walks: the count, the total size and the newest time."""
    return {"files": len(rows), "bytes": sum(r["size"] for r in rows),
            "newest": max((r["mtime"] for r in rows), default=0)}


def format_rank(ext):
    ext = (ext or "").lower()
    if ext in RAW:
        return 0
    return FORMATS.get(ext, 1)


def _need_stems(need):
    return [s for s in (stem(w) for w in words(need)) if s not in STOP]


def score(row, need, rules=()):
    """How well a row answers a need, and the words that made it match.

    A need word found in the file's name counts 3 and in a folder's name 2. A rule whose
    `when` words meet the need adds 6 for each of its `folders` words found in a folder
    name and 3 for each of its `names` words found in the file name, once each.
    """
    folders, _, name = row["path"].rpartition("/")
    folder_stems = stems(folders)
    name_stems = stems(name.rsplit(".", 1)[0] if "." in name else name)
    asked = _need_stems(need)
    total, why = 0, []
    for w in dict.fromkeys(asked):
        if w in name_stems:
            total += 3
            why.append(f"name has {w}")
        elif w in folder_stems:
            total += 2
            why.append(f"folder has {w}")
    asked_set = set(asked)
    for rule in rules:
        if not asked_set & stems(" ".join(rule.get("when", []))):
            continue
        for w in dict.fromkeys(stems(" ".join(rule.get("folders", [])))):
            if w in folder_stems:
                total += 6
                why.append(f"folder says {w}")
        for w in dict.fromkeys(stems(" ".join(rule.get("names", [])))):
            if w in name_stems:
                total += 3
                why.append(f"name says {w}")
    return total, why


def find(rows, need, rules=(), limit=5, min_score=3):
    """The best `limit` rows for a need, best first, each with `score`, `why` and `format`.

    Order is the score, then the sendable format, then the newest. A row that scores under
    `min_score` is never returned, so a need that matches nothing gives an empty list.
    """
    found = []
    for row in rows:
        s, why = score(row, need, rules)
        if s >= min_score:
            found.append(dict(row, score=s, why=why, format=row["ext"] or "none"))
    found.sort(key=lambda r: (-r["score"], -format_rank(r["ext"]), -r["mtime"], r["path"]))
    return found[:limit]
