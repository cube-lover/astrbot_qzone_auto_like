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
        """拉取目标空间的动态链接集合。

        兼容不同前端：优先使用 feeds_html_act_all（较常见），必要时可再扩展其他 CGI。
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

        self._liked: Set[str] = set()
        self._data_path = Path(__file__).parent / "data" / "liked_records.json"

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
            await asyncio.sleep(1.0 + random.random() * 2.0)

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

    async def terminate(self):
        if self._is_running():
            self._stop_event.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:
                pass
        self._save_records()
        logger.info("[Qzone] 插件卸载完成")
