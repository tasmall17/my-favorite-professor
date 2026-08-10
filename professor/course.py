"""Turning a pile of references into a course.

The difference between a folder of files and a course is order: what to read
first, what depends on what, and when you've covered enough of something to
move on. That ordering is what this module asks Claude to work out, once, and
caches in `.mfp-course/syllabus.json`.

Three things keep it honest:

**Every lesson cites its sources.** A lesson that can't point at a note in your
library is a lesson about material you don't have, which is worse than useless
-- it teaches you something you can't go and check. Lessons whose citations
don't resolve are dropped after synthesis rather than trusted.

**Your goal changes the map.** "Enough to live in a terminal" and "enough to put
it on a CV" are different courses over identical material, so the goal goes into
the prompt and is recorded in the syllabus that came out of it.

**Progress survives regeneration.** Add a file, rebuild the course, and what
you'd already read should still be marked read. Progress lives in its own file
keyed by a stable hash of the lesson title, not by position.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .capture.paths import course_dir
from .config import Config, request_kwargs
from .library import Topic, read_topic

SYLLABUS_NAME = "syllabus.json"
PROGRESS_NAME = "progress.json"

# How much of each note to send. Whole libraries would fit in the context
# window, but the syllabus only needs enough of each source to place it -- and
# this is billed per rebuild. Truncation is reported rather than silent.
CHARS_PER_NOTE = 9000
MAX_TOTAL_CHARS = 160_000

MAX_TOKENS = 32_000

SYSTEM = """\
You are building a study course from one person's own reference material.

You will be given every source they have on a subject. Produce a syllabus: an \
ordered set of modules, each holding ordered lessons, that takes them from \
where they are to the goal they named.

Rules that matter:

1. **Only teach what the sources cover.** You are organising their material, \
not writing a curriculum from your own knowledge. If an obvious prerequisite \
is missing from their sources, do not invent a lesson for it -- instead note \
it in the module's `gaps` field so they know to go and find it.

2. **Cite real sources.** Every lesson lists the `note_id`s it draws on, taken \
verbatim from the source list. A lesson with no citation will be discarded.

3. **Order by dependency, not by file.** A single long reference usually splits \
across several lessons, and one lesson often draws on several references. Do \
not simply mirror the file list back.

4. **Pitch it at the goal.** Someone who wants to be dangerous in a terminal \
needs a different path through the same pages than someone preparing for an \
interview.

5. **Lessons are a sitting's work.** Roughly 10-25 minutes each. If something \
needs longer, it is two lessons.

Titles are plain and concrete: "Redirecting output and errors", not \
"Understanding I/O concepts". Summaries say what the reader will be able to do, \
in one sentence."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": "Two sentences: what this course covers and where it ends.",
        },
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Prerequisites the sources do not cover.",
                    },
                    "lessons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "subtopics": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "note_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "minutes": {"type": "integer"},
                            },
                            "required": ["title", "summary", "subtopics",
                                         "note_ids", "minutes"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "summary", "gaps", "lessons"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "modules"],
    "additionalProperties": False,
}


class CourseError(Exception):
    pass


# ------------------------------------------------------------------ identity

def lesson_key(module_title: str, lesson_title: str) -> str:
    """A stable id for progress, derived from the titles rather than position.

    Rebuilding after adding a source reorders things; keying progress on
    position would silently mark the wrong lessons read.
    """
    raw = f"{module_title}\x00{lesson_title}".lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def fingerprint(topic: Topic) -> str:
    """Identifies the corpus a syllabus was built from.

    Changes when a note is added, removed or re-ingested with a different
    length -- which is exactly when the syllabus is worth rebuilding.
    """
    parts = sorted(f"{n.id}:{n.words}" for n in topic.notes)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------- files

def syllabus_path(topic_dir: Path) -> Path:
    return course_dir(topic_dir) / SYLLABUS_NAME


def progress_path(topic_dir: Path) -> Path:
    return course_dir(topic_dir) / PROGRESS_NAME


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_syllabus(topic_dir: Path) -> dict:
    return _read_json(syllabus_path(topic_dir))


def load_progress(topic_dir: Path) -> dict:
    return _read_json(progress_path(topic_dir)).get("lessons", {})


def set_progress(topic_dir: Path, key: str, done: bool) -> dict:
    path = progress_path(topic_dir)
    data = _read_json(path)
    lessons = data.setdefault("lessons", {})
    if done:
        lessons[key] = {"done": True, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    else:
        lessons.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return lessons


def is_stale(topic_dir: Path, topic: Topic) -> bool:
    syllabus = load_syllabus(topic_dir)
    if not syllabus:
        return False
    return syllabus.get("fingerprint") != fingerprint(topic)


# ----------------------------------------------------------------- synthesis

_HEADING = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)


def _corpus(topic: Topic) -> tuple[str, list[str]]:
    """Render the sources for the prompt. Returns (text, truncated note ids)."""
    blocks: list[str] = []
    truncated: list[str] = []
    total = 0

    for note in topic.notes:
        try:
            text = note.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        from .markdown import split_frontmatter

        _, body = split_frontmatter(text)
        body = body.strip()

        if len(body) > CHARS_PER_NOTE:
            # Keep the head, plus the headings from the tail, so the model can
            # still see the shape of what it isn't reading in full.
            tail = "\n".join(f"(section) {h}" for h in _HEADING.findall(body)[-40:])
            body = body[:CHARS_PER_NOTE] + "\n\n[...truncated...]\n" + tail
            truncated.append(note.id)

        block = (f"<source id=\"{note.id}\" title=\"{note.title}\" "
                 f"words=\"{note.words}\" provenance=\"{note.source}\">\n"
                 f"{body}\n</source>")
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        blocks.append(block)
        total += len(block)

    return "\n\n".join(blocks), truncated


def _prune(modules: list[dict], valid_ids: set[str]) -> tuple[list[dict], int]:
    """Drop lessons whose citations don't resolve to a real note.

    A hallucinated citation is the one failure that would send someone looking
    for material they don't have, so it is checked rather than trusted.
    """
    dropped = 0
    cleaned: list[dict] = []
    for module in modules:
        lessons = []
        for lesson in module.get("lessons", []):
            cited = [i for i in lesson.get("note_ids", []) if i in valid_ids]
            if not cited:
                dropped += 1
                continue
            lesson["note_ids"] = cited
            lesson["key"] = lesson_key(module.get("title", ""), lesson.get("title", ""))
            lessons.append(lesson)
        if lessons:
            module["lessons"] = lessons
            cleaned.append(module)
    return cleaned, dropped


def build(topic_dir: Path, *, config: Config | None = None,
          goal: str = "") -> dict:
    """Synthesise a syllabus for one topic and cache it. Returns the syllabus."""
    config = config or Config.load()
    topic = read_topic(topic_dir)
    if not topic.notes:
        raise CourseError("Add some material to this subject first.")

    key = config.resolve_key()
    if not key:
        raise CourseError("No API key yet. Add one in Settings.")

    corpus, truncated = _corpus(topic)
    if not corpus.strip():
        raise CourseError("None of the material in this subject could be read.")

    goal = goal.strip() or config.goals.get(topic.name, "").strip()
    goal_line = goal or ("Become genuinely useful with this -- able to do real "
                         "work in it without looking everything up.")

    import anthropic

    client = anthropic.Anthropic(api_key=key)
    kwargs = request_kwargs(config.model, config.effort)

    prompt = (
        f"# Their goal\n\n{goal_line}\n\n"
        f"# Their sources ({len(topic.notes)} in {topic.name})\n\n{corpus}"
    )

    try:
        # Streamed because a syllabus over a large corpus is a long generation,
        # and a non-streaming request this size risks an HTTP timeout.
        with client.messages.stream(
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_config={**kwargs.pop("output_config", {}),
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            **kwargs,
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:
        from .professor import _friendly

        raise CourseError(_friendly(exc)) from exc

    if getattr(message, "stop_reason", None) == "refusal":
        raise CourseError("Claude declined to build a course from this material.")

    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CourseError("The syllabus came back malformed. Try again.") from exc

    modules, dropped = _prune(data.get("modules", []), {n.id for n in topic.notes})
    if not modules:
        raise CourseError(
            "No lesson could be tied back to your material. That usually means "
            "the sources are too short to build a course from yet."
        )

    usage = getattr(message, "usage", None)
    syllabus = {
        "topic": topic.name,
        "goal": goal_line,
        "overview": data.get("overview", ""),
        "modules": modules,
        "fingerprint": fingerprint(topic),
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": getattr(message, "model", config.model),
        "sources": len(topic.notes),
        "truncated": truncated,
        "dropped_lessons": dropped,
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
        } if usage else {},
    }

    path = syllabus_path(topic_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(syllabus, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return syllabus


def public(topic_dir: Path, topic: Topic) -> dict:
    """The syllabus plus progress, shaped for the browser."""
    syllabus = load_syllabus(topic_dir)
    if not syllabus:
        return {"exists": False, "stale": False}

    progress = load_progress(topic_dir)
    modules = syllabus.get("modules", [])
    total = sum(len(m.get("lessons", [])) for m in modules)
    done = sum(
        1 for m in modules for l in m.get("lessons", [])
        if progress.get(l.get("key", ""), {}).get("done")
    )

    for module in modules:
        for lesson in module.get("lessons", []):
            lesson["done"] = bool(progress.get(lesson.get("key", ""), {}).get("done"))

    return {
        **syllabus,
        "exists": True,
        "stale": syllabus.get("fingerprint") != fingerprint(topic),
        "total_lessons": total,
        "done_lessons": done,
    }
