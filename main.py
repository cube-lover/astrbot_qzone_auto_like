import asyncio
import json
import random
import re
import time
import traceback
from pathlib import Path
from typing import Optional, Set, Tuple

from .qzone_post import QzonePoster
from urllib.parse import quote

import requests

from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import ToolSet
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
    # Cookie 灞炰簬鐧诲綍鎬侊紝榛樿涓嶈緭鍑轰换浣曞彲鍏宠仈淇℃伅銆?
    if not cookie_str:
        return ""

    has_p_skey = bool(_extract_cookie_value(cookie_str, "p_skey"))
    return f"<cookie:redacted has_p_skey={has_p_skey}>"


class _QzoneClient:
    def __init__(self, my_qq: str, cookie: str):
        # my_qq: 褰撳墠鐧诲綍 Cookie 瀵瑰簲鐨?QQ锛堢敤浜?referer / opuin锛?
        self.my_qq = my_qq

        # 鍏煎鐢ㄦ埛浠?DevTools 閲屽鍒舵暣琛?"cookie: ..." 鐨勬儏鍐?
        cookie = (cookie or "").strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()

        self.cookie = cookie

        p_skey = _extract_cookie_value(cookie, "p_skey")
        if not p_skey:
            raise ValueError("cookie 缂哄皯 p_skey=...锛堟棤娉曡绠?g_tk锛?)

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
        """鎷夊彇鐩爣绌洪棿鐨勫姩鎬侀摼鎺ラ泦鍚堛€?

        璇ユ帴鍙ｇ敤浜庘€滄墜鍔?/鐐硅禐鈥濓紙鏀寔 target_qq + 鍒嗛〉/鎵╁睍锛夈€?
        鑷姩杞涓嶈蛋杩欓噷锛堣嚜鍔ㄨ疆璇㈢敤 legacy 鑷敤鎺ュ彛锛岃 fetch_keys_self_legacy锛夈€?
        """
        target = str(target_qq or self.my_qq).strip()

        # feeds_html_act_all 鍙傛暟鍚箟锛歶in=鐧诲綍QQ锛宧ostuin=鐩爣绌洪棿QQ
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
        """鑷姩杞涓撶敤锛氭棫鐗?feeds3_html_more锛堜粎鎷夊彇鑷繁鐨勮璇达級銆?

        浣犺繖杈瑰疄娴嬭鎺ュ彛鏇寸ǔ瀹氳兘杩斿洖 mood 閾炬帴锛涘彧鐢ㄤ簬 worker锛屼笉褰卞搷鎵嬪姩 /鐐硅禐銆?
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
        # 澶嶅埢娴忚鍣細h5.qzone.qq.com 鐨?proxy/domain -> w.qzone.qq.com likes CGI銆?
        like_url = f"https://h5.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app?g_tk={self.g_tk}"

        headers = dict(self.headers)
        headers["origin"] = "https://user.qzone.qq.com"
        headers["referer"] = "https://user.qzone.qq.com/"

        # full_key 褰㈠锛歨ttp(s)://user.qzone.qq.com/<hostuin>/mood/<fid>.1
        # 娴忚鍣ㄥ疄闄呬紶鐨勬槸涓嶅甫 .1 鐨?unikey/curkey锛屽苟棰濆甯?from/abstime/fid 绛夊瓧娈点€?
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

        # 涓庢祻瑙堝櫒涓€鑷达細濡傛灉鑳借В鏋愬埌 hostuin锛屽氨鎶婃洿瀹屾暣鐨?qzreferrer 琛ヤ笂銆?
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
    desc="鑷姩渚︽祴骞剁偣璧濹Q绌洪棿鍔ㄦ€侊紙寮哄悗鍙版棩蹇楃増锛?,
    version="1.0.0",
    repo="",
)
class QzoneAutoLikePlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config or {}

        # 杩愯鏃讹細鐩爣绌洪棿锛堣嫢涓虹┖鍒欑洃鎺?鐐硅禐鑷繁鐨勭┖闂达級
        self._target_qq: str = ""
        self._manual_like_limit: int = 0

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # AI 瀹氭椂鍙戣璇翠换鍔★紙涓嶄緷璧栫兢鑱婂悕鍗曪紱鎸夐厤缃紑鍏筹級
        self._ai_task: Optional[asyncio.Task] = None
        self._ai_stop = asyncio.Event()

        self._liked: Set[str] = set()
        self._data_path = Path(__file__).parent / "data" / "liked_records.json"

        # 浠呯敤浜庤嚜鍔ㄨ疆璇㈢殑鈥滃唴瀛樺幓閲嶁€濓紙涓嶈惤鐩橈級锛氶伩鍏嶆瘡杞噸澶嶇偣鍚屼竴鏉°€?
        self._auto_seen: dict[str, float] = {}

        self.my_qq = str(self.config.get("my_qq", "")).strip()
        self.cookie = str(self.config.get("cookie", "")).strip()
        self._target_qq = str(self.config.get("target_qq", "")).strip()
        self.poll_interval = int(self.config.get("poll_interval_sec", 20))
        # 椋庢帶鍙嬪ソ锛氶粯璁ゆ斁鎱㈢偣璧為棿闅旓紙鍙湪閰嶇疆閲屾敼鍥炲幓锛?
        self.delay_min = int(self.config.get("like_delay_min_sec", 12))
        self.delay_max = int(self.config.get("like_delay_max_sec", 25))
        if self.delay_min > self.delay_max:
            self.delay_min, self.delay_max = self.delay_max, self.delay_min
        self.max_feeds = int(self.config.get("max_feeds_count", 15))
        self.persist = False

        self.enabled = bool(self.config.get("enabled", False))
        self.auto_start = bool(self.config.get("auto_start", False))

        # 鍘绘帀缂撳瓨/鍘婚噸鏈哄埗锛氫笉鍔犺浇鍘嗗彶鐐硅禐璁板綍

        logger.info(
            "[Qzone] 鎻掍欢鍒濆鍖?| my_qq=%s poll=%ss delay=[%s,%s] max_feeds=%s persist=%s enabled=%s auto_start=%s liked_cache=%s cookie=%s",
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
            logger.error(f"[Qzone] 鍔犺浇鐐硅禐璁板綍澶辫触: {e}")

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
            logger.error(f"[Qzone] 淇濆瓨鐐硅禐璁板綍澶辫触: {e}")

    def _is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)
        self.config["enabled"] = self.enabled
        try:
            # AstrBotConfig 鏀寔 save_config锛涙櫘閫?dict 娌℃湁
            if hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[Qzone] 淇濆瓨 enabled 閰嶇疆澶辫触: {e}")

    async def _maybe_autostart(self) -> None:
        if not self.auto_start:
            return
        if not self.enabled:
            logger.info("[Qzone] auto_start 寮€鍚紝浣?enabled=false锛屼笉鑷姩鍚姩")
            return
        if self._is_running():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._worker())
        logger.info("[Qzone] auto_start锛氫换鍔″凡鑷姩鍚姩")

    def _ai_enabled(self) -> bool:
        return bool(self.config.get("ai_post_enabled", False))

    async def _maybe_start_ai_task(self) -> None:
        if not self._ai_enabled():
            return
        if self._ai_task is not None and not self._ai_task.done():
            return
        self._ai_stop.clear()
        self._ai_task = asyncio.create_task(self._ai_poster_worker())
        logger.info("[Qzone] AI post锛氫换鍔″凡鍚姩")

    async def _ai_poster_worker(self) -> None:
        if not self.my_qq or not self.cookie:
            logger.error("[Qzone] AI post 閰嶇疆缂哄け锛歮y_qq 鎴?cookie 涓虹┖")
            return

        interval_min = int(self.config.get("ai_post_interval_min", 0) or 0)
        daily_time = str(self.config.get("ai_post_daily_time", "") or "").strip()
        if interval_min <= 0 and not daily_time:
            logger.info("[Qzone] AI post锛氭湭閰嶇疆 interval/daily锛屼换鍔￠€€鍑?)
            return

        # 鍥哄畾鍙戝埌褰撳墠鐧诲綍绌洪棿
        target_umo = None
        try:
            # umo 鐢?None 鍙栭粯璁?provider锛涘彂閫佹秷鎭敤褰撳墠浼氳瘽涓嶅ソ鎷匡紝杩欓噷浠呭悗鍙板彂锛屼笉鍥炵兢
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
                logger.error("[Qzone] AI post锛氭湭閰嶇疆鏂囨湰鐢熸垚鏈嶅姟")
                return

            system_prompt = (
                "浣犳槸涓枃鍐欎綔鍔╂墜銆傝杈撳嚭QQ绌洪棿绾枃瀛楄璇存鏂囥€俓n"
                "瑕佹眰锛氫笉灏€佷笉钀ラ攢銆佷笉甯﹂摼鎺ワ紱1-3鍙ワ紱鎬诲瓧鏁?=120锛涘彧杈撳嚭姝ｆ枃锛屼笉瑕佽В閲娿€?
            )
            try:
                resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt, context=[])
                content = (resp.content or "").strip()
            except Exception as e:
                logger.error(f"[Qzone] AI post锛歀LM 璋冪敤澶辫触: {e}")
                return

            if not content:
                logger.error("[Qzone] AI post锛歀LM 杩斿洖涓虹┖")
                return

            content = content.strip("\"'` ")
            content = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", content)
            content = re.sub(r"```\s*$", "", content).strip()
            if len(content) > 120:
                content = content[:120].rstrip()

            if bool(self.config.get("ai_post_mark", True)):
                content = "銆怉I鍙戦€併€? + content

            status, result = await asyncio.to_thread(poster.publish_text, content)
            logger.info(
                "[Qzone] AI post 杩斿洖 | status=%s ok=%s code=%s msg=%s tid=%s",
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
                        "[Qzone] AI delete 杩斿洖 | status=%s ok=%s code=%s msg=%s tid=%s",
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
                logger.error(f"[Qzone] AI post worker 寮傚父: {e}")
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

        # 榛樿鍛戒护浠嶇劧鏄竴娆″彇 count=10锛涘彧鏈夎嚜瀹氫箟璇锋眰澶т簬10鏃舵墠鍚敤閫掑妯″紡銆?
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
                # 鑷姩杞锛氱敤鏃х増 self-feeds 鎺ュ彛锛屾洿绋冲畾銆?
                status, keys, text_len = await asyncio.to_thread(client.fetch_keys_self_legacy, cur_count)
            else:
                status, keys, text_len = await asyncio.to_thread(client.fetch_keys, cur_count, target)
            logger.info(
                "[Qzone] feeds 杩斿洖 | target=%s status=%s text_len=%s keys=%d count=%d",
                target,
                status,
                text_len,
                len(keys),
                cur_count,
            )

            if not keys:
                # keys=0 涓?text_len 寰堢煭鏃讹紝閫氬父鏄潈闄?椋庢帶/杩斿洖缁撴瀯鍙樺寲锛涙墦鍗扮墖娈垫柟渚挎帓鏌ャ€?
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
                    logger.warning("[Qzone] feeds head 鑾峰彇澶辫触: %s", e)

            if status != 200:
                logger.warning("[Qzone] feeds 闈?00锛屽彲鑳界櫥褰曞け鏁?椋庢帶/閲嶅畾鍚戯紙璇锋鏌ookie锛?)

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
                    # 娓呯悊杩囨湡
                    expired = [k for k, ts in self._auto_seen.items() if now_ts - ts > ttl]
                    for k in expired:
                        self._auto_seen.pop(k, None)

            for full_key in new_keys:
                if attempted >= limit:
                    break

                if dedup and full_key in self._auto_seen:
                    continue

                attempted += 1
                logger.info("[Qzone] 鍙戠幇鏂板姩鎬? %s", full_key[-24:])

                # 杩涗竴姝ユ姈鍔細閬垮厤鍥哄畾闂撮殧瑙﹀彂椋庢帶
                jitter = random.random() * 1.5
                await asyncio.sleep(random.randint(self.delay_min, self.delay_max) + jitter)

                like_status, resp = await asyncio.to_thread(client.send_like, full_key)
                resp_head = resp[:300].replace("\n", " ").replace("\r", " ")
                logger.info("[Qzone] like 杩斿洖 | status=%s resp_head=%s", like_status, resp_head)

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

                logger.info("[Qzone] like 缁撴灉 | code=%s msg=%s", code, msg)
                if msg and "璁板綍鎴愬姛" in msg:
                    ok = False
                else:
                    ok = code == 0

                if ok:
                    liked_ok += 1
                    logger.info("[Qzone] 鉁?鐐硅禐鎴愬姛: %s", full_key[-24:])
                    if dedup:
                        self._auto_seen[full_key] = now_ts
                else:
                    logger.warning("[Qzone] 鉂?鐐硅禐澶辫触: %s", full_key[-24:])

            if not ramp_enabled:
                break

            if cur_count >= max_count:
                break
            cur_count = min(cur_count + ramp_step, max_count)
            # 姣忔鍔犲ぇ count 鍓嶇◢寰紤鎭竴涓嬶紝闄嶄綆椋庢帶姒傜巼
            await asyncio.sleep(1.0 + random.random() * 2.0)

        return attempted, liked_ok

    async def _worker(self) -> None:
        if not self.enabled:
            logger.info("[Qzone] enabled=false锛寃orker 涓嶅惎鍔?)
            return

        if not self.my_qq or not self.cookie:
            logger.error("[Qzone] 閰嶇疆缂哄け锛歮y_qq 鎴?cookie 涓虹┖锛屼换鍔℃棤娉曞惎鍔?)
            return

        try:
            client = _QzoneClient(self.my_qq, self.cookie)
        except Exception as e:
            logger.error(f"[Qzone] 鍒濆鍖栧鎴风澶辫触: {e}")
            return

        logger.info("[Qzone] worker 鍚姩 | g_tk=%s", client.g_tk)

        while not self._stop_event.is_set():
            try:
                logger.info("[%s] 姝ｅ湪渚︽祴...锛坙iked_cache=%d锛?, _now_hms(), len(self._liked))

                target = self._target_qq.strip() or self.my_qq
                limit = self._manual_like_limit if self._manual_like_limit > 0 else self.max_feeds

                attempted, ok = await self._like_once(client, target, limit, dedup=True)

                if attempted == 0:
                    logger.info("[Qzone] 鏈疆娌℃湁鏂板姩鎬佸緟澶勭悊")

                if self._manual_like_limit > 0:
                    logger.info(
                        "[Qzone] 鎵嬪姩鐐硅禐闄愬埗=%d锛屾湰杞皾璇?%d 鎴愬姛=%d",
                        self._manual_like_limit,
                        attempted,
                        ok,
                    )
                    self._manual_like_limit = 0

                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"[Qzone] worker 寮傚父: {e}")
                logger.error(traceback.format_exc())
                await asyncio.sleep(self.poll_interval)

        logger.info("[Qzone] worker 宸插仠姝?)

    @filter.command("start")
    async def start(self, event: AstrMessageEvent):
        if self._is_running():
            yield event.plain_result("鐐硅禐浠诲姟宸茬粡鍦ㄨ繍琛屼腑锛堣鐪嬪悗鍙版棩蹇楋級")
            return

        self._set_enabled(True)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._worker())
        yield event.plain_result("馃殌 Qzone 鑷姩鐐硅禐鍚庡彴浠诲姟宸插惎鍔紙宸叉墦寮€ enabled 寮€鍏筹級")

    @filter.command("stop")
    async def stop(self, event: AstrMessageEvent):
        if not self._is_running():
            self._set_enabled(False)
            yield event.plain_result("褰撳墠娌℃湁杩愯涓殑浠诲姟锛堝凡鍏抽棴 enabled 寮€鍏筹級")
            return

        self._set_enabled(False)
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except Exception:
            pass
        yield event.plain_result("馃洃 鐐硅禐浠诲姟宸插仠姝紙宸插叧闂?enabled 寮€鍏筹級")

    @filter.command("status")
    async def status(self, event: AstrMessageEvent):
        target = self._target_qq.strip() or self.my_qq
        yield event.plain_result(
            f"杩愯涓?{self._is_running()} | enabled={self.enabled} | auto_start={self.auto_start} | target={target} | liked_cache={len(self._liked)}"
        )

    @filter.command("post")
    async def post(self, event: AstrMessageEvent):
        """鍙戜竴鏉＄函鏂囧瓧璇磋銆?

        鐢ㄦ硶锛?post 浣犵殑鍐呭...
        """
        text = (event.message_str or "").strip()
        for prefix in ("/post", "post"):
            if text.lower().startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        if not text:
            yield event.plain_result("鐢ㄦ硶锛?post 浣犵殑鍐呭锛堟殏浠呮敮鎸佺函鏂囧瓧锛?)
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("閰嶇疆缂哄け锛歮y_qq 鎴?cookie 涓虹┖")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.publish_text, text)
            logger.info(
                "[Qzone] post 杩斿洖 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )

            if status == 200 and result.ok:
                yield event.plain_result("鉁?宸插彂閫佽璇?)
            else:
                hint = result.message or "鍙戦€佸け璐ワ紙鍙兘 cookie/椋庢帶/楠岃瘉椤碉級"
                yield event.plain_result(f"鉂?鍙戦€佸け璐ワ細status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] 鍙戣璇村紓甯? {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"鉂?寮傚父锛歿e}")

    @filter.llm_tool(name="qz_post")
    async def llm_tool_qz_post(self, event: AstrMessageEvent, text: str, confirm: bool = False):
        """鍙戦€丵Q绌洪棿璇磋銆?

        Args:
            text(string): 瑕佸彂閫佺殑璇磋姝ｆ枃锛堢函鏂囧瓧锛?
            confirm(boolean): 鏄惁纭鐩存帴鍙戦€侊紱false 鏃跺彧杩斿洖鑽夌
        """
        content = (text or "").strip()
        if not content:
            yield event.plain_result("鑽夌涓虹┖")
            return

        if not confirm:
            yield event.plain_result(f"鑽夌锛堟湭鍙戦€侊級锛歿content}")
            return

        if not self.my_qq or not self.cookie:
            yield event.plain_result("閰嶇疆缂哄け锛歮y_qq 鎴?cookie 涓虹┖")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.publish_text, content)
            logger.info(
                "[Qzone] llm_tool post 杩斿洖 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )
            if status == 200 and result.ok:
                yield event.plain_result("鉁?宸插彂閫佽璇?)
            else:
                hint = result.message or "鍙戦€佸け璐ワ紙鍙兘 cookie/椋庢帶/楠岃瘉椤碉級"
                yield event.plain_result(f"鉂?鍙戦€佸け璐ワ細status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] llm_tool 鍙戣璇村紓甯? {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"鉂?寮傚父锛歿e}")

    @filter.on_llm_request(priority=5)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """鎶?qz_post 宸ュ叿鎸傚埌褰撳墠浼氳瘽鐨?LLM 璇锋眰閲屻€?

        璇存槑锛氳繖鏍蜂綘鐢ㄥ敜閱掕瘝鑱婂ぉ鏃讹紝妯″瀷灏卞彲浠ラ€夋嫨璋冪敤 qz_post銆?
        """
        try:
            mgr = self.context.get_llm_tool_manager()
            tool = mgr.get_func("qz_post") if mgr else None
            if not tool:
                return

            ts = req.func_tool or ToolSet()
            ts.add_tool(tool)
            req.func_tool = ts
        except Exception as e:
            logger.warning(f"[Qzone] on_llm_request 鎸傝浇宸ュ叿澶辫触: {e}")

    @filter.command("genpost")
    async def genpost(self, event: AstrMessageEvent):
        """鐢?AstrBot 宸查厤缃殑 LLM 鐢熸垚涓€鏉¤璇达紝鐒跺悗鑷姩鍙戦€併€?

        鐢ㄦ硶锛?genpost 涓婚鎴栬姹?..
        """
        prompt = (event.message_str or "").strip()
        for prefix in ("/genpost", "genpost"):
            if prompt.lower().startswith(prefix):
                prompt = prompt[len(prefix) :].strip()
                break

        if not prompt:
            yield event.plain_result("鐢ㄦ硶锛?genpost 缁欐垜涓€涓富棰樻垨瑕佹眰锛堝锛氬啓鏉′笉灏殑鏅氬畨璇磋锛?)
            return

        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("鏈厤缃枃鏈敓鎴愭湇鍔★紙璇峰湪 AstrBot WebUI 娣诲姞/鍚敤鎻愪緵鍟嗭級")
            return

        system_prompt = (
            "浣犳槸涓枃鍐欎綔鍔╂墜銆傝涓篞Q绌洪棿鍐欎竴鏉＄函鏂囧瓧璇磋锛岀鍚堢湡浜哄彛鍚汇€俓n"
            "瑕佹眰锛氫笉灏€佷笉钀ラ攢銆佷笉甯﹂摼鎺ワ紱1-3鍙ワ紱鎬诲瓧鏁?=120锛涘彧杈撳嚭璇磋姝ｆ枃锛屼笉瑕佽В閲娿€?
        )

        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt, context=[])
            content = (resp.content or "").strip()
        except Exception as e:
            yield event.plain_result(f"LLM 璋冪敤澶辫触锛歿e}")
            return

        if not content:
            yield event.plain_result("LLM 杩斿洖涓虹┖")
            return

        # 绠€鍗曟竻娲楋細鍘绘帀寮曞彿/浠ｇ爜鍧?
        content = content.strip("\"'` ")
        content = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", content)
        content = re.sub(r"```\s*$", "", content).strip()

        if len(content) > 120:
            content = content[:120].rstrip()

        yield event.plain_result(f"鐢熸垚鍐呭锛歿content}\n姝ｅ湪鍙戦€?..")

        if not self.my_qq or not self.cookie:
            yield event.plain_result("閰嶇疆缂哄け锛歮y_qq 鎴?cookie 涓虹┖")
            return

        try:
            poster = QzonePoster(self.my_qq, self.cookie)
            status, result = await asyncio.to_thread(poster.publish_text, content)
            logger.info(
                "[Qzone] genpost->post 杩斿洖 | status=%s ok=%s code=%s msg=%s head=%s",
                status,
                result.ok,
                result.code,
                result.message,
                result.raw_head,
            )

            if status == 200 and result.ok:
                yield event.plain_result("鉁?宸插彂閫佽璇?)
            else:
                hint = result.message or "鍙戦€佸け璐ワ紙鍙兘 cookie/椋庢帶/楠岃瘉椤碉級"
                yield event.plain_result(f"鉂?鍙戦€佸け璐ワ細status={status} code={result.code} msg={hint}")
        except Exception as e:
            logger.error(f"[Qzone] genpost 鍙戣璇村紓甯? {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"鉂?寮傚父锛歿e}")

    @filter.command("鐐硅禐")
    async def like_other(self, event: AstrMessageEvent, count: str = "10"):
        """杈撳叆锛?鐐硅禐 @鏌愪汉 [娆℃暟]
        鎴栵細/鐐硅禐 QQ鍙?[娆℃暟]

        浣滅敤锛氭妸鐩爣涓存椂鍒囨崲鍒版寚瀹歈Q绌洪棿锛屽苟绔嬪嵆鎵ц涓€娆＄偣璧炪€?
        瑙勫垯锛氫紭鍏堣В鏋?@ 娈碉紱鑻ユ病鏈?@锛屽垯浠庢枃鏈噷鍙栫涓€涓函鏁板瓧浣滀负QQ鍙枫€?

        鍏煎璇存槑锛氶儴鍒嗛€傞厤鍣ㄤ細鍚炴帀绗簩涓弬鏁帮紙娆℃暟锛夛紝鎵€浠ヨ繖閲屼細浠庢暣鏉℃秷鎭噷鍏滃簳鎻愬彇銆?
        """
        # count 鍙傛暟鍦ㄩ儴鍒嗛€傞厤鍣ㄤ笅涓嶅彲闈狅紙鍙兘琚敊璇～鍏咃級銆?
        # 杩欓噷浠呬俊浠?message_str 閲屾槑纭嚭鐜扮殑娆℃暟锛涘惁鍒欎竴寰嬮粯璁?10銆?
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
            # 浠庢枃鏈噷鍙栫涓€涓?QQ 鍙?
            m = re.search(r"\b(\d{5,12})\b", msg_text)
            if m:
                target_qq = m.group(1)

        if not target_qq:
            yield event.plain_result("鐢ㄦ硶锛?鐐硅禐 @鏌愪汉 20  鎴? /鐐硅禐 3483935913 20")
            return

        # 瑙ｆ瀽娆℃暟锛氬彧璁ゆ槑纭殑鈥滅洰鏍囧悗闈㈢揣璺熸鏁扳€濈殑鏍煎紡
        m_count = None
        if target_qq:
            m_count = re.search(rf"{re.escape(target_qq)}\D+(\d{{1,3}})\b", msg_text)
        if not m_count:
            m_count = re.search(r"\b鐐硅禐\b\D+\d{5,12}\D+(\d{1,3})\b", msg_text)
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

        # 绔嬪嵆鎵ц涓€娆＄偣璧烇紙涓嶄緷璧栧悗鍙?worker 鏄惁宸插惎鍔級
        if not self.my_qq or not self.cookie:
            yield event.plain_result("閰嶇疆缂哄け锛歮y_qq 鎴?cookie 涓虹┖锛屾棤娉曠偣璧?)
            return

        yield event.plain_result(
            f"鏀跺埌锛氱洰鏍囩┖闂?{target_qq}锛屽噯澶囩偣璧烇紙璇锋眰 {count_int}锛屽崟杞笂闄?{count_int} 鏉★級..."
        )

        try:
            client = _QzoneClient(self.my_qq, self.cookie)
        except Exception as e:
            yield event.plain_result(f"鍒濆鍖栧鎴风澶辫触锛歿e}")
            return

        attempted, ok = await self._like_once(client, target_qq, count_int)
        yield event.plain_result(f"瀹屾垚锛氱洰鏍囩┖闂?{target_qq} | 鏈灏濊瘯={attempted} | 鎴愬姛={ok}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        # Bot 鍚姩瀹屾垚鍚庯紝鏍规嵁閰嶇疆鍐冲畾鏄惁鑷姩鍚姩
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
        logger.info("[Qzone] 鎻掍欢鍗歌浇瀹屾垚")
