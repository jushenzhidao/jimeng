#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国际站 generate 被 shark 风控拒后的**逐因素排查**（每步 1 次真实提交，
用户已授权真实测试；都是 flare 1k×1 最小单）。

因素矩阵（逐步叠加，找出 shark 在意哪个）：
  A. sessionid-only + 随机 did          （已测：ret=-6 shark not pass）
  B. 完整抓包 cookie + 随机 did
  C. 完整抓包 cookie + 抓包 did 头
  D. C + 抓包 query 里的 babi_param/generate_id 形态

用法：python scripts/probe_sg_shark.py --cookie-file <完整cookie串文件>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

sys.path.insert(0, __file__.rsplit("/", 2)[0])

import httpx

from app.upstream.jimeng.client import build_draft
from app.upstream.jimeng.sign import sign_headers
from app.upstream.jimeng.site import SG_SITE

BASE = SG_SITE.base
AID = SG_SITE.aid


def hdrs(path: str, did: str, cookie_extra: dict) -> dict:
    h = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "app-sdk-version": "48.0.0",
        "appid": AID,
        "appvr": "8.4.0",
        "content-type": "application/json",
        "lan": "zh-Hant-TW",
        "loc": "PH",
        "origin": SG_SITE.origin,
        "pf": "7",
        "priority": "u=1, i",
        "referer": f"{SG_SITE.origin}/",
        "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "store-country-code": "ph",
        "store-country-code-src": "uid",
        "tdid": "",
        "did": did,
        "device-time": "",
        "sign": "",
        "sign-ver": "1",
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36"),
    }
    h.update(cookie_extra)
    return h


def submit(client: httpx.Client, cookies: dict, did: str,
           tag: str) -> None:
    path = "/mweb/v1/aigc_draft/generate"
    sid = str(uuid.uuid4())
    draft = build_draft(prompt="a small red cube on white background",
                        model="dreamina_gpt_image_2_5_flare", count=1,
                        width=1024, height=1024, da_version=SG_SITE.da_version)
    body = {
        "extend": {"root_model": "dreamina_gpt_image_2_5_flare"},
        "submit_id": sid,
        "metrics_extra": json.dumps(
            {"promptSource": "custom", "generateCount": 1, "generateId": sid,
             "templateId": "0", "enterFrom": "click", "isRegenerate": False,
             "isBoxSelect": False, "isCutout": False},
            separators=(",", ":")),
        "draft_content": draft,
        "http_common_info": {"aid": int(AID)},
    }
    q = ("aid=513641&device_platform=web&region=PH&da_version=3.3.30&os=mac"
         "&web_component_open_flag=1&web_version=7.5.0"
         "&aigc_features=app_lip_sync")
    h = hdrs(path, did, {})
    sh = sign_headers(path)
    h["sign"] = sh["sign"]
    h["device-time"] = sh["device-time"]
    r = client.post(f"{BASE}{path}?{q}", headers=h,
                    content=json.dumps(body, ensure_ascii=False).encode())
    try:
        d = r.json()
    except Exception:  # noqa: BLE001
        d = {"raw": r.text[:150]}
    print(f"[{tag}] HTTP {r.status_code} ret={d.get('ret')!r} "
          f"errmsg={str(d.get('errmsg'))[:60]!r} "
          f"data={json.dumps(d.get('data'), ensure_ascii=False)[:120]}")
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", required=True,
                    help="完整 cookie 串（抓包原样，分号分隔）")
    a = ap.parse_args()
    raw = open(a.cookie_file).read().strip()

    cap_did = "7696496563418203668"   # 抓包里的 did
    rnd_did = str(79649656341820366)  # 随机同长度

    client = httpx.Client(timeout=30.0, trust_env=False)
    cookies_all = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies_all[k.strip()] = v.strip()

    print("== B: 完整 cookie + 随机 did ==")
    submit(client, cookies_all, rnd_did, "B")
    time.sleep(2)
    print("== C: 完整 cookie + 抓包 did ==")
    submit(client, cookies_all, cap_did, "C")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
