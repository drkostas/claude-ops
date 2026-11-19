"""Whether a running container's image is behind the image its tag points at now.

A tag such as `example/app:1.10` or `example/app:latest` is a moving pointer. A container keeps the
image it was created from, so a security release published under the same tag, or a newer tag the
reader follows, never reaches it until the container is created again from a fresh pull. Looking at
the local tag says nothing, because the local tag still names the image that was pulled.

So this compares two digests. The running image's digests (its `RepoDigests`) come from the local
engine, and the tag's current digest comes from the registry, read without pulling anything
(`docker buildx imagetools inspect`, falling back to `docker manifest inspect`). If the registry's
current digest is not among the running image's digests, the container is behind. Nothing is pulled,
stopped or recreated.

    from claude_ops import image_check
    r = image_check.read("my-container")
    print(r.reference, r.behind, r.age_days)

`behind` is True or False when both digests were read and None when either could not be, so a
registry that did not answer is never read as an image that is current.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class ImageReading:
    container: str
    reference: str | None             # the image reference the container was created with
    running: tuple[str, ...] = ()     # the digests the running image is known by
    current: str | None = None        # the digest the reference points at in the registry now
    created: datetime | None = None   # when the running image was built
    detail: str = ""

    @property
    def behind(self) -> bool | None:
        if not self.running or not self.current:
            return None
        return self.current not in self.running

    @property
    def age_days(self) -> float | None:
        """How old the running image is, in days, or None when its build time is not known."""
        if self.created is None:
            return None
        return (datetime.now(timezone.utc) - self.created).total_seconds() / 86400


def _docker(args: list[str], run: Callable[..., "subprocess.CompletedProcess[str]"], timeout: int = 60) -> str | None:
    try:
        r = run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def parse_time(text: str | None) -> datetime | None:
    """An image's `Created` time, which the engine writes with up to nine fractional digits."""
    if not text:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?", text.strip())
    if not m:
        return None
    base, frac, zone = m.groups()
    micro = (frac or "0")[:6].ljust(6, "0")
    zone = "+00:00" if not zone or zone == "Z" else zone
    try:
        return datetime.fromisoformat(f"{base}.{micro}{zone}")
    except ValueError:
        return None


def running_image(container: str, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run
                  ) -> tuple[str | None, tuple[str, ...], datetime | None]:
    """(the reference the container was created with, the digests of its image, when it was built)."""
    out = _docker(["inspect", "--format", "{{.Config.Image}}|{{.Image}}", container], run)
    if not out or "|" not in out:
        return None, (), None
    reference, image_id = out.strip().split("|", 1)
    info = _docker(["image", "inspect", "--format", "{{json .RepoDigests}}|{{.Created}}", image_id], run)
    if not info or "|" not in info:
        return reference or None, (), None
    digests_json, created = info.strip().split("|", 1)
    try:
        listed = json.loads(digests_json) or []
    except ValueError:
        listed = []
    digests = tuple(m.group(0) for d in listed for m in [_DIGEST.search(str(d))] if m)
    return reference or None, digests, parse_time(created)


def current_digest(reference: str, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> str | None:
    """The digest a reference points at in the registry now, read without pulling. A reference that
    already names a digest (`name@sha256:...`) is its own current digest."""
    pinned = _DIGEST.search(reference.split("@", 1)[1]) if "@" in reference else None
    if pinned:
        return pinned.group(0)
    out = _docker(["buildx", "imagetools", "inspect", reference, "--format", "{{.Manifest.Digest}}"], run, timeout=90)
    m = _DIGEST.search(out or "")
    if m:
        return m.group(0)
    out = _docker(["manifest", "inspect", "-v", reference], run, timeout=90)
    if out:
        try:
            parsed = json.loads(out)
            first = parsed[0] if isinstance(parsed, list) and parsed else parsed
            m = _DIGEST.search(json.dumps((first or {}).get("Descriptor", {}).get("digest", "")))
            if m:
                return m.group(0)
        except (ValueError, AttributeError):
            pass
    return None


def read(container: str, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> ImageReading:
    """One container's reading. A reference the registry cannot be asked about is left with no
    current digest, so `behind` says None."""
    reference, running, created = running_image(container, run)
    if not reference:
        return ImageReading(container, None, detail="the container or its image could not be read")
    return ImageReading(container, reference, running, current_digest(reference, run), created)
