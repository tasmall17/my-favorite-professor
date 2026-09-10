"""The topic funnel: decide which directory a capture belongs in.

The problem this solves, in the user's words: "what if it's another page on
python and I already have a py-professor dir with 3 things in it? I wouldn't
want it to make a new directory, I would want it to add it to the py-professor
dir."

The shape is **normalize -> dictionary lookup -> slow path only on a miss**:

    "-python"  ->  normalize -> "python"  ->  dict hit?  -> yes: done, O(1)
                                                         -> no:  fuzzy scan,
                                                                 then WRITE THE
                                                                 RESULT BACK

That write-back matters. An expensive fuzzy match runs once per new alias
*ever*; every future `-python` is a plain dictionary hit. Same idea as
memoization -- caching the answer to "what did I mean by this word?"

Two details make the fuzzy match actually work, and both were found by testing
rather than guessing:

1. **Compare stems.** Strip the "-professor" suffix from both sides first.
   Otherwise every directory shares 10 characters of suffix, which dominates
   the similarity ratio -- inflating unrelated pairs while doing nothing for
   related ones.

2. **Match prefixes in both directions.** The naive rule "slug is a prefix of
   an existing topic" is one-directional and misses the exact scenario above:
   with `py-professor` on disk and `-python` typed, `python` is not a prefix of
   `py`, and difflib scores the pair 0.333 -- under any sane threshold. So it
   would create the duplicate. Checking *either* direction fixes it.

Disk is authoritative; topics.json is a rebuildable cache. The user will create
and rename directories by hand, so a registry that can't be regenerated from a
directory listing will drift out of sync with reality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .paths import (
    CAPTURES_DIR,
    INBOX_TOPIC,
    MACHINE_DIR,
    RESERVED_DIRNAMES,
    TOPIC_STRUCTURE_DIRNAMES,
    TOPIC_SUFFIX,
    ensure_library,
    ensure_topic_layout,
    slugify,
    topics_file,
)

# Similarity at or above this binds an alias to an existing topic.
FUZZY_THRESHOLD = 0.72
# Prefix matching below this length is too trigger-happy to trust. Note this
# constrains the *prefix*, never the alias: '-ltt' and '-rei' are ordinary
# three-character aliases and always have been.
MIN_PREFIX_LEN = 2

# Separates a topic from a subtopic in a CLI flag: `-js.react`.
SUBTOPIC_SEP = "."


@dataclass
class Resolution:
    """What the funnel decided, and why -- so the CLI can explain itself.

    `action` always describes the directory captures actually land in, which
    for `-js.react` is the subtopic. The parent's own outcome rides along in
    `parent_action` so a first `-js.react` on an empty library can report both
    directories it created rather than silently making one of them.
    """

    directory: Path
    alias: str
    action: str  # "exact" | "bound" | "created"
    matched_topic: str | None = None
    score: float | None = None
    # Set only for a subtopic; None means `directory` is a top-level topic.
    parent: Path | None = None
    parent_action: str | None = None

    @property
    def is_new_directory(self) -> bool:
        return self.action == "created"

    @property
    def is_subtopic(self) -> bool:
        return self.parent is not None

    @property
    def label(self) -> str:
        """The topic's identity everywhere outside the filesystem.

        'js-professor', or 'js-professor/react' for a subtopic. This is what
        goes in the manifest, the note id and the Downloads mirror -- all three
        need a subtopic note to be distinguishable from a parent note that
        happens to share its filename, and a bare 'react' would not be.
        """
        if self.parent is None:
            return self.directory.name
        return f"{self.parent.name}/{self.directory.name}"


def stem(name: str) -> str:
    """'python-professor' -> 'python'.  '-py' -> 'py'."""
    s = name.strip().lower().lstrip("-")
    if s.endswith(TOPIC_SUFFIX):
        s = s[: -len(TOPIC_SUFFIX)]
    return s.strip("-")


def normalize_alias(raw: str) -> str:
    """Turn a raw CLI token like '--Py3' into the dictionary key 'py3'."""
    return slugify(stem(raw))


def split_alias(raw: str) -> tuple[str, str | None]:
    """'-js.react' -> ('js', 'react').  '-js' -> ('js', None).

    This has to run *before* normalize_alias(), and that ordering is the whole
    reason it is a separate function: slugify() rewrites '.' to '-', so a
    dotted token handed to it whole comes back as the single topic name
    'js-react' -- a plausible-looking directory that is not what anyone asked
    for and gives no hint that nesting was ever attempted.

    Nesting stops at one level. A second dot is part of the subtopic name
    rather than a grandchild, because the layout has exactly two levels and
    quietly inventing a third would put captures somewhere find_capture()
    cannot see them.
    """
    token = raw.strip().lstrip("-")
    head, sep, tail = token.partition(SUBTOPIC_SEP)
    if not sep:
        return token, None
    tail = tail.replace(SUBTOPIC_SEP, "-").strip("-")
    return head, (tail or None)


def normalize_key(raw: str) -> str:
    """The dictionary key for a raw flag, dot intact: '-JS.React' -> 'js.react'."""
    head, tail = split_alias(raw)
    head = normalize_alias(head)
    if tail is None:
        return head
    return f"{head}{SUBTOPIC_SEP}{normalize_alias(tail)}"


def dir_name_for(alias: str) -> str:
    """The directory a brand-new topic gets: 'py' -> 'py-professor'.

    'inbox' is the one exception. It is where untagged captures land, not a
    subject, and 'inbox-professor' reads like nonsense.
    """
    base = slugify(stem(alias))
    if base == INBOX_TOPIC:
        return INBOX_TOPIC
    return f"{base}{TOPIC_SUFFIX}"


def sub_dir_name_for(alias: str) -> str:
    """The directory a brand-new subtopic gets: 'react' -> 'react'.

    No '-professor' suffix, unlike a top-level topic. The professor is the
    parent; a subtopic is one of the subjects that professor covers, and
    'js-professor/react-professor/' would claim two of them.
    """
    return slugify(stem(alias)) or INBOX_TOPIC


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def matches(a: str, b: str) -> tuple[bool, float]:
    """Do two stems refer to the same topic?

    Returns (matched, score). Prefix hits report their ratio for display but
    bind regardless of it -- 'py' vs 'python' only scores 0.5, which is why
    the ratio alone is not enough.
    """
    if not a or not b:
        return False, 0.0
    score = similarity(a, b)
    if a == b:
        return True, 1.0
    if len(a) >= MIN_PREFIX_LEN and b.startswith(a):
        return True, score
    if len(b) >= MIN_PREFIX_LEN and a.startswith(b):
        return True, score
    return score >= FUZZY_THRESHOLD, score


class TopicRegistry:
    """The alias dictionary, backed by .mfp/topics.json."""

    def __init__(self, root: Path | None = None):
        self.root = ensure_library(root)
        self.path = topics_file(self.root)
        self._aliases: dict[str, str] = {}
        self._load()

    # ---------------------------------------------------------------- storage

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._aliases = dict(data.get("aliases", {}))
            except (json.JSONDecodeError, OSError):
                # A corrupt cache is not a crash -- disk is authoritative and
                # we can rebuild the whole thing from a directory listing.
                self._aliases = {}
        self._prune_missing()
        self._absorb_disk()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": (
                "Rebuildable cache of alias -> topic directory. Disk is "
                "authoritative; delete this file and it regenerates."
            ),
            "aliases": dict(sorted(self._aliases.items())),
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ disk

    def topic_dirs(self) -> list[Path]:
        """Visible topic directories on disk, which are the real source of truth.

        RESERVED_DIRNAMES is excluded here rather than at the call sites because
        _absorb_disk() runs on every load and would otherwise mint an alias for
        anything sitting beside the topics -- silently turning the learning
        profile into a subject you could capture pages into.
        """
        if not self.root.exists():
            return []
        return sorted(
            p
            for p in self.root.iterdir()
            if p.is_dir()
            and not p.name.startswith(".")
            and p.name != MACHINE_DIR
            and p.name not in RESERVED_DIRNAMES
        )

    def subtopic_dirs(self, topic_dir: Path) -> list[Path]:
        """Visible subtopic directories inside one topic.

        Anything that is not structure is a subtopic. Defining it by exclusion
        rather than by a marker file is what lets `mkdir js-professor/react`
        work as well as `mfp -n js.react` -- the same "disk is authoritative"
        rule the top level already follows.
        """
        if not topic_dir.is_dir():
            return []
        return sorted(
            p
            for p in topic_dir.iterdir()
            if p.is_dir()
            and not p.name.startswith(".")
            and p.name not in TOPIC_STRUCTURE_DIRNAMES
        )

    def _rel(self, directory: Path) -> str:
        """The stored form of a directory: 'js-professor', 'js-professor/react'.

        Aliases have always mapped to something joinable onto the root, so a
        subtopic needs no new storage shape and no migration -- a top-level
        entry is its own relative path, byte for byte what older topics.json
        files already contain.
        """
        return directory.relative_to(self.root).as_posix()

    def _prune_missing(self) -> None:
        """Drop aliases pointing at directories the user deleted or renamed."""
        for alias, name in list(self._aliases.items()):
            if not (self.root / name).is_dir():
                del self._aliases[alias]

    def _absorb_disk(self) -> None:
        """Every topic and subtopic directory answers to its own stem, for free."""
        for d in self.topic_dirs():
            self._aliases.setdefault(stem(d.name), d.name)
            for sub_dir in self.subtopic_dirs(d):
                key = f"{stem(d.name)}{SUBTOPIC_SEP}{stem(sub_dir.name)}"
                self._aliases.setdefault(key, self._rel(sub_dir))

    # ------------------------------------------------------------- resolution

    def resolve(self, raw_alias: str, *, force_new: bool = False) -> Resolution:
        """Map a CLI topic flag to a directory, creating or binding as needed.

        `-js` resolves one level, `-js.react` two. The second level runs the
        same funnel against the parent's subtopics, so everything true of
        topics is true of subtopics: '-js.rea' finds an existing react/, the
        binding is written back, and only a genuine miss creates a directory.
        """
        head, tail = split_alias(raw_alias)
        if tail is None:
            return self._resolve_topic(head, force_new=force_new)

        # force_new is deliberately NOT passed to the parent. On a dotted flag
        # it means "a separate subtopic", and forcing the parent as well would
        # answer `--new-topic -python.async`, with py-professor already on
        # disk, by creating a second python-professor/ to hold it -- the exact
        # duplicate the funnel exists to prevent, in the one command whose
        # whole purpose is to be deliberate about creating a directory.
        parent = self._resolve_topic(head)
        return self._resolve_subtopic(parent, tail, force_new=force_new)

    def _resolve_topic(self, raw_alias: str, *, force_new: bool = False) -> Resolution:
        alias = normalize_alias(raw_alias)
        if not alias:
            raise ValueError("empty topic alias")

        # 1. Exact hit -- the fast path, and after the first use of any alias
        #    this is the only path that ever runs.
        if not force_new and alias in self._aliases:
            directory = self.root / self._aliases[alias]
            if directory.is_dir():
                return Resolution(directory, alias, "exact")

        # 2. Miss -> fuzzy scan against what is actually on disk.
        if not force_new:
            best = self._best_match(alias, self.topic_dirs())
            if best is not None:
                directory, score = best
                self._aliases[alias] = self._rel(directory)
                self.save()
                return Resolution(
                    directory, alias, "bound",
                    matched_topic=directory.name, score=score,
                )

        # 3. No match -> a genuinely new topic.
        directory = ensure_topic_layout(self.root / dir_name_for(alias))
        self._aliases[alias] = self._rel(directory)
        self.save()
        return Resolution(directory, alias, "created")

    def _resolve_subtopic(self, parent: Resolution, raw_sub: str, *,
                          force_new: bool = False) -> Resolution:
        """Second level of the funnel, inside an already-resolved parent.

        force_new deliberately applies to the subtopic only. `--new-topic` on
        `-js.react` means "a separate react/", not "a second js-professor/" --
        forcing the parent too would strand the new subtopic in a duplicate
        parent nobody asked for.
        """
        sub_alias = normalize_alias(raw_sub)
        if not sub_alias:
            raise ValueError("empty subtopic alias")
        key = f"{parent.alias}{SUBTOPIC_SEP}{sub_alias}"

        def resolved(directory: Path, action: str, **extra) -> Resolution:
            return Resolution(
                directory, key, action,
                parent=parent.directory, parent_action=parent.action, **extra,
            )

        if not force_new and key in self._aliases:
            directory = self.root / self._aliases[key]
            if directory.is_dir():
                return resolved(directory, "exact")

        if not force_new:
            best = self._best_match(sub_alias, self.subtopic_dirs(parent.directory))
            if best is not None:
                directory, score = best
                self._aliases[key] = self._rel(directory)
                self.save()
                return resolved(directory, "bound",
                                matched_topic=directory.name, score=score)

        directory = ensure_topic_layout(
            parent.directory / sub_dir_name_for(sub_alias)
        )
        self._aliases[key] = self._rel(directory)
        self.save()
        return resolved(directory, "created")

    def _best_match(self, alias: str,
                    directories: list[Path]) -> tuple[Path, float] | None:
        """Highest-scoring directory in `directories` that matches, if any.

        Takes its candidates as an argument rather than reading topic_dirs()
        itself, which is what lets the identical rule serve both levels: the
        subtopic pass simply hands it the parent's children.
        """
        target = stem(alias)
        best: tuple[Path, float] | None = None
        for directory in directories:
            rel = self._rel(directory)
            # Compare against the directory's own stem and every alias already
            # bound to it, so '-py3' can find a dir reached earlier via '-py'.
            # A subtopic alias is stored dotted ('js.react'), and only its last
            # segment names this directory -- comparing the whole key would
            # score the parent's name as part of the child's.
            candidates = {stem(directory.name)}
            candidates.update(
                a.rpartition(SUBTOPIC_SEP)[2] or a
                for a, name in self._aliases.items() if name == rel
            )
            for candidate in candidates:
                ok, score = matches(target, stem(candidate))
                if ok and (best is None or score > best[1]):
                    best = (directory, score)
        return best

    # ------------------------------------------------------------ maintenance

    def link(self, alias: str, topic_dir_name: str) -> Path:
        """Bind an alias by hand -- the fix for any bad automatic guess.

        Both sides accept the dotted form, so a subtopic can be linked the same
        way a topic can: `--link hooks=js-professor/react`, or
        `--link js.hooks=js-professor/react`.
        """
        alias = normalize_key(alias)
        target = topic_dir_name.strip().strip("/")
        directory = self.root / target
        if not directory.is_dir():
            # Accept a bare stem too: `--link ml=machine-learning` should work.
            # For a nested target only the parent carries the suffix, so the
            # stem is expanded on the first segment alone.
            head, _, tail = target.partition("/")
            alt = self.root / dir_name_for(head) / tail if tail else \
                self.root / dir_name_for(head)
            if alt.is_dir():
                directory = alt
            else:
                raise FileNotFoundError(f"no such topic directory: {topic_dir_name}")
        self._aliases[alias] = self._rel(directory)
        self.save()
        return directory

    def rebuild(self) -> None:
        """Resynchronise the cache with what is actually on disk.

        Hand-made links are kept as long as their target directory still
        exists. A rebuild that discarded them would quietly undo every
        `--link ml=machine-learning-professor` the user ever made -- and those
        are exactly the bindings the fuzzy matcher can't rediscover on its own,
        since string similarity doesn't do synonyms.
        """
        self._aliases = {
            alias: name
            for alias, name in self._aliases.items()
            if (self.root / name).is_dir()
        }
        self._absorb_disk()
        self.save()

    def summary(self) -> list[tuple[str, list[str], int, int]]:
        """(name, aliases, capture count, depth) for `--topics`.

        Subtopics follow their parent, at depth 1, so the listing reads in the
        same shape as the directory tree it describes.
        """
        rows: list[tuple[str, list[str], int, int]] = []
        for directory in self.topic_dirs():
            rows.append(self._summary_row(directory, 0))
            for sub_dir in self.subtopic_dirs(directory):
                rows.append(self._summary_row(sub_dir, 1))
        return rows

    def _summary_row(self, directory: Path, depth: int) -> tuple[str, list[str], int, int]:
        rel = self._rel(directory)
        aliases = sorted(a for a, name in self._aliases.items() if name == rel)
        captures = directory / CAPTURES_DIR
        count = (
            len([p for p in captures.iterdir() if p.is_dir()])
            if captures.is_dir() else 0
        )
        return (directory.name, aliases, count, depth)
