"""Professor-Claude.

Answers questions about whatever you're reading, using your material as the
source. Three things shape the design:

**Your references come first.** The section you're looking at is put in the
system prompt verbatim. Claude is told to answer from it, to say plainly when
the answer isn't in there, and to mark anything it adds from its own knowledge
as exactly that -- so you always know whether you're reading your source or a
gloss on it.

**Three paragraphs, then stop.** Long answers are how you end up nodding along
without understanding. If there's more to say, the answer stops and offers it,
and "tell me more" continues from where it left off.

**The source text is cached.** It is re-sent on every turn -- the API is
stateless -- and for a long chapter it dwarfs the conversation. A cache
breakpoint on the last system block means you pay for it once per five minutes
rather than once per question. The minimum cacheable prefix differs per model
and falling under it silently doesn't cache, so `usage` is reported back to the
caller and surfaced in the UI rather than taken on trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import MODELS, Config, model_spec, request_kwargs

# Long enough for three paragraphs several times over, with headroom for
# adaptive thinking -- max_tokens caps thinking and visible text together, so a
# tight limit here truncates the answer rather than shortening it. Brevity is
# the prompt's job, not this number's.
MAX_TOKENS = 8000

SYSTEM = """\
You are Professor-Claude, helping someone learn from material they chose \
themselves. You are patient, concrete, and you never pad.

## Your source

The reading below is what the person is looking at right now. It is the \
primary source and it outranks your own knowledge.

- Answer from the reading wherever it covers the question.
- If the reading does not cover it, say so in a short sentence, then answer \
from your own knowledge and mark that part clearly (for example: "That isn't \
in your material, but generally...").
- Never invent something and attribute it to their material.

## How much to say

**Three paragraphs maximum.** This is a hard ceiling, not a target -- one \
good paragraph beats three padded ones. Prefer a concrete example over an \
abstract description; prefer their material's own vocabulary over yours.

If a full answer genuinely needs more, give the most useful three paragraphs \
and end with one short line naming what you left out, so they can ask for it. \
Do not write a fourth paragraph instead.

Skip preambles. Don't restate the question. Don't close with a summary of what \
you just said. If code helps, show the smallest version that makes the point.

## What you're aiming at

They want to be able to *use* this, not recite it. Where it fits naturally, \
connect an idea to what it lets them do.\
"""

SEARCH_RULE = """

## Looking things up

You can search the web, but their material comes first. Only search when the \
reading genuinely does not cover what they asked and the answer would be \
guesswork otherwise. Say that you are doing it and why, in a few words.

Anything you find gets saved into their library alongside their own material, \
so prefer primary sources -- official documentation, specifications, the \
project's own pages -- over blog posts and content farms. Cite what you used."""

GOAL_TEMPLATE = """
## What they're working towards

{goal}

Pitch explanations at that level: enough to get there, without a detour into \
material they don't need yet."""

READING_TEMPLATE = """\
# The reading: {title}

{body}"""

EXPAND_INSTRUCTION = (
    "Continue from where you stopped, with the part you named as left out. "
    "Same three-paragraph ceiling. Don't recap what you already said."
)


@dataclass
class Turn:
    role: str
    content: str


@dataclass
class Answer:
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    refused: bool = False
    error: str = ""


class ProfessorError(Exception):
    pass


def _client(config: Config):
    key = config.resolve_key()
    if not key:
        raise ProfessorError(
            "No API key yet. Add one in Settings, or set ANTHROPIC_API_KEY."
        )
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ProfessorError("the anthropic package is not installed") from exc
    return anthropic.Anthropic(api_key=key)


def build_system(*, reading_title: str, reading_text: str, goal: str = "",
                 profile: str = "", can_search: bool = False) -> list[dict[str, Any]]:
    """Assemble the system prompt, with the cache breakpoint in the right place.

    Order matters and is not cosmetic. Caching is a prefix match, so the blocks
    run most-stable to least-stable: the fixed instructions, then the profile
    and goal, then the reading. The breakpoint goes on the last block so the
    whole thing is cached together, and the volatile part -- the question --
    stays in `messages`, after everything cached.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": SYSTEM + (SEARCH_RULE if can_search else "")}
    ]

    if profile.strip():
        blocks.append({
            "type": "text",
            "text": (
                "## How this person learns\n\n"
                "Built from what has actually worked with them before. Lean on "
                "it, but drop any part that fights the question in front of "
                "you.\n\n" + profile.strip()
            ),
        })

    if goal.strip():
        blocks.append({"type": "text", "text": GOAL_TEMPLATE.format(goal=goal.strip())})

    blocks.append({
        "type": "text",
        "text": READING_TEMPLATE.format(title=reading_title, body=reading_text),
        "cache_control": {"type": "ephemeral"},
    })
    return blocks


def _messages(history: list[Turn], question: str) -> list[dict[str, str]]:
    messages = [{"role": t.role, "content": t.content} for t in history if t.content]
    messages.append({"role": "user", "content": question})
    # The API rejects a leading assistant turn, which a truncated history can
    # produce.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def _usage(raw: Any) -> dict[str, Any]:
    """Pull out the cache counters, which are how caching gets verified."""
    if raw is None:
        return {}
    get = (lambda k: getattr(raw, k, None)) if not isinstance(raw, dict) else raw.get
    return {
        "input_tokens": get("input_tokens") or 0,
        "output_tokens": get("output_tokens") or 0,
        "cache_read_input_tokens": get("cache_read_input_tokens") or 0,
        "cache_creation_input_tokens": get("cache_creation_input_tokens") or 0,
    }


def stream_answer(
    *,
    config: Config,
    reading_title: str,
    reading_text: str,
    question: str,
    history: list[Turn] | None = None,
    goal: str = "",
    profile: str = "",
    can_search: bool = False,
) -> Iterator[dict[str, Any]]:
    """Stream one answer, yielding `{"type": ...}` events for the transport.

    Events: `delta` (text), `done` (usage and the full text), `error`. The
    caller turns them into server-sent events; nothing here knows about HTTP.
    """
    spec = model_spec(config.model)
    try:
        client = _client(config)
    except ProfessorError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    system = build_system(
        reading_title=reading_title, reading_text=reading_text,
        goal=goal, profile=profile, can_search=can_search,
    )
    kwargs = request_kwargs(config.model, config.effort)
    if can_search:
        # Declared here rather than in request_kwargs because searching is a
        # per-conversation choice, not a property of the model.
        kwargs["tools"] = [{"type": spec.web_search_tool, "name": "web_search",
                            "max_uses": 4}]

    collected: list[str] = []
    try:
        with client.messages.stream(
            max_tokens=MAX_TOKENS,
            system=system,
            messages=_messages(history or [], question),
            **kwargs,
        ) as stream:
            for text in stream.text_stream:
                collected.append(text)
                yield {"type": "delta", "text": text}
            final = stream.get_final_message()
    except Exception as exc:
        yield {"type": "error", "message": _friendly(exc)}
        return

    # A refusal is a successful HTTP 200 with no usable content, so it has to
    # be checked before the text is treated as an answer.
    if getattr(final, "stop_reason", None) == "refusal":
        yield {
            "type": "error",
            "message": "Claude declined to answer that one. Try rephrasing it.",
            "refused": True,
        }
        return

    usage = _usage(getattr(final, "usage", None))
    usage["cache_min_tokens"] = spec.cache_min_tokens
    usage["model"] = getattr(final, "model", config.model)
    yield {
        "type": "done",
        "text": "".join(collected),
        "usage": usage,
        "sources": cited_sources(final),
    }


def cited_sources(message: Any) -> list[dict[str, str]]:
    """Pages the model actually consulted, pulled off the response.

    Server-tool errors arrive as a successful response whose result `content`
    is a single error object rather than a list, so the shape is checked before
    iterating -- an unreachable search should cost the citation list, not the
    whole answer.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        results = getattr(block, "content", None)
        if not isinstance(results, list):
            continue
        for result in results:
            url = getattr(result, "url", "")
            if url and url not in seen:
                seen.add(url)
                found.append({"url": url, "title": getattr(result, "title", "") or url})
    return found


def verify_key(key: str) -> dict[str, Any]:
    """Check a key before saving it, and report which models it can reach.

    Uses the models endpoint rather than sending a message: it needs the same
    authentication, costs nothing, and answers the more useful question. A key
    that authenticates but can't reach the selected model is a real case --
    catching it here beats discovering it on the first question.
    """
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "Paste a key first."}
    if not key.startswith("sk-ant-"):
        return {"ok": False,
                "message": "Anthropic keys start with sk-ant-. That looks like "
                           "something else."}

    try:
        import anthropic
    except ImportError:  # pragma: no cover - declared dependency
        return {"ok": False, "message": "The anthropic package is not installed."}

    try:
        available = {m.id for m in anthropic.Anthropic(api_key=key).models.list()}
    except Exception as exc:
        return {"ok": False, "message": _friendly(exc)}

    usable = [m for m in MODELS if m in available]
    if not usable:
        return {"ok": True, "models": [],
                "message": "That key works, but it can't reach Opus, Sonnet or "
                           "Haiku. Check which workspace it belongs to."}
    return {"ok": True, "models": usable,
            "message": f"Key works — {len(usable)} of 3 models available."}


def _friendly(exc: Exception) -> str:
    """Turn an SDK exception into something worth showing in the UI."""
    name = type(exc).__name__
    if "Authentication" in name:
        return "Anthropic rejected that key. Check you copied all of it."
    if "PermissionDenied" in name:
        return "That key is valid but not allowed to do this. Check its workspace."
    if "RateLimit" in name:
        return "Rate limited by the API. Give it a moment and try again."
    if "Connection" in name or "Timeout" in name:
        return "Couldn't reach the API. Check your connection."
    if "NotFound" in name:
        return f"The model {name} isn't available to this key."
    return f"{name}: {exc}"
