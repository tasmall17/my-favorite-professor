# mfp(1)

## NAME

**mfp**, **my-fav-professor** — capture a web page into a readable Markdown note plus a hidden, re-compilable archive

## SYNOPSIS

```
mfp [-TOPIC] URL [--open] [--no-t3] [--timeout SECONDS] [--quiet]
mfp [-TOPIC] REPO [--repo] [--only GLOB] [--skip GLOB] [--max-bytes N]
mfp [-TOPIC] URL --full [--depth N] [--max-pages N]
mfp -n NAME [--new-topic]
mfp --compile CAPTURE [--pdf | --html] [--open]
mfp --topics | --link ALIAS=TOPIC | --rebuild
mfp --retry-failed | --self-test
mfp --help | --version
```

`mfp` and `my-fav-professor` are the same program. Every option below works
under either name.

## DESCRIPTION

**mfp** saves a web page you are reading into two things at once:

* a **Markdown note** you can read offline — the only file visible in the
  topic directory;
* a **hidden archive** (`page.html`, normalised images, a manifest) that
  reconstructs the page, and that a language model can open and see.

It is built for one motion: you are mid-article, you decide you want to keep
it, you run one command, and you go back to reading. The tool picks the
directory, converts the page, downloads and normalises the images, and records
what it did. If it cannot get the page it says so and writes a machine-readable
row you can act on later.

Captures are filed under **topics**, which you invent at the moment you need
them (see **TOPIC FLAGS**).

## OPTIONS

### Capturing

`-TOPIC`
: Any flag-shaped word that is not a reserved option is read as a topic.
  `mfp -py URL` files under `py-professor/`. See **TOPIC FLAGS**.

`--topic NAME`
: Specify the topic explicitly. The escape hatch for a topic whose name
  collides with a reserved option — `mfp --topic list URL`.

`--new-topic`
: Force a new directory instead of binding to a similar existing one. This is
  a modifier, not a command — it changes what `-TOPIC` or `-n` does. Compare:
  `mfp -n python` *creates a topic*; `mfp --new-topic -go URL` *captures*
  while refusing to merge into an existing `google-professor/`.

`--no-t3`
: Stop after the headless browser. Skips the stealth and archive tiers, which
  makes a failure fail faster.

`--timeout SECONDS`
: Per-request timeout. Default 25.

`--open`
: Open the result when finished — the note after a capture, the compiled file
  after `--compile`.

`--quiet`, `-q`
: Suppress progress output. Errors still print.

`--library PATH`
: Use a different library root for this run. See **ENVIRONMENT**.

### Re-compiling

`--compile CAPTURE`
: Rebuild a capture into a single portable file. `CAPTURE` may be the capture
  directory, the `.md` note, or any unambiguous fragment of the capture name.

`--html`
: With `--compile`, produce a self-contained `.html` with images inlined as
  `data:` URIs. This is the default.

`--pdf`
: With `--compile`, produce a `.pdf`.

### Topics

`-n NAME`, `--new NAME`
: Create a topic directory without capturing anything, so you can set up a
  topic before you have a page for it:

  ```
  mfp -n python        →  creates python-professor/
  mfp -py URL          →  lands there, because 'py' matches 'python'
  ```

  Idempotent, and funnel-aware. If a matching topic already exists it reports
  that instead of creating a near-duplicate — `mfp -n python` with
  `py-professor/` already present will point you at `py-professor/` rather
  than adding a second directory for the same subject. Combine with
  `--new-topic` to force a genuinely separate one.

`--topics`
: List topic directories with their capture counts and aliases.

`--link ALIAS=TOPIC`
: Bind an alias to an existing topic directory by hand. The fix for any wrong
  automatic guess, and the only way to teach it a synonym.

`--rebuild`
: Resynchronise the alias dictionary with what is on disk. Aliases whose target
  directory still exists are preserved, including hand-made links.

### Maintenance

`--retry-failed`
: Re-run every row in `failed-attempts.csv`. Rows that now succeed are dropped
  from the file.

`--self-test`
: Fetch a list of fixture URLs spanning every category the tool claims to
  handle and report which worked, at which tier, with what word count. Writes
  nothing to the library.

`--help`, `--version`
: Usage, and version.

## TOPIC FLAGS

Topics are invented at the call site. There is no list to maintain.

```
mfp -py        URL  →  py-professor/
mfp -rust      URL  →  rust-professor/
mfp -py.async  URL  →  py-professor/async/
mfp            URL  →  inbox/
```

An alias can be any length — `-py`, `-pyt` and `-python` all reach the same
professor. A dot nests one level, for a subject that belongs *under* a
professor rather than beside it.

If the topic does not exist it is created. If it does, the capture is added to
it. The interesting case is the near-miss: you have `py-professor/` with three
things in it, and later you type

```
mfp -python https://peps.python.org/pep-0318/
```

This does **not** create a second directory. It reports

```
topic: 'python' -> existing py-professor/  (use --new-topic to separate)
```

### Repositories

A `github.com` URL pointing at a repository, a `tree/` listing or a `blob/`
file is walked rather than fetched as a page. The page tier is actively wrong
on one: it renders a file listing and a README and calls that the material.

```
mfp -py https://github.com/owner/repo                walks the default branch
mfp -py https://github.com/owner/repo/tree/main/docs walks that subtree
mfp -py --repo owner/repo                            shorthand, no URL needed
mfp -py --page https://github.com/owner/repo         the rendered page instead
```

The whole recursive tree arrives in one API call, so nothing is crawled
directory by directory. That call carries every file's **size**, which is what
lets a 4 MB lockfile, a minified bundle and a PNG be rejected before any of
them is downloaded. Only the survivors are fetched, concurrently.

The result is one note: frontmatter, a directory tree, a table of contents,
then every file in path order. Markdown files are inlined with their headings
pushed down so they nest under their own filename; everything else is fenced
with a language tag, using a fence long enough to survive backticks inside the
file.

Left out by default: dependency and build directories (`node_modules`,
`vendor`, `dist`, `target`, `.venv` and friends), lockfiles, minified and
generated files, and anything binary or of unrecognised type.

| Option | Effect |
|---|---|
| `--only GLOB` | Include only matching paths. Repeatable. |
| `--skip GLOB` | Exclude matching paths. Repeatable. |
| `--max-bytes N` | Raise or lower the 2 MB budget. |
| `--repo` | Force the walk, and accept `owner/repo` shorthand. |
| `--page` | Capture a GitHub URL as an ordinary page. |

Ceilings are 256 KB per file, 2 MB and 400 files per capture. Hitting one is
reported in the note itself, not silently. GitHub allows 60 API requests an
hour anonymously and 5000 authenticated, so a token from `gh auth login` or
`GITHUB_TOKEN` is used when one is available. It is also what makes private
repositories visible.

### Full-site crawl

`--full` treats the URL as the entry point into a site rather than a single
page: it fetches it, extracts every link on it, and walks those the same way
— a link found three pages in is followed exactly like one found on the
first page. There is no separate recursion step; a visited set is what makes
the walk stop rather than a depth counter, so a normal site simply runs out
of new links to find.

```
mfp -py --full https://docs.example.com/guide/
mfp -py --full --depth 2 https://docs.example.com/guide/
mfp -py --full --max-pages 50 https://docs.example.com/guide/
```

Everything the crawl finds — including the origin page itself — lands in one
subfolder, named from the origin URL and resolved exactly as `-py.<page>`
would resolve by hand. Each page is written with the ordinary capture
pipeline (note, archive, manifest); only the per-page asset-download chatter
is suppressed, since a crawl of dozens of pages needs one status line per
page, not each page's own image-fetch noise.

Only links on the same domain as the origin page are followed; anything
pointing elsewhere is left alone. A page that fails to fetch is recorded to
`failed-attempts.csv` and skipped — one broken link does not stop the rest of
the site from being captured.

| Option | Effect |
|---|---|
| `--depth N` | Stop following links after N hops from the origin page. Unlimited by default. |
| `--max-pages N` | Total page budget for the crawl (default 200). The real safety valve against a site that never runs out of "new" links (infinite pagination, a calendar widget). |

### Subtopics

`-py.async` files into `py-professor/async/`. The subtopic is a separate
subject with the same four directories a topic has — its own notes, its own
`.captures/`, its own syllabus — sitting inside the professor it belongs to.

Nesting stops at one level. A second dot is read as part of the subtopic's
name rather than a grandchild, because the layout has exactly two levels and
a third would file captures where `--compile` cannot find them.

Everything true of topics is true of subtopics. `-py.asy` binds to an
existing `async/` by the same rules below, the binding is written back as the
dotted key `py.async`, and a directory you create by hand with `mkdir` is
adopted on the next run. `--new-topic` applies to the subtopic alone, so
`mfp --new-topic -py.async URL` makes a second `async/` under the *existing*
professor rather than a second professor.

### How the matching works

`.mfp/topics.json` is a dictionary of alias → directory. Resolution is
**normalize → dictionary lookup → slow path only on a miss**:

1. **Exact hit.** Return the directory. O(1).
2. **Miss.** Strip the `-professor` suffix from both the alias and every
   candidate, then bind if either stem is a prefix of the other (minimum
   length 2), or if their similarity ratio is at least 0.72.
3. **No match.** Create `TOPIC-professor/` and register the alias.

For a dotted flag this runs twice: once for the professor, then again over
that professor's subtopics. Only the parent carries the `-professor` suffix.

Whatever step 2 or 3 decides is **written back into the dictionary**, so the
expensive comparison runs once per new alias ever; every later use is a plain
lookup.

Both refinements in step 2 are load-bearing. Comparing full directory names
lets the shared 10-character `-professor` suffix dominate the ratio. And
matching prefixes in only one direction misses the common case: with
`py-professor` on disk and `-python` typed, `python` is not a prefix of `py`,
and the pair scores 0.333 — under any usable threshold. Checking both
directions is what makes it work.

### What it gets wrong

| Case | Behaviour | Fix |
|---|---|---|
| `-go` with `google-professor` present | Binds, and says so | `--link` |
| `-ml` with `machine-learning-professor` present | Does **not** bind — 0.222 | `--link ml=machine-learning-professor` |

String similarity cannot do synonyms. Link it once and it is remembered.
Every decision is printed, so a wrong one is visible immediately.

## OUTPUT LAYOUT

```
~/Code/My-Favorite-Professor/
   .mfp/                                  machinery, hidden
      topics.json                         alias dictionary
      audit.log                           what happened, and when
      failed-attempts.csv                 what didn't
   py-professor/
      Primer on Python Decorators.md      ← the only visible file
      .captures/                          hidden archive
         realpython-primer-...-1a83ddee/
            page.html                     rebuilt page, local image links
            assets/                       images, normalised
            manifest.json                 url, title, tier, asset map
            original.html                 raw DOM, to re-extract offline
      async/                              a subtopic: mfp -py.async URL
         Asyncio Event Loops.md           its own notes
         .captures/                       its own archive
```

The note carries Obsidian-style YAML frontmatter (`title`, `url`, `site`,
`author`, `captured`, `topic`, `tier`, `tags`). Images are linked into the
hidden assets directory rather than shown inline, so the note stays readable
as prose; point `MFP_LIBRARY` at a vault folder and each capture becomes a
native Obsidian note.

Capture directories are named from a **stable hash of the URL**, not the date.
That is what makes re-capture an overwrite rather than an accumulation.

## GIVING A CAPTURE TO CLAUDE

Two delivery paths:

**Claude Code** — point it at the hidden capture directory. It reads
`page.html` and opens each file in `assets/`. Best fidelity.

**claude.ai** — compile to PDF and upload the single file:

```
mfp --compile "Primer on Python Decorators.md" --pdf
```

A chat window cannot take a folder of thirty images, but a PDF carries them
inline. `--compile` warns if the PDF exceeds about 30 MB, which is past what
is practical to upload.

Compiling is **offline and deterministic**: capture in, file out, no network.
All scripts and `<noscript>` blocks are stripped, so an archive opened later
cannot re-render itself or contact analytics endpoints.

## FETCH LADDER

Each tier is tried only if the previous one fails.

| Tier | Method | Handles |
|---|---|---|
| T1 | HTTP with full browser headers | Most pages. ~1 second |
| T2 | Headless Chromium | JavaScript-rendered sites |
| T3a | Stealth-patched driver, real Chrome | Bot walls |
| T3b | Wayback Machine snapshot | Pages that are gone or closed |

T2 scrolls the page in steps to trigger lazy-loaded images, reads each image's
resolved `currentSrc` rather than its `src` attribute, strips consent banners
and modals, and collects CSS background images.

**T3 makes an automated browser behave like the browser you would have used
yourself, and failing that looks for a public archive.** It does not solve
CAPTCHAs, use paywall-bypass tooling, or touch credentials. Pages that stay
walled are recorded as failures rather than fought.

## DIAGNOSTICS

### `.mfp/audit.log`

Every action, with the date and 24-hour time and **no seconds** — so if
everything else is lost you can take a timestamp to your browser history and
find the page by hand.

```
2026-08-08 10:56  CREATE  py-professor/  (from alias 'py')
2026-08-08 10:56  SAVED   https://realpython.com/primer-on-python-decorators/
                          -> py-professor/Primer on Python Decorators.md  [T1]
2026-08-08 10:57  LINK    alias 'python' -> existing py-professor/
2026-08-08 10:57  UPDATE  https://realpython.com/...  overwrote prior capture
2026-08-08 11:03  FAIL    https://example.com/404
                          T3b: ... -> wrote .mfp/failed-attempts.csv
```

### `.mfp/failed-attempts.csv`

```
failed_at,topic,domain,url,stage,reason
```

`stage` is how far it got. `reason` lists **every** tier's error in order,
because the last tier is always the archive lookup — recording only that would
make every failure read "no archived snapshot available" and tell you nothing:

```
T1: HTTP 404 Not Found; T2: rendered page still had almost no text; ...
```

Real CSV, quoted properly, so you can loop over it in code or copy a URL out
by hand.

**Only total failures are recorded** — an unreachable page, or one with no
readable content. Losing some images is still a successful capture; the count
goes in `manifest.json` and a warning prints. Otherwise a page that dropped two
tracking pixels would sit in the retry queue forever.

## IMAGE HANDLING

Images are sniffed by magic bytes, never by URL extension. AVIF, WebP, HEIC
and HEIF are converted to PNG. Everything is downscaled to 1500px on its
longest edge and de-duplicated by content hash. Anything under 100px in either
dimension is dropped as an icon or tracking pixel. Capped at 150 images and
80 MB per page — and it reports what it dropped rather than quietly truncating.

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Capture failed at every tier, compile target not found, or `--self-test` had unexpected failures |
| 2 | Malformed arguments, e.g. `--link` without `ALIAS=TOPIC` |

## ENVIRONMENT

`MFP_LIBRARY`
: Library root. Default `~/Code/My-Favorite-Professor`. `--library` overrides
  it for one run.

## FILES

| Path | Purpose |
|---|---|
| `~/Code/My-Favorite-Professor/` | Library root |
| `.mfp/topics.json` | Alias dictionary. Rebuildable — disk is authoritative |
| `.mfp/audit.log` | Append-only action log |
| `.mfp/failed-attempts.csv` | Failed captures |
| `TOPIC/*.md` | The notes you read |
| `TOPIC/.captures/*/` | Hidden archives |

## EXAMPLES

Capture an article under a topic, creating it if needed:

```
mfp -py https://realpython.com/primer-on-python-decorators/
```

Set up a topic ahead of time, then fill it as you read:

```
mfp -n python
mfp -py  https://peps.python.org/pep-0318/
mfp -py3 https://docs.python.org/3/library/functools.html
```

All three aliases resolve to the one `python-professor/` directory.

Capture and open the note immediately:

```
mfp -rust --open https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html
```

Make a PDF to upload to a chat:

```
mfp --compile "Primer on Python Decorators.md" --pdf
```

See how topics are wired, then correct a wrong guess:

```
mfp --topics
mfp --link ml=machine-learning-professor
```

Work through everything that failed:

```
mfp --retry-failed
```

Loop over the failures yourself:

```python
import csv
with open(".mfp/failed-attempts.csv") as fh:
    for row in csv.DictReader(fh):
        print(row["url"], row["reason"])
```

## PYTHON API

```python
from mfp import capture_url, compile_capture

result = capture_url("https://example.com/article", topic="py")
print(result.note_path, result.word_count, result.assets_kept)

pdf = compile_capture(result.capture_dir, as_pdf=True)
```

`capture_url` raises `mfp.FetchError` when every tier fails. The CLI is what
turns that into a CSV row, so a caller wanting the same behaviour should call
`mfp.journal.record_failure` itself.

## LIMITATIONS

* Embedded video, iframes and `<canvas>` are not archived. An iframe becomes a
  placeholder card carrying its URL.
* Web fonts are not captured; compiled files fall back to system fonts.
* Paywalled and hard bot-walled pages are recorded as failures by design.
* Synonym topics (`-ml` / `machine-learning`) need one `--link` each.
* Re-capturing a URL **overwrites** the previous capture. Newer wins.

## NOTES

Two different URLs that produce the same page title get distinct notes — the
second takes its URL hash as a filename suffix. Overwriting is keyed on the
URL, never on the title.

`~/Code/Article-Scraper` is a separate, unrelated project. This tool does not
read from, write to, or depend on it.

## SEE ALSO

`README.txt` in the project root for the same material in prose form.
