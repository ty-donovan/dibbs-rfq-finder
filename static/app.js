// DIBBS RFQ Finder - vanilla JS front end. All filter state lives in the URL so searches are bookmarkable.

const PAGE_SIZE = 100;
const MULTI = ["kw", "nsn", "fsc", "amsc", "source"];
const CHIP_KEYS = ["kw", "nsn", "fsc", "source"];
const $ = (sel) => document.querySelector(sel);

const state = {
  date: "", kw: [], nsn: [], fsc: [], amsc: [], source: [], set_aside: "",
  include_closed: false, include_past_due: false, sort: "return_by", desc: false, page: 0,
};
let fscNames = {};
let amscMeanings = {};
let amscGroups = [];

// ---------- URL <-> state ----------
function readUrl() {
  const p = new URLSearchParams(location.search);
  for (const k of MULTI) state[k] = p.getAll(k);
  state.date = p.get("date") || "";
  state.set_aside = p.get("set_aside") || "";
  state.include_closed = p.get("include_closed") === "true";
  state.include_past_due = p.get("include_past_due") === "true";
  state.sort = p.get("sort") || "return_by";
  state.desc = p.get("desc") === "true";
  state.page = Number(p.get("page") || 0);
}

function query({ paged = true } = {}) {
  const p = new URLSearchParams();
  if (state.date) p.set("date", state.date);
  for (const k of MULTI) state[k].forEach((v) => p.append(k, v));
  if (state.set_aside) p.set("set_aside", state.set_aside);
  if (state.include_closed) p.set("include_closed", "true");
  if (state.include_past_due) p.set("include_past_due", "true");
  if (state.sort !== "return_by") p.set("sort", state.sort);
  if (state.desc) p.set("desc", "true");
  if (paged && state.page) p.set("page", state.page);
  return p;
}

function update(changes, { resetPage = true } = {}) {
  Object.assign(state, changes);
  if (resetPage) state.page = 0;
  history.replaceState(null, "", "?" + query());
  renderChips();
  renderAmscChecks();
  loadResults();
}

// ---------- helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const parseDay = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const fmtDay = (iso, opts = { weekday: "short", month: "short", day: "numeric", year: "numeric" }) => parseDay(iso).toLocaleDateString(undefined, opts);
const fmtNsn = (n) => /^\d{13}$/.test(n) ? `${n.slice(0, 4)}-${n.slice(4, 6)}-${n.slice(6, 9)}-${n.slice(9)}` : n;
const fmtMin = (m) => m < 1 ? "under a minute" : m < 60 ? `~${Math.round(m)} min` : `~${(m / 60).toFixed(1)} hr`;
const getJson = async (url, opts) => { const r = await fetch(url, opts); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); };

function daysLeft(iso) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return Math.round((parseDay(iso) - today) / 86400000);
}

function highlight(text, terms) {
  const safe = esc(text);
  const words = terms.map((k) => esc(k.trim())).filter(Boolean);
  if (!words.length) return safe;
  const re = new RegExp("(" + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  return safe.replace(re, "<mark>$1</mark>");
}

// ---------- data ----------
async function loadFacets() {
  const f = await getJson("/api/facets?" + new URLSearchParams(state.date ? { date: state.date } : {}));
  if (!state.date) state.date = f.date || "";
  amscMeanings = f.amsc_meanings;
  amscGroups = f.amsc_groups;

  $("#date").innerHTML = f.dates.map((d) =>
    `<option value="${d.date}" ${d.date === state.date ? "selected" : ""}>${fmtDay(d.date)} · ${d.count.toLocaleString()} RFQs</option>`).join("");
  const current = f.dates.find((d) => d.date === state.date);
  $("#freshness").textContent = current?.scraped_at ? `Pulled ${new Date(current.scraped_at).toLocaleString()}` : "";

  fscNames = Object.fromEntries(f.fscs.map((x) => [x.fsc, x.name]));
  $("#fsc-options").innerHTML = f.fscs.map((x) => `<option value="${x.fsc}">${esc(x.name || "")} (${x.count})</option>`).join("");
  $("#top-fsc").innerHTML = f.fscs.slice(0, 8).map((x) =>
    `<li><button type="button" data-fsc="${x.fsc}" title="Filter to FSC ${x.fsc}">
       <span><span class="code">${x.fsc}</span>${esc(x.name || "")}</span><span class="count">${x.count}</span>
     </button></li>`).join("");

  const opts = [["", "All"], ["Any set-aside", "Any set-aside"],
    ...f.set_asides.filter((s) => s.set_aside && s.set_aside !== "Unrestricted").map((s) => [s.set_aside, `${s.set_aside} (${s.count})`])];
  $("#set-aside").innerHTML = opts.map(([v, label]) => `<option value="${esc(v)}" ${v === state.set_aside ? "selected" : ""}>${esc(label)}</option>`).join("");

  const counts = f.amsc || {};
  const known = Object.entries(counts).filter(([k]) => k !== "unchecked").reduce((a, [, n]) => a + n, 0);
  const total = known + (counts.unchecked || 0);
  $("#coverage").textContent = total ? `Checked ${known.toLocaleString()} of ${total.toLocaleString()} NSN line items this day.` : "";
  const choices = [
    ...amscGroups.map((g) => ({ key: g.key, label: g.label, hint: `${g.letters.split("").join(", ")}: ${g.hint}`, cls: g.key })),
    { key: "unchecked", label: "Not checked yet", hint: "Use “Check approved sources & AMSC” to look these up.", cls: "unchecked" },
  ];
  $("#amsc-options").innerHTML = choices.map((c) =>
    `<label class="check amsc-choice" title="${esc(c.hint)}">
       <input type="checkbox" value="${c.key}"> <span class="dot ${c.cls}"></span>${esc(c.label)}
       <span class="count">${(counts[c.key] || 0).toLocaleString()}</span>
     </label>`).join("");
  renderAmscChecks();
}

async function loadResults() {
  const p = query({ paged: false });
  p.set("limit", PAGE_SIZE);
  p.set("offset", state.page * PAGE_SIZE);
  const data = await getJson("/api/solicitations?" + p);
  renderRows(data);
  $("#export").href = "/api/solicitations.csv?" + query({ paged: false });
  if ($("#check-menu").open) loadEstimate();
}

async function loadEstimate() {
  $("#est-filtered").textContent = $("#est-day").textContent = "…";
  const e = await getJson("/api/enrich/estimate?" + query({ paged: false }));
  const line = (x) => x.to_check ? `${x.to_check.toLocaleString()} of ${x.nsns.toLocaleString()} NSNs to check · ${fmtMin(x.minutes)}` : `All ${x.nsns.toLocaleString()} NSNs already checked`;
  $("#est-filtered").textContent = line(e.filtered);
  $("#est-day").textContent = line(e.day);
  document.querySelector('[data-scope="filtered"]').disabled = !e.filtered.to_check || jobRunning;
  document.querySelector('[data-scope="day"]').disabled = !e.day.to_check || jobRunning;
}

// ---------- rendering ----------
function renderRows({ total, rows, offset }) {
  const filtered = ["kw", "nsn", "fsc", "amsc", "source"].some((k) => state[k].length) || state.set_aside;
  const noun = filtered ? "matching " : state.include_closed || state.include_past_due ? "" : "open ";
  $("#summary").textContent = `${total.toLocaleString()} ${noun}RFQ${total === 1 ? "" : "s"}` +
    (state.date ? ` issued ${fmtDay(state.date, { weekday: "short", month: "short", day: "numeric" })}` : "");
  $("#empty").hidden = total > 0;
  $("#rows").innerHTML = rows.map(rowHtml).join("");

  const end = Math.min(offset + rows.length, total);
  $("#page-info").textContent = total ? `Showing ${(offset + 1).toLocaleString()}–${end.toLocaleString()} of ${total.toLocaleString()}` : "";
  $("#prev").disabled = state.page === 0;
  $("#next").disabled = end >= total;

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    if (th.dataset.sort === state.sort) th.setAttribute("aria-sort", state.desc ? "descending" : "ascending");
    else th.removeAttribute("aria-sort");
  });
}

const GROUP_SHORT = { open: "Open", approved: "Approved only", no_data: "Lacks data", other: "Other" };

function amscHtml(r) {
  if (!r.info_checked) return `<span class="muted">—</span>`;
  if (!r.amsc) return `<span class="muted" title="DIBBS lists no AMSC for this NSN">Not listed</span>`;
  const meaning = amscMeanings[r.amsc] || "";
  return `<span class="badge amsc ${r.amsc_group}" title="${esc(`AMSC ${r.amsc}: ${meaning}`)}">${esc(r.amsc)} · ${GROUP_SHORT[r.amsc_group] || ""}</span>`;
}

function sourcesHtml(r) {
  if (!r.info_checked) return `<span class="muted">—</span>`;
  if (!r.sources.length) return `<span class="muted">None listed</span>`;
  const line = (s) => `<div class="source"><span class="mono">${highlight(s.cage, state.source)}</span> · ${highlight(s.part_number, state.source)}` +
    (s.company ? `<span class="sub">${highlight(s.company, state.source)}</span>` : "") + `</div>`;
  const all = r.sources.map((s) => `${s.cage} ${s.part_number}${s.company ? ` (${s.company})` : ""}`).join("\n");
  const more = r.sources.length > 2 ? `<span class="sub" title="${esc(all)}">+${r.sources.length - 2} more</span>` : "";
  return r.sources.slice(0, 2).map(line).join("") + more;
}

function rowHtml(r) {
  const left = r.return_by_date ? daysLeft(r.return_by_date) : null;
  const due = left === null ? "" : left < 0 ? `<span class="badge closed">Closed</span>`
    : `<span class="badge ${left <= 3 ? "due-soon" : "due"}">${left === 0 ? "Due today" : `${left} day${left === 1 ? "" : "s"} left`}</span>`;
  const status = r.status && r.status !== "Open" ? ` <span class="badge status">${esc(r.status)}</span>` : "";
  const setAside = r.set_aside && r.set_aside !== "Unrestricted" ? ` <span class="badge setaside">${esc(r.set_aside)}</span>` : "";
  const nsn = r.nsn_url ? `<a class="mono" href="${esc(r.nsn_url)}" target="_blank" rel="noopener">${esc(fmtNsn(r.nsn))}</a>` : `<span class="mono">${esc(r.nsn)}</span>`;
  return `<tr>
    <td>${r.return_by_date ? fmtDay(r.return_by_date, { month: "short", day: "numeric" }) : "—"}<span class="sub">${due}</span></td>
    <td><a class="mono" href="${esc(r.package_url)}" target="_blank" rel="noopener" title="Open on DIBBS">${esc(r.solicitation_number)}</a>${status}
        <span class="sub"><a href="${esc(r.solicitation_url)}" target="_blank" rel="noopener">PDF</a>${r.tech_docs_url ? `<a href="${esc(r.tech_docs_url)}" target="_blank" rel="noopener">Tech docs</a>` : ""}${setAside}</span></td>
    <td>${nsn}${r.mil_spec ? ` <span class="badge milspec">Mil-Spec</span>` : ""}</td>
    <td>${highlight(r.nomenclature, state.kw)}</td>
    <td>${r.fsc ? `<button type="button" class="fsc-link" data-fsc="${r.fsc}" title="Filter to this FSC">${r.fsc}</button><span class="sub">${esc(r.fsc_name || "")}</span>` : `<span class="muted">—</span>`}</td>
    <td class="num">${r.quantity != null ? r.quantity.toLocaleString() : "—"}</td>
    <td>${amscHtml(r)}</td>
    <td class="sources">${sourcesHtml(r)}</td>
  </tr>`;
}

function renderChips() {
  document.querySelectorAll(".chips").forEach((box) => {
    const key = box.dataset.key;
    box.innerHTML = state[key].map((v, i) => {
      const label = key === "nsn" ? fmtNsn(v) : key === "fsc" && fscNames[v] ? `${v} · ${fscNames[v]}` : v;
      return `<span class="chip">${esc(label)}<button type="button" data-key="${key}" data-i="${i}" aria-label="Remove ${esc(label)}">×</button></span>`;
    }).join("");
  });
}

function renderAmscChecks() {
  document.querySelectorAll("#amsc-options input").forEach((cb) => { cb.checked = state.amsc.includes(cb.value); });
}

// ---------- DIBBS days + background jobs ----------
let jobRunning = false;
let pollTimer = null;
let lastRefresh = 0;

async function loadDibbsDates() {
  const sel = $("#dibbs-date");
  try {
    const dates = await getJson("/api/dibbs/dates");
    const fresh = dates.filter((d) => !d.stored), stored = dates.filter((d) => d.stored);
    sel.innerHTML =
      (fresh.length ? `<optgroup label="Not pulled yet">${fresh.map((d) => `<option value="${d.date}">${fmtDay(d.date)}</option>`).join("")}</optgroup>` : "") +
      (stored.length ? `<optgroup label="Already stored (re-pull)">${stored.map((d) => `<option value="${d.date}" data-stored="1">${fmtDay(d.date)} ✓</option>`).join("")}</optgroup>` : "");
    sel.disabled = false;
    $("#pull").disabled = jobRunning;
  } catch (e) {
    sel.innerHTML = `<option>DIBBS unavailable</option>`;
    sel.title = e.message;
  }
}

async function startJob(url) {
  try {
    showJob(await getJson(url, { method: "POST" }));
    pollJob();
  } catch (e) {
    alert(e.message);
  }
}

function showJob(job) {
  if (!job) return;
  jobRunning = job.status === "running";
  $("#job").hidden = false;
  $("#job").className = `job ${job.status}`;
  $("#job-label").textContent = job.status === "done" ? `✓ ${job.label}` : job.status === "error" ? `✗ ${job.label}` : job.label;
  $("#job-message").textContent = job.message || "";
  const pct = job.total ? Math.min(100, (job.done / job.total) * 100) : jobRunning ? 3 : 100;
  $("#job-bar").style.width = `${pct}%`;
  $("#job-dismiss").hidden = jobRunning;
  let eta = "";
  if (jobRunning && job.done && job.total) {
    const elapsed = (Date.now() - new Date(job.started_at)) / 1000;
    eta = `${fmtMin(((elapsed / job.done) * (job.total - job.done)) / 60)} left`;
  }
  $("#job-eta").textContent = eta;
  $("#pull").disabled = jobRunning || $("#dibbs-date").disabled;
}

async function pollJob() {
  clearTimeout(pollTimer);
  const job = await getJson("/api/jobs/current").catch(() => null);
  if (!job) return;
  const wasRunning = jobRunning;
  showJob(job);
  if (job.status === "running") {
    // Show enrichment results as they land, without hammering the API.
    if (job.kind === "enrich" && Date.now() - lastRefresh > 10000) { lastRefresh = Date.now(); loadFacets().then(loadResults); }
    pollTimer = setTimeout(pollJob, 2000);
  } else if (wasRunning) {
    if (job.kind === "scrape" && job.status === "done") {
      state.date = job.result.date;
      loadDibbsDates();
    }
    await loadFacets();
    update({}, { resetPage: job.kind === "scrape" });
  }
}

// ---------- events ----------
function addValue(key, raw) {
  const v = key === "nsn" ? raw.replace(/[^0-9A-Za-z]/g, "") : raw.trim();
  if (!v || state[key].includes(v)) return;
  update({ [key]: [...state[key], v] });
}

document.querySelectorAll("input[data-key]").forEach((input) => {
  const commit = () => { addValue(input.dataset.key, input.value); input.value = ""; };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } });
  input.addEventListener("input", (e) => { // picking a datalist suggestion applies it immediately
    if (input.dataset.key === "fsc" && (!e.inputType || e.inputType === "insertReplacementText")) commit();
  });
  input.addEventListener("blur", () => { if (input.value.trim()) commit(); });
});

document.addEventListener("click", (e) => {
  const remove = e.target.closest(".chip button");
  if (remove) {
    const { key, i } = remove.dataset;
    update({ [key]: state[key].filter((_, j) => j !== Number(i)) });
    return;
  }
  const fsc = e.target.closest("[data-fsc]");
  if (fsc) addValue("fsc", fsc.dataset.fsc);
  const scope = e.target.closest("[data-scope]");
  if (scope && !scope.disabled) {
    const big = scope.dataset.scope === "day";
    const est = $(big ? "#est-day" : "#est-filtered").textContent;
    if (big && !confirm(`Check the whole day on DIBBS?\n${est}\n\nIt runs in the background; you can keep searching.`)) return;
    $("#check-menu").open = false;
    startJob(`/api/jobs/enrich?scope=${scope.dataset.scope}&` + query({ paged: false }));
  }
});

$("#amsc-options").addEventListener("change", () =>
  update({ amsc: [...document.querySelectorAll("#amsc-options input:checked")].map((cb) => cb.value) }));

document.querySelectorAll("th[data-sort]").forEach((th) =>
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    update({ sort: key, desc: state.sort === key ? !state.desc : false });
  }));

$("#check-menu").addEventListener("toggle", (e) => { if (e.target.open) loadEstimate(); });
$("#pull").addEventListener("click", () => {
  const opt = $("#dibbs-date").selectedOptions[0];
  if (!opt?.value) return;
  if (opt.dataset.stored && !confirm(`${fmtDay(opt.value)} is already stored. Pull it again from DIBBS (~10 min)?`)) return;
  startJob(`/api/jobs/scrape?date=${opt.value}`);
});
$("#job-dismiss").addEventListener("click", () => { $("#job").hidden = true; });
$("#date").addEventListener("change", async (e) => {
  state.date = e.target.value;
  await loadFacets();
  update({});
});
$("#set-aside").addEventListener("change", (e) => update({ set_aside: e.target.value }));
$("#include-closed").addEventListener("change", (e) => update({ include_closed: e.target.checked }));
$("#include-past-due").addEventListener("change", (e) => update({ include_past_due: e.target.checked }));
$("#clear").addEventListener("click", () =>
  update({ kw: [], nsn: [], fsc: [], amsc: [], source: [], set_aside: "", include_closed: false, include_past_due: false }));
$("#prev").addEventListener("click", () => update({ page: state.page - 1 }, { resetPage: false }));
$("#next").addEventListener("click", () => update({ page: state.page + 1 }, { resetPage: false }));

// ---------- boot ----------
(async () => {
  readUrl();
  await loadFacets();
  $("#include-closed").checked = state.include_closed;
  $("#include-past-due").checked = state.include_past_due;
  $("#set-aside").value = state.set_aside;
  renderChips();
  history.replaceState(null, "", "?" + query());
  loadResults();
  loadDibbsDates();
  const job = await getJson("/api/jobs/current").catch(() => null);
  if (job?.status === "running") { showJob(job); pollJob(); }
})();
