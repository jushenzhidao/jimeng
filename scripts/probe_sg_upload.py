#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国际版 Dreamina **上传链路**真跑探针（免费动作：只传素材，不建任务、不生成）。

验证点：AWS4 scope 的 region —— token 下发 `region='sg'` 与域名首段
`ap-singapore-1` 不一致，只有真上传能判定。先按 token 值 `sg` 试；
被签名拒（403/SignatureDoesNotMatch 类）再换 `ap-singapore-1` 复试。

用法：python scripts/probe_sg_upload.py --sessionid <sid>
"""
from __future__ import annotations

import argparse
import sys
import zlib

import httpx

sys.path.insert(0, ".")   # 以包形态 import app.upstream…（client 有相对 import）

from app.upstream.jimeng.client import JimengClient
from app.upstream.jimeng.site import SG_SITE
from app.upstream.jimeng.upload import (ImageXError, ImageXUploader,
                                        _aws_region, aws4_sign)

# 1×1 红色 PNG（8 字节以上，足够走完四段式）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc0000000030001cdd4506e0000000049"
    "454e44ae426082")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessionid", required=True)
    a = ap.parse_args()

    c = JimengClient(sessionid=a.sessionid, profile=SG_SITE)
    up = ImageXUploader(c)
    http = httpx.Client(timeout=30.0)   # 上传段独立用（imagex 域，无 cookie）
    t = up.token()
    host = t.get("upload_domain") or ""
    print(f"token: domain={host} space={t.get('space_name')} "
          f"token_region={t.get('region')!r} _aws_region→{_aws_region(t, host)!r}")

    for region in (t.get("region"), "ap-singapore-1", None):
        if not region:
            continue
        try:
            apply_res = _apply(up, t, host, region, http)
            addr = apply_res.get("UploadAddress") or apply_res.get("uploadAddress")
            si = (addr.get("StoreInfos") or [{}])[0]
            uri = si.get("StoreUri")
            auth = si.get("Auth")
            upload_host = (addr.get("UploadHosts") or [addr.get("UploadHost")])[0]
            # 上传字节
            headers = {"authorization": auth,
                       "content-type": "application/octet-stream",
                       "content-crc32": f"{zlib.crc32(PNG) & 0xFFFFFFFF:08x}"}
            r = http.post(f"https://{upload_host}/upload/v1/{uri}",
                          headers=headers, content=PNG)
            print(f"[region={region}] PUT → HTTP {r.status_code} {r.text[:120]!r}")
            if r.status_code >= 400:
                continue
            # commit
            import base64, json
            session_key = addr.get("SessionKey") or base64.b64encode(
                json.dumps(addr, ensure_ascii=False,
                           separators=(",", ":")).encode()).decode()
            body = json.dumps({"SessionKey": session_key},
                              ensure_ascii=False, separators=(",", ":")).encode()
            from app.upstream.jimeng.upload import API_VERSION
            query = {"Action": "CommitImageUpload", "Version": API_VERSION,
                     "ServiceId": t["space_name"]}
            signed = ("content-type", "host", "x-amz-content-sha256",
                      "x-amz-date", "x-amz-security-token")
            h = aws4_sign(method="POST", host=host, path="/", query=query,
                          access_key=t["access_key_id"],
                          secret_key=t["secret_access_key"],
                          session_token=t["session_token"], payload=body,
                          signed_headers=signed,
                          extra_headers={"content-type": "application/json"},
                          region=region)
            r2 = http.post(f"https://{host}/?" + "&".join(
                f"{k}={v}" for k, v in query.items()),
                headers=h, content=body)
            print(f"[region={region}] COMMIT → HTTP {r2.status_code} "
                  f"{r2.text[:200]!r}")
            if r2.status_code == 200 and '"Code":"Success"' in r2.text.replace("'", '"'):
                # 免费验真：image_by_uri
                verify = c.image_by_uri(uri)
                print(f"[region={region}] ✅ 上传+commit+验真全通 uri={uri} "
                      f"verify={bool(verify)}")
                return 0
        except ImageXError as e:
            print(f"[region={region}] ImageXError: {e}")
            continue
    print("两个 region 都没走通")
    return 1


def _apply(up: ImageXUploader, t: dict, host: str, region: str,
           http: httpx.Client) -> dict:
    from app.upstream.jimeng.upload import API_VERSION
    query = {"Action": "ApplyImageUpload", "Version": API_VERSION,
             "ServiceId": t["space_name"], "FileSize": str(len(PNG))}
    signed = ("x-amz-date", "x-amz-security-token")
    h = aws4_sign(method="GET", host=host, path="/", query=query,
                  access_key=t["access_key_id"],
                  secret_key=t["secret_access_key"],
                  session_token=t["session_token"], payload=b"",
                  signed_headers=signed, region=region)
    url = f"https://{host}/?" + "&".join(f"{k}={v}" for k, v in query.items())
    r = http.get(url, headers=h)
    print(f"[region={region}] APPLY → HTTP {r.status_code} {r.text[:160]!r}")
    if r.status_code >= 400:
        raise ImageXError(f"apply 被拒 HTTP {r.status_code}", retryable=False)
    res = r.json().get("Result") or r.json().get("result") or {}
    if not res:
        raise ImageXError(f"apply 无 Result: {r.text[:200]!r}", retryable=False)
    return res


if __name__ == "__main__":
    sys.exit(main())
