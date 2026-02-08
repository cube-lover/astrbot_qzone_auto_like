import asyncio
import json
import random
import re
import time
import traceback
from pathlib import Path
from typing import Optional, Set, Tuple

from .qzone_post import QzonePoster
from .qzone_comment import QzoneCommenter
from .qzone_del_comment import QzoneCommentDeleter
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
    version="1.0.0",
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

        # 去掉缓存/去重机制：不加载历史点赞记录

        logger.info(
            "[Qzone] 插件初始化 | my_qq=%s poll=%ss delay=[%s,%s] max_feeds=%s persist=%s enabled=%s auto_start=%s liked_cache=%s cookie=%s",
            self.my_qq,
            self.poll_interval,
            self.delay_min,
            self.delay_max,
            self.max_feeds,
            self.persist,
            self.enabled,
            self.auto_start,
            len(self._liked),
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
        if not self._ai_enabled():
            return
        if self._ai_task is not None and not self._ai_task.done():
            return
        self._ai_stop.clear()
        self._ai_task = asyncio.create_task(self._ai_poster_worker())
        logger.info("[Qzone] AI post：任务已启动")

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

        async def _gen_and_post(prompt: str) -> None:
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
                content = (resp.content or "").strip()
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
            logger.info(
                "[Qzone] AI post 返回 | status=%s ok=%s code=%s msg=%s tid=%s",
                status,
                result.ok,
                result.code,
                result.message,
                getattr(result, "tid", ""),
            )

            delete_after = int(self.config.get("ai_post_delete_after_min", 0) or 0)
            tid = getattr(result, "tid", "")
            if status == 200 and result.ok and delete_after > 0 and tid:
                async def _del_later() -> None:
                    await asyncio.sleep(delete_after * 60)
                    ds, dr = await asyncio.to_thread(poster.delete_by_tid, tid)
                    logger.info(
                        "[Qzone] AI delete 返回 | status=%s ok=%s code=%s msg=%s tid=%s",
                        ds,
                        dr.ok,
                        dr.code,
                        dr.message,
                        tid,
                    )
                asyncio.create_task(_del_later())

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

        while not self._ai_stop.is_set():
            try:
                # interval first
                if interval_min > 0:
                    prompt = str(self.config.get("ai_post_prompt", "") or "").strip()
                    if prompt:
                        await _gen_and_post(prompt)
                    # sleep with jitter
                    jitter = random.random() * 3.0
                    await asyncio.wait_for(self._ai_stop.wait(), timeout=interval_min * 60 + jitter)
                    continue

                # daily mode
                if daily_time and next_daily_sleep is not None:
                    await asyncio.wait_for(self._ai_stop.wait(), timeout=next_daily_sleep)
                    if self._ai_stop.is_set():
                        break
                    prompt = str(self.config.get("ai_post_daily_prompt", "") or "").strip()
                    if prompt:
                        await _gen_and_post(prompt)
                    next_daily_sleep = _seconds_until(daily_time)
                    continue

                # fallback
                await asyncio.wait_for(self._ai_stop.wait(), timeout=60)
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

    @filter.command("status")
    async def status(self, event: AstrMessageEvent):
        target = self._target_qq.strip() or self.my_qq
        yield event.plain_result(
            f"运行中={self._is_running()} | enabled={self.enabled} | auto_start={self.auto_start} | target={target} | liked_cache={len(self._liked)}"
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
        text = (event.message_str or "").strip()
        for prefix in ("/评论", "评论"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

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
                    cid = getattr(result, "comment_id", "")
                    topic = getattr(result, "topic_id", "")
                    if cid and topic:
                        self._recent_comment_refs.append(
                            {"topicId": str(topic), "commentId": str(cid), "ts": time.time()}
                        )
                        if self._comment_ref_max > 0 and len(self._recent_comment_refs) > self._comment_ref_max:
                            self._recent_comment_refs = self._recent_comment_refs[-self._comment_ref_max :]
                except Exception:
                    pass
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

        posts = list(reversed(self._recent_posts))[:n]
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
            status, result = await asyncio.to_thread(commenter.add_comment, tid, cmt)
            logger.info(
                "[Qzone] comment 返回 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )
            if status == 200 and result.ok:
                ok_cnt += 1
                try:
                    cid = getattr(result, "comment_id", "")
                    topic = getattr(result, "topic_id", "")
                    if cid and topic:
                        self._recent_comment_refs.append(
                            {"topicId": str(topic), "commentId": str(cid), "ts": time.time()}
                        )
                        if self._comment_ref_max > 0 and len(self._recent_comment_refs) > self._comment_ref_max:
                            self._recent_comment_refs = self._recent_comment_refs[-self._comment_ref_max :]
                except Exception:
                    pass
            await asyncio.sleep(delay_min + random.random() * max(0.0, delay_max - delay_min))

        yield event.plain_result(f"评论完成：成功={ok_cnt}/{attempted}")

    @filter.command("删评")
    async def del_comment(self, event: AstrMessageEvent):
        """删除评论（删评）。

        用法：/删评 <topicId> <commentId>
        示例：/删评 2267154199_17072287a6cb88698f750200__1 2

        说明：topicId/commentId 可从浏览器请求 emotion_cgi_delcomment_ugc 的 Form Data 中获取。
        """
        text = (event.message_str or "").strip()
        for prefix in ("/删评", "删评"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        parts = [p for p in (text or "").split() if p.strip()]

        # Simplified mode: /删评 1 -> delete latest successful comment recorded in memory.
        if len(parts) == 1 and parts[0].isdigit():
            if not self._recent_comment_refs:
                yield event.plain_result("没有可删的最近评论记录（请先成功评论一次）")
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

    @filter.llm_tool(name="qz_comment")
    async def llm_tool_qz_comment(self, event: AstrMessageEvent, count: int = 1, confirm: bool = False):
        """根据最近发布的说说内容生成并发表评论（仅自己的空间）。

        Args:
            count(int): 评论最近 N 条（默认1；建议 <= 10）
            confirm(boolean): 是否确认直接发表评论；false 时只返回草稿
        """
        n = int(count or 1)
        if n <= 0:
            n = 1

        posts = list(reversed(self._recent_posts))[:n]
        if not posts:
            # Fallback: if we just posted via qz_post, we may only have in-memory last text.
            if self._last_tid and (self._last_post_text or "").strip() and n == 1:
                posts = [{"tid": self._last_tid, "text": self._last_post_text, "ts": time.time()}]
            else:
                yield event.plain_result("当前说说内容为空，无法评论（请先用 /post 或 qz_post 发布；或检查 post_store_max>0）")
                return

        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("未配置文本生成服务")
            return

        system_prompt = (
            "你是中文评论助手。请对QQ空间说说写一条具体、贴合内容的评论。\n"
            "要求：不尬、不营销、不带链接；1句或2句；总字数<=60；只输出评论正文，不要解释。"
        )

        drafts = []
        for item in posts:
            content = str(item.get("text") or "").strip()
            tid = str(item.get("tid") or "").strip()
            if not tid or not content:
                continue
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
            cmt = cmt_txt.strip().strip("\"'` ")
            if len(cmt) > 60:
                cmt = cmt[:60].rstrip()
            drafts.append((tid, cmt))

        if not drafts:
            yield event.plain_result("生成评论为空")
            return

        if not confirm:
            preview = "\n".join([f"tid={t} 评论={c}" for t, c in drafts[:5]])
            more = "" if len(drafts) <= 5 else f"\n...(+{len(drafts)-5})"
            yield event.plain_result("草稿（未发送）：\n" + preview + more)
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
        for tid, cmt in drafts:
            status, result = await asyncio.to_thread(commenter.add_comment, tid, cmt)
            if status == 200 and result.ok:
                ok_cnt += 1
            await asyncio.sleep(delay_min + random.random() * max(0.0, delay_max - delay_min))

        yield event.plain_result(f"评论完成：成功={ok_cnt}/{len(drafts)}")

    @filter.llm_tool(name="qz_del_comment")
    async def llm_tool_qz_del_comment(self, event: AstrMessageEvent, topic_id: str = "", comment_id: str = "", comment_uin: str = "", confirm: bool = False):
        """删除QQ空间评论（删评）。

        LLM 使用指南：
        - topic_id 通常形如 "<hostUin>_<tid>__1"。
        - comment_id 是评论唯一 id（可从浏览器 delcomment_ugc 的 FormData 里拿到）。

        Args:
            topic_id(string): 说说 topicId（形如 2267..._tid__1）
            comment_id(string): 评论 commentId
            comment_uin(string): 评论作者 uin（可选；缺省用自己 uin）
            confirm(boolean): 是否确认直接删除；false 时只返回待删除信息
        """
        t = (topic_id or "").strip()
        cid = (comment_id or "").strip()
        if not t or not cid:
            yield event.plain_result("参数不足：需要 topic_id + comment_id")
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
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """把 qz_post 工具挂到当前会话的 LLM 请求里。

        说明：这样你用唤醒词聊天时，模型就可以选择调用 qz_post。
        """
        try:
            mgr = self.context.get_llm_tool_manager()
            tool = mgr.get_func("qz_post") if mgr else None
            if not tool:
                return

            ts = req.func_tool or ToolSet()
            ts.add_tool(tool)
            # AstrBot versions differ: some managers expose get_tool(), others only get_func().
            try:
                ts.add_tool(mgr.get_tool("qz_delete"))
                ts.add_tool(mgr.get_tool("qz_comment"))
                ts.add_tool(mgr.get_tool("qz_del_comment"))
            except Exception:
                ts.add_tool(mgr.get_func("qz_delete"))
                ts.add_tool(mgr.get_func("qz_comment"))
                ts.add_tool(mgr.get_func("qz_del_comment"))
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

    async def terminate(self):
        if self._is_running():
            self._stop_event.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
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
