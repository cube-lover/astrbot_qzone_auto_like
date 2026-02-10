import asyncio
import json
import random
import re
import time
import traceback
from pathlib import Path
from typing import Optional, Set, Tuple, List, Dict, Any

from .qzone_post import QzonePoster
from .qzone_sleep import sleep_seconds
from .qzone_comment import QzoneCommenter
from .qzone_del_comment import QzoneCommentDeleter
from .qzone_feed_fetch import QzoneFeedFetcher
from .qzone_protect import QzoneProtectScanner
from urllib.parse import quote

import requests

from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import ToolSet
from astrbot.api import logger


def _now_hms() -> str:
    return time.strftime("%H:%M:%S")


def _get_gtk(skey: str) -> int:
    hash_val = 5381
    for ch in skey:
        hash_val += (hash_val << 5) + ord(ch)
    return hash_val & 0x7FFFFFFF


def _pick_skey_for_gtk(cookie: str) -> str:
    """Pick a usable skey value from cookie for g_tk calculation.

    Qzone commonly uses p_skey, but some cookie sets only have skey or media_p_skey.
    """

    for key in ("p_skey", "skey", "media_p_skey"):
        v = _extract_cookie_value(cookie, key)
        if v:
            return v
    return ""


def _extract_cookie_value(cookie: str, key: str) -> str:
    if not cookie:
        return ""
    for item in cookie.split(";"):
        item = item.strip()
        if item.startswith(key + "="):
            return item.split("=", 1)[1]
    return ""


def _sanitize_cookie_for_log(cookie_str: str) -> str:
    # Cookie 属于登录态，默认不输出任何可关联信息。
    if not cookie_str:
        return ""

    has_p_skey = bool(_extract_cookie_value(cookie_str, "p_skey"))
    return f"<cookie:redacted has_p_skey={has_p_skey}>"


class _QzoneClient:
    def __init__(self, my_qq: str, cookie: str):
        # my_qq: 当前登录 Cookie 对应的 QQ（用于 referer / opuin）
        self.my_qq = my_qq

        # 兼容用户从 DevTools 里复制整行 "cookie: ..." 的情况
        cookie = (cookie or "").strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()

        self.cookie = cookie

        skey_for_gtk = _pick_skey_for_gtk(cookie)
        if not skey_for_gtk:
            raise ValueError("cookie 缺少 p_skey/skey/media_p_skey（无法计算 g_tk）")

        self.g_tk = _get_gtk(skey_for_gtk)
        self.headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            ),
            "cookie": cookie,
            "referer": f"https://user.qzone.qq.com/{my_qq}",
        }

    def fetch_keys(self, count: int, target_qq: Optional[str] = None) -> Tuple[int, Set[str], int]:
        """拉取目标空间的动态链接集合。

        该接口用于“手动 /点赞”（支持 target_qq + 分页/扩展）。
        自动轮询不走这里（自动轮询用 legacy 自用接口，见 fetch_keys_self_legacy）。
        """
        target = str(target_qq or self.my_qq).strip()

        # feeds_html_act_all 参数含义：uin=登录QQ，hostuin=目标空间QQ
        feeds_url = (
            "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
            f"feeds_html_act_all?uin={self.my_qq}&hostuin={target}"
            f"&scope=0&filter=all&flag=1&refresh=0&firstGetGroup=0&mixnocache=0&scene=0"
            f"&begintime=undefined&icServerTime=&start=0&count={count}"
            f"&sidomain=qzonestyle.gtimg.cn&useutf8=1&outputhtmlfeed=1&refer=2"
            f"&r={random.random()}&g_tk={self.g_tk}"
        )
        res = requests.get(feeds_url, headers=self.headers, timeout=20)
        status = res.status_code
        text_len = len(res.text or "")

        raw_links = re.findall(
            r"(http[s]?[:\\/]+user\.qzone\.qq\.com[:\\/]+\d+[:\\/]+mood[:\\/]+[a-f0-9]+)",
            res.text or "",
        )
        keys = {link.replace("\\", "") for link in raw_links}
        return status, keys, text_len

    def fetch_keys_self_legacy(self, count: int) -> Tuple[int, Set[str], int]:
        """自动轮询专用：旧版 feeds3_html_more（仅拉取自己的说说）。

        你这边实测该接口更稳定能返回 mood 链接；只用于 worker，不影响手动 /点赞。
        """
        feeds_url = (
            "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
            f"feeds3_html_more?uin={self.my_qq}&scope=0&view=1&flag=1&refresh=1&count={count}"
            f"&outputhtmlfeed=1&g_tk={self.g_tk}"
        )
        res = requests.get(feeds_url, headers=self.headers, timeout=20)
        status = res.status_code
        text_len = len(res.text or "")

        raw_links = re.findall(
            r"(http[s]?[:\\/]+user\.qzone\.qq\.com[:\\/]+\d+[:\\/]+mood[:\\/]+[a-f0-9]+)",
            res.text or "",
        )
        keys = {link.replace("\\", "") for link in raw_links}
        return status, keys, text_len

    def send_like(self, full_key: str) -> Tuple[int, str]:
        # 复刻浏览器：h5.qzone.qq.com 的 proxy/domain -> w.qzone.qq.com likes CGI。
        like_url = f"https://h5.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app?g_tk={self.g_tk}"

        headers = dict(self.headers)
        headers["origin"] = "https://user.qzone.qq.com"
        headers["referer"] = "https://user.qzone.qq.com/"

        # full_key 形如：http(s)://user.qzone.qq.com/<hostuin>/mood/<fid>.1
        # 浏览器实际传的是不带 .1 的 unikey/curkey，并额外带 from/abstime/fid 等字段。
        hostuin = ""
        fid = full_key
        m = re.search(r"user\.qzone\.qq\.com/(\d+)/mood/([a-f0-9]+)", full_key)
        if m:
            hostuin = m.group(1)
            fid = m.group(2)
        else:
            if fid.endswith(".1"):
                fid = fid[:-2]
            if "/mood/" in fid:
                fid = fid.split("/mood/", 1)[1]

        payload = {
            "qzreferrer": f"https://user.qzone.qq.com/",
            "opuin": self.my_qq,
            "unikey": full_key[:-2] if full_key.endswith(".1") else full_key,
            "curkey": full_key[:-2] if full_key.endswith(".1") else full_key,
            "from": "1",
            "appid": "311",
            "typeid": "0",
            "abstime": str(int(time.time())),
            "fid": fid,
            "active": "0",
            "fupdate": "1",
        }

        # 与浏览器一致：如果能解析到 hostuin，就把更完整的 qzreferrer 补上。
        if hostuin:
            payload["qzreferrer"] = (
                "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds_html_module"
                "?g_iframeUser=1"
                f"&i_uin={hostuin}"
                f"&i_login_uin={self.my_qq}"
                "&mode=4&previewV8=1&style=35&version=8&needDelOpr=true&transparence=true"
                "&hideExtend=false&showcount=5"
                "&MORE_FEEDS_CGI=http%3A%2F%2Fic2.s8.qzone.qq.com%2Fcgi-bin%2Ffeeds%2Ffeeds_html_act_all"
                "&refer=2"
                "&paramstring=os-winxp%7C100"
            )

        res = requests.post(like_url, headers=headers, data=payload, timeout=20)
        return res.status_code, res.text or ""


@register(
    name="qzone_auto_like",
    author="AI",
    desc="自动侦测并点赞QQ空间动态（强后台日志版）",
    version="1.6.3",
    repo="",
)
class QzoneAutoLikePlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config or {}

        # 运行时：目标空间（若为空则监控/点赞自己的空间）
        self._target_qq: str = ""
        self._manual_like_limit: int = 0

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # AI 定时发说说任务（不依赖群聊名单；按配置开关）
        self._ai_task: Optional[asyncio.Task] = None
        self._ai_stop = asyncio.Event()

        # AI post notifications (optional; default off to avoid spamming groups)
        self.ai_post_notify_enabled = bool(self.config.get("ai_post_notify_enabled", False))
        self.ai_post_notify_mode = str(self.config.get("ai_post_notify_mode", "error") or "error").strip().lower()
        if self.ai_post_notify_mode not in ("off", "error", "all"):
            self.ai_post_notify_mode = "error"
        self.ai_post_notify_private_qq = str(self.config.get("ai_post_notify_private_qq", "") or "").strip()
        self.ai_post_notify_group_id = str(self.config.get("ai_post_notify_group_id", "") or "").strip()

        self.ai_post_delete_notify_enabled = bool(self.config.get("ai_post_delete_notify_enabled", False))
        self.ai_post_delete_notify_mode = str(self.config.get("ai_post_delete_notify_mode", "error") or "error").strip().lower()
        if self.ai_post_delete_notify_mode not in ("off", "error", "all"):
            self.ai_post_delete_notify_mode = "error"
        self.ai_post_delete_notify_private_qq = str(self.config.get("ai_post_delete_notify_private_qq", "") or "").strip()
        self.ai_post_delete_notify_group_id = str(self.config.get("ai_post_delete_notify_group_id", "") or "").strip()

        self._liked: Set[str] = set()
        self._data_path = Path(__file__).parent / "data" / "liked_records.json"

        # In-memory: last posted tid/content for quick follow-up actions.
        self._last_tid: str = ""
        self._last_post_text: str = ""

        # In-memory: recent successful comment refs for quick deletion (no disk persistence).
        # Each item: {'topicId': str, 'commentId': str, 'ts': float}
        self._recent_comment_refs: list[dict] = []
        self._comment_ref_max = int(self.config.get("comment_ref_max", 50) or 50)
        if self._comment_ref_max < 0:
            self._comment_ref_max = 0

        # Optional small on-disk store for recent tids (bounded, overwrites file).
        self._tid_path = Path(__file__).parent / "data" / "recent_tids.json"
        self._recent_tids: list[str] = []
        self._tid_store_max = int(self.config.get("tid_store_max", 200) or 200)
        if self._tid_store_max < 0:
            self._tid_store_max = 0
        self._load_recent_tids()

        # Optional store for recent posts (tid->text). Used for auto-comment without extra API calls.
        self._post_path = Path(__file__).parent / "data" / "recent_posts.json"
        self._recent_posts: list[dict] = []
        self._post_store_max = int(self.config.get("post_store_max", 200) or 200)
        if self._post_store_max < 0:
            self._post_store_max = 0
        self._load_recent_posts()

        # Pending delete queue for AI timed posts (persist across restart).
        # Each item: {"tid": str, "due_ts": float, "created_ts": float}
        self._pending_delete_path = Path(__file__).parent / "data" / "pending_deletes.json"
        self._pending_deletes: List[Dict[str, Any]] = []
        self._pending_delete_lock = asyncio.Lock()
        self._load_pending_deletes()

        # 仅用于自动轮询的“内存去重”（不落盘）：避免每轮重复点同一条。
        self._auto_seen: dict[str, float] = {}

        self.my_qq = str(self.config.get("my_qq", "")).strip()
        self.cookie = str(self.config.get("cookie", "")).strip()
        self._target_qq = str(self.config.get("target_qq", "")).strip()
        self.poll_interval = int(self.config.get("poll_interval_sec", 20))
        # 风控友好：默认放慢点赞间隔（可在配置里改回去）
        self.delay_min = int(self.config.get("like_delay_min_sec", 12))
        self.delay_max = int(self.config.get("like_delay_max_sec", 25))
        if self.delay_min > self.delay_max:
            self.delay_min, self.delay_max = self.delay_max, self.delay_min
        self.max_feeds = int(self.config.get("max_feeds_count", 15))
        self.persist = False

        self.enabled = bool(self.config.get("enabled", False))
        self.auto_start = bool(self.config.get("auto_start", False))

        # 护评：后台轮询评论区（基于 feeds3_html_more 回包内嵌 comments-list HTML）
        self.protect_enabled = bool(self.config.get("protect_enabled", False))
        self.protect_window_minutes = int(self.config.get("protect_window_minutes", 30) or 30)
        self.protect_notify_mode = str(self.config.get("protect_notify_mode", "error") or "error").strip().lower()
        if self.protect_notify_mode not in ("off", "error", "all"):
            self.protect_notify_mode = "error"
        self.protect_poll_interval = int(self.config.get("protect_poll_interval_sec", 10) or 10)
        if self.protect_poll_interval <= 0:
            self.protect_poll_interval = 10
        self.protect_pages = int(self.config.get("protect_pages", 2) or 2)
        if self.protect_pages <= 0:
            self.protect_pages = 1

        self._protect_task: Optional[asyncio.Task] = None
        self._protect_stop = asyncio.Event()
        self._protect_seen: dict[str, float] = {}

        # Some AstrBot builds don't reliably call on_astrbot_loaded for plugins.
        # To make protect actually run, schedule a best-effort autostart here.
        self._protect_last_scan = ""
        self._protect_last_delete = ""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(asyncio.create_task, self._maybe_start_protect_task())
        except Exception:
            try:
                asyncio.get_event_loop().call_soon(asyncio.create_task, self._maybe_start_protect_task())
            except Exception:
                pass

        # 去掉缓存/去重机制：不加载历史点赞记录

        logger.info(
            "[Qzone] 插件初始化 | my_qq=%s poll=%ss delay=[%s,%s] max_feeds=%s persist=%s enabled=%s auto_start=%s protect=%s protect_window_min=%s protect_notify=%s cookie=%s",
            self.my_qq,
            self.poll_interval,
            self.delay_min,
            self.delay_max,
            self.max_feeds,
            self.persist,
            self.enabled,
            self.auto_start,
            self.protect_enabled,
            self.protect_window_minutes,
            self.protect_notify_mode,
            _sanitize_cookie_for_log(self.cookie),
        )

    def _load_records(self) -> None:
        if not self._data_path.exists():
            return
        try:
            data = json.loads(self._data_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._liked = set(str(x) for x in data)
        except Exception as e:
            logger.error(f"[Qzone] 加载点赞记录失败: {e}")

    def _load_recent_tids(self) -> None:
        if self._tid_store_max <= 0:
            return
        if not self._tid_path.exists():
            return
        try:
            data = json.loads(self._tid_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._recent_tids = [str(x) for x in data if str(x).strip()]
        except Exception as e:
            logger.warning(f"[Qzone] 加载 recent_tids 失败: {e}")

    def _save_recent_tids(self) -> None:
        if self._tid_store_max <= 0:
            return
        try:
            self._tid_path.parent.mkdir(parents=True, exist_ok=True)
            self._tid_path.write_text(
                json.dumps(self._recent_tids[-self._tid_store_max :], ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Qzone] 保存 recent_tids 失败: {e}")

    def _remember_tid(self, tid: str) -> None:
        t = (tid or "").strip()
        if not t:
            return
        self._last_tid = t
        if self._tid_store_max <= 0:
            return
        if t in self._recent_tids:
            self._recent_tids.remove(t)
        self._recent_tids.append(t)
        if len(self._recent_tids) > self._tid_store_max:
            self._recent_tids = self._recent_tids[-self._tid_store_max :]
        self._save_recent_tids()

    def _load_recent_posts(self) -> None:
        if self._post_store_max <= 0:
            return
        if not self._post_path.exists():
            return
        try:
            data = json.loads(self._post_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                items = []
                for x in data:
                    if isinstance(x, dict) and str(x.get("tid", "")).strip():
                        items.append(
                            {
                                "tid": str(x.get("tid")),
                                "text": str(x.get("text", "")),
                                "ts": float(x.get("ts", 0) or 0),
                            }
                        )
                self._recent_posts = items
        except Exception as e:
            logger.warning(f"[Qzone] 加载 recent_posts 失败: {e}")

    def _save_recent_posts(self) -> None:
        if self._post_store_max <= 0:
            return
        try:
            self._post_path.parent.mkdir(parents=True, exist_ok=True)
            self._post_path.write_text(
                json.dumps(self._recent_posts[-self._post_store_max :], ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Qzone] 保存 recent_posts 失败: {e}")

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
            # keep sorted by due
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

    async def _queue_delete(self, tid: str, delete_after_min: int) -> None:
        t = str(tid or "").strip()
        if not t or delete_after_min <= 0:
            return
        now = time.time()
        due = now + delete_after_min * 60
        async with self._pending_delete_lock:
            # de-dup by tid (keep earliest due)
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
        """Try deleting all due items. Returns number deleted successfully."""
        now = time.time()
        due_items: List[Dict[str, Any]] = []
        async with self._pending_delete_lock:
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
            # tentatively remove due items; if delete fails we will requeue with backoff below
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
                    continue

                # requeue failed delete with small backoff (avoid tight loop)
                async with self._pending_delete_lock:
                    backoff_due = time.time() + 60
                    self._pending_deletes.append({"tid": tid, "due_ts": backoff_due, "created_ts": float(it.get("created_ts") or time.time())})
                    self._pending_deletes.sort(key=lambda x: float(x.get("due_ts") or 0))
                    self._save_pending_deletes()
            except Exception as e:
                logger.warning(f"[Qzone] pending delete 异常 tid={tid}: {e}")
                async with self._pending_delete_lock:
                    backoff_due = time.time() + 60
                    self._pending_deletes.append({"tid": tid, "due_ts": backoff_due, "created_ts": float(it.get("created_ts") or time.time())})
                    self._pending_deletes.sort(key=lambda x: float(x.get("due_ts") or 0))
                    self._save_pending_deletes()

        return ok_count

    def _remember_post(self, tid: str, text: str) -> None:
        t = (tid or "").strip()
        if not t:
            return
        self._remember_tid(t)
        self._last_post_text = (text or "")
        if self._post_store_max <= 0:
            return
        self._recent_posts = [x for x in self._recent_posts if str(x.get("tid")) != t]
        self._recent_posts.append({"tid": t, "text": (text or ""), "ts": time.time()})
        if len(self._recent_posts) > self._post_store_max:
            self._recent_posts = self._recent_posts[-self._post_store_max :]
        self._save_recent_posts()

    def _save_records(self) -> None:
        if not self.persist:
            return
        try:
            self._data_path.parent.mkdir(parents=True, exist_ok=True)
            self._data_path.write_text(
                json.dumps(sorted(self._liked), ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[Qzone] 保存点赞记录失败: {e}")

    def _is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)
        self.config["enabled"] = self.enabled
        try:
            # AstrBotConfig 支持 save_config；普通 dict 没有
            if hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[Qzone] 保存 enabled 配置失败: {e}")

    async def _maybe_autostart(self) -> None:
        if not self.auto_start:
            return
        if not self.enabled:
            logger.info("[Qzone] auto_start 开启，但 enabled=false，不自动启动")
            return
        if self._is_running():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._worker())
        logger.info("[Qzone] auto_start：任务已自动启动")

    def _ai_enabled(self) -> bool:
        return bool(self.config.get("ai_post_enabled", False))

    async def _maybe_start_ai_task(self) -> None:
        # Start only when enabled and interval/daily is configured.
        if not self._ai_enabled():
            return

        interval_min = int(self.config.get("ai_post_interval_min", 0) or 0)
        daily_time = str(self.config.get("ai_post_daily_time", "") or "").strip()
        if interval_min <= 0 and not daily_time:
            return

        if self._ai_task is not None and not self._ai_task.done():
            return
        self._ai_stop.clear()
        self._ai_task = asyncio.create_task(self._ai_poster_worker())
        logger.info("[Qzone] AI post：任务已启动")

    async def _maybe_start_protect_task(self) -> None:
        if not self.protect_enabled:
            return
        if not self.my_qq or not self.cookie:
            return
        if self._protect_task is not None and (not self._protect_task.done()):
            return

        self._protect_stop.clear()
        self._protect_task = asyncio.create_task(self._protect_worker())
        logger.info("[Qzone] protect worker task created (fallback)")

    def _try_parse_and_apply_ai_schedule(self, text: str) -> tuple[bool, str]:
        """Parse natural language schedule settings and apply to config.

        Returns:
            (ok, message)
        """

        src = (text or "").strip()
        if not src:
            return False, ""

        # Only attempt when it looks like a schedule request.
        if not ("每" in src or "分钟" in src or "interval" in src or "daily" in src or "删" in src or "删除" in src):
            return False, ""

        # interval minutes
        interval = None
        m = re.search(r"每隔\s*(\d{1,3})\s*分", src)
        if not m:
            m = re.search(r"每\s*(\d{1,3})\s*分", src)
        if not m:
            m = re.search(r"(\d{1,3})\s*(?:min|mins|minutes)\b", src, re.I)
        if not m:
            m = re.search(r"(\d{1,3})\s*分钟", src)
        if m:
            try:
                interval = int(m.group(1))
            except Exception:
                interval = None

        delete_after = None
        m = re.search(r"(\d{1,3})\s*分钟后\s*(?:自动)?(?:删除|删)", src)
        if not m:
            m = re.search(r"删后\s*(\d{1,3})", src)
        if m:
            try:
                delete_after = int(m.group(1))
            except Exception:
                delete_after = None

        # daily time HH:MM
        daily = None
        m = re.search(r"每天\s*(\d{1,2}:\d{2})", src)
        if not m:
            m = re.search(r"daily\s*(\d{1,2}:\d{2})", src, re.I)
        if m:
            daily = m.group(1)

        # content/prompt extraction
        prompt = ""
        m = re.search(r"内容(?:是|为)?\s*([^，。,\n\r]+)", src)
        if m:
            prompt = m.group(1).strip()
        if not prompt:
            # patterns like: 发一条XXX说说
            m = re.search(r"发(?:一条|1条)?\s*([^，。,\n\r]{1,80})\s*说说", src)
            if m:
                prompt = m.group(1).strip()
        if not prompt:
            # fallback: take tail after interval phrase
            m = re.search(r"分钟\s*发(?:一条|1条)?\s*([^，。,\n\r]{1,80})", src)
            if m:
                prompt = m.group(1).strip()

        changed = []

        if interval is not None and interval > 0:
            self.config["ai_post_interval_min"] = interval
            changed.append(f"间隔={interval}分钟")

        if daily:
            self.config["ai_post_daily_time"] = daily
            changed.append(f"每天={daily}")

        if delete_after is not None and delete_after >= 0:
            self.config["ai_post_delete_after_min"] = delete_after
            changed.append(f"删后={delete_after}分钟")

        if prompt:
            self.config["ai_post_prompt"] = prompt
            # Also set fixed-text by default; user said they don't want LLM.
            if not str(self.config.get("ai_post_fixed_text", "") or "").strip():
                self.config["ai_post_fixed_text"] = prompt
            self.config["ai_post_mode"] = str(self.config.get("ai_post_mode", "fixed") or "fixed")
            changed.append(f"提示词={prompt}")

        if not changed:
            return False, ""

        # enable
        self.config["ai_post_enabled"] = True

        mode = str(self.config.get("ai_post_mode", "fixed") or "fixed")
        msg = "✅ 已按自然语言设置定时任务：" + " | ".join(changed) + f"\n模式={mode}（fixed=固定文本，不调用LLM）\n已自动开启：，定时任务 列表 可查看状态"
        return True, msg

    async def _ai_poster_worker(self) -> None:
        if not self.my_qq or not self.cookie:
            logger.error("[Qzone] AI post 配置缺失：my_qq 或 cookie 为空")
            return

        interval_min = int(self.config.get("ai_post_interval_min", 0) or 0)
        daily_time = str(self.config.get("ai_post_daily_time", "") or "").strip()
        if interval_min <= 0 and not daily_time:
            logger.info("[Qzone] AI post：未配置 interval/daily，任务退出")
            return

        # 固定发到当前登录空间
        target_umo = None
        try:
            # umo 用 None 取默认 provider；发送消息用当前会话不好拿，这里仅后台发，不回群
            target_umo = None
        except Exception:
            target_umo = None

        poster = QzonePoster(self.my_qq, self.cookie)

        async def _send_private(to_qq: str, msg: str) -> None:
            to_qq = str(to_qq or "").strip()
            if not to_qq:
                return
            if hasattr(self.context, "send_private_message"):
                await self.context.send_private_message(user_id=to_qq, message=msg)
                return
            if hasattr(self.context, "send_private"):
                await self.context.send_private(user_id=to_qq, message=msg)
                return
            raise RuntimeError("context has no send_private_message")

        async def _send_group(to_group: str, msg: str) -> None:
            to_group = str(to_group or "").strip()
            if not to_group:
                return
            if hasattr(self.context, "send_group_message"):
                await self.context.send_group_message(group_id=to_group, message=msg)
                return
            if hasattr(self.context, "send_group"):
                await self.context.send_group(group_id=to_group, message=msg)
                return
            raise RuntimeError("context has no send_group_message")

        async def _ai_notify(kind: str, msg: str) -> None:
            enabled = self.ai_post_notify_enabled if kind == "post" else self.ai_post_delete_notify_enabled
            mode = self.ai_post_notify_mode if kind == "post" else self.ai_post_delete_notify_mode
            to_private = self.ai_post_notify_private_qq if kind == "post" else self.ai_post_delete_notify_private_qq
            to_group = self.ai_post_notify_group_id if kind == "post" else self.ai_post_delete_notify_group_id

            if not enabled or mode == "off":
                return
            text = str(msg or "").strip()
            if not text:
                return

            try:
                if to_private:
                    await _send_private(to_private, text)
                if to_group:
                    await _send_group(to_group, text)
            except Exception as e:
                logger.warning(f"[Qzone] AI notify failed kind={kind}: {e}")

        async def _gen_and_post(prompt: str) -> None:
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

                logger.info(
                    "[Qzone] fixed post 返回 | status=%s ok=%s code=%s msg=%s tid=%s",
                    status,
                    result.ok,
                    result.code,
                    result.message,
                    getattr(result, "tid", ""),
                )

                ok = bool(status == 200 and result.ok)
                if ok and self.ai_post_notify_mode == "all":
                    await _ai_notify("post", f"定时发说说成功 tid={getattr(result, 'tid', '')}")
                if (not ok) and self.ai_post_notify_mode in ("error", "all"):
                    await _ai_notify(
                        "post",
                        f"定时发说说失败 status={status} code={getattr(result, 'code', '')} msg={getattr(result, 'message', '')}",
                    )

                delete_after = int(self.config.get("ai_post_delete_after_min", 0) or 0)
                tid = getattr(result, "tid", "")
                if status == 200 and result.ok and delete_after > 0 and tid:
                    await self._queue_delete(str(tid), delete_after)
                return

            provider_id = str(self.config.get("ai_post_provider_id", "") or "").strip()
            provider = None
            if provider_id:
                try:
                    provider = self.context.get_provider_by_id(provider_id)
                except Exception:
                    provider = None
            if not provider:
                provider = self.context.get_using_provider(umo=target_umo)

            if not provider:
                logger.error("[Qzone] AI post：未配置文本生成服务")
                return

            system_prompt = (
                "你是中文写作助手。请输出QQ空间纯文字说说正文。\n"
                "要求：不尬、不营销、不带链接；1-3句；总字数<=120；只输出正文，不要解释。"
            )
            try:
                resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt, context=[])
                content = ""
                try:
                    content = str(getattr(resp, "content", "") or "").strip()
                except Exception:
                    content = ""
                if not content:
                    try:
                        content = str(getattr(resp, "text", "") or "").strip()
                    except Exception:
                        content = ""
                if not content:
                    try:
                        content = str(resp).strip()
                    except Exception:
                        content = ""
            except Exception as e:
                logger.error(f"[Qzone] AI post：LLM 调用失败: {e}")
                return

            if not content:
                logger.error("[Qzone] AI post：LLM 返回为空")
                return

            content = content.strip("\"'` ")
            content = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", content)
            content = re.sub(r"```\s*$", "", content).strip()
            if len(content) > 120:
                content = content[:120].rstrip()

            if bool(self.config.get("ai_post_mark", True)):
                content = "【AI发送】" + content

            status, result = await asyncio.to_thread(poster.publish_text, content)
            try:
                self.config["ai_post_last_run_ts"] = time.time()
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass

            logger.info(
                "[Qzone] AI post 返回 | status=%s ok=%s code=%s msg=%s tid=%s",
                status,
                result.ok,
                result.code,
                result.message,
                getattr(result, "tid", ""),
            )

            # Optional notify
            ok = bool(status == 200 and result.ok)
            if ok and self.ai_post_notify_mode == "all":
                await _ai_notify("post", f"AI发说说成功 tid={getattr(result, 'tid', '')}")
            if (not ok) and self.ai_post_notify_mode in ("error", "all"):
                await _ai_notify(
                    "post",
                    f"AI发说说失败 status={status} code={getattr(result, 'code', '')} msg={getattr(result, 'message', '')}",
                )

            delete_after = int(self.config.get("ai_post_delete_after_min", 0) or 0)
            tid = getattr(result, "tid", "")
            if status == 200 and result.ok and delete_after > 0 and tid:
                await self._queue_delete(str(tid), delete_after)

        # daily_time: HH:MM
        def _seconds_until(hhmm: str) -> Optional[int]:
            m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm)
            if not m:
                return None
            hh = int(m.group(1))
            mm = int(m.group(2))
            if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                return None
            now = time.time()
            lt = time.localtime(now)
            # next trigger today
            tgt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
            if tgt <= now:
                tgt += 86400
            return int(tgt - now)

        next_daily_sleep = _seconds_until(daily_time) if daily_time else None

        def _next_interval_due_ts() -> float:
            # Use last-run as anchor when available; otherwise start immediately.
            last = float(self.config.get("ai_post_last_run_ts", 0) or 0)
            if last <= 0:
                return time.time()
            return last + interval_min * 60

        def _next_daily_due_ts() -> Optional[float]:
            if not daily_time:
                return None
            sec = _seconds_until(daily_time)
            if sec is None:
                return None
            return time.time() + sec

        while not self._ai_stop.is_set():
            try:
                # Always attempt draining due deletes even if posting is paused.
                try:
                    drained = await self._drain_due_deletes(poster)
                    if drained:
                        logger.info("[Qzone] pending deletes drained=%s", drained)
                except Exception as e:
                    logger.warning(f"[Qzone] drain pending deletes failed: {e}")

                now = time.time()
                next_candidates: List[float] = []

                if interval_min > 0:
                    next_candidates.append(_next_interval_due_ts())

                dts = _next_daily_due_ts()
                if dts is not None:
                    next_candidates.append(dts)

                if not next_candidates:
                    # nothing scheduled; check deletes periodically
                    await asyncio.wait_for(self._ai_stop.wait(), timeout=30)
                    continue

                next_due = min(next_candidates)
                sleep_s = max(0.0, next_due - now)
                # small cap so we still drain deletes even if far future
                sleep_s = min(sleep_s, 30.0)
                if sleep_s > 0:
                    await asyncio.wait_for(self._ai_stop.wait(), timeout=sleep_s)
                    continue

                # time to run something; prefer whichever is due now
                ran = False
                # interval due?
                if interval_min > 0 and now >= _next_interval_due_ts() - 0.5:
                    prompt = str(self.config.get("ai_post_prompt", "") or "").strip()
                    if prompt:
                        await _gen_and_post(prompt)
                    ran = True

                # daily due?
                if daily_time and (_seconds_until(daily_time) is not None):
                    # If daily due is in the past/now within 60s window, run it.
                    dd = _next_daily_due_ts()
                    if dd is not None and (time.time() >= dd - 0.5):
                        prompt = str(self.config.get("ai_post_daily_prompt", "") or "").strip()
                        if prompt:
                            await _gen_and_post(prompt)
                        ran = True

                if not ran:
                    await asyncio.wait_for(self._ai_stop.wait(), timeout=1)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"[Qzone] AI post worker 异常: {e}")
                logger.error(traceback.format_exc())
                await asyncio.sleep(5)

    async def _like_once(
        self,
        client: _QzoneClient,
        target_qq: str,
        limit: int,
        *,
        dedup: bool = False,
    ) -> Tuple[int, int]:
        target = str(target_qq).strip() or self.my_qq
        if limit <= 0:
            limit = 10
        if limit > 100:
            limit = 100

        liked_ok = 0
        attempted = 0

        # 默认命令仍然是一次取 count=10；只有自定义请求大于10时才启用递增模式。
        ramp_enabled = limit > 10
        ramp_step = int(self.config.get("like_ramp_step", 10))
        if ramp_step <= 0:
            ramp_step = 10

        max_count = max(self.max_feeds, limit)
        seen: Set[str] = set()

        def _normalize_key(k: str) -> str:
            return k if k.endswith(".1") else (k + ".1")

        cur_count = min(ramp_step if ramp_enabled else 10, max_count)

        while attempted < limit:
            if dedup:
                # 自动轮询：用旧版 self-feeds 接口，更稳定。
                status, keys, text_len = await asyncio.to_thread(client.fetch_keys_self_legacy, cur_count)
            else:
                status, keys, text_len = await asyncio.to_thread(client.fetch_keys, cur_count, target)
            logger.info(
                "[Qzone] feeds 返回 | target=%s status=%s text_len=%s keys=%d count=%d",
                target,
                status,
                text_len,
                len(keys),
                cur_count,
            )

            if not keys:
                # keys=0 且 text_len 很短时，通常是权限/风控/返回结构变化；打印片段方便排查。
                try:
                    res = await asyncio.to_thread(
                        requests.get,
                        (
                            "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
                            f"feeds_html_act_all?uin={self.my_qq}&hostuin={target}"
                            f"&scope=0&filter=all&flag=1&refresh=0&firstGetGroup=0&mixnocache=0&scene=0"
                            f"&begintime=undefined&icServerTime=&start=0&count={cur_count}"
                            f"&sidomain=qzonestyle.gtimg.cn&useutf8=1&outputhtmlfeed=1&refer=2"
                            f"&r={random.random()}&g_tk={client.g_tk}"
                        ),
                        headers=client.headers,
                        timeout=20,
                    )
                    head = (res.text or "")[:300].replace("\n", " ").replace("\r", " ")
                    logger.info("[Qzone] feeds head | status=%s head=%s", res.status_code, head)
                except Exception as e:
                    logger.warning("[Qzone] feeds head 获取失败: %s", e)

            if status != 200:
                logger.warning("[Qzone] feeds 非200，可能登录失效/风控/重定向（请检查cookie）")

            if not keys:
                break

            new_keys = []
            for k in sorted(keys):
                fk = _normalize_key(k)
                if fk in seen:
                    continue
                seen.add(fk)
                new_keys.append(fk)

            if not new_keys:
                break

            now_ts = time.time()
            if dedup:
                ttl = int(self.config.get("auto_dedup_ttl_sec", 86400))
                if ttl < 0:
                    ttl = 0
                if ttl:
                    # 清理过期
                    expired = [k for k, ts in self._auto_seen.items() if now_ts - ts > ttl]
                    for k in expired:
                        self._auto_seen.pop(k, None)

            for full_key in new_keys:
                if attempted >= limit:
                    break

                if dedup and full_key in self._auto_seen:
                    continue

                attempted += 1
                logger.info("[Qzone] 发现新动态: %s", full_key[-24:])

                # 进一步抖动：避免固定间隔触发风控
                jitter = random.random() * 1.5
                await asyncio.sleep(random.randint(self.delay_min, self.delay_max) + jitter)

                like_status, resp = await asyncio.to_thread(client.send_like, full_key)
                resp_head = resp[:300].replace("\n", " ").replace("\r", " ")
                logger.info("[Qzone] like 返回 | status=%s resp_head=%s", like_status, resp_head)

                code = None
                msg = ""
                m = re.search(r"\"code\"\s*:\s*(\d+)", resp)
                if m:
                    try:
                        code = int(m.group(1))
                    except Exception:
                        code = None
                m2 = re.search(r"\"message\"\s*:\s*\"([^\"]*)\"", resp)
                if m2:
                    msg = m2.group(1)

                logger.info("[Qzone] like 结果 | code=%s msg=%s", code, msg)
                if msg and "记录成功" in msg:
                    ok = False
                else:
                    ok = code == 0

                if ok:
                    liked_ok += 1
                    logger.info("[Qzone] ✅ 点赞成功: %s", full_key[-24:])
                    if dedup:
                        self._auto_seen[full_key] = now_ts
                else:
                    logger.warning("[Qzone] ❌ 点赞失败: %s", full_key[-24:])

            if not ramp_enabled:
                break

            if cur_count >= max_count:
                break
            cur_count = min(cur_count + ramp_step, max_count)
            # 每次加大 count 前稍微休息一下，降低风控概率
            await asyncio.sleep(0.5 + random.random() * 0.7)

        return attempted, liked_ok

    async def _protect_worker(self) -> None:
        if not self.protect_enabled:
            logger.info("[Qzone] protect_enabled=false，护评不启动")
            return

        if not self.my_qq or not self.cookie:
            logger.error("[Qzone] 配置缺失：my_qq 或 cookie 为空，护评无法启动")
            return

        try:
            scanner = QzoneProtectScanner(self.my_qq, self.cookie)
        except Exception as e:
            logger.error(f"[Qzone] 护评初始化失败: {e}")
            return

        logger.info(
            "[Qzone] protect worker 启动 | window_min=%s notify=%s interval=%ss pages=%s",
            self.protect_window_minutes,
            self.protect_notify_mode,
            self.protect_poll_interval,
            self.protect_pages,
        )

        # runtime state for diagnostics (even if logs are filtered)
        self._protect_last_scan = ""
        self._protect_last_delete = ""

        while not self._protect_stop.is_set():
            try:
                status, refs = await asyncio.to_thread(scanner.scan_recent_comments, self.protect_pages, 10)
                diag = getattr(scanner, "last_diag", "")
                errs = getattr(scanner, "last_errors", [])
                self._protect_last_scan = f"ts={int(time.time())} status={status} refs={len(refs)}"
                if diag:
                    self._protect_last_scan += " | " + diag
                if diag:
                    logger.info("%s", diag)
                if errs and self.protect_notify_mode in ("error", "all"):
                    # only print a few to avoid log spam
                    for s in list(errs)[:3]:
                        logger.warning("[Qzone][protect_scan_err] %s", s)

                if status != 200:
                    if self.protect_notify_mode in ("error", "all"):
                        logger.warning("[Qzone] protect scan failed status=%s", status)
                else:
                    refs = scanner.filter_within_window(refs, self.protect_window_minutes)

                    deleter = QzoneCommentDeleter(self.my_qq, self.cookie)

                    del_try = 0
                    del_ok = 0
                    del_fail = 0

                    # Delete only others' comments; never delete own comments.
                    for r in refs:
                        if str(r.comment_uin) == str(self.my_qq):
                            continue

                        k = f"{r.topic_id}:{r.comment_id}"
                        ts = self._protect_seen.get(k)
                        if ts and (time.time() - ts) < max(60.0, float(self.protect_poll_interval) * 2.0):
                            continue
                        # mark first to avoid spamming on repeated failures
                        self._protect_seen[k] = time.time()

                        del_try += 1
                        ds, dr = await asyncio.to_thread(deleter.delete_comment, r.topic_id, r.comment_id, r.comment_uin)
                        if ds == 200 and dr.ok:
                            del_ok += 1
                            if self.protect_notify_mode == "all":
                                logger.info(
                                    "[Qzone] protect delete ok topicId=%s commentId=%s commentUin=%s",
                                    r.topic_id,
                                    r.comment_id,
                                    r.comment_uin,
                                )
                        else:
                            del_fail += 1
                            if self.protect_notify_mode in ("error", "all"):
                                logger.warning(
                                    "[Qzone] protect delete failed status=%s code=%s msg=%s topicId=%s commentId=%s commentUin=%s",
                                    ds,
                                    dr.code,
                                    dr.message,
                                    r.topic_id,
                                    r.comment_id,
                                    r.comment_uin,
                                )

                    self._protect_last_delete = (
                        f"ts={int(time.time())} kept={len(refs)} try={del_try} ok={del_ok} fail={del_fail}"
                    )

                await asyncio.wait_for(self._protect_stop.wait(), timeout=self.protect_poll_interval)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.error(f"[Qzone] protect worker 异常: {e}")
                logger.error(traceback.format_exc())
                await asyncio.sleep(min(10, self.protect_poll_interval))

        logger.info("[Qzone] protect worker 已停止")

    async def _worker(self) -> None:
        if not self.enabled:
            logger.info("[Qzone] enabled=false，worker 不启动")
            return

        if not self.my_qq or not self.cookie:
            logger.error("[Qzone] 配置缺失：my_qq 或 cookie 为空，任务无法启动")
            return

        try:
            client = _QzoneClient(self.my_qq, self.cookie)
        except Exception as e:
            logger.error(f"[Qzone] 初始化客户端失败: {e}")
            return

        logger.info("[Qzone] worker 启动 | g_tk=%s", client.g_tk)

        while not self._stop_event.is_set():
            try:
                logger.info("[%s] 正在侦测...（liked_cache=%d）", _now_hms(), len(self._liked))

                target = self._target_qq.strip() or self.my_qq
                limit = self._manual_like_limit if self._manual_like_limit > 0 else self.max_feeds

                attempted, ok = await self._like_once(client, target, limit, dedup=True)

                if attempted == 0:
                    logger.info("[Qzone] 本轮没有新动态待处理")

                if self._manual_like_limit > 0:
                    logger.info(
                        "[Qzone] 手动点赞限制=%d，本轮尝试=%d 成功=%d",
                        self._manual_like_limit,
                        attempted,
                        ok,
                    )
                    self._manual_like_limit = 0

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"[Qzone] worker 异常: {e}")
                logger.error(traceback.format_exc())
                await asyncio.sleep(self.poll_interval)

        logger.info("[Qzone] worker 已停止")

    @filter.command("start")
    async def start(self, event: AstrMessageEvent):
        if self._is_running():
            yield event.plain_result("点赞任务已经在运行中（请看后台日志）")
            return

        self._set_enabled(True)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._worker())
        yield event.plain_result("🚀 Qzone 自动点赞后台任务已启动（已打开 enabled 开关）")

    @filter.command("stop")
    async def stop(self, event: AstrMessageEvent):
        if not self._is_running():
            self._set_enabled(False)
            yield event.plain_result("当前没有运行中的任务（已关闭 enabled 开关）")
            return

        self._set_enabled(False)
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except Exception:
            pass
        yield event.plain_result("🛑 点赞任务已停止（已关闭 enabled 开关）")

    @filter.command("护评状态")
    async def protect_status(self, event: AstrMessageEvent):
        protect_running = self._protect_task is not None and (not self._protect_task.done())
        task_state = "none"
        if self._protect_task is not None:
            task_state = "done" if self._protect_task.done() else "running"

        lines = [
            f"护评 enabled={self.protect_enabled} running={protect_running} task={task_state}",
            f"interval={self.protect_poll_interval}s pages={self.protect_pages} window_min={self.protect_window_minutes} notify={self.protect_notify_mode}",
            f"seen_cache={len(self._protect_seen)}",
            f"last_scan={getattr(self, '_protect_last_scan', '')}",
            f"last_delete={getattr(self, '_protect_last_delete', '')}",
        ]
        yield event.plain_result("\n".join([s for s in lines if s and not s.endswith('=')]))

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _intercept_local_scheduler_cmds(self, event: AstrMessageEvent):
        # Intercept scheduler commands early to prevent external tool-loop agents from hijacking them.
        # Also provide a fallback path for custom command prefixes (e.g. users replace default "/" with "，").
        raw = (event.message_str or "").strip()
        if not raw:
            return

        txt = raw
        used_prefix = False
        if txt.startswith("，") or txt.startswith(","):
            txt = txt[1:].lstrip()
            used_prefix = True

        if not txt:
            return

        for p in (
            "qz定时",
            "qz任务",
            "qzone定时",
        ):
            if txt.startswith(p):
                try:
                    setattr(event, "_qz_stop_after", True)
                except Exception:
                    pass

                # If a custom prefix was used, command parser may not trigger @filter.command.
                # In that case, call handlers directly and stop propagation.
                if used_prefix:
                    if txt.startswith("定时任务列表") or txt.startswith("定时说说任务列表"):
                        async for r in self.cron_list_local(event):
                            await event.send(r)
                    else:
                        async for r in self.ai_post_ctl(event):
                            await event.send(r)
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                return

    @filter.command("qz定时列表")
    @filter.command("qz任务列表")
    @filter.command("qzone定时列表")
    async def cron_list_local(self, event: AstrMessageEvent):
        """List plugin-local scheduled tasks (AI post interval/daily + deletion policy)."""

        ai_running = self._ai_task is not None and (not self._ai_task.done())
        ai_state = "none"
        if self._ai_task is not None:
            ai_state = "done" if self._ai_task.done() else "running"

        enabled = bool(self.config.get("ai_post_enabled", False))
        interval_min = int(self.config.get("ai_post_interval_min", 0) or 0)
        daily_time = str(self.config.get("ai_post_daily_time", "") or "").strip()
        delete_after = int(self.config.get("ai_post_delete_after_min", 0) or 0)

        mode = str(self.config.get("ai_post_mode", "fixed") or "fixed").strip() or "fixed"
        fixed_text = str(self.config.get("ai_post_fixed_text", "") or "").strip()

        mark = bool(self.config.get("ai_post_mark", True))
        provider_id = str(self.config.get("ai_post_provider_id", "") or "").strip()

        prompt = str(self.config.get("ai_post_prompt", "") or "").strip()
        daily_prompt = str(self.config.get("ai_post_daily_prompt", "") or "").strip()

        # Best-effort estimate for next run time.
        next_run = "-"
        try:
            now = time.time()
            if interval_min > 0:
                last = float(self.config.get("ai_post_last_run_ts", 0) or 0)
                base = last if last > 0 else now
                nxt = base + interval_min * 60
                next_run = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(nxt))
            elif daily_time:
                m = re.match(r"^(\d{1,2}):(\d{2})$", daily_time)
                if m:
                    hh = int(m.group(1)); mm = int(m.group(2))
                    lt = time.localtime(now)
                    tgt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
                    if tgt <= now:
                        tgt += 86400
                    next_run = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tgt))
        except Exception:
            next_run = "-"

        def _short(s: str, n: int = 40) -> str:
            s = (s or "").strip().replace("\n", " ").replace("\r", " ")
            return s if len(s) <= n else (s[:n] + "...")

        lines = [
            "本插件定时任务（AI发说说）：",
            f"开关: {enabled} | 任务: {ai_state} | 运行中: {ai_running}",
            f"下次触发: {next_run}",
            f"模式: {mode} | 间隔(分钟): {interval_min} | 每日: {daily_time or '-'} | 删后(分钟): {delete_after} | AI标记: {mark}",
            f"固定文本: {_short(fixed_text) or '-'}",
            f"提示词(interval): {_short(prompt) or '-'}",
            f"提示词(daily): {_short(daily_prompt) or '-'}",
            f"模型(provider_id): {provider_id or '-'}",
            f"通知(发): enabled={self.ai_post_notify_enabled} mode={self.ai_post_notify_mode} 私聊={self.ai_post_notify_private_qq or '-'} 群={self.ai_post_notify_group_id or '-'}",
            f"通知(删): enabled={self.ai_post_delete_notify_enabled} mode={self.ai_post_delete_notify_mode} 私聊={self.ai_post_delete_notify_private_qq or '-'} 群={self.ai_post_delete_notify_group_id or '-'}",
        ]
        yield event.plain_result("\n".join(lines))
        try:
            if getattr(event, "_qz_stop_after", False):
                event.stop_event()
        except Exception:
            pass

    @filter.command("qz定时")
    @filter.command("qz任务")
    @filter.command("qzone定时")
    async def ai_post_ctl(self, event: AstrMessageEvent):
        # Local scheduler control for AI post/delete. Supports: 开启/关闭/间隔/删后/列表.

        raw = (event.message_str or "").strip()
        text = raw
        if text.startswith("，") or text.startswith(","):
            text = text[1:].lstrip()
        for prefix in (
            "/qz定时",
            "qz定时",
            "/qz任务",
            "qz任务",
            "/qzone定时",
            "qzone定时",
        ):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if not text or text in ("状态", "status", "任务列表", "列表", "list"):
            async for r in self.cron_list_local(event):
                yield r
            return

        # Natural language quick-setup (only under our command prefix):
        # Examples:
        # - ，定时任务 每隔五分钟发一条Python测试中 五分钟后自动删除
        # - ，qz定时 每5分钟 发 Python测试中 删后5
        ok, applied = self._try_parse_and_apply_ai_schedule(text)
        if ok:
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            await self._maybe_start_ai_task()
            yield event.plain_result(applied)
            return

        # Be tolerant: some adapters insert extra spaces or hidden chars.
        tnorm = "".join(ch for ch in (text or "").strip().lower() if ch not in ("\u200b", "\ufeff"))

        if tnorm in ("开", "开启", "打开", "start", "on"):
            self.config["ai_post_enabled"] = True
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            await self._maybe_start_ai_task()
            state = "running" if (self._ai_task is not None and (not self._ai_task.done())) else "none"
            yield event.plain_result(f"✅ 已开启 AI 定时发说说（task={state}）")
            return

        if tnorm in ("关", "关闭", "停", "停止", "stop", "off"):
            self.config["ai_post_enabled"] = False
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            if self._ai_task is not None and not self._ai_task.done():
                self._ai_stop.set()
            yield event.plain_result("🛑 已关闭 AI 定时发说说")
            return

        parts = [p for p in text.split() if p.strip()]
        if len(parts) >= 2 and parts[0].lower() in ("interval", "每隔"):
            try:
                n = int(parts[1])
            except Exception:
                n = 0
            if n <= 0:
                yield event.plain_result("用法：，定时说说 interval 5")
                return
            self.config["ai_post_interval_min"] = n
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            await self._maybe_start_ai_task()
            yield event.plain_result(f"✅ 已设置 interval={n} 分钟")
            return

        if len(parts) >= 2 and parts[0].lower() in ("daily", "每天"):
            hhmm = parts[1].strip()
            if not re.match(r"^\d{1,2}:\d{2}$", hhmm):
                yield event.plain_result("用法：，定时说说 daily 08:30")
                return
            self.config["ai_post_daily_time"] = hhmm
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            await self._maybe_start_ai_task()
            yield event.plain_result(f"✅ 已设置 daily_time={hhmm}")
            return

        if len(parts) >= 2 and parts[0] in ("删后", "删除", "delete_after"):
            try:
                n = int(parts[1])
            except Exception:
                n = -1
            if n < 0:
                yield event.plain_result("用法：，定时说说 删后 5（0=不删）")
                return
            self.config["ai_post_delete_after_min"] = n
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            yield event.plain_result(f"✅ 已设置 delete_after_min={n}")
            return

        if parts and parts[0].lower() == "prompt":
            prompt = text[len(parts[0]) :].strip()
            if not prompt:
                yield event.plain_result("用法：，定时说说 prompt 你的提示词")
                return
            self.config["ai_post_prompt"] = prompt
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            yield event.plain_result("✅ 已更新 interval prompt")
            return

        yield event.plain_result("用法：，定时任务 状态|开|关|interval 5|daily 08:30|删后 5|prompt ... | 或直接说：每隔五分钟发一条Python测试中，五分钟后自动删除")

    @filter.command("护评扫一次")
    async def protect_scan_once(self, event: AstrMessageEvent):
        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            scanner = QzoneProtectScanner(self.my_qq, self.cookie)
        except Exception as e:
            yield event.plain_result(f"护评初始化失败：{e}")
            return

        pages = self.protect_pages
        count = 10
        try:
            status, refs = await asyncio.to_thread(scanner.scan_recent_comments, pages, count)
            diag = getattr(scanner, "last_diag", "")
            errs = getattr(scanner, "last_errors", [])
            lines = [f"scan status={status} refs={len(refs)}"]
            if diag:
                lines.append(diag)
            if errs:
                for s in list(errs)[:5]:
                    lines.append(f"err: {s}")
            if refs:
                for r in refs[:3]:
                    lines.append(
                        f"ref: topicId={r.topic_id} commentId={r.comment_id} commentUin={r.comment_uin} abstime={r.abstime}"
                    )

            # Debug: try fetch a full module HTML for a recent topic and show whether it contains comments-list.
            # This helps verify URL/params/cookie correctness.
            if self.my_qq and self.cookie:
                try:
                    status2, html2 = await asyncio.to_thread(scanner.fetch_feeds_module_html, self.my_qq, 5)
                    has_feed_data = "name=\"feed_data\"" in (html2 or "")
                    has_comments = "comments-item" in (html2 or "")
                    lines.append(f"module_fetch status={status2} has_feed_data={has_feed_data} has_comments={has_comments}")

                    # Return body slice around feeds list so the user can visually confirm comment blocks exist.
                    html2 = html2 or ""
                    s = html2.find('<ul id="host_home_feeds"')
                    if s < 0:
                        s = html2.find("host_home_feeds")
                        if s >= 0:
                            s = max(0, s - 200)

                    e = -1
                    if s >= 0:
                        e = html2.find("</ul>", s)
                        if e >= 0:
                            e += len("</ul>")

                    if s >= 0 and e > s:
                        body2 = html2[s:e]
                        lines.append("module_body_slice=\n" + body2)
                    else:
                        # Fallback: keep a moderate head/tail for diagnosis.
                        head2 = html2[:2000]
                        tail2 = html2[-2000:]
                        lines.append("module_html_head=\n" + head2)
                        lines.append("module_html_tail=\n" + tail2)
                except Exception as e:
                    lines.append(f"module_fetch_error: {e}")

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"[Qzone] protect scan once 异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"护评扫一次异常：{e}")

    @filter.command("status")
    async def status(self, event: AstrMessageEvent):
        target = self._target_qq.strip() or self.my_qq
        protect_running = self._protect_task is not None and (not self._protect_task.done())
        yield event.plain_result(
            f"运行中={self._is_running()} | enabled={self.enabled} | auto_start={self.auto_start} | target={target} | liked_cache={len(self._liked)}\n"
            f"护评 enabled={self.protect_enabled} running={protect_running} interval={self.protect_poll_interval}s pages={self.protect_pages} window_min={self.protect_window_minutes} notify={self.protect_notify_mode}"
        )

    @filter.command("post")
    async def post(self, event: AstrMessageEvent):
        """发一条纯文字说说。

        用法：/post 你的内容...
        """
        text = (event.message_str or "").strip()
        for prefix in ("/post", "post"):
            if text.lower().startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if not text:
            yield event.plain_result("用法：/post 你的内容（暂仅支持纯文字）")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.publish_text, text)
            logger.info(
                "[Qzone] post 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )

            if status == 200 and result.ok:
                tid_info = f" tid={result.tid}" if getattr(result, "tid", "") else ""
                if getattr(result, "tid", ""):
                    self._remember_post(str(result.tid), text)
                yield event.plain_result(f"✅ 已发送说说{tid_info}")
            else:
                hint = result.message or "发送失败（可能 cookie/风控/验证页）"
                yield event.plain_result(f"❌ 发送失败：status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] 发说说异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")


    @filter.command("删除")
    async def delete(self, event: AstrMessageEvent):
        """删除一条说说。

        用法：/删除 tid
        """
        text = (event.message_str or "").strip()
        for prefix in ("/删除", "删除"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break

        # Support both: "/删除 <tid>" and "删除 <N>" to delete recent N posts.
        tid = (text or "").strip()
        n = 0
        if tid.isdigit() and len(tid) <= 3:
            try:
                n = int(tid)
            except Exception:
                n = 0

        if n > 0:
            max_n = min(n, self._tid_store_max if self._tid_store_max > 0 else n)
            tids = list(reversed(self._recent_tids))[:max_n]
            if not tids:
                yield event.plain_result("没有可删除的 recent tids（先用 /post 发几条，或开启 tid_store_max 落盘）")
                return

            yield event.plain_result(f"准备删除最近 {len(tids)} 条（可能触发风控，失败会提示 code/msg）")
            deleted = 0
            for t in tids:
                status, result = await asyncio.to_thread(QzonePoster(self.my_qq, self.cookie).delete_by_tid, t)
                if status == 200 and result.ok:
                    deleted += 1
                await asyncio.sleep(0.5 + random.random() * 0.7)
            yield event.plain_result(f"批量删除完成：成功={deleted}/{len(tids)}")
            return
        if not tid:
            if self._last_tid:
                yield event.plain_result(f"用法：/删除 tid（最近一条 tid={self._last_tid}，可直接 /删除 {self._last_tid}）")
            else:
                yield event.plain_result("用法：/删除 tid（tid 可从 /post 成功回显里复制）")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.delete_by_tid, tid)
            logger.info(
                "[Qzone] delete 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )

            if status == 200 and result.ok:
                yield event.plain_result(f"✅ 已删除说说 tid={tid}")
            else:
                hint = result.message or "删除失败（可能 cookie/风控/验证码/权限）"
                yield event.plain_result(f"❌ 删除失败：status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] 删除说说异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.command("说说")
    async def moods(self, event: AstrMessageEvent):
        """列出最近的说说内容（用于快速挑选要评论/删除的条目）。

        用法：
        - /说说        （默认 5 条）
        - /说说 10     （展示最近 10 条，最多 50）
        """

        raw = (event.message_str or "").strip()
        text = raw

        # Make comma/Chinese-comma triggered routing compatible (",说说" / "，说说")
        if text.startswith(",") or text.startswith("，"):
            text = text[1:].lstrip()

        for prefix in ("/说说", "说说"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        # optional: @12345 to fetch other's space
        # 1) normal message chain: align with /点赞
        # 2) fallback to raw_message (some LLM/routers may rebuild message_obj.message)
        target_uin = ""
        try:
            chain = getattr(event.message_obj, "message", [])
            for seg in chain:
                if getattr(seg, "type", "") == "at":
                    qq = getattr(seg, "qq", "")
                    if qq:
                        target_uin = str(qq).strip()
                        break
        except Exception:
            pass

        if not target_uin:
            try:
                raw_msg = getattr(event.message_obj, "raw_message", None)
                if isinstance(raw_msg, dict):
                    raw_chain = raw_msg.get("message") or []
                    for seg in raw_chain:
                        if isinstance(seg, dict) and seg.get("type") == "at":
                            data = seg.get("data") or {}
                            qq = data.get("qq")
                            if qq:
                                target_uin = str(qq).strip()
                                break
            except Exception:
                pass

        if not target_uin:
            m_at = re.search(r"@\s*(\d{5,12})", text)
            if m_at:
                target_uin = m_at.group(1)

        # aiocqhttp log-format fallback: ",说说 [At:12345]"
        if not target_uin:
            m_cq = re.search(r"\[At:(\d{5,12})\]", raw)
            if m_cq:
                target_uin = m_cq.group(1)

        if target_uin:
            text = re.sub(r"@\s*\d{5,12}", "", text).strip()

        n = 5
        m_n = re.search(r"\b(\d{1,3})\b", text)
        if m_n:
            try:
                n = int(m_n.group(1))
            except Exception:
                n = 5
        if n <= 0:
            n = 5
        if n > 50:
            n = 50

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            host_uin = target_uin or self.my_qq
            fetcher = QzoneFeedFetcher(host_uin, self.cookie, my_qq=self.my_qq)
            page_size = 10
            pages = (n + page_size - 1) // page_size
            status, posts = await asyncio.to_thread(fetcher.fetch_mood_posts, page_size, pages)
            if status != 200 or not posts:
                diag = getattr(fetcher, "last_diag", "")
                extra = f" | {diag}" if diag else ""
                yield event.plain_result(f"获取失败：status={status} posts={len(posts) if posts else 0}{extra}")
                return

            posts = posts[:n]
            lines = [f"最近 {len(posts)} 条说说（最新在前）："]
            i = 1
            for p in posts:
                tid = str(getattr(p, "tid", "") or "").strip()
                ts = int(getattr(p, "abstime", 0) or 0)
                tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"
                content = str(getattr(p, "text", "") or "").strip()
                # keep it extremely short for quick scanning
                if len(content) > 6:
                    content = content[:6] + "..."
                lines.append(f"{i}) {tstr} tid={tid} | {content}")
                i += 1

            # include short target echo to make diagnosis easy
            if target_uin and target_uin != self.my_qq:
                lines[0] = lines[0] + f"（目标空间 {target_uin}）"
            yield event.plain_result("\n\n".join(lines))
        except Exception as e:
            logger.error(f"[Qzone] 说说列表异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.command("说说表")
    async def mood_table(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()

        # Make comma/Chinese-comma triggered routing compatible (",说说表" / "，说说表")
        if text.startswith(",") or text.startswith("，"):
            text = text[1:].lstrip()

        for prefix in ("/说说表", "说说表"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        n = 10
        if text and text.isdigit() and len(text) <= 3:
            try:
                n = int(text)
            except Exception:
                n = 10
        if n <= 0:
            n = 10
        if n > 200:
            n = 200

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            host_uin = self.my_qq
            # allow "说说表 10 @12345" / "说说表 @12345 10" / "说说表 [At:12345]"
            try:
                chain = getattr(event.message_obj, "message", [])
                for seg in chain:
                    if getattr(seg, "type", "") == "at":
                        qq = getattr(seg, "qq", "")
                        if qq:
                            u = str(qq).strip()
                            if u.isdigit():
                                host_uin = u
                                break
            except Exception:
                pass

            if host_uin == self.my_qq:
                try:
                    raw_msg = getattr(event.message_obj, "raw_message", None)
                    if isinstance(raw_msg, dict):
                        raw_chain = raw_msg.get("message") or []
                        for seg in raw_chain:
                            if isinstance(seg, dict) and seg.get("type") == "at":
                                data = seg.get("data") or {}
                                qq = data.get("qq")
                                if qq:
                                    u = str(qq).strip()
                                    if u.isdigit():
                                        host_uin = u
                                        break
                except Exception:
                    pass

            if host_uin == self.my_qq:
                m_at = re.search(r"@\s*(\d{5,12})", (event.message_str or ""))
                if m_at:
                    host_uin = m_at.group(1)
            if host_uin == self.my_qq:
                m_cq = re.search(r"\[At:(\d{5,12})\]", (event.message_str or ""))
                if m_cq:
                    host_uin = m_cq.group(1)

            fetcher = QzoneFeedFetcher(host_uin, self.cookie, my_qq=self.my_qq)
            # page size 10, pages enough to cover n
            page_size = 10
            pages = (n + page_size - 1) // page_size
            status, posts = await asyncio.to_thread(fetcher.fetch_mood_posts, page_size, pages)
            if status != 200 or not posts:
                diag = getattr(fetcher, "last_diag", "")
                sample = getattr(fetcher, "last_sample_html_head", "")
                extra = f" | {diag}" if diag else ""
                if sample:
                    extra += f" | sample={sample}"
                yield event.plain_result(f"获取失败：status={status} posts={len(posts) if posts else 0}{extra}")
                return

            posts = posts[:n]

            def _fmt(p) -> str:
                # Prefer Qzone-rendered time string (avoids server timezone issues).
                fs = str(getattr(p, "feedstime", "") or "").strip()
                if fs:
                    return fs

                ts = int(getattr(p, "abstime", 0) or 0)
                if ts:
                    try:
                        # NOTE: this depends on server timezone; only used when feedstime is unavailable.
                        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                    except Exception:
                        return str(ts)

                return "-"

            lines = [f"共 {len(posts)} 条（最新在前）"]
            i = 1
            for p in posts:
                lines.append(f"{i}) {_fmt(p)} | tid={p.tid}")
                i += 1

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"[Qzone] 说说表异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.command("评论")
    async def comment(self, event: AstrMessageEvent):
        """发表评论入口。

        用法：
        - /评论 内容...  (手动评论最近一条)
        - /评论 [N]     (自动生成评论，评论最近 N 条；不带 N 默认 1)

        说明：为避免 LLM/适配器参数吞掉，这里优先从 message_str 解析。

        用法：/评论 [N]
        - 不带 N：评论最近 1 条
        - 带 N：评论最近 N 条（例如 /评论 4）

        说明：这是“自动生成评论”的命令。要手动指定评论内容，用 /评论发。
        """
        # 支持：/评论 1 @xxx  或  /评论 @xxx 1  或  /评论 @xxx
        raw = (event.message_str or "").strip()
        text = raw

        # Make comma/Chinese-comma triggered routing compatible (",评论" / "，评论")
        if text.startswith(",") or text.startswith("，"):
            text = text[1:].lstrip()

        for prefix in ("/评论", "评论"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        # Extract mentioned QQ (if any) from message_obj / plain text.
        # 1) normal message chain: align with /点赞
        # 2) fallback to raw_message (some LLM/routers may rebuild message_obj.message)
        target_uin = ""
        try:
            chain = getattr(event.message_obj, "message", [])
            for seg in chain:
                if getattr(seg, "type", "") == "at":
                    qq = getattr(seg, "qq", "")
                    if qq:
                        target_uin = str(qq).strip()
                        break
        except Exception:
            pass

        if not target_uin:
            try:
                raw_msg = getattr(event.message_obj, "raw_message", None)
                if isinstance(raw_msg, dict):
                    raw_chain = raw_msg.get("message") or []
                    for seg in raw_chain:
                        if isinstance(seg, dict) and seg.get("type") == "at":
                            data = seg.get("data") or {}
                            qq = data.get("qq")
                            if qq:
                                target_uin = str(qq).strip()
                                break
            except Exception:
                pass

        if not target_uin:
            m_at = re.search(r"@\s*(\d{5,12})", text)
            if m_at:
                target_uin = m_at.group(1)

        # aiocqhttp log-format fallback: ",评论 ... [At:12345]"
        if not target_uin:
            m_cq = re.search(r"\[At:(\d{5,12})\]", raw)
            if m_cq:
                target_uin = m_cq.group(1)

        if target_uin:
            # Remove @... / [At:...] from arg text so later parsing works.
            text = re.sub(r"@\s*\d{5,12}", "", text).strip()
            text = re.sub(r"\[At:\d{5,12}\]", "", text).strip()

        # If message contains a clear idx (1..999), prefer idx-mode even if routers left extra tokens.
        # This avoids mis-parsing "评论 1" as manual content.
        m_idx = re.search(r"\b(\d{1,3})\b", text)
        if m_idx:
            try:
                text = str(int(m_idx.group(1)))
            except Exception:
                pass

        # If user provided manual comment text, comment the latest post directly.
        manual = (text or "").strip()
        if manual and not (manual.isdigit() and len(manual) <= 3):
            if not self.my_qq or not self.cookie:
                yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
                return

            tid = ""
            if self._recent_posts:
                tid = str(self._recent_posts[-1].get("tid") or "").strip()
            if not tid and self._last_tid:
                tid = str(self._last_tid)

            if not tid:
                yield event.plain_result("找不到最近一条说说的 tid（请先用 /post 或 qz_post 发布）")
                return

            commenter = QzoneCommenter(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(commenter.add_comment, tid, manual)
            logger.info(
                "[Qzone] comment_manual 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )
            if status == 200 and result.ok:
                try:
                    cid = str(getattr(result, "comment_id", "") or "").strip()
                    topic = str(getattr(result, "topic_id", "") or "").strip()
                    if cid and topic:
                        ref = {"topicId": topic, "commentId": cid, "ts": time.time()}
                        self._recent_comment_refs = [
                            r
                            for r in self._recent_comment_refs
                            if not (
                                str(r.get("topicId") or "") == topic
                                and str(r.get("commentId") or "") == cid
                            )
                        ]
                        self._recent_comment_refs.append(ref)
                        logger.info("[Qzone] comment_recorded topicId=%s commentId=%s", topic, cid)
                        if self._comment_ref_max > 0 and len(self._recent_comment_refs) > self._comment_ref_max:
                            self._recent_comment_refs = self._recent_comment_refs[-self._comment_ref_max :]
                except Exception as e:
                    logger.info("[Qzone] comment_record_failed: %s", e)
                yield event.plain_result(f"✅ 已评论 tid={tid}")
            else:
                hint = result.message or "评论失败"
                yield event.plain_result(f"❌ 评论失败：status={status} code={result.code} msg={hint}")
            return

        n = 1
        if text and text.isdigit() and len(text) <= 3:
            try:
                n = int(text)
            except Exception:
                n = 1
        if n <= 0:
            n = 1

        # /评论 N 语义：评论“第 N 新的说说”（只包含说说），不依赖本地缓存。
        # - 未 @ 人：拉取“我主页(main)”
        # - @ 了人：拉取对方主页(main)
        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        host_uin = target_uin or self.my_qq

        # Always fetch target space's mood list first (same data source as /说说, do not fall back to self cache when @别人)
        try:
            fetcher = QzoneFeedFetcher(host_uin, self.cookie, my_qq=self.my_qq)
            # Align with /说说 and /说说表 pagination parameters to avoid triggering different response shapes.
            status, posts_obj = await asyncio.to_thread(fetcher.fetch_mood_posts, 10, 2)
            if status != 200 or not posts_obj:
                diag = getattr(fetcher, "last_diag", "")
                extra = f" | {diag}" if diag else ""
                raise RuntimeError(f"fetch feeds failed status={status} posts={len(posts_obj) if posts_obj else 0}{extra}")

            idx = n - 1
            if idx < 0:
                idx = 0
            if idx >= len(posts_obj):
                yield event.plain_result(f"当前只抓到 {len(posts_obj)} 条说说，无法评论第 {n} 条")
                return

            target = posts_obj[idx]
            tid = str(getattr(target, "tid", "") or "").strip()
            text_hint = str(getattr(target, "text", "") or "").strip()
            if not tid:
                raise RuntimeError("target tid empty")

            topic_id = str(getattr(target, "topic_id", "") or "").strip()
            # Use fetched text if available; otherwise keep a generic hint.
            posts = [{"tid": tid, "topic_id": topic_id, "text": text_hint or "（根据该说说内容生成一句自然短评）", "ts": time.time()}]
        except Exception as e:
            logger.info("[Qzone] fetch mood posts failed: %s", e)

            # When commenting other's space, do not silently fall back to my local cache (it will target wrong tid).
            if target_uin and target_uin != self.my_qq:
                yield event.plain_result(f"获取目标空间说说失败，无法评论（目标空间 {target_uin}）：{e}")
                return

            # Fallback: use in-memory / on-disk post store for SELF only.
            distinct = []
            seen_tid = set()
            for item in reversed(self._recent_posts):
                tid = str(item.get("tid") or "").strip()
                if not tid or tid in seen_tid:
                    continue
                seen_tid.add(tid)
                distinct.append(item)

            idx = n - 1
            if idx < 0:
                idx = 0
            posts = []
            if distinct and idx < len(distinct):
                posts = [distinct[idx]]

            if not posts:
                if self._last_tid and (self._last_post_text or "").strip() and n == 1:
                    posts = [{"tid": self._last_tid, "text": self._last_post_text, "ts": time.time()}]
                else:
                    yield event.plain_result("当前说说内容为空，无法评论（请先用 /post 或 qz_post 发布；或检查 post_store_max>0）")
                    return

        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("未配置文本生成服务（请在 AstrBot WebUI 添加/启用提供商）")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        delay_min = float(self.config.get("comment_delay_min_sec", 1) or 1)
        delay_max = float(self.config.get("comment_delay_max_sec", 2) or 2)
        if delay_min > delay_max:
            delay_min, delay_max = delay_max, delay_min

        commenter = QzoneCommenter(self.my_qq, self.cookie)
        ok_cnt = 0
        attempted = 0
        for item in posts:
            tid = str(item.get("tid") or "").strip()
            content = str(item.get("text") or "").strip()
            if not tid or not content:
                continue

            system_prompt = (
                "你是中文评论助手。请对QQ空间说说写一条具体、贴合内容的评论。\n"
                "要求：不尬、不营销、不带链接；1句或2句；总字数<=60；只输出评论正文，不要解释。"
            )
            resp = await provider.text_chat(prompt=content, system_prompt=system_prompt, context=[])
            cmt_raw = getattr(resp, "content", None)
            if cmt_raw is None:
                cmt_raw = getattr(resp, "text", None)
            if cmt_raw is None:
                rc = getattr(resp, "result_chain", None)
                if rc is not None:
                    cmt_raw = str(rc)
            if cmt_raw is None:
                cmt_raw = str(resp)

            cmt_txt = str(cmt_raw or "")
            m = re.search(r"text='([^']*)'", cmt_txt)
            if m:
                cmt_txt = m.group(1)
            else:
                m = re.search(r"text=\"([^\"]*)\"", cmt_txt)
                if m:
                    cmt_txt = m.group(1)

            cmt = cmt_txt.strip().strip("\"'` ")

            # Debug: if provider returns object repr, log structure to derive correct extraction.
            if "LLMResponse(" in cmt or "MessageChain(" in cmt:
                try:
                    logger.info("[Qzone] comment_debug resp_type=%s", type(resp))
                    keys = [k for k in dir(resp) if not k.startswith("_")]
                    logger.info("[Qzone] comment_debug resp_dir=%s", keys[:80])
                    rc = getattr(resp, "result_chain", None)
                    if rc is not None:
                        logger.info("[Qzone] comment_debug rc_type=%s", type(rc))
                        rc_keys = [k for k in dir(rc) if not k.startswith("_")]
                        logger.info("[Qzone] comment_debug rc_dir=%s", rc_keys[:80])
                        logger.info("[Qzone] comment_debug rc_repr=%s", (repr(rc) or "")[:800])
                except Exception as e:
                    logger.info("[Qzone] comment_debug failed: %s", e)

                # Do not send object repr into Qzone.
                cmt = ""

            if not cmt:
                continue
            if len(cmt) > 60:
                cmt = cmt[:60].rstrip()

            attempted += 1
            topic_id = str(item.get("topic_id") or "").strip()
            status, result = await asyncio.to_thread(commenter.add_comment, tid, cmt, topic_id)
            logger.info(
                "[Qzone] comment 返回 | status=%s ok=%s code=%s msg=%s comment_id=%s topic_id=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                getattr(result, "comment_id", ""),
                getattr(result, "topic_id", ""),
                result.raw_head,
            )
            if status == 200 and result.ok:
                ok_cnt += 1
                try:
                    cid = str(getattr(result, "comment_id", "") or "").strip()
                    topic = str(getattr(result, "topic_id", "") or "").strip()
                    if cid and topic:
                        ref = {"topicId": topic, "commentId": cid, "ts": time.time()}
                        # De-dup while preserving order (avoid repeated deletes on same ref)
                        self._recent_comment_refs = [
                            r
                            for r in self._recent_comment_refs
                            if not (
                                str(r.get("topicId") or "") == topic
                                and str(r.get("commentId") or "") == cid
                            )
                        ]
                        self._recent_comment_refs.append(ref)
                        logger.info("[Qzone] comment_recorded topicId=%s commentId=%s", topic, cid)
                        if self._comment_ref_max > 0 and len(self._recent_comment_refs) > self._comment_ref_max:
                            self._recent_comment_refs = self._recent_comment_refs[-self._comment_ref_max :]
                except Exception:
                    pass
            await asyncio.sleep(delay_min + random.random() * max(0.0, delay_max - delay_min))

        yield event.plain_result(f"评论完成：成功={ok_cnt}/{attempted}")

    @filter.command("评论记录")
    async def comment_refs(self, event: AstrMessageEvent):
        """查看最近成功评论的记录（用于 /删评 1）。

        用法：
        - /评论记录        （默认 10 条）
        - /评论记录 5      （查看最近 5 条）
        """

        text = (event.message_str or "").strip()
        # support both "/评论记录" and "评论记录"
        for prefix in ("/评论记录", "评论记录"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        n = 10
        if text and text.isdigit() and len(text) <= 3:
            try:
                n = int(text)
            except Exception:
                n = 10
        if n <= 0:
            n = 1
        if n > 200:
            n = 200

        refs = list(self._recent_comment_refs or [])
        if not refs:
            yield event.plain_result("评论记录为空（重启会清空；需要先成功评论一次后才会记录）。")
            return

        refs = refs[-n:]
        lines = [f"共 {len(self._recent_comment_refs)} 条，展示最近 {len(refs)} 条（最新在后）："]
        i = 1
        for r in refs:
            topic_id = str(r.get("topicId") or "").strip()
            comment_id = str(r.get("commentId") or "").strip()
            ts = int(float(r.get("ts") or 0) or 0)
            tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"
            lines.append(f"{i}) {tstr} | topicId={topic_id} | commentId={comment_id}")
            i += 1

        yield event.plain_result("\n".join(lines))

    @filter.command("清空评论记录")
    async def clear_comment_refs(self, event: AstrMessageEvent):
        """清空内存中的评论记录（仅影响 /删评 1，重启也会清空）。

        用法：/清空评论记录
        """

        self._recent_comment_refs = []
        yield event.plain_result("✅ 已清空评论记录（仅内存；重启本来也会清空）")

    @filter.command("删评")
    async def del_comment(self, event: AstrMessageEvent):
        """删除评论（删评）。

        用法：
        - /删评 1  （删除“最近一次成功评论”的那条）
        - /删评 <topicId> <commentId>

        说明：topicId/commentId 可从浏览器请求 emotion_cgi_delcomment_ugc 的 Form Data 中获取。
        """
        text = (event.message_str or "").strip()
        for prefix in ("/删评", "删评"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        parts = [p for p in (text or "").split() if p.strip()]

        # Simplified mode:
        # - /删评 1 -> delete latest successful comment recorded in memory.
        # Friendly aliases:
        # - "删除刚刚的评论" / "删除刚刚评论" / "删刚刚的评论" -> same as /删评 1
        alias_text = "".join(parts)
        if alias_text in ("删除刚刚的评论", "删除刚刚评论", "删刚刚的评论", "删刚刚评论"):
            parts = ["1"]

        if len(parts) == 1 and parts[0].isdigit():
            if not self._recent_comment_refs:
                yield event.plain_result("找不到评论记录（重启后会清空）。请用 /删评 <topicId> <commentId> 或先再评论一次。")
                return
            idx = int(parts[0])
            if idx <= 0:
                idx = 1
            if idx > len(self._recent_comment_refs):
                idx = len(self._recent_comment_refs)
            ref = self._recent_comment_refs[-idx]
            topic_id = str(ref.get("topicId") or "").strip()
            comment_id = str(ref.get("commentId") or "").strip()
        else:
            if len(parts) < 2:
                yield event.plain_result("用法：/删评 1  或  /删评 <topicId> <commentId>")
                return
            topic_id = parts[0].strip()
            comment_id = parts[1].strip()

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            deleter = QzoneCommentDeleter(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(deleter.delete_comment, topic_id, comment_id, self.my_qq)
            logger.info(
                "[Qzone] del_comment 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )
            if status == 200 and result.ok:
                # Remove the deleted ref from memory so /评论记录 stays accurate.
                try:
                    self._recent_comment_refs = [
                        r
                        for r in self._recent_comment_refs
                        if not (
                            str(r.get("topicId") or "").strip() == str(topic_id).strip()
                            and str(r.get("commentId") or "").strip() == str(comment_id).strip()
                        )
                    ]
                except Exception:
                    pass
                yield event.plain_result("✅ 已删除评论")
            else:
                hint = result.message or "删除评论失败"
                yield event.plain_result(f"❌ 删除评论失败：status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] 删评异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.command("评论发")
    async def comment_send(self, event: AstrMessageEvent):
        """手动发表评论（仅自己的空间，默认评论最近一条）。

        用法：/评论发 评论内容...
        """
        text = (event.message_str or "").strip()
        for prefix in ("/评论发", "评论发"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        content = (text or "").strip()
        if not content:
            yield event.plain_result("用法：/评论发 评论内容...")
            return

        tid = ""
        if self._recent_posts:
            tid = str(self._recent_posts[-1].get("tid") or "").strip()
        if not tid and self._last_tid:
            tid = str(self._last_tid)

        if not tid:
            yield event.plain_result("找不到最近一条说说的 tid（请先用 /post 或 qz_post 发布）")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        commenter = QzoneCommenter(self.my_qq, self.cookie)
        status, result = await asyncio.to_thread(commenter.add_comment, tid, content)
        logger.info(
            "[Qzone] comment_send 返回 | status=%s ok=%s code=%s msg=%s head=%s",
            status,
            result.ok,
            result.code,
            result.message,
            result.raw_head,
        )
        if status == 200 and result.ok:
            yield event.plain_result(f"✅ 已评论 tid={tid}")
        else:
            hint = result.message or "评论失败"
            yield event.plain_result(f"❌ 评论失败：status={status} code={result.code} msg={hint}")

    # qz_comment removed (LLM feature) per user request.
    # NOTE: Keep the name out to avoid tool_loop calling it.

    @filter.llm_tool(name="qz_del_comment")
    async def llm_tool_qz_del_comment(
        self,
        event: AstrMessageEvent = None,
        topic_id: str = "",
        comment_id: str = "",
        comment_uin: str = "",
        confirm: bool = False,
        latest: bool = False,
        idx: str = "1",
        count: int = 0,
    ):
        """删除QQ空间评论（删评）。

        LLM 使用指南：
        - 优先使用 latest=true（删除最近一次成功评论），避免用户必须提供 topic_id/comment_id。
        - 若用户提供了 topic_id/comment_id，直接按指定删。

        Args:
            topic_id(string): 说说 topicId（形如 2267..._tid__1）
            comment_id(string): 评论 commentId
            comment_uin(string): 评论作者 uin（可选；缺省用自己 uin）
            confirm(boolean): 是否确认直接删除；false 时只返回待删除信息
            latest(boolean): 是否删除最近一次成功评论（基于内存记录，重启清空）
            idx(string): 删除倒数第 idx 条记录（1=最近一次，2=上一次...）
            count(int): 批量删除最近 count 条（优先于 latest/idx；建议 <= 20）
        """

        if event is None:
            logger.error("[Qzone] qz_del_comment missing event (tool runner issue)")
            return

        t = (topic_id or "").strip()
        cid = (comment_id or "").strip()

        # Batch delete (highest priority)
        try:
            c = int(count or 0)
        except Exception:
            c = 0
        if c > 0:
            if not self._recent_comment_refs:
                yield event.plain_result("找不到评论记录（重启后会清空）。请先评论一次再批量删评。")
                return
            max_n = min(c, len(self._recent_comment_refs), 20)
            if not confirm:
                yield event.plain_result(f"待批量删评（未执行）：count={max_n}")
                return

            deleter = QzoneCommentDeleter(self.my_qq, self.cookie)
            ok_cnt = 0
            fail_cnt = 0
            for _ in range(max_n):
                ref = self._recent_comment_refs[-1]
                rt = str(ref.get("topicId") or "").strip()
                rcid = str(ref.get("commentId") or "").strip()
                if not rt or not rcid:
                    self._recent_comment_refs.pop()
                    continue
                status, result = await asyncio.to_thread(deleter.delete_comment, rt, rcid, comment_uin)
                if status == 200 and result.ok:
                    ok_cnt += 1
                    self._recent_comment_refs.pop()
                else:
                    fail_cnt += 1
                    # Avoid infinite loop on same bad ref
                    self._recent_comment_refs.pop()
                await asyncio.sleep(0.4 + random.random() * 0.8)

            yield event.plain_result(f"删评完成：成功={ok_cnt} 失败={fail_cnt}")
            return

        # Resolve ids from memory when requested.
        # - latest=true: delete the most recent successful comment recorded by this plugin.
        # - idx: delete the Nth from latest (1=latest).
        if (not t or not cid) and (latest or (str(idx or "").strip() not in ("", "1"))):
            if not self._recent_comment_refs:
                yield event.plain_result("找不到评论记录（重启后会清空）。请先用命令评论一次（，评论 1），或提供 topic_id/comment_id。")
                return
            try:
                n = int(str(idx or "1").strip())
            except Exception:
                n = 1
            if n <= 0:
                n = 1
            if n > len(self._recent_comment_refs):
                n = len(self._recent_comment_refs)
            ref = self._recent_comment_refs[-n]
            t = str(ref.get("topicId") or "").strip()
            cid = str(ref.get("commentId") or "").strip()

        if not t or not cid:
            yield event.plain_result("参数不足：需要 topic_id + comment_id（或传 latest=true）")
            return

        if not confirm:
            yield event.plain_result(f"待删评（未执行）：topicId={t} commentId={cid}")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            deleter = QzoneCommentDeleter(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(deleter.delete_comment, t, cid, comment_uin)
            logger.info(
                "[Qzone] llm_tool del_comment 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )
            if status == 200 and result.ok:
                try:
                    self._recent_comment_refs = [
                        r
                        for r in self._recent_comment_refs
                        if not (
                            str(r.get("topicId") or "").strip() == str(t).strip()
                            and str(r.get("commentId") or "").strip() == str(cid).strip()
                        )
                    ]
                except Exception:
                    pass
                yield event.plain_result("✅ 已删除评论")
            else:
                hint = result.message or "删除评论失败"
                yield event.plain_result(f"❌ 删除评论失败：status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] llm_tool 删评异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.llm_tool(name="qz_delete")
    async def llm_tool_qz_delete(self, event: AstrMessageEvent, tid: str = "", confirm: bool = False, latest: bool = False, count: int = 0):
        """删除QQ空间说说。

        LLM 使用指南：
        - 如果用户说“删除刚刚/最近那条”，优先传 latest=true（不要凭空编 tid）。
        - 如果对话里出现“tid=xxxx”，就把 xxxx 作为 tid 传入。

        Args:
            tid(string): 说说的 tid（可选；当 latest=true 时可留空）
            confirm(boolean): 是否确认直接删除；false 时只返回待删除信息
            latest(boolean): 是否删除最近一条（仅本插件本次运行内记录；重启会清空）
            count(int): 批量删除最近 N 条（优先级高于 tid/latest；建议 <= 20）
        """
        # Batch delete recent N
        try:
            c = int(count or 0)
        except Exception:
            c = 0

        if c > 0:
            max_n = min(c, self._tid_store_max if self._tid_store_max > 0 else c)
            tids = list(reversed(self._recent_tids))[:max_n]
            if not tids:
                yield event.plain_result("没有可删除的 recent tids")
                return
            if not confirm:
                preview = " ".join(tids[:5])
                more = "" if len(tids) <= 5 else f" ...(+{len(tids)-5})"
                yield event.plain_result(f"将删除最近 {len(tids)} 条 tid：{preview}{more}")
                return

            deleted = 0
            for t2 in tids:
                status, result = await asyncio.to_thread(QzonePoster(self.my_qq, self.cookie).delete_by_tid, t2)
                if status == 200 and result.ok:
                    deleted += 1
                await asyncio.sleep(0.5 + random.random() * 0.7)
            yield event.plain_result(f"批量删除完成：成功={deleted}/{len(tids)}")
            return

        t = (tid or "").strip()

        # If user intent is 'latest', fall back to in-memory last tid.
        if latest and not t:
            t = (self._last_tid or "").strip()

        if not t:
            if self._last_tid:
                yield event.plain_result(f"tid 为空。最近一条 tid={self._last_tid}（建议 latest=true 或直接传 tid）")
            else:
                yield event.plain_result("tid 为空")
            return

        if not confirm:
            yield event.plain_result(f"待删除（未执行）：tid={t}")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.delete_by_tid, t)
            logger.info(
                "[Qzone] llm_tool delete 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )
            if status == 200 and result.ok:
                yield event.plain_result(f"✅ 已删除说说 tid={t}")
            else:
                hint = result.message or "删除失败（可能 cookie/风控/验证码/权限）"
                yield event.plain_result(f"❌ 删除失败：status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] llm_tool 删除说说异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.llm_tool(name="sleep_seconds")
    async def llm_tool_sleep_seconds(self, event: AstrMessageEvent = None, sec: float = 0):
        """Sleep for N seconds (tool-loop helper).

        Args:
            sec(number): seconds to sleep
        """
        await asyncio.to_thread(sleep_seconds, sec)
        if event is not None:
            yield event.plain_result(f"slept {sec} sec")

    @filter.llm_tool(name="qz_post")
    async def llm_tool_qz_post(self, event: AstrMessageEvent, text: str, confirm: bool = False):
        """发送QQ空间说说。

        Args:
            text(string): 要发送的说说正文（纯文字）
            confirm(boolean): 是否确认直接发送；false 时只返回草稿
        """
        content = (text or "").strip()
        if not content:
            yield event.plain_result("草稿为空")
            return

        if not confirm:
            yield event.plain_result(f"草稿（未发送）：{content}")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.publish_text, content)
            logger.info(
                "[Qzone] llm_tool post 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )
            if status == 200 and result.ok:
                tid_info = f" tid={result.tid}" if getattr(result, "tid", "") else ""
                if getattr(result, "tid", ""):
                    self._remember_post(str(result.tid), content)
                yield event.plain_result(f"✅ 已发送说说{tid_info}")
            else:
                hint = result.message or "发送失败（可能 cookie/风控/验证页）"
                yield event.plain_result(f"❌ 发送失败：status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] llm_tool 发说说异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.on_llm_request(priority=5)
    async def on_llm_request(self, event: AstrMessageEvent, req, *args):
        """把 Qzone 工具挂到当前会话的 LLM 请求里（让“，”触发的自然语言也能调用）。"""
        try:
            mgr = self.context.get_llm_tool_manager()
            if not mgr:
                return

            ts = req.func_tool or ToolSet()

            # Prefer get_tool when available; fallback to get_func.
            for name in ("qz_post", "qz_delete", "qz_del_comment", "sleep_seconds"):
                tool = None
                try:
                    tool = mgr.get_tool(name)
                except Exception:
                    try:
                        tool = mgr.get_func(name)
                    except Exception:
                        tool = None
                if tool:
                    ts.add_tool(tool)

            req.func_tool = ts
        except Exception as e:
            logger.warning(f"[Qzone] on_llm_request 挂载工具失败: {e}")

    @filter.command("genpost")
    async def genpost(self, event: AstrMessageEvent):
        """用 AstrBot 已配置的 LLM 生成一条说说，然后自动发送。

        用法：/genpost 主题或要求...
        """
        prompt = (event.message_str or "").strip()
        for prefix in ("/genpost", "genpost"):
            if prompt.lower().startswith(prefix):
                prompt = prompt[len(prefix) :].strip()
                break

        if not prompt:
            yield event.plain_result("用法：/genpost 给我一个主题或要求（如：写条不尬的晚安说说）")
            return

        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("未配置文本生成服务（请在 AstrBot WebUI 添加/启用提供商）")
            return

        system_prompt = (
            "你是中文写作助手。请为QQ空间写一条纯文字说说，符合真人口吻。\n"
            "要求：不尬、不营销、不带链接；1-3句；总字数<=120；只输出说说正文，不要解释。"
        )

        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt, context=[])
            content = (resp.content or "").strip()
        except Exception as e:
            yield event.plain_result(f"LLM 调用失败：{e}")
            return

        if not content:
            yield event.plain_result("LLM 返回为空")
            return

        # 简单清洗：去掉引号/代码块
        content = content.strip("\"'` ")
        content = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", content)
        content = re.sub(r"```\s*$", "", content).strip()

        if len(content) > 120:
            content = content[:120].rstrip()

        yield event.plain_result(f"生成内容：{content}\n正在发送...")

        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.publish_text, content)
            logger.info(
                "[Qzone] genpost->post 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )

            if status == 200 and result.ok:
                yield event.plain_result("✅ 已发送说说")
            else:
                hint = result.message or "发送失败（可能 cookie/风控/验证页）"
                yield event.plain_result(f"❌ 发送失败：status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] genpost 发说说异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 异常：{e}")

    @filter.command("点赞")
    async def like_other(self, event: AstrMessageEvent, count: str = "10"):
        """输入：/点赞 @某人 [次数]
        或：/点赞 QQ号 [次数]

        作用：把目标临时切换到指定QQ空间，并立即执行一次点赞。
        规则：优先解析 @ 段；若没有 @，则从文本里取第一个纯数字作为QQ号。

        兼容说明：部分适配器会吞掉第二个参数（次数），所以这里会从整条消息里兜底提取。
        """
        # count 参数在部分适配器下不可靠（可能被错误填充）。
        # 这里仅信任 message_str 里明确出现的次数；否则一律默认 10。
        count_int: Optional[int] = None

        target_qq = ""
        try:
            chain = getattr(event.message_obj, "message", [])
            for seg in chain:
                if getattr(seg, "type", "") == "at":
                    qq = getattr(seg, "qq", "")
                    if qq:
                        target_qq = str(qq).strip()
                        break
        except Exception:
            target_qq = ""

        msg_text = event.message_str or ""

        if not target_qq:
            # 从文本里取第一个 QQ 号
            m = re.search(r"\b(\d{5,12})\b", msg_text)
            if m:
                target_qq = m.group(1)

        if not target_qq:
            yield event.plain_result("用法：/点赞 @某人 20  或  /点赞 3483935913 20")
            return

        # 解析次数：只认明确的“目标后面紧跟次数”的格式
        m_count = None
        if target_qq:
            m_count = re.search(rf"{re.escape(target_qq)}\D+(\d{{1,3}})\b", msg_text)
        if not m_count:
            m_count = re.search(r"\b点赞\b\D+\d{5,12}\D+(\d{1,3})\b", msg_text)
        if m_count:
            try:
                count_int = int(m_count.group(1))
            except Exception:
                count_int = None

        if count_int is None:
            count_int = 10

        if count_int <= 0:
            count_int = 10
        if count_int > 100:
            count_int = 100

        self._target_qq = target_qq

        # 立即执行一次点赞（不依赖后台 worker 是否已启动）
        if not self.my_qq or not self.cookie:
            yield event.plain_result("配置缺失：my_qq 或 cookie 为空，无法点赞")
            return

        yield event.plain_result(
            f"收到：目标空间={target_qq}，准备点赞（请求 {count_int}，单轮上限 {count_int} 条）..."
        )

        try:
            client = _QzoneClient(self.my_qq, self.cookie)
        except Exception as e:
            yield event.plain_result(f"初始化客户端失败：{e}")
            return

        attempted, ok = await self._like_once(client, target_qq, count_int)
        yield event.plain_result(f"完成：目标空间={target_qq} | 本次尝试={attempted} | 成功={ok}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        # Bot 启动完成后，根据配置决定是否自动启动
        await self._maybe_autostart()
        await self._maybe_start_ai_task()

        if self.protect_enabled:
            if not self.my_qq or not self.cookie:
                logger.error("[Qzone] protect_enabled=true 但 my_qq/cookie 缺失，护评不启动")
            elif self._protect_task is None or self._protect_task.done():
                self._protect_stop.clear()
                self._protect_task = asyncio.create_task(self._protect_worker())
                logger.info("[Qzone] protect worker task created")

    async def terminate(self):
        if self._is_running():
            self._stop_event.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:
                pass

        if self._protect_task is not None and not self._protect_task.done():
            self._protect_stop.set()
            try:
                await asyncio.wait_for(self._protect_task, timeout=10)
            except Exception:
                pass

        if self._ai_task is not None and not self._ai_task.done():
            self._ai_stop.set()
            try:
                await asyncio.wait_for(self._ai_task, timeout=10)
            except Exception:
                pass

        self._save_records()
        logger.info("[Qzone] 插件卸载完成")
