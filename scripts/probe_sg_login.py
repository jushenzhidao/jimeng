#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：验证 `Bearer 邮箱|密码` 的真实登录链路（**零计费**）。

用法：

    SG_LOGIN_PROBE='user@example.com|password' \
        python scripts/probe_sg_login.py

只发两类请求：
  1. CapCut passport 登录（一次；成功后进程内缓存）；
  2. `get_history_by_ids` —— 读接口 + 假 submit_id ⇒ 空结果、**零额度**。

**不创建任何生成任务**，因此不会花积分。这正是本仓"探针只准打只读端点"
纪律的落地：要验的是"这把凭据有没有效"，不是"能不能出图"。

⚠️ 凭据从**环境变量**读，不接受命令行参数 —— 命令行会进 shell history
和 `ps` 输出，等于把密码贴在公告板上。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.config import Settings
from app.sg_login import SgAccountLogin, mask_account, parse_account_token

ENV = "SG_LOGIN_PROBE"
SG_Q = ("aid=513641&device_platform=web&region=PH&da_version=3.3.30"
        "&web_version=7.5.0&aigc_features=app_lip_sync")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


async def main() -> int:
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        print(f"请设置 {ENV}='<邮箱>|<密码>'（账号密码不要走命令行参数）")
        return 2
    parsed = parse_account_token(raw)
    if parsed is None:
        print(f"{ENV} 不是 `邮箱|密码` 形态")
        return 2
    account, password = parsed

    login = SgAccountLogin(Settings())
    try:
        sid = await login.sessionid_for(account, password)
    except Exception as e:  # noqa: BLE001 —— 探针要把错误原样打出来
        print(f"登录失败：{type(e).__name__}: {e}")
        return 1
    print(f"登录成功 account={mask_account(account)} sid={sid[:10]}… "
          f"stats={login.stats()}")
    await login.aclose()

    # 凭据有效性 = 上游读接口 ret=0（不是 200：上游 200 也可能是 1015）
    with httpx.Client(timeout=20.0, trust_env=False,
                      headers={"user-agent": UA, "content-type": "application/json",
                               "origin": "https://dreamina.capcut.com",
                               "referer": "https://dreamina.capcut.com/",
                               "cookie": f"sessionid={sid}"}) as c:
        r = c.post(f"https://mweb-api-sg.capcut.com/mweb/v1/get_history_by_ids?{SG_Q}",
                   json={"submit_ids": ["probe-0"]})
    try:
        ret = r.json().get("ret")
    except ValueError:
        ret = None
    print(f"读接口探针：HTTP {r.status_code} ret={ret}")
    if str(ret) != "0":
        print(f"  ⚠️ 凭据未被上游接受：{r.text[:200]}")
        return 1
    print("✅ sessionid 有效（ret=0），可直接作为 Bearer 使用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
