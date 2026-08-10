"""Settings, and the API key.

The key never reaches the browser. It is read here, in the server process, and
used to build the Anthropic client; no endpoint returns it and no template
interpolates it. The browser asks "is a key configured?" and gets a boolean.

That is why the app is a local server rather than a static page: calling
Anthropic straight from the browser needs the
`anthropic-dangerous-direct-browser-access` header and puts your key inside
reach of anything that can run script on the page.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "my-favorite-professor"

CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

# The profile is deliberately outside both the library and the repo: it is
# about *you*, not about any one subject, so it should outlive any particular
# library and follow you to the next one.
PROFILE_HOME = Path.home() / f".{APP_NAME}"
PROFILE_DIR = PROFILE_HOME / "usr-learning-profile"

DOWNLOADS_MIRROR = Path.home() / "Downloads" / APP_NAME


# --------------------------------------------------------------------- models

@dataclass(frozen=True)
class ModelSpec:
    """What a model will actually accept.

    This table exists because the request parameters are *not* uniform across
    the three models offered in Settings, and the mismatches are 400s rather
    than warnings:

      * `output_config.effort` is generally available on Opus 5 and Sonnet 5.
        Sending it to Haiku 4.5 is an error, so the effort control has to
        disappear from the UI when Haiku is selected -- not merely be ignored.
      * `thinking: {type: "adaptive"}` is likewise not a Haiku 4.5 parameter.
      * The minimum cacheable prefix differs, and falling under it doesn't warn
        -- it just silently doesn't cache. Since a lesson's source text is
        re-sent on every turn, that difference is most of the running cost.
    """

    id: str
    label: str
    blurb: str
    supports_effort: bool
    supports_adaptive_thinking: bool
    cache_min_tokens: int
    efforts: tuple[str, ...] = ()
    # Which web-search tool version this model takes. The dynamic-filtering
    # variant is newer-models-only; older ones keep the basic tool, and sending
    # the wrong version is an error rather than a downgrade.
    web_search_tool: str = "web_search_20250305"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "blurb": self.blurb,
            "supports_effort": self.supports_effort,
            "efforts": list(self.efforts),
            "cache_min_tokens": self.cache_min_tokens,
        }


EFFORTS = ("low", "medium", "high", "xhigh", "max")

MODELS: dict[str, ModelSpec] = {
    "claude-opus-5": ModelSpec(
        id="claude-opus-5",
        label="Opus",
        blurb="Break it right down. Best when the topic is new to you.",
        supports_effort=True,
        supports_adaptive_thinking=True,
        cache_min_tokens=512,
        efforts=EFFORTS,
        web_search_tool="web_search_20260209",
    ),
    "claude-sonnet-5": ModelSpec(
        id="claude-sonnet-5",
        label="Sonnet",
        blurb="Quick and capable. Good for recap and revision.",
        supports_effort=True,
        supports_adaptive_thinking=True,
        cache_min_tokens=1024,
        efforts=EFFORTS,
        web_search_tool="web_search_20260209",
    ),
    "claude-haiku-4-5": ModelSpec(
        id="claude-haiku-4-5",
        label="Haiku",
        blurb="Fastest and cheapest. Short definitions and lookups.",
        supports_effort=False,
        supports_adaptive_thinking=False,
        cache_min_tokens=4096,
    ),
}

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"


def model_spec(model_id: str) -> ModelSpec:
    return MODELS.get(model_id, MODELS[DEFAULT_MODEL])


def request_kwargs(model_id: str, effort: str | None) -> dict[str, Any]:
    """Build the model-dependent half of a Messages request.

    Returns only what this model accepts, so a Haiku request simply has no
    `output_config` or `thinking` key rather than one the API rejects.
    """
    spec = model_spec(model_id)
    kwargs: dict[str, Any] = {"model": spec.id}
    if spec.supports_adaptive_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    if spec.supports_effort:
        chosen = effort if effort in spec.efforts else DEFAULT_EFFORT
        kwargs["output_config"] = {"effort": chosen}
    return kwargs


# --------------------------------------------------------------------- config

@dataclass
class Config:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    library: str = ""
    mirror_to_downloads: bool | None = None  # None = never asked
    web_supplements: bool = False
    initials: str = ""
    goals: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def load(cls) -> "Config":
        data: dict[str, Any] = {}
        if CONFIG_FILE.is_file():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # A corrupt config is not a crash. Everything in it is either
                # re-enterable in Settings or has a working default.
                data = {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "api_key": self.api_key,
            "model": self.model,
            "effort": self.effort,
            "library": self.library,
            "mirror_to_downloads": self.mirror_to_downloads,
            "web_supplements": self.web_supplements,
            "initials": self.initials,
            "goals": self.goals,
        }
        # Write then chmod, and chmod the directory too: the key is the one
        # genuinely sensitive thing this application stores.
        CONFIG_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        CONFIG_DIR.chmod(stat.S_IRWXU)

    # ---------------------------------------------------------------- access

    def resolve_key(self) -> str:
        """The key to use, config file first, then the environment."""
        return self.api_key.strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()

    @property
    def has_key(self) -> bool:
        return bool(self.resolve_key())

    def library_path(self) -> Path:
        from .capture.paths import library_root

        if self.library.strip():
            return Path(self.library).expanduser()
        return library_root()

    def public(self) -> dict[str, Any]:
        """Everything the browser is allowed to know. Note the absent key."""
        return {
            "has_key": self.has_key,
            "key_from_env": not self.api_key.strip() and self.has_key,
            "model": self.model,
            "effort": self.effort,
            "library": str(self.library_path()),
            "mirror_to_downloads": self.mirror_to_downloads,
            "mirror_path": str(DOWNLOADS_MIRROR),
            "web_supplements": self.web_supplements,
            "initials": self.initials,
            "goals": self.goals,
            "models": [m.as_dict() for m in MODELS.values()],
            "profile_dir": str(PROFILE_DIR),
        }
