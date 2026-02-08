import asyncio
import json
import random
import re
import time
import traceback
from pathlib import Path
from typing import Optional, Set, Tuple
from urllib.parse import quote

import requests

from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger


def _now_hms() -> str:
    return time.strftime("%H:%M:%S")


def _get_gtk(p_skey: str) -> int:
    hash_val = 5381
    for ch in p_skey:
        hash_val += (hash_val << 5) + ord(ch)
    return hash_val & 0x7FFFFFFF


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

        p_skey = _extract_cookie_value(cookie, "p_skey")
        if not p_skey:
            raise ValueError("cookie 缺少 p_skey=...（无法计算 g_tk）")

        self.g_tk = _get_gtk(p_skey)
        self.headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            ),
            "cookie": cookie,
            "referer": f"https://user.qzone.qq.com/{my_qq}",
        }

    def fetch_keys(self, count: int, target_qq: Optional[str] = None) -> Tuple[int, Set[str], int]:
        target = str(target_qq or self.my_qq).strip()
        feeds_url = (
            "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
            f"feeds3_html_more?uin={target}&scope=0&view=1&flag=1&refresh=1&count={count}"
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
        like_url = (
            "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/"
            f"internal_dolike_app?g_tk={self.g_tk}"
        )
        payload = {
            "qzreferrer": f"https://user.qzone.qq.com/{self.my_qq}",
            "opuin": self.my_qq,
            "unikey": full_key,
            "curkey": full_key,
            "appid": "311",
            "typeid": "0",
            "active": "0",
            "fupdate": "1",
        }
        res = requests.post(like_url, headers=self.headers, data=payload, timeout=20)
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

        self._liked: Set[str] = set()
        self._data_path = Path(__file__).parent / "data" / "liked_records.json"

        self.my_qq = str(self.config.get("my_qq", "")).strip()
        self.cookie = str(self.config.get("cookie", "")).strip()
        self._target_qq = str(self.config.get("target_qq", "")).strip()
        self.poll_interval = int(self.config.get("poll_interval_sec", 20))
        self.delay_min = int(self.config.get("like_delay_min_sec", 2))
        self.delay_max = int(self.config.get("like_delay_max_sec", 5))
        if self.delay_min > self.delay_max:
            self.delay_min, self.delay_max = self.delay_max, self.delay_min
        self.max_feeds = int(self.config.get("max_feeds_count", 15))
        self.persist = bool(self.config.get("persist_liked", True))

        self.enabled = bool(self.config.get("enabled", False))
        self.auto_start = bool(self.config.get("auto_start", False))

        if self.persist:
            self._load_records()

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
                fetch_count = self.max_feeds
                if self._manual_like_limit > 0:
                    fetch_count = max(fetch_count, self._manual_like_limit)

                status, keys, text_len = await asyncio.to_thread(client.fetch_keys, fetch_count, target)
                logger.info(
                    "[Qzone] feeds 返回 | target=%s status=%s text_len=%s keys=%d",
                    target,
                    status,
                    text_len,
                    len(keys),
                )

                if status != 200:
                    logger.warning("[Qzone] feeds 非200，可能登录失效/风控/重定向（请检查cookie）")

                if not keys:
                    logger.info("[Qzone] 未发现 mood 动态")
                    await asyncio.sleep(self.poll_interval)
                    continue

                new_targets = 0
                liked_this_round = 0
                limit = self._manual_like_limit if self._manual_like_limit > 0 else 0

                for unikey in sorted(keys):
                    if limit and liked_this_round >= limit:
                        break

                    full_key = unikey if unikey.endswith(".1") else (unikey + ".1")
                    if full_key in self._liked:
                        continue

                    new_targets += 1
                    logger.info("[Qzone] 发现新动态: %s", full_key[-24:])

                    await asyncio.sleep(random.randint(self.delay_min, self.delay_max))

                    like_status, resp = await asyncio.to_thread(client.send_like, full_key)
                    resp_head = resp[:300].replace("\n", " ").replace("\r", " ")
                    logger.info("[Qzone] like 返回 | status=%s resp_head=%s", like_status, resp_head)

                    ok = False
                    m = re.search(r"\"code\"\s*:\s*(\d+)", resp)
                    if m and m.group(1) == "0":
                        ok = True

                    if ok:
                        liked_this_round += 1
                        logger.info("[Qzone] ✅ 点赞成功: %s", full_key[-24:])
                        self._liked.add(full_key)
                        self._save_records()
                    else:
                        logger.warning("[Qzone] ❌ 点赞失败: %s", full_key[-24:])

                if new_targets == 0:
                    logger.info("[Qzone] 本轮没有新动态待处理")
                if limit:
                    logger.info("[Qzone] 手动点赞限制=%d，本轮成功点赞=%d", limit, liked_this_round)
                    self._manual_like_limit = 0

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"[Qzone] worker 异常: {e}")
                logger.error(traceback.format_exc())
                await asyncio.sleep(self.poll_interval)

        logger.info("[Qzone] worker 已停止")

    @filter.command("qz_start")
    async def qz_start(self, event: AstrMessageEvent):
        if self._is_running():
            yield event.plain_result("点赞任务已经在运行中（请看后台日志）")
            return

        self._set_enabled(True)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._worker())
        yield event.plain_result("🚀 Qzone 自动点赞后台任务已启动（已打开 enabled 开关）")

    @filter.command("qz_stop")
    async def qz_stop(self, event: AstrMessageEvent):
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

    @filter.command("qz_status")
    async def qz_status(self, event: AstrMessageEvent):
        target = self._target_qq.strip() or self.my_qq
        yield event.plain_result(
            f"运行中={self._is_running()} | enabled={self.enabled} | auto_start={self.auto_start} | target={target} | liked_cache={len(self._liked)}"
        )

    @filter.command("点赞")
    async def like_other(self, event: AstrMessageEvent, count: str = "10"):
        """输入：/点赞 @某人 [次数]
        或：/点赞 QQ号 [次数]

        作用：把目标临时切换到指定QQ空间，并在下一轮最多点赞 count 条动态。
        规则：优先解析 @ 段；若没有 @，则从文本里取第一个纯数字作为QQ号。
        """
        # 兼容平台/适配器把 int 参数当成字符串传入
        try:
            count_int = int(str(count).strip())
        except Exception:
            count_int = 10

        if count_int <= 0:
            count_int = 10
        if count_int > 100:
            count_int = 100

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

        if not target_qq:
            m = re.search(r"\b(\d{5,12})\b", event.message_str or "")
            if m:
                target_qq = m.group(1)

        if not target_qq:
            yield event.plain_result("用法：/点赞 @某人 20  或  /点赞 3483935913 20")
            return

        self._target_qq = target_qq
        self._manual_like_limit = count_int

        if not self._is_running():
            yield event.plain_result(
                f"已切换目标空间：{target_qq}；本次计划点赞 {count_int} 条。\n"
                f"当前任务未运行，请先 /qz_start 启动后台任务。"
            )
            return

        yield event.plain_result(f"已切换目标空间：{target_qq}；下一轮最多点赞 {count_int} 条动态（请看后台日志）。")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        # Bot 启动完成后，根据配置决定是否自动启动
        await self._maybe_autostart()

    async def terminate(self):
        if self._is_running():
            self._stop_event.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:
                pass
        self._save_records()
        logger.info("[Qzone] 插件卸载完成")
