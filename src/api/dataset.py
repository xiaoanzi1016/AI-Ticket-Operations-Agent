# -*- coding: utf-8 -*-
"""上传文件 → 临时目录 → 任务级数据源（PHASE 2 新增）。

【这个文件是干什么的】
接口允许随任务一起上传 Excel（发货订单 / 退货订单 / 仓库退货订单）。这个模块负责
把"浏览器发过来的文件流"变成"Agent 真正能查到的数据"：

    接收文件流 → 落盘到系统临时目录 → 校验列名 → 统一转成 UTF-8 CSV
      → 组装成一个**只属于本次任务**的 DataSource → 任务跑完删掉临时目录

【为什么要单独做"任务级数据源"】
Phase 1 的数据源是模块级单例（`src.data_source.data_source`），全局只有一份。
如果直接把上传文件塞进这个单例，A 客户上传的订单会串到 B 客户的任务里 —— 数据串台。
所以这里用「临时替换 + 用完还原」的方式：在任务执行的这段窗口内，
把全局单例换成一份指向本任务临时文件的新实例，`finally` 里必定还原。

【安全考虑】
- 文件名只取最后一段（`Path(name).name`），杜绝 `../../etc/passwd` 这类路径穿越；
- 边读边计数，超过 `MAX_UPLOAD_SIZE` 立刻中断并删除半截文件，防止磁盘被写满；
- 只接受白名单扩展名，其余在落盘**之前**就拒绝；
- 临时文件处理完即删（除非显式开了 `KEEP_UPLOAD_FILES`）。
"""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from fastapi import UploadFile

from src.api.settings import api_settings
from src.logger import log

# 读取文件流时的分块大小（1MB）。边读边写边计数，避免把整个文件先吞进内存。
_CHUNK_SIZE: int = 1024 * 1024

# 订单 Excel 的必需列。提前校验是为了给出"缺哪一列"的明确报错，
# 而不是让数据源层在深层抛一个看不出所以然的 KeyError。
_ORDERS_REQUIRED_COLUMNS: set[str] = {
    "订单号", "商品编码", "商品名称", "规格", "数量", "单价", "门店",
}


# ----------------------------------------------------------------------
# 异常
# ----------------------------------------------------------------------
class UploadRejected(Exception):
    """上传被拒绝（文件太大 / 类型不支持 / 内容格式不对）。

    带 status_code 是为了让路由层能直接把它翻译成合适的 HTTP 状态码：
    400 = 请求有问题（类型/格式），413 = 体积超限。
    """

    def __init__(self, message: str, *, code: str = "invalid_upload", status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class UploadedFileInfo:
    """一个上传文件的落地信息（会写进 task 的流水里做留痕）。"""

    field: str                 # 表单字段名：delivery / return / warehouse
    filename: str              # 用户上传时的原始文件名
    size: int                  # 字节数
    saved_path: str            # 临时目录里的原始文件路径
    role: str                  # orders（驱动查询）/ returns（退货数据）/ archive（仅留痕）
    csv_path: str | None = None  # 转成 CSV 后的路径（仅 orders/returns 有）

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "filename": self.filename,
            "size": self.size,
            "role": self.role,
            "csv": bool(self.csv_path),
        }


@dataclass
class TaskDataset:
    """一次任务专属的上传数据集。"""

    temp_dir: Path
    orders_csv: Path | None = None     # 对应数据源的 orders_file
    returns_csv: Path | None = None    # 对应数据源的 returns_file
    files: list[UploadedFileInfo] = field(default_factory=list)

    @property
    def drives_query(self) -> bool:
        """本次上传的数据是否可以真正驱动查询。

        只有 orders（订单）能接入数据源 —— 退货文件在 Phase 1 的数据源里
        还没有对应的查询入口，上传它只做留痕。
        """
        return self.orders_csv is not None

    def summary(self) -> dict:
        """给审计/日志用的摘要（不包含临时绝对路径的完整信息，只留文件名与大小）。"""
        return {
            "temp_dir": str(self.temp_dir),
            "drives_query": self.drives_query,
            "files": [f.to_dict() for f in self.files],
        }


# ----------------------------------------------------------------------
# 表单字段 -> 数据角色 的映射
# ----------------------------------------------------------------------
# 需求给的三个上传字段是"业务口径"的名字，数据源要的是"文件口径"的名字，这里做对齐：
#   delivery  发货订单   -> orders   ：真正驱动 query_order / 建议生成
#   return    退货订单   -> returns  ：落到 returns_file，当前查询层暂未消费
#   warehouse 仓库退货单 -> archive  ：数据源没有对应文件，只做接收与留痕
_FIELD_ROLE: dict[str, str] = {
    "delivery": "orders",
    "return": "returns",
    "warehouse": "archive",
}


# ----------------------------------------------------------------------
# 落盘 + 转换
# ----------------------------------------------------------------------
async def _spool_to_disk(upload: UploadFile, dest: Path) -> int:
    """把上传流分块写入目标文件，并做体积守门。返回实际字节数。

    大白话：一边收一边数，超过上限就当场拒绝，不等到写完才发现太大。

    技术细节：`await upload.read(n)` 拿到的是 bytes 块；累计超过
    `MAX_UPLOAD_SIZE` 时抛 UploadRejected(413)，由调用方清理半截文件。
    """
    limit = api_settings.max_upload_size
    size = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await upload.read(_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise UploadRejected(
                    f"文件 {upload.filename} 超过大小上限 {limit} 字节",
                    code="payload_too_large",
                    status_code=413,
                )
            fh.write(chunk)
    if size == 0:
        raise UploadRejected(f"文件 {upload.filename} 是空文件", code="empty_upload")
    return size


def _to_utf8_csv(src: Path, dest: Path, role: str) -> None:
    """把上传文件统一转成数据源能读的 UTF-8 CSV。

    大白话：数据源只会读 CSV，而用户上传的多半是 Excel；这里做一次"翻译"。

    技术细节：
    - 用 pandas 读：Excel 走 read_excel（openpyxl 引擎），CSV 走 read_csv；
    - 写出时用 `encoding="utf-8-sig"`（带 BOM），与 Phase 1 数据源的读取口径一致，
      避免中文列名在 Windows 下被按 GBK 解码成乱码；
    - 订单文件额外做"必需列"体检，缺列直接给用户一句人话，而不是抛 KeyError。
    """
    suffix = src.suffix.lower()
    try:
        if suffix in (".xlsx", ".xls"):
            # .xls（老版二进制格式）需要 xlrd；没装时给出可执行的提示
            engine = "xlrd" if suffix == ".xls" else None
            df = pd.read_excel(src, engine=engine)
        else:
            df = pd.read_csv(src, encoding="utf-8-sig")
    except ImportError as e:  # pragma: no cover - 取决于环境是否装了 xlrd
        raise UploadRejected(
            f"读取 {src.suffix} 需要额外依赖：{e}", code="missing_reader"
        ) from e
    except Exception as e:
        raise UploadRejected(
            f"文件 {src.name} 无法解析（请确认是标准的 Excel/CSV 导出）：{e}",
            code="unparsable_file",
        ) from e

    if df.empty:
        raise UploadRejected(f"文件 {src.name} 没有数据行", code="empty_upload")

    if role == "orders":
        missing = _ORDERS_REQUIRED_COLUMNS - set(map(str, df.columns))
        if missing:
            raise UploadRejected(
                "订单文件缺少必需列："
                + "、".join(sorted(missing))
                + "。请使用极客云导出的订单明细（列名需与导出模板一致）",
                code="schema_mismatch",
            )

    df.to_csv(dest, index=False, encoding="utf-8-sig")


async def save_uploads(
    delivery: UploadFile | None = None,
    returns: UploadFile | None = None,
    warehouse: UploadFile | None = None,
) -> TaskDataset | None:
    """把三个可选上传文件落盘并转换成任务数据集。

    返回 None 表示这次请求没有带任何文件（走默认数据源）。

    异常：
        UploadRejected —— 类型不支持 / 体积超限 / 内容格式不对。
        任何失败都会把本次已写入的临时目录删干净，不留垃圾。
    """
    candidates: list[tuple[str, UploadFile]] = [
        (name, up)
        for name, up in (("delivery", delivery), ("return", returns), ("warehouse", warehouse))
        if up is not None and (up.filename or "").strip()
    ]
    if not candidates:
        return None

    temp_dir = Path(tempfile.mkdtemp(prefix="task-", dir=str(api_settings.upload_dir)))
    ds = TaskDataset(temp_dir=temp_dir)
    try:
        for field_name, upload in candidates:
            role = _FIELD_ROLE[field_name]
            safe_name = Path(upload.filename or "upload").name  # 只取文件名，杜绝路径穿越
            suffix = Path(safe_name).suffix.lower()
            if suffix not in api_settings.allowed_suffix_set:
                raise UploadRejected(
                    f"不支持的文件类型 {suffix or '(无扩展名)'}，"
                    f"允许：{api_settings.allowed_upload_suffixes}",
                    code="unsupported_type",
                )

            saved = temp_dir / safe_name
            size = await _spool_to_disk(upload, saved)

            csv_path: Path | None = None
            if role in ("orders", "returns"):
                csv_path = temp_dir / f"{role}.csv"
                _to_utf8_csv(saved, csv_path, role)

            ds.files.append(UploadedFileInfo(
                field=field_name, filename=safe_name, size=size,
                saved_path=str(saved), role=role,
                csv_path=str(csv_path) if csv_path else None,
            ))
            if role == "orders":
                ds.orders_csv = csv_path
            elif role == "returns":
                ds.returns_csv = csv_path

        log.info("上传文件已就绪: %s", ds.summary())
        return ds
    except Exception:
        # 出错就把这次任务目录整个删掉：半截文件比没有文件更危险
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def cleanup(ds: TaskDataset | None) -> None:
    """任务处理完后清理临时目录（幂等，重复调用安全）。"""
    if ds is None or api_settings.keep_upload_files:
        return
    shutil.rmtree(ds.temp_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# 任务级数据源：临时替换全局单例，用完必定还原
# ----------------------------------------------------------------------
@contextmanager
def install_dataset(ds: TaskDataset | None) -> Iterator[None]:
    """在 with 作用域内，把全局数据源换成"本次任务上传的文件"。

    大白话：这段时间里 Agent 查的就是你要的那份表；出了这个 with，
    一切照旧（还是项目自带的 data/mock 数据）。

    技术细节：需要打两个补丁，少一个都会出现"有的工具读到新数据、有的读到旧数据"：
    - `src.data_source.data_source`：agent.py / safety/gate.py / llm_suggestion.py
      都是在函数里 `from src.data_source import data_source`（调用时才取），
      所以改模块属性即可生效；
    - `src.tools.business_tools.data_source`：它在模块顶层就 `from ... import data_source`
      把名字绑进了自己的命名空间，必须单独再改一次。

    并发注意：这是对全局状态的临时改写，所以**必须**保证同一时刻只有一个任务在跑。
    这个约束由 `worker.py` 里的执行锁负责，不要把本函数用到别处。
    """
    if ds is None or not ds.drives_query or not api_settings.use_uploaded_dataset:
        # 没上传、上传的不驱动查询、或显式关闭了该能力 -> 保持默认数据源
        yield
        return

    import src.data_source as data_source_module
    import src.tools.business_tools as business_tools_module
    from src.data_source import DataSource

    overrides: dict[str, str] = {}
    if ds.orders_csv is not None:
        overrides["orders_file"] = str(ds.orders_csv)
    if ds.returns_csv is not None:
        overrides["returns_file"] = str(ds.returns_csv)

    task_source = DataSource(**overrides)
    try:
        # 提前 load：列名不对/文件损坏会在这一步暴露，报错信息比运行时随机崩更清楚
        task_source.load()
    except Exception as e:
        raise UploadRejected(
            f"上传的数据文件无法加载（请确认列名与极客云导出一致）：{e}",
            code="dataset_load_failed",
        ) from e

    old_module_source = data_source_module.data_source
    old_tools_source = business_tools_module.data_source
    data_source_module.data_source = task_source
    business_tools_module.data_source = task_source
    log.info("本次任务启用上传数据集: orders=%s returns=%s",
             ds.orders_csv, ds.returns_csv)
    try:
        yield
    finally:
        # 无论任务成功、失败还是被取消，都必须还原，否则会污染下一个任务
        data_source_module.data_source = old_module_source
        business_tools_module.data_source = old_tools_source
