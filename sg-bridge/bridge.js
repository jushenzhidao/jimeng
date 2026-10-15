#!/usr/bin/env node
/**
 * sg-bridge —— 国际站（Dreamina SG）写链路的浏览器代签桥。
 *
 * 背景（2026-10-15 实测矩阵，见仓库 .workbuddy/memory/2026-10-15.md）：
 * shark 风控校验浏览器运行时会话 —— 任何纯 HTTP 组合（完整实时 cookie /
 * curl_cffi chrome150 / accelerate 预检）打 `aigc_draft/generate` 一律
 * `ret=-6 shark not pass`；浏览器页面上下文的 fetch 天然通过。
 * 本服务常驻一个已登录的 headless Chromium（user-data-dir 持久化），
 * jimeng-service 的 SG **写请求**转发到页面内 fetch 执行；**读请求**
 * （能力表/历史/上传 token）不受 shark 影响，仍由 jimeng-service 直连。
 *
 * API：
 *   GET  /health          → {ok, loggedIn, url}（页面存活 + 登录态）
 *   POST /generate        → body 原样作为 generate 的 JSON body，页面内
 *                           fetch 同一端点；返回上游 JSON（原样透传）
 *   POST /fetch           → {url, method?, body?} 通用页面内 fetch（超分腿
 *                           / 后编辑等其它写端点复用同一通路）
 *   GET  /login/qr.png    → 未登录时截当前页面（二维码）给运维扫码
 *
 * 运维：user-data-dir 挂 volume（/data/profile）⇒ 扫一次码长期有效
 *（sessionid 有效期 60 天）；页面崩溃自动重开；请求串行（页面单实例）。
 */
const { chromium } = require("playwright");
const express = require("express");

const START_URL = process.env.SG_START_URL || "https://dreamina.capcut.com/ai-tool/image";
const PROFILE_DIR = process.env.SG_PROFILE_DIR || "/data/profile";
const LISTEN = Number(process.env.SG_LISTEN || 8300);
const NAV_TIMEOUT = Number(process.env.SG_NAV_TIMEOUT_MS || 60000);

let ctx = null;
let page = null;
let starting = null;

async function ensureLogin() {
  const p = await ensurePage();
  const s = await loggedIn(p);
  if (s.onLogin) {
    try {
      const file = require("fs").readFileSync(PROFILE_DIR + "/cookie.txt", "utf8");
      const cookies = file.split(";").map((pair) => {
        const i = pair.indexOf("=");
        return { name: pair.slice(0, i).trim(), value: pair.slice(i + 1).trim(),
                 domain: ".capcut.com", path: "/",
                 expires: Math.floor(Date.now() / 1000) + 90 * 24 * 3600 };
      }).filter((c) => c.name);
      if (cookies.length) {
        await p.context().addCookies(cookies);
        await p.reload({ waitUntil: "domcontentloaded",
                         timeout: NAV_TIMEOUT }).catch(() => {});
        const s2 = await loggedIn(p);
        console.log("[sg-bridge] cookie restore:", s2.onLogin ? "still out" : "ok");
        return { p, loggedIn: !s2.onLogin };
      }
    } catch (e) { /* 无 cookie.txt */ }
  }
  return { p, loggedIn: !s.onLogin };
}

async function ensurePage() {
  if (page && !page.isClosed()) return page;
  if (!starting) {
    starting = (async () => {
      ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
        headless: true,
        args: ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        viewport: { width: 1600, height: 1000 },
        locale: "zh-CN",
      });
      page = ctx.pages()[0] || (await ctx.newPage());
      await page.goto(START_URL, { waitUntil: "domcontentloaded",
                                  timeout: NAV_TIMEOUT }).catch(() => {});
      return page;
    })();
    starting.catch(() => { starting = null; });
  }
  return starting;
}

function loggedIn(page_) {
  return page_.evaluate(() => {
    // 登录判定：存在 sessionid cookie（httpOnly 不可见）⇒ 用业务信号：
    // 已登录页面会出现 workspace/feed；未登录跳登录页。
    const text = document.body ? document.body.innerText.slice(0, 2000) : "";
    const onLogin = /登入|登錄|log\s?in|sign in/i.test(location.href) ||
                    /sign in with/i.test(text);
    return { url: location.href, onLogin };
  });
}

async function pageFetch(url, options) {
  const p = await ensurePage();
  return p.evaluate(async ({ url, options }) => {
    const r = await fetch(url, Object.assign({ credentials: "include" }, options));
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) { /* 原样返回 text */ }
    return { status: r.status, json, text: json ? undefined : text.slice(0, 4000) };
  }, { url, options });
}

const app = express();
app.use(express.json({ limit: "4mb" }));

// 异步 handle 注册表：jimeng-service 派发线程不能等 UI 流程（20s+），
// 立即返回 handle，UI 流程后台跑，结果在 /handle/{id} 查。
const HANDLES = new Map();   // id -> {state, submit_id?, url?, error?, startedAt}
const HANDLES_TTL = 3600 * 1000;
setInterval(() => {
  const now = Date.now();
  for (const [k, v] of HANDLES) {
    if (now - v.startedAt > HANDLES_TTL) HANDLES.delete(k);
  }
}, 300 * 1000);

app.get("/health", async (_req, res) => {
  try {
    const p = await ensurePage();
    const s = await loggedIn(p);
    res.json({ ok: true, loggedIn: !s.onLogin, url: s.url });
  } catch (e) {
    res.status(503).json({ ok: false, error: String(e).slice(0, 200) });
  }
});

app.post("/fetch", async (req, res) => {
  const { url, method = "POST", body, headers } = req.body || {};
  if (!url) return res.status(400).json({ error: "url required" });
  try {
    const out = await pageFetch(url, {
      method,
      headers: Object.assign({ "content-type": "application/json" }, headers || {}),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    res.json(out);
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 300) });
  }
});

// 🔧 运维端点（只绑 127.0.0.1，勿暴露公网）：注入登录 cookie。
// body: {"cookie": "k=v; k2=v2"} —— 抓包/导出的完整 cookie 串。
app.post("/cookies", async (req, res) => {
  try {
    const raw = String((req.body || {}).cookie || "");
    if (!raw.includes("=")) return res.status(400).json({ error: "cookie required" });
    const p = await ensurePage();
    const cookies = raw.split(";").map((pair) => {
      const i = pair.indexOf("=");
      return { name: pair.slice(0, i).trim(), value: pair.slice(i + 1).trim(),
               domain: ".capcut.com", path: "/" };
    }).filter((c) => c.name);
    // ⚠️ 不带 expires 的 cookie 是 session cookie ⇒ 容器重启即丢
    //（实测踩过）——统一加 90 天有效期保证写进 user-data-dir 持久化；
    // 同时落盘 /data/cookie.txt，启动时自动恢复（见 ensureLogin）。
    const exp = Math.floor(Date.now() / 1000) + 90 * 24 * 3600;
    for (const c of cookies) c.expires = exp;
    await p.context().addCookies(cookies);
    try { require("fs").writeFileSync(PROFILE_DIR + "/cookie.txt", raw); } catch (e) {}
    await p.reload({ waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT }).catch(() => {});
    const s = await loggedIn(p);
    res.json({ ok: true, injected: cookies.length, loggedIn: !s.onLogin });
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 300) });
  }
});

app.post("/generate", async (req, res) => {
  const endpoint = "https://mweb-api-sg.capcut.com/mweb/v1/aigc_draft/generate";
  const q = "aid=513641&device_platform=web&region=PH&da_version=3.3.30&os=mac"
          + "&web_component_open_flag=1&web_version=7.5.0"
          + "&aigc_features=app_lip_sync";
  try {
    const out = await pageFetch(`${endpoint}?${q}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(req.body || {}),
    });
    res.json(out);
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 300) });
  }
});

// 🔰 保底通路（2026-10-15 实测唯一全通形态）：Playwright 驱动真实 UI。
// page-fetch 恒 3018（页面 axios 拦截器注入 secsdk 签名，裸 fetch 不被注入）；
// UI 点击天然全通（E0 实证：过 shark + ret=0）。
// 流程：监听页面请求抓 submit_id → 填 prompt（click+keyboard 触发 React）→
// Enter/按钮提交 → 轮询 get_history_by_ids（读接口无权限问题）到终态。
const HISTORY_QS = "aid=513641&device_platform=web&region=PH"
  + "&da_version=3.3.30&web_version=7.5.0";
// UI 流程核心：切 AI Image → （可选）选模型/档位 → 填 prompt → 发送 →
// 返回 submit_id。终态轮询由调用方直连读接口完成（读接口不受 shark 影响）。
async function generateUiCore({ prompt, model, resolution, ratio }) {
  const p0 = await ensureLogin();
  const p = p0.p;
  // 🔴 模型/模式必须选 AI Image（默认 AI Agent 的提交不走
  // aigc_draft/generate）。用「locate 坐标 → 真鼠标点击」模式：
  // DOM 生命周期（React 重渲染）会让 locator.click 卡死，坐标不受影响。
  await p.waitForSelector('[contenteditable="true"]', { timeout: 30000 });
  const tabPos = await p.evaluate(() => {
    const span = [...document.querySelectorAll("span,div")]
      .find((e) => e.childElementCount === 0
                    && e.textContent.trim() === "AI Image");
    if (!span) return null;
    const r = span.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  if (tabPos) {
    await p.mouse.click(tabPos.x, tabPos.y);
    await p.waitForTimeout(800);
  }
  // 🔴 模型选择（可选）：参数行 combobox（当前值如 "GPT Image 2.5 Sunburst"）
  // —— 点开下拉 → 点目标模型文本
  if (model) {
    const combo = p.locator("[role=combobox]").first();
    if (await combo.isVisible().catch(() => false)) {
      await combo.click({ force: true, timeout: 5000 });
      await p.waitForTimeout(800);
      const opt = p.getByText(String(model), { exact: false }).first();
      await opt.click({ force: true, timeout: 5000 }).catch(() => {});
      await p.waitForTimeout(600);
    }
  }
  // 🔴 比例/档位都在**参数面板**里（点参数行的档位按钮才渲染——实测
  // 面板不开时控件不在 DOM）。比例 = 面板顶部格子（SPAN 叶子，文本
  // "1:1"/"16:9"/…）；档位 = radiogroup 的 radio（value 1k/2k/4k）。
  if (ratio || resolution) {
    await p.evaluate(() => {
      const b = [...document.querySelectorAll("button")]
        .find((x) => /2K/.test(x.textContent || "")
                      && x.getBoundingClientRect().width > 0);
      if (b) b.click();
    });
    await p.waitForTimeout(700);
    if (ratio) {
      await p.evaluate((want) => {
        const span = [...document.querySelectorAll("span")]
          .find((e) => e.childElementCount === 0
                        && e.textContent.trim() === want);
        if (span) (span.closest("button") || span).click();
      }, String(ratio)).catch(() => {});
      await p.waitForTimeout(500);
    }
    if (resolution) {
      await p.evaluate((want) => {
        const radio = [...document.querySelectorAll('[role=radio]')]
          .find((r) => r.getAttribute("value") === want);
        if (radio) radio.click();
      }, String(resolution)).catch(() => {});
      await p.waitForTimeout(500);
    }
  }
  const comp = p.locator('[contenteditable="true"]').first();
  await comp.click({ force: true, timeout: 10000 });
  await p.keyboard.press("Escape").catch(() => {});
  await comp.click({ force: true, timeout: 10000 });
  await p.keyboard.type(String(prompt).slice(0, 400), { delay: 15 });
  await p.waitForTimeout(400);
  // 发送按钮：**精确**定位 primary（容器内唯一），禁用则等它启用
  const sendBtn = p.locator(
    "[class*='collapsed-submit-button-container'] button.lv-btn-primary"
  ).first();
  await sendBtn.waitFor({ state: "visible", timeout: 10000 })
    .catch(() => {});
  await p.waitForFunction(
    () => { const b = document.querySelector(
      "[class*='collapsed-submit-button-container'] button.lv-btn-primary");
      return b && !b.disabled; }, { timeout: 15000 }).catch(() => {});
  await sendBtn.evaluate((b) => {
    b.click();   // 🔴 DOM click：React onClick 绑定在组件树内，直接触发
  });
  const dl0 = Date.now() + 10000;
  // 提交完成即返回 —— submit_id 由入口的请求监听器捕获
  await (await ensurePage()).waitForTimeout(1500);
}

app.post("/generate-ui", async (req, res) => {
  const { prompt, timeoutMs = 180000 } = req.body || {};
  if (!prompt) return res.status(400).json({ error: "prompt required" });
  const { p } = await ensureLogin();
  let genSubmitId = null;
  const onRequest = (rq) => {
    if (rq.method() === "POST" && rq.url().includes("aigc_draft/generate")) {
      try {
        genSubmitId = JSON.parse(rq.postData() || "{}").submit_id || genSubmitId;
      } catch (e) { /* 非 JSON 帧 */ }
    }
  };
  p.on("request", onRequest);
  const started = Date.now();
  try {
    await generateUiCore(Object.assign({}, req.body));
    const dl0 = Date.now() + 10000;
    while (!genSubmitId && Date.now() < dl0) await p.waitForTimeout(400);
    if (!genSubmitId) throw new Error("no generate request observed");
    return res.json({ ok: true, mode: "ui", submit_id: genSubmitId,
                      elapsedMs: Date.now() - started });
  } catch (e) {
    return res.status(502).json({ error: String(e).slice(0, 300) });
  } finally {
    p.off("request", onRequest);
  }
});

// 🔱 异步入口（jimeng-service 派发线程用）：立即返回 handle，UI 流程后台跑。
// 派发线程 20s+ 的 UI 流程会阻塞 WORKERS=1 的串行派发 —— 绝不能同步等。
app.post("/generate-ui-async", async (req, res) => {
  const { prompt, model, resolution } = req.body || {};
  if (!prompt) return res.status(400).json({ error: "prompt required" });
  const id = "h" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  HANDLES.set(id, { state: "running", startedAt: Date.now() });
  res.json({ ok: true, handle: id });
  // 后台执行（不 await）
  (async () => {
    const p = (await ensureLogin()).p;
    let genSubmitId = null;
    const onRequest = (rq) => {
      if (rq.method() === "POST" && rq.url().includes("aigc_draft/generate")) {
        try {
          genSubmitId = JSON.parse(rq.postData() || "{}").submit_id
            || genSubmitId;
        } catch (e) { /* 非 JSON 帧 */ }
      }
    };
    p.on("request", onRequest);
    try {
      await generateUiCore({ prompt, model, resolution, ratio });
      const dl = Date.now() + 15000;
      while (!genSubmitId && Date.now() < dl) await p.waitForTimeout(300);
      if (!genSubmitId) throw new Error("no generate request observed");
      HANDLES.set(id, { state: "done", submit_id: genSubmitId,
                        startedAt: HANDLES.get(id).startedAt });
    } catch (e) {
      const prev = HANDLES.get(id) || {};
      HANDLES.set(id, { state: "error", error: String(e).slice(0, 300),
                        startedAt: prev.startedAt || Date.now() });
    } finally {
      p.off("request", onRequest);
    }
  })();
});

app.get("/handle/:id", async (req, res) => {
  const h = HANDLES.get(req.params.id);
  if (!h) return res.status(404).json({ error: "unknown handle" });
  const out = { state: h.state, startedAt: h.startedAt };
  if (h.submit_id) out.submit_id = h.submit_id;
  if (h.error) out.error = h.error;
  res.json(out);
});


app.get("/debug/buttons", async (_req, res) => {
  try {
    const p = await ensurePage();
    const out = await p.evaluate(() => {
      const ed = document.querySelector('[contenteditable="true"]');
      if (!ed) return { error: "no-editor" };
      const edr = ed.getBoundingClientRect();
      const buttons = [...document.querySelectorAll("button")]
        .map((b) => {
          const r = b.getBoundingClientRect();
          return { txt: (b.textContent || "").trim().slice(0, 12),
                   dis: b.disabled, svg: !!b.querySelector("svg"),
                   x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height),
                   cls: String(b.className).slice(0, 44) };
        })
        .filter((o) => o.w > 0 && o.h > 0);
      // 与 composer 距离排序（近的可能相关）
      const near = buttons
        .map((o) => Object.assign(o, { d: Math.round(Math.hypot(
          o.x - edr.x, o.y - edr.y)) }))
        .sort((a, b) => a.d - b.d).slice(0, 12);
      // 参数行区域（编辑器正下方 350~480，x>340）的所有可点击元素
      const paraRow = [...document.querySelectorAll(
        "button,div[role=button],span[role=button],div[class*=send],"
        + "div[class*=submit],div[class*=generate],div[class*=arrow]")]
        .map((b) => {
          const r = b.getBoundingClientRect();
          return { tag: b.tagName, txt: (b.textContent || "").trim()
                     .slice(0, 12),
                   dis: b.disabled === true, svg: !!b.querySelector("svg"),
                   x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height),
                   cls: String(b.className).slice(0, 56) };
        })
        .filter((o) => o.w > 8 && o.h > 8 && o.y > edr.y + edr.height - 20
                       && o.y < edr.y + edr.height + 180 && o.x > 340);
      return { editor: { x: Math.round(edr.x), y: Math.round(edr.y),
                         w: Math.round(edr.width), h: Math.round(edr.height) },
               near, paraRow };
    });
    res.json(out);
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 300) });
  }
});

app.get("/debug/shot", async (_req, res) => {
  try {
    const p = await ensurePage();
    const buf = await p.screenshot({ type: "png" });
    res.type("png").send(buf);
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 200) });
  }
});

app.get("/login/qr.png", async (_req, res) => {
  try {
    const p = await ensurePage();
    const buf = await p.screenshot({ type: "png" });
    res.type("png").send(buf);
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 200) });
  }
});

async function main() {
  await ensurePage();
  app.listen(LISTEN, () => console.log(`[sg-bridge] listening on :${LISTEN}`));
  // 页面保活：每 10 分钟轻触一次（导航卡死则重开）
  setInterval(async () => {
    try {
      if (!page || page.isClosed()) { await ensurePage(); return; }
      const s = await loggedIn(page);
      if (s.onLogin) await page.goto(START_URL, { waitUntil: "domcontentloaded",
                                                  timeout: NAV_TIMEOUT }).catch(() => {});
    } catch (e) {
      console.error("[sg-bridge] keepalive:", String(e).slice(0, 120));
      try { if (ctx) await ctx.close(); } catch (_) {}
      page = null; ctx = null; starting = null;
      await ensurePage();
    }
  }, 10 * 60 * 1000);
}

main().catch((e) => { console.error("[sg-bridge] fatal:", e); process.exit(1); });
