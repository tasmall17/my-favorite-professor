# my-favorite-professor

Learn a subject from material *you* chose, with a Claude that gets better at
explaining things to you specifically.

You bring the references — Markdown, text, PDFs, or web pages you saved while
reading them. It maps them into a course you work through in the browser, and
puts a Professor-Claude next to whatever section you're on. Over time it works
out what makes an idea land for *you* — the analogy that clicked, the level of
detail you actually want — and keeps that in a profile you own and can hand to
any other Claude.

The name is a file extension, because the subject is:

```
my-favorite-professor.sh      learn the shell
my-favorite-professor.py      learn Python
my-favorite-professor.sql     learn just enough SQL to finish the thing you're building
```

Nothing here is subject-specific. Upload references, get a course.

---

## Install

Needs Python 3.12+ and an [Anthropic API key](https://console.anthropic.com/).

```sh
# The -app suffix matters on macOS -- see the note at the bottom.
git clone https://github.com/tasmall17/my-favorite-professor.git my-favorite-professor-app
cd my-favorite-professor-app
uv tool install --editable . --with patchright
playwright install chromium          # only needed for capturing web pages
```

Tested on Python 3.12 and 3.14.

Then:

```sh
my-favorite-professor serve
```

It opens in your browser. Paste your API key into Settings on first run.

> `mfp` is installed as a shorter alias for the same command.

---

## The two ways material gets in

**Upload**, from Settings → Upload materials. `.md`, `.txt`, `.pdf`.

**Capture**, from the command line, while you're reading something:

```sh
mfp -py https://realpython.com/primer-on-python-decorators/
```

Topic flags are invented on the spot — `-py` files into `py-professor/`, and
typing `-python` later lands in the same place rather than making a second
directory. The full capture manual is in
[`professor/capture/MANUAL.md`](professor/capture/MANUAL.md).

Either way you get the same thing: a readable Markdown note, plus a hidden
archive holding the page with its images normalised and inlined, so the
material still works offline and Claude can actually see the figures.

---

## The course

Once you've added material, press **Build the course**. Claude reads
everything in the subject and works out an order to learn it in — modules,
lessons, what depends on what, and which of your sources covers each part.

It asks what you're aiming at first, and it matters: "enough to live in a
terminal" and "enough to put it on a CV" produce different courses over
identical material.

Two things it does deliberately:

- **Every lesson cites your sources.** A lesson that can't point at something
  in your library is dropped rather than shown — otherwise it's teaching you
  something you can't go and check.
- **It tells you what's missing.** If an obvious prerequisite isn't in your
  material, it says so under the module instead of inventing a lesson for it.

Progress is tracked per lesson and survives rebuilding, so adding a source and
regenerating doesn't lose what you'd already read.

## The learning profile

This is the point of the whole thing. When an explanation lands, press
**That clicked**. When one is too thin, press **Go deeper**. Those become
observations in `~/.my-favorite-professor/usr-learning-profile/evidence/`, and
every few of them Claude rewrites `profile.md` — a short description of how
*you* understand things.

Professor-Claude reads that profile before every answer. Over time it stops
being a generic explainer and starts being one that knows you find the
mechanism more useful than the syntax, or that you want the example first.

It's yours and it's portable. Open it from the menu and press **Copy**, then
paste it into any Claude:

> Here's how I learn best. Teach me accordingly.

Or `my-favorite-professor profile export` for a zip.

## When your material falls short

Off by default. Turn on **Allow web supplements** in Settings and Professor-
Claude may search the web when your own sources genuinely don't cover
something — and it's told to prefer primary sources and to say when it's doing
it.

Anything it uses is captured into `claude-references-provided/` with its
images and an offline archive, exactly like a page you saved yourself. The
separate directory is the point: you can always tell what you chose from what
Claude went and found.

## Where things live

Three separate places, deliberately. None of them is inside this repo.

```
~/code/My-Favorite-Professor/        your material
  py-professor/
    usr-references-provided/         what you chose
    claude-references-provided/      what Claude fetched to fill a gap
    .captures/                       the archives, images and all
    .mfp-course/syllabus.json        the generated course map

~/.my-favorite-professor/            your learning profile
  usr-learning-profile/
    profile.md                       the artifact you hand to another Claude
    evidence/                        the observations behind it

~/Downloads/my-favorite-professor/   the second copy
  py-professor/<title>-<hash>.html   self-contained, opens anywhere
```

**The profile is global on purpose.** It is about how *you* understand things,
not about any one subject, so it lives outside any particular library and
carries over to every topic you ever study. `mfp profile export` bundles it up.

**The second copy** exists so your material isn't hostage to this program. The
Downloads copies are self-contained HTML — double-click them, read them on a
plane, mail them to someone. Filenames carry the capture's content hash so
re-saving a page overwrites cleanly and two pages that share a title can't
clobber each other.

Set `MFP_LIBRARY` to put your material somewhere else.

---

## Your API key

Stays in `~/.config/my-favorite-professor/config.json`, mode `0600`, and is read
only by the local server process. It is never sent to the browser, never
embedded in a page, and never returned by any endpoint — the front end can only
ask *whether* a key is configured.

That's the reason this is a local server and not a single HTML file: calling
Anthropic from the browser needs the `anthropic-dangerous-direct-browser-access`
header and puts your key within reach of anything that can run script on the
page.

`ANTHROPIC_API_KEY` is used as a fallback if you'd rather not store it at all.

Bring your own key — this talks to Anthropic as you, and nothing routes through
anyone else.

---

## Picking a model

In Settings, per session:

| | |
|---|---|
| **Opus** | Break it right down. Best when the topic is new to you. |
| **Sonnet** | Quick and capable. Good for recap and revision. |
| **Haiku** | Fastest and cheapest. Short definitions and lookups. |

Opus and Sonnet also take an **effort** setting, from `low` to `max` — how hard
Claude works before answering. Haiku doesn't support it, so the control
disappears when you pick Haiku rather than silently doing nothing.

---

## Note for macOS

Don't clone this repo as `~/code/my-favorite-professor` next to a library at
`~/code/My-Favorite-Professor`. The filesystem is case-insensitive, so those are
the *same directory*, and you'll end up with the app's source inside your
material. The app detects and refuses to treat a source checkout as a library,
but the tidy fix is to keep the two apart — clone it as
`my-favorite-professor-app`, as the install command above does, or put it
anywhere outside `~/code`.

This is not hypothetical. It happened while building the app.

---

## Licence

MIT.
