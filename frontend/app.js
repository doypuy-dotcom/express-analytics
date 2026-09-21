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
];

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
function chart(canvas, cfg) { CHARTS.push(new Chart(canvas, cfg)); }
function clearCharts() { while (CHARTS.length) CHARTS.pop().destroy(); }

function table(cols, rows, opts = {}) {
  const head = cols.map(c => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map(r => "<tr>" + cols.map(c => {
    const v = c.render ? c.render(r) : esc(r[c.key]);
    return `<td class="${c.num ? "num" : ""}">${v}</td>`;
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

P.overview = async (el) => {
  const d = await api("/api/overview");
  const t = d.totals, k = d.kpi_monthly;
  el.innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">รายได้รวม (ไม่รวม VAT)</div>
        <div class="value">${baht(t.revenue_ex_vat)}</div><div class="sub">${t.months} เดือน</div></div>
      <div class="card"><div class="label">จำนวนเอกสาร</div>
        <div class="value">${nf(t.documents)}</div><div class="sub">ใบขาย</div></div>
      <div class="card"><div class="label">เดือนล่าสุด (${esc(t.latest_month)})</div>
        <div class="value">${baht(t.latest_revenue)}</div>
        <div class="sub">ต่อวันขาย ${signed(t.latest_pct_change_per_selling_day)}</div></div>
    </div>
    <div class="panel"><h3>รายได้รายเดือน</h3>
      <p class="hint">แท่ง = รายได้รวม · เส้น = รายได้ต่อวันที่มีการขาย (ตัดผลของจำนวนวันทำการที่ต่างกันออก)</p>
      <div class="chart-wrap"><canvas id="c1"></canvas></div></div>
    <div class="panel"><h3>ตารางรายเดือน</h3>
      ${table([
        { key: "month", label: "เดือน" },
        { key: "revenue_ex_vat", label: "รายได้", num: true, render: r => baht(r.revenue_ex_vat) },
        { key: "n_documents", label: "เอกสาร", num: true, render: r => nf(r.n_documents) },
        { key: "selling_days", label: "วันขาย", num: true },
        { key: "revenue_per_selling_day", label: "ต่อวันขาย", num: true, render: r => baht(r.revenue_per_selling_day) },
        { key: "n_customers", label: "ลูกค้า", num: true, render: r => nf(r.n_customers) },
        { key: "avg_document_value", label: "เฉลี่ย/ใบ", num: true, render: r => baht(r.avg_document_value) },
        { key: "pct_change_per_selling_day", label: "เปลี่ยนแปลง", num: true, render: r => signed(r.pct_change_per_selling_day) },
      ], k, { scroll: false })}</div>`;

  chart(el.querySelector("#c1"), {
    data: {
      labels: k.map(r => r.month),
      datasets: [
        { type: "bar", label: "รายได้", data: k.map(r => r.revenue_ex_vat), backgroundColor: "#a8c7fa", yAxisID: "y" },
        { type: "line", label: "ต่อวันขาย", data: k.map(r => r.revenue_per_selling_day), borderColor: "#b42318", backgroundColor: "#b42318", yAxisID: "y1", tension: .25 },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: {
        y: { position: "left", ticks: { callback: v => (v / 1e6).toFixed(1) + "M" } },
        y1: { position: "right", grid: { drawOnChartArea: false }, ticks: { callback: v => (v / 1e3).toFixed(0) + "K" } },
      },
    },
  });
};

P.sales = async (el) => {
  const d = await api("/api/sales");
  const g = d.by_group_month, p = d.by_person_month;
  const months = [...new Set(g.map(r => r.month))].sort();
  const byGroup = {};
  g.forEach(r => { (byGroup[r.group_name] ||= {})[r.month] = r.revenue_ex_vat; });
  const totals = Object.entries(byGroup)
    .map(([name, m]) => ({ name, total: Object.values(m).sum ? 0 : Object.values(m).reduce((a, b) => a + (b || 0), 0), m }))
    .sort((a, b) => b.total - a.total);
  const palette = ["#1f6feb", "#1a7f37", "#9a6700", "#b42318", "#6f42c1", "#0a7ea4", "#d1671f", "#6b7889"];

  el.innerHTML = `
    <div class="panel"><h3>รายได้ตามกลุ่มสินค้า</h3>
      <p class="hint">แสดง 8 กลุ่มที่มีรายได้สูงสุด</p>
      <div class="chart-wrap"><canvas id="c2"></canvas></div></div>
    <div class="panel"><h3>สรุปตามกลุ่มสินค้า</h3>
      ${table([
        { key: "name", label: "กลุ่มสินค้า" },
        { key: "total", label: "รายได้รวม", num: true, render: r => baht(r.total) },
        { key: "share", label: "สัดส่วน", num: true, render: r => nf(r.total / totals.reduce((a, b) => a + b.total, 0) * 100, 1) + "%" },
      ], totals)}</div>
    <div class="panel"><h3>พนักงานขาย</h3>
      ${table([
        { key: "salesperson_code", label: "รหัส" },
        { key: "month", label: "เดือน" },
        { key: "revenue_ex_vat", label: "รายได้", num: true, render: r => baht(r.revenue_ex_vat) },
        { key: "n_documents", label: "เอกสาร", num: true, render: r => nf(r.n_documents) },
      ], p)}</div>`;

  chart(el.querySelector("#c2"), {
    type: "bar",
    data: {
      labels: months,
      datasets: totals.slice(0, 8).map((t, i) => ({
        label: t.name, data: months.map(m => t.m[m] || 0), backgroundColor: palette[i % palette.length],
      })),
    },
    options: {
      maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true, ticks: { callback: v => (v / 1e6).toFixed(1) + "M" } } },
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
        labels: rows.map(r => r.week_start),
        datasets: [{ label: name, data: rows.map(r => r.qty), borderColor: "#1f6feb", backgroundColor: "#dbe8ff", fill: true, tension: .2 }],
      },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } } },
    });
  };
  el.querySelector("#g").onchange = (e) => draw(e.target.value);
  draw(sel);
};

P.forecast = async (el) => {
  const d = await api("/api/forecast");
  const f = d.next_4_weeks, a = d.trend_alerts;
  const flagged = a.filter(r => r.alert === true || r.alert === "true");
  el.innerHTML = `
    <div class="banner">กลุ่มสินค้าที่ขายไม่สม่ำเสมอ (intermittent) แสดงเป็นค่าประมาณเท่านั้น
      ไม่ควรใช้สั่งซื้อโดยตรง — ให้ใช้จุดสั่งซื้อในหน้า «สต็อก» แทน</div>
    <div class="panel"><h3>พยากรณ์ 4 สัปดาห์ข้างหน้า</h3>
      <p class="hint">ช่วงค่าคือเปอร์เซ็นไทล์ที่ 10–90 ของอัตราส่วนจริง/พยากรณ์จากการทดสอบย้อนหลัง
        เครื่องหมาย – หมายถึงข้อมูลไม่พอที่จะให้ช่วงที่เชื่อถือได้</p>
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
              : (r.is_intermittent === true || r.is_intermittent === "true")
              ? `<span class="pill warn">ไม่สม่ำเสมอ</span>` : `<span class="pill good">ใช้สั่งซื้อได้</span>`
        },
      ], f)}</div>
    <div class="panel"><h3>สัญญาณเตือนแนวโน้ม (${flagged.length} จาก ${a.length} กลุ่ม)</h3>
      <p class="hint">เปรียบเทียบค่ามัธยฐานของ 4 สัปดาห์ล่าสุดกับ 8 สัปดาห์ล่าสุด
        ใช้มัธยฐานเพื่อไม่ให้ออร์เดอร์ใหญ่เพียงรายการเดียวมีอิทธิพลเกินจริง</p>
      ${table([
        { key: "sku_prefix", label: "รหัส" },
        { key: "group_name", label: "กลุ่มสินค้า" },
        { key: "pct_change_median", label: "มัธยฐาน", num: true, render: r => (r.stopped === true || r.stopped === "true") ? `<span class="pill bad">หยุดขาย</span>` : signed(r.pct_change_median) },
        { key: "pct_change_mean", label: "ค่าเฉลี่ย", num: true, render: r => signed(r.pct_change_mean) },
        {
          key: "direction", label: "ทิศทาง", render: r => {
            if (r.outlier_driven === true || r.outlier_driven === "true")
              return `<span class="pill flat" title="ค่าเฉลี่ยเปลี่ยนแต่มัธยฐานไม่เปลี่ยน = ออร์เดอร์ใหญ่รายการเดียว">ออร์เดอร์ใหญ่</span>`;
            const m = { rising: ["good", "เพิ่มขึ้น"], falling: ["bad", "ลดลง"], stable: ["flat", "คงที่"] }[r.direction] || ["flat", r.direction];
            return `<span class="pill ${m[0]}">${esc(m[1])}</span>`;
          }
        },
      ], a.slice().sort((x, y) => (y.alert === true) - (x.alert === true)))}</div>`;
};

P.stock = async (el) => {
  const d = await api("/api/stock");
  const rp = d.reorder_points, sc = d.stock_check;
  const risk = sc.filter(r => r.risk_level === "risk").length;
  const watch = sc.filter(r => r.risk_level === "watch").length;
  el.innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">ควรตรวจสอบ (90 วันขึ้นไป)</div><div class="value">${nf(risk)}</div><div class="sub">รายการ</div></div>
      <div class="card"><div class="label">เฝ้าระวัง (60–89 วัน)</div><div class="value">${nf(watch)}</div><div class="sub">รายการ</div></div>
      <div class="card"><div class="label">กลุ่มที่มีจุดสั่งซื้อ</div><div class="value">${nf(rp.length)}</div><div class="sub">กลุ่มที่ขายไม่สม่ำเสมอ</div></div>
    </div>
    <div class="banner">ระบบนี้มีเฉพาะข้อมูล<strong>การขาย</strong> ไม่มีข้อมูลสต็อกคงเหลือ
      รายการด้านล่างคือสินค้าที่<strong>ไม่มีการขาย</strong>มานาน ซึ่งอาจเป็นของค้างสต็อก
      หรืออาจเป็นเพราะของหมดจึงขายไม่ได้ — จึงควร<strong>ไปตรวจสอบ</strong> ไม่ใช่ตัดขายทิ้งทันที</div>
    <div class="panel"><h3>จุดสั่งซื้อ (Reorder point)</h3>
      <p class="hint">ROP = ความต้องการเฉลี่ยช่วงรอของ + ส่วนเผื่อความปลอดภัย (ระดับบริการ 95%)</p>
      ${table([
        { key: "sku_prefix", label: "รหัส" },
        { key: "group_name", label: "กลุ่มสินค้า" },
        { key: "unit", label: "หน่วย" },
        { key: "mean_weekly_qty", label: "เฉลี่ย/สัปดาห์", num: true, render: r => nf(r.mean_weekly_qty, 1) },
        { key: "reorder_point", label: "จุดสั่งซื้อ", num: true, render: r => stopped(r, x => `<strong>${nf(x.reorder_point)}</strong>`) },
        { key: "basis_window", label: "ฐานคำนวณ" },
      ], rp, { scroll: false })}</div>
    <div class="panel"><h3>สินค้าที่ควรตรวจสอบ</h3>
      ${table([
        { key: "sku", label: "รหัสสินค้า" },
        { key: "product_name", label: "ชื่อสินค้า" },
        { key: "days_since_last_sale", label: "วันที่ไม่ขาย", num: true },
        { key: "last_sale_date", label: "ขายล่าสุด" },
        { key: "total_qty_sold", label: "ขายสะสม", num: true, render: r => nf(r.total_qty_sold) },
        { key: "total_revenue_ex_vat", label: "รายได้สะสม", num: true, render: r => baht(r.total_revenue_ex_vat) },
        {
          key: "risk_level", label: "ระดับ", render: r =>
            r.risk_level === "risk" ? `<span class="pill bad">ควรตรวจสอบ</span>` : `<span class="pill warn">เฝ้าระวัง</span>`
        },
      ], sc)}</div>`;
};

P.accuracy = async (el) => {
  const d = await api("/api/accuracy");
  const m = d.model_accuracy;
  // Pooled figures come from the API. They are NOT the mean of the per-group
  // WAPEs below: WAPE is a ratio of sums, so averaging the percentages gives
  // a group selling 68 units the same weight as one selling 90,000 and put
  // ma8 at 50.9% instead of its real 14.2%.
  const pooled = d.pooled || [];
  const scen = [...new Set(pooled.map(r => r.scenario))];
  const models = [...new Set(pooled.map(r => r.model))];
  el.innerHTML = `
    <div class="panel"><h3>ความแม่นยำของโมเดล (WAPE ยอดรวม 4 สัปดาห์)</h3>
      <p class="hint">ยิ่งต่ำยิ่งดี · «earlier» คือช่วงปกติ «recent» คือช่วงที่ยอดขายกำลังลดลง
        ค่าที่สูงขึ้นในช่วงหลังสะท้อนการเปลี่ยนระดับของยอดขาย ไม่ใช่ความผันผวนรายสัปดาห์
        ค่านี้ถ่วงน้ำหนักตามปริมาณขายของแต่ละกลุ่ม</p>
      <div class="chart-wrap"><canvas id="c4"></canvas></div>
      ${table([
        { key: "scenario", label: "ช่วงทดสอบ" },
        { key: "model", label: "โมเดล" },
        { key: "wape_4wk_total", label: "WAPE 4 สัปดาห์", num: true, render: r => nf(r.wape_4wk_total, 1) + "%" },
        { key: "wape_weekly", label: "WAPE รายสัปดาห์", num: true, render: r => nf(r.wape_weekly, 1) + "%" },
        { key: "n_groups", label: "กลุ่ม", num: true },
      ], pooled, { scroll: false })}</div>
    <div class="panel"><h3>รายละเอียดรายกลุ่ม</h3>
      <p class="hint">อย่านำค่าเหล่านี้มาเฉลี่ยกันเพื่อหาค่ารวม — ใช้ตารางด้านบนแทน</p>
      ${table([
        { key: "scenario", label: "ช่วงทดสอบ" },
        { key: "model", label: "โมเดล" },
        { key: "sku_prefix", label: "รหัส" },
        { key: "group_name", label: "กลุ่มสินค้า" },
        { key: "wape_4wk_total", label: "WAPE 4 สัปดาห์", num: true, render: r => r.wape_4wk_total == null ? "–" : nf(r.wape_4wk_total, 1) + "%" },
      ], m)}</div>`;

  chart(el.querySelector("#c4"), {
    type: "bar",
    data: {
      labels: models,
      datasets: scen.map((s, i) => ({
        label: s,
        data: models.map(mo => { const f = pooled.find(p => p.scenario === s && p.model === mo); return f ? f.wape_4wk_total : null; }),
        backgroundColor: i === 0 ? "#1f6feb" : "#d1671f",
      })),
    },
    options: { maintainAspectRatio: false, scales: { y: { ticks: { callback: v => v + "%" } } } },
  });
};

P.customers = async (el) => {
  const d = await api("/api/customers?limit=300");
  const seg = d.segments, top = d.top_customers;
  const totalRev = seg.reduce((a, b) => a + (b.revenue || 0), 0);
  el.innerHTML = `
    <div class="panel"><h3>การจัดกลุ่มลูกค้า (RFM)</h3>
      <p class="hint">R = ซื้อล่าสุดเมื่อไร · F = ซื้อบ่อยแค่ไหน · M = ใช้จ่ายเท่าไร
        คะแนนเป็นควินไทล์ตามลำดับ (แต่ละช่วงมีลูกค้าราว 20%)
        นับ «ล่าสุด» จากวันสุดท้ายในไฟล์ ไม่ใช่วันนี้</p>
      <div class="chart-wrap"><canvas id="c5"></canvas></div></div>
    <div class="panel"><h3>สรุปรายกลุ่ม</h3>
      ${table([
        { key: "segment_th", label: "กลุ่ม" },
        { key: "segment", label: "Segment" },
        { key: "n_customers", label: "ลูกค้า", num: true, render: r => nf(r.n_customers) },
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

function renderResult(result, msg, d) {
  if (d.errors_th?.length && !d.checks) {
    msg.innerHTML = d.errors_th.map(e => `<div class="msg err">${esc(e)}</div>`).join("");
    return;
  }
  const allPass = (d.checks || []).every(c => c.passed);
  msg.innerHTML = d.ok && allPass
    ? `<div class="msg ok">นำเข้าข้อมูลสำเร็จใน ${d.seconds} วินาที — ตัวเลขตรวจสอบตรงกับค่าอ้างอิงทั้งหมด</div>`
    : d.errors_th.map(e => `<div class="msg err">${esc(e)}</div>`).join("");

  result.innerHTML = `
    <div class="panel"><h3>ผลการตรวจสอบ</h3>
      <p class="hint">ค่าเหล่านี้ถูกตรวจทุกครั้งที่นำเข้า หากไม่ตรงแปลว่าข้อมูลหรือโปรแกรมมีบางอย่างเปลี่ยนไป</p>
      ${table([
        { key: "label_th", label: "รายการ" },
        { key: "actual", label: "ค่าที่ได้", num: true, render: r => nf(r.actual, r.check === "revenue_ex_vat" ? 2 : 0) },
        { key: "expected", label: "ค่าอ้างอิง", num: true, render: r => nf(r.expected, r.check === "revenue_ex_vat" ? 2 : 0) },
        { key: "passed", label: "ผล", render: r => r.passed ? `<span class="pill good">ตรงกัน</span>` : `<span class="pill bad">ไม่ตรง</span>` },
      ], d.checks || [], { scroll: false })}
      <h3 style="margin-top:18px">จำนวนที่นำเข้า</h3>
      <div class="cards" style="margin-top:10px">
        ${Object.entries(d.counts || {}).map(([k, v]) => `<div class="card"><div class="label">${esc(k)}</div><div class="value">${nf(v)}</div></div>`).join("")}
      </div>
      <h3 style="margin-top:18px">ขั้นตอน</h3>
      <ul class="steps">${(d.steps || []).map(s =>
        `<li><span class="tick">${s.ok ? "✓" : "✕"}</span><span>${esc(s.name_th || s.name)}</span>
         <span class="muted" style="margin-left:auto">${s.seconds}s</span></li>`).join("")}</ul>
    </div>`;
}

// -------------------------------------------------------------- shell
function shell(active, email) {
  return $(`<div class="shell">
    <aside class="sidebar">
      <div class="brand"><h1>ระบบวิเคราะห์การขาย</h1><p>หลังคาเหล็ก ฉนวนพียูโฟม</p></div>
      <nav>${PAGES.map(([k, label]) =>
        `<a href="#${k}" class="${k === active ? "active" : ""}">${label}</a>`).join("")}</nav>
      <div class="userbox">
        ${AUTH_ON ? `<div>${esc(email || "")}</div><button class="ghost" id="out">ออกจากระบบ</button>`
                  : `<div>โหมดไม่ต้องเข้าสู่ระบบ</div>`}
      </div>
    </aside>
    <main class="main">
      <h2 class="page-title">${PAGES.find(p => p[0] === active)[1]}</h2>
      <p class="page-sub">${PAGES.find(p => p[0] === active)[2]}</p>
      <div id="page"><div class="panel"><span class="spinner"></span> กำลังโหลด…</div></div>
    </main>
  </div>`);
}

async function render() {
  clearCharts();
  let session = null;
  if (AUTH_ON) {
    const { data } = await sb.auth.getSession();
    session = data.session;
    if (!session) { root().replaceChildren(loginView()); return; }
  }

  const key = (location.hash.replace("#", "") || "overview");
  const active = PAGES.some(p => p[0] === key) ? key : "overview";
  const view = shell(active, session?.user?.email);
  root().replaceChildren(view);
  view.querySelector("#out")?.addEventListener("click", signOut);

  const el = view.querySelector("#page");
  try {
    await P[active](el);
  } catch (err) {
    el.innerHTML = `<div class="msg err">${esc(err.message)}</div>
      <p class="muted">หากเพิ่งติดตั้งระบบ ให้ไปที่หน้า «อัปโหลดข้อมูล» เพื่อนำเข้าไฟล์ก่อน</p>`;
  }
}

window.addEventListener("hashchange", render);
if (AUTH_ON) sb.auth.onAuthStateChange((e) => { if (e === "SIGNED_IN" || e === "SIGNED_OUT") render(); });
render();
