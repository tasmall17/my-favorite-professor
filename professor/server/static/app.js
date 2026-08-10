/* my-favorite-professor -- the front end.
 *
 * No framework and no build step, so that cloning the repo and running one
 * command is the whole setup. The server is the only source of truth; this
 * file renders it and sends things back.
 *
 * Two things worth knowing before reading on:
 *
 *  - The API key is never here. Settings sends one up; the server only ever
 *    sends back whether a key exists.
 *  - Answers arrive as server-sent events over POST, so they stream in as
 *    Claude writes them rather than landing all at once.
 */

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

const state = {
  settings: null,
  topics: [],
  topic: null,        // topic directory name
  note: null,         // note id
  doc: null,          // rendered note
  history: [],        // [{role, content}]
  busy: false,
  view: "course",     // which rail tab is showing
  course: null,       // syllabus + progress for the current topic
  lesson: null,       // {module, lesson} when reading via the course
};

/* ── subject colour ────────────────────────────────────────────────
 * Each subject gets a stable hue so you can tell at a glance which
 * professor you're in. Hashed from the name, picked from a fixed set --
 * an arbitrary hue would eventually land somewhere unreadable.
 */
const HUES = [
  { a: "#5b4fd6", w: "#f0eefc" },  // violet
  { a: "#0f766e", w: "#e7f4f2" },  // teal
  { a: "#b4530a", w: "#fdf1e6" },  // amber
  { a: "#2563a5", w: "#e9f1fa" },  // blue
  { a: "#8b3a62", w: "#fbecf3" },  // plum
  { a: "#3f7a2e", w: "#eef6ea" },  // moss
];

function hueFor(name) {
  let h = 0;
  for (const ch of name || "") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
}

function applyHue(name) {
  const { a, w } = hueFor(name);
  document.documentElement.style.setProperty("--accent", a);
  document.documentElement.style.setProperty("--accent-wash", w);
}

/* ── talking to the server ─────────────────────────────────────── */

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { node.hidden = true; }, 3200);
}

/* ── subjects ──────────────────────────────────────────────────── */

function extLabel(topic) {
  return "." + (topic ? topic.stem : "??");
}

function renderSubjects() {
  const list = $("#ext-list");
  list.replaceChildren();

  if (!state.topics.length) {
    list.append(el("p", "note", "No subjects yet. Add one below."));
  }

  for (const topic of state.topics) {
    const button = el("button");
    button.type = "button";
    button.setAttribute("aria-current", String(topic.name === state.topic));
    const swatch = el("span", "swatch");
    swatch.style.background = hueFor(topic.name).a;
    button.append(swatch, el("span", null, extLabel(topic)));
    button.append(el("span", "count", String(topic.notes.length)));
    button.onclick = () => { selectTopic(topic.name); closePops(); };
    list.append(button);
  }

  const current = state.topics.find((t) => t.name === state.topic);
  $("#ext-label").textContent = extLabel(current);
  $(".empty-mark") && ($(".empty-mark").textContent = extLabel(current));

  const picker = $("#upload-topic");
  picker.replaceChildren();
  for (const topic of state.topics) {
    const option = el("option", null, `${extLabel(topic)}  —  ${topic.name}`);
    option.value = topic.name;
    if (topic.name === state.topic) option.selected = true;
    picker.append(option);
  }
  if (!state.topics.length) {
    const option = el("option", null, "Add a subject first");
    option.value = "";
    picker.append(option);
  }
}

function currentTopic() {
  return state.topics.find((t) => t.name === state.topic);
}

async function selectTopic(name) {
  state.topic = name;
  state.lesson = null;
  applyHue(name);
  renderSubjects();
  await loadCourse();
  renderRail();
  const topic = currentTopic();
  if (topic && topic.notes.length) openNote(topic.notes[0].id);
  else showEmptyReading();
}

/* ── the rail ──────────────────────────────────────────────────── */

function renderRail() {
  $("#tab-course").setAttribute("aria-selected", String(state.view === "course"));
  $("#tab-material").setAttribute("aria-selected", String(state.view === "material"));
  if (state.view === "course") renderCourse();
  else renderMaterial();
}

/* The course view. Modules are numbered because the order *is* the content --
 * a course is a sequence, and the number says where you are in it. */
function renderCourse() {
  const body = $("#rail-body");
  body.replaceChildren();
  const bar = $("#rail-progress");
  const topic = state.topics.find((t) => t.name === state.topic);
  const syllabus = state.course;

  if (!topic || !topic.notes.length) {
    bar.hidden = true;
    body.append(el("p", "rail-empty", "Add some material first — the course is built from it."));
    return;
  }

  if (!syllabus || !syllabus.exists) {
    bar.hidden = true;
    const cta = el("div", "rail-cta");
    cta.append(el("p", null,
      `Claude can read your ${topic.notes.length} ` +
      `${topic.notes.length === 1 ? "source" : "sources"} and work out an order ` +
      `to learn them in.`));
    const button = el("button", "primary", "Build the course");
    button.onclick = openCourseBuild;
    cta.append(button);
    body.append(cta);
    return;
  }

  bar.hidden = false;
  const pct = syllabus.total_lessons
    ? Math.round((syllabus.done_lessons / syllabus.total_lessons) * 100) : 0;
  $("#bar-fill").style.width = pct + "%";

  // The label doubles as the way back to a rebuild. Without it the only route
  // is to change your material and wait for the stale prompt, which is no
  // route at all when you simply want a different goal.
  const label = $("#bar-label");
  label.replaceChildren();
  label.append(el("span", null, `${syllabus.done_lessons}/${syllabus.total_lessons} lessons`));
  const again = el("button", "rebuild-link", "rebuild");
  again.type = "button";
  again.title = "Rebuild the course, or change what you're aiming at";
  again.onclick = openCourseBuild;
  label.append(again);

  if (syllabus.stale) {
    const note = el("div", "stale-note");
    note.append(el("span", null, "Your material changed since this was built."));
    const rebuild = el("button", null, "Rebuild the course");
    rebuild.onclick = openCourseBuild;
    note.append(rebuild);
    body.append(note);
  }

  syllabus.modules.forEach((module, index) => {
    const wrap = el("div", "module");
    const head = el("div", "module-head");
    head.append(el("span", "module-n", String(index + 1).padStart(2, "0")));
    head.append(el("span", "module-title", module.title));
    const done = module.lessons.filter((l) => l.done).length;
    head.append(el("span", "module-done", `${done}/${module.lessons.length}`));
    wrap.append(head);

    for (const lesson of module.lessons) {
      const button = el("button", "lesson");
      button.type = "button";
      button.dataset.done = lesson.done ? "1" : "0";
      button.setAttribute("aria-current", String(state.lesson?.key === lesson.key));
      button.append(el("span", "tick"));
      button.append(el("span", "lesson-title", lesson.title));
      if (lesson.minutes) button.append(el("span", "mins", `${lesson.minutes}m`));
      button.onclick = () => openLesson(module, lesson);
      wrap.append(button);
    }

    if (module.gaps && module.gaps.length) {
      const gaps = el("div", "module-gaps");
      gaps.textContent = "Not covered by your material: " + module.gaps.join("; ");
      wrap.append(gaps);
    }
    body.append(wrap);
  });
}

function renderMaterial() {
  const body = $("#rail-body");
  body.replaceChildren();
  $("#rail-progress").hidden = true;

  const topic = state.topics.find((t) => t.name === state.topic);
  if (!topic || !topic.notes.length) {
    body.append(el("p", "rail-empty", "Nothing here yet. Add a file to get started."));
    return;
  }

  const groups = [
    ["Yours", topic.notes.filter((n) => n.source === "user")],
    ["Claude found", topic.notes.filter((n) => n.source === "claude")],
  ];

  for (const [label, notes] of groups) {
    if (!notes.length) continue;
    const group = el("div", "group");
    const head = el("div", "group-label");
    head.append(el("span", null, label), el("span", "n", String(notes.length)));
    group.append(head);

    for (const note of notes) {
      const button = el("button", "item");
      button.type = "button";
      button.setAttribute("aria-current", String(note.id === state.note));
      button.append(el("span", null, note.title));
      if (note.words) button.append(el("span", "meta", `${note.words.toLocaleString()} words`));
      button.onclick = () => openNote(note.id);
      group.append(button);
    }
    body.append(group);
  }
}

/* ── the reading ───────────────────────────────────────────────── */

function showEmptyReading() {
  state.note = null;
  state.doc = null;
  const body = $("#reading-body");
  body.replaceChildren();
  const empty = el("div", "empty");
  const topic = state.topics.find((t) => t.name === state.topic);
  empty.append(el("p", "empty-mark", extLabel(topic)));
  empty.append(el("h2", null, "Pick what you're studying"));
  const p = el("p", null,
    "Add a Markdown, text or PDF file and it becomes your first reading. " +
    "Or save a page while you're reading it:");
  empty.append(p);
  empty.append(el("code", "empty-cmd", `mfp -${topic ? topic.stem : "py"} https://example.com/article`));
  const button = el("button", "primary", "Add materials");
  button.onclick = () => openSheet("upload");
  empty.append(button);
  body.append(empty);
  resetProfessor();
}

async function openNote(id, { keepLesson = false } = {}) {
  try {
    const doc = await api(`/api/note?id=${encodeURIComponent(id)}`);
    state.note = id;
    state.doc = doc;
    state.history = [];
    // Opening a note directly from the Material tab leaves the course; only a
    // lesson click keeps the lesson framing attached.
    if (!keepLesson) state.lesson = null;

    const body = $("#reading-body");
    body.replaceChildren();

    const wrap = el("article", "doc");
    if (state.lesson) {
      wrap.append(lessonHeader(state.lesson.module, state.lesson.lesson));
    }
    const head = el("div", "doc-head");
    const kicker = el("div", "doc-kicker");
    const tag = el("span", doc.source === "claude" ? "tag claude" : "tag",
                   doc.source === "claude" ? "Claude found this" : "Yours");
    kicker.append(tag);
    if (doc.words) kicker.append(el("span", null, `${doc.words.toLocaleString()} words`));
    if (doc.origin) kicker.append(el("span", null, doc.origin.replace(/^local:/, "")));
    head.append(kicker, el("h1", null, doc.title));
    wrap.append(head);

    const prose = el("div", "prose");
    prose.innerHTML = doc.html;   // rendered and escaped server-side
    wrap.append(prose);
    body.append(wrap);
    $("#reading").scrollTop = 0;

    renderRail();
    resetProfessor();
  } catch (err) {
    toast(err.message);
  }
}

/* ── the course ────────────────────────────────────────────────── */

async function loadCourse() {
  if (!state.topic) { state.course = null; return; }
  try {
    state.course = await api(`/api/course?topic=${encodeURIComponent(state.topic)}`);
  } catch {
    state.course = null;   // no syllabus yet is a normal state, not an error
  }
}

/* A lesson is a framing over sources, not a document of its own. Opening one
 * loads its first source and puts the lesson's own header above it, so you
 * always read the real material rather than a summary of it. */
async function openLesson(module, lesson) {
  state.lesson = { module, lesson };
  await openNote(lesson.note_ids[0], { keepLesson: true });
  renderRail();
}

function lessonHeader(module, lesson) {
  const head = el("div", "lesson-head");
  head.append(el("p", "lesson-eyebrow", `${module.title} · lesson`));
  head.append(el("h2", null, lesson.title));
  if (lesson.summary) head.append(el("p", null, lesson.summary));

  if (lesson.subtopics?.length) {
    const covers = el("div", "lesson-covers");
    for (const item of lesson.subtopics) covers.append(el("span", null, item));
    head.append(covers);
  }

  const actions = el("div", "lesson-actions");
  const done = el("button", "mark-done", lesson.done ? "Read ✓" : "Mark as read");
  done.type = "button";
  done.dataset.done = lesson.done ? "1" : "0";
  done.onclick = () => markLesson(lesson, !lesson.done);
  actions.append(done);

  // More than one source means the lesson spans them; make the others reachable
  // rather than stranding them behind the first.
  if (lesson.note_ids.length > 1) {
    const sources = el("span", "lesson-sources");
    sources.append(document.createTextNode("Also reads: "));
    for (const id of lesson.note_ids.slice(1)) {
      const note = currentTopic()?.notes.find((n) => n.id === id);
      if (!note) continue;
      const link = el("button", null, note.title);
      link.type = "button";
      link.onclick = () => openNote(id, { keepLesson: true });
      sources.append(link);
    }
    actions.append(sources);
  }

  head.append(actions);
  return head;
}

async function markLesson(lesson, done) {
  try {
    state.course = await api("/api/course/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic: state.topic, key: lesson.key, done }),
    });
    lesson.done = done;
    renderRail();
    const button = $(".mark-done");
    if (button) {
      button.dataset.done = done ? "1" : "0";
      button.textContent = done ? "Read ✓" : "Mark as read";
    }
    if (done && state.course.done_lessons === state.course.total_lessons) {
      toast("That's the whole course. Add more material to go further.");
    }
  } catch (err) {
    toast(err.message);
  }
}

const GOAL_SUGGESTIONS = [
  "Get comfortable enough to use this day to day",
  "Build a specific thing I have in mind",
  "Know it well enough to put on a CV",
  "Just enough to unblock the project I'm on",
];

function openCourseBuild() {
  closePops();
  const existing = state.course?.exists;
  $("#cb-title").textContent = existing ? "Rebuild the course" : "Build the course";
  $("#cb-go").textContent = existing ? "Rebuild it" : "Build it";
  $("#cb-status").textContent = "";
  $("#cb-status").removeAttribute("data-ok");
  $("#cb-goal").value =
    state.course?.goal && !state.course.goal.startsWith("Become genuinely useful")
      ? state.course.goal
      : (state.settings?.goals?.[state.topic] || "");

  const picks = $("#cb-picks");
  picks.replaceChildren();
  for (const suggestion of GOAL_SUGGESTIONS) {
    const button = el("button", null, suggestion);
    button.type = "button";
    button.onclick = () => { $("#cb-goal").value = suggestion; $("#cb-goal").focus(); };
    picks.append(button);
  }
  $("#course-build").showModal();
}

async function buildCourse() {
  const status = $("#cb-status");
  const button = $("#cb-go");
  button.disabled = true;
  status.removeAttribute("data-ok");
  status.textContent = "Reading your material and working out an order… "
                     + "this takes a minute.";

  try {
    state.course = await api("/api/course/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic: state.topic, goal: $("#cb-goal").value }),
    });
    state.settings = await api("/api/settings");
    status.dataset.ok = "1";
    status.textContent =
      `Built: ${state.course.modules.length} modules, ${state.course.total_lessons} lessons.`;
    state.view = "course";
    renderRail();
    setTimeout(() => $("#course-build").close(), 1100);
    if (state.course.dropped_lessons) {
      toast(`${state.course.dropped_lessons} lessons dropped — they cited material you don't have.`);
    }
  } catch (err) {
    status.dataset.ok = "0";
    status.textContent = err.message;
  } finally {
    button.disabled = false;
  }
}

/* ── Professor-Claude ──────────────────────────────────────────── */

function resetProfessor() {
  const log = $("#prof-log");
  log.replaceChildren();
  $("#prof-meta").textContent = "";
  state.history = [];

  const sub = $("#prof-sub");
  if (!state.doc) {
    sub.textContent = "Open a reading to ask about it";
    log.append(el("p", "prof-empty", "Nothing open yet."));
    return;
  }

  sub.textContent = state.doc.title;
  const empty = el("div", "prof-empty");
  empty.append(el("p", null, "Ask about anything in this reading. Try:"));
  const list = el("ul");
  for (const line of [
    "Explain this section in plain terms",
    "Give me a concrete example",
    "What do I need to understand before this?",
  ]) list.append(el("li", null, line));
  empty.append(list);
  log.append(empty);
}

function addMessage(kind, who, text) {
  const log = $("#prof-log");
  log.querySelector(".prof-empty")?.remove();

  const msg = el("div", `msg msg-${kind}`);
  msg.append(el("p", "msg-who", who));
  const body = el("div", "msg-body");
  if (kind === "prof") body.innerHTML = "";
  else body.textContent = text;
  msg.append(body);
  log.append(msg);
  log.scrollTop = log.scrollHeight;
  return body;
}

/* A deliberately tiny renderer for streamed answers: paragraphs, fenced code,
 * inline code, bold, and links. The reading pane gets proper server-side
 * Markdown; this only has to keep up with text arriving a token at a time. */
function renderAnswer(text) {
  const escape = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const parts = text.split(/```/);
  return parts.map((part, index) => {
    if (index % 2 === 1) {
      const body = part.replace(/^[\w+-]*\n/, "");
      return `<pre><code>${escape(body)}</code></pre>`;
    }
    return part
      .split(/\n{2,}/)
      .filter((block) => block.trim())
      .map((block) => {
        let html = escape(block.trim())
          .replace(/`([^`]+)`/g, "<code>$1</code>")
          .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
          .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
                   '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
          .replace(/\n/g, "<br>");
        return `<p>${html}</p>`;
      })
      .join("");
  }).join("");
}

async function ask(question, { quiet = false } = {}) {
  if (state.busy || !state.doc) return;
  if (!state.settings?.has_key) {
    toast("Add your API key in Settings first");
    openSheet("settings");
    return;
  }

  state.busy = true;
  $("#prof-send").disabled = true;
  addMessage("you", "You", question);
  const target = addMessage("prof", "Professor-Claude", "");
  target.innerHTML = '<p style="color:var(--slate-dim)">Thinking…</p>';
  $("#prof-meta").textContent = "";

  let answer = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        note_id: state.note,
        question,
        history: state.history,
      }),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
      throw new Error(detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line; a partial frame stays in
      // the buffer until the rest of it arrives.
      const frames = buffer.split("\n\n");
      buffer = frames.pop();

      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const event = JSON.parse(line.slice(6));

        if (event.type === "delta") {
          answer += event.text;
          target.innerHTML = renderAnswer(answer);
          $("#prof-log").scrollTop = $("#prof-log").scrollHeight;
        } else if (event.type === "error") {
          target.parentElement.className = "msg msg-err";
          target.textContent = event.message;
          answer = "";
        } else if (event.type === "done") {
          answer = event.text || answer;
          target.innerHTML = renderAnswer(answer);
          showUsage(event.usage);
          if (event.saved?.length) showSaved(target.parentElement, event.saved);
        }
      }
    }

    if (answer) {
      // A repeat of a question already asked in this session means the first
      // answer didn't land — worth knowing, and cheap to notice.
      const repeated = state.history.some(
        (t) => t.role === "user" && t.content.trim().toLowerCase() === question.trim().toLowerCase());
      if (repeated && !quiet) sendSignal("reasked", question, answer);

      state.history.push({ role: "user", content: question });
      state.history.push({ role: "assistant", content: answer });
      addAnswerActions(target.parentElement, question, answer);
    }
  } catch (err) {
    target.parentElement.className = "msg msg-err";
    target.textContent = err.message;
  } finally {
    state.busy = false;
    $("#prof-send").disabled = false;
    $("#prof-log").scrollTop = $("#prof-log").scrollHeight;
  }
}

/* When Claude looks something up, say so and say where it went. A supplement
 * that lands silently in your library is indistinguishable from one you chose
 * yourself, which is exactly what the split exists to prevent. */
function showSaved(message, saved) {
  const ok = saved.filter((s) => s.ok);
  const box = el("div", "saved");
  box.append(el("p", "saved-head",
    ok.length ? "Not in your material — saved for you:" : "Tried to look this up:"));

  for (const source of saved) {
    const row = el("div", "saved-row");
    row.dataset.ok = source.ok ? "1" : "0";
    const link = el("a", null, source.title || source.url);
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    row.append(link);
    row.append(el("span", "saved-note",
      source.ok ? `${(source.words || 0).toLocaleString()} words` : "couldn't fetch"));
    box.append(row);
  }
  message.append(box);
  if (ok.length) refreshTopics(state.topic).then(renderRail);
}

function showUsage(usage) {
  if (!usage) return;
  const read = usage.cache_read_input_tokens || 0;
  const written = usage.cache_creation_input_tokens || 0;
  const bits = [`${usage.output_tokens || 0} out`];
  if (read) bits.push(`${read.toLocaleString()} cached`);
  else if (written) bits.push(`${written.toLocaleString()} cached for next time`);
  else bits.push(`reading too short to cache (needs ${usage.cache_min_tokens})`);
  $("#prof-meta").textContent = bits.join(" · ");
}

/* The strongest signal for the learning profile is the one you give on
 * purpose. Passive signals fill in around it. */
function addAnswerActions(message, question, answer) {
  const row = el("div", "answer-actions");

  const clicked = el("button", "clicked", "That clicked");
  clicked.type = "button";
  clicked.dataset.on = "0";
  clicked.onclick = async () => {
    if (clicked.dataset.on === "1") return;
    clicked.dataset.on = "1";
    clicked.textContent = "Noted ✓";
    await sendSignal("clicked", question, answer);
  };
  row.append(clicked);

  // Asking for more is itself evidence that the answer was too thin, so the
  // button records the signal as well as continuing the conversation.
  const deeper = el("button", "clicked", "Go deeper");
  deeper.type = "button";
  deeper.onclick = () => {
    deeper.remove();
    sendSignal("expanded", question, answer);
    ask("Tell me more about that — the part you left out.", { quiet: true });
  };
  row.append(deeper);

  message.append(row);
}

async function sendSignal(kind, question, answer) {
  try {
    const result = await api("/api/profile/signal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind, question, answer,
        topic: state.topic,
        lesson: state.lesson?.lesson?.title || state.doc?.title || "",
      }),
    });
    if (result.resynthesised) {
      toast("Professor-Claude updated how it explains things to you.");
    } else if (kind === "clicked") {
      toast(result.next_synthesis_in
        ? `Noted. ${result.next_synthesis_in} more and the profile updates.`
        : "Noted.");
    }
  } catch {
    /* Losing one signal is not worth interrupting a study session over. */
  }
}

/* ── the learning profile ──────────────────────────────────────── */

async function showProfile() {
  try {
    renderProfile(await api("/api/profile"));
    $("#profile").showModal();
  } catch (err) {
    toast(err.message);
  }
}

function renderProfile(data) {
  const stats = $("#pf-stats");
  stats.replaceChildren();
  const pairs = [
    ["observations", data.signals],
    ["landed", data.counts?.clicked || 0],
    ["too thin", data.counts?.expanded || 0],
    ["re-asked", data.counts?.reasked || 0],
  ];
  for (const [label, value] of pairs) {
    const cell = el("div", "pf-stat");
    cell.append(el("span", "pf-n", String(value)), el("span", null, label));
    stats.append(cell);
  }

  const text = $("#pf-text");
  if (data.exists && data.text) {
    text.textContent = data.text;
    text.removeAttribute("data-empty");
  } else {
    text.dataset.empty = "1";
    text.textContent = data.signals
      ? `${data.signals} observations recorded. The profile is written once `
        + `${data.next_synthesis_in} more come in, or press Rebuild now.`
      : "Nothing yet. Ask Professor-Claude something, and when an answer "
        + "lands press “That clicked” — that's what this is built from.";
  }
  $("#pf-copy").disabled = !(data.exists && data.text);
  $("#pf-path").textContent = data.dir;
  $("#pf-status").textContent = "";
}

/* ── settings ──────────────────────────────────────────────────── */

function renderSettings() {
  const settings = state.settings;
  $("#avatar-initials").textContent = settings.initials || "··";
  $("#initials").value = settings.initials || "";
  $("#lib-hint").textContent = settings.library || "";
  $("#mirror-path").textContent = settings.mirror_path;
  $("#mirror-toggle").checked = settings.mirror_to_downloads !== false;
  $("#web-toggle").checked = !!settings.web_supplements;

  const status = $("#key-status");
  if (settings.has_key) {
    status.textContent = settings.key_from_env
      ? "Using ANTHROPIC_API_KEY from your environment."
      : "A key is saved.";
    status.dataset.ok = "1";
  } else {
    status.textContent = "No key yet. Professor-Claude can't answer without one.";
    status.dataset.ok = "0";
  }

  const choices = $("#model-choices");
  choices.replaceChildren();
  for (const model of settings.models) {
    const button = el("button", "choice");
    button.type = "button";
    button.setAttribute("aria-pressed", String(model.id === settings.model));
    const text = el("span");
    text.append(el("span", "name", model.label));
    text.append(el("span", "blurb", model.blurb));
    button.append(text);
    button.onclick = () => saveSettings({ model: model.id });
    choices.append(button);
  }

  // Effort is not a parameter every model takes -- sending it to one that
  // doesn't is an API error, so the control disappears rather than pretending.
  const current = settings.models.find((m) => m.id === settings.model);
  const field = $("#effort-field");
  if (current && current.supports_effort) {
    field.hidden = false;
    const segmented = $("#effort");
    segmented.replaceChildren();
    for (const level of current.efforts) {
      const button = el("button", null, level);
      button.type = "button";
      button.setAttribute("aria-pressed", String(level === settings.effort));
      button.onclick = () => saveSettings({ effort: level });
      segmented.append(button);
    }
  } else {
    field.hidden = true;
    $("#effort-note").textContent = "";
  }
}

/* The key is checked before it's stored. Verification is a free call to the
 * models endpoint, so there's no reason to accept a typo and let it surface
 * as a failed question ten minutes later. */
async function submitKey(input, status) {
  const key = input.value.trim();
  if (!key) { input.focus(); return; }

  status.textContent = "Checking…";
  status.removeAttribute("data-ok");

  try {
    const result = await api("/api/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    status.textContent = result.message;
    status.dataset.ok = result.ok ? "1" : "0";

    if (result.ok) {
      input.value = "";
      state.settings = result.settings;
      renderSettings();
      setTimeout(() => {
        $("#welcome").close();
        toast("Key saved. Ask Professor-Claude anything.");
      }, 900);
    }
  } catch (err) {
    status.textContent = err.message;
    status.dataset.ok = "0";
  }
}

async function saveSettings(patch) {
  try {
    state.settings = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    renderSettings();
    $("#settings-saved").textContent = "Saved.";
    setTimeout(() => { $("#settings-saved").textContent = ""; }, 1800);
  } catch (err) {
    toast(err.message);
  }
}

/* ── uploads ───────────────────────────────────────────────────── */

async function uploadFiles(files) {
  const topic = $("#upload-topic").value;
  if (!topic) { toast("Add a subject first"); return; }

  const log = $("#upload-log");
  for (const file of files) {
    const row = el("li");
    row.dataset.state = "busy";
    row.append(el("span", "name", file.name), el("span", "info", "adding…"));
    log.prepend(row);

    const form = new FormData();
    form.append("file", file);
    form.append("topic", topic);

    try {
      const result = await api("/api/upload", { method: "POST", body: form });
      row.dataset.state = "ok";
      const bits = [`${result.words.toLocaleString()} words`];
      if (result.images) bits.push(`${result.images} images`);
      if (result.updated) bits.push("replaced");
      row.querySelector(".info").textContent = bits.join(" · ");
      await refreshTopics(result.topic);
      if (result.note) openNote(result.note.id);
    } catch (err) {
      row.dataset.state = "err";
      row.querySelector(".info").textContent = err.message;
    }
  }
}

/* ── popovers and sheets ───────────────────────────────────────── */

function closePops() {
  for (const id of ["ext-pop", "avatar-pop"]) $("#" + id).hidden = true;
  $("#ext").setAttribute("aria-expanded", "false");
  $("#avatar").setAttribute("aria-expanded", "false");
}

function togglePop(popId, buttonId) {
  const pop = $("#" + popId);
  const open = pop.hidden;
  closePops();
  pop.hidden = !open;
  $("#" + buttonId).setAttribute("aria-expanded", String(open));
}

function openSheet(name) {
  closePops();
  if (name === "settings") renderSettings();
  $("#" + name).showModal();
}

/* ── boot ──────────────────────────────────────────────────────── */

async function refreshTopics(preferred) {
  const data = await api("/api/topics");
  state.topics = data.topics;
  const wanted = preferred || state.topic;
  const found = state.topics.find((t) => t.name === wanted);
  // Falling back to the alphabetically-first subject lands you on an empty one
  // whenever you have several. Prefer a subject that actually has material.
  state.topic = found
    ? found.name
    : (state.topics.find((t) => t.notes.length) ?? state.topics[0])?.name ?? null;
  applyHue(state.topic || "");
  renderSubjects();
  await loadCourse();
  renderRail();
}

function wire() {
  $("#ext").onclick = () => togglePop("ext-pop", "ext");
  $("#avatar").onclick = () => togglePop("avatar-pop", "avatar");

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".pop") && !event.target.closest("#ext")
        && !event.target.closest("#avatar")) closePops();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closePops();
  });

  for (const button of document.querySelectorAll("[data-open]")) {
    button.onclick = () => openSheet(button.dataset.open);
  }

  // Course / Material tabs
  $("#tab-course").onclick = () => { state.view = "course"; renderRail(); };
  $("#tab-material").onclick = () => { state.view = "material"; renderRail(); };
  $("#cb-go").onclick = buildCourse;

  $("#new-topic-form").onsubmit = async (event) => {
    event.preventDefault();
    const input = $("#new-topic");
    const name = input.value.trim();
    if (!name) return;
    try {
      const result = await api("/api/topics", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      input.value = "";
      await refreshTopics(result.topic.name);
      selectTopic(result.topic.name);
      closePops();
      if (result.action === "bound") {
        toast(`"${name}" goes into ${result.matched}`);
      }
    } catch (err) {
      toast(err.message);
    }
  };

  // Settings
  $("#api-key-save").onclick = () => submitKey($("#api-key"), $("#key-status"));
  $("#api-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); $("#api-key-save").click(); }
  });
  $("#key-clear").onclick = () => saveSettings({ api_key: null });

  // First run
  $("#welcome-save").onclick = () => submitKey($("#welcome-key"), $("#welcome-status"));
  $("#welcome-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); $("#welcome-save").click(); }
  });
  $("#welcome-skip").onclick = () => $("#welcome").close();
  $("#initials").onchange = (event) => saveSettings({ initials: event.target.value });
  $("#mirror-toggle").onchange = (event) =>
    saveSettings({ mirror_to_downloads: event.target.checked });
  $("#web-toggle").onchange = (event) =>
    saveSettings({ web_supplements: event.target.checked });
  $("#open-profile").onclick = async (event) => {
    event.preventDefault();
    closePops();
    await showProfile();
  };
  $("#pf-rebuild").onclick = async () => {
    const status = $("#pf-status");
    status.textContent = "Reading your sessions…";
    status.removeAttribute("data-ok");
    try {
      const result = await api("/api/profile/synthesise", { method: "POST" });
      renderProfile(result);
      status.dataset.ok = "1";
      status.textContent = "Rebuilt.";
    } catch (err) {
      status.dataset.ok = "0";
      status.textContent = err.message;
    }
  };
  $("#pf-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText($("#pf-text").textContent);
      toast("Copied. Paste it into any Claude.");
    } catch {
      toast("Couldn't copy — select the text and copy it manually.");
    }
  };

  // Uploads
  const drop = $("#drop");
  const input = $("#file-input");
  drop.onclick = () => input.click();
  input.onchange = () => { uploadFiles([...input.files]); input.value = ""; };
  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (e) => { e.preventDefault(); drop.dataset.over = "1"; });
  }
  for (const type of ["dragleave", "drop"]) {
    drop.addEventListener(type, (e) => { e.preventDefault(); drop.dataset.over = "0"; });
  }
  drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

  // Professor
  const shell = $(".shell");
  $("#prof-close").onclick = () => shell.classList.add("prof-hidden");
  $("#prof-open").onclick = () => {
    shell.classList.remove("prof-hidden");
    $("#prof-input").focus();
  };

  const field = $("#prof-input");
  field.addEventListener("input", () => {
    field.style.height = "auto";
    field.style.height = Math.min(field.scrollHeight, 128) + "px";
  });
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#prof-form").requestSubmit();
    }
  });
  $("#prof-form").onsubmit = (event) => {
    event.preventDefault();
    const question = field.value.trim();
    if (!question) return;
    field.value = "";
    field.style.height = "auto";
    ask(question);
  };
}

async function boot() {
  wire();
  try {
    state.settings = await api("/api/settings");
    $("#cfg-path").textContent = "~/.config/my-favorite-professor/config.json";
    renderSettings();
    await refreshTopics();
    const topic = state.topics.find((t) => t.name === state.topic);
    if (topic && topic.notes.length) openNote(topic.notes[0].id);
    else showEmptyReading();

    // First run: nothing works without a key, so explain where to get one
    // rather than dropping them into a settings form with an empty box.
    if (!state.settings.has_key) {
      $("#welcome .welcome-ext").textContent = extLabel(
        state.topics.find((t) => t.name === state.topic));
      $("#welcome").showModal();
    }
  } catch (err) {
    toast("Couldn't reach the server: " + err.message);
  }
}

boot();
