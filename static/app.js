// DIBBS RFQ Finder - vanilla JS front end. All filter state lives in the URL so searches are bookmarkable.

const PAGE_SIZE = 100;
const MULTI = ["kw", "nsn", "fsc"];
const $ = (sel) => document.querySelector(sel);

const state = {
  date: "", kw: [], nsn: [], fsc: [], set_aside: "", include_closed: false,
  sort: "return_by", desc: false, page: 0,
};
let fscNames = {};

// ---------- URL <-> state ----------
function readUrl() {
  const p = new URLSearchParams(location.search);
  for (const k of MULTI) state[k] = p.getAll(k);
  state.date = p.get("date") || "";
  state.set_aside = p.get("set_aside") || "";
  state.include_closed = p.get("include_closed") === "true";
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
  loadResults();
}

// ---------- helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const parseDay = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const fmtDay = (iso, opts = { weekday: "short", month: "short", day: "numeric", year: "numeric" }) => parseDay(iso).toLocaleDateString(undefined, opts);
const fmtNsn = (n) => /^\d{13}$/.test(n) ? `${n.slice(0, 4)}-${n.slice(4, 6)}-${n.slice(6, 9)}-${n.slice(9)}` : n;

function daysLeft(iso) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return Math.round((parseDay(iso) - today) / 86400000);
}

function highlight(text) {
  const safe = esc(text);
  const words = state.kw.map((k) => esc(k.trim())).filter(Boolean);
  if (!words.length) return safe;
  const re = new RegExp("(" + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  return safe.replace(re, "<mark>$1</mark>");
}

// ---------- data ----------
async function loadFacets() {
  const f = await (await fetch("/api/facets?" + new URLSearchParams(state.date ? { date: state.date } : {}))).json();
  if (!state.date) state.date = f.date || "";

  $("#date").innerHTML = f.dates.map((d) =>
    `<option value="${d.date}" ${d.date === state.date ? "selected" : ""}>${fmtDay(d.date)} · ${d.count.toLocaleString()} RFQs</option>`).join("");
  const current = f.dates.find((d) => d.date === state.date);
  $("#freshness").textContent = current?.scraped_at ? `Pulled ${new Date(current.scraped_at).toLocaleString()}` : "";

  fscNames = Object.fromEntries(f.fscs.map((x) => [x.fsc, x.name]));
  $("#fsc-options").innerHTML = f.fscs.map((x) =>
    `<option value="${x.fsc}">${esc(x.name || "")} (${x.count})</option>`).join("");
  $("#top-fsc").innerHTML = f.fscs.slice(0, 8).map((x) =>
    `<li><button type="button" data-fsc="${x.fsc}" title="Filter to FSC ${x.fsc}">
       <span><span class="code">${x.fsc}</span>${esc(x.name || "")}</span><span class="count">${x.count}</span>
     </button></li>`).join("");

  const opts = [["", "All"], ["Any set-aside", "Any set-aside"],
    ...f.set_asides.filter((s) => s.set_aside && s.set_aside !== "Unrestricted").map((s) => [s.set_aside, `${s.set_aside} (${s.count})`])];
  $("#set-aside").innerHTML = opts.map(([v, label]) => `<option value="${esc(v)}" ${v === state.set_aside ? "selected" : ""}>${esc(label)}</option>`).join("");
}

async function loadResults() {
  const p = query({ paged: false });
  p.set("limit", PAGE_SIZE);
  p.set("offset", state.page * PAGE_SIZE);
  const data = await (await fetch("/api/solicitations?" + p)).json();
  renderRows(data);
  $("#export").href = "/api/solicitations.csv?" + query({ paged: false });
}

// ---------- rendering ----------
function renderRows({ total, rows, offset }) {
  const filtered = MULTI.some((k) => state[k].length) || state.set_aside;
  $("#summary").textContent = `${total.toLocaleString()} ${filtered ? "matching" : "open"} RFQ${total === 1 ? "" : "s"}` +
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

function rowHtml(r) {
  const left = r.return_by_date ? daysLeft(r.return_by_date) : null;
  const due = left === null ? "" : left < 0 ? `<span class="badge closed">Closed</span>`
    : `<span class="badge ${left <= 3 ? "due-soon" : "due"}">${left === 0 ? "Due today" : `${left} day${left === 1 ? "" : "s"} left`}</span>`;
  const status = r.status && r.status !== "Open" ? ` <span class="badge status">${esc(r.status)}</span>` : "";
  const setAside = r.set_aside && r.set_aside !== "Unrestricted"
    ? `<span class="badge setaside">${esc(r.set_aside)}</span>` : `<span class="muted">Unrestricted</span>`;
  const nsn = r.nsn_url ? `<a class="mono" href="${esc(r.nsn_url)}" target="_blank" rel="noopener">${esc(fmtNsn(r.nsn))}</a>` : `<span class="mono">${esc(r.nsn)}</span>`;
  return `<tr>
    <td>${r.return_by_date ? fmtDay(r.return_by_date, { month: "short", day: "numeric" }) : "—"}<span class="sub">${due}</span></td>
    <td><a class="mono" href="${esc(r.package_url)}" target="_blank" rel="noopener" title="Open on DIBBS">${esc(r.solicitation_number)}</a>${status}
        <span class="sub"><a href="${esc(r.solicitation_url)}" target="_blank" rel="noopener">PDF</a>${r.tech_docs_url ? `<a href="${esc(r.tech_docs_url)}" target="_blank" rel="noopener">Tech docs</a>` : ""}</span></td>
    <td>${nsn}${r.mil_spec ? ` <span class="badge milspec">Mil-Spec</span>` : ""}</td>
    <td>${highlight(r.nomenclature)}</td>
    <td>${r.fsc ? `<button type="button" class="fsc-link" data-fsc="${r.fsc}" title="Filter to this FSC">${r.fsc}</button><span class="sub">${esc(r.fsc_name || "")}</span>` : `<span class="muted">—</span>`}</td>
    <td class="num">${r.quantity != null ? r.quantity.toLocaleString() : "—"}</td>
    <td>${setAside}</td>
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

// ---------- events ----------
function addValue(key, raw) {
  const v = key === "nsn" ? raw.replace(/[^0-9A-Za-z]/g, "") : raw.trim();
  if (!v || state[key].includes(v)) return;
  update({ [key]: [...state[key], v] });
}

document.querySelectorAll("input[data-key]").forEach((input) => {
  const commit = () => { addValue(input.dataset.key, input.value); input.value = ""; };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } });
  input.addEventListener("change", () => { if (input.dataset.key === "fsc") commit(); }); // datalist pick
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
});

document.querySelectorAll("th[data-sort]").forEach((th) =>
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    update({ sort: key, desc: state.sort === key ? !state.desc : false });
  }));

$("#date").addEventListener("change", async (e) => {
  state.date = e.target.value;
  await loadFacets();
  update({});
});
$("#set-aside").addEventListener("change", (e) => update({ set_aside: e.target.value }));
$("#include-closed").addEventListener("change", (e) => update({ include_closed: e.target.checked }));
$("#clear").addEventListener("click", () => update({ kw: [], nsn: [], fsc: [], set_aside: "", include_closed: false }));
$("#prev").addEventListener("click", () => update({ page: state.page - 1 }, { resetPage: false }));
$("#next").addEventListener("click", () => update({ page: state.page + 1 }, { resetPage: false }));

// ---------- boot ----------
(async () => {
  readUrl();
  await loadFacets();
  $("#include-closed").checked = state.include_closed;
  $("#set-aside").value = state.set_aside;
  renderChips();
  history.replaceState(null, "", "?" + query());
  loadResults();
})();
