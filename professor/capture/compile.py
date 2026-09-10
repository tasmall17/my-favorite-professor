"""Rebuilding a capture into a single portable file.

The contract that makes this worth having: **compile is offline and
deterministic.** Capture directory in, artifact out, zero network. That is both
the feature and the test -- if it needs the internet, the archive wasn't
complete in the first place.

Two outputs, for two different readers:

  * single-file HTML -- assets inlined as data: URIs, so one file opens
    anywhere with images intact.
  * PDF -- rendered from that HTML over file://, so the PDF matches the
    archive rather than a fresh fetch of a page that may have changed.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from bs4 import BeautifulSoup

from .capture import MANIFEST_NAME

MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".svg": "image/svg+xml", ".bmp": "image/bmp",
}


class CompileError(Exception):
    pass


def _data_uri(path: Path) -> str | None:
    if not path.is_file():
        return None
    mime = MIME_BY_EXT.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    if not mime:
        mime = "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _inline(soup: BeautifulSoup, capture_dir: Path) -> int:
    """Replace every local asset reference with an inline data: URI."""
    inlined = 0

    def resolve(value: str | None) -> str | None:
        nonlocal inlined
        if not value or value.startswith(("data:", "http://", "https://", "//")):
            return None
        uri = _data_uri((capture_dir / value).resolve())
        if uri:
            inlined += 1
        return uri

    for img in soup.find_all("img"):
        uri = resolve(img.get("src"))
        if uri:
            img["src"] = uri
        img.attrs.pop("srcset", None)

    for source in soup.find_all("source"):
        uri = resolve(source.get("srcset"))
        if uri:
            source["srcset"] = uri

    for el in soup.select("[style*='assets/']"):
        style = el.get("style", "")
        for token in set(part.strip("'\"") for part in style.split("url(")[1:]):
            ref = token.split(")")[0].strip("'\"")
            uri = resolve(ref)
            if uri:
                style = style.replace(ref, uri)
        el["style"] = style

    # Remote stylesheets and scripts can't resolve offline; drop them so the
    # rendered page doesn't sit waiting on requests that will never complete.
    for tag in soup.find_all("link", rel="stylesheet"):
        if (tag.get("href") or "").startswith(("http", "//")):
            tag.decompose()
    # Every script goes, not just remote ones. The capture already holds the
    # post-JS DOM, so scripts can only do harm here: re-render the page into
    # something else, wipe content on load, or phone home from an inline
    # snippet. An archive is for reading, not for re-running the site.
    for tag in soup.find_all("script"):
        tag.decompose()

    # <noscript> is where analytics SDKs and tracking pixels hide, and parsers
    # treat its contents as text so element loops never see them. Opening the
    # archive offline would otherwise still try to ping Facebook et al.
    for tag in soup.find_all("noscript"):
        tag.decompose()

    return inlined


def compile_html(capture_dir: Path, output: Path | None = None) -> Path:
    """Fold a capture into one self-contained .html file."""
    capture_dir = Path(capture_dir).expanduser().resolve()
    page = capture_dir / "page.html"
    if not page.exists():
        raise CompileError(f"no page.html in {capture_dir}")

    soup = BeautifulSoup(page.read_text(encoding="utf-8"), "lxml")
    _inline(soup, capture_dir)

    manifest_path = capture_dir / MANIFEST_NAME
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if soup.head:
                note = soup.new_tag("meta")
                note["name"] = "mfp-source"
                note["content"] = manifest.get("url", "")
                soup.head.append(note)
        except (json.JSONDecodeError, OSError):
            pass

    output = Path(output) if output else capture_dir / "standalone.html"
    output.write_text(str(soup), encoding="utf-8")
    return output


def compile_pdf(capture_dir: Path, output: Path | None = None) -> Path:
    """Render the standalone HTML to PDF via headless Chromium, over file://."""
    capture_dir = Path(capture_dir).expanduser().resolve()
    standalone = compile_html(capture_dir)
    output = Path(output) if output else capture_dir / "standalone.pdf"

    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:
        raise CompileError("playwright is required for PDF output") from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(standalone.as_uri(), wait_until="load", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.pdf(
                path=str(output),
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "14mm",
                        "left": "12mm", "right": "12mm"},
            )
        finally:
            browser.close()

    _warn_if_unwieldy(output)
    return output


# Chat upload limits are the practical ceiling for the PDF delivery path. A
# book-length page compiles happily and then fails at upload time, which is a
# rotten place to discover the problem.
PDF_WARN_BYTES = 30 * 1024 * 1024


def _warn_if_unwieldy(pdf: Path) -> None:
    try:
        size = pdf.stat().st_size
    except OSError:
        return
    if size > PDF_WARN_BYTES:
        mb = size / (1024 * 1024)
        print(
            f"  note: {mb:.0f} MB is likely too large to upload to a chat.\n"
            f"        For Claude Code, point it at the capture directory instead:\n"
            f"        {pdf.parent}"
        )


def find_capture(target: str, root: Path) -> Path:
    """Accept a capture directory, a note path, or a bare capture name."""
    candidate = Path(target).expanduser()
    if candidate.is_dir() and (candidate / "page.html").exists():
        return candidate.resolve()

    # A .md note -> the capture its manifest points back to.
    #
    # Notes now live one level deeper than they used to, inside
    # usr-references-provided/ or claude-references-provided/, while .captures/
    # stays put at the top of the topic. So the note's own directory is checked
    # first (the original flat layout, and any note left there) and its parent
    # second. The manifest's `note` field is a bare filename either way.
    if candidate.is_file() and candidate.suffix == ".md":
        for topic_dir in (candidate.parent, candidate.parent.parent):
            if (topic_dir / ".captures").is_dir():
                break
        for capture in sorted((topic_dir / ".captures").glob("*")):
            manifest = capture / MANIFEST_NAME
            if manifest.exists():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if data.get("note") == candidate.name:
                    return capture.resolve()

    # Two depths, because a subtopic keeps its own .captures/ one level down.
    # Globbed explicitly rather than with "**" so the search still cannot
    # descend into assets/ directories or a topic's own captures-within-captures.
    def at_either_depth(pattern: str) -> list[Path]:
        return [
            p
            for depth in ("*", "*/*")
            for p in root.glob(f"{depth}/.captures/{pattern}")
            if p.is_dir()
        ]

    matches = at_either_depth(target)
    if not matches:
        matches = at_either_depth(f"*{target}*")
    if len(matches) == 1:
        return matches[0].resolve()
    if len(matches) > 1:
        # Name alone is ambiguous once the same page can be captured into both
        # a topic and one of its subtopics, so say which topic each one is in.
        # The full path from the library root, not the containing directory's
        # name: a bare "react" beside "js-professor" reads like a second
        # top-level topic rather than a child of the one listed next to it.
        def topic_label(capture: Path) -> str:
            try:
                return capture.parent.parent.relative_to(root).as_posix()
            except ValueError:
                return capture.parent.parent.name

        names = ", ".join(f"{topic_label(p)}/{p.name}" for p in matches[:5])
        raise CompileError(f"{target!r} matches several captures: {names}")
    raise CompileError(f"no capture found for {target!r}")
