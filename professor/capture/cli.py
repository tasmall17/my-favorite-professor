"""Command line entry point, exposed as both `mfp` and `my-fav-professor`.

    mfp -py https://realpython.com/decorators     # topic flags, invented on the spot
    mfp -py.async https://.../asyncio             # a subtopic inside py-professor
    mfp https://example.com/article               # no flag -> inbox

Topic flags are the interesting part. argparse cannot accept arbitrary unknown
flags, so sys.argv is pre-scanned: the first token that looks like a flag and
isn't on the reserved list is the topic, and everything else is handed to
argparse untouched.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import fetch as fetch_module
from . import full as full_module
from . import journal
from . import repo as repo_module
from .capture import write_capture
from .compile import CompileError, compile_html, compile_pdf, find_capture
from .fetch import FetchError, fetch
from .paths import INBOX_TOPIC, ensure_library, library_root
from .topics import TopicRegistry

# Flags that belong to the program, so they can never be read as a topic.
RESERVED = {
    "-h", "--help", "--version", "--topics", "--link", "--new-topic",
    "--compile", "--pdf", "--html", "--open", "--retry-failed", "--self-test",
    "--timeout", "--library", "--quiet", "-q", "--topic", "--no-t3", "--rebuild",
    "-n", "--new", "--repo", "--page", "--max-bytes", "--only", "--skip",
    "--full", "--depth", "--max-pages",
}

# The dot is what separates a topic from a subtopic, so it has to survive the
# scan that picks the topic token out of argv. Nothing else in the grammar uses
# it, and no reserved flag contains one, so admitting it here is unambiguous.
FLAG_RE = re.compile(r"^--?[A-Za-z][\w.-]*$")

# Width of the label column in status output. Sized for the longest label
# ("updated:") so paths line up across every message.
_LABEL_W = 9


def split_topic(argv: list[str]) -> tuple[str | None, list[str]]:
    """Pull an on-the-fly topic flag out of the argument list.

    `--topic NAME` is the escape hatch for a topic that collides with a
    reserved word (`mfp --topic list <url>`).
    """
    topic: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--topic" and i + 1 < len(argv):
            topic = topic or argv[i + 1]
            i += 2
            continue
        if topic is None and token not in RESERVED and FLAG_RE.match(token):
            topic = token
            i += 1
            continue
        rest.append(token)
        i += 1
    return topic, rest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mfp",
        description=(
            "Capture a web page into a readable Markdown note plus a hidden, "
            "re-compilable archive."
        ),
        epilog=(
            "Topic flags are invented on the spot: `mfp -py <url>` files into "
            "py-professor/. Type -python later and it lands in the same place. "
            "A dot nests one level: `mfp -py.async <url>` files into "
            "py-professor/async/, which is a separate subject under the same "
            "professor."
        ),
    )
    parser.add_argument("target", nargs="?", help="URL to capture")
    parser.add_argument("--version", action="version", version="my-favorite-professor 0.1.0")
    parser.add_argument("--topics", action="store_true",
                        help="list topic directories and their aliases")
    parser.add_argument("--rebuild", action="store_true",
                        help="regenerate the alias dictionary from disk")
    parser.add_argument("--link", metavar="ALIAS=TOPIC",
                        help="bind an alias to an existing topic directory")
    parser.add_argument("-n", "--new", metavar="NAME",
                        help="create a topic directory without capturing anything")
    parser.add_argument("--new-topic", action="store_true",
                        help="force a new directory instead of matching an existing one")
    parser.add_argument("--compile", metavar="CAPTURE",
                        help="rebuild a capture into a single file")
    parser.add_argument("--pdf", action="store_true",
                        help="with --compile, produce a PDF")
    parser.add_argument("--html", action="store_true",
                        help="with --compile, produce a single-file HTML (the default)")
    parser.add_argument("--open", dest="open_after", action="store_true",
                        help="open the result when finished")
    parser.add_argument("--timeout", type=int, metavar="SECONDS",
                        help=f"per-request timeout (default {fetch_module.TIMEOUT})")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-run every row in failed-attempts.csv")
    parser.add_argument("--self-test", action="store_true",
                        help="run the fixture URLs and report coverage")
    parser.add_argument("--no-t3", action="store_true",
                        help="stop at the headless browser; skip the stealth/archive tier")
    parser.add_argument("--repo", action="store_true",
                        help="treat the URL as a GitHub subtree to walk")
    parser.add_argument("--page", action="store_true",
                        help="capture a GitHub URL as a page instead of walking it")
    parser.add_argument("--max-bytes", type=int, metavar="N",
                        help=f"repo size budget (default {repo_module.MAX_TOTAL_BYTES // 1024} KB)")
    parser.add_argument("--only", action="append", metavar="GLOB", default=[],
                        help="repo: include only paths matching GLOB (repeatable)")
    parser.add_argument("--skip", action="append", metavar="GLOB", default=[],
                        help="repo: exclude paths matching GLOB (repeatable)")
    parser.add_argument("--full", action="store_true",
                        help="follow every same-site hyperlink from the URL, "
                             "capturing each one into a subfolder")
    parser.add_argument("--depth", type=int, metavar="N",
                        help="with --full, hop limit from the origin page "
                             "(default: unlimited, stops when links run out)")
    parser.add_argument("--max-pages", type=int, metavar="N",
                        help=f"with --full, total page budget "
                             f"(default {full_module.MAX_PAGES_DEFAULT})")
    parser.add_argument("--library", metavar="PATH", help="override the library root")
    parser.add_argument("--quiet", "-q", action="store_true")
    return parser


def _root(args) -> Path:
    if args.library:
        import os  # noqa: PLC0415

        os.environ["MFP_LIBRARY"] = args.library
    if args.timeout:
        fetch_module.TIMEOUT = args.timeout
    return ensure_library()


def _open_path(path: Path) -> None:
    """Hand a finished artifact to the OS to open. Never fatal."""
    import subprocess  # noqa: PLC0415

    opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
    try:
        subprocess.run([opener, str(path)], check=False)
    except OSError:
        pass


def _mirror(capture, topic: str, *, quiet: bool) -> str | None:
    """Write the second copy into ~/Downloads, if that's been turned on.

    Deliberately best-effort and never fatal: the capture already succeeded in
    the library, and losing a convenience copy is not a reason to report a
    failed save. Imported here rather than at module scope so the capture layer
    keeps working standalone if it is ever lifted back out.
    """
    from ..config import Config  # noqa: PLC0415
    from ..mirror import mirror_capture, prune_stale  # noqa: PLC0415

    if not Config.load().mirror_to_downloads:
        return None

    prune_stale(title=capture.title, capture_dir=capture.capture_dir, topic=topic)
    outcome = mirror_capture(
        capture_dir=capture.capture_dir, note_path=capture.note_path,
        title=capture.title, topic=topic,
    )
    if outcome.error and not quiet:
        print(f"  note: {outcome.error}")
    target = outcome.page or outcome.note
    return str(target.parent) if target else None


def _announce_topic(resolution, root: Path, quiet: bool) -> None:
    """Print and journal how a topic (or subtopic) alias resolved.

    Shared by do_capture and do_full: a first `-js.react` can create two
    directories, and reporting only the leaf would leave the new parent
    unmentioned -- the parent is the one the user has to live with if the
    funnel guessed its name wrong.
    """
    if resolution.is_subtopic and resolution.parent_action == "created":
        journal.audit_create(resolution.parent, resolution.alias.split(".")[0], root=root)
        if not quiet:
            print(f"  topic: created {resolution.parent.name}/")

    if not quiet:
        if resolution.action == "created":
            print(f"  topic: created {resolution.label}/")
        elif resolution.action == "bound":
            print(f"  topic: '{resolution.alias}' -> existing "
                  f"{resolution.label}/  (use --new-topic to separate)")

    if resolution.action == "created":
        journal.audit_create(resolution.directory, resolution.alias, root=root)
    elif resolution.action == "bound":
        journal.audit_link(resolution.alias, resolution.directory, root=root)


def do_capture(url: str, topic_flag: str | None, args, root: Path) -> int:
    if args.full:
        return do_full(url, topic_flag, args, root)

    registry = TopicRegistry(root)
    alias = topic_flag or INBOX_TOPIC
    resolution = registry.resolve(alias, force_new=args.new_topic)
    _announce_topic(resolution, root, args.quiet)

    # A repository URL goes to the repo tier by default, because the page tier
    # is actively wrong on one: fetching github.com/owner/repo renders a file
    # listing and a README and calls that the material. --page asks for the
    # rendered page anyway, --repo forces the walk on a URL not recognised as
    # one. topic_dir is the leaf, and a subtopic directory has the identical
    # layout a topic does, which is why nesting needs nothing from either tier.
    # The *label* is the nested one, so the manifest and the mirror can tell a
    # subtopic note apart from a parent note of the same name.
    walk_repo = args.repo or (
        not args.page and repo_module.parse_repo_url(url) is not None
    )

    if walk_repo:
        def say(message: str) -> None:
            if not args.quiet:
                print(f"  repo: {message} ...")

        options = repo_module.RepoOptions(
            max_total_bytes=args.max_bytes or repo_module.MAX_TOTAL_BYTES,
            only=tuple(args.only),
            skip=tuple(args.skip),
        )
        try:
            document = repo_module.capture_repo(
                url, topic=resolution.label, options=options, on_status=say,
            )
        except repo_module.RepoError as exc:
            journal.record_failure(url, resolution.alias, "REPO", exc.reason,
                                   root=root)
            print(f"  FAILED  {exc.reason}", file=sys.stderr)
            print(f"  logged to {journal.failures_file(root)}", file=sys.stderr)
            return 1

        capture = repo_module.write_repo_capture(
            document, topic_dir=resolution.directory, topic=resolution.label,
        )
    else:
        def announce(tier: str) -> None:
            if not args.quiet:
                print(f"  fetching [{tier}] ...")

        try:
            result = fetch(url, max_tier="T2" if args.no_t3 else "T3",
                           on_tier=announce)
        except FetchError as exc:
            journal.record_failure(url, resolution.alias, exc.stage, exc.reason,
                                   root=root)
            print(f"  FAILED  {exc.reason}", file=sys.stderr)
            print(f"  logged to {journal.failures_file(root)}", file=sys.stderr)
            return 1

        document = None
        capture = write_capture(
            result,
            topic_dir=resolution.directory,
            topic=resolution.label,
            original_url=url,
            quiet=args.quiet,
        )
    journal.audit_saved(url, capture.note_path, capture.tier,
                        updated=capture.updated, root=root)

    mirrored = _mirror(capture, resolution.label, quiet=args.quiet)

    if not args.quiet:
        verb = "updated" if capture.updated else "saved"
        # Filename and directory on separate lines: the name stays readable,
        # and the directory is an absolute path you can cd to, paste, or
        # cmd-click. Labels are padded to a fixed width so the paths stay
        # flush whether the verb is "saved" or the longer "updated".
        print(f"  {verb + ':':<{_LABEL_W}}{capture.note_path.name}")
        print(f"  {'dir:':<{_LABEL_W}}{capture.note_path.parent.resolve()}")
        if document is not None:
            detail = (f"  {document.files} files, {capture.word_count} words "
                      f"[{capture.tier}]")
            if document.selection.skipped:
                skipped = sum(document.selection.skipped.values())
                detail += f"  ({skipped} skipped)"
        else:
            detail = (f"  {capture.word_count} words, {capture.assets_kept} "
                      f"images [{capture.tier}]")
        if capture.from_archive:
            detail += "  (from web archive)"
        print(detail)
        if mirrored:
            print(f"  {'copy:':<{_LABEL_W}}{mirrored}")
    if getattr(args, "open_after", False):
        _open_path(capture.note_path)
    return 0


def do_full(url: str, topic_flag: str | None, args, root: Path) -> int:
    """Walk every same-site link reachable from `url` into one subfolder.

    The subfolder is resolved up front, exactly the directory `-xy.<page>`
    would resolve to by hand, and every page the crawl finds -- including the
    origin page itself -- is written into it with the ordinary write_capture()
    pipeline. Asset-download noise is suppressed per page (quiet=True to
    write_capture) regardless of --quiet; a crawl of dozens of pages needs its
    own one-line-per-page status, not each page's own image-fetch chatter.
    """
    registry = TopicRegistry(root)
    alias = topic_flag or INBOX_TOPIC
    slug = full_module.page_slug(url)
    resolution = registry.resolve(f"{alias}.{slug}", force_new=args.new_topic)
    _announce_topic(resolution, root, args.quiet)

    saved = 0
    failed = 0

    def on_page(page_url: str, depth: int, fetched, error) -> None:
        nonlocal saved, failed
        indent = "  " * min(depth, 4)
        if error is not None:
            failed += 1
            journal.record_failure(page_url, resolution.alias, error.stage,
                                   error.reason, root=root)
            if not args.quiet:
                print(f"  {indent}FAILED  {page_url}  ({error.reason})")
            return

        capture = write_capture(
            fetched, topic_dir=resolution.directory, topic=resolution.label,
            original_url=page_url, quiet=True,
        )
        journal.audit_saved(page_url, capture.note_path, capture.tier,
                            updated=capture.updated, root=root)
        saved += 1
        if not args.quiet:
            verb = "updated" if capture.updated else "saved"
            print(f"  {indent}{verb}: {capture.note_path.name}  (depth {depth})")

    options = full_module.FullOptions(
        max_depth=args.depth,
        max_pages=args.max_pages or full_module.MAX_PAGES_DEFAULT,
        max_tier="T2" if args.no_t3 else "T3",
    )
    full_module.crawl(url, options, on_page=on_page)

    if not args.quiet:
        print()
        print(f"  full: {saved} page(s) captured, {failed} failed")
        print(f"  {'dir:':<{_LABEL_W}}{resolution.directory.resolve()}")
    return 0 if saved > 0 else 1


def do_new_topic(name: str, root: Path, *, force: bool = False) -> int:
    """Create a topic directory up front, with no URL to capture.

    Seeding a topic and filling it later are separate motions:

        mfp -n python                 create python-professor/
        mfp -py <url>                 lands there ('py' matches 'python')
        mfp -n python.async           create python-professor/async/

    Deliberately funnel-aware. Creating the directory blindly would let
    `mfp -n python` sit a fresh python-professor/ next to an existing
    py-professor/, which is exactly the duplicate the alias matcher exists to
    prevent -- so an existing match is reported instead. Idempotent: running
    it twice is harmless. Pass --new-topic to force a genuinely separate one.
    """
    registry = TopicRegistry(root)
    resolution = registry.resolve(name, force_new=force)

    where = resolution.directory.resolve()

    if resolution.is_subtopic and resolution.parent_action == "created":
        journal.audit_create(resolution.parent, resolution.alias.split(".")[0], root=root)
        print(f"  {'created:':<{_LABEL_W}}{resolution.parent.name}/")

    if resolution.action == "created":
        journal.audit_create(resolution.directory, resolution.alias, root=root)
        print(f"  {'created:':<{_LABEL_W}}{resolution.label}/")
        print(f"  {'dir:':<{_LABEL_W}}{where}")
        print(f"  {'now:':<{_LABEL_W}}mfp -{resolution.alias} <url>")
        return 0

    if resolution.action == "bound":
        journal.audit_link(resolution.alias, resolution.directory, root=root)
        print(f"  '{resolution.alias}' already covered by {resolution.label}/")
        print(f"  {'dir:':<{_LABEL_W}}{where}")
        print(f"  captures with -{resolution.alias} will land there.")
        print("  use --new-topic to make a separate directory anyway.")
        return 0

    print(f"  {resolution.label}/ already exists")
    print(f"  {'dir:':<{_LABEL_W}}{where}")
    print(f"  {'use:':<{_LABEL_W}}mfp -{resolution.alias} <url>")
    return 0


def do_topics(root: Path) -> int:
    registry = TopicRegistry(root)
    rows = registry.summary()
    if not rows:
        print("No topics yet. Try:  mfp -py https://realpython.com/decorators")
        return 0
    labels = [("  " * depth) + name for name, _, _, depth in rows]
    width = max(len(label) for label in labels)
    print(f"{'TOPIC':<{width}}  CAPTURES  ALIASES")
    for label, (_, aliases, count, _) in zip(labels, rows):
        print(f"{label:<{width}}  {count:>8}  {', '.join(aliases) or '-'}")
    return 0


def do_link(spec: str, root: Path) -> int:
    if "=" not in spec:
        print("--link expects ALIAS=TOPIC, e.g. --link ml=machine-learning-professor",
              file=sys.stderr)
        return 2
    alias, topic = (part.strip() for part in spec.split("=", 1))
    registry = TopicRegistry(root)
    try:
        directory = registry.link(alias, topic)
    except FileNotFoundError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    journal.audit_link(alias, directory, root=root)
    print(f"  '{alias}' -> {directory.name}/")
    return 0


def do_compile(target: str, as_pdf: bool, root: Path, *, open_after: bool = False) -> int:
    try:
        capture_dir = find_capture(target, root)
        output = compile_pdf(capture_dir) if as_pdf else compile_html(capture_dir)
    except CompileError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    size = output.stat().st_size / 1024
    print(f"  {output}  ({size:.0f} KB)")
    if open_after:
        _open_path(output)
    return 0


def do_retry(args, root: Path) -> int:
    rows = journal.read_failures(root)
    if not rows:
        print("  failed-attempts.csv is empty.")
        return 0

    print(f"  retrying {len(rows)} failed capture(s)")
    still_failing: list[dict[str, str]] = []
    for row in rows:
        url = row.get("url", "").strip()
        if not url:
            continue
        print(f"\n  {url}")
        code = do_capture(url, row.get("topic") or None, args, root)
        if code != 0:
            still_failing.append(row)

    # do_capture appends a fresh row for anything that failed again, so the
    # file is rewritten from the pre-retry set minus whatever now succeeds.
    fresh = journal.read_failures(root)
    seen = {r["url"] for r in still_failing}
    merged = still_failing + [r for r in fresh if r["url"] not in seen
                              and r["url"] not in {x["url"] for x in rows}]
    journal.rewrite_failures(merged, root)

    fixed = len(rows) - len(still_failing)
    print(f"\n  {fixed} recovered, {len(still_failing)} still failing")
    return 0


def do_self_test(args, root: Path) -> int:
    from tests.fixtures import FIXTURES  # noqa: PLC0415

    print(f"  running {len(FIXTURES)} fixture URLs\n")
    passed = failed = 0
    for fixture in FIXTURES:
        label = f"{fixture['kind']:<16}"
        try:
            result = fetch(fixture["url"], max_tier="T2" if args.no_t3 else "T3")
            from .extract import extract  # noqa: PLC0415

            extraction = extract(result.html, fixture["url"], capture_slug="selftest")
            ok = extraction.word_count >= 100
            expected = fixture.get("expect_fail")
            # A thin result on a page we expect to be walled is the right
            # answer, not a regression -- a paywall stub genuinely has no prose.
            if ok:
                status = "PASS"
            elif expected:
                status = "PASS*"
            else:
                status = "THIN"
            counts_as_pass = ok or bool(expected)
            passed += counts_as_pass
            failed += not counts_as_pass
            print(f"  {status:<5} {label} [{result.tier}] "
                  f"{extraction.word_count:>6}w  {fixture['url'][:58]}")
        except Exception as exc:
            expected = fixture.get("expect_fail")
            status = "PASS*" if expected else "FAIL"
            passed += bool(expected)
            failed += not expected
            reason = str(exc)[:110]
            print(f"  {status:<5} {label} {'(expected) ' if expected else ''}{reason}")

    total = passed + failed
    print(f"\n  {passed}/{total} passed  ({100 * passed // max(total, 1)}%)")
    print("  * expected failure (paywall / dead link)")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    topic_flag, rest = split_topic(raw)
    args = build_parser().parse_args(rest)
    root = _root(args)

    if args.new:
        return do_new_topic(args.new, root, force=args.new_topic)
    if args.topics:
        return do_topics(root)
    if args.rebuild:
        TopicRegistry(root).rebuild()
        print("  alias dictionary rebuilt from disk")
        return 0
    if args.link:
        return do_link(args.link, root)
    if args.compile:
        return do_compile(args.compile, args.pdf, root, open_after=args.open_after)
    if args.retry_failed:
        return do_retry(args, root)
    if args.self_test:
        return do_self_test(args, root)

    if not args.target:
        build_parser().print_help()
        return 0

    return do_capture(args.target, topic_flag, args, root)


if __name__ == "__main__":
    raise SystemExit(main())
