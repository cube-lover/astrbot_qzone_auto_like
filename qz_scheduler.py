import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from .qzone_post import QzonePoster


@dataclass
class SchedulerStatus:
    enabled: bool
    task_state: str
    running: bool
    interval_min: int
    daily_time: str
    delete_after_min: int
    mode: str
    fixed_text: str
    prompt: str
    daily_prompt: str
    provider_id: str
    mark: bool
    last_run_ts: float
    next_run: str
    pending_deletes: int


class QzScheduler:
    """Local scheduler for timed Qzone posting + deletion.

    - interval: every N minutes
    - daily: HH:MM every day
    - delete_after: delete post after N minutes (persisted to disk)
    """

    def __init__(
        self,
        *,
        context: Any,
        config: Any,
        my_qq: str,
        cookie: str,
        data_dir: Path,
        notify_cb=None,
    ):
        self.context = context
        self.config = config
        self.my_qq = str(my_qq or "").strip()
        self.cookie = str(cookie or "").strip()
        self.data_dir = Path(data_dir)
        self.notify_cb = notify_cb  # async fn(kind:str, msg:str)

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

        self._pending_delete_path = self.data_dir / "pending_deletes.json"
        self._pending_deletes: List[Dict[str, Any]] = []
        self._pending_lock = asyncio.Lock()
        self._load_pending_deletes()

    def running(self) -> bool:
        return self._task is not None and (not self._task.done())

    async def start(self) -> None:
        if self._task is not None and (not self._task.done()):
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except Exception:
            pass

    def _load_pending_deletes(self) -> None:
        try:
            if not self._pending_delete_path.exists():
                self._pending_deletes = []
                return
            data = json.loads(self._pending_delete_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                self._pending_deletes = []
                return
            items: List[Dict[str, Any]] = []
            for it in data:
                if not isinstance(it, dict):
                    continue
                tid = str(it.get("tid") or "").strip()
                due_ts = float(it.get("due_ts") or 0)
                created_ts = float(it.get("created_ts") or 0)
                if not tid or due_ts <= 0:
                    continue
                items.append({"tid": tid, "due_ts": due_ts, "created_ts": created_ts or time.time()})
            items.sort(key=lambda x: float(x.get("due_ts") or 0))
            self._pending_deletes = items
        except Exception as e:
            logger.warning(f"[Qzone] 加载 pending_deletes 失败: {e}")
            self._pending_deletes = []

    def _save_pending_deletes(self) -> None:
        try:
            self._pending_delete_path.parent.mkdir(parents=True, exist_ok=True)
            self._pending_delete_path.write_text(
                json.dumps(self._pending_deletes, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Qzone] 保存 pending_deletes 失败: {e}")

    async def queue_delete(self, tid: str, delete_after_min: int) -> None:
        t = str(tid or "").strip()
        if not t or delete_after_min <= 0:
            return
        now = time.time()
        due = now + delete_after_min * 60
        async with self._pending_lock:
            found = False
            for it in self._pending_deletes:
                if str(it.get("tid") or "") == t:
                    old_due = float(it.get("due_ts") or 0)
                    if old_due <= 0 or due < old_due:
                        it["due_ts"] = due
                    found = True
                    break
            if not found:
                self._pending_deletes.append({"tid": t, "due_ts": due, "created_ts": now})
            self._pending_deletes.sort(key=lambda x: float(x.get("due_ts") or 0))
            self._save_pending_deletes()

    async def _drain_due_deletes(self, poster: QzonePoster) -> int:
        now = time.time()
        due_items: List[Dict[str, Any]] = []
        async with self._pending_lock:
            if not self._pending_deletes:
                return 0
            keep: List[Dict[str, Any]] = []
            for it in self._pending_deletes:
                try:
                    due_ts = float(it.get("due_ts") or 0)
                except Exception:
                    due_ts = 0
                if due_ts > 0 and due_ts <= now:
                    due_items.append(it)
                else:
                    keep.append(it)
            self._pending_deletes = keep
            self._save_pending_deletes()

        ok_count = 0
        for it in due_items:
            tid = str(it.get("tid") or "").strip()
            if not tid:
                continue
            try:
                ds, dr = await asyncio.to_thread(poster.delete_by_tid, tid)
                ok = bool(ds == 200 and getattr(dr, "ok", False))
                logger.info(
                    "[Qzone] pending delete 执行 | status=%s ok=%s code=%s msg=%s tid=%s",
                    ds,
                    getattr(dr, "ok", False),
                    getattr(dr, "code", ""),
                    getattr(dr, "message", ""),
                    tid,
                )
                if ok:
                    ok_count += 1
                    if self.notify_cb:
                        try:
                            await self.notify_cb("delete", f"定时删说说成功 tid={tid}")
                        except Exception:
                            pass
                    continue

                async with self._pending_lock:
                    backoff_due = time.time() + 60
                    self._pending_deletes.append({"tid": tid, "due_ts": backoff_due, "created_ts": float(it.get("created_ts") or time.time())})
                    self._pending_deletes.sort(key=lambda x: float(x.get("due_ts") or 0))
                    self._save_pending_deletes()
            except Exception as e:
                logger.warning(f"[Qzone] pending delete 异常 tid={tid}: {e}")
                async with self._pending_lock:
                    backoff_due = time.time() + 60
                    self._pending_deletes.append({"tid": tid, "due_ts": backoff_due, "created_ts": float(it.get("created_ts") or time.time())})
                    self._pending_deletes.sort(key=lambda x: float(x.get("due_ts") or 0))
                    self._save_pending_deletes()

        return ok_count

    def _seconds_until(self, hhmm: str) -> Optional[int]:
        m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm or "")
        if not m:
            return None
        hh = int(m.group(1))
        mm = int(m.group(2))
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            return None
        now = time.time()
        lt = time.localtime(now)
        tgt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
        if tgt <= now:
            tgt += 86400
        return int(tgt - now)

    def _compute_next_run_str(self) -> str:
        interval_min = int(self.config.get("ai_post_interval_min", 0) or 0)
        daily_time = str(self.config.get("ai_post_daily_time", "") or "").strip()
        now = time.time()
        next_ts: Optional[float] = None
        if interval_min > 0:
            last = float(self.config.get("ai_post_last_run_ts", 0) or 0)
            base = last if last > 0 else now
            next_ts = base + interval_min * 60
        if daily_time:
            sec = self._seconds_until(daily_time)
            if sec is not None:
                dts = now + sec
                next_ts = dts if next_ts is None else min(next_ts, dts)
        if not next_ts:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_ts))

    def status(self) -> SchedulerStatus:
        enabled = bool(self.config.get("ai_post_enabled", False))
        interval_min = int(self.config.get("ai_post_interval_min", 0) or 0)
        daily_time = str(self.config.get("ai_post_daily_time", "") or "").strip()
        delete_after = int(self.config.get("ai_post_delete_after_min", 0) or 0)
        mode = str(self.config.get("ai_post_mode", "fixed") or "fixed").strip() or "fixed"
        fixed_text = str(self.config.get("ai_post_fixed_text", "") or "").strip()
        prompt = str(self.config.get("ai_post_prompt", "") or "").strip()
        daily_prompt = str(self.config.get("ai_post_daily_prompt", "") or "").strip()
        provider_id = str(self.config.get("ai_post_provider_id", "") or "").strip()
        mark = bool(self.config.get("ai_post_mark", True))
        last = float(self.config.get("ai_post_last_run_ts", 0) or 0)
        next_run = self._compute_next_run_str()
        task_state = "none"
        if self._task is not None:
            task_state = "done" if self._task.done() else "running"
        return SchedulerStatus(
            enabled=enabled,
            task_state=task_state,
            running=self.running(),
            interval_min=interval_min,
            daily_time=daily_time,
            delete_after_min=delete_after,
            mode=mode,
            fixed_text=fixed_text,
            prompt=prompt,
            daily_prompt=daily_prompt,
            provider_id=provider_id,
            mark=mark,
            last_run_ts=last,
            next_run=next_run,
            pending_deletes=len(self._pending_deletes),
        )

    async def _gen_and_post(self, poster: QzonePoster, prompt: str) -> None:
        mode = str(self.config.get("ai_post_mode", "fixed") or "fixed").strip() or "fixed"
        if mode == "fixed":
            content = str(self.config.get("ai_post_fixed_text", "") or "").strip() or str(prompt or "").strip()
            if not content:
                logger.error("[Qzone] AI post：fixed 模式未配置文本")
                return
            if len(content) > 120:
                content = content[:120].rstrip()
            status, result = await asyncio.to_thread(poster.publish_text, content)
            try:
                self.config["ai_post_last_run_ts"] = time.time()
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            ok = bool(status == 200 and getattr(result, "ok", False))
            logger.info(
                "[Qzone] fixed post 返回 | status=%s ok=%s code=%s msg=%s tid=%s",
                status,
                getattr(result, "ok", False),
                getattr(result, "code", ""),
                getattr(result, "message", ""),
                getattr(result, "tid", ""),
            )
            if ok and self.notify_cb:
                try:
                    await self.notify_cb("post", f"定时发说说成功 tid={getattr(result, 'tid', '')}")
                except Exception:
                    pass
            delete_after = int(self.config.get("ai_post_delete_after_min", 0) or 0)
            tid = getattr(result, "tid", "")
            if ok and delete_after > 0 and tid:
                await self.queue_delete(str(tid), delete_after)
            return

        # LLM mode is handled by main plugin; keep scheduler simple.
        logger.error("[Qzone] AI post：LLM 模式尚未在 QzScheduler 中实现（请用 fixed 模式或在 main.py 里实现 LLM 生成）")

    async def _worker(self) -> None:
        if not self.my_qq or not self.cookie:
            logger.error("[Qzone] AI post 配置缺失：my_qq 或 cookie 为空")
            return

        interval_min = int(self.config.get("ai_post_interval_min", 0) or 0)
        daily_time = str(self.config.get("ai_post_daily_time", "") or "").strip()
        if interval_min <= 0 and not daily_time:
            logger.info("[Qzone] AI post：未配置 interval/daily，任务退出")
            return

        poster = QzonePoster(self.my_qq, self.cookie)

        def next_interval_due_ts() -> Optional[float]:
            if interval_min <= 0:
                return None
            last = float(self.config.get("ai_post_last_run_ts", 0) or 0)
            if last <= 0:
                return time.time()
            return last + interval_min * 60

        def next_daily_due_ts() -> Optional[float]:
            if not daily_time:
                return None
            sec = self._seconds_until(daily_time)
            if sec is None:
                return None
            return time.time() + sec

        logger.info("[Qzone] AI post scheduler started interval_min=%s daily=%s delete_after=%s", interval_min, daily_time or "-", int(self.config.get("ai_post_delete_after_min", 0) or 0))

        while not self._stop.is_set():
            try:
                # drain deletes often
                try:
                    drained = await self._drain_due_deletes(poster)
                    if drained:
                        logger.info("[Qzone] pending deletes drained=%s", drained)
                except Exception as e:
                    logger.warning(f"[Qzone] drain pending deletes failed: {e}")

                enabled = bool(self.config.get("ai_post_enabled", False))
                if not enabled:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                    continue

                now = time.time()
                cands: List[float] = []
                its = next_interval_due_ts()
                if its is not None:
                    cands.append(its)
                dts = next_daily_due_ts()
                if dts is not None:
                    cands.append(dts)

                if not cands:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                    continue

                next_due = min(cands)
                if next_due > now:
                    sleep_s = min(30.0, max(0.0, next_due - now))
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
                    continue

                # due now: prefer interval if due
                if its is not None and now >= its - 0.5:
                    prompt = str(self.config.get("ai_post_prompt", "") or "").strip()
                    if prompt:
                        await self._gen_and_post(poster, prompt)
                    # jitter to avoid exact periodic signature
                    jitter = random.random() * 1.5
                    await asyncio.wait_for(self._stop.wait(), timeout=jitter)
                    continue

                if dts is not None and now >= dts - 0.5:
                    prompt = str(self.config.get("ai_post_daily_prompt", "") or "").strip()
                    if prompt:
                        await self._gen_and_post(poster, prompt)
                    jitter = random.random() * 1.5
                    await asyncio.wait_for(self._stop.wait(), timeout=jitter)
                    continue

                await asyncio.wait_for(self._stop.wait(), timeout=1)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"[Qzone] AI post scheduler 异常: {e}")
                await asyncio.sleep(5)
