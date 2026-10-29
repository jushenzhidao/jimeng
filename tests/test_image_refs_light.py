#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`image_refs` 不再长期存 base64 —— 换成上游 `image_uri` + **内容摘要**。

背景（线上实测 2026-10-21）：`tasks` 表 18330 行占 **3741 MB**，而真实数据求和只有
~394 MB；单行 `image_refs` 最大 **1,023,295 字节**（i2i 垫图的
`data:image/jpeg;base64,…` 原文）。磁盘被吃到最后剩 418MB（99%）——
`prune` 一直在删 7 天前的行，但普通 vacuum 从不还磁盘，bloat 只增不减。

改法（三条缺一不可）：

  · **受理期算摘要** `input_digests`（data URI 本地解码、零网络；外链存原文，
    保持 `create` 的"请求内零上游往返"不变式）；
  · **首次派发上传成功后**，`image_refs` 就地换成上游 `image_uri`
    （`tos-cn-i-…/<hex>`，几十字节）⇒ base64 只活在"受理→派发"这个秒级窗口里，
    终态任务的行不再占大字段（旧行由 autovacuum 回收、空间可重用）；
  · **去重指纹改读 `input_digests`** ⇒ 跨上面那个替换**保持不变**，否则
    "同一次请求的重试"在前一条已派发后就不再被认成重复（去重静默失效）。

外加一条容易被漏掉的配套：`reuse_image_uri` 必须认**裸 `tos-cn-i-…`** ——
不然重试时会把裸 uri 当外链去下载，**把一次可重试的失败变成必然失败**。
"""
from __future__ import annotations

import base64
import hashlib

from app.media import reuse_image_uri
from app.store import TaskRecord
from tests.conftest import AUTH, drive

ASYNC = "/async/v1/images/generations"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
PNG_DIGEST = "sha256:" + hashlib.sha256(PNG_BYTES).hexdigest()

#: 上游 `image_uri` 的真实形态（`<space>/<32 位 hex>`，见 media 模块注释）
BARE_URI = "tos-cn-i-tb4s082cfz/" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

#: 🔴 **国际站**（SG）上传回执的形态 —— 空间前缀是 `tos-alisg-i-<space>-sg`，
#: 取自线上真实行：`tasks.image_refs = ["tos-alisg-i-wopfjsm1ax-sg/<32 hex>"]`。
BARE_URI_SG = "tos-alisg-i-wopfjsm1ax-sg/" + "60ec172d5dda4e208660156a98532e97"


# ---------------------------------------------------------------------------
# 受理期：摘要落库（原文照旧，因为还不知道上游 uri）
# ---------------------------------------------------------------------------


def test_input_digests_recorded_at_accept_time(client):
    """受理时就把 data URI 换成摘要 —— 本轮**不下载、不上传**（零网络）。"""
    r = client.post(ASYNC, json={"model": "jimeng-i2i", "prompt": "p",
                                 "image": PNG_DATA_URI}, headers=AUTH)
    assert r.status_code == 202, r.text

    rec = client.app.state.service.store.get(r.json()["task_id"])
    assert rec.input_digests == [PNG_DIGEST], rec.input_digests
    #: 受理时还没派发 ⇒ 原文仍在（只有派发成功才替换）
    assert rec.image_refs == [PNG_DATA_URI]


def test_url_inputs_keep_the_url_not_a_digest(client):
    """外链**不算摘要**（受理期不许下载）⇒ 存原文，与改动前行为逐字一致。"""
    url = "https://example.com/a.png"
    r = client.post(ASYNC, json={"model": "jimeng-i2i", "prompt": "p",
                                 "image": url}, headers=AUTH)
    assert r.status_code == 202, r.text

    rec = client.app.state.service.store.get(r.json()["task_id"])
    assert rec.input_digests == [url], rec.input_digests


# ---------------------------------------------------------------------------
# 派发后：image_refs 换成上游 uri（这是省空间的那一刀）
# ---------------------------------------------------------------------------


def test_image_refs_replaced_by_upstream_uri_after_dispatch(client,
                                                            client_state):
    """派发成功后 `image_refs` 必须是上游 uri，**摘要保持不变**。"""
    r = client.post(ASYNC, json={"model": "jimeng-i2i", "prompt": "p",
                                 "image": PNG_DATA_URI}, headers=AUTH)
    tid = r.json()["task_id"]
    store = client_state.service.store
    assert store.get(tid).image_refs == [PNG_DATA_URI]

    drive(client_state, times=1)

    rec = store.get(tid)
    assert rec.status in ("in_progress", "success"), rec.status
    assert len(rec.image_refs) == 1
    assert rec.image_refs[0].startswith("tos-cn-i-"), \
        f"派发后仍存着原始 ref：{rec.image_refs[0][:60]!r}"
    assert "base64" not in rec.image_refs[0]
    #: 摘要跨替换不变 —— 去重靠它
    assert rec.input_digests == [PNG_DIGEST], rec.input_digests


def test_no_reupload_when_refs_are_already_uris(client, client_state,
                                               fake_uploader):
    """`image_refs` 已经是上游 uri ⇒ 派发**一次上传都不发**（重试零成本）。"""
    tid = "jimeng_bare_uri_case"
    client_state.service.store.put(TaskRecord(
        task_id=tid, credential_id="cred-a", model="jimeng-i2i",
        cap_key="jimeng:i2i", status="queued", prompt="p",
        image_refs=[BARE_URI], input_digests=[PNG_DIGEST],
        size="2048x2048", n=1, created_at=0, updated_at=0))

    before = len(fake_uploader.uploads)
    drive(client_state, times=1)

    assert len(fake_uploader.uploads) == before, "已是 uri 还上传 = 白搬一次字节"
    assert client_state.service.store.get(tid).status != "queued"


# ---------------------------------------------------------------------------
# 去重：替换前后必须都命中（否则"重试去重"静默失效）
# ---------------------------------------------------------------------------


def test_dedupe_survives_ref_replacement(client, client_state):
    """第一条已派发（`image_refs` 已换成 uri）后，同一请求仍被认成重复。"""
    body = {"model": "jimeng-i2i", "prompt": "same", "image": PNG_DATA_URI}
    first = client.post(ASYNC, json=body, headers=AUTH)
    assert first.status_code == 202, first.text
    tid = first.json()["task_id"]

    drive(client_state, times=1)
    rec = client_state.service.store.get(tid)
    assert rec.image_refs[0].startswith("tos-cn-i-"), "前提：refs 已被替换"

    second = client.post(ASYNC, json=body, headers=AUTH)
    assert second.status_code == 202, second.text
    assert second.json()["task_id"] == tid, "同请求第二次必须复用（否则重复计费）"


# ---------------------------------------------------------------------------
# 配套：`reuse_image_uri` 认裸 uri（纯函数，不需要库）
# ---------------------------------------------------------------------------


def test_reuse_image_uri_accepts_bare_uri():
    """裸 `tos-cn-i-<space>/<16+ hex>` 必须被认出来。"""
    assert reuse_image_uri(BARE_URI) == BARE_URI


def test_reuse_image_uri_accepts_sg_bare_uri():
    """🔴 **国际站**的裸 uri 也必须被认出来（2026-10-29 线上事故）。

    原先 `_TOS_ASSET_RE` 写死 `tos-cn-i-`，而国际站上传回执是
    `tos-alisg-i-<space>-sg/<hex>` ⇒ 返回 None ⇒ 重试时把这个**上游自己的 uri**
    当外链去下载 ⇒ `load_one` 判定"既不是 URL 也不是 base64" ⇒ 抛出
    `image 需为 http(s) URL、data URI 或 base64 字符串` ——
    **把上游真错（SG 写接口 `ret=-6 shark not pass reject`）整段盖住**，
    两天 644 单被记成"调用方参数错"，排查方向完全跑偏。

    这条用例就是那个缺口：原先只覆盖 CN 前缀，SG 空间**从来没被测过**。
    """
    assert reuse_image_uri(BARE_URI_SG) == BARE_URI_SG


def test_reuse_image_uri_rejects_lookalikes():
    """守卫不放宽：路径穿越 / 非 hex key / 别的桶一律不认。"""
    assert reuse_image_uri("tos-cn-i-x/../../etc/passwd") is None
    assert reuse_image_uri("tos-cn-i-x/short") is None
    assert reuse_image_uri("https://evil.example.com/tos-cn-i-x/" + "a" * 32) is None
    #: 空间前缀只认 `cn` / `alisg` 两个站 —— 不做"任意 region"通配
    assert reuse_image_uri("tos-us-i-bucket/" + "a" * 32) is None
    assert reuse_image_uri("tos-alisg-i-bucket/../../etc/passwd") is None
    assert reuse_image_uri("tos-alisg-i-bucket/short") is None


# ---------------------------------------------------------------------------
# 迁移：旧库必须**自动补列**（否则一上线就 `column does not exist`）
# ---------------------------------------------------------------------------


def test_old_table_gets_input_digests_column_automatically(db_dsn):
    """老库（没有 `input_digests`）在 `TaskStore` 构造时被自动补列。

    `_ensure_columns` 只补"非空 **且** 有标量默认值"的列；`default_factory` 的
    JSON 列历史上只能手工 ALTER（`image_refs` 就是这么欠下的债）。这一列显式给了
    `server_default='[]'::jsonb` ⇒ 必须走自动补列这条路。
    """
    from sqlalchemy import create_engine, inspect, text

    from app.store import TaskStore

    eng = create_engine(db_dsn)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE tasks (task_id varchar PRIMARY KEY)"))

    TaskStore(db_dsn)          # 构造 = 建表 + 补列

    names = {col["name"] for col in inspect(eng).get_columns("tasks")}
    assert "input_digests" in names, "旧库没被自动补列 ⇒ 首次写新字段就 500"
    eng.dispose()
