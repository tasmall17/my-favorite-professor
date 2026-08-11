"""Regression tests for the three changes made to the vendored capture layer.

Each of these guards a specific way the layer could silently break when notes
moved into per-provenance subdirectories. They are the reason this file exists:
the capture code was working before it was vendored, so anything failing here is
something this project broke.

    python -m tests.test_layout
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from professor.capture.compile import CompileError, compile_html, find_capture
from professor.capture.paths import (
    DEFAULT_LIBRARY_NAME,
    LIBRARY_NAME,
    MACHINE_DIR,
    RESERVED_DIRNAMES,
    TOPIC_SUFFIX,
    USER_REFS_DIR,
    ensure_topic_layout,
    library_root,
)
from professor.capture.topics import TopicRegistry
from professor.ingest import ingest_file
from professor.markdown import parse, to_html

PASS, FAIL = "  ok  ", "  FAIL"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}  {name}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        _failures.append(name)


def sample_markdown(words: int = 80) -> bytes:
    body = " ".join(f"word{i}" for i in range(words))
    return f"# A Sample Lesson\n\n{body}\n\n## Second Part\n\n{body}\n".encode()


# --------------------------------------------------------------------- tests

def test_find_capture_resolves_nested_note(root: Path) -> None:
    """A note inside usr-references-provided/ still finds its capture.

    find_capture() resolved manifest["note"] against the note's own parent,
    which was the topic directory under the old flat layout. Notes are now one
    level deeper; without the fallback this raises CompileError.
    """
    topic_dir = root / "py-professor"
    result = ingest_file(
        data=sample_markdown(), filename="lesson.md",
        topic_dir=topic_dir, topic="py-professor",
    )

    check(
        "note is written into usr-references-provided/",
        result.note_path.parent.name == USER_REFS_DIR,
        str(result.note_path.parent.name),
    )

    # find_capture returns a resolved path, and on macOS a temp directory under
    # /var resolves to /private/var -- so both sides have to be resolved.
    expected = result.capture_dir.resolve()

    try:
        found = find_capture(str(result.note_path), root)
        check("find_capture resolves a nested note", found == expected,
              f"{found} != {expected}")
    except CompileError as exc:
        check("find_capture resolves a nested note", False, str(exc))

    # The bare-name and directory forms must keep working too.
    check(
        "find_capture still resolves a capture directory",
        find_capture(str(result.capture_dir), root) == expected,
    )
    check(
        "find_capture still resolves a partial name",
        find_capture("lesson", root) == expected,
    )


def test_reserved_dirs_are_not_topics(root: Path) -> None:
    """A visible non-subject directory must not become a phantom topic.

    topic_dirs() adopts every non-dot directory at the library root, and
    _absorb_disk() mints an alias for each on every load -- so without the
    guard, usr-learning-profile/ becomes a subject answering to `-usr`.
    """
    (root / "usr-learning-profile").mkdir(exist_ok=True)
    ensure_topic_layout(root / "py-professor")

    registry = TopicRegistry(root)
    names = [d.name for d in registry.topic_dirs()]

    check("reserved directory is not listed as a topic",
          "usr-learning-profile" not in names, str(names))
    check("real topic is still listed", "py-professor" in names, str(names))
    check("every reserved name is excluded",
          not (RESERVED_DIRNAMES & set(names)))

    # _absorb_disk() runs on load and writes an alias per topic it sees. Force
    # a save so the persisted dictionary can be inspected for a stray entry.
    registry.save()
    aliases = json.loads((root / ".mfp" / "topics.json").read_text()).get("aliases", {})
    check("no alias was minted for the reserved directory",
          "usr" not in aliases and "usr-learning-profile" not in aliases, str(aliases))


def test_compile_is_offline_and_self_contained(root: Path) -> None:
    """An ingested capture compiles to one file with its assets inlined."""
    topic_dir = root / "sh-professor"
    result = ingest_file(
        data=sample_markdown(), filename="shell-basics.md",
        topic_dir=topic_dir, topic="sh-professor",
    )
    output = compile_html(result.capture_dir)
    text = output.read_text(encoding="utf-8")

    check("compile produces a file", output.is_file())
    check("compiled page carries the content", "A Sample Lesson" in text)
    check("compiled page has no remote references",
          "http://" not in text and "https://" not in text.replace("https://www.w3.org", ""))


def test_topic_layout_created(root: Path) -> None:
    """Resolving a new topic builds the full subdirectory layout."""
    registry = TopicRegistry(root)
    directory = registry.resolve("js").directory
    for sub in (".captures", "usr-references-provided",
                "claude-references-provided", ".mfp-course"):
        check(f"new topic has {sub}", (directory / sub).is_dir())


def test_source_split(root: Path) -> None:
    """Provenance decides the directory, and is recorded in the manifest."""
    topic_dir = root / "js-professor"
    mine = ingest_file(data=sample_markdown(), filename="mine.md",
                       topic_dir=topic_dir, topic="js-professor", source="user")
    theirs = ingest_file(data=sample_markdown(), filename="theirs.md",
                         topic_dir=topic_dir, topic="js-professor", source="claude")

    check("user material lands in usr-references-provided",
          mine.note_path.parent.name == "usr-references-provided")
    check("claude material lands in claude-references-provided",
          theirs.note_path.parent.name == "claude-references-provided")

    manifest = json.loads((theirs.capture_dir / "manifest.json").read_text())
    check("manifest records provenance", manifest.get("source") == "claude")


def test_library_root_rejects_source_checkout() -> None:
    """The app's own checkout must never be mistaken for a material library.

    On a case-insensitive filesystem `my-favorite-professor` and
    `My-Favorite-Professor` are the same path, so a repo cloned beside a library
    merges into it. This is the guard against then treating it as material.
    """
    from professor.capture.paths import _looks_like_source_checkout

    repo = Path(__file__).resolve().parent.parent
    check("a real checkout is detected", _looks_like_source_checkout(repo))
    check("a plain library is not", not _looks_like_source_checkout(library_root()))


def _use_home(scratch: Path, name: str) -> Path:
    """Make a throwaway $HOME and point the process at it.

    Path.home() reads $HOME on POSIX, so moving the variable moves every caller
    at once -- including any os.path.expanduser deeper in the code. That keeps
    these tests off the real filesystem, which matters here more than usual: the
    thing under test is a path that gets *created* on demand.
    """
    home = scratch / name
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ.pop("MFP_LIBRARY", None)
    return home


def _make_library(path: Path) -> Path:
    """A directory that looks like real material: machinery plus a subject."""
    (path / MACHINE_DIR).mkdir(parents=True, exist_ok=True)
    (path / f"py{TOPIC_SUFFIX}").mkdir(exist_ok=True)
    return path


def _make_checkout(path: Path) -> Path:
    """A directory that looks like this repo."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (path / "professor").mkdir(exist_ok=True)
    return path


def test_library_root_resolution(scratch: Path) -> None:
    """Where a library is found, and what must never be mistaken for one.

    The default moved out of ~/code because on macOS a clone named
    `my-favorite-professor` IS `~/code/My-Favorite-Professor`, so the installer
    built the library inside the app's own source. Everything here is a way that
    could regress: silently relocating someone's existing library is as bad as
    the original bug.
    """
    home = _use_home(scratch, "empty")
    check("a fresh machine gets the new default",
          library_root() == home / DEFAULT_LIBRARY_NAME, str(library_root()))
    check("resolving does not create anything",
          not (home / DEFAULT_LIBRARY_NAME).exists())

    home = _use_home(scratch, "legacy-code")
    legacy = _make_library(home / "code" / "My-Favorite-Professor")
    check("a legacy library in ~/code is still found",
          library_root() == legacy, str(library_root()))

    home = _use_home(scratch, "legacy-home")
    legacy = _make_library(home / "My-Favorite-Professor")
    check("a legacy library in ~ is still found",
          library_root() == legacy, str(library_root()))

    home = _use_home(scratch, "legacy-topic-only")
    legacy = home / "Documents" / "My-Favorite-Professor"
    (legacy / f"sh{TOPIC_SUFFIX}").mkdir(parents=True)
    check("a subject directory alone is enough to count as a library",
          library_root() == legacy, str(library_root()))

    home = _use_home(scratch, "legacy-empty")
    (home / "code" / "My-Favorite-Professor").mkdir(parents=True)
    check("an empty directory with the right name is not adopted",
          library_root() == home / DEFAULT_LIBRARY_NAME, str(library_root()))

    # The disaster case: a checkout cloned on top of an old library, which is
    # the only way both sets of markers appear in one directory. Without the
    # source-checkout guard this is indistinguishable from material.
    home = _use_home(scratch, "collided")
    collided = _make_checkout(home / "code" / "My-Favorite-Professor")
    _make_library(collided)
    check("a source checkout is never adopted as a library",
          library_root() == home / DEFAULT_LIBRARY_NAME, str(library_root()))

    home = _use_home(scratch, "both")
    current = _make_library(home / DEFAULT_LIBRARY_NAME)
    _make_library(home / "code" / "My-Favorite-Professor")
    check("the current default outranks a legacy location",
          library_root() == current, str(library_root()))

    home = _use_home(scratch, "env")
    _make_library(home / DEFAULT_LIBRARY_NAME)
    _make_library(home / "code" / "My-Favorite-Professor")
    os.environ["MFP_LIBRARY"] = str(home / "elsewhere")
    check("MFP_LIBRARY wins over everything",
          library_root() == home / "elsewhere", str(library_root()))
    os.environ["MFP_LIBRARY"] = "~/tilde-library"
    check("MFP_LIBRARY expands ~",
          library_root() == home / "tilde-library", str(library_root()))
    os.environ.pop("MFP_LIBRARY", None)


def test_macos_clone_is_not_the_library(scratch: Path) -> None:
    """The end-to-end scenario: plain `git clone` then `./install.sh`.

    Reproduces what actually happened -- a checkout at ~/code/my-favorite-professor,
    which on a case-insensitive filesystem is the old default library path. The
    library must land somewhere else entirely, with no part of it inside the
    checkout.
    """
    home = _use_home(scratch, "clone")
    checkout = _make_checkout(home / "code" / "my-favorite-professor")

    root = library_root()
    check("the library is not the checkout",
          root.resolve() != checkout.resolve(), f"{root} vs {checkout}")
    check("the library is not inside the checkout",
          not root.resolve().is_relative_to(checkout.resolve()), str(root))
    check("the library is the new default",
          root == home / DEFAULT_LIBRARY_NAME, str(root))


def test_empty_default_does_not_hide_a_real_library(scratch: Path) -> None:
    """A default directory with nothing in it must not outrank real material.

    The failure this guards is silent and alarming: you open the app and your
    material is simply gone, with no error, because a stray mkdir or an aborted
    first run left an empty directory sitting at the winning path.
    """
    home = _use_home(scratch, "empty-default-vs-legacy")
    legacy = _make_library(home / "code" / LIBRARY_NAME)

    check("legacy library is found on its own", library_root() == legacy)

    (home / DEFAULT_LIBRARY_NAME).mkdir()
    check("an empty default does not hide it", library_root() == legacy,
          str(library_root()))

    # Once it genuinely holds material it is the right answer.
    _make_library(home / DEFAULT_LIBRARY_NAME)
    check("a default with material wins",
          library_root() == home / DEFAULT_LIBRARY_NAME)


def test_environment_beats_the_stored_setting(scratch: Path) -> None:
    """MFP_LIBRARY outranks the path saved in Settings.

    Both entry points have to agree on this. `mfp -py <url>` resolves through
    library_root(), which reads the environment, so if the stored setting won
    inside the app you could save a page from the terminal and then not find it
    in the reader.
    """
    from professor.config import Config

    home = _use_home(scratch, "env-vs-settings")
    stored = _make_library(home / "from-settings")
    override = _make_library(home / "from-env")

    config = Config(library=str(stored))
    check("the stored setting is used when the environment is quiet",
          config.library_path() == stored)
    check("and it reports itself as such", config.library_source() == "settings")

    os.environ["MFP_LIBRARY"] = str(override)
    try:
        check("the environment wins", config.library_path() == override,
              str(config.library_path()))
        check("and the app can say so", config.library_source() == "environment")
    finally:
        os.environ.pop("MFP_LIBRARY", None)


def test_markdown_renderer() -> None:
    """The renderer must escape, and must not emit live javascript: URLs."""
    html = to_html("# Hi\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))")
    check("html in source is escaped", "<script>" not in html)
    check("javascript: urls are defused", 'href="javascript:' not in html)

    # Browsers execute all of these; the check has to survive obfuscation.
    for hostile in ("javascript:alert(1)", "JaVaScRiPt:alert(1)",
                    "java&#9;script:alert(1)", "data:text/html,<script>x</script>",
                    "vbscript:msgbox(1)"):
        rendered = to_html(f"[click]({hostile})")
        check(f"defused: {hostile[:24]}", 'href="#"' in rendered, rendered[:90])

    # A quote in the URL must not close the attribute and start a new one.
    # No spaces, or the link regex simply declines to match and the whole thing
    # stays inert as escaped text -- which is safe but tests nothing.
    breakout = to_html('[x](https://e.com/"onerror="alert(1))')
    check("quotes cannot break out of an attribute",
          'onerror="alert' not in breakout, breakout[:110])

    check("ordinary links survive",
          'href="https://example.com/a?b=1"' in to_html("[a](https://example.com/a?b=1)"))
    check("relative asset links survive",
          'src="assets/img-001.png"' in to_html("![f](assets/img-001.png)"))

    doc = parse("---\ntitle: \"Quoted Title\"\n---\n\n# Body Heading\n\ntext\n")
    check("frontmatter title wins", doc.title == "Quoted Title", doc.title)
    check("frontmatter is stripped from the body", "---" not in doc.body)

    fenced = to_html("```python\nx = 1\n```")
    check("fenced code renders", "<pre><code" in fenced and "language-python" in fenced)

    table = to_html("| a | b |\n|---|---|\n| 1 | 2 |")
    check("tables render", "<table>" in table and "<td>1</td>" in table)


# ---------------------------------------------------------------------- main

def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="mfp-test-"))
    scratch = Path(tempfile.mkdtemp(prefix="mfp-home-"))
    # The library tests move $HOME, so they run last and put it back. Anything
    # earlier that reads the real home -- the mirror path, the config directory
    # -- then sees the environment it expects.
    saved_env = {name: os.environ.get(name) for name in ("HOME", "MFP_LIBRARY")}
    try:
        test_find_capture_resolves_nested_note(root)
        test_reserved_dirs_are_not_topics(root)
        test_compile_is_offline_and_self_contained(root)
        test_topic_layout_created(root)
        test_source_split(root)
        test_library_root_rejects_source_checkout()
        test_markdown_renderer()
        test_library_root_resolution(scratch)
        test_macos_clone_is_not_the_library(scratch)
        test_empty_default_does_not_hide_a_real_library(scratch)
        test_environment_beats_the_stored_setting(scratch)
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    if _failures:
        print(f"  {len(_failures)} failed: {', '.join(_failures)}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
