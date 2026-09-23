// Dashboard for the Express sales analytics pipeline.
//
// No build step on purpose: this machine has no working npm, so a bundled
// framework could not be compiled or tested before deploying. Plain ES modules
// load straight from the browser and from a CDN, and the whole app is
// verifiable by opening the file.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.47.10";

const CFG = window.CONFIG;
const AUTH_ON = Boolean(CFG.SUPABASE_URL && CFG.SUPABASE_ANON_KEY);
const sb = AUTH_ON ? createClient(CFG.SUPABASE_URL, CFG.SUPABASE_ANON_KEY) : null;

const PAGES = [
  ["overview",  "ภาพรวม",          "รายได้และแนวโน้มรายเดือน"],
  ["sales",     "ยอดขาย",          "แยกตามกลุ่มสินค้าและพนักงานขาย"],
  ["demand",    "ความต้องการ",     "ปริมาณขายรายสัปดาห์"],
  ["forecast",  "พยากรณ์",         "ล่วงหน้า 4 สัปดาห์ และสัญญาณเตือน"],
  ["stock",     "สต็อก",           "จุดสั่งซื้อและสินค้าที่ควรตรวจสอบ"],
  ["accuracy",  "ความแม่นยำ",      "ผลการทดสอบย้อนหลังของโมเดล"],
  ["customers", "ลูกค้า",          "การจัดกลุ่มลูกค้าด้วย RFM"],
  ["upload",    "อัปโหลดข้อมูล",   "นำเข้าไฟล์รายงานจาก Express"],
  ["admin",     "ผู้ใช้และสิทธิ์",  "กำหนดบทบาท รหัสพนักงานขาย และทีม"],
];

// Identity + what this role may open, from GET /api/me. Fetched once per
// render, never cached across a sign-in.
//
// This drives which links appear in the sidebar. That is PRESENTATION ONLY.
// Every endpoint re-checks the same rule server-side and answers 403, so
// typing #upload into the address bar as a salesperson gets an error page and
// not a dataset. Hiding a link is a courtesy; it is not the control.
let ME = null;

async function loadMe() {
  try {
    ME = await api("/api/me");
  } catch {
    ME = { role: "none", pages: {} };
  }
  return ME;
}

const allowed = (key) => ME?.pages?.[key] !== false;

// A figure computed over a subset must not sit under a label that says
// otherwise. Every page payload carries a scope note; this prints it.
function scopeBanner(s) {
  if (!s) return "";
  if (s.level === "company" && s.role === "ceo") return "";
  const cls = s.level === "company" ? "info" : "scope";
  const extra = s.level === "company" && s.role !== "ceo"
    ? " — ตัวเลขหน้านี้เป็นของทั้งบริษัท ไม่ใช่เฉพาะยอดของคุณ" : "";
  return `<div class="msg ${cls}">ขอบเขตข้อมูล: <b>${esc(s.label_th || "")}</b>${esc(extra)}</div>`;
}

// ------------------------------------------------------------- utilities
const $ = (h) => { const t = document.createElement("template"); t.innerHTML = h.trim(); return t.content.firstChild; };
const root = () => document.getElementById("root");

const nf = (v, d = 0) => v === null || v === undefined || Number.isNaN(v)
  ? "–" : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const baht = (v, d = 0) => v === null || v === undefined ? "–" : nf(v, d);
const pct = (v, d = 1) => v === null || v === undefined || Number.isNaN(v) ? "–" : `${Number(v) > 0 ? "+" : ""}${nf(v, d)}%`;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function signed(v, d = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return '<span class="muted">–</span>';
  const cls = Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "muted";
  return `<span class="${cls}">${pct(v, d)}</span>`;
}

// Dates arrive ISO ("2025-12", "2026-08-31") and were printed that way. Every
// other document in this office is dated in the Buddhist Era, so 2025-12 read
// as a year three years in the past to the people using the page.
//
// DISPLAY ONLY, and that is the whole design: nothing here is ever used as a
// key, a sort value or an API argument. The payloads stay ISO, the charts are
// still ordered by the ISO string, and `by_group_month` is still keyed on it.
// A Buddhist-Era label that leaked into a key would sort ก.พ. before ม.ค. and
// silently reorder every chart on the site.
const MONTH_TH = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                  "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];

// Anything that is not an ISO date falls through unchanged rather than
// rendering "NaN undefined" -- a blank last_sale_date is a real row.
const monthTH = (iso) => {
  const m = /^(\d{4})-(\d{2})/.exec(String(iso ?? ""));
  return m ? (window.UI.language === "en" ? `${window.UI.months[+m[2]-1]} ${m[1]}` : `${MONTH_TH[+m[2] - 1]} ${+m[1] + 543}`) : esc(iso ?? "");
};
const dateTH = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso ?? ""));
  return m ? (window.UI.language === "en" ? `${+m[3]} ${window.UI.months[+m[2]-1]} ${m[1]}` : `${+m[3]} ${MONTH_TH[+m[2] - 1]} ${+m[1] + 543}`) : esc(iso ?? "");
};

// A group that has sold nothing for four weeks still had a forecast and a
// reorder point printed against it -- group 05 was advertising 251 m to order
// and a 323 m reorder level months after it stopped moving. Both numbers are
// arithmetically correct and practically wrong: they are extrapolations of a
// series that ended. Suppress the number rather than qualify it, so nobody can
// order from it by reading past the caveat.
const STOPPED_TH = "หยุดขาย — ไม่แนะนำให้สั่ง";
const isStopped = (r) => r.stopped === true || r.stopped === "true";
const stopped = (r, render) => isStopped(r)
  ? `<span class="pill bad">${STOPPED_TH}</span>`
  : render(r);

// Charts must be destroyed before their canvas is replaced or Chart.js keeps
// the old instance alive and the tooltips of two charts fight each other.
const CHARTS = [];
function chart(canvas, cfg) {
  cfg.data.datasets.forEach(d => { d.label = window.UI.text(d.label); });
  cfg.data.labels = cfg.data.labels?.map(window.UI.text);
  const ink = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim();
  const grid = getComputedStyle(document.documentElement).getPropertyValue("--line").trim();
  Chart.defaults.color = ink;
  Chart.defaults.borderColor = grid;
  CHARTS.push(new Chart(canvas, cfg));
}
function clearCharts() { while (CHARTS.length) CHARTS.pop().destroy(); }

function table(cols, rows, opts = {}) {
  const head = cols.map(c => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map(r => "<tr>" + cols.map(c => {
    const v = c.render ? c.render(r) : esc(r[c.key]);
    return `<td ${["customer_name", "product_name", "group_name", "name", "customer_code", "sku"].includes(c.key) ? "data-original-value" : ""} class="${c.num ? "num" : ""}">${v}</td>`;
  }).join("") + "</tr>").join("");
  return `<div class="${opts.scroll === false ? "" : "scroll"}"><table>
    <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// ----------------------------------------------------------------- auth
async function token() {
  if (!AUTH_ON) return null;
  const { data } = await sb.auth.getSession();
  return data.session?.access_token ?? null;
}

async function api(path, opts = {}) {
  const t = await token();
  const headers = { ...(opts.headers || {}) };
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`${CFG.API_URL}${path}`, { ...opts, headers });
  if (res.status === 401) { await signOut(); throw new Error("กรุณาเข้าสู่ระบบใหม่"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok && !body.checks) {
    throw new Error(body.detail || body.errors_th?.join(" / ") || `เกิดข้อผิดพลาด (${res.status})`);
  }
  return body;
}

async function signOut() { if (AUTH_ON) await sb.auth.signOut(); location.hash = ""; render(); }

function loginView() {
  const v = $(`<div class="login-wrap"><div class="login-card">
    <h1>เข้าสู่ระบบ</h1>
    <p class="sub">ระบบวิเคราะห์การขาย — หลังคาเหล็ก ฉนวนพียูโฟม</p>
    <div id="err"></div>
    <label for="em">อีเมล</label>
    <input id="em" type="email" autocomplete="email" placeholder="you@example.com">
    <label for="pw">รหัสผ่าน</label>
    <input id="pw" type="password" autocomplete="current-password" placeholder="••••••••">
    <button id="go">เข้าสู่ระบบ</button>
  </div></div>`);

  const err = v.querySelector("#err");
  const go = v.querySelector("#go");
  const submit = async () => {
    err.innerHTML = "";
    const email = v.querySelector("#em").value.trim();
    const password = v.querySelector("#pw").value;
    if (!email || !password) { err.innerHTML = `<div class="msg err">กรุณากรอกอีเมลและรหัสผ่าน</div>`; return; }
    go.disabled = true; go.textContent = "กำลังเข้าสู่ระบบ…";
    const { error } = await sb.auth.signInWithPassword({ email, password });
    go.disabled = false; go.textContent = "เข้าสู่ระบบ";
    if (error) {
      const th = /invalid login/i.test(error.message)
        ? "อีเมลหรือรหัสผ่านไม่ถูกต้อง"
        : /email not confirmed/i.test(error.message)
        ? "ยังไม่ได้ยืนยันอีเมล กรุณาตรวจสอบกล่องจดหมาย"
        : error.message;
      err.innerHTML = `<div class="msg err">${esc(th)}</div>`;
      return;
    }
    render();
  };
  go.onclick = submit;
  v.querySelectorAll("input").forEach(i => i.addEventListener("keydown", e => { if (e.key === "Enter") submit(); }));
  return v;
}

// ---------------------------------------------------------------- pages
const P = {};

// "สิ่งที่ต้องดูวันนี้" summarises two OTHER pages, so it is built from their
// endpoints -- and only for a role that is allowed to open them. A rep who
// cannot reach «พยากรณ์» must not be handed its headline count on the front
// page; that would be the same leak by a shorter route.
//
// `allowed()` mirrors /api/me and is a courtesy. The fetch is wrapped anyway:
// the server is the control, and a 403 from a side panel must not take the
// whole overview page down with it.
async function sidePanelData() {
  const side = async (path, key) => {
    if (!allowed(key)) return null;
    try { return await api(path); } catch { return null; }
  };
  const [forecast, stock] = await Promise.all([
    side("/api/forecast", "forecast"),
    side("/api/stock", "stock"),
  ]);
  return { forecast, stock };
}

function todayPanel({ forecast, stock }) {
  const tiles = [];
  if (forecast) {
    const alerts = (forecast.trend_alerts || [])
      .filter(r => r.alert === true || r.alert === "true");
    const down = alerts.filter(r => r.direction === "falling").length;
    const up = alerts.filter(r => r.direction === "rising").length;
    tiles.push({
      href: "#forecast", n: alerts.length, unit: "กลุ่มสินค้า",
      label: "แนวโน้มเปลี่ยน",
      sub: alerts.length ? `ลดลง ${down} · เพิ่มขึ้น ${up}` : "ไม่มีสัญญาณเตือน",
    });
  }
  if (stock) {
    const sc = stock.stock_check || [];
    const risk = sc.filter(r => r.risk_level === "risk").length;
    const watch = sc.filter(r => r.risk_level === "watch").length;
    tiles.push({
      href: "#stock", n: risk, unit: "รายการ",
      label: "ควรไปตรวจสอบสต็อก",
      sub: watch ? `และเฝ้าระวังอีก ${nf(watch)} รายการ` : "ไม่มีรายการเฝ้าระวัง",
    });
  }
  if (!tiles.length) return "";

  // Both source pages are company-level whatever the reader's scope, so the
  // panel says so. Without it a rep reads "12 กลุ่มสินค้า" directly under
  // their own revenue total and takes it for their own.
  const note = ME?.role === "ceo" ? ""
    : `<p class="hint">ตัวเลขสองช่องนี้เป็นของทั้งบริษัท ไม่ใช่เฉพาะยอดของคุณ</p>`;
  const quiet = tiles.every(t => t.n === 0);
  return `<div class="panel"><h3>สิ่งที่ต้องดูวันนี้</h3>${note}
    ${quiet ? `<p class="hint">ไม่มีรายการที่ต้องดูวันนี้</p>` : ""}
    <div class="cards">${tiles.map(t => `
      <a class="card" href="${t.href}" style="text-decoration:none;color:inherit">
        <div class="label">${esc(t.label)}</div>
        <div class="value">${nf(t.n)}</div>
        <div class="sub">${esc(t.unit)} · ${esc(t.sub)}</div>
      </a>`).join("")}</div></div>`;
}

P.overview = async (el) => {
  const [d, side] = await Promise.all([api("/api/overview"), sidePanelData()]);
  const t = d.totals, k = d.kpi_monthly;
  el.innerHTML = `
    ${scopeBanner(d.scope)}
    <div class="cards">
      <div class="card"><div class="label">รายได้รวม (ไม่รวม VAT)</div>
        <div class="value">${baht(t.revenue_ex_vat)}</div><div class="sub">${t.months} เดือน</div></div>
      <div class="card"><div class="label">จำนวนเอกสาร</div>
        <div class="value">${nf(t.documents)}</div><div class="sub">ใบขาย</div></div>
      <div class="card"><div class="label">เดือนล่าสุด (${monthTH(t.latest_month)})</div>
        <div class="value">${baht(t.latest_revenue)}</div>
        <div class="sub">ต่อวันขาย ${signed(t.latest_pct_change_per_selling_day)}</div></div>
    </div>
    ${todayPanel(side)}
    <div class="panel"><h3>รายได้รวมรายเดือน</h3>
      <p class="hint">รายได้รวมไม่รวม VAT ของแต่ละเดือน (บาท)</p>
      <div class="chart-wrap"><canvas id="monthly-revenue"></canvas></div></div>
    <div class="panel"><h3>รายได้เฉลี่ยต่อวันขาย</h3>
      <p class="hint">รายได้หารด้วยจำนวนวันที่มีการขาย ช่วยเปรียบเทียบเดือนที่มีวันขายไม่เท่ากัน (บาท/วันขาย)</p>
      <div class="chart-wrap"><canvas id="daily-revenue"></canvas></div></div>
    <div class="panel"><h3>ตารางรายเดือน</h3>
      ${table([
        { key: "month", label: "เดือน", render: r => monthTH(r.month) },
        { key: "revenue_ex_vat", label: "รายได้", num: true, render: r => baht(r.revenue_ex_vat) },
        { key: "n_documents", label: "เอกสาร", num: true, render: r => nf(r.n_documents) },
        { key: "selling_days", label: "วันขาย", num: true },
        { key: "revenue_per_selling_day", label: "ต่อวันขาย", num: true, render: r => baht(r.revenue_per_selling_day) },
        { key: "n_customers", label: "จำนวนรหัสลูกค้า", num: true, render: r => nf(r.n_customers) },
        { key: "avg_document_value", label: "เฉลี่ย/ใบ", num: true, render: r => baht(r.avg_document_value) },
        // Named for what it measures. Plain "เปลี่ยนแปลง" next to a revenue
        // column reads as the change in revenue, but this is the change in
        // revenue PER SELLING DAY -- a month with three more working days can
        // show more revenue and a negative figure here, and that looked like
        // a bug until the column said which one it was.
        { key: "pct_change_per_selling_day", label: "เปลี่ยนแปลง (ต่อวันขาย)", num: true, render: r => signed(r.pct_change_per_selling_day) },
      ], k, { scroll: false })}</div>`;

  for (const [id, type, field, label, color] of [
    ["monthly-revenue", "bar", "revenue_ex_vat", "รายได้รวมรายเดือน", "#79aaff"],
    ["daily-revenue", "line", "revenue_per_selling_day", "รายได้เฉลี่ยต่อวันขาย", "#e36555"],
  ]) {
    chart(el.querySelector(`#${id}`), {
      type,
      data: { labels: k.map(r => monthTH(r.month)), datasets: [{ label,
        data: k.map(r => r[field]), backgroundColor: color, borderColor: color,
        tension: .25, pointRadius: 4 }] },
      options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        scales: { y: { beginAtZero: true, min: 0, ticks: { callback: v => nf(v) } } } },
    });
  }
};

// rows x columns from a long payload, plus a row total and a column total.
// Returns pre-escaped HTML: a pivot has a different column set per dataset,
// so it cannot go through `table()` without building the column objects
// dynamically, and doing it here keeps the sum in one place.
function pivot(rows, rowKey, colKey, valKey, opts = {}) {
  const cols = opts.cols || [...new Set(rows.map(r => r[colKey]))].sort();
  const cell = {};
  const rowTotal = {}, colTotal = {};
  let grand = 0;
  for (const r of rows) {
    const a = r[rowKey], b = r[colKey], v = Number(r[valKey]) || 0;
    (cell[a] ||= {})[b] = (cell[a][b] || 0) + v;
    rowTotal[a] = (rowTotal[a] || 0) + v;
    colTotal[b] = (colTotal[b] || 0) + v;
    grand += v;
  }
  // Biggest first: with five reps and nine months the useful question is who
  // is where, and alphabetical order by code answers a different one.
  const names = Object.keys(cell).sort((a, b) => rowTotal[b] - rowTotal[a]);
  const head = `<th>${esc(opts.rowLabel || "")}</th>` +
    cols.map(c => `<th class="num">${opts.colFmt ? opts.colFmt(c) : esc(c)}</th>`).join("") +
    `<th class="num">รวม</th>`;
  const body = names.map(n =>
    `<tr><td>${esc(n)}</td>` +
    cols.map(c => `<td class="num">${cell[n][c] ? baht(cell[n][c]) : '<span class="muted">–</span>'}</td>`).join("") +
    `<td class="num"><strong>${baht(rowTotal[n])}</strong></td></tr>`).join("");
  // The column total row is the check that makes the pivot readable as
  // evidence: it must re-add to the same figure the overview card shows.
  const foot = `<tr><td><strong>รวม</strong></td>` +
    cols.map(c => `<td class="num"><strong>${baht(colTotal[c] || 0)}</strong></td>`).join("") +
    `<td class="num"><strong>${baht(grand)}</strong></td></tr>`;
  return `<div class="scroll"><table>
    <thead><tr>${head}</tr></thead><tbody>${body}</tbody>
    <tfoot>${foot}</tfoot></table></div>`;
}

P.sales = async (el) => {
  const d = await api("/api/sales");
  const g = d.by_group_month, p = d.by_person_month;
  const pc = d.by_person_category || [];
  const months = [...new Set(g.map(r => r.month))].sort();
  const byGroup = {};
  g.forEach(r => { (byGroup[r.group_name] ||= {})[r.month] = r.revenue_ex_vat; });
  const totals = Object.entries(byGroup)
    .map(([name, m]) => ({ name, total: Object.values(m).reduce((a, b) => a + (b || 0), 0), m }))
    .sort((a, b) => b.total - a.total);
  const grand = totals.reduce((a, b) => a + b.total, 0);
  const palette = ["#1f6feb", "#1a7f37", "#9a6700", "#b42318", "#6f42c1", "#0a7ea4", "#d1671f", "#6b7889"];

  // The chart drew the top 8 groups only, so a stacked bar was short of the
  // month's real revenue by whatever the remaining groups sold -- the reader
  // has no way to see that the bar is a subset. Everything past the 8th is
  // summed into one grey "อื่นๆ" band so the bar height IS the month total.
  const top = totals.slice(0, 8);
  const rest = totals.slice(8);
  const series = top.map((t, i) => ({
    label: t.name, data: months.map(m => t.m[m] || 0),
    backgroundColor: palette[i % palette.length],
  }));
  if (rest.length) {
    series.push({
      label: `อื่นๆ (${rest.length} กลุ่ม)`,
      data: months.map(m => rest.reduce((a, t) => a + (t.m[m] || 0), 0)),
      backgroundColor: "#c9ced6",
    });
  }

  el.innerHTML = `
    ${scopeBanner(d.scope)}
    <div class="panel"><h3>รายได้ตามกลุ่มสินค้า</h3>
      <p class="hint">${rest.length
        ? `แสดง 8 กลุ่มที่มีรายได้สูงสุด ส่วนที่เหลือรวมเป็น «อื่นๆ» — ความสูงของแท่งจึงเท่ากับรายได้รวมของเดือนนั้น`
        : `ทุกกลุ่มสินค้า — ความสูงของแท่งเท่ากับรายได้รวมของเดือนนั้น`}</p>
      <div class="chart-wrap"><canvas id="c2"></canvas></div></div>
    <div class="panel"><h3>สรุปตามกลุ่มสินค้า</h3>
      ${table([
        { key: "name", label: "กลุ่มสินค้า" },
        { key: "total", label: "รายได้รวม", num: true, render: r => baht(r.total) },
        { key: "share", label: "สัดส่วน", num: true, render: r => nf(r.total / grand * 100, 1) + "%" },
      ], totals)}</div>
    <div class="panel"><h3>พนักงานขาย × เดือน</h3>
      <p class="hint">รายได้ไม่รวม VAT · แถวเรียงจากยอดรวมมากไปน้อย
        แถวล่างสุดคือยอดรวมของทุกคนในเดือนนั้น</p>
      ${pivot(p, "salesperson_code", "month", "revenue_ex_vat",
              { cols: months, rowLabel: "รหัสพนักงานขาย", colFmt: monthTH })}</div>
    <div class="panel"><h3>พนักงานขาย × หมวดสินค้า</h3>
      <p class="hint">ใครขายอะไร — รายได้ไม่รวม VAT ตลอดช่วงข้อมูล</p>
      ${pc.length ? pivot(pc, "salesperson_code", "category", "revenue_ex_vat",
                          { rowLabel: "รหัสพนักงานขาย" })
                  : `<p class="hint">ไม่มีข้อมูล</p>`}</div>
    <div class="panel"><h3>พนักงานขายรายเดือน (รายละเอียด)</h3>
      ${table([
        { key: "salesperson_code", label: "รหัส" },
        { key: "month", label: "เดือน", render: r => monthTH(r.month) },
        { key: "revenue_ex_vat", label: "รายได้", num: true, render: r => baht(r.revenue_ex_vat) },
        { key: "n_documents", label: "เอกสาร", num: true, render: r => nf(r.n_documents) },
      ], p)}</div>`;

  chart(el.querySelector("#c2"), {
    type: "bar",
    data: { labels: months.map(monthTH), datasets: series },
    options: {
      maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { callback: v => (v / 1e6).toFixed(1) + "M" } } },
    },
  });
};

P.demand = async (el) => {
  const d = await api("/api/demand");
  const w = d.weekly.filter(r => r.forecast_scope === true || r.forecast_scope === "true" || r.forecast_scope === undefined);
  const weeks = [...new Set(d.weekly.map(r => r.week_start))].sort();
  const groups = [...new Set(d.weekly.map(r => r.group_name))];
  const sel = groups[0];
  el.innerHTML = `
    ${scopeBanner(d.scope)}
    <div class="panel"><h3>ความต้องการรายสัปดาห์</h3>
      <p class="hint">เลือกกลุ่มสินค้าเพื่อดูปริมาณขายรายสัปดาห์ (${weeks.length} สัปดาห์)</p>
      <select id="g" style="padding:8px;border-radius:7px;border:1px solid var(--line);font-family:inherit;margin-bottom:12px">
        ${groups.map(g => `<option ${g === sel ? "selected" : ""}>${esc(g)}</option>`).join("")}
      </select>
      <div class="chart-wrap"><canvas id="c3"></canvas></div></div>`;

  const draw = (name) => {
    clearCharts();
    const rows = d.weekly.filter(r => r.group_name === name).sort((a, b) => a.week_start < b.week_start ? -1 : 1);
    chart(el.querySelector("#c3"), {
      type: "line",
      data: {
        labels: rows.map(r => dateTH(r.week_start)),
        datasets: [{ label: name, data: rows.map(r => r.qty), borderColor: "#1f6feb", backgroundColor: "#dbe8ff", fill: true, tension: .2 }],
      },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } } },
    });
  };
  el.querySelector("#g").onchange = (e) => draw(e.target.value);
  draw(sel);
};

const isTrue = (v) => v === true || v === "true";

P.forecast = async (el) => {
  const d = await api("/api/forecast");
  const f = d.next_4_weeks, a = d.trend_alerts;
  const flagged = a.filter(r => isTrue(r.alert));
  // Groups whose whole 10-90 band sits UNDER the point forecast. That is the
  // backtest saying this model has overshot every time it was tested here, so
  // the point figure is the wrong number to order from -- the low end is.
  // Pulled from the payload, not hardcoded: it is recomputed on every upload
  // and a list of group codes frozen into the page would go stale silently.
  const overs = f.filter(r => isTrue(r.range_below_forecast) && !isStopped(r));
  const overNote = overs.length
    ? `<div class="banner">${overs.length} กลุ่ม (${overs.map(r => esc(r.sku_prefix)).join(", ")})
        มีช่วงค่าทั้งช่วงต่ำกว่าค่าพยากรณ์ — แปลว่าในการทดสอบย้อนหลัง
        โมเดลพยากรณ์สูงเกินจริงเกือบทุกครั้ง ให้ยึด «ต่ำสุด» เป็นหลัก</div>`
    : "";

  el.innerHTML = `
    ${scopeBanner(d.scope)}
    <div class="banner">กลุ่มสินค้าที่ขายไม่สม่ำเสมอ (intermittent) แสดงเป็นค่าประมาณเท่านั้น
      ไม่ควรใช้สั่งซื้อโดยตรง — ให้ใช้จุดสั่งซื้อในหน้า «สต็อก» แทน</div>
    ${overNote}
    <div class="panel"><h3>พยากรณ์ 4 สัปดาห์ข้างหน้า</h3>
      <p class="hint">ช่วงที่ยอดจริงน่าจะอยู่ (จากผลทดสอบย้อนหลัง)
        เครื่องหมาย – หมายถึงข้อมูลไม่พอที่จะให้ช่วงที่เชื่อถือได้<br>
        แม่นยำ ~14% ช่วงปกติ / ~22% ช่วงยอดเปลี่ยนระดับ</p>
      ${table([
        { key: "sku_prefix", label: "รหัส" },
        { key: "group_name", label: "กลุ่มสินค้า" },
        { key: "unit", label: "หน่วย" },
        { key: "forecast_qty_4wk", label: "พยากรณ์ 4 สัปดาห์", num: true, render: r => stopped(r, x => nf(x.forecast_qty_4wk)) },
        { key: "low_qty_4wk", label: "ต่ำสุด", num: true, render: r => isStopped(r) ? `<span class="muted">–</span>` : nf(r.low_qty_4wk) },
        { key: "high_qty_4wk", label: "สูงสุด", num: true, render: r => isStopped(r) ? `<span class="muted">–</span>` : nf(r.high_qty_4wk) },
        {
          key: "is_intermittent", label: "สถานะ", render: r =>
            isStopped(r) ? `<span class="pill bad">หยุดขาย</span>`
              : isTrue(r.range_below_forecast)
              ? `<span class="pill warn">โมเดลมักพยากรณ์สูงเกิน — ควรอ้างอิงค่าต่ำ</span>`
              : isTrue(r.is_intermittent)
              ? `<span class="pill warn">ไม่สม่ำเสมอ</span>` : `<span class="pill good">ใช้สั่งซื้อได้</span>`
        },
      ], f)}</div>
    <div class="panel"><h3>สัญญาณเตือนแนวโน้ม (${flagged.length} จาก ${a.length} กลุ่ม)</h3>
      <p class="hint">เปรียบเทียบค่ามัธยฐานของ 4 สัปดาห์ล่าสุดกับ 8 สัปดาห์ล่าสุด
        ใช้มัธยฐานเพื่อไม่ให้ออร์เดอร์ใหญ่เพียงรายการเดียวมีอิทธิพลเกินจริง<br>
        กลุ่มที่ขายไม่สม่ำเสมอจะแสดงเพียง «ยังขาย / หยุดขาย» เท่านั้น</p>
      ${table([
        { key: "sku_prefix", label: "รหัส" },
        { key: "group_name", label: "กลุ่มสินค้า" },
        // An intermittent group sells in a handful of weeks out of eight, so
        // its "median of the last 4 weeks" is routinely a median of zeros and
        // the percentage is +/-100% or nothing at all. Printing that number
        // invites somebody to act on it. The only honest thing these three
        // groups support is whether they are still selling, so that is all
        // they are given -- the same reasoning as the หยุดขาย suppression.
        { key: "pct_change_median", label: "มัธยฐาน", num: true, render: r => noTrend(r) ? MUTED : signed(r.pct_change_median) },
        { key: "pct_change_mean", label: "ค่าเฉลี่ย", num: true, render: r => noTrend(r) ? MUTED : signed(r.pct_change_mean) },
        {
          key: "direction", label: "ทิศทาง", render: r => {
            // หยุดขาย wins over ยังขาย: 05 is both, and "still selling" on
            // something that has not sold for a month is the worse of the two
            // wrong answers.
            if (isStopped(r)) return `<span class="pill bad">หยุดขาย</span>`;
            if (isTrue(r.is_intermittent)) return `<span class="pill flat">ยังขาย</span>`;
            if (isTrue(r.outlier_driven))
              return `<span class="pill flat" title="ค่าเฉลี่ยเปลี่ยนแต่มัธยฐานไม่เปลี่ยน = ออร์เดอร์ใหญ่รายการเดียว">ออร์เดอร์ใหญ่</span>`;
            const m = { rising: ["good", "เพิ่มขึ้น"], falling: ["bad", "ลดลง"], stable: ["flat", "คงที่"] }[r.direction] || ["flat", r.direction];
            return `<span class="pill ${m[0]}">${esc(m[1])}</span>`;
          }
        },
      ], a.slice().sort((x, y) => (y.alert === true) - (x.alert === true)))}</div>`;
};

const MUTED = '<span class="muted">–</span>';
// An intermittent group sells in a handful of weeks out of eight, so its
// "median of the last 4 weeks" is routinely a median of zeros and the
// percentage comes out +/-100% or nothing at all. Printing that number invites
// somebody to act on it. The only thing these groups honestly support is
// whether they are still selling, so that is all they are given -- the same
// reasoning as the หยุดขาย suppression on the forecast figures.
const noTrend = (r) => isStopped(r) || isTrue(r.is_intermittent);

// The pipeline picks between two windows per group. "full active window" and
// "last 8 weeks" are the column values; they were printed raw at an operator
// who does not read English, next to a number they are meant to order from.
const BASIS_TH = {
  "full active window": "ทั้งช่วงที่ยังขายอยู่",
  "last 8 weeks": "8 สัปดาห์ล่าสุด",
};

P.stock = async (el) => {
  const d = await api("/api/stock");
  const rp = d.reorder_points;
  // Default order was days-since-last-sale, which puts a part that sold 300
  // baht two years ago above one that sold 2m last quarter. Whoever opens this
  // page has limited time to go and look at things; lifetime revenue is the
  // order that spends it best. Sorted here rather than server-side so the
  // company-wide endpoint keeps serving one ordering to everybody.
  const sc = (d.stock_check || []).slice()
    .sort((a, b) => (Number(b.total_revenue_ex_vat) || 0)
                  - (Number(a.total_revenue_ex_vat) || 0));
  const risk = sc.filter(r => r.risk_level === "risk").length;
  const watch = sc.filter(r => r.risk_level === "watch").length;
  el.innerHTML = `
    ${scopeBanner(d.scope)}
    <div class="cards">
      <div class="card"><div class="label">ควรตรวจสอบ (90 วันขึ้นไป)</div><div class="value">${nf(risk)}</div><div class="sub">รายการ</div></div>
      <div class="card"><div class="label">เฝ้าระวัง (60–89 วัน)</div><div class="value">${nf(watch)}</div><div class="sub">รายการ</div></div>
      <div class="card"><div class="label">กลุ่มที่มีจุดสั่งซื้อ</div><div class="value">${nf(rp.length)}</div><div class="sub">กลุ่มที่ขายไม่สม่ำเสมอ</div></div>
    </div>
    <div class="banner">ระบบนี้มีเฉพาะข้อมูล<strong>การขาย</strong> ไม่มีข้อมูลสต็อกคงเหลือ
      รายการด้านล่างคือสินค้าที่<strong>ไม่มีการขาย</strong>มานาน ซึ่งอาจเป็นของค้างสต็อก
      หรืออาจเป็นเพราะของหมดจึงขายไม่ได้ — จึงควร<strong>ไปตรวจสอบ</strong> ไม่ใช่ตัดขายทิ้งทันที</div>
    <div class="panel"><h3>จุดสั่งซื้อ (Reorder point)</h3>
      <p class="hint">ROP = ความต้องการเฉลี่ยช่วงรอของ + ส่วนเผื่อความปลอดภัย (ระดับบริการ 95%)<br>
        <strong>เมื่อสต็อกในโกดังเหลือต่ำกว่าจุดสั่งซื้อ ให้สั่งเพิ่ม</strong></p>
      ${table([
        { key: "sku_prefix", label: "รหัส" },
        { key: "group_name", label: "กลุ่มสินค้า" },
        { key: "unit", label: "หน่วย" },
        { key: "mean_weekly_qty", label: "เฉลี่ย/สัปดาห์", num: true, render: r => nf(r.mean_weekly_qty, 1) },
        { key: "reorder_point", label: "จุดสั่งซื้อ", num: true, render: r => stopped(r, x => `<strong>${nf(x.reorder_point)}</strong>`) },
        { key: "basis_window", label: "ฐานคำนวณ", render: r => esc(BASIS_TH[r.basis_window] || r.basis_window) },
      ], rp, { scroll: false })}</div>
    <div class="panel"><h3>สินค้าที่ควรตรวจสอบ</h3>
      <p class="hint">เรียงตามรายได้สะสมมากไปน้อย — รายการบนสุดคือของที่เคยทำเงินให้มากที่สุด
        จึงคุ้มที่จะไปดูก่อน</p>
      ${table([
        { key: "sku", label: "รหัสสินค้า" },
        { key: "product_name", label: "ชื่อสินค้า" },
        { key: "days_since_last_sale", label: "วันที่ไม่ขาย", num: true },
        { key: "last_sale_date", label: "ขายล่าสุด", render: r => dateTH(r.last_sale_date) },
        { key: "total_qty_sold", label: "ขายสะสม", num: true, render: r => nf(r.total_qty_sold) },
        { key: "unit", label: "หน่วย" },
        { key: "total_revenue_ex_vat", label: "รายได้สะสม", num: true, render: r => baht(r.total_revenue_ex_vat) },
        {
          key: "risk_level", label: "ระดับ", render: r =>
            r.risk_level === "risk" ? `<span class="pill bad">ควรตรวจสอบ</span>` : `<span class="pill warn">เฝ้าระวัง</span>`
        },
      ], sc)}</div>`;
};

// "earlier"/"recent" and "ma8"/"naive" were printed raw. Nobody outside this
// repo knows that "recent" is the window where sales stepped down, which is
// the single fact that explains why the second number is worse.
const SCEN_TH = { earlier: "ช่วงปกติ", recent: "ช่วงยอดเปลี่ยนระดับ" };
const MODEL_TH = {
  ma8: "ค่าเฉลี่ย 8 สัปดาห์ (ที่ใช้จริง)",
  ma4: "ค่าเฉลี่ย 4 สัปดาห์",
  naive: "ใช้สัปดาห์ล่าสุดซ้ำ",
  "ses_a0.3": "ถ่วงน้ำหนักแบบลดหลั่น",
};
const scenTH = (s) => SCEN_TH[s] || esc(s);
const modelTH = (m) => MODEL_TH[m] || esc(m);

// Target band. No annotation plugin is loaded -- the page takes Chart.js from
// a CDN with no build step -- so this draws the rectangle itself, underneath
// the bars, which is all the plugin would have done.
const BAND_LO = 10, BAND_HI = 15;
const bandPlugin = {
  id: "targetBand",
  beforeDatasetsDraw(c) {
    const { ctx, chartArea: area, scales: { y } } = c;
    if (!y) return;
    const top = y.getPixelForValue(BAND_HI), bottom = y.getPixelForValue(BAND_LO);
    ctx.save();
    ctx.fillStyle = "rgba(26,127,55,.10)";
    ctx.fillRect(area.left, top, area.right - area.left, bottom - top);
    ctx.strokeStyle = "rgba(26,127,55,.45)";
    ctx.setLineDash([4, 3]);
    ctx.strokeRect(area.left, top, area.right - area.left, bottom - top);
    ctx.restore();
  },
};

P.accuracy = async (el) => {
  const d = await api("/api/accuracy");
  // Pooled figures come from the API. They are NOT the mean of the per-group
  // WAPEs below: WAPE is a ratio of sums, so averaging the percentages gives
  // a group selling 68 units the same weight as one selling 90,000 and put
  // ma8 at 50.9% instead of its real 14.2%.
  const pooled = d.pooled || [];
  const scen = [...new Set(pooled.map(r => r.scenario))];
  const models = [...new Set(pooled.map(r => r.model))];
  const wape = (s) => pooled.find(p => p.scenario === s && p.model === "ma8")?.wape_4wk_total;

  // The headline in one sentence, from the payload. This page used to open
  // with a chart of four models across two scenarios and leave the reader to
  // work out which number was theirs; only ma8 is in production, so that is
  // the number the sentence quotes.
  const e = wape("earlier"), rc = wape("recent");
  const summary = (e == null || rc == null) ? ""
    // One decimal, not zero. The recent figure is 22.5%, which rounds to 23%
    // and then contradicts the "~22%" printed on the forecast page -- two
    // screens quoting the same backtest at different numbers.
    : `<div class="msg info">โดยรวมแล้วโมเดลที่ใช้จริงพยากรณ์ยอด 4 สัปดาห์
        <b>คลาดเคลื่อนประมาณ ${nf(e, 1)}%</b> ในช่วงปกติ
        และ <b>${nf(rc, 1)}%</b> ในช่วงที่ยอดขายเปลี่ยนระดับ
        — เป้าหมายที่ยอมรับได้คือ ${BAND_LO}–${BAND_HI}%</div>`;

  // One row per group, both periods side by side, ma8 only. The old table was
  // 4 models x 2 scenarios x 15 groups = 120 rows of which 30 mattered, and
  // the reader had to scan for the pair that could be compared.
  const ma8 = (d.model_accuracy || []).filter(r => r.model === "ma8");
  const byGroup = {};
  for (const r of ma8) {
    const g = (byGroup[r.sku_prefix] ||= {
      sku_prefix: r.sku_prefix, group_name: r.group_name });
    g[r.scenario] = r.wape_4wk_total;
  }
  const inBand = (v) => v == null ? MUTED
    : `<span class="pill ${v <= BAND_HI ? "good" : v <= 25 ? "warn" : "bad"}">${nf(v, 1)}%</span>`;
  const rows = Object.values(byGroup)
    .sort((a, b) => (a.recent ?? 999) - (b.recent ?? 999));

  el.innerHTML = `
    ${scopeBanner(d.scope)}
    ${summary}
    <div class="panel"><h3>ความแม่นยำของโมเดล (WAPE ยอดรวม 4 สัปดาห์)</h3>
      <p class="hint">ยิ่งต่ำยิ่งดี · «ช่วงปกติ» คือช่วงที่ยอดขายทรงตัว
        «ช่วงยอดเปลี่ยนระดับ» คือช่วงที่ยอดขายกำลังลดลง
        ค่าที่สูงขึ้นในช่วงหลังสะท้อนการเปลี่ยนระดับของยอดขาย ไม่ใช่ความผันผวนรายสัปดาห์
        ค่านี้ถ่วงน้ำหนักตามปริมาณขายของแต่ละกลุ่ม · แถบเขียวคือเป้าหมาย ${BAND_LO}–${BAND_HI}%</p>
      <div class="chart-wrap"><canvas id="c4"></canvas></div>
      ${table([
        { key: "scenario", label: "ช่วงทดสอบ", render: r => scenTH(r.scenario) },
        { key: "model", label: "โมเดล", render: r => modelTH(r.model) },
        { key: "wape_4wk_total", label: "คลาดเคลื่อน 4 สัปดาห์", num: true, render: r => nf(r.wape_4wk_total, 1) + "%" },
        { key: "wape_weekly", label: "คลาดเคลื่อนรายสัปดาห์", num: true, render: r => nf(r.wape_weekly, 1) + "%" },
        { key: "n_groups", label: "กลุ่ม", num: true },
      ], pooled, { scroll: false })}</div>
    <div class="panel"><h3>รายกลุ่มสินค้า (เฉพาะโมเดลที่ใช้จริง)</h3>
      <p class="hint">อย่านำค่าเหล่านี้มาเฉลี่ยกันเพื่อหาค่ารวม — ใช้ตารางด้านบนแทน
        เรียงจากกลุ่มที่แม่นที่สุดในช่วงยอดเปลี่ยนระดับ</p>
      ${table([
        { key: "sku_prefix", label: "รหัส" },
        { key: "group_name", label: "กลุ่มสินค้า" },
        { key: "earlier", label: "ช่วงปกติ", num: true, render: r => inBand(r.earlier) },
        { key: "recent", label: "ช่วงยอดเปลี่ยนระดับ", num: true, render: r => inBand(r.recent) },
      ], rows, { scroll: false })}</div>`;

  chart(el.querySelector("#c4"), {
    type: "bar",
    plugins: [bandPlugin],
    data: {
      labels: models.map(modelTH),
      datasets: scen.map((s, i) => ({
        label: scenTH(s),
        data: models.map(mo => { const f = pooled.find(p => p.scenario === s && p.model === mo); return f ? f.wape_4wk_total : null; }),
        backgroundColor: i === 0 ? "#1f6feb" : "#d1671f",
      })),
    },
    options: { maintainAspectRatio: false, scales: { y: { beginAtZero: true, ticks: { callback: v => v + "%" } } } },
  });
};

P.customers = async (el) => {
  const d = await api("/api/customers?limit=300");
  const seg = d.segments, top = d.top_customers;
  const totalRev = seg.reduce((a, b) => a + (b.revenue || 0), 0);

  // "Cannot Lose" = bought a lot and often, and has now gone quiet. That is
  // the one segment where a phone call has a clear job, so it gets lifted out
  // of the 300-row table into a list somebody can work through.
  //
  // Filtered from top_customers, which for a rep is already their own book
  // scoped server-side -- so a rep is handed their own customers to call and
  // never learns that a colleague's customer went quiet.
  const callList = top.filter(r => r.segment === "Cannot Lose")
    .slice().sort((a, b) => (Number(b.monetary) || 0) - (Number(a.monetary) || 0));

  el.innerHTML = `
    ${scopeBanner(d.scope)}
    ${callList.length ? `<div class="panel"><h3>ลูกค้าที่ควรติดต่อ</h3>
      <p class="hint">ลูกค้ากลุ่ม «ต้องรักษาไว้» — เคยซื้อมากและซื้อบ่อย แต่หายไปนาน
        เรียงตามรายได้สะสมมากไปน้อย · นับจำนวนวันจากวันสุดท้ายในไฟล์ ไม่ใช่วันนี้</p>
      ${table([
        { key: "customer_code", label: "รหัส" },
        { key: "customer_name", label: "ชื่อลูกค้า" },
        { key: "monetary", label: "รายได้สะสม", num: true, render: r => baht(r.monetary) },
        { key: "frequency", label: "เคยซื้อ (ครั้ง)", num: true },
        { key: "recency_days", label: "หายไปแล้ว (วัน)", num: true, render: r => `<strong>${nf(r.recency_days)}</strong>` },
        { key: "last_purchase", label: "ซื้อล่าสุด", render: r => dateTH(r.last_purchase) },
      ], callList, { scroll: true })}</div>` : ""}
    <div class="panel"><h3>การจัดกลุ่มลูกค้า (RFM)</h3>
      <p class="hint">R = ซื้อล่าสุดเมื่อไร · F = ซื้อบ่อยแค่ไหน · M = ใช้จ่ายเท่าไร
        คะแนนเป็นควินไทล์ตามลำดับ (แต่ละช่วงมีลูกค้าราว 20%)
        นับ «ล่าสุด» จากวันสุดท้ายในไฟล์ ไม่ใช่วันนี้</p>
      <div class="chart-wrap"><canvas id="c5"></canvas></div></div>
    <div class="panel"><h3>สรุปรายกลุ่ม</h3>
      ${table([
        { key: "segment_th", label: "กลุ่ม" },
        { key: "segment", label: "Segment" },
        { key: "n_customers", label: "จำนวนรหัสลูกค้า", num: true, render: r => nf(r.n_customers) },
        { key: "customer_share_pct", label: "สัดส่วนลูกค้า", num: true, render: r => nf(r.customer_share_pct, 1) + "%" },
        { key: "revenue", label: "รายได้", num: true, render: r => baht(r.revenue) },
        { key: "revenue_share_pct", label: "สัดส่วนรายได้", num: true, render: r => nf(r.revenue_share_pct, 1) + "%" },
        { key: "avg_recency_days", label: "ซื้อล่าสุด (วัน)", num: true },
        { key: "avg_frequency", label: "ความถี่", num: true, render: r => nf(r.avg_frequency, 1) },
      ], seg, { scroll: false })}</div>
    <div class="panel"><h3>ลูกค้ารายใหญ่</h3>
      ${table([
        { key: "revenue_rank", label: "อันดับ", num: true },
        { key: "customer_code", label: "รหัส" },
        { key: "customer_name", label: "ชื่อลูกค้า" },
        { key: "segment_th", label: "กลุ่ม" },
        { key: "monetary", label: "รายได้", num: true, render: r => baht(r.monetary) },
        { key: "frequency", label: "ครั้ง", num: true },
        { key: "recency_days", label: "ล่าสุด (วัน)", num: true },
        { key: "cumulative_revenue_pct", label: "สะสม", num: true, render: r => nf(r.cumulative_revenue_pct, 1) + "%" },
      ], top)}</div>`;

  chart(el.querySelector("#c5"), {
    type: "bar",
    data: {
      labels: seg.map(r => r.segment_th),
      datasets: [
        { label: "สัดส่วนรายได้ %", data: seg.map(r => r.revenue / totalRev * 100), backgroundColor: "#1f6feb" },
        { label: "สัดส่วนลูกค้า %", data: seg.map(r => r.customer_share_pct), backgroundColor: "#c9d8f0" },
      ],
    },
    options: { maintainAspectRatio: false, scales: { y: { ticks: { callback: v => v + "%" } } } },
  });
};

// --------------------------------------------------------------- upload
P.upload = async (el) => {
  el.innerHTML = `
    <div class="panel">
      <h3>อัปโหลดไฟล์รายงานจาก Express</h3>
      <p class="hint">ต้องใช้ 3 ไฟล์: <strong>รายงานขายเงินสด</strong>, <strong>รายงานใบกำกับสินค้า (ขายเงินเชื่อ)</strong>
        และ <strong>รายงานใบรับมัดจำ</strong> — ระบบตรวจชนิดไฟล์จากเนื้อหา ไม่ได้ดูจากชื่อไฟล์
        จึงลากไฟล์เข้ามาพร้อมกันได้โดยไม่ต้องเรียงลำดับ</p>
      <div class="drop" id="drop">
        ลากไฟล์มาวางที่นี่ หรือคลิกเพื่อเลือกไฟล์ (.csv)
        <input type="file" id="file" multiple accept=".csv,text/csv" hidden>
      </div>
      <ul class="filelist" id="list"></ul>
      <div id="msg"></div>
      <div class="bar" id="bar" style="display:none"><div></div></div>
      <button id="go" disabled>เริ่มนำเข้าข้อมูล</button>
    </div>
    <div id="result"></div>`;

  const drop = el.querySelector("#drop"), input = el.querySelector("#file");
  const list = el.querySelector("#list"), msg = el.querySelector("#msg");
  const go = el.querySelector("#go"), bar = el.querySelector("#bar");
  const result = el.querySelector("#result");
  let files = [];

  const refresh = () => {
    list.innerHTML = files.map((f, i) =>
      `<li><span>${esc(f.name)} <span class="muted">(${(f.size / 1024).toFixed(0)} KB)</span></span>
       <button class="ghost" data-i="${i}" style="padding:3px 9px;font-size:12px">ลบ</button></li>`).join("");
    list.querySelectorAll("button").forEach(b => b.onclick = () => { files.splice(+b.dataset.i, 1); refresh(); });
    go.disabled = files.length === 0;
    msg.innerHTML = files.length && files.length !== 3
      ? `<div class="msg info">เลือกไว้ ${files.length} ไฟล์ — ต้องใช้ทั้งหมด 3 ไฟล์</div>` : "";
  };

  const add = (fl) => { files = files.concat([...fl]); refresh(); };
  drop.onclick = () => input.click();
  input.onchange = (e) => add(e.target.files);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); add(e.dataTransfer.files); };

  go.onclick = async () => {
    go.disabled = true; result.innerHTML = ""; bar.style.display = "block";
    const fill = bar.firstElementChild;
    msg.innerHTML = `<div class="msg info"><span class="spinner"></span>
      กำลังประมวลผล… ขั้นตอนนี้ใช้เวลาประมาณ 20–60 วินาที กรุณาอย่าปิดหน้านี้</div>`;

    // The server does the parse in one request, so real byte progress only
    // covers the upload. Past that we advance a slow indeterminate bar rather
    // than pretend to know how far the pipeline has got.
    let p = 0;
    const tick = setInterval(() => { p = Math.min(p + 1.4, 95); fill.style.width = p + "%"; }, 400);

    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    try {
      const d = await api("/api/upload", { method: "POST", body: fd });
      clearInterval(tick); fill.style.width = "100%";
      renderResult(result, msg, d);
      if (d.ok) files = [], refresh();
    } catch (err) {
      clearInterval(tick); bar.style.display = "none";
      msg.innerHTML = `<div class="msg err">${esc(err.message)}</div>`;
    } finally { go.disabled = files.length === 0; }
  };
};

// The counts come back keyed in English. Without this map the page printed
// "customers"/"sales_lines" at the operator, which is the one screen that is
// read by someone who does not read the code.
const COUNT_LABELS = {
  cash: "เอกสารขายเงินสด",
  credit: "เอกสารขายเงินเชื่อ",
  deposit: "เอกสารรับมัดจำ",
  sales_lines: "รายการสินค้า",
  customer_codes: "จำนวนรหัสลูกค้า",
  products: "รหัสสินค้า",
  unparsed: "บรรทัดที่อ่านไม่ได้",
};

function renderResult(result, msg, d) {
  if (d.errors_th?.length && !d.checks) {
    msg.innerHTML = d.errors_th.map(e => `<div class="msg err">${esc(e)}</div>`).join("");
    return;
  }
  const allPass = (d.checks || []).every(c => c.passed);
  msg.innerHTML = d.ok && allPass
    ? `<div class="msg ok">นำเข้าข้อมูลสำเร็จใน ${d.seconds} วินาที — ผ่านการตรวจความครบถ้วนทุกข้อ</div>`
    : d.errors_th.map(e => `<div class="msg err">${esc(e)}</div>`).join("");

  result.innerHTML = `
    <div class="panel"><h3>ผลการตรวจสอบ</h3>
      <p class="hint">ตรวจความครบถ้วนของไฟล์ที่อัปโหลด โดยเทียบข้อมูลกับตัวเอง
        จึงใช้ได้กับข้อมูลชุดใหม่ทุกเดือน ไม่ผูกกับยอดของงวดใดงวดหนึ่ง</p>
      ${table([
        { key: "label_th", label: "รายการที่ตรวจ" },
        { key: "value", label: "ค่าที่ได้", render: r => esc(String(r.value ?? "-")) },
        { key: "detail_th", label: "รายละเอียด", render: r => `<span class="muted">${esc(r.detail_th || "")}</span>` },
        { key: "passed", label: "ผล", render: r => r.passed ? `<span class="pill good">ผ่าน</span>` : `<span class="pill bad">ไม่ผ่าน</span>` },
      ], d.checks || [], { scroll: false })}
      <h3 style="margin-top:18px">จำนวนที่นำเข้า</h3>
      <div class="cards" style="margin-top:10px">
        ${Object.entries(d.counts || {}).map(([k, v]) => `<div class="card"><div class="label">${esc(COUNT_LABELS[k] || k)}</div><div class="value">${nf(v)}</div></div>`).join("")}
      </div>
      <h3 style="margin-top:18px">ขั้นตอน</h3>
      <ul class="steps">${(d.steps || []).map(s =>
        `<li><span class="tick">${s.ok ? "✓" : "✕"}</span><span>${esc(s.name_th || s.name)}</span>
         <span class="muted" style="margin-left:auto">${s.seconds}s</span></li>`).join("")}</ul>
    </div>`;
}

// ------------------------------------------------------ 8. users & roles
P.admin = async (el) => {
  const d = await api("/api/admin/users");
  const codes = d.salesperson_codes || [];

  const draw = () => {
    el.innerHTML = `
      <div class="panel"><h3>ผู้ใช้และสิทธิ์</h3>
        <p class="hint">บทบาทกำหนดว่าผู้ใช้เห็นข้อมูลแถวไหนได้บ้าง —
          <b>พนักงานขาย</b> เห็นเฉพาะรหัสของตนเอง ·
          <b>หัวหน้าทีม</b> เห็นรหัสในทีม ·
          <b>ผู้บริหาร</b> เห็นทั้งหมด รวมเอกสารที่ไม่มีรหัสพนักงานขาย</p>
        <div id="amsg"></div>
        ${table([
          { key: "email", label: "อีเมล" },
          { key: "role", label: "บทบาท", render: r => roleSelect(r) },
          { key: "salesperson_code", label: "รหัสพนักงานขาย", render: r => codeSelect(r) },
          { key: "team", label: "ทีม (เฉพาะหัวหน้าทีม)", render: r => teamPicker(r) },
          { key: "save", label: "", render: r =>
              `<button class="ghost save" data-u="${esc(r.user_id)}">บันทึก</button>` },
        ], d.users, { scroll: true })}
      </div>`;
    wire();
  };

  const roleSelect = (r) => `<select class="role" data-u="${esc(r.user_id)}">` +
    [["none", "— ยังไม่กำหนด —"], ["sales", "พนักงานขาย"],
     ["sales_manager", "หัวหน้าทีมขาย"], ["ceo", "ผู้บริหาร"]]
      .map(([v, t]) => `<option value="${v}" ${r.role === v ? "selected" : ""}>${t}</option>`)
      .join("") + "</select>";

  const codeSelect = (r) => `<select class="code" data-u="${esc(r.user_id)}"
      ${r.role === "sales" ? "" : "disabled"}>
      <option value="">—</option>` +
    codes.map(c => `<option value="${esc(c)}" ${r.salesperson_code === c ? "selected" : ""}>${esc(c)}</option>`)
      .join("") + "</select>";

  // Checkboxes rather than a multi-select: a team is normally two or three
  // codes out of five, and a multi-select that loses its selection on a
  // mis-click is how somebody silently ends up managing nobody.
  const teamPicker = (r) => `<span class="teamwrap ${r.role === "sales_manager" ? "" : "off"}"
      data-u="${esc(r.user_id)}">` +
    codes.map(c => `<label class="chk"><input type="checkbox" class="team"
        data-u="${esc(r.user_id)}" value="${esc(c)}"
        ${(r.team || []).includes(c) ? "checked" : ""}
        ${r.role === "sales_manager" ? "" : "disabled"}> ${esc(c)}</label>`).join("") + "</span>";

  const msg = (html) => { el.querySelector("#amsg").innerHTML = html; };

  function wire() {
    // Enable/disable the code and team controls as the role changes, so the
    // form cannot express a combination the API will reject.
    el.querySelectorAll("select.role").forEach(s => s.onchange = () => {
      const u = s.dataset.u, role = s.value;
      const code = el.querySelector(`select.code[data-u="${u}"]`);
      code.disabled = role !== "sales";
      if (role !== "sales") code.value = "";
      el.querySelectorAll(`input.team[data-u="${u}"]`).forEach(c => {
        c.disabled = role !== "sales_manager";
        if (role !== "sales_manager") c.checked = false;
      });
      el.querySelector(`.teamwrap[data-u="${u}"]`)
        .classList.toggle("off", role !== "sales_manager");
    });

    el.querySelectorAll("button.save").forEach(b => b.onclick = async () => {
      const u = b.dataset.u;
      const row = d.users.find(x => x.user_id === u);
      const role = el.querySelector(`select.role[data-u="${u}"]`).value;
      const code = el.querySelector(`select.code[data-u="${u}"]`).value || null;
      const team = [...el.querySelectorAll(`input.team[data-u="${u}"]:checked`)]
        .map(c => c.value);
      if (role === "none") {
        msg(`<div class="msg err">เลือกบทบาทก่อนบันทึก</div>`); return;
      }
      b.disabled = true; b.textContent = "กำลังบันทึก…";
      try {
        await api("/api/admin/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: u, role, salesperson_code: code,
                                 team, email: row?.email }),
        });
        Object.assign(row, { role, salesperson_code: code, team });
        msg(`<div class="msg ok">บันทึกสิทธิ์ของ ${esc(row?.email || u)} แล้ว</div>`);
        // The signed-in user may have just changed their OWN nav.
        if (u === ME.user_id) await loadMe();
      } catch (e) {
        msg(`<div class="msg err">${esc(e.message)}</div>`);
      } finally {
        b.disabled = false; b.textContent = "บันทึก";
      }
    });
  }

  draw();
};

const ROLE_TH = {
  ceo: "ผู้บริหาร (เห็นทั้งบริษัท)",
  sales_manager: "หัวหน้าทีมขาย",
  sales: "พนักงานขาย",
  none: "ยังไม่ได้กำหนดสิทธิ์",
};

// -------------------------------------------------------------- shell
function shell(active, email) {
  const links = PAGES.filter(([k]) => allowed(k));
  const meta = PAGES.find(p => p[0] === active) || PAGES[0];
  const who = ME?.role
    ? `<div class="muted" style="font-size:12px">${esc(ROLE_TH[ME.role] || ME.role)}${
        ME.own_code ? ` · รหัส ${esc(ME.own_code)}` : ""}${
        ME.team?.length ? ` · ทีม ${esc(ME.team.join(", "))}` : ""}</div>`
    : "";
  return $(`<div class="shell">
    <aside class="sidebar">
      <div class="brand"><h1>ระบบวิเคราะห์การขาย</h1><p>หลังคาเหล็ก ฉนวนพียูโฟม</p></div>
      <nav>${links.map(([k, label]) =>
        `<a href="#${k}" class="${k === active ? "active" : ""}">${label}</a>`).join("")}</nav>
      <div class="userbox">
        ${AUTH_ON ? `<div>${esc(email || "")}</div>${who}<button class="ghost" id="out">ออกจากระบบ</button>`
                  : `<div>โหมดไม่ต้องเข้าสู่ระบบ</div>`}
      </div>
    </aside>
    <main class="main">
      <h2 class="page-title">${meta[1]}</h2>
      <p class="page-sub">${meta[2]}</p>
      <div id="page"><div class="panel"><span class="spinner"></span> กำลังโหลด…</div></div>
    </main>
  </div>`);
}

function noRoleView(email) {
  return $(`<div class="login-wrap"><div class="login-card">
    <h1>บัญชียังไม่ได้รับสิทธิ์</h1>
    <p class="sub">${esc(email || "")}</p>
    <div class="msg err">บัญชีนี้เข้าสู่ระบบได้ แต่ยังไม่ได้กำหนดบทบาท
      จึงยังไม่เห็นข้อมูลใด ๆ</div>
    <p class="muted">กรุณาแจ้งผู้ดูแลระบบให้กำหนดบทบาทและรหัสพนักงานขายในหน้า
      «ผู้ใช้และสิทธิ์»</p>
    <button class="ghost" id="out">ออกจากระบบ</button>
  </div></div>`);
}

async function render() {
  clearCharts();
  let session = null;
  if (AUTH_ON) {
    const { data } = await sb.auth.getSession();
    session = data.session;
    if (!session) { root().replaceChildren(loginView()); return; }
  }

  await loadMe();
  // A signed-in account with no role would otherwise land on an overview page
  // full of red error text, which reads as "the site is broken" rather than
  // "you are not set up yet".
  if (AUTH_ON && ME?.role === "none") {
    const v = noRoleView(session?.user?.email);
    root().replaceChildren(v);
    v.querySelector("#out")?.addEventListener("click", signOut);
    return;
  }

  const key = (location.hash.replace("#", "") || "overview");
  const known = PAGES.some(p => p[0] === key);
  // Land on the first page this role can actually open, not always overview.
  const fallback = (PAGES.find(([k]) => allowed(k)) || PAGES[0])[0];
  const active = known ? key : fallback;
  const view = shell(active, session?.user?.email);
  root().replaceChildren(view);
  view.querySelector("#out")?.addEventListener("click", signOut);

  const el = view.querySelector("#page");
  if (!allowed(active)) {
    el.innerHTML = `<div class="msg err">ไม่มีสิทธิ์เข้าถึงหน้านี้</div>
      <p class="muted">บทบาทของคุณคือ «${esc(ROLE_TH[ME.role] || ME.role)}»
        หากต้องการสิทธิ์เพิ่ม กรุณาติดต่อผู้ดูแลระบบ</p>`;
    return;
  }
  try {
    await P[active](el);
  } catch (err) {
    el.innerHTML = `<div class="msg err">${esc(err.message)}</div>
      <p class="muted">หากเพิ่งติดตั้งระบบ ให้ไปที่หน้า «อัปโหลดข้อมูล» เพื่อนำเข้าไฟล์ก่อน</p>`;
  }
}

window.addEventListener("preferenceschange", e => {
  if (e.detail !== "theme") {
    // Keep selected files, in-flight uploads, admin edits and login fields.
    if (["#upload", "#admin"].includes(location.hash) || !document.querySelector(".main")) return;
    render(); return;
  }
  const styles = getComputedStyle(document.documentElement);
  const color = styles.getPropertyValue("--muted").trim();
  const grid = styles.getPropertyValue("--line").trim();
  Chart.defaults.color = color; Chart.defaults.borderColor = grid;
  CHARTS.forEach(c => {
    c.options.plugins.legend.labels.color = color;
    Object.values(c.options.scales).forEach(axis => { axis.ticks.color = color; axis.grid.color = grid; });
    c.update();
  });
});
window.addEventListener("hashchange", render);
if (AUTH_ON) sb.auth.onAuthStateChange((e) => { if (e === "SIGNED_IN" || e === "SIGNED_OUT") render(); });
render();
