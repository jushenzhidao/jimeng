#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分辨率**档位单价**探针：同一账号上实测 2k vs 4k 的实扣（单变量）。

**为什么需要它**："4k 请求改走 2k 生成 + 超清放大"这条优化的**收益**
就是「4k 单价 − 2k 单价 − 超清单价」。而仓库现有的 4 / 0 是**会员号**上测的
（见 `docs/UPSTREAM.md` §19），"Lite 2k 免费"是**账号属性**不是模型属性 ——
换个账号（非会员底价实测 3/张）结论就变。⇒ **省钱幅度必须按目标账号重测。**

**单变量**：只变分辨率档。同 prompt / 同模型（Lite）/ 同 n=1 / 同为 1:1，
2k=2048×2048、4k=4096×4096。

用法：
    python scripts/probe_res_tier_price.py              # dry_run：只打印两份草稿
    python scripts/probe_res_tier_price.py --go         # 真提交 2 次（计费！）
    python scripts/probe_res_tier_price.py --check SID1,SID2   # 只读：对账（结算有延迟）

⚠️ 计费：Lite 2k 约 0~3 / 张、4k 约 4~7 / 张（随账号会员状态）。
   `--go` 必须用户明确授权（本探针 2026-10-12 已获授权）。
🔴 结算**有延迟**（>10 分钟）：提交后当即查不到**不代表免费**，
   要拿 `submit_id` 去 `user_credit_history` 三证对账（余额差分 + 消耗记录 + 任务 id）。
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
    build_draft,
)

MODEL = "high_aes_general_v50"          # Lite（默认模型）
PROMPT = "一只橘猫坐在窗台上晒太阳，水彩插画风格"
CASES = (("2k", "2048x2048"), ("4k", "4096x4096"))
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 300.0
CREDIT_HISTORY_PATH = "/commerce/v1/benefits/user_credit_history"


def _load_dotenv() -> None:
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
    return (os.environ.get(key) or default).strip()


def _fp(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12] if secret else "—"


def _client() -> JimengClient:
    sessionid = _env("JIMENG_SESSIONID")
    cookie = _env("JIMENG_COOKIE")
    ws = _env("JIMENG_WORKSPACE_ID")
    print(f"凭据指纹：sessionid={_fp(sessionid)} cookie={_fp(cookie)} "
          f"workspace={ws or '—'}")
    return JimengClient(
        sessionid=sessionid, cookie=cookie,
        base=_env("JIMENG_BASE_URL", "https://jimeng.jianying.com"),
        workspace_id=int(ws) if ws and ws.isdigit() else None,
        poll_interval=float(_env("JIMENG_POLL_INTERVAL", "10") or 10),
        capture_upstream=False)


def _balance(client: JimengClient) -> tuple[object, dict]:
    """只读余额（`records`/`total_credit` 嵌在响应的 `data` 下，不是顶层）。"""
    d = client._post(  # noqa: SLF001
        CREDIT_HISTORY_PATH, {"count": 8, "cursor": "0", "history_type": 2}
    ).get("data") or {}
    return d.get("total_credit"), d


def _poll(client: JimengClient, sid: str, label: str):
    print(f"  [{label}] 轮询 submit_id={sid} …")
    t0 = time.monotonic()
    st = None
    while time.monotonic() - t0 < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        st = client.fetch(sid)
        print(f"    status={st.status} {st.status_name} failed={st.failed} "
              f"images={len(st.images)}")
        if st.finished:
            return st
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="真提交（计费）。缺省只 dry_run")
    ap.add_argument("--check", metavar="SID1,SID2",
                    help="只读对账：这两个 submit_id 出账了吗")
    args = ap.parse_args()

    _load_dotenv()
    client = _client()

    # ---- 只读对账模式 ----
    if args.check:
        want = [s.strip() for s in args.check.split(",") if s.strip()]
        bal, d = _balance(client)
        print(f"\n余额 total_credit = {bal}")
        for r in d.get("records") or []:
            print(f"  submit={r.get('submit_id')} amount={r.get('amount')} "
                  f"title={r.get('title')} t={r.get('create_time')}")
        blob = json.dumps(d)
        for sid in want:
            print(f"  {sid}: {'✅ 已出账' if sid in blob else '尚未出现（延迟或未计费）'}")
        return 0

    # ---- dry：两份草稿的自检（零消耗）----
    print("== 两份草稿（单变量：只差分辨率档）==")
    for tier, size in CASES:
        d = json.loads(build_draft(prompt=PROMPT, model=MODEL, count=1,
                                   width=int(size.split("x")[0]),
                                   height=int(size.split("x")[1]),
                                   resolution_type=tier))
        core = json.dumps(d)
        assert f'"{tier}"' in core, f"{tier} 没落到草稿里"
        print(f"  [{tier}] size={size} resolution_type={tier} "
              f"（草稿里已带）")
    if not args.go:
        print("\n[dry_run] 单变量自检通过（同 prompt/模型/n，只差档位）。加 --go 真提交。")
        return 0

    # ---- 真跑：逐档提交 + 记录 submit_id（供事后三证对账）----
    before, d0 = _balance(client)
    print(f"\n== 提交前余额 total_credit={before} ==")
    results = []
    for tier, size in CASES:
        try:
            sid = client.submit(PROMPT, model=MODEL, size=size, count=1,
                                resolution_type=tier)
        except Exception as e:                  # noqa: BLE001
            bal, _ = _balance(client)
            results.append((tier, size, "<提交被拒>", f"{type(e).__name__}: {e}",
                            0, [], bal))
            print(f"  [{tier}] ❌ 提交被拒：{type(e).__name__}: {e}")
            print(f"     余额读数={bal}")
            if "1006" in str(e) or "credit_not_enough" in str(e):
                print("     ⇒ 1006 = **积分不足 或 没有该档权益**（两种含义都写在文案里）。"
                      "本条只能记'在余额 X 下不可提交'，**不能反推单价**；"
                      "要单价请在 UI 里读该档的显示价，或充值后再测。")
            continue
        st = _poll(client, sid, tier)
        sizes = [(im.width, im.height) for im in (st.images if st else [])]
        bal, _ = _balance(client)
        results.append((tier, size, sid,
                        getattr(st, "status_name", "?"),
                        len(st.images) if st else 0, sizes, bal))
        print(f"  [{tier}] submit_id={sid} 产物 {sizes} 此刻余额={bal}")
        time.sleep(3.0)          # 别贴着上游风控的节奏连发

    print("\n== 汇总（⚠️ 结算有延迟：此刻余额未动 ≠ 免费）==")
    for tier, size, sid, status, n, sizes, bal in results:
        print(f"  {tier:<3} size={size:<10} submit_id={sid} status={status} "
              f"n={n} 产物={sizes} 余额读数={bal}")
    ok_ids = [r[2] for r in results if not str(r[2]).startswith("<")]
    if ok_ids:
        print("\n事后对账（建议本次提交后 >10 分钟再跑）：")
        print(f"  python scripts/probe_res_tier_price.py --check {','.join(ok_ids)}")
    print("\n判据（三证吻合才算数）：余额差分 + 消耗记录 `amount` + submit_id。")
    print("⚠️ 同账号可能有人并发在用（见 scripts/credit_probe.py 的教训）——"
          "只认 submit_id 对上号的记录，别按时间戳猜。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
