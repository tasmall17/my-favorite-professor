"""Regression tests for the three changes made to the vendored capture layer.

Each of these guards a specific way the layer could silently break when notes
moved into per-provenance subdirectories. They are the reason this file exists:
the capture code was working before it was vendored, so anything failing here is
something this project broke.

    python -m tests.test_layout
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from professor.capture.compile import CompileError, compile_html, find_capture
from professor import library as lib
from professor.capture import full, repo
from professor.capture.fetch import FetchError, FetchResult
from professor.capture.paths import (
    RESERVED_DIRNAMES,
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


def test_subtopics(root: Path) -> None:
    """A dotted flag nests one level, and only one level.

    The dot is load-bearing in a way that is easy to regress: slugify() turns
    it into a hyphen, so anything that normalises the flag before splitting it
    produces a single top-level 'js-react-professor' -- a plausible directory
    that silently is not nesting. The first check here is what catches that.
    """
    print("\n  subtopics")
    library = root / "subtopics"
    registry = TopicRegistry(library)

    parent = registry.resolve("js")
    child = registry.resolve("js.react")

    check("a dotted flag nests rather than making one flat topic",
          child.directory.parent == parent.directory,
          child.directory.relative_to(library).as_posix())
    check("the subtopic carries no -professor suffix",
          child.directory.name == "react", child.directory.name)
    check("the nested label is what identifies it",
          child.label == "js-professor/react", child.label)
    check("a subtopic gets the full topic layout",
          all((child.directory / d).is_dir()
              for d in (".captures", USER_REFS_DIR, "claude-references-provided")))

    # The funnel has to work at the second level too, or every abbreviation
    # spawns a duplicate directory -- the exact problem it exists to prevent.
    again = registry.resolve("js.rea")
    check("an abbreviated subtopic binds to the existing one",
          again.directory == child.directory, again.action)
    sibling = registry.resolve("js.async")
    check("an unrelated subtopic gets its own directory",
          sibling.directory != child.directory
          and sibling.directory.parent == parent.directory)
    check("the parent is reused, never duplicated",
          len([d for d in library.iterdir() if d.is_dir()
               and not d.name.startswith(".")]) == 1)

    # Nesting stops at one level: the layout has two, and a third would put
    # captures where find_capture() cannot see them.
    deep = registry.resolve("js.a.b")
    check("a second dot does not create a third level",
          deep.directory.parent == parent.directory, deep.directory.name)

    check("subtopic aliases survive a rebuild from disk",
          (registry.rebuild() or TopicRegistry(library).resolve("js.react").directory)
          == child.directory)

    fresh = TopicRegistry(library)
    check("a hand-made subtopic directory is adopted",
          "js.async" in fresh._aliases, str(sorted(fresh._aliases)))

    # --new-topic on a dotted flag must force the *subtopic* only. Forcing the
    # parent too answers "-jsx.react", with js-professor already there, by
    # building a second jsx-professor/ to put it in. 'jsx' has to be a genuine
    # prefix of 'js' for this to discriminate -- an alias the funnel would not
    # have bound anyway proves nothing about what force_new did.
    forced = registry.resolve("jsx.react", force_new=True)
    check("--new-topic on a subtopic does not duplicate the parent",
          forced.directory.parent == parent.directory,
          forced.directory.parent.name)
    check("still exactly one professor after forcing",
          len([d for d in library.iterdir() if d.is_dir()
               and not d.name.startswith(".")]) == 1)


def test_subtopic_notes_are_distinct(root: Path) -> None:
    """Two notes of the same name, one per level, must not collapse.

    Note ids are built from the topic label, so a subtopic that reported a bare
    'react' would give its notes the same id as a sibling subtopic's -- and the
    reading pane would serve whichever it found first.
    """
    print("\n  subtopic notes")
    library = root / "subnotes"
    registry = TopicRegistry(library)
    parent = registry.resolve("js")
    child = registry.resolve("js.react")

    a = ingest_file(data=sample_markdown(), filename="Same.md",
                    topic_dir=parent.directory, topic=parent.label)
    b = ingest_file(data=sample_markdown(), filename="Same.md",
                    topic_dir=child.directory, topic=child.label)

    check("the subtopic note lands under its own directory",
          b.note_path.parent.parent == child.directory,
          b.note_path.relative_to(library).as_posix())

    manifest = json.loads((b.capture_dir / "manifest.json").read_text())
    check("the manifest records the nested topic",
          manifest["topic"] == "js-professor/react", manifest["topic"])

    topics = {t.name for t in lib.list_topics(library)}
    check("the subtopic is listed as a topic of its own",
          "js-professor/react" in topics, str(sorted(topics)))

    ids = [n.id for t in lib.list_topics(library) for n in t.notes]
    check("same-named notes at two levels get distinct ids",
          len(set(ids)) == len(ids) == 2, str(ids))
    # resolve() on both sides: resolve_note() returns a resolved path, and on
    # macOS the temp root arrives via the /var -> /private/var symlink.
    check("every id resolves back to its own file",
          {lib.resolve_note(library, i).path.resolve() for i in ids} ==
          {a.note_path.resolve(), b.note_path.resolve()})

    # The id gained a variable number of segments; the containment check that
    # keeps a crafted id inside the library has to survive that.
    walks = ["../../etc/user/passwd.md", "js-professor/../../user/x.md",
             "js-professor/react/admin/Same.md"]
    check("a crafted note id still cannot walk out of the library",
          all(lib.resolve_note(library, w) is None for w in walks))

    check("find_capture reaches a capture nested two levels down",
          find_capture(str(b.note_path), library) == b.capture_dir.resolve())


def test_repo_urls() -> None:
    """Which GitHub URLs describe a subtree, and which are ordinary pages."""
    print("\n  repo urls")
    cases = {
        "https://github.com/o/r": ("o", "r", None, ""),
        "https://github.com/o/r.git": ("o", "r", None, ""),
        "https://github.com/o/r/tree/main": ("o", "r", "main", ""),
        "https://github.com/o/r/tree/main/a/b": ("o", "r", "main", "a/b"),
        "https://github.com/o/r/blob/v2/a/f.py": ("o", "r", "v2", "a/f.py"),
    }
    for url, expected in cases.items():
        t = repo.parse_repo_url(url)
        got = (t.owner, t.repo, t.ref, t.path) if t else None
        check(f"parses {url}", got == expected, str(got))

    # Everything else on github.com is a page, not a tree. Routing an issue
    # thread to the repo tier would walk the whole repository to answer a URL
    # that pointed at a conversation.
    for url in ("https://github.com/o/r/issues/4",
                "https://github.com/o/r/pull/9",
                "https://github.com/o",
                "https://gitlab.com/o/r",
                "https://example.com/o/r"):
        check(f"not a subtree: {url}", repo.parse_repo_url(url) is None)


def test_repo_selection() -> None:
    """Selection runs on tree metadata, before anything is downloaded.

    Every rejection here is one file not fetched, which is the whole reason the
    tree call asks for sizes up front rather than discovering them mid-download.
    """
    print("\n  repo selection")
    entries = [
        repo.Entry("docs/guide.md", 2000),
        repo.Entry("src/app.py", 3000),
        repo.Entry("src/util.py", 1000),
        repo.Entry("node_modules/left-pad/index.js", 500),
        repo.Entry("dist/bundle.min.js", 900),
        repo.Entry("package-lock.json", 400000),
        repo.Entry("logo.png", 50000),
        repo.Entry("huge.py", 9_000_000),
    ]
    target = repo.RepoTarget("o", "r", "main", "")
    kept = {e.path for e in repo.select(entries, target, repo.RepoOptions()).kept}
    check("source and docs are kept",
          {"docs/guide.md", "src/app.py", "src/util.py"} <= kept, str(sorted(kept)))
    for path, why in [("node_modules/left-pad/index.js", "dependency directory"),
                      ("dist/bundle.min.js", "build output"),
                      ("package-lock.json", "lockfile"),
                      ("logo.png", "binary"),
                      ("huge.py", "oversized file")]:
        check(f"skips {why}", path not in kept)

    only = repo.select(entries, target, repo.RepoOptions(only=("*.md",))).kept
    check("--only narrows to matching paths",
          [e.path for e in only] == ["docs/guide.md"], str([e.path for e in only]))
    skip = repo.select(entries, target, repo.RepoOptions(skip=("*.md",))).kept
    check("--skip removes matching paths",
          "docs/guide.md" not in {e.path for e in skip})

    scoped = repo.select(entries, repo.RepoTarget("o", "r", "main", "src"),
                         repo.RepoOptions())
    check("a subtree URL narrows to that subtree",
          {e.path for e in scoped.kept} == {"src/app.py", "src/util.py"})

    budget = repo.select(entries, target, repo.RepoOptions(max_total_bytes=2500))
    check("the byte budget stops collection and says so", budget.truncated)

    try:
        repo.select(entries, repo.RepoTarget("o", "r", "main", "nope"),
                    repo.RepoOptions())
        check("an empty subtree is an error", False)
    except repo.RepoError:
        check("an empty subtree is an error", True)


def test_repo_rendering() -> None:
    """The document has to survive Markdown that fights back."""
    print("\n  repo rendering")

    # A '#' inside a fenced block is a shell comment. Shifting it turns a code
    # comment into a document heading, and it shows up in the contents list.
    src = "# Title\n\n```sh\n# not a heading\n```\n\n## Sub\n"
    shifted = repo.shift_headings(src, 2)
    check("headings shift", "### Title" in shifted and "#### Sub" in shifted)
    check("a # inside a fence is left alone", "# not a heading" in shifted)

    check("h6 does not overflow", repo.shift_headings("###### Deep", 2) == "###### Deep")
    check("a bare hash is not a heading", repo.shift_headings("#hashtag", 2) == "#hashtag")

    # A file containing a fence would otherwise close the fence wrapping it.
    check("the fence outgrows embedded backticks",
          repo._fence_for("a ``` b") == "````", repo._fence_for("a ``` b"))

    target = repo.RepoTarget("o", "r", "main", "")
    selection = repo.Selection(kept=[repo.Entry("README.md", 10),
                                     repo.Entry("app.py", 10)])
    doc = repo.render(target, selection,
                      {"README.md": "# Hello\n\ntext", "app.py": "x = 1"},
                      title="o/r @ main", topic="t-professor",
                      captured="2026-01-01 00:00")
    check("frontmatter names the repo", 'repo: "o/r"' in doc)
    check("the tree is included", "## Tree" in doc)
    check("the contents list is included", "## Contents" in doc)
    check("markdown is inlined, not fenced", "## Hello" in doc)
    check("code is fenced with its language", "```python\nx = 1\n```" in doc)


def test_full_page_slug() -> None:
    """A subtopic name derived from the URL alone, before any fetch."""
    print("\n  full page slug")
    check("host and path fold into one slug",
          full.page_slug("https://www.realpython.com/decorators/") ==
          "realpython-decorators")
    check("a bare domain falls back to its own name",
          full.page_slug("https://example.com/") == "example")


def test_full_link_extraction() -> None:
    """What counts as a page worth queuing, out of everything <a> can hold."""
    print("\n  full link extraction")
    html = """
    <a href="/about">About</a>
    <a href="https://example.com/blog/post-1">Post 1</a>
    <a href="#top">skip: fragment-only</a>
    <a href="mailto:me@example.com">skip: mailto</a>
    <a href="javascript:void(0)">skip: javascript</a>
    <a href="/logo.png">skip: image</a>
    <a href="/about#section">dup of /about once normalised</a>
    """
    links = full.extract_links(html, "https://example.com/index.html")
    check("relative link is made absolute",
          "https://example.com/about" in links, str(links))
    check("absolute link on the page is kept",
          "https://example.com/blog/post-1" in links)
    check("fragment-only href is skipped",
          not any("top" in link for link in links))
    check("mailto: is skipped", not any("mailto" in link for link in links))
    check("javascript: is skipped", not any("javascript" in link for link in links))
    check("an image extension is skipped", not any(link.endswith(".png") for link in links))
    check("a link differing only by fragment is not duplicated",
          sum(1 for link in links if link.split("#")[0].endswith("/about")) == 1,
          str(links))


def test_full_normalize_folds_index_files() -> None:
    """'/blog/' and '/blog/index.html' must count as the same visited page."""
    print("\n  full normalize")
    check("index.html folds onto its directory",
          full._normalize("https://example.com/blog/index.html") ==
          full._normalize("https://example.com/blog/"))
    check("a fragment does not create a new page",
          full._normalize("https://example.com/a#x") ==
          full._normalize("https://example.com/a"))


def test_full_crawl_respects_bounds() -> None:
    """The crawl walks same-site links, stops at depth/max-pages, and never
    revisits a page -- without making a single real HTTP request.
    """
    print("\n  full crawl")

    # home -> about, blog/post1 ; blog/post1 -> blog/post2, external ;
    # blog/post2 -> home (a cycle, which is what visited-set dedup is for).
    pages = {
        "https://site.test/": (
            '<a href="/about">a</a><a href="/blog/post1">b</a>'
        ),
        "https://site.test/about": "<p>about, no outbound links</p>",
        "https://site.test/blog/post1": (
            '<a href="/blog/post2">c</a>'
            '<a href="https://other.test/page">external</a>'
        ),
        "https://site.test/blog/post2": '<a href="/">home</a>',
    }

    def fake_fetch(url: str, *, max_tier: str = "T3", on_tier=None) -> FetchResult:
        if url not in pages:
            raise FetchError(f"no such fixture page: {url}", "T1")
        return FetchResult(html=pages[url], final_url=url, tier="T1")

    original_fetch = full.fetch
    full.fetch = fake_fetch  # noqa: PLC0415 - swap the module-level import for the test
    try:
        seen: list[tuple[str, int]] = []
        result = full.crawl(
            "https://site.test/", full.FullOptions(max_pages=50),
            on_page=lambda url, depth, fetched, error: seen.append((url, depth)),
        )
        urls = {u for u, _ in seen}
        check("all four same-site pages are captured", len(urls) == 4, str(urls))
        check("the external link is never queued",
              not any("other.test" in u for u in urls))
        check("the home-page cycle does not revisit home",
              sum(1 for u, _ in seen if u.rstrip("/") == "https://site.test") == 1)
        check("crawl succeeded on every page", result.succeeded == 4)

        # A depth cap of 0 must still capture the origin page itself and
        # nothing it links to.
        seen_shallow: list[tuple[str, int]] = []
        full.crawl(
            "https://site.test/", full.FullOptions(max_depth=0),
            on_page=lambda url, depth, fetched, error: seen_shallow.append((url, depth)),
        )
        check("depth 0 captures only the origin page",
              seen_shallow == [("https://site.test/", 0)], str(seen_shallow))

        # A page budget of 2 must stop the walk after exactly two pages.
        seen_capped: list[tuple[str, int]] = []
        full.crawl(
            "https://site.test/", full.FullOptions(max_pages=2),
            on_page=lambda url, depth, fetched, error: seen_capped.append((url, depth)),
        )
        check("max_pages stops the walk at the budget",
              len(seen_capped) == 2, str(seen_capped))
    finally:
        full.fetch = original_fetch

    # A page that fails to fetch is reported, not fatal to the rest.
    def flaky_fetch(url: str, *, max_tier: str = "T3", on_tier=None) -> FetchResult:
        if url == "https://site.test/about":
            raise FetchError("boom", "T1")
        return fake_fetch(url, max_tier=max_tier)

    full.fetch = flaky_fetch
    try:
        outcomes: list[bool] = []
        result = full.crawl(
            "https://site.test/", full.FullOptions(),
            on_page=lambda url, depth, fetched, error: outcomes.append(error is None),
        )
        check("a failed page is reported, not fatal", False in outcomes)
        check("the rest of the site is still captured",
              outcomes.count(True) == 3, str(outcomes))
        check("FullResult.failed lists the failure",
              len(result.failed) == 1 and result.failed[0].url == "https://site.test/about")
    finally:
        full.fetch = original_fetch


# ---------------------------------------------------------------------- main

def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="mfp-test-"))
    try:
        test_find_capture_resolves_nested_note(root)
        test_reserved_dirs_are_not_topics(root)
        test_compile_is_offline_and_self_contained(root)
        test_topic_layout_created(root)
        test_source_split(root)
        test_subtopics(root)
        test_subtopic_notes_are_distinct(root)
        test_repo_urls()
        test_repo_selection()
        test_repo_rendering()
        test_full_page_slug()
        test_full_link_extraction()
        test_full_normalize_folds_index_files()
        test_full_crawl_respects_bounds()
        test_library_root_rejects_source_checkout()
        test_markdown_renderer()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if _failures:
        print(f"  {len(_failures)} failed: {', '.join(_failures)}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
