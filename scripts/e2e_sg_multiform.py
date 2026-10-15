#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国际版 Dreamina（SG 站）**多形态**测试 —— 契约照抄 `e2e.py` 的纪律：

| 阶段 | 花积分 | 做什么 |
|---|---|---|
| `dry` | ❌ | **纯本地**：5 个国际模型 × 张数吸附 × 尺寸档的草稿构造矩阵（一次网络都不发）|
| `accept` | ❌ | 本地起服务（**协调器关闭**）受理多形态请求：只落本地库 + 零成本读能力表，**不提交上游** |
| `config` | ❌ | 用国际站**真实能力表**（已 dump）喂解析层，验证张数/分辨率/4k 改道判据 |
| `generate` | 🔴 | 真建任务 + 轮询到终态，**必须 `--allow-real-submit`**（国际站价格未对账，消耗未知）|

用法：
  python scripts/e2e_sg_multiform.py --sessionid <sid>                 # 默认 dry+config+accept
  python scripts/e2e_sg_multiform.py --sessionid <sid> --phases dry    # 只跑本地矩阵
  python scripts/e2e_sg_multiform.py --sessionid <sid> --phases generate --allow-real-submit
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.upstream.jimeng.client import JimengClient
from app.upstream.jimeng.site import SG_SITE

SG_MODELS = (
    # (别名, 上游 key, counts 声明)
    ("gpt-image-2-5-flare", "dreamina_gpt_image_2_5_flare", (1,)),
    ("gpt-image-2-5-sunburst", "dreamina_gpt_image_2_5_sunburst", (1,)),
    ("gpt-image-2", "gpt_image_2", (1,)),
    ("nano-banana-pro", "gemini_3_image_pro", (1, 2, 3, 4)),
    ("nano-banana-2-1", "jm_image_model_gg_bp_21", (1, 2, 3, 4)),
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✅" if cond else "❌"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {mark} {name}" + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------------------
# dry：纯本地草稿构造矩阵
# ---------------------------------------------------------------------------

def phase_dry() -> None:
    print("== dry：草稿构造矩阵（零网络）==")
    from app.upstream.jimeng.client import build_draft, resolve_count

    def draft_of(key: str, count: int, size: str, declared):
        """与 client.submit(dry_run=True) 同一构造路径（dry_run 不落 last_draft）。

        `count_options` 显式传能力表声明 —— 与生产 `cfg.count_options()` 同源。
        """
        n, warn = resolve_count(key, count, options=declared)
        w, h = (int(x) for x in size.split("x"))
        return (json.loads(build_draft(prompt="a cat", model=key, count=n,
                                       width=w, height=h)), warn)

    for alias, key, declared in SG_MODELS:
        # ① n=1 全部模型都该原样
        core, _ = draft_of(key, 1, "2048x2048", declared)
        got = core["component_list"][0]["abilities"]["gen_option"]["gen_count"]
        check(f"{alias} n=1 原样", got == 1, f"gen_count={got}")

        # ② 张数吸附：counts=[1] 的模型给 n=4 必须吸到 1 并留痕；
        #    counts=[1,2,3,4] 的给 n=4 应原样 4
        core, warn = draft_of(key, 4, "2048x2048", declared)
        got = core["component_list"][0]["abilities"]["gen_option"]["gen_count"]
        if declared == (1,):
            check(f"{alias} n=4 吸附→1", got == 1 and warn,
                  f"gen_count={got} warn={warn}")
        else:
            check(f"{alias} n=4 原样", got == 4 and not warn,
                  f"gen_count={got} warn={warn}")

    # ③ 尺寸档：1k / 2k / 4k 的 resolution_type
    for size, want in (("1024x1024", "1k"), ("2048x2048", "2k"),
                       ("4096x4096", "4k")):
        core, _ = draft_of("dreamina_gpt_image_2_5_flare", 1, size, (1,))
        li = (core["component_list"][0]["abilities"]["generate"]
              ["core_param"]["large_image_info"])
        check(f"flare {size} → {want}", li["resolution_type"] == want,
              f"got {li['resolution_type']}")

    # ④ draft version = 国际站 3.3.30（da_version 按 profile 传入）
    d = json.loads(build_draft(prompt="a cat", model="gpt_image_2", count=1,
                               da_version=SG_SITE.da_version))
    check("draft version=3.3.30", d.get("version") == "3.3.30",
          str(d.get("version")))


def _core(draft: str) -> dict:
    d = json.loads(draft)
    comp = d["component_list"][0]
    return {"gen_count": comp["abilities"]["gen_option"]["gen_count"],
            **comp["abilities"]["generate"]["core_param"]}


# ---------------------------------------------------------------------------
# config：真实能力表喂解析层
# ---------------------------------------------------------------------------

def phase_config() -> None:
    print("== config：国际站真实能力表 → 解析层 ==")
    from app.upstream.jimeng.capabilities import parse_common_config

    path = "/tmp/sg_common_config.json"
    if not os.path.exists(path):
        print(f"  ⚠️ 缺 {path}（先跑 probe_sg.py 存一份）—— 跳过")
        return
    data = json.load(open(path))
    # dump 存的是 data 部分；解析器吃**完整响应信封**（ret/errmsg/data）
    specs, notes = parse_common_config(
        {"ret": "0", "errmsg": "success", "data": data})
    check("解析出 15 个模型", len(specs) == 15, str(len(specs)))
    for _, key, declared in SG_MODELS:
        spec = specs.get(key)
        check(f"{key} 在表中", spec is not None)
        if spec:
            check(f"{key} counts={declared}",
                  tuple(spec.count_options or ()) == declared,
                  f"got {spec.count_options}")
    # 4k 槽：flare 系声明了 4k（4096²）⇒ 4k 改道判据的"两槽同在"在国际站成立
    from app.upstream.jimeng.capabilities import slot_for_ratio_in
    flare = specs.get("dreamina_gpt_image_2_5_flare")
    if flare:
        rmap = flare.resolution_map or {}
        s2k = slot_for_ratio_in(rmap, "2k", 4096, 4096)
        s4k = slot_for_ratio_in(rmap, "4k", 4096, 4096)
        check("flare 有 2k+4k 槽（4096² 请求可吸附）",
              s2k == (2048, 2048) and s4k == (4096, 4096),
              f"2k={s2k} 4k={s4k}")
        # 垫图上限（byte_edit）
        lim = (flare.input_image_limits or {}).get("byte_edit")
        check("flare byte_edit 上限=16", lim == 16, str(lim))


# ---------------------------------------------------------------------------
# accept：本地服务受理矩阵（协调器关闭 ⇒ 零上游提交）
# ---------------------------------------------------------------------------

ACCEPT_MATRIX = (
    # (说明, body)
    ("t2i flare 1k（抓包原形态）",
     {"model": "gpt-image-2-5-flare", "prompt": "a cat",
      "size": "1024x1024"}),
    ("t2i flare 2k",
     {"model": "gpt-image-2-5-flare", "prompt": "a cat",
      "size": "2048x2048"}),
    ("t2i nano-banana-pro n=2",
     {"model": "nano-banana-pro", "prompt": "a cat", "n": 2}),
    ("t2i 共享模型 Seedream 5.0 Lite（国际凭据）",
     {"model": "Seedream 5.0 Lite", "prompt": "a cat"}),
    ("t2i 默认模型（不写 model）",
     {"prompt": "a cat"}),
    ("t2i flare 4k（改道观察）",
     {"model": "gpt-image-2-5-flare", "prompt": "a cat",
      "size": "4096x4096"}),
)


def phase_accept(sessionid: str) -> None:
    print("== accept：本地服务受理矩阵（协调器关闭，零上游提交）==")
    eval_pg = os.popen("scripts/local-pg.sh dsn").read()
    dsn = ""
    for line in eval_pg.splitlines():
        if line.startswith("TEST_DATABASE_URL="):
            dsn = line.split("=", 1)[1]
    assert dsn, "起本地 PG 失败（scripts/local-pg.sh start）"
    os.environ["TEST_DATABASE_URL"] = dsn

    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    st = Settings.from_env().replace(
        coordinator_enabled=False, task_db=dsn,
        otel_token="", normalize_uploads=False)
    app = create_app(st)
    auth = {"Authorization": f"Bearer {sessionid}"}
    with TestClient(app) as client:
        for name, body in ACCEPT_MATRIX:
            r = client.post("/async/v1/images/generations", json=body,
                            headers=auth)
            ok = r.status_code == 202
            detail = ""
            if ok:
                task_id = r.json().get("task_id", "")
                detail = task_id[:24]
                # 查回任务确认模型解析落库
                r2 = client.get(f"/async/v1/images/generations/{task_id}",
                                headers=auth)
                if r2.status_code == 200:
                    j = r2.json()
                    detail += f" model={j.get('model')}"
                    if j.get("degradations"):
                        detail += f" deg={j['degradations'][:1]}"
            else:
                detail = f"{r.status_code} {r.text[:120]}"
            check(name, ok, detail)


# ---------------------------------------------------------------------------
# generate：真实生成（计费！必须 --allow-real-submit）
# ---------------------------------------------------------------------------

def phase_generate(sessionid: str) -> None:
    """真实链路实验（🔴 计费）：**最小发量** —— 1 次 flare 1k 生成 + 最多 2 次
    normal_hd 超分，验证三件事：
      ① 国际站超清腿（normal_hd 引用形态）是否可用；
      ② 超分的**实际放大倍率**（对 1k 输入出 2048 还是直接 4096？
         —— 决定 4k 是两级串联还是一级到位）；
      ③ 超分的**计费**（forecast 与 cost，国际站价格未对账）。
    """
    print("== generate：真实链路实验（🔴 计费动作，最小发量）==")
    c = JimengClient(sessionid=sessionid, profile=SG_SITE, poll_interval=3.0,
                     poll_timeout=300.0)

    # ① 生成腿：flare 1k×1
    print("  → [gen] flare 1k×1 提交 …")
    sid = c.submit("a small red cube on white background",
                   model="dreamina_gpt_image_2_5_flare",
                   size="1024x1024", count=1, count_options=(1,))
    st = c.wait(sid)
    imgs = st.images
    print(f"    status={st.status_name} images={len(imgs)} cost={st.cost}")
    check("generate flare 1k", st.ok and len(imgs) == 1,
          f"{st.status_name} cost={st.cost}")
    if not (st.ok and imgs):
        c.close()
        return
    img = imgs[0]
    print(f"    产物: {img.width}x{img.height} item_id={img.item_id} "
          f"url={img.url[:80]}")

    hist = getattr(st, "history_record_id", None)
    if not (img.item_id and hist):
        print("    ❌ 缺 item_id / history_record_id —— 超分腿引用形态的前提缺失")
        c.close()
        return

    # ② 超分腿（对 1k 产物发 normal_hd）—— 看倍率与计费
    def one_upscale(tag: str, item_id: int, hist_id: str) -> None:
        print(f"  → [{tag}] normal_hd 引用 item={item_id} hist={hist_id} …")
        esid = c.edit("normal_hd", item_id=item_id, origin_history_id=hist_id,
                      count=1, count_options=(1,))
        est = c.wait(esid)
        eimg = est.images[0] if est.images else None
        print(f"    status={est.status_name} cost={est.cost} "
              f"out={eimg.width}x{eimg.height}" if eimg else
              f"    status={est.status_name} cost={est.cost} 无产物")
        check(f"upscale {tag}", est.ok and eimg is not None,
              f"{est.status_name} → {eimg.width}x{eimg.height}" if eimg
              else f"{est.status_name}")

    one_upscale("L1", int(img.item_id), str(hist))
    c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessionid", default="")
    ap.add_argument("--phases", default="dry,config,accept")
    ap.add_argument("--allow-real-submit", action="store_true")
    a = ap.parse_args()
    phases = [p.strip() for p in a.phases.split(",") if p.strip()]

    if "dry" in phases:
        phase_dry()
    if "config" in phases:
        phase_config()
    if "accept" in phases:
        assert a.sessionid, "accept 阶段需要 --sessionid（凭据透传受理）"
        phase_accept(a.sessionid)
    if "generate" in phases:
        if not a.allow_real_submit:
            print("🔴 generate 是计费动作：加 --allow-real-submit 才会真发。"
                  "国际站价格未对账，消耗未知。")
            return 2
        phase_generate(a.sessionid)

    print(f"\n结果：{PASS} 过 / {FAIL} 挂")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
