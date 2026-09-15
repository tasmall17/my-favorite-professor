"""Capturing a whole GitHub subtree as one readable document.

The page tier is useless on a repository. `mfp <repo url>` fetches the rendered
HTML, which is a file listing and a README -- none of the code, and none of the
directories below. Reading a codebase means reading its files, so this module
walks the tree instead of the page.

**It does not crawl.** The obvious implementation follows the directory links,
fetches each listing, recurses, and comes back up. That is one request per
directory, plus one per file, all serialised on the previous response. GitHub
will hand over the entire recursive tree in a single call:

    GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1

which returns every path, type and **size** in the repository at once. Sizes
arriving before any content is the thing that matters: a 4 MB lockfile, a
minified bundle and a PNG are all rejected without being downloaded. Only the
files that survive selection are fetched, and those go out concurrently.

The output is a single Markdown note: frontmatter, a directory tree for
orientation, a table of contents, then every selected file in path order.
Markdown files are inlined with their headings pushed down so they nest under
their own filename; everything else is fenced with a language tag.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from .fetch import TIMEOUT, _ssl_context

API_ROOT = "https://api.github.com"
RAW_ROOT = "https://raw.githubusercontent.com"

# A ref can contain slashes ('release/2.0'), and the URL gives no hint where the
# ref stops and the path starts. Rather than a branch listing on every capture,
# the first segment is tried and more are folded in only if that 404s. Three
# covers 'release/2.0/rc1'; beyond that the ref is pathological.
MAX_REF_SEGMENTS = 3

# Per-file and whole-capture ceilings. The note is meant to be read, and to fit
# in a context window alongside a question about it.
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_FILES = 400

# Machinery, dependencies and build output. Present in the tree, never the
# material: nobody studies a repository by reading its vendored dependencies.
SKIP_DIRS = frozenset({
    ".git", ".github", ".svn", ".hg", ".idea", ".vscode",
    "node_modules", "bower_components", "vendor", "third_party",
    "dist", "build", "out", "target", "bin", "obj",
    ".next", ".nuxt", ".output", ".svelte-kit", ".parcel-cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "env", "site-packages", "eggs", ".eggs",
    "coverage", "htmlcov", ".nyc_output", "testdata", "fixtures",
    ".terraform", ".gradle", "Pods", "DerivedData",
})

# Generated, and generated files are noise: enormous, unread, and they crowd out
# the source they were built from.
SKIP_FILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock", "conda-lock.yml",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum", "flake.lock",
    "mix.lock", "packages.lock.json", "gradle.lockfile",
})

SKIP_GLOBS = ("*.min.js", "*.min.css", "*.min.mjs", "*.map", "*.lock",
              "*.snap", "*.pb.go", "*_pb2.py", "*.generated.*", "*.g.dart")

# Extension -> fenced-code language tag. Membership is also what makes a file
# eligible: an extension absent from here and from TEXT_FILENAMES is assumed
# binary or uninteresting, which is the right default for a tree that can hold
# anything at all.
LANGUAGES = {
    ".py": "python", ".pyi": "python", ".rb": "ruby", ".go": "go",
    ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".m": "objectivec", ".mm": "objectivec",
    ".swift": "swift", ".cs": "csharp", ".fs": "fsharp", ".scala": "scala",
    ".clj": "clojure", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    ".hs": "haskell", ".ml": "ocaml", ".lua": "lua", ".pl": "perl",
    ".php": "php", ".r": "r", ".jl": "julia", ".dart": "dart", ".zig": "zig",
    ".nim": "nim", ".v": "v", ".sol": "solidity",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx", ".vue": "vue",
    ".svelte": "svelte", ".astro": "astro",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".sass": "sass", ".less": "less",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
    ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".sql": "sql", ".graphql": "graphql", ".gql": "graphql",
    ".json": "json", ".jsonc": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "conf",
    ".xml": "xml", ".svg": "xml", ".proto": "protobuf", ".tf": "terraform",
    ".tfvars": "terraform", ".hcl": "hcl", ".dockerfile": "dockerfile",
    ".gradle": "groovy", ".groovy": "groovy", ".cmake": "cmake",
    ".mk": "makefile", ".nix": "nix", ".vim": "vim", ".el": "lisp",
    ".tex": "latex", ".bib": "bibtex", ".diff": "diff", ".patch": "diff",
    ".env": "bash", ".properties": "properties", ".gitignore": "gitignore",
    ".txt": "", ".text": "", ".rst": "rst", ".adoc": "asciidoc",
    ".csv": "csv", ".tsv": "tsv",
}

# Extensionless files that are still text, and worth reading.
TEXT_FILENAMES = frozenset({
    "README", "LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS",
    "CONTRIBUTORS", "CHANGELOG", "CHANGES", "TODO", "VERSION", "CODEOWNERS",
    "Makefile", "GNUmakefile", "Dockerfile", "Containerfile", "Vagrantfile",
    "Jenkinsfile", "Procfile", "Rakefile", "Gemfile", "Brewfile", "Justfile",
    "justfile", "BUILD", "WORKSPACE", ".gitignore", ".gitattributes",
    ".editorconfig", ".dockerignore", ".env.example", ".nvmrc",
})

MARKDOWN_EXTS = frozenset({".md", ".markdown", ".mdown", ".mkd", ".mdx"})


class RepoError(Exception):
    """A repository capture that could not be completed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RepoTarget:
    owner: str
    repo: str
    ref: str | None = None      # None means the repository's default branch
    path: str = ""              # "" means the repository root

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def web_url(self, ref: str | None = None) -> str:
        ref = ref or self.ref
        if not ref:
            return f"https://github.com/{self.slug}"
        if not self.path:
            return f"https://github.com/{self.slug}/tree/{ref}"
        return f"https://github.com/{self.slug}/tree/{ref}/{self.path}"


@dataclass
class Entry:
    path: str
    size: int

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.path).suffix.lower()


@dataclass
class Selection:
    kept: list[Entry] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


@dataclass
class RepoOptions:
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_files: int = MAX_FILES
    only: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()


# --------------------------------------------------------------------- URL

_HOSTS = {"github.com", "www.github.com"}


def parse_repo_url(url: str, *, shorthand: bool = False) -> RepoTarget | None:
    """A GitHub repo/tree/blob URL as a target, or None if it is not one.

    Returning None rather than raising is what lets the CLI use this as the
    test for "should this go to the repo tier?" -- every other URL, including
    a GitHub issue or pull request, falls through to the ordinary page fetch.

    `shorthand` additionally accepts a bare `owner/repo[/tree/ref/path]`. It is
    off by default because a bare slug is indistinguishable from a relative
    file path, and only `--repo` says which of the two was meant.
    """
    text = url.strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return None

    if shorthand and not parsed.scheme and "/" in text and "." not in text.split("/")[0]:
        parts = [unquote(p) for p in text.strip("/").split("/") if p]
    else:
        if parsed.scheme not in {"http", "https"}:
            return None
        if parsed.netloc.lower() not in _HOSTS:
            return None
        parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not owner or not repo:
        return None

    rest = parts[2:]
    if not rest:
        return RepoTarget(owner, repo)
    # Only listings and files describe a subtree. Issues, pulls, releases and
    # the rest are ordinary pages and belong to the page tier.
    if rest[0] not in {"tree", "blob"}:
        return None
    if len(rest) == 1:
        return RepoTarget(owner, repo)
    # The ref/path boundary is genuinely ambiguous here; resolved against the
    # API in fetch_tree().
    return RepoTarget(owner, repo, ref=rest[1], path="/".join(rest[2:]))


# ------------------------------------------------------------------- HTTP

def _token() -> str | None:
    """A GitHub token, if one can be had without asking.

    Unauthenticated callers get 60 requests an hour against the whole API, which
    a handful of captures will exhaust. `gh` is already installed and logged in
    for most people who would use this, so its token is worth borrowing before
    falling back to anonymous access.
    """
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:
        done = subprocess.run(["gh", "auth", "token"], capture_output=True,
                              text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    token = done.stdout.strip()
    return token if done.returncode == 0 and token else None


def _headers(token: str | None, *, api: bool) -> dict[str, str]:
    headers = {"User-Agent": "my-favorite-professor"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _explain_http(response: httpx.Response, target: RepoTarget,
                  token: str | None) -> str:
    if response.status_code == 404:
        return (f"{target.slug} not found. It may be private, renamed, or the "
                f"branch may not exist"
                + ("" if token else " (no GitHub token found, so private "
                   "repositories are invisible)"))
    if response.status_code in (401, 403):
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return ("GitHub API rate limit reached. "
                    + ("Try again shortly" if token else
                       "Run `gh auth login`, or set GITHUB_TOKEN, to raise the "
                       "limit from 60 to 5000 an hour"))
        return f"GitHub refused the request ({response.status_code})"
    return f"GitHub returned HTTP {response.status_code}"


def fetch_tree(target: RepoTarget, client: httpx.Client,
               token: str | None) -> tuple[list[Entry], str, bool]:
    """Every path under the repository, in one request.

    Returns (entries, resolved_ref, truncated). Entries are repo-relative and
    cover the whole repository; narrowing to the target's subpath is selection's
    job, not the API's -- one unfiltered call is cheaper than one per directory.
    """
    candidates: list[tuple[str, str]] = []
    if target.ref:
        segments = [target.ref, *target.path.split("/")] if target.path else [target.ref]
        for count in range(1, min(MAX_REF_SEGMENTS, len(segments)) + 1):
            candidates.append(("/".join(segments[:count]),
                               "/".join(segments[count:])))
    else:
        candidates.append((_default_branch(target, client, token), target.path))

    last: httpx.Response | None = None
    for ref, path in candidates:
        url = f"{API_ROOT}/repos/{target.slug}/git/trees/{ref}"
        response = client.get(url, params={"recursive": "1"},
                              headers=_headers(token, api=True))
        if response.status_code == 200:
            payload = response.json()
            entries = [
                Entry(item["path"], int(item.get("size") or 0))
                for item in payload.get("tree", [])
                if item.get("type") == "blob"
            ]
            target.ref, target.path = ref, path
            return entries, ref, bool(payload.get("truncated"))
        last = response

    raise RepoError(_explain_http(last, target, token) if last is not None
                    else "could not read the repository tree")


def _default_branch(target: RepoTarget, client: httpx.Client,
                    token: str | None) -> str:
    response = client.get(f"{API_ROOT}/repos/{target.slug}",
                          headers=_headers(token, api=True))
    if response.status_code != 200:
        raise RepoError(_explain_http(response, target, token))
    return response.json().get("default_branch") or "main"


def fetch_blobs(entries: list[Entry], target: RepoTarget, client: httpx.Client,
                token: str | None, *, on_progress=None) -> dict[str, str]:
    """Fetch the selected files concurrently, decoded as text.

    Concurrent because this is the only part that scales with repository size,
    and each request is independent. A file that fails or turns out to be binary
    is dropped rather than fatal: one unreadable file should not cost the other
    two hundred.
    """
    contents: dict[str, str] = {}
    done = 0

    def one(entry: Entry) -> tuple[str, str | None]:
        url = f"{RAW_ROOT}/{target.slug}/{target.ref}/{entry.path}"
        try:
            response = client.get(url, headers=_headers(token, api=False))
        except httpx.HTTPError:
            return entry.path, None
        if response.status_code != 200:
            return entry.path, None
        raw = response.content
        # A null byte means binary, whatever the extension claimed.
        if b"\x00" in raw[:8192]:
            return entry.path, None
        try:
            return entry.path, raw.decode("utf-8")
        except UnicodeDecodeError:
            return entry.path, raw.decode("utf-8", errors="replace")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for path, text in pool.map(one, entries):
            done += 1
            if on_progress:
                on_progress(done, len(entries))
            if text is not None:
                contents[path] = text
    return contents


# -------------------------------------------------------------- selection

def _is_skipped_path(entry: Entry, root: str) -> str | None:
    relative = entry.path[len(root):].lstrip("/") if root else entry.path
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        if part in SKIP_DIRS:
            return "in a build or dependency directory"
    if entry.name in SKIP_FILES:
        return "generated lockfile"
    if any(fnmatch.fnmatch(entry.name, pattern) for pattern in SKIP_GLOBS):
        return "generated or minified"
    return None


def _is_text(entry: Entry) -> bool:
    if entry.suffix in LANGUAGES or entry.suffix in MARKDOWN_EXTS:
        return True
    return entry.name in TEXT_FILENAMES or PurePosixPath(entry.path).stem in TEXT_FILENAMES


def select(entries: list[Entry], target: RepoTarget,
           options: RepoOptions) -> Selection:
    """Decide what goes in the document, cheaply, before anything is fetched."""
    root = target.path.strip("/")
    selection = Selection()
    total = 0

    under_root = [
        e for e in entries
        if not root or e.path == root or e.path.startswith(root + "/")
    ]
    if not under_root:
        raise RepoError(
            f"nothing under {root or '/'} in {target.slug}@{target.ref}"
        )

    for entry in sorted(under_root, key=lambda e: e.path):
        reason = _is_skipped_path(entry, root)
        if reason:
            selection.skip(reason)
            continue
        if options.only and not any(
            fnmatch.fnmatch(entry.path, p) or fnmatch.fnmatch(entry.name, p)
            for p in options.only
        ):
            selection.skip("not matched by --only")
            continue
        if any(fnmatch.fnmatch(entry.path, p) or fnmatch.fnmatch(entry.name, p)
               for p in options.skip):
            selection.skip("matched by --skip")
            continue
        if not _is_text(entry):
            selection.skip("binary or unrecognised type")
            continue
        if entry.size > options.max_file_bytes:
            selection.skip(f"larger than {options.max_file_bytes // 1024} KB")
            continue
        if len(selection.kept) >= options.max_files:
            selection.skip(f"over the {options.max_files}-file limit")
            selection.truncated = True
            continue
        if total + entry.size > options.max_total_bytes:
            selection.skip(f"over the {options.max_total_bytes // 1024} KB budget")
            selection.truncated = True
            continue
        selection.kept.append(entry)
        total += entry.size

    if not selection.kept:
        raise RepoError(
            f"no readable text files under {root or '/'} in {target.slug} "
            f"({sum(selection.skipped.values())} entries all filtered out)"
        )
    return selection


# -------------------------------------------------------------- rendering

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def shift_headings(text: str, by: int = 2) -> str:
    """Push every ATX heading down, so a file's headings nest under its name.

    Fenced blocks are tracked and left alone. Without that, a shell example
    containing a '# comment' line becomes a document heading and lands in the
    table of contents -- which is how naively concatenated Markdown ends up
    with a contents list full of code comments.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        match = _FENCE_RE.match(line)
        if match:
            token = match.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            out.append(line)
            continue
        if fence is None:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                hashes = len(stripped) - len(stripped.lstrip("#"))
                if 1 <= hashes <= 6 and (len(stripped) == hashes
                                         or stripped[hashes] in " \t"):
                    line = "#" * min(hashes + by, 6) + stripped[hashes:]
        out.append(line)
    return "\n".join(out)


def _fence_for(text: str) -> str:
    """A fence long enough to survive whatever backticks the file contains."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_tree(paths: list[str], root: str) -> str:
    """An indented directory listing, for orientation before the contents."""
    lines: list[str] = []
    seen: set[str] = set()
    for path in paths:
        relative = path[len(root):].lstrip("/") if root else path
        parts = PurePosixPath(relative).parts
        for depth in range(len(parts) - 1):
            directory = "/".join(parts[:depth + 1])
            if directory not in seen:
                seen.add(directory)
                lines.append(f"{'  ' * depth}{parts[depth]}/")
        lines.append(f"{'  ' * (len(parts) - 1)}{parts[-1]}")
    return "\n".join(lines)


def _anchor(path: str) -> str:
    """GitHub-style anchor for a '## path' heading."""
    slug = re.sub(r"[^a-z0-9 _-]", "", path.lower()).strip().replace(" ", "-")
    return slug.replace("/", "").replace(".", "")


def render(target: RepoTarget, selection: Selection, contents: dict[str, str],
           *, title: str, topic: str, captured: str) -> str:
    """The whole note: frontmatter, orientation, contents, then the files."""
    root = target.path.strip("/")
    kept = [e for e in selection.kept if e.path in contents]
    total = sum(len(contents[e.path].encode("utf-8")) for e in kept)

    def rel(path: str) -> str:
        return path[len(root):].lstrip("/") if root else path

    head = [
        "---",
        f'title: "{title}"',
        f"url: {target.web_url()}",
        'site: "GitHub"',
        f"captured: {captured}",
        f'topic: "{topic}"',
        f'repo: "{target.slug}"',
        f'ref: "{target.ref}"',
        f'subtree: "{root or "/"}"',
        f"files: {len(kept)}",
        "tags: [mfp/repo]",
        "---",
        "",
        f"# {title}",
        "",
        f"> [{target.slug}]({target.web_url()}) · `{target.ref}`"
        f" · {len(kept)} files · {total // 1024} KB",
        "",
    ]

    if selection.truncated:
        head += ["> **Truncated.** The subtree exceeded the size budget; "
                 "raise it with `--max-bytes`.", ""]

    if selection.skipped:
        summary = ", ".join(f"{count} {reason}"
                            for reason, count in sorted(selection.skipped.items()))
        head += [f"> Skipped: {summary}.", ""]

    head += ["## Tree", "", "```", render_tree([e.path for e in kept], root),
             "```", "", "## Contents", ""]
    head += [f"- [{rel(e.path)}](#{_anchor(rel(e.path))})" for e in kept]
    head += [""]

    body: list[str] = []
    for entry in kept:
        text = contents[entry.path].strip("\n")
        body += ["---", "", f"## {rel(entry.path)}", ""]
        if entry.suffix in MARKDOWN_EXTS:
            # Inlined, not fenced: this is prose, and the point of the document
            # is to read it. Headings shift so the file's own H1 sits under the
            # H2 naming the file rather than beside it.
            body += [shift_headings(text, by=2), ""]
        else:
            fence = _fence_for(text)
            body += [f"{fence}{LANGUAGES.get(entry.suffix, '')}", text, fence, ""]

    return "\n".join(head + body).rstrip() + "\n"


# ----------------------------------------------------------------- driver

@dataclass
class RepoResult:
    markdown: str
    title: str
    target: RepoTarget
    files: int
    bytes_written: int
    selection: Selection


def capture_repo(url: str, *, topic: str, options: RepoOptions | None = None,
                 on_status=None) -> RepoResult:
    """Walk a GitHub subtree and return it as one Markdown document."""
    from datetime import datetime  # noqa: PLC0415

    target = parse_repo_url(url, shorthand=True)
    if target is None:
        raise RepoError(
            f"not a GitHub repository: {url}. Expected a github.com URL, or "
            f"owner/repo"
        )

    options = options or RepoOptions()
    token = _token()
    say = on_status or (lambda _msg: None)

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      verify=_ssl_context()) as client:
        say("reading the tree")
        entries, ref, truncated = fetch_tree(target, client, token)
        if truncated:
            say("GitHub truncated the tree; some paths are missing")

        selection = select(entries, target, options)
        selection.truncated = selection.truncated or truncated
        say(f"fetching {len(selection.kept)} files")
        contents = fetch_blobs(selection.kept, target, client, token)

    root = target.path.strip("/")
    title = f"{target.slug}/{root}" if root else target.slug
    title = f"{title} @ {ref}"
    captured = datetime.now().strftime("%Y-%m-%d %H:%M")
    markdown = render(target, selection, contents,
                      title=title, topic=topic, captured=captured)
    return RepoResult(
        markdown=markdown,
        title=title,
        target=target,
        files=len([e for e in selection.kept if e.path in contents]),
        bytes_written=len(markdown.encode("utf-8")),
        selection=selection,
    )


def write_repo_capture(result: RepoResult, *, topic_dir, topic: str,
                       source: str = "user"):
    """File a repository document as an ordinary capture.

    Deliberately the same bundle a fetched page produces -- note in the
    references directory, archive under .captures/, manifest beside it -- so
    --compile, the Downloads mirror and the reading pane need to know nothing
    about repositories. The archived artifact is the Markdown itself rather
    than a DOM, because that *is* the original here.
    """
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from .capture import MANIFEST_NAME, CaptureResult, capture_slug  # noqa: PLC0415
    from .paths import (  # noqa: PLC0415
        captures_dir,
        ensure_topic_layout,
        refs_dir_for,
        safe_filename,
    )

    ensure_topic_layout(topic_dir)
    url = result.target.web_url()
    slug = capture_slug(url)
    capture_dir = captures_dir(topic_dir) / slug

    prior = None
    manifest_path = capture_dir / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = None

    if capture_dir.exists():
        shutil.rmtree(capture_dir)
    (capture_dir / "assets").mkdir(parents=True, exist_ok=True)
    (capture_dir / "original.md").write_text(result.markdown, encoding="utf-8")

    body = result.markdown.split("---\n", 2)[-1]
    try:
        from ..markdown import to_html  # noqa: PLC0415

        rendered = to_html(body)
    except Exception:  # noqa: BLE001 - page.html is a convenience, never the note
        rendered = f"<pre>{result.markdown}</pre>"
    (capture_dir / "page.html").write_text(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{result.title}</title>"
        "<style>body{max-width:52rem;margin:2rem auto;padding:0 1rem;"
        "font:16px/1.6 system-ui,sans-serif}pre{overflow-x:auto;padding:.8rem;"
        "background:#f5f5f4;border-radius:6px}code{font-size:.85em}</style>"
        f"</head><body><article>{rendered}</article></body></html>",
        encoding="utf-8",
    )

    note_name = f"{safe_filename(result.title)}.md"
    note_path = refs_dir_for(topic_dir, source) / note_name
    note_path.write_text(result.markdown, encoding="utf-8")

    if prior and prior.get("note") and prior["note"] != note_name:
        stale = refs_dir_for(topic_dir, prior.get("source", source)) / prior["note"]
        if stale.exists():
            stale.unlink()

    manifest = {
        "url": url,
        "final_url": url,
        "title": result.title,
        "author": None,
        "site": "GitHub",
        "topic": topic,
        "note": note_name,
        "source": source,
        "origin": "repo",
        "repo": result.target.slug,
        "ref": result.target.ref,
        "subtree": result.target.path or "/",
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tier": "REPO",
        "from_archive": False,
        "archive_timestamp": None,
        "word_count": len(result.markdown.split()),
        "files": result.files,
        "assets_kept": 0,
        "assets_failed": 0,
        "assets_dropped": 0,
        "assets": {},
    }
    (capture_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return CaptureResult(
        note_path=note_path,
        capture_dir=capture_dir,
        title=result.title,
        tier="REPO",
        word_count=manifest["word_count"],
        assets_kept=0,
        assets_failed=0,
        updated=prior is not None,
        from_archive=False,
        source=source,
    )
