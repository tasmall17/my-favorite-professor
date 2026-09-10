"""Reading the library: what topics exist, what's in them, what a note says.

Everything here is read-only and cheap. The library on disk is the source of
truth -- there is no database and no index to fall out of step with it, which
is what lets you drop a file into a references directory by hand and have it
show up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

from .capture.paths import CAPTURES_DIR, CLAUDE_REFS_DIR, USER_REFS_DIR
from .capture.topics import stem
from .markdown import parse, to_html

REFS_DIRS = (USER_REFS_DIR, CLAUDE_REFS_DIR)

# Spelled out rather than imported from capture.capture, which would drag
# trafilatura and httpx into a module that only ever reads JSON off disk.
MANIFEST_NAME = "manifest.json"


@dataclass
class Note:
    """One readable piece of material."""

    id: str
    title: str
    topic: str
    source: str
    path: Path
    words: int = 0
    origin: str = ""

    def public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "topic": self.topic,
            "source": self.source,
            "words": self.words,
            "origin": self.origin,
        }


@dataclass
class Topic:
    name: str
    stem: str
    notes: list[Note] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "name": self.name,
            "stem": self.stem,
            "label": self.stem,
            "notes": [n.public() for n in self.notes],
            "counts": {
                "user": sum(n.source == "user" for n in self.notes),
                "claude": sum(n.source == "claude" for n in self.notes),
            },
        }


def note_id(topic: str, source: str, filename: str) -> str:
    """A stable, URL-safe handle for a note.

    Built from the three things that locate it rather than stored anywhere, so
    it survives the library being moved and never needs migrating.

    `topic` is the topic's path within the library, so for a subtopic it holds
    a slash of its own ('js-professor/react'). That makes the id four segments
    rather than three, which is why resolve_note() splits from the right.
    """
    return f"{topic}/{source}/{quote(filename)}"


def _manifests(topic_dir: Path) -> dict[str, dict]:
    """Every manifest in a topic, keyed by the note filename it owns."""
    captures = topic_dir / CAPTURES_DIR
    if not captures.is_dir():
        return {}
    found: dict[str, dict] = {}
    for capture in captures.iterdir():
        path = capture / MANIFEST_NAME
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("note"):
            found[data["note"]] = data
    return found


def read_topic(topic_dir: Path, label: str | None = None) -> Topic:
    """Read one topic directory. `label` is its path within the library.

    A subtopic needs the nested label ('js-professor/react') because note ids
    are built from it: two subtopics of different parents can hold notes with
    the same filename, and a bare leaf name would give them the same id.
    """
    label = label or topic_dir.name
    topic = Topic(name=label, stem=stem(topic_dir.name))
    manifests = _manifests(topic_dir)

    for source, dirname in (("user", USER_REFS_DIR), ("claude", CLAUDE_REFS_DIR)):
        directory = topic_dir / dirname
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            manifest = manifests.get(path.name, {})
            topic.notes.append(Note(
                id=note_id(label, source, path.name),
                title=manifest.get("title") or path.stem,
                topic=label,
                source=source,
                path=path,
                words=int(manifest.get("word_count") or 0),
                origin=manifest.get("original_filename") or manifest.get("url", ""),
            ))

    # Notes predating the provenance split sit flat in the topic root. Showing
    # them as user material is right -- that is what they were.
    for path in sorted(topic_dir.glob("*.md")):
        manifest = manifests.get(path.name, {})
        topic.notes.append(Note(
            id=note_id(label, "user", path.name),
            title=manifest.get("title") or path.stem,
            topic=topic_dir.name,
            source="user",
            path=path,
            words=int(manifest.get("word_count") or 0),
            origin=manifest.get("url", ""),
        ))

    topic.notes.sort(key=lambda n: n.title.lower())
    return topic


def list_topics(root: Path) -> list[Topic]:
    """Every topic in the library, each subtopic following its parent."""
    from .capture.topics import TopicRegistry

    registry = TopicRegistry(root)
    topics: list[Topic] = []
    for directory in registry.topic_dirs():
        topics.append(read_topic(directory))
        for sub_dir in registry.subtopic_dirs(directory):
            topics.append(read_topic(sub_dir, f"{directory.name}/{sub_dir.name}"))
    return topics


def resolve_note(root: Path, identifier: str) -> Note | None:
    """Turn a note id back into a Note, refusing anything outside the library.

    The id arrives from the browser, so the resolved path is checked against
    the topic directory before anything is read -- a `..` in the filename must
    not be able to walk out of the library.
    """
    # From the right: the last two segments are always source and filename,
    # and everything before them is the topic path -- which is one segment for
    # a topic and two for a subtopic.
    parts = identifier.rsplit("/", 2)
    if len(parts) != 3:
        return None
    topic_name, source, filename = parts[0], parts[1], unquote(parts[2])
    if source not in {"user", "claude"} or not filename.endswith(".md"):
        return None

    topic_dir = (root / topic_name).resolve()
    try:
        topic_dir.relative_to(root.resolve())
    except ValueError:
        return None
    if not topic_dir.is_dir():
        return None

    dirname = USER_REFS_DIR if source == "user" else CLAUDE_REFS_DIR
    for candidate in (topic_dir / dirname / filename, topic_dir / filename):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(topic_dir)
        except ValueError:
            continue
        if resolved.is_file():
            manifest = _manifests(topic_dir).get(resolved.name, {})
            return Note(
                id=identifier,
                title=manifest.get("title") or resolved.stem,
                topic=topic_name,
                source=source,
                path=resolved,
                words=int(manifest.get("word_count") or 0),
                origin=manifest.get("original_filename") or manifest.get("url", ""),
            )
    return None


_ASSET_REF = re.compile(
    rf"\]\((?:\./)?{re.escape(CAPTURES_DIR)}/([^/)]+)/assets/([^)\s]+)\)"
)


def _link_assets(body: str, topic: str) -> str:
    """Point image links at the asset endpoint.

    Notes store asset paths relative to the topic directory, which is right for
    reading them in an editor or a vault but means nothing to a browser talking
    to this server. The stored form is left alone; only the copy being rendered
    is rewritten.
    """
    return _ASSET_REF.sub(
        lambda m: f"](/api/asset/{quote(topic)}/{quote(m.group(1))}/{quote(m.group(2))})",
        body,
    )


_LEADING_H1 = re.compile(r"\A\s*#\s+[^\n]*\n+")


def _drop_leading_title(body: str) -> str:
    """Remove the body's own H1.

    Notes are standalone documents and open with their title, which is right
    for reading the file in an editor. The reading pane puts the title in its
    own header, so leaving the H1 in renders it twice.
    """
    return _LEADING_H1.sub("", body, count=1)


def note_document(note: Note) -> dict:
    """The note, rendered, plus enough metadata for the reading pane header."""
    text = note.path.read_text(encoding="utf-8", errors="replace")
    doc = parse(text)
    return {
        **note.public(),
        "title": doc.title if doc.title != "Untitled" else note.title,
        "html": to_html(_link_assets(_drop_leading_title(doc.body), note.topic)),
        "frontmatter": doc.frontmatter,
        # The unrewritten body is what Professor-Claude reads. It has no use for
        # URLs into this server, and the original paths are what match the
        # material on disk if it ever needs to cite a location.
        "text": doc.body,
    }
