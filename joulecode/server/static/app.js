/* joule:code platform – web interface without a build step and without external libraries.
 *
 * hash routes:   #/            projects
 *               #/p/<key>     project (history, jobs, CI setup)
 *               #/j/<id>      job (phases, gate, measurement, findings, log)
 *               #/queue       queue
 *               #/settings    admin token for write actions
 *
 * All content from the API (file names, commit messages …) goes into the page via
 * textContent, never via innerHTML.
 */
"use strict";

// -- Helpers ------------------------------------------------------------------------
const $view = document.getElementById("view");
const $tooltip = document.getElementById("tooltip");
const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.style.cssText = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}
function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* ignore */ } },
};
let adminToken = store.get("joulecode.admin");

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.admin && adminToken) headers.Authorization = `Bearer ${adminToken}`;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, { method: opts.method || "GET", headers, body: opts.body });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}

const nf = (d) => new Intl.NumberFormat("en-GB", { maximumFractionDigits: d, minimumFractionDigits: d });
function pct(x, d = 1) {
  if (x == null) return "–";
  return (x > 0 ? "+" : x < 0 ? "−" : "±") + nf(d).format(Math.abs(x * 100)) + "%";
}
function num(x, d = 1) { return x == null ? "–" : nf(d).format(x); }
function ago(iso) {
  if (!iso) return "–";
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 45) return "just now";
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
  if (sec < 86400 * 14) return `${Math.round(sec / 86400)} d ago`;
  return new Date(iso).toLocaleDateString("en-GB");
}
function dt(iso) { return iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "–"; }
function duration(a, b) {
  if (!a) return "–";
  const sec = Math.max(0, ((b ? new Date(b) : new Date()) - new Date(a)) / 1000);
  return sec < 90 ? `${Math.round(sec)} s` : `${Math.round(sec / 60)} min`;
}
const short = (sha) => (sha ? sha.slice(0, 10) : "–");

const STATUS = {
  queued: ["neutral", "queued"], running: ["info", "running"], done: ["good", "done"],
  failed: ["bad", "failed"], cancelled: ["neutral", "cancelled"],
};
const GATE = { passed: ["good", "✓", "passed"], failed: ["bad", "✗", "failed"], skipped: ["neutral", "–", "no conditions"] };
const VERDICT = { "better": ["good", "✓"], "worse": ["bad", "✗"], "no significant difference": ["neutral", "≈"] };
const MODE = { review: "Review", gate: "Gate", optimize: "Optimisation" };
const EVIDENCE = {
  static: ["outline", "detected statically", "An analysis tool found the pattern. Whether it costs energy has not been measured."],
  llm: ["warn", "LLM suggestion", "Suggested by the language model, neither tested nor measured."],
  measured: ["good", "measured", "This kind of change demonstrably saved energy in earlier measurements."],
};

function statusBadge(status) {
  const [cls, label] = STATUS[status] || ["neutral", status];
  return h("span", { class: `badge ${cls}` }, status === "running" ? h("span", { class: "spin", "aria-hidden": "true" }) : null, label);
}
function gateBadge(gate) {
  if (!gate) return h("span", { class: "muted" }, "–");
  const [cls, icon, label] = GATE[gate] || ["neutral", "?", gate];
  return h("span", { class: `badge ${cls}` }, icon, " ", label);
}
function verdictBadge(v) {
  if (!v) return h("span", { class: "muted" }, "–");
  const [cls, icon] = VERDICT[v] || ["neutral", "?"];
  return h("span", { class: `badge ${cls}` }, icon, " ", v);
}
function evidenceBadge(e, detail) {
  const [cls, label, title] = EVIDENCE[e] || ["outline", e, ""];
  return h("span", { class: `badge ${cls}`, title: detail ? `${title} (${detail})` : title }, label);
}
function emptyState(title, text) { return h("div", { class: "empty" }, h("b", {}, title), text); }

function showTip(evt, ...content) {
  $tooltip.replaceChildren(...content.map((c) => (c instanceof Node ? c : h("div", {}, c))));
  $tooltip.hidden = false;
  const pad = 14, r = $tooltip.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - pad;
  $tooltip.style.left = `${Math.max(8, x)}px`;
  $tooltip.style.top = `${Math.max(8, y)}px`;
}
function hideTip() { $tooltip.hidden = true; }

// Elements that survive redrawing while polling (forms, freshly generated tokens).
const keep = new Map();
function persistent(name) {
  if (!keep.has(name)) keep.set(name, h("div"));
  return keep.get(name);
}

// -- Router + Polling -------------------------------------------------------------
let timer = null;
let routeToken = 0;
function poll(fn, ms) {
  clearTimeout(timer);
  const mine = routeToken;
  timer = setTimeout(async () => {
    if (mine !== routeToken) return;
    try { await fn(); } catch { /* next attempt */ }
  }, ms);
}

async function route() {
  routeToken++;
  keep.clear();
  clearTimeout(timer);
  hideTip();
  const hash = location.hash.replace(/^#/, "") || "/";
  const parts = hash.split("/").filter(Boolean).map(decodeURIComponent);
  document.querySelectorAll("nav a").forEach((a) => a.classList.remove("active"));
  const nav = { "": "projects", p: "projects", j: "projects", queue: "queue", settings: "settings" }[parts[0] || ""];
  document.querySelector(`nav a[data-nav=${nav}]`)?.classList.add("active");
  try {
    if (parts[0] === "p" && parts[1]) await viewProject(parts[1]);
    else if (parts[0] === "j" && parts[1]) await viewJob(parts[1]);
    else if (parts[0] === "queue") await viewQueue();
    else if (parts[0] === "settings") viewSettings();
    else await viewProjects();
  } catch (e) {
    $view.replaceChildren(h("div", { class: "banner bad" }, `Error: ${e.message}`));
  }
}
addEventListener("hashchange", route);

function modesLabel(modes) {
  const real = Object.keys(modes).filter((m) => modes[m] !== "dummy").map((m) => MODE[m] || m);
  const dummy = Object.keys(modes).filter((m) => modes[m] === "dummy").map((m) => MODE[m] || m);
  if (!dummy.length) return "all modes connected";
  if (!real.length) return "processing: placeholder (dummy)";
  return `${real.join(", ")}: real · ${dummy.join(", ")}: placeholder`;
}

async function refreshHealth() {
  const el = document.getElementById("health");
  try {
    const hlt = await api("/api/health");
    const n = hlt.queue.queued + hlt.queue.running;
    const qc = document.getElementById("queue-count");
    qc.hidden = n === 0;
    qc.textContent = n;
    el.className = "health ok";
    el.replaceChildren(h("span", { class: "dot" }), h("span", { class: "label" },
      `${hlt.version} · ${modesLabel(hlt.modes || {})}`));
  } catch {
    el.className = "health down";
    el.replaceChildren(h("span", { class: "dot" }), h("span", { class: "label" }, "server unreachable"));
  }
  setTimeout(refreshHealth, 5000);
}

// -- Projects -------------------------------------------------------------------------
async function viewProjects() {
  const mine = routeToken;
  const projects = await api("/api/projects");
  if (mine !== routeToken) return;

  const createBox = persistent("create-project");
  const head = h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Projects"),
      h("p", { class: "lede" }, "Every project has its own token that CI uses to submit jobs. Review gives quick findings, gate measures energy regressions against the target branch.")),
    h("button", { class: "btn primary", onclick: () => toggleCreate(createBox) }, "+ New project"));

  let body;
  if (!projects.length) {
    body = h("div", { class: "card" }, emptyState("No projects yet", "Create a project – joule:code then shows how to connect CI to it."));
  } else {
    body = h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Project"), h("th", {}, "Last job"), h("th", {}, "Status"),
        h("th", {}, "Last gate"), h("th", { class: "num" }, "Energy"), h("th", { class: "num" }, "Jobs"))),
      h("tbody", {}, projects.map((p) => {
        const lj = p.last_job, lg = p.last_gate;
        return h("tr", { class: "clickable", onclick: () => (location.hash = `#/p/${encodeURIComponent(p.key)}`) },
          h("td", {}, h("a", { href: `#/p/${encodeURIComponent(p.key)}` }, h("b", {}, p.name)), h("div", { class: "muted small mono" }, p.key)),
          h("td", {}, lj ? [MODE[lj.mode], h("div", { class: "muted small" }, `${lj.branch || "–"} · ${ago(lj.created_at)}`)] : h("span", { class: "muted" }, "–")),
          h("td", {}, lj ? statusBadge(lj.status) : null),
          h("td", {}, lg ? gateBadge(lg.gate_status) : h("span", { class: "muted" }, "–")),
          h("td", { class: "num" }, lg && lg.summary ? pct(lg.summary.change) : "–"),
          h("td", { class: "num" }, p.job_count));
      })))));
  }
  $view.replaceChildren(head, createBox, body);
  poll(viewProjects, 5000);
}

function toggleCreate(box) {
  if (box.firstChild) { box.replaceChildren(); return; }
  if (!adminToken) {
    box.replaceChildren(h("div", { class: "banner info" }, "Creating a project requires the admin token. ",
      h("a", { href: "#/settings" }, "Enter token"), "."));
    return;
  }
  const key = h("input", { type: "text", placeholder: "e.g. wordstats", required: true, pattern: "[a-z0-9][a-z0-9._\\-]{0,63}", autocomplete: "off" });
  const name = h("input", { type: "text", placeholder: "Display name (optional)" });
  const msg = h("div");
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/projects", { method: "POST", admin: true, json: { key: key.value.trim(), name: name.value } });
      box.replaceChildren(h("div", { class: "card" }, h("h2", {}, `Project “${r.project.name}” created`),
        tokenBox(r.token), setupSnippets(r.project.key, r.token),
        h("p", {}, h("a", { href: `#/p/${encodeURIComponent(r.project.key)}` }, "Go to project →"))));
    } catch (err) {
      msg.replaceChildren(h("div", { class: "banner bad" }, err.message));
    }
  } },
  h("h2", {}, "New project"),
  h("div", { class: "form-row" },
    h("label", {}, "Key (lower-case letters, digits, . _ -)", key),
    h("label", {}, "Name", name),
    h("button", { class: "btn primary", type: "submit" }, "Create")), msg);
  box.replaceChildren(form);
  key.focus();
}

function tokenBox(token) {
  const btn = h("button", { class: "btn", type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(token); btn.textContent = "Copied"; } catch { btn.textContent = "Please copy manually"; }
  } }, "Copy");
  return h("div", {}, h("div", { class: "token-box" }, h("b", {}, "Project token:"), h("code", {}, token), btn),
    h("p", { class: "small muted" }, "The token is shown only now. Store it in CI as the secret JOULECODE_TOKEN."));
}

function setupSnippets(key, token) {
  const server = location.origin;
  const tok = token ? token : "<project-token>";
  const snippets = {
    "Local": `pip install joulecode   # or pip install -e . in the joule:code repository
export JOULECODE_SERVER=${server}
export JOULECODE_PROJECT=${key}
export JOULECODE_TOKEN=${tok}

joulecode submit                                   # review of the whole repository
joulecode submit --base origin/main                # changed functions only
joulecode submit --mode gate --base origin/main    # measure energy regression`,
    "GitHub Actions": `# .github/workflows/joulecode.yml
# Measuring happens on the joule:code server; the runner only has to reach it
# (a local server is usually only reachable from a self-hosted runner in the same network).
name: joule:code
on: [pull_request]
jobs:
  review:
    runs-on: ubuntu-latest
    permissions: { contents: read, security-events: write }
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      # joule:code is not on PyPI: pip install "joulecode @ git+https://github.com/fhac-ida/joulecode.git"
      - run: pip install joulecode
      # target branch comes from GITHUB_BASE_REF → changed functions only
      - run: joulecode submit --fail-on major --sarif joulecode.sarif
        env:
          JOULECODE_SERVER: ${server}
          JOULECODE_PROJECT: ${key}
          JOULECODE_TOKEN: \${{ secrets.JOULECODE_TOKEN }}
      - uses: github/codeql-action/upload-sarif@v3
        if: always() && hashFiles('joulecode.sarif') != ''
        with: { sarif_file: joulecode.sarif, category: joulecode }
  energy-gate:
    if: contains(github.event.pull_request.labels.*.name, 'energy-check')
    runs-on: ubuntu-latest   # or [self-hosted]
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - run: pip install joulecode
      - run: joulecode submit --mode gate --max-regression 5
        env:
          JOULECODE_SERVER: ${server}
          JOULECODE_PROJECT: ${key}
          JOULECODE_TOKEN: \${{ secrets.JOULECODE_TOKEN }}`,
    "GitLab CI": `# .gitlab-ci.yml
joulecode-review:
  image: python:3.12
  variables: { GIT_DEPTH: 0, JOULECODE_SERVER: "${server}", JOULECODE_PROJECT: "${key}" }
  script:
    - pip install joulecode
    - joulecode submit --fail-on major --output joulecode.json --sarif joulecode.sarif
  artifacts: { paths: [joulecode.json, joulecode.sarif], when: always }
  rules: [{ if: $CI_PIPELINE_SOURCE == "merge_request_event" }]

joulecode-gate:               # measuring happens on the server; the runner only waits
  image: python:3.12
  variables: { GIT_DEPTH: 0, JOULECODE_SERVER: "${server}", JOULECODE_PROJECT: "${key}" }
  script:
    - pip install joulecode
    - joulecode submit --mode gate --max-regression 5
  rules: [{ if: $CI_PIPELINE_SOURCE == "merge_request_event", when: manual }]`,
  };
  const pre = h("pre", { class: "snippet" });
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const select = (name) => {
    pre.textContent = snippets[name];
    tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", b.dataset.name === name));
  };
  for (const name of Object.keys(snippets)) {
    tabs.append(h("button", { type: "button", role: "tab", "data-name": name, onclick: () => select(name) }, name));
  }
  select(store.get("joulecode.snippet") in snippets ? store.get("joulecode.snippet") : "Local");
  tabs.addEventListener("click", (e) => e.target.dataset.name && store.set("joulecode.snippet", e.target.dataset.name));
  return h("div", {}, h("h3", {}, "Setup"), tabs, pre,
    h("p", { class: "small muted" }, "Exit code of joulecode submit: 0 = gate passed/no conditions, 1 = failed, 2 = error. Never store secrets in CI in plain text."));
}

// -- Project --------------------------------------------------------------------------
async function viewProject(key, filters) {
  const mine = routeToken;
  filters = filters || { branch: store.get(`joulecode.branch.${key}`) || "", mode: "" };
  const q = new URLSearchParams({ limit: "100" });
  if (filters.branch) q.set("branch", filters.branch);
  if (filters.mode) q.set("mode", filters.mode);
  const [project, jobs] = await Promise.all([
    api(`/api/projects/${encodeURIComponent(key)}`),
    api(`/api/projects/${encodeURIComponent(key)}/jobs?${q}`),
  ]);
  if (mine !== routeToken) return;

  const lastGate = project.trend[project.trend.length - 1];
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  const kpis = h("div", { class: "kpis" },
    kpi("Jobs (filtered)", jobs.length, active ? `${active} active` : "none active"),
    kpi("Last gate", lastGate ? gateBadge(lastGate.gate) : "–", lastGate ? `${lastGate.branch || "–"} · ${ago(lastGate.created_at)}` : "no measurement yet"),
    kpi("Energy (last gate)", lastGate ? pct(lastGate.change) : "–", lastGate && lastGate.ci95 ? `CI ${pct(lastGate.ci95[0])} … ${pct(lastGate.ci95[1])}` : ""),
    kpi("Branches", project.branches.length, project.branches.slice(0, 3).join(", ")));

  const branchSel = h("select", { "aria-label": "Branch", onchange: (e) => { store.set(`joulecode.branch.${key}`, e.target.value); viewProject(key, { ...filters, branch: e.target.value }); } },
    h("option", { value: "" }, "all branches"), project.branches.map((b) => h("option", { value: b, selected: b === filters.branch }, b)));
  const modeSel = h("select", { "aria-label": "Mode", onchange: (e) => viewProject(key, { ...filters, mode: e.target.value }) },
    h("option", { value: "" }, "all modes"), Object.entries(MODE).map(([m, l]) => h("option", { value: m, selected: m === filters.mode }, l)));

  const jobsCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Jobs")),
    h("div", { class: "filters" }, branchSel, modeSel),
    jobs.length ? jobsTable(jobs) : emptyState("No jobs", "Submit the first job with joulecode submit (see setup)."));

  const trendCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Energy over time"),
      h("span", { class: "small muted" }, "Change relative to the target branch per gate run, with 95% CI")),
    project.trend.length ? trendChart(project.trend) : emptyState("No gate measurement yet", "Gate runs (joulecode submit --mode gate) appear here as a history."));

  const tokenArea = persistent(`token-${key}`);
  const setupCard = h("details", { class: "card" }, h("summary", { style: "cursor:pointer;font-weight:600" }, "Setup & token"),
    setupSnippets(project.key, null),
    h("div", { style: "margin-top:12px" }, h("button", { class: "btn danger", onclick: async () => {
      if (!adminToken) { tokenArea.replaceChildren(h("div", { class: "banner info" }, "This requires the admin token. ", h("a", { href: "#/settings" }, "Enter token"), ".")); return; }
      try { const r = await api(`/api/projects/${encodeURIComponent(key)}/token`, { method: "POST", admin: true }); tokenArea.replaceChildren(tokenBox(r.token), h("p", { class: "small" }, "The old token is no longer valid.")); }
      catch (e) { tokenArea.replaceChildren(h("div", { class: "banner bad" }, e.message)); }
    } }, "Generate new token")), tokenArea);
  const wasOpen = $view.querySelector("details.card")?.open;
  if (wasOpen) setupCard.open = true;

  $view.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("div", { class: "crumbs" }, h("a", { href: "#/" }, "Projects"), " / "),
      h("h1", {}, project.name), h("div", { class: "meta" }, h("span", {}, "Key ", h("b", { class: "mono" }, project.key)), h("span", {}, `created ${dt(project.created_at)}`)))),
    kpis, trendCard, jobsCard, setupCard);
  poll(() => viewProject(key, filters), active ? 2000 : 6000);
}

function kpi(label, value, sub) {
  return h("div", { class: "kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), h("div", { class: "sub" }, sub || " "));
}

function jobsTable(jobs, withProject = false) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, withProject ? h("th", {}, "Project") : null, h("th", {}, "Mode"), h("th", {}, "Branch / Commit"),
      h("th", {}, "Status"), h("th", {}, "Gate"), h("th", { class: "num" }, "Findings"), h("th", { class: "num" }, "Energy"), h("th", {}, "Submitted"))),
    h("tbody", {}, jobs.map((j) => h("tr", { class: "clickable", onclick: () => (location.hash = `#/j/${j.id}`) },
      withProject ? h("td", {}, j.project_name) : null,
      h("td", {}, h("a", { href: `#/j/${j.id}` }, MODE[j.mode] || j.mode)),
      h("td", {}, h("div", { class: "trunc" }, j.branch || h("span", { class: "muted" }, "–")),
        h("div", { class: "muted small mono trunc", title: j.message || "" }, short(j.commit_sha), j.message ? ` · ${j.message}` : "")),
      h("td", {}, statusBadge(j.status), j.status === "running" && j.phase ? h("div", { class: "muted small" }, j.phase) : null,
        j.status === "queued" ? h("div", { class: "muted small" }, "in the queue") : null),
      h("td", {}, j.status === "done" ? gateBadge(j.gate_status) : h("span", { class: "muted" }, "–")),
      h("td", { class: "num" }, j.summary ? j.summary.findings : "–"),
      h("td", { class: "num" }, j.summary && j.summary.change != null ? pct(j.summary.change) : "–"),
      h("td", { title: dt(j.created_at) }, ago(j.created_at)))))));
}

// Draw charts at their actual width so that labels do not scale with them.
function chartWidth() { return Math.max(300, Math.min(1080, $view.clientWidth - 90)); }

function trendChart(points) {
  const W = chartWidth(), H = 220, m = { l: 52, r: 16, t: 12, b: 28 };
  const lo = Math.min(0, ...points.map((p) => (p.ci95 ? p.ci95[0] : p.change)));
  const hi = Math.max(0, ...points.map((p) => (p.ci95 ? p.ci95[1] : p.change)));
  const span = Math.max(hi - lo, 0.02), pad = span * 0.12;
  const y0 = lo - pad, y1 = hi + pad;
  const x = (i) => m.l + (points.length === 1 ? (W - m.l - m.r) / 2 : (i * (W - m.l - m.r)) / (points.length - 1));
  const y = (v) => m.t + ((y1 - v) / (y1 - y0)) * (H - m.t - m.b);
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Energy change per gate run" });
  const step = niceStep((y1 - y0) / 4);
  for (let v = Math.ceil(y0 / step) * step; v <= y1 + 1e-9; v += step) {
    svg.append(s("line", { class: Math.abs(v) < 1e-9 ? "zero" : "grid", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }),
      s("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, pct(v, step < 0.01 ? 1 : 0)));
  }
  if (points.length > 1) {
    svg.append(s("polyline", { class: "line", points: points.map((p, i) => `${x(i)},${y(p.change)}`).join(" ") }));
  }
  const colors = { better: "var(--good)", worse: "var(--bad)" };
  points.forEach((p, i) => {
    const c = colors[p.verdict] || "var(--neutral)";
    if (p.ci95) svg.append(s("line", { class: "whisker", x1: x(i), x2: x(i), y1: y(p.ci95[0]), y2: y(p.ci95[1]), stroke: c, opacity: 0.55 }));
    svg.append(s("circle", { class: "pt", cx: x(i), cy: y(p.change), r: 5, fill: c }));
    const tip = (e) => showTip(e, h("div", {}, h("b", {}, pct(p.change)), p.ci95 ? ` (CI ${pct(p.ci95[0])} … ${pct(p.ci95[1])})` : ""),
      h("div", {}, `${VERDICT[p.verdict]?.[1] || ""} ${p.verdict || ""}`), h("div", { class: "muted" }, `${p.branch || "–"} · ${short(p.commit_sha)}`),
      h("div", { class: "muted" }, dt(p.created_at)));
    svg.append(s("rect", { class: "hit", x: x(i) - 12, y: m.t, width: 24, height: H - m.t - m.b, onmousemove: tip, onmouseleave: hideTip,
      onclick: () => (location.hash = `#/j/${p.id}`) }));
  });
  const first = points[0], last = points[points.length - 1];
  svg.append(s("text", { x: m.l, y: H - 6 }, ago(first.created_at)));
  if (points.length > 1) svg.append(s("text", { x: W - m.r, y: H - 6, "text-anchor": "end" }, ago(last.created_at)));
  return h("div", {}, svg, h("div", { class: "legend-inline" },
    h("span", { style: "--c: var(--good)" }, "✓ less energy (significant)"),
    h("span", { style: "--c: var(--bad)" }, "✗ more energy (significant)"),
    h("span", { style: "--c: var(--neutral)" }, "≈ no significant difference")));
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}

// -- Job ------------------------------------------------------------------------------
async function viewJob(id) {
  const mine = routeToken;
  const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
  if (mine !== routeToken) return;
  const r = job.result;
  const active = job.status === "queued" || job.status === "running";

  const actions = h("div", {});
  if (active) {
    actions.append(h("button", { class: "btn danger", onclick: async (e) => {
      if (!adminToken) { location.hash = "#/settings"; return; }
      e.target.disabled = true;
      try { await api(`/api/jobs/${id}/cancel`, { method: "POST", admin: true }); viewJob(id); }
      catch (err) { actions.append(h("div", { class: "banner bad" }, err.message)); }
    } }, "Cancel"));
  }

  const parts = [
    h("div", { class: "page-head" }, h("div", {},
      h("div", { class: "crumbs" }, h("a", { href: "#/" }, "Projects"), " / ", h("a", { href: `#/p/${encodeURIComponent(job.project_key)}` }, job.project_name), " / "),
      h("h1", {}, `${MODE[job.mode] || job.mode} `, h("span", { class: "muted mono", style: "font-size:16px;font-weight:500" }, job.id)),
      h("div", { class: "meta" },
        statusBadge(job.status),
        job.status === "done" ? gateBadge(job.gate_status) : null,
        h("span", {}, "Branch ", h("b", {}, job.branch || "–")),
        h("span", {}, "Commit ", h("b", { class: "mono" }, short(job.commit_sha))),
        job.base_ref ? h("span", {}, "Basis ", h("b", {}, job.base_ref), " @ ", h("span", { class: "mono" }, short(job.base_commit))) : null,
        job.options && job.options.dirty ? h("span", { class: "badge warn" }, "with local changes") : null,
        h("span", { title: dt(job.created_at) }, `submitted ${ago(job.created_at)}`),
        job.started_at ? h("span", {}, `duration ${duration(job.started_at, job.finished_at)}`) : null),
      job.message ? h("p", { class: "lede" }, job.message) : null), actions),
  ];

  if (r && r.dummy) {
    parts.push(h("div", { class: "banner warn" }, h("b", {}, "Dummy result. "),
      "Findings and measurements are made up – processing is not connected yet. Layout and workflow match the final state."));
  }

  parts.push(phasesCard(job));
  if (job.status === "failed") parts.push(h("div", { class: "banner bad" }, h("b", {}, "Failed: "), job.error || "unknown error"));

  if (r) {
    if (r.gate && r.gate.conditions.length) parts.push(gateCard(r.gate));
    if (r.measurement) parts.push(measurementCard(r.measurement, job.mode, r.best));
    if (r.tests && r.tests.length) parts.push(testsCard(r.tests));
    for (const w of r.warnings || []) parts.push(h("div", { class: "banner warn" }, w));
    if (r.files && Object.keys(r.files).length) parts.push(filesCard(job, r.files));
    if (r.hotspots && r.hotspots.length) parts.push(hotspotsCard(r.hotspots));
    if (r.candidates && r.candidates.length) parts.push(candidatesCard(r.candidates, job, r.best));
    if (job.mode === "review" && r.metrics) parts.push(reviewKpis(r, job));
    if (r.changed_functions && r.changed_functions.length) parts.push(changedFunctionsCard(r.changed_functions));
    if (job.mode !== "optimize" || (r.findings && r.findings.length)) parts.push(findingsCard(r, job));
  }
  parts.push(h("details", { class: "card", open: job.status === "failed" || null },
    h("summary", { style: "cursor:pointer;font-weight:600" }, `Log (${job.logs.length})`),
    h("div", { class: "log", style: "margin-top:10px" }, job.logs.map((l) =>
      h("div", { class: l.level }, h("time", {}, new Date(l.ts).toLocaleTimeString("en-GB")), l.message)))));

  const openDetails = [...$view.querySelectorAll("details")].map((d) => d.open);
  $view.replaceChildren(...parts);
  [...$view.querySelectorAll("details")].forEach((d, i) => { if (openDetails[i]) d.open = true; });
  if (active) poll(() => viewJob(id), 1200);
}

function phasesCard(job) {
  const phases = job.phases || [];
  const cur = phases.indexOf(job.phase);
  const doneAll = job.status === "done";
  const card = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Progress"),
    h("span", { class: "small muted" },
      job.status === "queued" ? `queued – position ${job.queue_position} in the queue` :
      job.status === "running" ? `${job.phase} (${Math.round(job.progress * 100)}%)` :
      job.status === "done" ? "finished" : job.status === "cancelled" ? "cancelled" : "aborted with an error")));
  card.append(h("div", { class: "phases" }, phases.map((p, i) => {
    let cls = "ph";
    if (doneAll || (cur >= 0 && i < cur)) cls += " done";
    else if (i === cur && job.status === "running") cls += " current";
    else if (i === cur && job.status === "failed") cls += " failed";
    return h("div", { class: cls }, h("div", { class: "bar" }), h("div", { class: "name", title: p }, p));
  })));
  return card;
}

function gateCard(gate) {
  const [cls, icon, label] = GATE[gate.status] || ["neutral", "?", gate.status];
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Quality Gate"), h("span", { class: `badge ${cls}` }, icon, " ", label)),
    h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, gate.conditions.map((c) =>
      h("tr", {}, h("td", { style: "width:32px" }, h("span", { class: `badge ${c.passed ? "good" : "bad"}` }, c.passed ? "✓" : "✗")),
        h("td", {}, h("b", {}, c.name)), h("td", {}, conditionText(c))))))));
}
function conditionText(c) {
  if (c.ci95) return `change ${pct(c.actual)} (CI ${pct(c.ci95[0])} … ${pct(c.ci95[1])}) · red only if the whole CI lies above ${pct(c.threshold)}`;
  if (typeof c.threshold === "string") return `${c.actual} finding(s) of severity “${c.threshold}” or higher`;
  return c.text;
}

function measurementCard(m, mode, best) {
  const [lo, hi] = m.ci95;
  const title = mode === "optimize" ? `Measurement: best candidate${best ? ` (${best})` : ""}` : "Measurement: PR vs. target branch";
  const card = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, title), verdictBadge(m.verdict)));
  if (m.is_energy === false) {
    card.append(h("div", { class: "banner info" }, `Back-end ${m.backend}: ${m.quantity || "proxy"}. Measuring energy requires metrion or RAPL on the server.`));
  }
  card.append(h("div", { class: "kpis", style: "margin-top:4px" },
    kpi(mode === "optimize" ? "Original (median)" : "Target branch (median)", `${num(m.baseline.median)} ${m.unit}`, `n = ${m.baseline.n}`),
    kpi(mode === "optimize" ? "Candidate (median)" : "PR (median)", `${num(m.candidate.median)} ${m.unit}`, `n = ${m.candidate.n}`),
    kpi("Change", pct(m.change), `95% CI ${pct(lo)} … ${pct(hi)}`),
    kpi("p value", num(m.p_value, 4), "Mann-Whitney-U")));
  card.append(ciBar(m));
  const LABEL = { cpu_s: "CPU time", wall_s: "Wall time" };
  const sec = Object.entries(m.secondary || {});
  if (sec.length) {
    card.append(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Other metric"), h("th", { class: "num" }, "Change"), h("th", {}, "95% CI"), h("th", {}, "Verdict"))),
      h("tbody", {}, sec.map(([k, v]) => h("tr", {}, h("td", {}, LABEL[k] || k), h("td", { class: "num" }, pct(v.change)),
        h("td", { class: "small nowrap" }, `${pct(v.ci95[0])} … ${pct(v.ci95[1])}`), h("td", {}, verdictBadge(v.verdict))))))));
  }
  card.append(h("p", { class: "small muted" }, `${m.quantity} · Backend: ${m.backend}${m.runs ? ` · ${m.runs} runs` : ""}. “Better” or “worse” only if p < α and the whole confidence interval lies on one side of zero.`));
  return card;
}

function testsCard(tests) {
  return h("div", { class: "card" }, h("h2", {}, "Tests"),
    tests.map((t) => h("div", { class: "finding" },
      h("div", { class: "line1" }, h("span", { class: `badge ${t.ok ? "good" : "bad"}` }, t.ok ? "✓ passed" : "✗ failed"),
        h("b", {}, t.variant), h("span", { class: "muted small" }, `${num(t.wall_s, 1)} s`)),
      t.output_tail ? h("details", { open: !t.ok || null }, h("summary", {}, "Output"), h("pre", { class: "snippet", style: "margin-top:6px" }, t.output_tail)) : null)));
}

function filesCard(job, files) {
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Reports"),
    h("span", { class: "small muted" }, "full evaluation as on the command line")),
    h("div", { class: "filters" }, Object.entries(files).map(([path, label]) =>
      h("a", { class: "btn", href: `/api/jobs/${job.id}/files/${path}`, target: "_blank", rel: "noopener" }, label))));
}

function hotspotsCard(hotspots) {
  return h("details", { class: "card" }, h("summary", { style: "cursor:pointer;font-weight:600" }, `Hotspots (${hotspots.length})`),
    h("p", { class: "small muted" }, "Functions with the largest share of the benchmark's runtime (profiling) – only these were optimised."),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Function"), h("th", {}, "File"), h("th", { class: "num" }, "Share of self time"), h("th", { class: "num" }, "Calls"))),
      h("tbody", {}, hotspots.map((x) => h("tr", {}, h("td", { class: "mono" }, x.qualname), h("td", { class: "mono small" }, `${x.file}:${x.line}`),
        h("td", { class: "num" }, `${num((x.share || 0) * 100, 0)} %`), h("td", { class: "num" }, x.ncalls ?? "–")))))));
}

function ciBar(m) {
  const W = chartWidth(), H = 64, ml = 16, mr = 16;
  const [lo, hi] = m.ci95;
  const ext = Math.max(Math.abs(lo), Math.abs(hi), 0.05) * 1.25;
  const x = (v) => ml + ((v + ext) / (2 * ext)) * (W - ml - mr);
  const c = { better: "var(--good)", worse: "var(--bad)" }[m.verdict] || "var(--neutral)";
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `change ${pct(m.change)}, CI ${pct(lo)} to ${pct(hi)}` });
  svg.append(s("line", { class: "grid", x1: ml, x2: W - mr, y1: 28, y2: 28 }),
    s("line", { class: "zero", x1: x(0), x2: x(0), y1: 10, y2: 46 }),
    s("text", { x: x(0), y: 60, "text-anchor": "middle" }, "0"),
    s("text", { x: ml, y: 60 }, pct(-ext, 0)), s("text", { x: W - mr, y: 60, "text-anchor": "end" }, pct(ext, 0)),
    s("rect", { x: x(lo), y: 22, width: Math.max(2, x(hi) - x(lo)), height: 12, rx: 4, fill: c, opacity: 0.3 }),
    s("circle", { class: "pt", cx: x(m.change), cy: 28, r: 6, fill: c }),
    s("text", { x: x(m.change), y: 14, "text-anchor": "middle", style: "fill:var(--text);font-weight:600" }, pct(m.change)));
  return svg;
}

const CAND_STATUS = {
  measured: ["info", "measured"], ready: ["neutral", "admitted"], vetoed: ["warn", "veto (maintainability)"],
  tests_failed: ["bad", "tests red"], duplicate: ["neutral", "duplicate"], invalid: ["bad", "not applicable"],
  measurement_failed: ["bad", "measurement failed"], new: ["neutral", "new"],
};

function candidatesCard(cands, job, best) {
  const measured = cands.filter((c) => c.change != null).length;
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, `Candidates (${cands.length})`),
    h("span", { class: "small muted" }, `${measured} measured · ★ Pareto (energy vs. maintainability) · a human makes the choice`)),
    cands.map((c) => {
      const [cls, label] = CAND_STATUS[c.status || "measured"] || ["neutral", c.status];
      return h("div", { class: "finding" },
        h("div", { class: "line1" },
          h("b", { class: "mono" }, c.id), c.pareto ? h("span", { title: "Pareto-optimal per hotspot" }, "★") : null,
          c.id === best ? h("span", { class: "badge good" }, "best measurement") : null,
          h("span", { class: "loc mono" }, c.hotspot), h("span", { class: `badge ${cls}` }, label),
          c.verdict ? verdictBadge(c.verdict) : null,
          c.change != null ? h("b", {}, pct(c.change)) : null,
          c.ci95 ? h("span", { class: "muted small" }, `CI ${pct(c.ci95[0])} … ${pct(c.ci95[1])}`) : null,
          c.maintainability != null ? h("span", { class: "muted small", title: "Reviewer score, 1–5" }, `maintainability ${num(c.maintainability, 1)}/5`) : null),
        h("div", { class: "msg" }, c.summary),
        c.rationale || (c.concerns && c.concerns.length) || (c.messages && c.messages.length) || c.patch ? h("details", {},
          h("summary", {}, "Details and patch"),
          c.rationale ? h("p", {}, c.rationale.replace(/\*\*(.+?)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1")) : null,
          c.parts && c.parts.length ? h("p", { class: "small muted" }, `combination of ${c.parts.join(", ")}`) : null,
          c.concerns && c.concerns.length ? h("div", { class: "small" }, h("b", {}, "Reviewer: "), c.concerns.join(" · ")) : null,
          c.messages && c.messages.length ? h("div", { class: "small muted" }, c.messages.join(" · ")) : null,
          c.patch ? patchView(c.patch) : null,
          c.patch_file ? h("p", {}, h("a", { href: `/api/jobs/${job.id}/files/${c.patch_file}`, download: `${c.id}.patch` }, "Download patch")) : null) : null);
    }));
}

const CATEGORY = { performance: "efficiency", maintainability: "maintainability", "dead-code": "dead code", other: "other" };
const findingFilter = { severity: "", category: "" };

function reviewKpis(r, job) {
  const sm = r.summary || {}, m = r.metrics;
  const diffKnown = sm.scope === "changed" || Boolean(job.base_ref);
  const sev = sm.by_severity || {};
  return h("div", { class: "kpis" },
    kpi("Findings", sm.findings ?? 0, `${sev.major || 0} major · ${sev.minor || 0} minor · ${sev.info || 0} info`),
    kpi(diffKnown ? "Changed functions" : "Functions", diffKnown ? sm.changed_functions : m.functions,
      diffKnown ? `of ${m.functions} in the repository` : "whole repository checked"),
    kpi("Code", `${m.files} ${m.files === 1 ? "file" : "files"}`, `${m.sloc} lines · ${m.language}`),
    kpi("Complexity avg / max", `${num(m.complexity_avg, 1)} / ${m.complexity_max}`, `MI avg ${num(m.mi_avg, 0)} · ${m.lint_tool}`));
}

function changedFunctionsCard(fns) {
  return h("details", { class: "card" },
    h("summary", { style: "cursor:pointer;font-weight:600" }, `Changed functions (${fns.length})`),
    h("p", { class: "small muted" }, "Functions with changed lines. Findings inside them count as changed code – even if the line itself was not touched."),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Function"), h("th", {}, "File"), h("th", { class: "num" }, "Lines"), h("th", { class: "num" }, "Complexity"), h("th", { class: "num" }, "Findings"))),
      h("tbody", {}, fns.map((f) => h("tr", {},
        h("td", { class: "mono" }, f.function), h("td", { class: "mono small" }, f.file),
        h("td", { class: "num" }, `${f.line}–${f.end_line}`),
        h("td", { class: "num" }, f.complexity > 10 ? h("span", { class: "badge warn", title: "complexity > 10" }, f.complexity) : f.complexity),
        h("td", { class: "num" }, f.findings || h("span", { class: "muted" }, "0"))))))));
}

function findingsCard(r, job) {
  const all = r.findings || [];
  const sm = r.summary || {};
  const card = h("div", { class: "card" });
  const counts = Object.entries(sm.by_evidence || {}).map(([e, n]) => h("span", {}, evidenceBadge(e), ` ${n}`));
  const head = h("div", { class: "card-head" }, h("h2", {}, `Findings (${all.length})`), h("span", { class: "counts" }, counts,
    job.status === "done" && all.length ? h("a", { href: `/api/jobs/${job.id}/sarif`, class: "small", title: "SARIF 2.1.0, e.g. for GitHub code scanning" }, "SARIF ↓") : null));
  card.append(head);
  if (sm.scope === "changed") {
    card.append(h("p", { class: "small muted" },
      `Only findings in functions changed relative to ${job.base_ref || "the target branch"}.`,
      sm.findings_outside_diff ? ` ${sm.findings_outside_diff} more in the remaining code (joulecode submit --all shows them).` : ""));
  } else if (job.changed_files && job.changed_files.length) {
    card.append(h("p", { class: "small muted" }, `${job.changed_files.length} changed file(s) relative to ${job.base_ref || "the target branch"} – findings in them first.`));
  } else if (!r.dummy) {
    card.append(h("p", { class: "small muted" }, "Whole repository checked (without --base there is no diff)."));
  }
  if (!all.length) {
    card.append(emptyState("No findings", sm.scope === "changed" ? "The tools found nothing in the changed code." : ""));
    return card;
  }

  const list = h("div");
  const sevSel = h("select", { "aria-label": "Severity", onchange: (e) => { findingFilter.severity = e.target.value; draw(); } },
    h("option", { value: "" }, "all severities"), ["major", "minor", "info"].map((v) => h("option", { value: v, selected: findingFilter.severity === v }, `${v} and up`)));
  const cats = [...new Set(all.map((f) => f.category).filter(Boolean))];
  const catSel = cats.length > 1 ? h("select", { "aria-label": "Category", onchange: (e) => { findingFilter.category = e.target.value; draw(); } },
    h("option", { value: "" }, "all categories"), cats.map((c) => h("option", { value: c, selected: findingFilter.category === c }, CATEGORY[c] || c))) : null;
  card.append(h("div", { class: "filters" }, sevSel, catSel), list);

  const rank = { major: 2, minor: 1, info: 0 };
  // only mark “changed code” if other code is shown as well
  const markDiff = sm.scope !== "changed" && all.some((f) => f.in_diff) && all.some((f) => !f.in_diff);
  function draw() {
    const shown = all.filter((f) => (!findingFilter.severity || rank[f.severity] >= rank[findingFilter.severity]) &&
      (!findingFilter.category || f.category === findingFilter.category));
    if (!shown.length) { list.replaceChildren(emptyState("No finding matches the filter", "")); return; }
    const byFile = new Map();
    for (const f of shown) { if (!byFile.has(f.file)) byFile.set(f.file, []); byFile.get(f.file).push(f); }
    const files = [...byFile.keys()].sort((a, b) => (byFile.get(b)[0].in_diff ? 1 : 0) - (byFile.get(a)[0].in_diff ? 1 : 0) || a.localeCompare(b));
    list.replaceChildren(...files.map((file) => h("div", { class: "file-group" },
      h("div", { class: "file" }, h("span", { class: "mono" }, file), h("span", { class: "muted small" }, `${byFile.get(file).length}`)),
      byFile.get(file).map((f) => findingView(f, markDiff)))));
  }
  draw();
  return card;
}

function findingView(f, markDiff) {
  return h("div", { class: "finding" },
    h("div", { class: "line1" }, h("span", { class: `sev ${f.severity}` }, f.severity), h("b", { class: "mono" }, f.rule),
      h("span", { class: "loc mono" }, `${f.function || "module level"} · line ${f.line}`), h("span", { class: "muted small" }, f.tool),
      f.category ? h("span", { class: "badge outline" }, CATEGORY[f.category] || f.category) : null,
      markDiff && f.in_diff ? h("span", { class: "badge info", title: "Located in a function changed by the PR" }, "changed code") : null,
      evidenceBadge(f.evidence, f.evidence_detail)),
    h("div", { class: "msg" }, f.message),
    f.why ? h("div", { class: "small muted" }, f.why) : null,
    f.evidence_detail ? h("div", { class: "small", style: "color:var(--good-text)" }, `Evidence: ${f.evidence_detail}`) : null,
    f.suggestion ? h("details", {}, h("summary", {}, `Suggestion: ${f.suggestion.description}`), patchView(f.suggestion.patch)) : null);
}

function patchView(patch) {
  const head = /^(\+\+\+|---|@@|diff |index )/;
  return h("pre", { class: "patch" }, patch.split("\n").map((l) =>
    h("span", { class: head.test(l) ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "" }, l || " ")));
}

// -- Queue ----------------------------------------------------------------------------
async function viewQueue() {
  const mine = routeToken;
  const q = await api("/api/queue");
  if (mine !== routeToken) return;
  $view.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Queue"),
      h("p", { class: "lede" }, "A single worker processes the jobs one after another – measurements must never overlap, and the LLM and measurements never run at the same time."))),
    h("div", { class: "card" }, h("h2", {}, "Running"), q.running.length ? jobsTable(q.running, true) : emptyState("Nothing in progress", "")),
    h("div", { class: "card" }, h("h2", {}, `Queued (${q.queued.length})`), q.queued.length ? jobsTable(q.queued, true) : emptyState("No queued jobs", "")));
  poll(viewQueue, 2000);
}

// -- Access ---------------------------------------------------------------------------
function viewSettings() {
  const input = h("input", { type: "password", placeholder: "gca_…", value: adminToken || "", autocomplete: "off", style: "flex:1 1 280px" });
  const msg = h("div");
  $view.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Access"),
      h("p", { class: "lede" }, "Viewing works without signing in. Creating projects, renewing tokens and cancelling jobs requires the admin token. joulecode serve prints it on first start; it is stored in admin-token in the data directory."))),
    h("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault();
      const t = input.value.trim();
      const prev = adminToken;
      adminToken = t || null;
      if (!t) { store.set("joulecode.admin", null); msg.replaceChildren(h("div", { class: "banner info" }, "Token removed.")); return; }
      try {
        await api("/api/auth/check", { admin: true });
        store.set("joulecode.admin", t);
        msg.replaceChildren(h("div", { class: "banner good" }, "✓ Token valid and saved in this browser."));
      } catch (err) {
        adminToken = prev;
        msg.replaceChildren(h("div", { class: "banner bad" }, `✗ ${err.message}`));
      }
    } },
    h("h2", {}, "Admin-Token"),
    h("div", { class: "form-row" }, input, h("button", { class: "btn primary", type: "submit" }, "Save")), msg,
    h("p", { class: "small muted" }, adminToken ? "A token is saved. Save an empty field to remove it." : "No token saved yet.")),
    h("div", { class: "card" }, h("h2", {}, "API"), h("p", {}, "The interface uses the same API as joulecode submit: ", h("a", { href: "/api/docs" }, "OpenAPI documentation"), ".")));
}

refreshHealth();
route();
