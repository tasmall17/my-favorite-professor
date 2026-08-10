"""The learning profile: what makes an explanation land for *you*.

The idea the whole app is pointed at. Every exchange with Professor-Claude is
evidence about how you understand things, and over time that evidence should
add up to something good enough that a fresh Claude, handed only this file,
explains things the way you'd want on the first try.

**It lives in ~/.my-favorite-professor, outside any library.** It is about you,
not about shell or Python, so it follows you across subjects and survives
deleting a library. `mfp profile export` bundles it up to hand to another
Claude.

**Evidence first, synthesis second.** Signals are appended as small JSON files
and never rewritten; `profile.md` is derived from them and can be regenerated
at any time. Keeping the raw observations means a bad synthesis is recoverable
and you can read what it actually concluded from.

**The strongest signal is the one you give on purpose.** "That clicked" marks a
specific explanation that worked, which is worth more than any amount of
inference. Passive signals -- asking for more, asking the same thing twice --
fill in the negative space around it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PROFILE_DIR, Config, request_kwargs

PROFILE_NAME = "profile.md"
EVIDENCE_DIR = "evidence"

# Synthesise once this many new signals have accumulated. Often enough to feel
# responsive, rarely enough not to spend a request per click.
SYNTHESIS_EVERY = 4

# How much of an explanation to keep. Enough to see the shape of what worked --
# the analogy, the ordering, the level of detail -- without storing whole
# transcripts.
EXCERPT_CHARS = 1400

KINDS = {
    "clicked":  "An explanation that landed. They said so explicitly.",
    "expanded": "They asked for more after this — it was too thin.",
    "reasked":  "They asked about the same thing again — it didn't land.",
}

SYSTEM = """\
You are maintaining a profile of how one specific person best understands new \
technical material.

You will be given observations: explanations that landed for them, ones that \
were too thin, and ones they had to ask about twice. Write the profile that \
another Claude should read before teaching this person anything.

Write about *how to explain to them*, not about what they are studying. \
"Reaches for the mechanism before the syntax" is useful. "Is learning zsh" is \
not — that changes weekly and is visible anyway.

Ground every claim in the observations. If the evidence is thin, say so and \
keep the profile short; an honest three lines beat a confident page. Never \
invent a preference to fill a section out.

Structure it as:

# How I learn

## What works
Concrete techniques, each with a one-line reason drawn from the evidence.

## What doesn't
Only if the evidence actually shows it.

## Notes
Anything that doesn't fit above — pacing, tolerance for detail, vocabulary.

Address the reader as "they". Keep the whole thing under 400 words. This file \
gets pasted into a system prompt, so every line has to earn its place."""

HEADER = """<!-- Written by my-favorite-professor from your sessions.
     Regenerated from evidence/ -- edit freely, it will be rewritten.
     Hand this to any Claude: "here's how I learn, teach me accordingly." -->

"""


@dataclass
class Signal:
    kind: str
    topic: str
    lesson: str
    question: str
    excerpt: str
    at: str


def profile_dir() -> Path:
    return PROFILE_DIR


def evidence_dir() -> Path:
    return PROFILE_DIR / EVIDENCE_DIR


def profile_path() -> Path:
    return PROFILE_DIR / PROFILE_NAME


def ensure_dirs() -> None:
    evidence_dir().mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ evidence

def record(kind: str, *, topic: str = "", lesson: str = "", question: str = "",
           answer: str = "") -> Path | None:
    """Append one observation. Returns the file written, or None if ignored."""
    if kind not in KINDS:
        return None
    ensure_dirs()

    excerpt = (answer or "").strip()
    if len(excerpt) > EXCERPT_CHARS:
        excerpt = excerpt[:EXCERPT_CHARS].rsplit(" ", 1)[0] + " …"

    payload = {
        "kind": kind,
        "topic": topic,
        "lesson": lesson,
        "question": (question or "").strip()[:400],
        "excerpt": excerpt,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    # Named from the content so the same click twice doesn't count twice.
    digest = hashlib.sha256(
        f"{kind}{topic}{payload['question']}{excerpt[:200]}".encode("utf-8")
    ).hexdigest()[:12]
    path = evidence_dir() / f"{payload['at'][:10]}-{kind}-{digest}.json"
    if path.exists():
        return None
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def signals() -> list[dict]:
    directory = evidence_dir()
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def read() -> str:
    """The profile text, for injecting into a system prompt."""
    path = profile_path()
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def status() -> dict[str, Any]:
    observations = signals()
    counts: dict[str, int] = {}
    for signal in observations:
        counts[signal["kind"]] = counts.get(signal["kind"], 0) + 1
    synthesised = profile_path().is_file()
    return {
        "dir": str(PROFILE_DIR),
        "exists": synthesised,
        "signals": len(observations),
        "counts": counts,
        "words": len(read().split()) if synthesised else 0,
        "next_synthesis_in": max(
            0, SYNTHESIS_EVERY - (len(observations) % SYNTHESIS_EVERY)
        ) if observations else SYNTHESIS_EVERY,
    }


def should_synthesise() -> bool:
    total = len(signals())
    return total > 0 and total % SYNTHESIS_EVERY == 0


# ----------------------------------------------------------------- synthesis

def synthesise(config: Config | None = None) -> str:
    """Rebuild profile.md from every signal on record. Returns the new text."""
    config = config or Config.load()
    observations = signals()
    if not observations:
        raise ValueError("No signals recorded yet.")

    key = config.resolve_key()
    if not key:
        raise ValueError("No API key yet.")

    lines: list[str] = []
    for signal in observations:
        lines.append(
            f"<observation kind=\"{signal['kind']}\" meaning=\"{KINDS[signal['kind']]}\" "
            f"subject=\"{signal.get('topic', '')}\" when=\"{signal['at']}\">\n"
            f"They asked: {signal.get('question', '(no question recorded)')}\n\n"
            f"The explanation:\n{signal.get('excerpt', '')}\n"
            f"</observation>"
        )

    import anthropic

    client = anthropic.Anthropic(api_key=key)
    # Synthesis is a small, well-specified writing task, so it runs at low
    # effort regardless of the model chosen for teaching -- there is no reason
    # to pay Opus-at-max prices to summarise a dozen observations.
    kwargs = request_kwargs(config.model, "low")

    try:
        with client.messages.stream(
            max_tokens=4000,
            system=[{"type": "text", "text": SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content":
                       f"{len(observations)} observations:\n\n" + "\n\n".join(lines)}],
            **kwargs,
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:
        from .professor import _friendly

        raise ValueError(_friendly(exc)) from exc

    if getattr(message, "stop_reason", None) == "refusal":
        raise ValueError("Claude declined to write the profile.")

    text = next((b.text for b in message.content if b.type == "text"), "").strip()
    if not text:
        raise ValueError("The profile came back empty.")

    ensure_dirs()
    profile_path().write_text(HEADER + text + "\n", encoding="utf-8")
    _write_readme()
    return text


def _write_readme() -> None:
    """A note for whoever opens this directory, including a future you."""
    (PROFILE_DIR / "README.md").write_text(
        "# Your learning profile\n\n"
        "`profile.md` describes how you understand new material best. It was\n"
        "written from the observations in `evidence/`, which are appended as\n"
        "you study and never rewritten.\n\n"
        "## Using it somewhere else\n\n"
        "Paste `profile.md` into any Claude conversation:\n\n"
        "> Here's how I learn best. Teach me accordingly.\n"
        "> *(paste)*\n\n"
        "It's yours. Copy it, edit it, or delete `evidence/` to start over --\n"
        "`profile.md` is regenerated from whatever is left.\n",
        encoding="utf-8",
    )
