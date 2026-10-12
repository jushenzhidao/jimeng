#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4k 改道**端到端**多形态测试（打**本服务**的 HTTP 接口，不是直连上游）。

用途：验证「请求 4k ⇒ 自动改走 2k 生成 + 超清就地放大」这条链在**真实部署**上
是否成立，并**逐形态**核对交付尺寸。

覆盖四个形态（用户口径：t2i + i2i 都覆盖；非 1:1 按输入比例就地放大）：

| 用例 | 能力 | 请求 size | 期望交付（= 2k 槽 ×2） | 与 4k 契约槽 |
|---|---|---|---|---|
| 1:1 多图 | t2i n=2 | 4096x4096 | 2 张 **4096×4096** | ✅ 完全一致 |
| 16:9 | t2i n=1 | 5404x3040 | 5120×2880 | ≠ |
| 9:16 竖 | t2i n=1 | 3040x5404 | 2880×5120 | ≠（**方向**回归） |
| i2i 1:1 | i2i n=1 | 4096x4096 | 4096×4096 | ✅ |

⚠️ **本脚本会真花钱**（每张 2k 生成按账号单价计，本次账号实测 3/张；
超清腿免费）⇒ 默认 dry_run，真跑必须显式 `--go`。

🔴 宽高**不从这个接口读**：`GET` 的 `data[]` **只有 `url`**（契约如此，见
`docs/INTERFACE.md` §2.2）。要核尺寸得下载产物量像素、或查 `tasks` 表的
`images`（本脚本走前者，自包含、不依赖数据库访问）。

用法::

    # 只看要发什么（零消耗）
    python scripts/e2e_4k_multiform.py

    # 真跑（计费）
    JIMENG_SESSIONID=<sessionid> python scripts/e2e_4k_multiform.py --go

    # 指定入口（默认公网）
    ... --base https://jimeng.1task.cn
"""
from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import time
import urllib.request

REPO = __file__.rsplit("/", 2)[0]
BASE_DEFAULT = "https://jimeng.1task.cn"
SUBMIT = "/async/v1/images/generations"
POLL_INTERVAL = 6.0
POLL_TIMEOUT = 420.0

#: `--only` 的取值 → 计划里标签的前缀（i2i 不在 plan 里，单独处理）
_ONLY_PREFIX = {"1x1": "1:1", "16x9": "16:9", "9x16": "9:16"}

PROMPTS = {
    "1x1": "一只橘猫坐在木窗台上晒太阳，水彩插画风格，暖色调",
    "16x9": "黄昏的海岸线与远处的灯塔，胶片摄影风格，宽幅构图",
    "9x16": "雪山与松林，清晨薄雾，油画风格，竖幅构图",
    "i2i": "把这张画面改成夜晚，加一片星空，保持构图",
}


def _req(url: str, *, body: dict | None = None, credential: str,
         method: str = "POST", timeout: float = 60.0) -> tuple[int, dict | str]:
    """打本服务。🔴 必须绕开代理（本机 `HTTP_PROXY` 连内网域名也会代理走）。"""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {credential}")
    if data:
        r.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except Exception:                            # noqa: BLE001
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:                                # noqa: BLE001
            return e.code, raw.decode("utf-8", "replace")


def image_size(url: str) -> tuple[int, int] | None:
    """只下载**头部**读像素（`Range` 取前 64KB 足够容纳 PNG/JPEG 的头部）。

    自包含、不依赖数据库；失败返回 None（**不猜**，别把"读不到"当"尺寸对"）。
    """
    r = urllib.request.Request(url)
    r.add_header("Range", "bytes=0-65535")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=60) as resp:
            buf = resp.read()
    except Exception as e:                               # noqa: BLE001
        print(f"      （尺寸读取失败：{type(e).__name__}: {e}）")
        return None
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", buf[16:24])
        return int(w), int(h)
    if buf[:2] == b"\xff\xd8":                           # JPEG：扫 SOFn
        i = 2
        while i < len(buf) - 9:
            if buf[i] != 0xFF:
                i += 1
                continue
            m = buf[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", buf[i + 5:i + 9])
                return int(w), int(h)
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", buf[i + 2:i + 4])[0]
            i += 2 + seg
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        if buf[12:16] == b"VP8X":
            w = int.from_bytes(buf[24:27], "little") + 1
            h = int.from_bytes(buf[27:30], "little") + 1
            return w, h
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="真跑（**会花钱**）。缺省只打印计划")
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--credential", default="",
                    help="本服务的 Bearer 凭据（即 sessionid 透传）；或设 JIMENG_SESSIONID")
    ap.add_argument("--only", choices=["1x1", "16x9", "9x16", "i2i"],
                    help="只跑一个形态 —— **定向复现**用（省积分）。缺省跑全部 4 形态")
    ap.add_argument("--input-url", default="",
                    help="`--only i2i` 的垫图 URL；不给则用本轮 t2i 的第 1 张产物")
    args = ap.parse_args()

    cred = args.credential or _env("JIMENG_SESSIONID")
    if not cred:
        print("缺少凭据：--credential 或 JIMENG_SESSIONID")
        return 2

    plan = [
        ("1:1 多图 (t2i n=2)", {"model": "jimeng-t2i", "prompt": PROMPTS["1x1"],
                                "size": "4096x4096", "n": 2}, (4096, 4096), 2),
        ("16:9    (t2i n=1)", {"model": "jimeng-t2i", "prompt": PROMPTS["16x9"],
                               "size": "5404x3040", "n": 1}, (5120, 2880), 1),
        ("9:16 竖 (t2i n=1)", {"model": "jimeng-t2i", "prompt": PROMPTS["9x16"],
                               "size": "3040x5404", "n": 1}, (2880, 5120), 1),
    ]
    i2i = ("i2i 1:1 (n=1)",
           {"model": "jimeng-i2i", "prompt": PROMPTS["i2i"],
            "size": "4096x4096", "n": 1}, (4096, 4096))

    # `--only`：定向复现。典型用途 —— 某形态的**超清腿**偶发失败过，
    # 要判"偶发 vs 系统性"（全量重跑太贵）。
    only = args.only
    if only == "i2i":
        plan = []                      # i2i 走"追加"那一段，不上 t2i
    elif only:
        plan = [x for x in plan if x[0].startswith(_ONLY_PREFIX[only])]
    run_i2i = (only is None) or (only == "i2i")

    total = sum(n for _, _, _, n in plan) + (1 if run_i2i else 0)
    print(f"== 计划（{'全部 4 形态' if not only else f'只跑 {only}'}；"
          f"超清腿免费、2k 生成腿按账号单价）==")
    for label, body, want, n in plan:
        print(f"  {label}: size={body['size']} n={n} ⇒ 期望交付 {n}×{want[0]}×{want[1]}")
    if run_i2i:
        src = args.input_url or "用例 1 的产物"
        print(f"  {i2i[0]}: size={i2i[1]['size']} ⇒ 期望交付 "
              f"{i2i[2][0]}×{i2i[2][1]}（垫图 = {src}）")
    print(f"  ⇒ 合计 {total} 张 2k 生成；账号实测 3/张 ⇒ 预计 {total * 3} 积分")
    if not args.go:
        print("\n[dry_run] 未发任何请求（零消耗）。加 --go 真跑。")
        return 0

    print(f"\n== 提交（{args.base}）==")
    tasks: list[tuple[str, str, tuple[int, int], int]] = []
    for label, body, want, n in plan:
        code, r = _req(args.base + SUBMIT, body=body, credential=cred)
        if code != 202 or not isinstance(r, dict) or "task_id" not in r:
            print(f"  ✗ {label}: HTTP {code} {r}")
            continue
        print(f"  ✓ {label}: task_id={r['task_id']}")
        tasks.append((label, r["task_id"], want, n))

    if not tasks and plan:
        print("没有任何任务被受理，终止。")
        return 1
    if not tasks:                       # `--only i2i`：本段无事可做
        print("（只跑 i2i：本次不发 t2i）")

    print("\n== 轮询到终态 ==")
    done: dict[str, dict] = {}
    t0 = time.monotonic()
    pending = {t[1] for t in tasks}
    while pending and time.monotonic() - t0 < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        for tid in sorted(pending):
            code, r = _req(f"{args.base}{SUBMIT}/{tid}", credential=cred,
                           method="GET")
            st = r.get("status") if isinstance(r, dict) else f"HTTP {code}"
            if st in ("success", "failure", "canceled"):
                done[tid] = r if isinstance(r, dict) else {"status": st}
                pending.discard(tid)
                print(f"  {tid} → {st}")
    for tid in pending:
        print(f"  {tid} → 超时未到终态")
        done[tid] = {"status": "timeout"}

    print("\n== 交付核对 ==")
    first_url = ""
    ok = bad = 0
    for label, tid, want, n in tasks:
        r = done.get(tid) or {}
        st = r.get("status")
        urls = [d.get("url") for d in (r.get("data") or [])]
        print(f"\n  【{label}】task={tid} status={st} 张数={len(urls)}")
        if not first_url and urls:
            first_url = urls[0]
        for u in urls:
            dim = image_size(u)
            mark = "✓" if dim == want else "✗"
            if dim == want:
                ok += 1
            else:
                bad += 1
            print(f"    {mark} 期望 {want[0]}×{want[1]}  实际 {dim}")
        if isinstance(r.get("error"), dict):
            print(f"    error: {r['error']}")

    first_url = first_url or args.input_url
    if first_url:
        src = "--input-url" if args.input_url else "上面第 1 张产物"
        print(f"\n== 追加 i2i（垫图 = {src}）==")
        label, body, want = i2i
        body = {**body, "image": [first_url]}
        code, r = _req(args.base + SUBMIT, body=body, credential=cred)
        if code != 202 or not isinstance(r, dict) or "task_id" not in r:
            print(f"  ✗ {label}: HTTP {code} {r}")
        else:
            tid = r["task_id"]
            print(f"  ✓ {label}: task_id={tid}")
            st = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < POLL_TIMEOUT:
                time.sleep(POLL_INTERVAL)
                _c, rr = _req(f"{args.base}{SUBMIT}/{tid}", credential=cred,
                              method="GET")
                st = rr.get("status") if isinstance(rr, dict) else None
                if st in ("success", "failure", "canceled"):
                    break
            urls = [d.get("url") for d in ((rr or {}).get("data") or [])]
            print(f"    status={st} 张数={len(urls)}")
            for u in urls:
                dim = image_size(u)
                mark = "✓" if dim == want else "✗"
                if dim == want:
                    ok += 1
                else:
                    bad += 1
                print(f"    {mark} 期望 {want[0]}×{want[1]}  实际 {dim}")

    if run_i2i and not first_url:
        # 别静默跳过 —— 少了 i2i 就等于这个形态**没测**
        print("\n⚠️ 没有可用垫图（t2i 全失败且未给 `--input-url`）⇒ **i2i 形态未测**")

    print(f"\n== 小结：尺寸符合 {ok} 张 / 不符 {bad} 张 ==")
    print("  （改道是否生效、每张走了哪条腿，看服务端 `degradations`："
          "`GET /async/v1/images/generations` 列表或 `tasks` 表；"
          "账单对账 `scripts/probe_res_tier_price.py --check <submit_id,...>`）")
    return 0 if bad == 0 else 1


def _env(key: str, default: str = "") -> str:
    import os
    v = os.environ.get(key)
    if v:
        return v.strip()
    p = REPO + "/.env"
    try:
        for line in io.open(p, encoding="utf-8").read().splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return default


if __name__ == "__main__":
    sys.exit(main())
