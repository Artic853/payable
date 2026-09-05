"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const rupees = (paise) =>
  "₹" + (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

let TASKS = [];

/* ---------------------------------------------------------------- presets */

function describeConstraint(c) {
  if (c.op === "true" || c.op === "false") return `${c.field} is ${c.op}`;
  return `${c.field} ${c.op} ${JSON.stringify(c.value)}`;
}

function renderChips(constraints) {
  const box = $("#chips");
  box.innerHTML = "";
  if (!constraints.length) {
    box.appendChild(el("span", "chip", "none — the agent must work from the brief alone"));
    return;
  }
  constraints.forEach((c) => {
    const hard = c.hard !== false;
    box.appendChild(el("span", "chip " + (hard ? "hard" : ""), describeConstraint(c)));
  });
}

function applyPreset(idx) {
  const t = TASKS[idx];
  if (!t) return;
  $("#brief").value = t.brief;
  $("#budget").value = Math.round((t.budget_paise || 1000000) / 100);
  renderChips(t.constraints || []);
}

async function loadTasks() {
  const data = await getJSON("/api/console/tasks");
  TASKS = data.tasks;
  const sel = $("#preset");
  sel.innerHTML = "";
  TASKS.forEach((t, i) => {
    const trap = !t.expected_sku;
    const opt = el("option", null, `${trap ? "⚠ " : ""}${t.task_id}`);
    opt.value = String(i);
    sel.appendChild(opt);
  });
  sel.addEventListener("change", () => applyPreset(Number(sel.value)));
  applyPreset(0);
}

/* ------------------------------------------------------------------- run */

function currentArm() {
  const checked = document.querySelector('input[name="arm"]:checked');
  return checked ? checked.value : "payable";
}

function renderStages(stages) {
  const order = ["discover", "select", "quote", "order", "pay"];
  const vals = order.map((k) => stages[k + "_ms"] || 0);
  const max = Math.max(...vals, 1);
  const box = el("div", "stages");
  order.forEach((k, i) => {
    const row = el("div", "stage");
    row.appendChild(el("span", "nm", k));
    const bar = el("span", "bar");
    const fill = el("i");
    fill.style.width = Math.max(2, (vals[i] / max) * 100) + "%";
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("span", "ms", vals[i].toFixed(0) + " ms"));
    box.appendChild(row);
  });
  return box;
}

function renderTrail(events) {
  const box = el("div", "trail");
  events.forEach((e) => {
    const ev = el("div", "ev " + e.actor);
    const hd = el("div", "hd");
    hd.appendChild(el("span", "actor", e.actor.replace("_", " ")));
    hd.appendChild(el("span", "step", e.step.replace(/_/g, " ")));
    if (e.decision) hd.appendChild(el("span", "dec", e.decision));
    ev.appendChild(hd);
    if (e.rationale) ev.appendChild(el("div", "why", e.rationale));
    box.appendChild(ev);
  });
  return box;
}

function kv(pairs) {
  const box = el("div", "kv");
  pairs.forEach(([k, v]) => {
    if (v === null || v === undefined || v === "") return;
    const cell = el("div");
    cell.appendChild(el("div", "k", k));
    cell.appendChild(el("div", "v", String(v)));
    box.appendChild(cell);
  });
  return box;
}

const VERDICT_ICON = { correct: "✓", wrong: "✗", missed: "–", unscored: "·" };

function renderRun(data) {
  const out = $("#out");
  out.innerHTML = "";
  const r = data.result;

  const v = el("div", "verdict " + data.verdict.grade);
  v.appendChild(el("span", "icon", VERDICT_ICON[data.verdict.grade] || "·"));
  v.appendChild(el("span", null, data.verdict.label));
  out.appendChild(v);

  out.appendChild(
    kv([
      ["arm", data.arm],
      ["outcome", r.outcome],
      ["failure code", r.failure_code === "none" ? "" : r.failure_code],
      ["amount", r.amount_paise ? rupees(r.amount_paise) : ""],
      ["order", r.order_id || ""],
      ["payment", r.payment_id || ""],
      ["http calls", r.http_calls],
      ["payment attempts", `${r.payment_attempts} (${r.payment_declines} declined)`],
      ["wall time", r.latency_ms.toFixed(0) + " ms"],
    ])
  );

  if (r.abstain_reason) {
    const note = el("div", "note");
    note.appendChild(el("b", null, "Why it stopped: "));
    note.appendChild(document.createTextNode(r.abstain_reason));
    out.appendChild(note);
  }

  (r.notes || []).forEach((n) => {
    const note = el("div", "note");
    note.appendChild(el("b", null, "Fallback: "));
    note.appendChild(document.createTextNode(n));
    out.appendChild(note);
  });

  if (data.mandate) {
    const m = data.mandate;
    const h = el("h3", null, "Mandate presented to the merchant");
    h.style.marginTop = "22px";
    out.appendChild(h);
    out.appendChild(
      kv([
        ["algorithm", m.alg],
        ["principal", m.principal],
        ["cap", rupees(m.max_amount_paise)],
        ["scope", (m.allowed_categories || []).join(", ") || "any category"],
        ["signature", (m.signature || "").slice(0, 24) + "…"],
        ["merchant holds", m.public_key ? m.public_key.slice(0, 24) + "… (public key)" : "—"],
      ])
    );
  }

  const sh = el("h3", null, "Stage latency");
  sh.style.marginTop = "22px";
  out.appendChild(sh);
  out.appendChild(renderStages(r.stages));

  const th = el("h3", null, `Audit trail — ${data.events.length} events, run ${r.run_id}`);
  th.style.marginTop = "22px";
  out.appendChild(th);
  out.appendChild(renderTrail(data.events));
}

async function run() {
  const btn = $("#run");
  const idx = Number($("#preset").value);
  const task = TASKS[idx] || {};
  const out = $("#out");

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>Running…';
  out.innerHTML = '<div class="empty">The agent is working…</div>';

  const briefChanged = $("#brief").value.trim() !== (task.brief || "").trim();

  try {
    const data = await getJSON("/api/console/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: task.task_id,
        brief: $("#brief").value,
        category: task.category || null,
        // A hand-edited brief no longer matches the preset's constraints or its
        // answer key, so we drop both and report the run instead of grading it.
        constraints: briefChanged ? [] : task.constraints || [],
        quantity: task.quantity || 1,
        budget_paise: Math.round(Number($("#budget").value || 0) * 100),
        expected_sku: briefChanged ? null : task.expected_sku || null,
        // A trap task's ground truth is "do not buy", so `expected_sku: null`
        // cannot itself signal "ungraded". Say it explicitly.
        graded: !briefChanged,
        arm: currentArm(),
      }),
    });
    renderRun(data);
  } catch (err) {
    out.innerHTML = "";
    out.appendChild(el("div", "err", "Run failed: " + err.message));
  } finally {
    btn.disabled = false;
    btn.textContent = "Run the agent";
  }
}

/* ------------------------------------------------------------- benchmark */

function pct(x) {
  return x.toFixed(1) + "%";
}

function renderHeroMetrics(bench) {
  const rows = bench.aggregate || [];
  const byArm = {};
  rows.forEach((r) => (byArm[r.arm] = r));
  const p = byArm["payable"];
  const lo = byArm["legacy-optimistic"];
  const ls = byArm["legacy-strict"];
  if (!p || !lo || !ls) return;

  const box = $("#hero-metrics");
  box.innerHTML = "";

  const add = (cls, k, v, s) => {
    const m = el("div", "metric " + cls);
    m.appendChild(el("div", "k", k));
    m.appendChild(el("div", "v", v));
    m.appendChild(el("div", "s", s));
    box.appendChild(m);
  };

  add("good", "payable — wrong-item rate", pct(p.wrong_item_rate_pct.mean),
      `sd ${p.wrong_item_rate_pct.stdev.toFixed(1)} across ${p.seeds} seeds`);
  add("", "payable — txn success", pct(p.transaction_success_pct.mean),
      `${p.transaction_success_pct.min}–${p.transaction_success_pct.max} range`);
  add("bad", "HTML storefront, confident buyer", pct(lo.wrong_item_rate_pct.mean),
      "of its purchases were wrong");
  add("bad", "money misspent", rupees(lo.misspent_paise.mean),
      `vs ${rupees(p.misspent_paise.mean)} on payable`);
}

function bar(value, max, kind) {
  const wrap = el("span", "mini");
  const fill = el("i", kind);
  fill.style.width = Math.max(1, (value / max) * 100) + "%";
  wrap.appendChild(fill);
  return wrap;
}

function renderBenchmark(bench) {
  const host = $("#bench");
  host.innerHTML = "";

  const seeds = bench.seeds || [];
  const intro = el("p", "sub",
    `${bench.task_count} tasks × ${seeds.length} seed${seeds.length === 1 ? "" : "s"} ` +
    `(${seeds[0]}–${seeds[seeds.length - 1]}), ` +
    `${(bench.payment_failure_rate * 100).toFixed(0)}% injected decline rate.`);
  host.appendChild(intro);

  // --- headline table
  const wrap = el("div", "tbl-wrap");
  const t = el("table");
  const thead = el("thead");
  const hr = el("tr");
  ["Arm", "Txn success", "", "Wrong-item rate", "", "Decision accuracy", "Money misspent"]
    .forEach((h, i) => {
      const th = el("th", i === 6 ? "num" : null, h);
      hr.appendChild(th);
    });
  thead.appendChild(hr);
  t.appendChild(thead);

  const tb = el("tbody");
  (bench.aggregate || []).forEach((r) => {
    const tr = el("tr", r.arm === "payable" ? "highlight" : null);
    tr.appendChild(el("td", null, r.arm));

    const ts = r.transaction_success_pct;
    tr.appendChild(el("td", "num", pct(ts.mean)));
    const c1 = el("td");
    c1.appendChild(bar(ts.mean, 100, "a"));
    c1.appendChild(el("div", "spread", `sd ${ts.stdev.toFixed(1)} · ${ts.min}–${ts.max}`));
    tr.appendChild(c1);

    const wi = r.wrong_item_rate_pct;
    tr.appendChild(el("td", "num", pct(wi.mean)));
    const c2 = el("td");
    c2.appendChild(bar(wi.mean, 100, wi.mean === 0 ? "g" : "b"));
    c2.appendChild(el("div", "spread", `sd ${wi.stdev.toFixed(1)} · ${wi.min}–${wi.max}`));
    tr.appendChild(c2);

    tr.appendChild(el("td", "num", pct(r.decision_accuracy_pct.mean)));
    tr.appendChild(el("td", "num", rupees(r.misspent_paise.mean)));
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  host.appendChild(wrap);

  const note = el("div", "note");
  note.appendChild(el("b", null, "Read the two legacy rows together. "));
  note.appendChild(document.createTextNode(
    "On the HTML storefront, going from cautious to confident buys about 30 points of " +
    "transaction success and costs about 26 points of wrong-item rate. Decision accuracy " +
    "barely moves, because the extra sales and the extra mistakes nearly cancel. The " +
    "transactability layer is what removes that trade."
  ));
  host.appendChild(note);

  // --- per-task matrix
  const arms = Object.keys(bench.runs || {});
  if (!arms.length) return;

  const h3 = el("h3", null, `Per-task outcomes (seed ${seeds[0]})`);
  h3.style.margin = "30px 0 12px";
  host.appendChild(h3);

  const byTask = {};
  arms.forEach((arm) => {
    (bench.runs[arm] || []).forEach((r) => {
      byTask[r.task_id] = byTask[r.task_id] || {};
      byTask[r.task_id][arm] = r;
    });
  });

  const mw = el("div", "tbl-wrap");
  const mt = el("table", "matrix");
  const mh = el("thead");
  const mhr = el("tr");
  mhr.appendChild(el("th", null, "Task"));
  arms.forEach((a) => mhr.appendChild(el("th", null, a)));
  mh.appendChild(mhr);
  mt.appendChild(mh);

  const mb = el("tbody");
  Object.keys(byTask).forEach((taskId) => {
    const tr = el("tr");
    const nameCell = el("td", "cell", taskId);
    tr.appendChild(nameCell);
    arms.forEach((arm) => {
      const r = byTask[taskId][arm];
      if (!r) { tr.appendChild(el("td", "cell ab", "—")); return; }
      let cls, txt;
      if (r.outcome === "purchased") {
        const ok = r.purchased_sku === r.expected_sku;
        cls = ok ? "ok" : "no";
        txt = (ok ? "✓ " : "✗ ") + r.purchased_sku;
      } else if (r.expected_sku === null) {
        cls = "ok";
        txt = "✓ refused";
      } else {
        cls = "ab";
        txt = "– " + r.failure_code;
      }
      tr.appendChild(el("td", "cell " + cls, txt));
    });
    mb.appendChild(tr);
  });
  mt.appendChild(mb);
  mw.appendChild(mt);
  host.appendChild(mw);

  const lg = el("div", "legend");
  lg.appendChild(el("span", "ok", "correct purchase or correct refusal"));
  lg.appendChild(el("span", "no", "bought the wrong thing"));
  lg.appendChild(el("span", "ab", "missed a sale it should have made"));
  host.appendChild(lg);
}

/* --------------------------------------------------------------- catalog */

const SPEC_LIMIT = 5;

function renderCatalog(data) {
  const cards = $("#cards");
  const filters = $("#filters");
  const cats = ["all", ...Array.from(new Set(data.products.map((p) => p.category))).sort()];

  const draw = (cat) => {
    cards.innerHTML = "";
    data.products
      .filter((p) => cat === "all" || p.category === cat)
      .forEach((p) => {
        const c = el("div", "card");
        c.appendChild(el("div", "sku", p.sku));
        c.appendChild(el("div", "nm", p.name));
        const pr = el("div", "pr", rupees(p.price_paise));
        const s = el("s", null, rupees(p.mrp_paise));
        pr.appendChild(s);
        c.appendChild(pr);
        c.appendChild(el("div", "st" + (p.stock ? "" : " out"),
          p.stock ? `${p.stock} in stock` : "Out of stock"));
        const specs = el("div", "specs");
        Object.entries(p.specs).slice(0, SPEC_LIMIT).forEach(([k, v]) => {
          const row = el("div");
          row.appendChild(el("b", null, k));
          row.appendChild(el("span", null, Array.isArray(v) ? v.join(", ") : String(v)));
          specs.appendChild(row);
        });
        c.appendChild(specs);
        cards.appendChild(c);
      });
  };

  filters.innerHTML = "";
  cats.forEach((cat, i) => {
    const b = el("button", i === 0 ? "on" : null, cat);
    b.addEventListener("click", () => {
      filters.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      draw(cat);
    });
    filters.appendChild(b);
  });
  draw("all");
}

/* ------------------------------------------------------------------ init */

function wireArms() {
  document.querySelectorAll(".arm-opt").forEach((opt) => {
    opt.addEventListener("click", () => {
      document.querySelectorAll(".arm-opt").forEach((o) => o.classList.remove("sel"));
      opt.classList.add("sel");
    });
  });
}

async function init() {
  wireArms();
  $("#run").addEventListener("click", run);

  try { await loadTasks(); } catch (e) { console.error("tasks", e); }

  try {
    const bench = await getJSON("/api/console/benchmark");
    renderHeroMetrics(bench);
    renderBenchmark(bench);
  } catch (e) {
    $("#bench").innerHTML = "";
    $("#bench").appendChild(el("div", "err",
      "No benchmark results yet. Run: python -m payable.bench.runner --repeats 5 --digest-out data/benchmark-digest.json"));
    $("#hero-metrics").innerHTML = "";
  }

  try {
    const cat = await getJSON("/api/console/catalog");
    renderCatalog(cat);
  } catch (e) { console.error("catalog", e); }

  try {
    const h = await getJSON("/health");
    $("#cfg").textContent = Object.entries(h.config).map(([k, v]) => `${k}: ${v}`).join("  ·  ");
  } catch (e) { /* non-essential */ }
}

init();
