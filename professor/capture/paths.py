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

# Where to look for an existing library, in order. The original tool hardcoded
# ~/Code, which resolves only because macOS is case-insensitive -- on Linux it
# simply misses a library sitting in ~/code. Both spellings are checked so the
# same default works on either filesystem.
_LIBRARY_PARENTS = ("code", "Code", "Documents")

DEFAULT_LIBRARY = Path.home() / "code" / LIBRARY_NAME

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

# The structural directories that make up a topic. A subtopic is simply any
# *other* visible directory inside one, so these four names are the only ones a
# subtopic may not be called -- otherwise `-js.usr-references-provided` would
# hand back the notes directory itself as somewhere to file notes.
#
# Only the two visible names can actually collide in practice; the dot-prefixed
# pair are filtered out by the same rule that hides them everywhere else. They
# are listed anyway so this set answers "what is structure?" rather than "what
# does the current filter happen to miss?".
TOPIC_STRUCTURE_DIRNAMES = frozenset({
    USER_REFS_DIR,
    CLAUDE_REFS_DIR,
    CAPTURES_DIR,
    COURSE_DIR,
})

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


def library_root() -> Path:
    """The library directory. MFP_LIBRARY overrides everything else."""
    env = os.environ.get("MFP_LIBRARY", "").strip()
    if env:
        return Path(env).expanduser()

    for parent in _LIBRARY_PARENTS:
        candidate = Path.home() / parent / LIBRARY_NAME
        if candidate.is_dir() and not _looks_like_source_checkout(candidate):
            return candidate
    return DEFAULT_LIBRARY


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
