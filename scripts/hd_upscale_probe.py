#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超清（`normal_hd`）就地放大探针 —— 一次真跑回答两个问题。

**背景（2026-10-12）**：目标是把"请求清晰度 > 2k"的图片任务改成
「2k 生成 + 超清放大」，因为 4k 是独立计费档（Lite 实测 4/张），而超清实测免费。

要接线就必须先答这两个问题（读文档答不了）：

① **引用形态吃不吃**：`item_id` + `origin_history_id`（**不带** `origin_image`）。
   即梦 UI 的"就地超清"就是对**已有作品**点超清（2026-10-12 用户截图，作品页工具栏），
   所以 UI 走的就是引用形态。但 `normal_hd` 的引用形态**从未真跑验证过**
   （只有 `detail` 验证过，见 `docs/UPSTREAM.md` §9.1）。
   若成立 ⇒ 串链时**不必**把 2k 产物下载再上传，直接用产物自带的
   `item_id` + 源任务的 `upstream_history_id` 即可。

② **非方形源的输出尺寸**：已实测只有 **2048² → 4096²**（既是"2×"，
   也恰好等于 4k 档的 1:1 槽 4096²）⇒ 两种假设在**方形上无法区分**。
   而 4k 档**不是 2k 的整数倍**（服务端 `resolution_map`：16:9 是
   2560×1440 → **5404×3040**，比例 2.11×）。所以拿**非方形**源跑一次就能分辨：
     · 输出 **5404×3040** ⇒ 吸附到"同比例 4k 槽" ⇒ 任意比例都能这么改道；
     · 输出 **5120×2880** ⇒ 固定 2× ⇒ 非方形改道后**尺寸不等于 4k 档**，
       要么按实测尺寸交付并留痕，要么非方形不许改道。

### 🔴 结论（2026-10-12 实跑，submit_id `1be406f7-aa42-4648-aace-74ce1dbc9629`）

1. **引用形态成立** ⇒ 串链**不需要**"下载 2k 产物 → 上传换 image_uri"那一跳，
   直接用产物自带的 `item_id`（`GeneratedImage.item_id`）+ 源任务的
   `upstream_history_id` 即可（后者正是 `origin_history_id`）。
2. **超清 = 固定 2×**（按**源**尺寸翻倍，不是吸附到 4k 槽）：
   源 2560×1440 → 输出 **5120×2880**；与 2048²→4096² 自洽。
   ⇒ **只有 1:1 的 4k 改道能精确命中契约尺寸**（2048² → 4096² = 4k 槽 4096²）。
     16:9 的 4k 契约是 5404×3040，超清给 5120×2880 —— **两者都不是**：
     既不是 2k（2560×1440）也不是 4k 契约值。**非 1:1 的改道策略必须显式拍板。**
3. 本次提交**未在 `user_credit_history` 出账**（结算有延迟，>10 分钟）；
   该账号历史「图片生成」记录是 **3/张**（像是非会员档）⇒
   **省钱幅度取决于账号会员状态**，别把"Lite 2k 免费"当全局前提
   （那个 0 只在会员号、且只在 2k 档上实测过）。

用法：
    python scripts/hd_upscale_probe.py            # dry_run：打印草稿，零消耗
    python scripts/hd_upscale_probe.py --go       # 真提交 1 次

⛔ **红线**：`--go` 是真实提交，必须先有用户明确授权（本探针 2026-10-12 已获授权）。
   超清 UI 标价免费、实测零出账，但仍按"计费动作"对待：提交后按 `submit_id`
   去 `user_credit_history` 对账（结算有延迟 ⇒ 没看到就写"待定"，别断定免费）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.upstream.jimeng.client import (  # noqa: E402
    JimengClient,
    build_post_edit_draft,
)

#: 2026-09-20 抓包里 UI 用它成功做过细节修复的作品，**16:9 / 2560×1440**。
#: 正好是本探针要的"非方形 2k 源"。同一条也被 `scripts/detail_fix_probe.py` 使用。
ITEM_ID = 7687536700452588862
ORIGIN_HISTORY_ID = 44853933660428

TOOL = "normal_hd"
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 300.0
CREDIT_HISTORY_PATH = "/commerce/v1/benefits/user_credit_history"


def _load_dotenv() -> None:
    """把仓库根的 .env 灌进 os.environ（已有环境变量优先，绝不打印值）。"""
    p = REPO / ".env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _env(key: str, default: str = "") -> str:
    """从环境变量取（`_load_dotenv` 已把 .env 灌进来；已有环境变量优先）。
    🔴 `Settings` **刻意不带 sessionid**（2026-10-08 起"不从环境变量取
    sessionid"，凭据按请求经 Bearer 透传）⇒ 探针直接从 env 读，同
    `scripts/dump_video_models.py` 的做法。
    """
    return (os.environ.get(key) or default).strip()


def _fp(secret: str) -> str:
    """凭据指纹 —— **只打印指纹，绝不打印凭据本身**（同本服务的 `credential_id` 口径）。"""
    return hashlib.sha256(secret.encode()).hexdigest()[:12] if secret else "—"


def _buckets(cfg: dict) -> dict[str, list[dict]]:
    """服务端 `resolution_map`：桶名 → 该档的 `image_ratio_sizes` 数组。"""
    out: dict[str, list[dict]] = {}
    for it in ((cfg.get("data") or {}).get("model_list") or []):
        if not isinstance(it, dict):
            continue
        key = it.get("model_req_key") or ""
        if key != "high_aes_general_v50":          # 默认模型 Lite
            continue
        for bucket, entries in (it.get("resolution_map") or {}).items():
            arr = (entries.get("image_ratio_sizes")
                   if isinstance(entries, dict) else entries) or []
            out[str(bucket)] = [e for e in arr if isinstance(e, dict)]
    return out


def _match_ratio(w: int, h: int, entries: list[dict]) -> dict | None:
    """按长宽比在某一档里找同比例的槽位（比例比较用短边/长边，避免方向歧义）。"""
    if not entries or not w or not h:
        return None
    want = min(w, h) / max(w, h)
    best = min(entries,
               key=lambda e: abs(
                   min(e["width"], e["height"]) / max(e["width"], e["height"])
                   - want))
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true",
                    help="真提交（超清实测免费，但仍按计费动作对待）。缺省只 dry_run")
    ap.add_argument("--item-id", type=int, default=ITEM_ID)
    ap.add_argument("--history-id", type=int, default=ORIGIN_HISTORY_ID)
    ap.add_argument("--src-size", default="2560x1440",
                    help="源作品已知尺寸，用于判'固定 2×'（默认那条 16:9 作品）")
    args = ap.parse_args()

    _load_dotenv()
    sessionid = _env("JIMENG_SESSIONID")
    cookie = _env("JIMENG_COOKIE")
    ws = _env("JIMENG_WORKSPACE_ID")
    print(f"凭据指纹：sessionid={_fp(sessionid)} cookie={_fp(cookie)} "
          f"workspace={ws or '—'}")
    client = JimengClient(
        sessionid=sessionid, cookie=cookie,
        base=_env("JIMENG_BASE_URL", "https://jimeng.jianying.com"),
        workspace_id=int(ws) if ws and ws.isdigit() else None,
        poll_interval=float(_env("JIMENG_POLL_INTERVAL", "10") or 10),
        capture_upstream=False)

    # ---- 0. 只读：拿 2k/4k 档的真实像素，用来判"保不保比例" ----
    buckets: dict[str, list[dict]] = {}
    try:
        buckets = _buckets(client.common_config())
    except Exception as e:                      # noqa: BLE001
        print(f"  （能力表读取失败，不影响主结论：{type(e).__name__}: {e}）")

    # ---- 1. dry：引用形态自检（零消耗）----
    draft = build_post_edit_draft(
        tool=TOOL, item_id=args.item_id,
        origin_history_id=args.history_id, count=1)
    print("== 草稿形态（引用：无 origin_image，仅 item_id + origin_history_id）==")
    print(json.dumps(json.loads(draft), ensure_ascii=False, indent=2))
    comp = json.loads(draft)["component_list"][0]
    ab = comp["abilities"]["normal_hd"]
    pedit = ab["postedit_param"]
    assert "origin_image" not in json.dumps(comp), "引用形态不该带 origin_image"
    assert pedit["generate_type"] == 13, "normal_hd 的 postedit generate_type 必须是 13"
    assert pedit["item_id"] == args.item_id
    assert pedit["origin_history_id"] == args.history_id
    assert "core_param" not in ab, "normal_hd 的 ability 不带 core_param（只有 detail 带）"
    if not args.go:
        print("\n[dry_run] 形态自检通过（无 origin_image / generate_type=13 / "
              "无 core_param）。加 --go 真提交。")
        return 0

    # ---- 2. 真跑 ----
    sid = client.edit(tool=TOOL, item_id=args.item_id,
                      origin_history_id=args.history_id, count=1)
    print(f"\n== 已提交 submit_id={sid}，轮询到终态（≤{POLL_TIMEOUT:.0f}s）==")
    st = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        st = client.fetch(sid)
        print(f"  status={st.status} {st.status_name} finished={st.finished} "
              f"failed={st.failed} images={len(st.images)}")
        if st.finished:
            break
    else:
        print("  超时未到终态（任务可能仍在上游跑）")

    if st is not None and st.finished:
        if st.failed:
            print(f"\n❌ 引用形态被拒 / 失败：{st.failed_reason}")
            print("   ⇒ 结论：normal_hd 引用形态不成立 —— 串链只能走"
                  "「产物下载 → 上传换 image_uri」（已知可用，首次实测见 docs/UPSTREAM.md §9）。")
        else:
            print("\n✅ 引用形态成立。产物：")
            for im in st.images:
                print(f"  {im.url[:80]}…  {im.width}x{im.height}")
            # ---- 3. 判比例：与"固定 2×（按**源**尺寸）"、
            #         "吸附到同比例 4k 槽"两种假设对比 ----
            # 🔴 2026-10-12 实测结论：**固定 2×**（源 2560×1440 → 5120×2880）。
            # 注意 2× 要拿**源**尺寸算，别拿输出再乘（初版就是这么写错的）。
            print(f"\n== 输出尺寸判定（源：{args.src_size}）==")
            sw, sh = (int(x) for x in args.src_size.lower().split("x"))
            for im in st.images:
                if not (im.width and im.height):
                    continue
                w, h = im.width, im.height
                slot4 = _match_ratio(w, h, buckets.get("4k") or [])
                print(f"  实际输出 {w}x{h}")
                print(f"  源×2        = {sw * 2}x{sh * 2}"
                      f" ⇒ {'✅ 命中（固定 2×）' if (w, h) == (sw * 2, sh * 2) else '❌ 不命中'}")
                if slot4:
                    hit = (w == slot4["width"] and h == slot4["height"])
                    print(f"  同比例 4k 槽 = {slot4['width']}x{slot4['height']}"
                          f" ⇒ {'✅ 命中（吸附到 4k 档）' if hit else '❌ 不命中'}")
                break

    # ---- 4. 账单对账（只读；⚠️ records 嵌在 data 下，且结算有延迟）----
    print("\n== user_credit_history 近 10 条 ==")
    try:
        # `client._post` 是私有方法（SLF001）：对账脚本直调是刻意的 ——
        # 它只读、签名自动带，且公开 API 不含账单接口（同 docs/UPSTREAM.md §12 口径）。
        raw = client._post(  # noqa: SLF001
            CREDIT_HISTORY_PATH, {"count": 10, "cursor": "0", "history_type": 2})
        d = raw.get("data") or {}
        print(f"  total_credit={d.get('total_credit')}")
        for r in d.get("records") or []:
            print(f"  submit={r.get('submit_id')} amount={r.get('amount')} "
                  f"title={r.get('title')} t={r.get('create_time')}")
        if sid in json.dumps(d):
            print(f"  ↑ 本次提交 {sid} 出现在账单里（对上号）")
        else:
            print(f"  本次 {sid} 暂未出现在账单（结算有延迟，别急着下结论）")
    except Exception as e:                      # noqa: BLE001
        print(f"  账单查询失败（不影响主结论）：{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
