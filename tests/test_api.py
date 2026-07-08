# -*- coding: utf-8 -*-
"""Phase 2 Web 服务层测试。

【这份测试在验什么】
不是"接口能返回 200"这种表面验收，而是逐条验**架构承诺**：
1. 提交是异步的：submit 立刻返回 processing，结果是后台 Worker 写回来的；
2. 上传文件**真的**驱动了查询（用一份本地数据源里绝对没有的订单来判定）；
3. 任务级数据源用完全还原，不会串到下一个任务；
4. 上传的临时文件处理完被清掉；
5. 取消是协作式的，能让正在跑的任务停下来并落 cancelled；
6. 各类错误都是统一 JSON 形状 + 合适的 HTTP 状态码。

【为什么整个文件共用一个 client】
TestClient 每次进 with 都会新建一个事件循环，而后台 Worker 是模块级单例。
共用 client 既贴近"服务一直开着"的真实情况，也避免反复启停带来的干扰。
"""
from __future__ import annotations

import io
import json
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api.settings import api_settings

# ----------------------------------------------------------------------
# 测试用的订单 Excel：列名与极客云导出模板完全一致
# （data_source 读的就是这套列名，列名对不上会被明确拒绝）
# ----------------------------------------------------------------------
_ORDER_COLUMNS = [
    "订单号", "订单创建时间", "付款时间", "商品编码", "商品名称", "规格", "数量",
    "单价", "商品金额", "门店", "收货人", "联系电话", "收货地址", "订单状态",
    "渠道", "支付方式", "物流单号",
]

# 这个订单号在项目自带的 data/mock 数据源里**不存在**，
# 所以"能不能查到它"正好可以判定本次任务用的是默认数据源还是上传的数据源。
_GHOST_ORDER = "PO20260929-88888"


def _orders_xlsx(order_id: str = _GHOST_ORDER, qty: int = 3,
                 store: str = "苏州工业园店", sku: str = "XC-JLY008") -> bytes:
    """按极客云导出的列名，造一份只有一条明细的订单 Excel。"""
    row = [order_id, "2026-09-29 10:00:00", "2026-09-29 10:01:00", sku, "玻璃水", "2L",
           qty, "29.9", "89.7", store, "王五", "13800000000", "杭州市西湖区",
           "已发货", "门店", "微信", "SF123456789"]
    buf = io.BytesIO()
    pd.DataFrame([row], columns=_ORDER_COLUMNS).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _submit(client: TestClient, text: str, customer: str = "测试客户", **files):
    data = {"user_input": text, "customer": customer}
    return client.post("/api/v1/tasks/submit", data=data, files=files or None)


def _wait(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    """轮询直到任务离开 processing（异步处理，测试必须等）。"""
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/api/v1/tasks/{task_id}").json()
        if detail.get("status") != "processing":
            return detail
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 超时未结束，最后状态：{detail}")


def _gate_outcome(detail: dict) -> dict:
    """从任务摘要里取出安全闸门对第一个动作的判定。"""
    summary = json.loads(detail["result_summary"])
    return summary["gate_outcomes"][0]


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    """整个模块共用的 TestClient（进入 with 才会触发 lifespan：建表 + 起 Worker）。"""
    from src.api.main import app

    with TestClient(app) as c:
        yield c


# ----------------------------------------------------------------------
# 1) 系统接口
# ----------------------------------------------------------------------
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["version"] == api_settings.api_version


def test_root_hint(client):
    body = client.get("/").json()
    assert body["docs"] == "/docs"
    assert "submit" in body


def test_openapi_docs_available(client):
    """/docs 能打开的前提是 OpenAPI schema 能生成。"""
    assert client.get("/openapi.json").status_code == 200


def test_metrics(client):
    body = client.get("/metrics").json()
    assert body["workers"] >= 1
    assert body["worker_alive"] is True
    assert "total" in body["today"]


# ----------------------------------------------------------------------
# 2) 提交 + 异步处理
# ----------------------------------------------------------------------
def test_submit_returns_immediately_and_completes_in_background(client):
    r = _submit(client, "客户要求补发一瓶玻璃水", customer="异步验证")
    assert r.status_code == 200
    body = r.json()
    # 关键点：接口当场返回 processing，而不是等 Agent 跑完
    assert body["status"] == "processing"
    assert body["task_id"].startswith("task_")
    assert body["created_at"]

    detail = _wait(client, body["task_id"])
    assert detail["status"] == "completed"
    assert detail["user_input"] == "客户要求补发一瓶玻璃水"
    assert detail["updated_at"] >= detail["created_at"]


def test_task_detail_contains_execution_chain(client):
    """查详情要能看到"当时到底做了什么" —— 这是 Phase 1 审计能力在 Web 上的出口。"""
    r = _submit(client, "客户反映订单 PO20260928-00001 少发了一瓶玻璃水，要求补发",
                customer="链路验证")
    detail = _wait(client, r.json()["task_id"])
    tools = [e["tool_name"] for e in detail["executions"]]
    assert "safety_gate" in tools          # 敏感动作一定过闸门并留痕
    gate = next(e for e in detail["executions"] if e["tool_name"] == "safety_gate")
    assert gate["status"] in ("success", "blocked")
    assert isinstance(gate["input_params"], dict)   # 库里存的 JSON 字符串已还原成对象


def test_submit_missing_user_input_422(client):
    r = client.post("/api/v1/tasks/submit", data={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_submit_blank_user_input_422(client):
    r = client.post("/api/v1/tasks/submit", data={"user_input": ""})
    assert r.status_code == 422


def test_detail_not_found_404(client):
    r = client.get("/api/v1/tasks/task_does_not_exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "task_not_found"


# ----------------------------------------------------------------------
# 3) 列表分页
# ----------------------------------------------------------------------
def test_recent_list_shape(client):
    body = client.get("/api/v1/tasks/recent?limit=5&offset=0").json()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert body["total"] >= 1
    assert len(body["items"]) <= 5
    assert set(body["items"][0]) == {"task_id", "user_input", "status", "created_at"}


def test_recent_pagination_does_not_overlap(client):
    first = client.get("/api/v1/tasks/recent?limit=2&offset=0").json()["items"]
    second = client.get("/api/v1/tasks/recent?limit=2&offset=2").json()["items"]
    assert {i["task_id"] for i in first}.isdisjoint({i["task_id"] for i in second})


def test_recent_route_not_shadowed_by_task_id(client):
    """/recent 不能被 /{task_id} 抢走（否则会被当成 task_id="recent" 查库）。"""
    assert client.get("/api/v1/tasks/recent").status_code == 200


def test_recent_limit_out_of_range_422(client):
    r = client.get("/api/v1/tasks/recent?limit=999")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


# ----------------------------------------------------------------------
# 4) 上传
# ----------------------------------------------------------------------
def test_upload_unsupported_suffix_rejected(client):
    r = client.post("/api/v1/tasks/submit", data={"user_input": "x"},
                    files={"delivery": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_type"


def test_upload_empty_file_rejected(client):
    r = client.post("/api/v1/tasks/submit", data={"user_input": "x"},
                    files={"delivery": ("a.csv", b"", "text/csv")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_upload"


def test_upload_wrong_columns_rejected(client):
    """列名不对要给出人话报错，而不是等数据源在深层抛 KeyError。"""
    buf = io.BytesIO()
    pd.DataFrame([{"商品": "x"}]).to_excel(buf, index=False, engine="openpyxl")
    r = client.post("/api/v1/tasks/submit", data={"user_input": "x"},
                    files={"delivery": ("b.xlsx", buf.getvalue(), _XLSX_MIME)})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "schema_mismatch"
    assert "缺少必需列" in body["message"]


def test_upload_recorded_in_execution_chain(client):
    """附件要留痕：审计得能回答"这一单当时带了什么材料"。"""
    r = client.post(
        "/api/v1/tasks/submit",
        data={"user_input": "客户要求补发一瓶玻璃水", "customer": "附件留痕"},
        files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)},
    )
    assert r.status_code == 200
    detail = _wait(client, r.json()["task_id"])
    upload = next(e for e in detail["executions"] if e["tool_name"] == "file_upload")
    assert upload["status"] == "success"
    files = upload["input_params"]["files"]
    assert files[0]["field"] == "delivery"
    assert files[0]["role"] == "orders"
    assert upload["output_result"]["drives_query"] is True


def test_uploaded_dataset_actually_drives_the_query(client):
    """**核心用例**：上传的订单文件必须真的参与查询。

    判定方式：用一份"默认数据源里根本没有"的订单。
    - 不带附件跑一次：闸门会因为"订单不存在"拒绝补发；
    - 带上附件再跑：订单能定位到、SKU 能取出来，闸门放行进人工确认。
    两次结果不同，才能证明上传的数据真的被用上了（否则"返回 200"说明不了任何事）。
    """
    text = f"客户反映订单 {_GHOST_ORDER} 少发了一瓶玻璃水，要求补发"

    # A：不带附件 -> 默认数据源查不到这个订单
    without = _wait(client, _submit(client, text, customer="无附件").json()["task_id"])
    a = _gate_outcome(without)
    assert a["params"]["order_id"] == _GHOST_ORDER
    note = a["params"]["inventory_note"]
    assert note.startswith("订单") and "无法定位" in note

    # B：带附件 -> 上传的数据源里有这个订单
    r = client.post("/api/v1/tasks/submit",
                    data={"user_input": text, "customer": "有附件"},
                    files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)})
    with_upload = _wait(client, r.json()["task_id"])
    b = _gate_outcome(with_upload)
    assert b["params"]["order_id"] == _GHOST_ORDER
    assert b["params"].get("sku") == "XC-JLY008"      # 从上传的订单里取到了 SKU
    assert "无法定位" not in b["params"].get("inventory_note", "")


def test_dataset_is_restored_after_upload_task(client):
    """任务级数据源用完必须还原，否则上传的数据会串到下一个任务（数据串台）。"""
    # 先跑一个带附件的任务
    r = client.post("/api/v1/tasks/submit",
                    data={"user_input": f"客户反映订单 {_GHOST_ORDER} 少发了一瓶，要求补发"},
                    files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)})
    _wait(client, r.json()["task_id"])

    # 再跑一个不带附件的任务：应该回到默认数据源，查不到那个"幽灵订单"
    after = _wait(client, _submit(client, f"客户反映订单 {_GHOST_ORDER} 少发了一瓶，要求补发",
                                  customer="还原验证").json()["task_id"])
    outcome = _gate_outcome(after)
    assert "无法定位" in outcome["params"].get("inventory_note", ""), \
        "上一个任务的上传数据串到了下一个任务，数据源没有被还原"


def test_upload_temp_dir_cleaned(client):
    """上传的临时文件处理完要删掉，不给磁盘留垃圾。"""
    upload_root = api_settings.upload_dir
    before = {p.name for p in upload_root.iterdir() if p.is_dir()}
    r = client.post("/api/v1/tasks/submit",
                    data={"user_input": "客户要求补发一瓶玻璃水"},
                    files={"delivery": ("orders.xlsx", _orders_xlsx(), _XLSX_MIME)})
    _wait(client, r.json()["task_id"])
    after = {p.name for p in upload_root.iterdir() if p.is_dir()}
    assert not (after - before), f"任务结束后仍残留临时目录: {after - before}"


# ----------------------------------------------------------------------
# 5) 取消
# ----------------------------------------------------------------------
def test_cancel_nonexistent_404(client):
    assert client.post("/api/v1/tasks/task_nope/cancel").status_code == 404


def test_cancel_finished_task_409(client):
    r = _submit(client, "客户要求补发一瓶玻璃水", customer="终态取消")
    task_id = r.json()["task_id"]
    _wait(client, task_id)
    resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_not_cancellable"


def test_cancel_running_task(client, monkeypatch):
    """协作式取消：正在跑的任务要能被叫停并落 cancelled。

    做法：把 Worker 里的 Agent 临时换成一个"会一直等取消信号"的假 Agent。
    这样取消请求到达时任务一定还在处理中，测试不依赖运气/时序。

    假 Agent 也必须遵守**真 Agent 的契约**（自己回写任务终态），
    否则测的就不是取消逻辑，而是"假实现忘了落库"。
    """
    from src.agent import TaskCancelled
    from src.api.worker import _finalize, task_worker

    class BlockingAgent:
        """模拟跑很久的 Agent：每 50ms 检查一次取消标志。"""

        def run(self, user_input, customer="访客", session_id=None,
                task_id=None, cancel_check=None):
            for _ in range(200):                      # 最多 10 秒
                if cancel_check is not None and cancel_check():
                    raise TaskCancelled("测试：被取消")
                time.sleep(0.05)
            _finalize(task_id, "completed", summary={"answer": "本不该跑到这里"})
            return {"answer": "本不该跑到这里", "task_id": task_id}

    monkeypatch.setattr(task_worker, "_agent", BlockingAgent())

    r = _submit(client, "一个会跑很久的任务", customer="取消验证")
    task_id = r.json()["task_id"]

    # 等它真的开始执行（进入 running 状态）再取消
    for _ in range(100):
        if client.get("/metrics").json()["running"] == 1:
            break
        time.sleep(0.05)

    resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["task_id"] == task_id

    detail = _wait(client, task_id, timeout=15)
    assert detail["status"] == "cancelled"


def test_cancel_queued_task(client, monkeypatch):
    """排队期间就被取消的任务：连执行都不该执行。"""
    from src.api.worker import _finalize, task_worker

    class SlowAgent:
        def run(self, user_input, customer="访客", session_id=None,
                task_id=None, cancel_check=None):
            time.sleep(1.0)
            _finalize(task_id, "completed", summary={"answer": "done"})
            return {"answer": "done", "task_id": task_id}

    # 先占住 Worker：塞一个慢任务，后面提交的任务只能排队
    monkeypatch.setattr(task_worker, "_agent", SlowAgent())
    occupied = _submit(client, "占位任务", customer="占位").json()["task_id"]

    queued = _submit(client, "排队中的任务", customer="排队验证").json()["task_id"]
    assert client.post(f"/api/v1/tasks/{queued}/cancel").json()["status"] == "cancelled"

    detail = _wait(client, queued, timeout=15)
    assert detail["status"] == "cancelled"
    # 摘要里要能看出"它压根没跑"，而不是"跑到一半被停"
    assert "取消" in (detail["result_summary"] or "")

    _wait(client, occupied, timeout=15)


# ----------------------------------------------------------------------
# 6) 向后兼容：CLI 入口还能用
# ----------------------------------------------------------------------
def test_agent_run_signature_still_backward_compatible():
    """Phase 1 的调用方式（只传 user_input/customer）必须继续可用。"""
    import inspect

    from src.agent import TicketAgent

    params = inspect.signature(TicketAgent.run).parameters
    assert list(params)[:3] == ["self", "user_input", "customer"]
    for name in ("task_id", "cancel_check"):
        assert params[name].default is None      # 新增参数必须都是可选的
