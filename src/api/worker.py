# -*- coding: utf-8 -*-
"""异步任务队列 + 后台 Worker（PHASE 2 新增）。

【为什么需要它】
`Agent.run()` 是**同步阻塞**的：它要调大模型、读数据文件，一次可能跑几秒到几十秒。
如果在 HTTP 请求里直接调用，事件循环会被卡住 —— 期间所有其他请求（包括健康检查）
全部排队等它跑完。所以架构改成"接单即返回，后台慢慢做"：

    POST /submit ──> 建任务记录(processing) ──> 丢进 asyncio.Queue ──> 立刻返回 task_id
                                                      │
                            后台 Worker 协程 <─────────┘
                                    │
                                    └─ asyncio.to_thread(agent.run)  ← 丢到线程里跑，不阻塞事件循环
                                            │
                                            └─ 成功/失败/取消 → 回写任务终态

【为什么用 asyncio.to_thread 而不是把 Agent 改成 async】
Phase 1 的 Agent 与 4 个业务工具都是同步实现（openai SDK 同步客户端、pandas 读文件）。
改 async 相当于重写整条链路，风险远大于收益。`to_thread` 是标准做法：
让同步函数在独立线程里跑，事件循环照常服务其他请求。

【为什么执行阶段要加锁串行】
两个原因，都不是"为了保险"这么含糊：
1. `dataset.install_dataset` 会**临时改写全局数据源单例**，两个任务同时跑会串数据；
2. Agent 内部持有会话记忆 / 安全闸门 / 追踪器等进程内单例。
所以 `WORKERS>1` 目前只在"取任务"这一层并行，真正执行仍是串行的。
要真正并行，需要把 Agent 改成无状态（每任务独立实例 + 独立数据源），
这是后续阶段的事 —— 这里把约束显式写出来，而不是假装支持。
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from src.agent import TaskCancelled, TicketAgent, create_agent
from src.api.dataset import TaskDataset
from src.api.dataset import cleanup as cleanup_dataset
from src.api.dataset import install_dataset
from src.api.settings import api_settings
from src.logger import log
from src.persistence import crud
from src.persistence.database import get_db as _session_scope


# ----------------------------------------------------------------------
# 队列元素
# ----------------------------------------------------------------------
@dataclass
class TaskJob:
    """一条待处理任务（队列里流动的东西）。"""

    task_id: str                 # 已在数据库里建好的任务编号
    user_input: str              # 用户的自然语言诉求
    customer: str                # 客户名（同时作为会话隔离键）
    session_id: Optional[str] = None   # 显式会话键；不传则由 Agent 按客户分桶
    dataset: Optional[TaskDataset] = None   # 本次上传的数据文件（可空）


# ----------------------------------------------------------------------
# 任务终态回写（集中一处，避免散落）
# ----------------------------------------------------------------------
def _finalize(task_id: str, status: str, *, summary: Optional[dict] = None,
              only_if_open: bool = False) -> None:
    """把任务状态写回数据库。

    大白话：把"这件事最后怎么样了"记到账本上。

    技术细节：写库失败只记 ERROR 日志、绝不向上抛 —— 账本写不进去不该让 Worker 挂掉。
    `only_if_open=True` 表示"仅当任务还没结束才覆盖"，避免把已经 completed
    的任务误改成 failed（例如异常发生在收尾阶段时）。
    """
    try:
        with _session_scope() as db:
            if only_if_open:
                task = crud.get_task_by_id(db, task_id)
                if task is None or task.status in ("completed", "failed", "cancelled"):
                    return
            crud.update_task_status(db, task_id, status, result_summary=summary)
    except Exception as e:  # pragma: no cover - 只有库挂了才会走到
        log.error("Worker：回写任务状态失败(task_id=%s -> %s): %s", task_id, status, e)


# ----------------------------------------------------------------------
# 默认 Agent 工厂
# ----------------------------------------------------------------------
def build_default_agent() -> TicketAgent:
    """按配置造出跑任务用的 Agent 实例。

    大白话：`AGENT_FORCE_MOCK=1` 时完全不碰大模型（离线演示、自动化测试用）；
    否则有 key 就走真实模型、没 key 自动降级 mock（Phase 1 既有行为）。
    """
    agent = create_agent(mock=api_settings.agent_force_mock)
    log.info("Worker：Agent 已就绪 mock=%s use_llm=%s persistence=%s",
             agent.mock, agent.use_llm, agent.persist)
    return agent


# ----------------------------------------------------------------------
# Worker
# ----------------------------------------------------------------------
class TaskWorker:
    """后台任务消费者。

    生命周期由 FastAPI 的 lifespan 管理：启动时 `await start()`，关闭时 `await stop()`。
    """

    def __init__(self,
                 agent_factory: Optional[Callable[[], TicketAgent]] = None,
                 concurrency: Optional[int] = None) -> None:
        self._agent_factory = agent_factory or build_default_agent
        self._concurrency = max(1, int(concurrency or api_settings.workers))
        self._queue: asyncio.Queue[TaskJob] = asyncio.Queue(
            maxsize=max(0, int(api_settings.max_queue_size))
        )
        self._consumers: list[asyncio.Task] = []
        self._agent: Optional[TicketAgent] = None

        # 同一时刻只允许一个任务真正执行（原因见模块头注释）
        self._run_lock = threading.Lock()

        # 取消标志位：task_id -> threading.Event。
        # 用 Event 而不是 bool，是因为它要被"事件循环线程"写、"工作线程"读，
        # Event 内部有锁保证跨线程可见性；普通变量在 CPython 虽然大概率也能读到，
        # 但没有内存可见性保证，属于"能跑但不对"。
        self._cancel_flags: dict[str, threading.Event] = {}
        self._queued: set[str] = set()      # 已入队、尚未开始执行
        self._running: set[str] = set()     # 正在执行
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动 Worker 协程。重复调用安全（幂等）。"""
        if self._started:
            return
        # 队列每次启动重建：asyncio 的 Queue 绑定"第一次使用它的那个事件循环"，
        # 跨事件循环复用会直接抛 RuntimeError（测试里反复建/停服务就会踩到）。
        # 反正启动时队列本来就该是空的，重建没有任何副作用。
        self._queue = asyncio.Queue(maxsize=max(0, int(api_settings.max_queue_size)))
        self._agent = self._agent_factory()
        if self._concurrency > 1:
            # 把"名不副实"这件事说出来：配 WORKERS>1 目前只提升取任务并行度，
            # 真正执行仍被 _run_lock 串行（任务级数据源要临时改写全局单例）。
            # 不说的话很容易被当成"已经支持并发处理"。
            log.warning("Worker 配置为 %d，但当前实现**执行阶段是串行的**"
                        "（Agent/数据源依赖进程内单例）；多出来的协程只并行取任务。"
                        "需要真并发请先让 Agent 无状态化并换外部队列。",
                        self._concurrency)
        self._consumers = [
            asyncio.create_task(self._consume(i), name=f"agent-worker-{i}")
            for i in range(self._concurrency)
        ]
        self._started = True
        log.info("后台 Worker 已启动 (workers=%d, queue_max=%s)",
                 self._concurrency, api_settings.max_queue_size or "无上限")

    async def stop(self) -> None:
        """停止 Worker（取消协程并等待退出）。"""
        self._started = False
        for task in self._consumers:
            task.cancel()
        if self._consumers:
            await asyncio.gather(*self._consumers, return_exceptions=True)
        self._consumers = []
        log.info("后台 Worker 已停止")

    # ------------------------------------------------------------------
    # 生产端
    # ------------------------------------------------------------------
    async def submit(self, job: TaskJob) -> None:
        """把任务放进队列。

        异常：
            asyncio.QueueFull —— 队列满了。调用方（路由）应翻译成 503，
            提醒调用方稍后重试，而不是无限期把请求堆在内存里。
        """
        await self._queue.put(job)
        self._queued.add(job.task_id)
        self._cancel_flags.setdefault(job.task_id, threading.Event())
        log.info("任务已入队 task_id=%s (队列长度=%d)", job.task_id, self._queue.qsize())

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def request_cancel(self, task_id: str) -> str:
        """请求取消任务。返回取消生效的时机：queued / running / unknown。

        大白话：取消是"协作式"的 —— 我们只能立个牌子告诉 Agent"别干了"，
                没法从外面直接把正在跑的函数掐死。所以：
                - 还在排队 → 立刻取消，Worker 取到它时会跳过；
                - 正在执行 → 设置标志位，Agent 跑到下一个检查点（每轮 LLM 调用前）就停。
        """
        flag = self._cancel_flags.get(task_id)
        if task_id in self._running:
            if flag is not None:
                flag.set()
            return "running"
        if task_id in self._queued:
            if flag is not None:
                flag.set()
            return "queued"
        return "unknown"

    # ------------------------------------------------------------------
    # 消费端
    # ------------------------------------------------------------------
    async def _consume(self, index: int) -> None:
        """消费者协程：不停地从队列取任务并处理。"""
        while True:
            job = await self._queue.get()
            try:
                await self._handle(job)
            except asyncio.CancelledError:
                # 关闭服务时被取消：必须落一个**终态**，否则这条任务会永远停在
                # processing，调用方轮询到天荒地老也等不到结果。
                _finalize(job.task_id, "failed",
                          summary={"error": "服务关闭导致任务中断，请重新提交"},
                          only_if_open=True)
                cleanup_dataset(job.dataset)
                raise
            except Exception as e:  # pragma: no cover - 兜底，保证单个任务炸不掉整个 Worker
                log.exception("Worker#%d 处理任务异常 task_id=%s: %s", index, job.task_id, e)
                _finalize(job.task_id, "failed",
                          summary={"error": f"{type(e).__name__}: {e}"},
                          only_if_open=True)
            finally:
                self._queue.task_done()

    async def _handle(self, job: TaskJob) -> None:
        """处理单个任务：执行 + 清理。"""
        flag = self._cancel_flags.setdefault(job.task_id, threading.Event())
        self._queued.discard(job.task_id)

        # 排队期间就被取消 -> 连跑都不用跑
        if flag.is_set():
            log.info("任务在排队期间被取消，跳过执行 task_id=%s", job.task_id)
            _finalize(job.task_id, "cancelled",
                      summary={"error": "任务在排队期间被取消"}, only_if_open=True)
            self._cancel_flags.pop(job.task_id, None)
            cleanup_dataset(job.dataset)
            return

        self._running.add(job.task_id)
        try:
            # 关键：同步的 Agent.run 丢进线程池，事件循环立刻空出来继续服务 HTTP 请求
            await asyncio.to_thread(self._execute, job, flag)
        finally:
            self._running.discard(job.task_id)
            self._cancel_flags.pop(job.task_id, None)
            cleanup_dataset(job.dataset)   # 上传的临时文件用完即删

    def _execute(self, job: TaskJob, flag: threading.Event) -> None:
        """在工作线程里真正执行 Agent（同步阻塞）。"""
        with self._run_lock:
            try:
                # 用本次任务上传的数据源（没上传就是默认的 data/mock）
                with install_dataset(job.dataset):
                    result = self._require_agent().run(
                        job.user_input,
                        customer=job.customer,
                        session_id=job.session_id,
                        task_id=job.task_id,          # 复用 API 已建好的任务编号
                        cancel_check=flag.is_set,     # 协作式取消：Agent 到检查点自查
                        # 收尾回调：Agent 会在写 completed **之前**调用它，
                        # 保证调用方看到终态时，上传的临时文件已经删干净了
                        #（清理原本在 _handle 的 finally 里，晚于终态，会形成竞态）。
                        on_completed=lambda: cleanup_dataset(job.dataset),
                    )
                log.info("任务处理完成 task_id=%s status=%s",
                         job.task_id, "cancelled" if result.get("cancelled") else "completed")
            except TaskCancelled:
                # 真实 Agent 会在 run() 内部就把状态落成 cancelled（且摘要更完整），
                # 走到这里时 only_if_open 会让它自动跳过。
                # 保留这一次兜底是为了保证"终态由 Worker 负责"这个不变量：
                # 只要有任何一条路径抛出了 TaskCancelled 而没落库，
                # 任务也绝不会永远停在 processing 上。
                _finalize(job.task_id, "cancelled",
                          summary={"error": "任务被用户取消"}, only_if_open=True)
                log.info("任务已取消 task_id=%s", job.task_id)
            except Exception as e:
                # run() 已经落过 failed；这里再兜一次是防止异常发生在 run() 之外
                # （比如 install_dataset 阶段数据文件加载失败）
                log.exception("任务执行失败 task_id=%s", job.task_id)
                _finalize(job.task_id, "failed",
                          summary={"error": f"{type(e).__name__}: {e}"},
                          only_if_open=True)

    # ------------------------------------------------------------------
    def _require_agent(self) -> TicketAgent:
        if self._agent is None:  # pragma: no cover - start() 之前不该被调用
            self._agent = self._agent_factory()
        return self._agent

    @property
    def stats(self) -> dict:
        """运行指标（/metrics 用）。"""
        return {
            "queue_size": self._queue.qsize(),
            "running": len(self._running),
            "workers": self._concurrency,
            "worker_alive": self._started and any(not t.done() for t in self._consumers),
        }


# 全局唯一 Worker（与 Phase 1 的 settings / tracer 同风格：模块级单例 + 可注入）
task_worker = TaskWorker()
