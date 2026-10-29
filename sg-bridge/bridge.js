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
      // 🔴 2026-10-29：改成新版首页后，页面是"创作 feed"，一进去就狂拉
      // 视频/大图 —— 在 8 核小机器上渲染进程会被堵住几十秒，表现就是
      // `page.evaluate` **永不返回**（改版适配时整条流程"挂死"的真因，
      // 单靠加超时只能把挂死变成必然失败）。图片工具只需要 DOM + 文本，
      // ⇒ 媒体类资源直接 abort，页面立刻轻下来。
      await ctx.route("**/*", (route) => {
        const t = route.request().resourceType();
        if (t === "media") return route.abort();
        return route.continue();
      }).catch(() => {});
      page = ctx.pages()[0] || (await ctx.newPage());
      await page.goto(START_URL, { waitUntil: "domcontentloaded",
                                  timeout: NAV_TIMEOUT }).catch(() => {});
      return page;
    })();
    // 🔴 成功也要清：否则 page 被关掉后，这里永远返回**旧的** promise
    //（里面是已 closed 的 page）⇒ 后续 evaluate 直接崩进程。
    starting.finally(() => { starting = null; }).catch(() => {});
    // （额外 catch：避免 finally 返回的 promise 成为 unhandled rejection）
  }
  return starting;
}

function loggedIn(page_) {
  return page_.evaluate(() => {
    // 登录判定：存在 sessionid cookie（httpOnly 不可见）⇒ 用业务信号：
    // 已登录页面会出现 workspace/feed；未登录跳登录页。
    //
    // 🔴 2026-10-29：原来读 `document.body.innerText` —— **innerText 会强制
    // 整页布局**，而改版后的首页 DOM 巨大，一次调用要 10~60s（实测把
    // ensureLogin 拖成"挂死"，所有派发一起卡住）。改用两条廉价信号：
    //   ① URL 里带 login/sign-in ⇒ 直接判未登录；
    //   ② 有 `[contenteditable]`（composer）⇒ 一定在已登录的应用内页面；
    // 只有前两条都不成立时才退回去读文本（此时页面大概率很轻）。
    const onLoginUrl = /登入|登錄|log\s?in|sign\s?in/i.test(location.href);
    if (onLoginUrl) return { url: location.href, onLogin: true };
    if (document.querySelector('[contenteditable="true"]')) {
      return { url: location.href, onLogin: false };
    }
    const text = document.body ? (document.body.textContent || "").slice(0, 2000) : "";
    return { url: location.href, onLogin: /sign in with/i.test(text) };
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
async function generateUiCore({ prompt, model, resolution, ratio, count,
                               dryRun = false }) {
  const _t = Date.now();
  console.log(`[ui] core enter dryRun=${dryRun}`);
  const p0 = await ensureLogin();
  console.log(`[ui] core login ok +${((Date.now() - _t) / 1000).toFixed(1)}s`);
  // 🔴 2026-10-29 **SG 前端改版**：`/ai-tool/image` 已 302 到 `/ai-tool/home`，
  // 图片工具变成首页上的一个 tab（`AI Image`），参数面板也换了形态 ——
  // 旧的三处判据全部失效：`[class*=generate-count-field]` 已不在 DOM、
  // 比例/档位合成一行、张数改成 `1 image` 按钮点开 popover。
  // 线上症状：**793 单**死在「张数控件找不到 1」，而重启 bridge 完全无用。
  //
  // 新形态（2026-10-29 在真实页面实测）：
  //   · 模式 tab      = `<button>AI Image</button>`（点完 URL 变 `?type=image`）
  //   · 模型          = `[role=combobox]`，文本即当前模型（默认 Seedream 5.0 Lite）
  //   · 比例 + 档位   = 同一个按钮，文本两行 `16:9\n2K`，点开 popover 里是
  //                     `LABEL.lv-radio`（Auto/1:1/3:4/16:9/… 与 2K/4K）
  //   · 张数          = `button[aria-label^="Images to generate"]`（文本 `1 image`），
  //                     点开 popover 里 `LABEL.lv-radio` 1..8
  //   ⇒ **就绪判据 = 张数按钮在不在**（`[role=combobox]` 在 AI Agent 模式下
  //     也存在，拿它判断会直接跳过点 tab —— 这正是本次事故的起点）。
  const countSel = 'button[aria-label^="Images to generate"]';
  // 步骤计时（`docker logs sg-bridge` 里看得见）：改版适配期定位卡点用。
  // 之前一次 dryRun 无响应地挂死过 3 分钟，就是靠这套打点定位的。
  const _t0 = Date.now();
  const step = (s) => console.log(`[ui] +${((Date.now() - _t0) / 1000).toFixed(1)}s ${s}`);
  // 每次页面交互都套一层硬超时：`page.evaluate` 在渲染进程忙/页面导航中会
  // **永不返回**，而它没有 timeout 参数（Playwright 的 default timeout 不覆盖
  // evaluate）⇒ 一次卡住就把 UI 锁焊死、后面所有派发排队等死。
  // 2026-10-29 实测：切参数的第二次调用就是这么挂的。
  const evR = (label, prom, ms = 15000) => Promise.race([
    prom,
    new Promise((_r, rej) => setTimeout(
      () => rej(new Error(`${label} 无响应 ${ms}ms`)), ms)),
  ]);

  // 🔴 **不许用 `innerText` 全表扫描**：`innerText` 会为每个元素强制布局，
  // 而首页按钮上千个 ⇒ 一次"找按钮"要 10~30s，整条流程看起来像挂死
  //（2026-10-29 实测：dryRun 3 分钟无响应，根因就是这个）。
  // 正确做法：用 aria-label **精确定位张数按钮**，再在它的**同一个父节点**里取
  // 兄弟按钮 —— 那里只有 4 个元素，全是 `textContent`（不触发布局）。
  //
  // 参数行实测结构：
  //   div.container-* > div > [button(比例, textContent "3:42K"),
  //                             button(张数, aria-label "Images to generate: 1"), …]
  const readParams = () => evR("readParams", p.evaluate((sel) => {
    const vis = (e) => e.getBoundingClientRect().width > 0;
    const cnt = [...document.querySelectorAll(sel)].find(vis);
    if (!cnt) return { count: "", ratio: "", resolution: "" };
    const rb = [...cnt.parentElement.querySelectorAll("button")]
      .find((b) => b !== cnt && /\dK$/i.test((b.textContent || "").trim()));
    // 🔴 解析必须"档位只占一位数字 + K"：`3:4` + `2K` 的 textContent 是
    // `3:42K`，写成 `(\d+K)$` 会把 `42K` 当档位（实测 got="3:"）。
    const m = rb ? (rb.textContent || "").trim().match(/^(.+?)(\d(?:\.\d)?K)$/i) : null;
    return {
      // aria-label = "Images to generate: 1" ⇒ 取冒号后的数字
      count: String(cnt.getAttribute("aria-label") || "").split(":").pop().trim(),
      ratio: m ? m[1] : "",
      resolution: m ? m[2] : "",
    };
  }, countSel), 12000).catch((e) => {
    step(`readParams FAIL ${e.message}`);
    return { count: "", ratio: "", resolution: "" };
  });

  const inImageMode = async () => !!(await readParams()).count;

  // 提交成功后页面会跳到 `/ai-tool/generate`（结果页，没有 tab/参数行）——
  // 那种状态只能**换页**（cookie 在 context 上，新页天然登录态）。
  //
  // 🔴 但**别急着换页**：首页加载是异步的，张数按钮要几秒才渲染出来。
  // 之前"立刻判断 inImageMode 失败就关页重开"，等于每单都多开一个重页面，
  // 两个 SG 首页在 8 核机器上互抢 CPU ⇒ 后续所有 evaluate 超时、UI 锁焊死
  //（2026-10-29 实测，dryRun 连续挂死就是这条）。
  // ⇒ 现在是「先等 → 点 tab → 再等 → 最后才换页」。
  const waitCount = async (ms = 15000) => {
    const dl = Date.now() + ms;
    while (Date.now() < dl) {
      if (await inImageMode()) return true;
      await p.waitForTimeout(500);
    }
    return await inImageMode();
  };

  let p = p0.p;
  step(`page ${p.url()}`);
  // 🔴 tab 是受控组件：必须**真鼠标点击**（`el.click()` 只改视觉不改 state），
  // 且单次不可靠（渲染慢/被遮挡）⇒ 点 → 校验 → 重试（最多 3 次）。
  const clickTab = async () => {
    for (let attempt = 0; attempt < 3; attempt++) {
      const pos = await p.evaluate(() => {
        const b = [...document.querySelectorAll("button")]
          .find((x) => (x.textContent || "").trim() === "AI Image");
        if (!b || b.getBoundingClientRect().width === 0) return null;
        const r = b.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }).catch(() => null);
      step(`tab click ${attempt} pos=${JSON.stringify(pos)}`);
      if (pos) await p.mouse.click(pos.x, pos.y);
      if (await waitCount(8000)) return true;
    }
    return false;
  };

  if (!(await waitCount(12000))) {
    if (!(await clickTab())) {
      step("换新标签页");
      const ctx = p.context();
      await p.close().catch(() => {});
      p = await ctx.newPage();
      page = p;                       // ← 关键：全局也要指向新 page
      await p.goto("https://dreamina.capcut.com/ai-tool/home",
                   { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
      step(`goto done ${p.url()}`);
      if (!(await waitCount(20000)) && !(await clickTab())) {
        throw new Error("进不去 AI Image 模式（等不到张数按钮）—— 拒绝在未知模式下提交");
      }
    }
  }
  // ⚠️ 这里**不要**加 `goto` 兜底 —— 实测加完会让后续 UI 流程卡住
  //（任务一直 in_progress 到 TASK_TIMEOUT）。页面位置由 `ensureLogin()` 的
  // START_URL 保证；状态不对宁可**报错不提交**。
  // 🔴 比例 / 张数：新 UI 都是「点参数行按钮 → 弹层里点 `LABEL.lv-radio`」。
  // 弹层 DOM 是 `.lv-popover-content`（页面上会残留历史节点）⇒ 取**最后一个
  // 可见的**；选项是受控 radio，`.click()` 只改视觉不改 React state
  //（2026-10-19 实测：读回 1、提交出去还是 4）⇒ **一律真鼠标点坐标**。
  const popoverPick = (want) => evR("popoverPick", p.evaluate((w) => {
    const vis = (e) => e.getBoundingClientRect().width > 0;
    const pops = [...document.querySelectorAll(".lv-popover-content")].filter(vis);
    const pop = pops[pops.length - 1];
    if (!pop) return null;
    const lab = [...pop.querySelectorAll("label.lv-radio")]
      .find((e) => (e.textContent || "").trim() === w
                   && e.getBoundingClientRect().width > 0);
    if (!lab) return null;
    const r = lab.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }, String(want)), 12000).catch((e) => {
    step(`popoverPick FAIL ${e.message}`);
    return null;
  });

  // 比例按钮 = 张数按钮的**前一个兄弟**（同父节点内，文本以档位 `2K/4K` 结尾）
  const openRatioBtn = () => evR("openRatioBtn", p.evaluate((sel) => {
    const vis = (e) => e.getBoundingClientRect().width > 0;
    const cnt = [...document.querySelectorAll(sel)].find(vis);
    if (!cnt) return null;
    const rb = [...cnt.parentElement.querySelectorAll("button")]
      .find((b) => b !== cnt && /\dK$/i.test((b.textContent || "").trim()));
    if (!rb) return null;
    const r = rb.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }, countSel), 12000).catch((e) => {
    step(`btn FAIL ${e.message}`);
    return null;
  });

  const openCountBtn = () => evR("openCountBtn", p.evaluate((sel) => {
    const vis = (e) => e.getBoundingClientRect().width > 0;
    const b = [...document.querySelectorAll(sel)].find(vis);
    if (!b) return null;
    const r = b.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }, countSel), 12000).catch((e) => {
    step(`btn FAIL ${e.message}`);
    return null;
  });

  const closePopover = async () => {
    await p.keyboard.press("Escape").catch(() => {});
    await p.waitForTimeout(300);
  };

  // 🔴 模型选择：参数行 combobox（当前值如 "Seedream 5.0 Lite"）。
  //
  // 🔴 2026-10-19 实测事故：原来用 `p.getByText(model)` **全局**找选项 ——
  // 它会命中页面上任意同名文本（模型介绍卡、历史记录…），点完下拉直接关掉，
  // 模型**没换**。线上 n=1 测试就这么静默跑了 `dreamina_gpt_image_2_5_flare`
  // （实扣 51/张），而调用方以为在用 Lite（36/张）。
  // ⇒ 三条修正：① 先确保在图片模式（上面已做）；② 只在**下拉弹层内**找选项；
  //    ③ 选完**读回校验**，对不上就抛错（没提交 = 不花钱，比"用错模型扣钱"好得多）。
  // 2026-10-29 改版后 combobox 仍在（默认 `Seedream 5.0 Lite`），选项文本不变。
  step(`mode=image params=${JSON.stringify(await readParams())}`);
  if (model) {
    const combo = p.locator("[role=combobox]").first();
    if (!(await combo.isVisible().catch(() => false))) {
      throw new Error(`模型选择器不可见（want=${model}）—— 拒绝在未知模型下提交`);
    }
    {
      await combo.click({ force: true, timeout: 15000 });
      await p.waitForTimeout(900);
      const picked = await p.evaluate((want) => {
        const lb = document.querySelector("[role=listbox]")
          || document.querySelector("[class*='lv-select-popup']")
          || document.querySelector("[class*='select-popup']");
        const root = lb || document;
        // 先按**文本**筛（textContent 不触发布局），再对候选量尺寸
        const all = [...root.querySelectorAll("[role=option],li,div,span")]
          .filter((e) => e.childElementCount === 0);
        const cand = all.filter((e) => (e.textContent || "").trim() === want);
        const el = cand.find((e) => e.getBoundingClientRect().width > 0)
          || all.filter((e) => (e.textContent || "").includes(want))
            .find((e) => e.getBoundingClientRect().width > 0);
        if (!el) return false;
        el.click();
        return true;
      }, String(model));
      await p.waitForTimeout(700);
      const now = (await combo.innerText().catch(() => "")) || "";
      if (!now.includes(String(model))) {
        throw new Error(
          `模型选择失败：want=${model} got=${now.trim().slice(0, 60)} picked=${picked}`);
      }
    }
  }
  // 比例：点参数行按钮 → 弹层点 `16:9` 之类 → 读回按钮第一行校验
  if (ratio) {
    step(`ratio want=${ratio}`);
    let got = (await readParams()).ratio;
    for (let attempt = 0; attempt < 3 && got !== String(ratio); attempt++) {
      const btn = await openRatioBtn();
      step(`ratio#${attempt} btn=${JSON.stringify(btn)}`);
      if (!btn) break;
      await p.mouse.click(btn.x, btn.y);
      await p.waitForTimeout(800);
      const opt = await popoverPick(ratio);
      step(`ratio#${attempt} opt=${JSON.stringify(opt)}`);
      if (opt) {
        await p.mouse.click(opt.x, opt.y);
        await p.waitForTimeout(700);
      }
      await closePopover();
      got = (await readParams()).ratio;
      step(`ratio#${attempt} got=${got}`);
    }
    if (got !== String(ratio)) {
      throw new Error(`比例选择失败：want=${ratio} got=${got || "?"}`);
    }
  }
  // 🔴 张数（"Images to generate"，1..8）：不选就按 UI 默认出 **4 张** ——
  // 国际站 36/张，4 张 = 144，也就是"调用方要 1 张、我们却花 4 倍的钱"。
  // ⇒ 读回不符**绝不提交**。读回源 = 按钮的 `aria-label="Images to generate: N"`。
  //
  // ⚠️ **档位（2K/4K）刻意不碰**：服务端也不传 resolution
  //（`service.py` 的 `_submit_via_bridge` 里记着实测：点档位会触发重渲染、
  //  把张数重置回 4，n=1 被吃掉）。默认档就是 2K。
  if (count) {
    step(`count want=${count}`);
    step("count readParams...");
    let got = (await readParams()).count;
    step(`count current=${got}`);
    for (let attempt = 0; attempt < 3 && got !== String(count); attempt++) {
      const btn = await openCountBtn();
      step(`count#${attempt} btn=${JSON.stringify(btn)}`);
      if (!btn) break;
      await p.mouse.click(btn.x, btn.y);
      await p.waitForTimeout(800);
      const opt = await popoverPick(count);
      step(`count#${attempt} opt=${JSON.stringify(opt)}`);
      if (opt) {
        await p.mouse.click(opt.x, opt.y);
        await p.waitForTimeout(700);
      }
      await closePopover();
      got = (await readParams()).count;
      step(`count#${attempt} got=${got}`);
    }
    if (got !== String(count)) {
      throw new Error(`张数选择失败：want=${count} got=${got || "?"}`);
    }
  }
  // 🧪 dryRun：只跑到"参数选完 + 读回校验通过"就返回，**不键入、不提交**
  //（零积分）—— 给 `/debug/ui-params` 用，改版适配期靠它做无成本回归。
  if (dryRun) {
    const got = await readParams();
    step(`dryRun done ${JSON.stringify(got)}`);
    return got;
  }
  // 键入 prompt。
  // ⚠️ 改版后**不要再按 Escape**（旧代码用它关参数面板）：新版 Escape 会把
  // composer 收起来，`Generate` 按钮随之消失 ⇒ 提交恒超时。
  const comp = p.locator('[contenteditable="true"]').first();
  await comp.click({ force: true, timeout: 15000 });
  await p.waitForTimeout(400);
  await p.keyboard.type(String(prompt).slice(0, 400), { delay: 15 });
  await p.waitForTimeout(600);
  step("prompt typed");
  // 🔴 发送按钮：2026-10-29 改版后是 composer 右下角的圆形 primary 按钮，
  // 唯一定位属性 = `aria-label="Generate"`（旧的
  // `[class*=collapsed-submit-button-container] button.lv-btn-primary` 已消失，
  // 线上表现为 "locator.evaluate: Timeout 30000ms exceeded"）。
  const sendPos = () => evR("sendPos", p.evaluate(() => {
    const vis = (e) => e.getBoundingClientRect().width > 0;
    const b = [...document.querySelectorAll('button[aria-label="Generate"]')]
      .find(vis);
    if (!b) return null;
    const r = b.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2,
             disabled: !!b.disabled };
  }), 12000).catch(() => null);
  // 等它变可用（prompt 落地后 React 才 enable），最多 15s
  let sp = null;
  for (let i = 0; i < 30; i++) {
    sp = await sendPos();
    if (sp && !sp.disabled) break;
    await p.waitForTimeout(500);
  }
  if (!sp) throw new Error("找不到发送按钮（aria-label=Generate）");
  if (sp.disabled) throw new Error("发送按钮始终 disabled（prompt 没进 composer？）");
  step(`send click ${JSON.stringify(sp)}`);
  await p.mouse.click(sp.x, sp.y);
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
    await withUiLock(() => generateUiCore(Object.assign({}, req.body)));
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

// 🔴 UI 互斥锁：同一 page 同时只能跑一个 UI 流程 —— 并发任务的
// 面板点击/键入互相污染（实测 4 连发时比例错乱：1:1 和 16:9 都出了
// 上一任务的 3:4）。后来者排队等前一个完成。
let _uiBusy = false;
let _uiBusySince = 0;
//: 僵尸流程最多占用 UI 多久（超过就强制放锁）。页面渲染进程被堵住时
//: `page.evaluate` 不会返回 ⇒ 没有这个看门狗，一次卡死会把**所有**后续
//: 派发焊在锁上（2026-10-29 实测：连续 6 次调用全部 60s 超时，日志一行没有）。
const UI_LOCK_MAX = 240 * 1000;
async function withUiLock(fn) {
  while (_uiBusy) {
    if (Date.now() - _uiBusySince > UI_LOCK_MAX) {
      console.log("[sg-bridge] UI 锁持有超时（僵尸流程），强制释放");
      _uiBusy = false;
      break;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  _uiBusy = true;
  _uiBusySince = Date.now();
  console.log("[ui] lock acquired");
  try {
    return await fn();
  } finally {
    _uiBusy = false;
    console.log("[ui] lock released");
  }
}

// 🧪 无提交探针：跑完整套「进图片模式 → 选模型/比例/张数 → 读回校验」，
// 但**不键入 prompt、不点发送** ⇒ 零积分。SG 前端改版后先打它，
// 读回值对得上再放真实任务进来（2026-10-29 改版适配就是靠它验证的）。
app.post("/debug/ui-params", async (req, res) => {
  const { model, ratio, count = 1, timeoutMs = 120000 } = req.body || {};
  try {
    // 🔴 硬超时：UI 流程里任何一步卡住（页面重渲染/弹层没关）都会让请求
    // 无限挂起，而 `withUiLock` 没有超时 ⇒ 后面所有请求排在锁上一起挂。
    // 2026-10-29 实测踩过：一次 dryRun 挂了 3 分钟，把锁焊死。
    console.log("[ui] dryRun requested");
    const got = await Promise.race([
      withUiLock(() => generateUiCore({
        prompt: "", model, ratio, count, dryRun: true })),
      new Promise((_r, rej) => setTimeout(
        () => rej(new Error(`dryRun 超时 ${timeoutMs}ms`)), timeoutMs)),
    ]);
    return res.json({ ok: true, got });
  } catch (e) {
    console.log(`[ui] dryRun 失败：${String(e).slice(0, 200)}`);
    return res.status(502).json({ ok: false, error: String(e).slice(0, 300) });
  }
});

// 🔱 异步入口（jimeng-service 派发线程用）：立即返回 handle，UI 流程后台跑。
// 派发线程 20s+ 的 UI 流程会阻塞 WORKERS=1 的串行派发 —— 绝不能同步等。
app.post("/debug/upload-probe", async (req, res) => {
  // 临时诊断端点：在真实页面里上传一张图，抓 `apply/upload` 类请求的完整形状
  //（这是"国际站上传到底怎么签"的唯一可靠来源 —— UI 能传成功）。
  const b64 = String((req.body || {}).png_b64 || "");
  if (!b64) return res.status(400).json({ error: "png_b64 required" });
  const buf = Buffer.from(b64.replace(/^data:[^,]+,/, ""), "base64");
  const p = (await ensureLogin()).p;
  const calls = [];
  const onReq = (rq) => {
    const u = rq.url();
    if (!/apply|upload|imagex|vod|tos-|\/space\//i.test(u)) return;
    let body = "";
    try { body = (rq.postData() || "").slice(0, 1200); } catch (e) { /* 二进制帧 */ }
    calls.push({ method: rq.method(), url: u.slice(0, 500),
                 headers: rq.headers(), body });
  };
  p.on("request", onReq);
  try {
    // 页面**没有** `input[type=file]`（实测）⇒ 即梦的上传是「粘贴/拖放」。
    // 先试 paste，再试 dragover+drop，两条都打不出来才算探针失败。
    const fired = [];
    for (const kind of ["paste", "drop"]) {
      const ok = await p.evaluate(({ b64, kind }) => {
        const bin = atob(b64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        const file = new File([arr], "probe.png", { type: "image/png" });
        const dt = new DataTransfer();
        dt.items.add(file);
        const target = document.querySelector('[contenteditable="true"]')
          || document.body;
        if (!target) return false;
        if (kind === "paste") {
          target.dispatchEvent(new ClipboardEvent("paste", {
            clipboardData: dt, bubbles: true, cancelable: true }));
        } else {
          target.dispatchEvent(new DragEvent("dragover", {
            dataTransfer: dt, bubbles: true, cancelable: true }));
          target.dispatchEvent(new DragEvent("drop", {
            dataTransfer: dt, bubbles: true, cancelable: true }));
        }
        return true;
      }, { b64: b64.replace(/^data:[^,]+,/, ""), kind }).catch(() => false);
      fired.push({ kind, ok });
      await p.waitForTimeout(6000);
    }
    return res.json({ ok: calls.length > 0, fired, n: calls.length, calls });
  } catch (e) {
    return res.json({ ok: false, reason: String(e).slice(0, 200), calls });
  } finally {
    p.off("request", onReq);
  }
});

app.post("/generate-ui-async", async (req, res) => {
  // 🔴 2026-10-19 事故：这里原来只解构 4 个字段，**漏了 `count`**
  // ⇒ 服务的 `n` 在这一层被丢掉，`generateUiCore` 拿到 undefined，
  // 直接跳过张数选择、按 UI 默认出 **4 张**（144 积分）。
  // 同参数走同步端点 `/generate-ui` 是好的（那里全量 `Object.assign`），
  // 所以"手工测通了"不等于"服务链路通"。
  const { prompt, model, resolution, ratio, count } = req.body || {};
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
      await withUiLock(() => generateUiCore({ prompt, model, resolution,
        ratio, count }));
      const dl = Date.now() + 15000;
      // 🔴 不能用 `p.waitForTimeout`：`generateUiCore` 可能已把旧 page 关掉换新，
      // 这里只是**等时间**，用 setTimeout 才不依赖 page 存活。
      while (!genSubmitId && Date.now() < dl) await new Promise((r) => setTimeout(r, 300));
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

// 🔧 调试端点（只绑 127.0.0.1）：页面内执行 JS（产物按钮定位等探测用）
app.post("/debug/eval", async (req, res) => {
  try {
    const p = await ensurePage();
    const out = await p.evaluate(String((req.body || {}).expr || "1"));
    res.json({ ok: true, out });
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 300) });
  }
});

// 🔧 调试端点（只绑 127.0.0.1）：真鼠标坐标点击（React 重渲染环境下的
// 可靠点击；坐标来源：/debug/eval 里 getBoundingClientRect）
app.post("/debug/click", async (req, res) => {
  try {
    const { x, y } = req.body || {};
    const p = await ensurePage();
    await p.mouse.click(Number(x), Number(y));
    res.json({ ok: true });
  } catch (e) {
    res.status(502).json({ error: String(e).slice(0, 200) });
  }
});

// 🔧 调试端点（只绑 127.0.0.1）：hover（产物操作按钮多为 hover 出现）
app.post("/debug/hover", async (req, res) => {
  try {
    const { x, y } = req.body || {};
    const p = await ensurePage();
    await p.mouse.move(Number(x), Number(y));
    await p.waitForTimeout(800);
    res.json({ ok: true });
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

// 🔱 超分腿（normal_hd）UI 驱动：进 session → hover 产物大图 → 工具行
// tooltip 定位 Upscale → 真鼠标 click → 监听 generate 请求抓 submit_id。
// 实测（2026-10-16）：产物 hover 出工具行，Upscale 图标 tooltip="Upscale"，
// 点击后创建 normal_hd 任务（条目标注档位 2K→4K）。
async function upscaleUiCore({ session }) {
  const { p } = await ensureLogin();
  await p.waitForSelector('[contenteditable="true"]', { timeout: 30000 });
  // ① 进目标 session（prompt 前缀匹配 Generative sessions；缺省=第一条）
  await p.evaluate((want) => {
    // 🔴 取**第一个最内层**匹配（侧栏项在 DOM 前部）；取最后一个会命中
    // 外层容器（click 无效，页面停在 home ⇒ 后续全链失败）
    const items = [...document.querySelectorAll("span,div")]
      .filter((e) => {
        const t = (e.textContent || "").trim();
        const r = e.getBoundingClientRect();
        return t.startsWith(want) && t.length < 60 && r.width > 40
               && e.childElementCount <= 2;
      });
    const target = items[0]
      || [...document.querySelectorAll("span,div")].find((e) => {
          const t = (e.textContent || "").trim();
          const r = e.getBoundingClientRect();
          return /flare|burst|leaf|boat|lighthouse|test|mandala/.test(t)
                 && r.width > 40 && r.width < 260
                 && e.childElementCount <= 2;
        });
    if (target) target.click();
  }, String(session || "")).catch(() => {});
  // 等 session 视图就绪：URL 跳到 /generate **或**大图出现（跳转+渲染
  // 时序不稳，实测最长 10s+；在 home 上等大图是白等——feed 未加载）。
  for (let i = 0; i < 20; i += 1) {
    await p.waitForTimeout(1000);
    const ready = await p.evaluate(() => ({
      path: location.pathname,
      hasBig: [...document.querySelectorAll("img")]
        .some((el) => el.getBoundingClientRect().width > 200),
    }));
    if (ready.path.includes("generate") || ready.hasBig) break;
  }
  await p.waitForTimeout(500);
  // ②③ 遍历 session 的每张大图：hover → 工具行 tooltip 找 "Upscale" →
  // 找到即真鼠标点击。🔴 不能只试第一张 —— 已 Upscaled 的产物工具行里
  // 没有 Upscale（实测第一张常是 4K 成品）。工具行 y 随滚动变化 ⇒
  // 相对每张图自己的底边定位。
  // 🔴 bigs 采集**轮询自愈**：session 跳转有瞬态空窗（DOM 骨架期 eval
  // 返回空 ⇒ 立即抛错误报），轮询最多 15s 等图渲染。
  let bigs = [];
  for (let i = 0; i < 15 && !bigs.length; i += 1) {
    await p.waitForTimeout(1000);
    bigs = await p.evaluate(() => {
      const out = [];
      for (const el of document.querySelectorAll("img")) {
        const r = el.getBoundingClientRect();
        if (r.width > 200) out.push({ x: r.x + r.width / 2,
          y: r.y + r.height / 2, bottom: r.bottom });
      }
      return out;
    });
  }
  if (!bigs.length) throw new Error("no product image in session view");
  let upPos = null;
  for (const b of bigs) {
    await p.mouse.move(b.x, b.y);
    await p.waitForTimeout(900);
    // 🔴 tooltip 用**真鼠标**逐图标 hover（合成 mouseenter 触发不了
    // React tooltip；真实鼠标路径今晚已手动验证）
    const iconCoords = await p.evaluate((btm) => {
      const icons = [...document.querySelectorAll("button,div")]
        .filter((x) => {
          const r = x.getBoundingClientRect();
          return r.width > 14 && r.width < 60 && r.height > 14
                 && r.height < 60 && r.y > btm - 140
                 && r.y < btm + 120 && r.x > 480;
        });
      const uniq = [];
      const seen = new Set();
      for (const ic of icons) {
        const k = Math.round(ic.getBoundingClientRect().x);
        if (seen.has(k)) continue;
        seen.add(k);
        const r = ic.getBoundingClientRect();
        uniq.push({ x: r.x + r.width / 2, y: r.y + r.height / 2 });
      }
      return uniq;
    }, b.bottom);
    let found = null;
    for (const c of iconCoords) {
      await p.mouse.move(c.x, c.y);
      await p.waitForTimeout(450);
      const tip = await p.evaluate(() => {
        const tips = [...document.querySelectorAll(
          "[role=tooltip],[class*=tooltip],[class*=Tooltip]")]
          .filter((e) => e.getBoundingClientRect().width > 0
                         && (e.textContent || "").trim());
        return tips.length ? tips[tips.length - 1].textContent.trim()
                           : null;
      });
      if (tip === "Upscale") { found = c; break; }
    }
    if (found) { upPos = found; break; }
  }
  if (!upPos) throw new Error("Upscale icon not found on any product image");
  await p.waitForTimeout(300);
  await p.mouse.click(upPos.x, upPos.y);
  await new Promise((r) => setTimeout(r, 4000));
  return true;
}

app.post("/upscale-ui-async", async (req, res) => {
  const { session } = req.body || {};
  const id = "u" + Date.now().toString(36)
           + Math.random().toString(36).slice(2, 8);
  HANDLES.set(id, { state: "running", startedAt: Date.now() });
  res.json({ ok: true, handle: id });
  (async () => {
    const p = (await ensureLogin()).p;
    let upSubmitId = null;
    const onRequest = (rq) => {
      if (rq.method() === "POST" && rq.url().includes("aigc_draft/generate")) {
        try {
          const b = JSON.parse(rq.postData() || "{}");
          const draft = (() => { try { return JSON.parse(b.draft_content
            || "{}"); } catch (e) { return {}; } })();
          const gt = (draft.component_list || [])[0]
            && draft.component_list[0].generate_type;
          if (gt === "normal_hd" || !gen_sid_marked(b)) {
            upSubmitId = b.submit_id || upSubmitId;
          }
        } catch (e) { /* 非 JSON 帧 */ }
      }
    };
    const gen_sid_marked = (b) => false;   // Upscale 的提交即目标
    p.on("request", onRequest);
    try {
      await withUiLock(() => upscaleUiCore({ session }));
      const dl = Date.now() + 15000;
      // 🔴 不能用 `p.waitForTimeout`：`generateUiCore` 可能已把旧 page 关掉换新，
      // 这里只是**等时间**，用 setTimeout 才不依赖 page 存活。
      while (!upSubmitId && Date.now() < dl) await new Promise((r) => setTimeout(r, 300));
      if (!upSubmitId) throw new Error("no normal_hd request observed");
      HANDLES.set(id, { state: "done", submit_id: upSubmitId,
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
