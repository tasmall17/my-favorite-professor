"""Where everything lives.

One rule drives this module: the user sees exactly one Markdown file per
capture. Every other artifact -- the alias dictionary, the audit log, the
failure CSV, the archived HTML and its images -- is machinery, and machinery
is hidden.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

LIBRARY_NAME = "My-Favorite-Professor"

# Where a *new* library goes. The old default was ~/code/My-Favorite-Professor,
# which is unusable on macOS: the filesystem is case-insensitive, so a repo
# cloned as ~/code/my-favorite-professor is literally that same directory, and
# `git clone` followed by `./install.sh` built the library inside the app's own
# source. This name is immune -- nothing is ever cloned as
# `my-favorite-professor-library`. It stays visible in $HOME rather than hiding
# in a dotdir because the whole point of the material is that you can browse it,
# copy it, and take it elsewhere.
DEFAULT_LIBRARY_NAME = "my-favorite-professor-library"

# Libraries created by earlier versions, newest guess first. Checked only when
# the current default is absent, and adopted only if the directory really holds
# material -- ~/code/My-Favorite-Professor is exactly where a macOS checkout
# lands, so "the directory exists" says nothing about what's in it. Both
# spellings of code/Code are listed because only macOS conflates them.
_LEGACY_LIBRARIES = (
    ("code", LIBRARY_NAME),
    ("Code", LIBRARY_NAME),
    ("Documents", LIBRARY_NAME),
    (LIBRARY_NAME,),
)

# The single hidden directory holding all cross-topic machinery.
MACHINE_DIR = ".mfp"
# Hidden directory inside each topic holding the archived pages.
CAPTURES_DIR = ".captures"

# Visible subdirectories inside a topic, splitting material by who chose it.
# The distinction is the point: your own references are the primary source
# Professor-Claude teaches from, and anything it fetched to fill a gap is
# clearly marked as such rather than silently blended in.
USER_REFS_DIR = "usr-references-provided"
CLAUDE_REFS_DIR = "claude-references-provided"

# Hidden, inside a topic: the generated syllabus and lesson cache.
COURSE_DIR = ".mfp-course"

TOPIC_SUFFIX = "-professor"
INBOX_TOPIC = "inbox"

# Visible directories at the library root that are emphatically not subjects.
# The topic registry treats every non-dot directory it finds as a topic and
# writes an alias for it, so anything that lands beside the topics gets adopted
# as one -- a `usr-learning-profile/` at the root would start answering to
# `-usr`. Hiding those directories would work but makes them hard to find and
# copy, which defeats the point of a profile you can hand to someone.
RESERVED_DIRNAMES = frozenset({
    "usr-learning-profile",
    "professor",
    "tests",
    "node_modules",
})


def _looks_like_source_checkout(path: Path) -> bool:
    """Is this the application's own repo rather than a material library?

    Worth checking because the app and the library want the same name, and on
    a case-insensitive filesystem `my-favorite-professor` and
    `My-Favorite-Professor` are the *same directory*. Cloning the repo beside
    your library silently merges the two, and the first thing that notices is
    the topic funnel offering `professor/` as a subject. Cheap to detect, very
    confusing to debug.
    """
    return (path / "pyproject.toml").is_file() and (path / "professor").is_dir()


def _looks_like_library(path: Path) -> bool:
    """Does this directory actually hold material, or does it just have the name?

    An empty ~/code/My-Favorite-Professor proves nothing: on macOS that path is
    created by the mere act of cloning the repo. Requiring the machine directory
    or at least one subject means a legacy location is only adopted when there
    is something there worth adopting.
    """
    if (path / MACHINE_DIR).is_dir():
        return True
    try:
        return any(child.is_dir() for child in path.glob(f"*{TOPIC_SUFFIX}"))
    except OSError:
        # Nearly every code path calls library_root(); an unreadable candidate
        # should cost us one guess, not the whole process.
        return False


def default_library() -> Path:
    """Where a library is created when there isn't one yet.

    Resolved on each call rather than frozen at import so that $HOME is read
    when it matters -- tests and anything running under a different user.
    """
    return Path.home() / DEFAULT_LIBRARY_NAME


def library_root() -> Path:
    """The library directory. MFP_LIBRARY overrides everything.

    Order: the environment, then the current default *if it holds material*,
    then any legacy location that does, then the current default again as the
    place to create one. Existing libraries keep working without being moved;
    new ones land somewhere a checkout can never collide with.

    Both the "holds material" gates matter. Requiring content rather than mere
    existence is what stops an empty directory from hiding a real library, and
    rejecting a source checkout is what stops the app adopting itself when a
    clone and a library have folded onto the same inode.
    """
    env = os.environ.get("MFP_LIBRARY", "").strip()
    if env:
        return Path(env).expanduser()

    # The default has to hold material to win, not merely exist. An empty
    # directory of that name -- a stray mkdir, an aborted first run, a typo in
    # --library -- would otherwise outrank a real library in a legacy location
    # and silently present someone with an empty app instead of their material.
    default = default_library()
    if default.is_dir() and _looks_like_library(default):
        return default

    home = Path.home()
    for parts in _LEGACY_LIBRARIES:
        candidate = home.joinpath(*parts)
        if (
            candidate.is_dir()
            and _looks_like_library(candidate)
            and not _looks_like_source_checkout(candidate)
        ):
            return candidate
    return default


def machine_dir(root: Path | None = None) -> Path:
    return (root or library_root()) / MACHINE_DIR


def topics_file(root: Path | None = None) -> Path:
    return machine_dir(root) / "topics.json"


def audit_file(root: Path | None = None) -> Path:
    return machine_dir(root) / "audit.log"


def failures_file(root: Path | None = None) -> Path:
    return machine_dir(root) / "failed-attempts.csv"


def captures_dir(topic_dir: Path) -> Path:
    return topic_dir / CAPTURES_DIR


def user_refs_dir(topic_dir: Path) -> Path:
    return topic_dir / USER_REFS_DIR


def claude_refs_dir(topic_dir: Path) -> Path:
    return topic_dir / CLAUDE_REFS_DIR


def course_dir(topic_dir: Path) -> Path:
    return topic_dir / COURSE_DIR


def refs_dir_for(topic_dir: Path, source: str) -> Path:
    """Which visible directory a note belongs in, given who chose the material.

    `source` is the same value stored in the manifest, so the note's location
    on disk and its provenance record can never drift apart.
    """
    return claude_refs_dir(topic_dir) if source == "claude" else user_refs_dir(topic_dir)


def ensure_topic_layout(topic_dir: Path) -> Path:
    """Create the subdirectories a topic needs. Safe to call repeatedly."""
    topic_dir.mkdir(parents=True, exist_ok=True)
    for sub in (CAPTURES_DIR, USER_REFS_DIR, CLAUDE_REFS_DIR, COURSE_DIR):
        (topic_dir / sub).mkdir(exist_ok=True)
    return topic_dir


def ensure_library(root: Path | None = None) -> Path:
    """Create the library and its hidden machine directory if absent."""
    root = root or library_root()
    root.mkdir(parents=True, exist_ok=True)
    machine_dir(root).mkdir(exist_ok=True)
    return root


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    """Lowercase, hyphen-joined, filesystem-safe."""
    s = _SLUG_STRIP.sub("-", (text or "").strip().lower()).strip("-")
    return s[:max_len].strip("-") or "untitled"


_UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_filename(title: str, max_len: int = 120) -> str:
    """A human-readable filename that won't upset the filesystem.

    Unlike slugify this keeps spaces and capitalisation -- these become the
    note titles the user actually reads, so 'Primer on Python Decorators.md'
    beats 'primer-on-python-decorators.md'.
    """
    name = _UNSAFE_FILENAME.sub("-", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if len(name) > max_len:
        name = name[:max_len].rsplit(" ", 1)[0].strip()
    return name or "Untitled"
