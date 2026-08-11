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

```sh
git clone https://github.com/tasmall17/my-favorite-professor.git
cd my-favorite-professor
./install.sh
```

That's it. The installer builds a self-contained environment, puts `mfp` and
`my-favorite-professor` on your PATH, and opens the app in your browser. The
first screen walks you through getting an API key.

<details>
<summary>What it actually does, and what to do if something goes wrong</summary>

<br>

It finds a Python 3.12+, creates `.venv` inside the checkout, installs the
dependencies (including a headless Chromium used for saving web pages and
making PDFs), and writes two small launcher scripts into `~/.local/bin`.
Nothing is installed system-wide and nothing is downloaded from anywhere but
PyPI.

| Flag | |
|---|---|
| `--no-launch` | Set up, but don't open the app afterwards |
| `--no-shims` | Don't touch `~/.local/bin`; run `.venv/bin/mfp` directly |
| `--force` | Replace an existing `mfp` command without asking |

**"command not found: my-favorite-professor"** — `~/.local/bin` isn't on your
PATH. The installer tells you the line to add; add it and reopen your terminal.

**It says the checkout is inside a material library** — only possible if you
cloned into a directory that already held material from an older version, on
macOS, where `my-favorite-professor` and `My-Favorite-Professor` are the *same
directory*. Delete the checkout — your material is untouched — and clone it
somewhere else, such as your home directory.

**Python 3.12+ not found** — `brew install python@3.12` on macOS,
`sudo apt install python3.12 python3.12-venv` on Debian/Ubuntu.

**No `install.sh`?** The manual equivalent:

```sh
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/playwright install chromium
.venv/bin/my-favorite-professor serve
```

Tested on Python 3.12 and 3.14.

</details>

---

## Using it

```sh
my-favorite-professor serve     # open the app
```

**Add material** from the menu (top right) → *Add materials*: `.md`, `.txt` or
`.pdf`.

**Or save a page while you're reading it**, from the terminal:

```sh
mfp -py https://realpython.com/primer-on-python-decorators/
mfp -sh https://zsh.sourceforge.io/Guide/
```

Topic flags are invented on the spot — `-py` files into `py-professor/`, and
typing `-python` later lands in the same place rather than making a second
directory. `mfp --topics` shows what you have. The full capture reference is in
[`professor/capture/MANUAL.md`](professor/capture/MANUAL.md).

Either route gives you the same thing: a readable Markdown note, plus a hidden
archive holding the page with its images normalised and inlined, so your
material still works offline and Claude can actually see the figures.

Then press **Build the course**, and start reading.

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
~/my-favorite-professor-library/     your material
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

**Already have a library?** Earlier versions kept it in
`~/code/My-Favorite-Professor`, `~/Documents/My-Favorite-Professor` or
`~/My-Favorite-Professor`. Those are still found automatically, so there is
nothing to move — a directory is only adopted if it actually contains material,
never just because it has the right name.

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

The library is called `my-favorite-professor-library`, not
`My-Favorite-Professor`, for one reason: the macOS filesystem is
case-insensitive, so a repo cloned as `my-favorite-professor` *is* a directory
called `My-Favorite-Professor`, and the old default put your material inside the
app's own source. Clone this wherever you like — the two names can no longer
land on the same directory.

This is not hypothetical. It happened while building the app.

---

## Licence

MIT.
