# -*- coding: utf-8 -*-
"""命令行查询工具：翻 Agent 的"历史台账"。

【这个文件是干什么的】
Phase 1 把任务和执行流水落进了 SQLite，但数据库文件人不好直接看。
这个 CLI 就是账本的"查询窗口"，一条命令就能回答：
- 最近处理了哪些任务？          python src/query_cli.py recent 10
- 某个任务当时到底干了什么？    python src/query_cli.py task task_3f9a1c07be24d5e1
- 某一天处理了哪些任务？        python src/query_cli.py date 2026-09-30
- 今天处理了多少、拦了多少？    python src/query_cli.py stats today

【技术细节】
- `argparse` 子命令：每个子命令一个独立 handler，新增查询方式不影响现有命令。
- 表格自己画（不引 prettytable）：项目 requirements 里没有这个依赖，
  为了一个 CLI 多加一个第三方包不划算。
- 中文对齐：中日韩字符在等宽字体里占 2 个字符宽，直接 `str.ljust()` 会错位。
  这里用 `unicodedata.east_asian_width` 计算"显示宽度"再补空格。
- Windows 控制台默认可能是 GBK，打印中文会 UnicodeEncodeError；
  因此在入口处把 stdout 重设为 UTF-8（失败也不致命，静默跳过）。
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

# 让 `python src/query_cli.py` 也能 import src（脚本在 src/ 下时 sys.path 只有 src/）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.persistence import crud  # noqa: E402
from src.persistence.database import DB_PATH, get_db  # noqa: E402

# 状态 -> 中文展示（库里存英文枚举，展示给人看中文）
_STATUS_ZH = {
    "processing": "处理中",
    "completed": "已完成",
    "failed": "失败",
    "success": "成功",
    "blocked": "已拦截",
}


# ----------------------------------------------------------------------
# 显示宽度 / 表格
# ----------------------------------------------------------------------
def _disp_width(text: str) -> int:
    """计算字符串在等宽终端里的显示宽度（中文按 2 列算）。"""
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _clip(text: str, limit: int) -> str:
    """按显示宽度截断，超出补省略号。"""
    text = str(text).replace("\n", " ").replace("\r", " ")
    if _disp_width(text) <= limit:
        return text
    out, width = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + w > limit - 1:
            break
        out += ch
        width += w
    return out + "…"


def _pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格对齐。"""
    gap = width - _disp_width(text)
    if gap <= 0:
        return str(text)
    return (" " * gap + str(text)) if align == "right" else (str(text) + " " * gap)


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """画一张 ASCII 表格（按显示宽度对齐，中文不会错位）。"""
    cols = len(headers)
    normalized = [[_clip(c, 60) for c in (list(r) + [""] * cols)[:cols]] for r in rows]
    widths = [
        max([_disp_width(headers[i])] + [_disp_width(r[i]) for r in normalized])
        for i in range(cols)
    ]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(_pad(headers[i], widths[i]) for i in range(cols)) + " |", line]
    for r in normalized:
        out.append("| " + " | ".join(_pad(r[i], widths[i]) for i in range(cols)) + " |")
    out.append(line)
    return "\n".join(out)


def _status_zh(status: str | None) -> str:
    s = str(status or "")
    return f"{_STATUS_ZH.get(s, s)}({s})" if s in _STATUS_ZH else s


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "-"


def _one_line(text: str, limit: int = 40) -> str:
    return _clip(str(text or "").replace("\n", " "), limit)


# ----------------------------------------------------------------------
# 子命令实现
# ----------------------------------------------------------------------
def cmd_recent(args: argparse.Namespace) -> int:
    """列出最近 N 条任务。"""
    with get_db() as db:
        tasks = crud.get_recent_tasks(db, args.limit)
    print(f"最近 {args.limit} 条任务（数据库：{DB_PATH}）")
    if not tasks:
        print("（暂无记录。先跑一次 python scripts/run.py 生成数据）")
        return 0
    print(render_table(
        ["任务ID", "状态", "创建时间", "更新时间", "用户输入"],
        [[t.task_id, _status_zh(t.status), _fmt_dt(t.created_at),
          _fmt_dt(t.updated_at), _one_line(t.user_input)] for t in tasks],
    ))
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """查看某任务详情 + 完整执行链路。"""
    task_id = args.task_id.strip()
    with get_db() as db:
        task = crud.get_task_by_id(db, task_id)
        if task is None:
            print(f"未找到任务：{task_id}")
            print("提示：用 `python src/query_cli.py recent 10` 查看最近的任务编号。")
            return 1
        execs = crud.get_tool_executions_by_task(db, task_id)

    print("=" * 72)
    print(f"任务详情  {task.task_id}")
    print("=" * 72)
    print(f"状态      : {_status_zh(task.status)}")
    print(f"创建时间  : {_fmt_dt(task.created_at)}")
    print(f"更新时间  : {_fmt_dt(task.updated_at)}")
    print(f"用户输入  : {task.user_input}")

    print("\n--- 结果摘要 ---")
    if task.result_summary:
        try:
            print(json.dumps(json.loads(task.result_summary), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            # 摘要超长被截断时不再是合法 JSON，原样展示即可
            print(task.result_summary)
    else:
        print("（无，任务可能仍在处理中）")

    print(f"\n--- 执行链路（共 {len(execs)} 条） ---")
    if not execs:
        print("（本次任务未产生工具调用记录）")
        return 0
    print(render_table(
        ["#", "执行时间", "工具/环节", "状态", "输入参数", "结果/错误"],
        [[str(i), _fmt_dt(e.executed_at), e.tool_name, _status_zh(e.status),
          _one_line(e.input_params, 34),
          _one_line(e.error_message or e.output_result or "", 44)]
         for i, e in enumerate(execs, start=1)],
    ))
    return 0


def cmd_date(args: argparse.Namespace) -> int:
    """按日期查询任务。"""
    try:
        with get_db() as db:
            tasks = crud.get_tasks_by_date(db, args.date)
    except ValueError as e:
        print(f"参数错误：{e}")
        return 1
    print(f"{args.date} 的任务（共 {len(tasks)} 条）")
    if not tasks:
        print("（该日期没有记录）")
        return 0
    print(render_table(
        ["任务ID", "状态", "创建时间", "用户输入"],
        [[t.task_id, _status_zh(t.status), _fmt_dt(t.created_at), _one_line(t.user_input)]
         for t in tasks],
    ))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """统计某日处理量（`today` 表示今天）。"""
    target = args.date
    try:
        with get_db() as db:
            data = crud.get_stats_today(db) if target == "today" else crud.get_stats(db, target)
    except ValueError as e:
        print(f"参数错误：{e}")
        return 1

    print("=" * 52)
    print(f"处理量统计  {data['date']}" + ("（今天）" if target == "today" else ""))
    print("=" * 52)
    print(f"任务总数      : {data['total']}")
    by_status = data.get("by_status") or {}
    if by_status:
        for status, count in sorted(by_status.items()):
            print(f"  - {_status_zh(status):<12}: {count}")
    else:
        print("  - （无任务）")
    print(f"工具执行次数  : {data['tool_calls']}")
    print(f"安全闸门拦截  : {data['blocked']}")
    print(f"工具执行失败  : {data['tool_failed']}")
    return 0


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="query_cli.py",
        description="AI-Ticket-Operations-Agent 历史记录查询工具（Phase 1 持久化层）",
        epilog=(
            "示例：\n"
            "  python src/query_cli.py recent 10\n"
            "  python src/query_cli.py task task_3f9a1c07be24d5e1\n"
            "  python src/query_cli.py date 2026-09-30\n"
            "  python src/query_cli.py stats today\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="{recent,task,date,stats}")

    p_recent = sub.add_parser("recent", help="查看最近 N 条任务（默认 10）")
    p_recent.add_argument("limit", nargs="?", type=int, default=10, help="返回条数，默认 10")
    p_recent.set_defaults(func=cmd_recent)

    p_task = sub.add_parser("task", help="查看某任务详情（含全部工具执行记录）")
    p_task.add_argument("task_id", help="任务编号，如 task_3f9a1c07be24d5e1")
    p_task.set_defaults(func=cmd_task)

    p_date = sub.add_parser("date", help="按日期查询任务")
    p_date.add_argument("date", help="日期，格式 YYYY-MM-DD")
    p_date.set_defaults(func=cmd_date)

    p_stats = sub.add_parser("stats", help="统计某日处理量")
    p_stats.add_argument("date", nargs="?", default="today",
                         help="'today' 或 YYYY-MM-DD，默认 today")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台可能是 GBK，中文会编码失败；能改就改成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):  # pragma: no cover - 非标准输出流
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
