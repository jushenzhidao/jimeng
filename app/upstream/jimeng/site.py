#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点 Profile：国内即梦（jimeng.jianying.com）与国际 Dreamina
（dreamina.capcut.com → mweb-api-sg.capcut.com）的**站点常量层**。

## 为什么需要这一层（2026-10-15 国际版适配的实测结论）

两站是**同一套 mweb 前端框架**的两个部署：

* `sign` 算法**逐字相同** —— 国际版抓包向量本地验算命中
  （`md5("9e2c|enerate|7|8.4.0|1792059011||11ac") ==
  f9fa6bce0f3447aa936d528b2309ff9a`，见 `sign.py`，算法零改动）；
* `draft_content` / 提交体结构、`/mweb/v1/*` 全部路径、
  `get_common_config` 能力表字段名 **完全一致**（探针实读国际表 15 个模型）；
* `get_history_by_ids` / `get_upload_token` 同路径可用（探针 `ret=0`）；
* **msToken / X-Bogus / X-Gnarly 不需要**（探针一个都没带，照样 `ret=0`，
  与国内版负对照结论一致）；
* 上传 STS 的 `upload_domain` 指向**火山国际版**
  （`imagex-ap-singapore-1.bytevcloudapi.com`），AWS4 region 随 token 下发的
  `region` 字段走（见 `upload.py`）。

**差异只在常量**：域名 / `aid` / `da_version` / `region` / `loc` /
`lan` / 站点 origin（`sec-fetch-site` 从 same-origin 变 same-site）/
`store-country-code` / `did` 头。本文件把这些差异收拢成两个不可变实例，
`JimengClient` 按实例取值 —— 客户端逻辑零分叉。

## 🔴 站点判定只认 sessionid，不认模型名

`high_aes_general_v50`（Seedream 5.0 Lite）**两个站都有**（同名同 key，
账号与计费体系独立）⇒ 模型名推不出站点。唯一可靠判据是**凭据归属**：
拿 sessionid 对两站各打一次**零成本** `get_common_config`（读接口，不建任务），
`ret=0` 即归属；`1015`（login error）换下一站。归属一旦确定不会变
（两站账号体系独立），进程内缓存即可。探测实现在 `service.Service._site_of`
（它持有建连与观测），本文件只放常量与顺序。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SiteProfile:
    """一个上游站点的全部常量。`JimengClient` 只从这里读，不做站点 if 分叉。"""

    #: 站点名（观测埋点 / 日志 / 测试用）：`cn` / `sg`
    name: str
    #: **API** 域名（请求打到这里）。注意国际版 API 与站点**不同域**。
    base: str
    #: 浏览器视角的**站点** origin（`origin` / `referer` 头用它）。
    #: 国际版是 dreamina.capcut.com（页面域），API 域是 mweb-api-sg.capcut.com。
    origin: str
    #: query `aid` + body `http_common_info.aid`（两站不同：513695 / 513641）
    aid: str
    #: query `da_version` + draft `version`（3.3.28 / 3.3.30）
    da_version: str
    #: query `region`（cn / 账号地区如 PH —— 国际版 `region` 跟账号走，
    #: 抓包样本是 PH；别站账号用 `JIMENG_SG_REGION` 覆盖，见 config.py）
    region: str
    #: `loc` 头
    loc: str
    #: `lan` 头
    lan: str
    #: `sec-fetch-site` 头：国内 API 与页面同域（same-origin），
    #: 国际版跨子域（same-site）
    sec_fetch_site: str
    #: `store-country-code` 头。国内版**不带**此头（空串 = 不带）；
    #: 国际版跟账号走（抓包 ph，`JIMENG_SG_STORE_CC` 可覆盖）。
    store_country_code: str = ""
    #: `referer` 的路径部分（origin 之上）。国内工作台是 /ai-tool/generate，
    #: 国际抓包就一个 `/`。
    referer_path: str = "/ai-tool/generate"
    #: `accept-language` 头的完整值（抓包逐字）。
    accept_language: str = "zh-CN,zh;q=0.9"
    #: 🔴 未在此处的字段就是**两站共享**的常量（pf/appvr/sign 算法/web_version/
    #: app-sdk-version…），继续住在 `sign.py` —— 别把共享常量搬进来，
    #: 那会让"两站同款"这个事实失去单一出处。


#: 国内即梦。所有取值 = 现有实现的行为（等价重构，不改任何请求字节）。
CN_SITE = SiteProfile(
    name="cn",
    base="https://jimeng.jianying.com",
    origin="https://jimeng.jianying.com",
    aid="513695",
    da_version="3.3.28",
    region="cn",
    loc="cn",
    lan="zh-Hans",
    sec_fetch_site="same-origin",
)

#: 国际 Dreamina。全部取值来自 2026-10-15 用户抓包 + 零成本探针实测
#: （scripts/probe_sg.py：能力表/历史/上传 token 三读全 `ret=0`）。
SG_SITE = SiteProfile(
    name="sg",
    base="https://mweb-api-sg.capcut.com",
    origin="https://dreamina.capcut.com",
    aid="513641",
    da_version="3.3.30",
    region="PH",
    loc="PH",
    lan="zh-Hant-TW",
    sec_fetch_site="same-site",
    store_country_code="ph",
    referer_path="/",
    accept_language="zh-CN,zh;q=0.9",
)

#: 探测顺序。**cn 优先**：存量部署的凭据几乎全是国内号，
#: 让它们第一次探测就命中，少一次国际站往返。
SITES: tuple[SiteProfile, ...] = (CN_SITE, SG_SITE)

_BY_NAME: dict[str, SiteProfile] = {s.name: s for s in SITES}


def site_by_name(name: str) -> SiteProfile:
    """按名取站点（未知名字是配置/测试错误，直接炸）。"""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"未知站点 {name!r}，可用：{sorted(_BY_NAME)}") from None


def with_overrides(site: SiteProfile, *, region: str | None = None,
                   store_country_code: str | None = None) -> SiteProfile:
    """按部署配置覆盖**账号相关**的取值（region/store-country-code）。

    这两个字段跟着**账号地区**走（抓包样本是 PH 区号），别的国际账号可能
    不同。默认原样（不改任何行为）；部署时可用环境变量覆盖（config.py）。
    """
    kw: dict[str, str] = {}
    if region:
        kw["region"] = region
    if store_country_code:
        kw["store_country_code"] = store_country_code
    return replace(site, **kw) if kw else site


__all__ = ["SiteProfile", "CN_SITE", "SG_SITE", "SITES",
           "site_by_name", "with_overrides"]
