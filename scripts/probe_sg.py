#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国际版 Dreamina（dreamina.capcut.com → mweb-api-sg.capcut.com）零成本探针。

只打**读接口**，不建任务、不计费：
  1. POST /mweb/v1/get_common_config   —— 能力表（body={}）
  2. POST /mweb/v1/get_history_by_ids  —— 用不存在的 submit_id（应回空而非报错）
  3. 负对照：去掉 sign 头再打 get_common_config（验 sign 是否参与鉴权）

验证的假设（来自 2026-10-15 用户抓包）：
  · sign 算法与国内同款（已本地验算命中，见 git log）
  · 域名/aid/da_version/region 差异是否足够（sessionid-only 能否通）
  · msToken/X-Bogus/X-Gnarly 是否强校验（负对照间接验证）

用法：
  python scripts/probe_sg.py --sessionid <sid> [--proxy http://127.0.0.1:7890]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from urllib.parse import urlsplit

import httpx

# ---- 国际版站点常量（全部来自 2026-10-15 抓包） ----
BASE = "https://mweb-api-sg.capcut.com"
SITE_ORIGIN = "https://dreamina.capcut.com"
APPID = "513641"
APPVR = "8.4.0"
PF = "7"
DA_VERSION = "3.3.30"
WEB_VERSION = "7.5.0"
APP_SDK_VERSION = "48.0.0"
REGION = "PH"          # 抓包 region=PH（跟随账号地区）
LOC = "PH"
STORE_CC = "ph"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

PATH_CONFIG = "/mweb/v1/get_common_config"
PATH_HISTORY = "/mweb/v1/get_history_by_ids"


def sign(pathname: str, ts: int) -> str:
    """与国内 sign.py 同款：md5("9e2c|{path[-7:]}|{pf}|{appvr}|{ts}|{tdid}|11ac")。"""
    payload = f"9e2c|{pathname[-7:]}|{PF}|{APPVR}|{ts}||11ac"
    return hashlib.md5(payload.encode()).hexdigest().lower()


def url(path: str) -> str:
    q = {
        "aid": APPID, "device_platform": "web", "region": REGION,
        "da_version": DA_VERSION, "os": "mac",
        "web_component_open_flag": "1", "web_version": WEB_VERSION,
        "aigc_features": "app_lip_sync",
    }
    return f"{BASE}{path}?" + "&".join(f"{k}={v}" for k, v in q.items())


def headers(path: str, *, with_sign: bool = True) -> dict[str, str]:
    pathname = urlsplit(path).path
    ts = int(time.time())
    h = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "app-sdk-version": APP_SDK_VERSION,
        "appid": APPID,
        "appvr": APPVR,
        "content-type": "application/json",
        "lan": "zh-Hant-TW",
        "loc": LOC,
        "origin": SITE_ORIGIN,
        "pf": PF,
        "priority": "u=1, i",
        "referer": f"{SITE_ORIGIN}/",
        "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", '
                     '"Chromium";v="153"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "store-country-code": STORE_CC,
        "store-country-code-src": "uid",
        "tdid": "",
        "user-agent": UA,
    }
    if with_sign:
        h["sign"] = sign(pathname, ts)
        h["sign-ver"] = "1"
        h["device-time"] = str(ts)
    return h


def post(client: httpx.Client, path: str, body: dict, *,
         with_sign: bool = True) -> None:
    """打一发，原样打印 ret/errmsg 与体量摘要。"""
    tag = f"{path}{' [无sign负对照]' if not with_sign else ''}"
    try:
        r = client.post(url(path), headers=headers(path, with_sign=with_sign),
                        content=json.dumps(body, ensure_ascii=False).encode())
    except Exception as e:  # noqa: BLE001 —— 探针要把网络错误打出来
        print(f"❌ {tag}\n   网络错误: {e!r}")
        return
    print(f"→ {tag}")
    print(f"   HTTP {r.status_code}, {len(r.content)}B")
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        print(f"   非 JSON: {r.text[:200]!r}")
        return
    ret = data.get("ret")
    errmsg = data.get("errmsg")
    print(f"   ret={ret!r} errmsg={errmsg!r}")
    d = data.get("data")
    if isinstance(d, dict):
        keys = list(d.keys())
        print(f"   data keys({len(keys)}): {keys[:20]}")
        # 上传 token 专项：STS 与 upload_domain 指向哪家
        for k in ("upload_domain", "space_name", "service_id"):
            if k in d:
                print(f"   {k} = {d[k]!r}")
        # 能力表里找国际版模型名，确认模型体系
        raw = json.dumps(d, ensure_ascii=False)
        for probe in ("dreamina_gpt_image_2_5_flare", "high_aes_general_v50",
                      "seedream", "image_ratio", "generate_count"):
            print(f"   含 {probe!r}: {probe in raw}")
    else:
        print(f"   data: {json.dumps(d, ensure_ascii=False)[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessionid", required=True)
    ap.add_argument("--proxy", default=None,
                    help="如 http://127.0.0.1:7890；缺省直连")
    ap.add_argument("--cookie", default="",
                    help="可选：完整 cookie 串（默认只带 sessionid）")
    a = ap.parse_args()

    cookie = a.cookie or f"sessionid={a.sessionid}"
    client = httpx.Client(
        timeout=20.0, trust_env=bool(a.proxy),
        proxy=a.proxy, cookies=_parse_cookie(cookie),
        headers={"user-agent": UA})
    print(f"== 国际版探针 base={BASE} aid={APPID} "
          f"cookie 长度={len(cookie)} ==")
    post(client, PATH_CONFIG, {})
    post(client, PATH_HISTORY, {"submit_ids": ["probe-nonexistent-0"]})
    # 上传链路前置：get_upload_token(scene=2) 只取 STS 不上传，零成本
    post(client, "/mweb/v1/get_upload_token", {"scene": 2})
    post(client, PATH_CONFIG, {}, with_sign=False)  # 负对照
    client.close()
    return 0


def _parse_cookie(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


if __name__ == "__main__":
    sys.exit(main())
