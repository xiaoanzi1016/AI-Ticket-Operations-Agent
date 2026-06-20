# -*- coding: utf-8 -*-
"""容器化验收脚本（Phase 2）。

【干什么用的】
对**已经跑起来的容器**做一次端到端体检 —— 不是单元测试，而是模拟真人使用：
探活 → 提交无附件任务 → 提交带 Excel 附件的任务 → 轮询结果 → 查列表/详情
→ 取消任务 → 校验审计库是否真的落到挂载出来的 data/ 目录。

用法：
    python scripts/acceptance_docker.py --base-url http://127.0.0.1:8000
    python scripts/acceptance_docker.py --skip-llm        # 只验接口骨架，不等模型

技术细节：用 httpx 同步客户端；每步打印 ✅/❌ 并累计失败数，最后以退出码
0/1 反馈（方便 CI 或 shell 判断）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from pathlib import Path

import httpx

# Windows 控制台默认可能是 GBK，先切 UTF-8，否则中文/符号会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 非 Windows 或已被重定向
    pass

ROOT = Path(__file__).resolve().parents[1]

# 统计
_PASS = 0
_FAIL = 0
_FAILED_NAMES: list[str] = []


def _w(text: str) -> int:
    """按显示宽度算字符占位（CJK 算 2 格），让中文表头能对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格。"""
    return text + " " * max(0, width - _w(text))


def step(name: str) -> None:
    print(f"\n{'=' * 70}\n▶ {name}\n{'=' * 70}")


def check(cond: bool, label: str, extra: str = "") -> bool:
    """记录一项检查结果并打印。"""
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [OK]   {label}" + (f"  {extra}" if extra else ""))
    else:
        _FAIL += 1
        _FAILED_NAMES.append(label)
        print(f"  [FAIL] {label}" + (f"  {extra}" if extra else ""))
    return cond


def wait_task(client: httpx.Client, base: str, task_id: str,
              timeout: float = 180.0, poll: float = 1.5) -> dict:
    """轮询任务直到进入终态，返回最终详情。"""
    deadline = time.time() + timeout
    detail: dict = {}
    last = ""
    while time.time() < deadline:
        detail = client.get(f"{base}/api/v1/tasks/{task_id}").json()
        status = detail.get("status", "?")
        if status != last:
            print(f"     …状态: {status}")
            last = status
        if status in {"completed", "failed", "cancelled"}:
            return detail
        time.sleep(poll)
    print(f"     …轮询超时（{timeout}s），当前状态 {last}")
    return detail


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 容器化验收")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="单个任务等待上限（秒）")
    ap.add_argument("--skip-llm", action="store_true",
                    help="跳过需要真调大模型的断言（只看接口是否通）")
    ap.add_argument(
        "--delivery",
        default=str(ROOT / "outputs" / "acceptance" / "上传验收_订单.csv"),
        help="用于驱动查询的发货订单文件（默认是一份项目自带数据里不存在的验收专用订单）",
    )
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print(f"目标服务: {base}")
    client = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------
    step("1. 探活 /health")
    # ------------------------------------------------------------------
    try:
        r = client.get(f"{base}/health")
        body = r.json()
        print("  响应:", json.dumps(body, ensure_ascii=False)[:400])
        check(r.status_code == 200, "/health 返回 200", f"status={r.status_code}")
        check(body.get("status") == "healthy",
              "/health 状态为 healthy（内含 SELECT 1 探库）",
              f"status={body.get('status')}")
        check(body.get("status") != "degraded", "数据库连通（未 degraded）")
        check(bool(body.get("version")), "返回了版本号", str(body.get("version")))
    except Exception as e:
        check(False, "/health 可访问", f"{type(e).__name__}: {e}")
        print("\n服务不可达，终止验收。")
        return 1

    # ------------------------------------------------------------------
    step("2. 根路径与 OpenAPI 文档")
    # ------------------------------------------------------------------
    r = client.get(f"{base}/")
    check(r.status_code == 200, "GET / 返回 200", f"status={r.status_code}")
    r = client.get(f"{base}/openapi.json")
    paths = sorted(r.json().get("paths", {}).keys()) if r.status_code == 200 else []
    print("  已注册路由:", ", ".join(paths))
    for p in ["/health", "/metrics", "/api/v1/tasks/submit",
              "/api/v1/tasks/recent", "/api/v1/tasks/{task_id}",
              "/api/v1/tasks/{task_id}/cancel"]:
        check(p in paths, f"路由存在 {p}")

    # ------------------------------------------------------------------
    step("3. 提交无附件任务（对照组：查一个默认数据里不存在的订单，应当查不到）")
    # ------------------------------------------------------------------
    t0 = time.time()
    r = client.post(f"{base}/api/v1/tasks/submit",
                    data={"user_input": "帮我查一下订单 PO-DOCKER-9001 现在到哪了",
                          "customer": "验收-无附件"})
    elapsed = time.time() - t0
    check(r.status_code == 200, "submit 返回 200", f"status={r.status_code}")
    sub = r.json() if r.status_code == 200 else {}
    print("  响应:", json.dumps(sub, ensure_ascii=False)[:300])
    plain_id = sub.get("task_id", "")
    check(bool(plain_id), "拿到 task_id", plain_id)
    check(elapsed < 5.0, "submit 立即返回（<5s，未阻塞在模型调用上）",
          f"耗时 {elapsed:.2f}s")

    if plain_id and not args.skip_llm:
        step("4. 轮询无附件任务结果")
        detail = wait_task(client, base, plain_id, args.timeout)
        check(detail.get("status") in {"completed", "failed"},
              "任务进入终态", f"status={detail.get('status')}")
        check(detail.get("status") == "completed", "任务成功完成（真调模型）")
        print("  结果摘要:", str(detail.get("result_summary"))[:500])
        execs = detail.get("executions") or []
        print(f"  工具执行记录 {len(execs)} 条:",
              ", ".join(e.get("tool_name", "?") for e in execs))
        check(len(execs) > 0, "产生了工具调用留痕")
        # ★ 负向对照：物流单号 / 商品名只存在于"验收专用订单文件"里，
        #   提示词里没提过它们。若没查不到它们，说明默认数据源里确实没有这单。
        blob_plain = str(detail.get("result_summary") or "") + str(execs)
        check("DOCKERTEST" not in blob_plain and "容器化验收专用商品" not in blob_plain,
              "未上传附件时查不到该单的独有信息（负向对照成立）")

    # ------------------------------------------------------------------
    step("5. 提交带 Excel/CSV 附件的任务（附件要真正驱动查询）")
    # ------------------------------------------------------------------
    delivery = Path(args.delivery)
    check(delivery.exists(), "测试用发货文件存在", str(delivery))
    file_id = ""
    if delivery.exists():
        with delivery.open("rb") as fh:
            r = client.post(
                f"{base}/api/v1/tasks/submit",
                data={"user_input": "我刚上传了一份发货订单，帮我查一下订单 "
                                    "PO-DOCKER-9001 现在是什么状态、货发到哪了",
                      "customer": "验收-带附件"},
                files={"delivery": (delivery.name, fh, "text/csv")},
            )
        check(r.status_code == 200, "带附件 submit 返回 200", f"status={r.status_code}")
        if r.status_code == 200:
            up = r.json()
            file_id = up.get("task_id", "")
            print("  响应:", json.dumps(up, ensure_ascii=False)[:300])
            check(bool(file_id), "拿到 task_id", file_id)
        else:
            print("  响应:", r.text[:500])

    if file_id and not args.skip_llm:
        step("6. 轮询带附件任务 + 校验附件留痕")
        detail = wait_task(client, base, file_id, args.timeout)
        check(detail.get("status") in {"completed", "failed", "cancelled"},
              "任务进入终态", f"status={detail.get('status')}")
        check(detail.get("status") == "completed", "带附件任务成功完成")
        execs = detail.get("executions") or []
        uploads = [e for e in execs if e.get("tool_name") == "file_upload"]
        check(len(uploads) == 1, "审计里有 file_upload 留痕")
        if uploads:
            params = uploads[0].get("input_params") or {}
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    params = {}
            print("  上传留痕:", json.dumps(params, ensure_ascii=False)[:400])
            check(bool(params.get("files")), "留痕记录了文件清单")

        # ★ 关键断言：验收专用物流单号 / 商品名只存在于上传文件里，且提示词未提及。
        #   它们出现在结果中 → 上传件真的替换了数据源，而不是被忽略。
        blob = str(detail.get("result_summary") or "") + str(execs)
        check("DOCKERTEST" in blob or "容器化验收专用商品" in blob,
              "结果里出现上传件独有的物流单号/商品名（证明附件真正驱动了查询）")

    # ------------------------------------------------------------------
    step("7. 任务列表分页 + 详情查询")
    # ------------------------------------------------------------------
    r = client.get(f"{base}/api/v1/tasks/recent", params={"limit": 5, "offset": 0})
    check(r.status_code == 200, "recent 返回 200", f"status={r.status_code}")
    lst = r.json() if r.status_code == 200 else {}
    print(f"  total={lst.get('total')} 本页={len(lst.get('items') or [])}")
    check((lst.get("total") or 0) >= 2, "列表里至少有刚提交的 2 个任务")
    if lst.get("items"):
        ids = [it.get("task_id") for it in lst["items"]]
        check(plain_id in ids or file_id in ids, "列表包含本次提交的任务")

    r = client.get(f"{base}/api/v1/tasks/recent", params={"limit": 1, "offset": 1})
    if r.status_code == 200:
        page2 = r.json()
        check(len(page2.get("items") or []) <= 1, "分页 offset 生效")

    r = client.get(f"{base}/api/v1/tasks/task_not_exist_xxx")
    check(r.status_code == 404, "不存在的任务返回 404", f"status={r.status_code}")
    if r.status_code == 404:
        err = r.json().get("error", {})
        check(err.get("code") == "task_not_found", "404 错误码统一", str(err.get("code")))

    # ------------------------------------------------------------------
    step("8. 入参校验（缺 user_input 必须 422 且是统一错误壳）")
    # ------------------------------------------------------------------
    r = client.post(f"{base}/api/v1/tasks/submit", data={"customer": "x"})
    check(r.status_code == 422, "缺字段返回 422", f"status={r.status_code}")
    if r.status_code == 422:
        check("error" in r.json(), "422 也是统一错误壳",
              json.dumps(r.json(), ensure_ascii=False)[:200])

    # ------------------------------------------------------------------
    step("9. 取消任务（协作式）")
    # ------------------------------------------------------------------
    r = client.post(f"{base}/api/v1/tasks/submit",
                    data={"user_input": "统计一下昨天的售后工单分布情况并给出处理建议",
                          "customer": "验收-取消"})
    if r.status_code == 200:
        cid = r.json()["task_id"]
        rc = client.post(f"{base}/api/v1/tasks/{cid}/cancel")
        check(rc.status_code == 200, "cancel 返回 200", f"status={rc.status_code}")
        print("  取消响应:", json.dumps(rc.json(), ensure_ascii=False)[:300])
        final = wait_task(client, base, cid, 60.0)
        check(final.get("status") in {"cancelled", "completed", "failed"},
              "取消后任务收敛到终态", f"status={final.get('status')}")
        # 终态后再取消应当 409
        rc2 = client.post(f"{base}/api/v1/tasks/{cid}/cancel")
        check(rc2.status_code in {409, 200}, "重复取消被正确拒绝/处理",
              f"status={rc2.status_code}")

    # ------------------------------------------------------------------
    step("10. 运行指标 /metrics")
    # ------------------------------------------------------------------
    r = client.get(f"{base}/metrics")
    check(r.status_code == 200, "metrics 返回 200", f"status={r.status_code}")
    if r.status_code == 200:
        print("  ", json.dumps(r.json(), ensure_ascii=False)[:400])

    client.close()

    # ------------------------------------------------------------------
    step("11. 审计库是否真正落到宿主机挂载目录（容器删了记录还在）")
    # ------------------------------------------------------------------
    import sqlite3

    db_path = ROOT / "data" / "agent_operations.db"
    check(db_path.exists(), "宿主机 data/agent_operations.db 存在", str(db_path))
    if db_path.exists():
        try:
            con = sqlite3.connect(str(db_path))
            n_tasks = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            n_exec = con.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0]
            last = con.execute(
                "SELECT task_id, status FROM tasks ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            # 本次验收提交的任务是否都能在宿主机库里查到
            known = 0
            for tid in (plain_id, file_id):
                if tid:
                    hit = con.execute(
                        "SELECT COUNT(*) FROM tasks WHERE task_id=?", (tid,)
                    ).fetchone()[0]
                    known += hit
            con.close()
            print(f"  tasks={n_tasks}  tool_executions={n_exec}  最新任务={last}")
            print(f"  本次验收的 {sum(1 for t in (plain_id, file_id) if t)} 个任务"
                  f"在宿主机库中命中 {known} 个")
            check(known == sum(1 for t in (plain_id, file_id) if t),
                  "本次验收提交的任务都已落进宿主机 data/ 目录里的库")
            check(n_exec > 0, "工具执行流水也已落库", f"executions={n_exec}")
        except Exception as e:
            check(False, "宿主机审计库可读", f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    print(f"\n{'=' * 70}")
    print(f"验收结果：通过 {_PASS} 项，失败 {_FAIL} 项")
    if _FAILED_NAMES:
        print("失败项：")
        for n in _FAILED_NAMES:
            print(f"  - {n}")
    print("=" * 70)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
