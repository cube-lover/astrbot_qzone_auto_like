# qzone_feed_fetch.py
# Fetch QQ空间 infocenter feeds and extract mood posts for commenting.

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


@dataclass
class MoodPost:
    host_uin: str
    tid: str
    topic_id: str
    abstime: int


def _get_gtk(skey: str) -> int:
    hash_val = 5381
    for ch in skey:
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


def _pick_skey_for_gtk(cookie: str) -> str:
    for key in ("p_skey", "skey", "media_p_skey"):
        v = _extract_cookie_value(cookie, key)
        if v:
            return v
    return ""


def _try_extract_json_from_callback(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()

    if t.startswith("{") and t.endswith("}"):
        try:
            return json.loads(t)
        except Exception:
            return None

    # _Callback({ ... });
    m = re.search(r"_Callback\s*\(\s*(\{.*\})\s*\)\s*;?\s*$", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    # frameElement.callback({ ... })
    m = re.search(r"frameElement\.callback\s*\(\s*(\{.*?\})\s*\)", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    return None


class QzoneFeedFetcher:
    def __init__(self, my_qq: str, cookie: str):
        self.my_qq = str(my_qq).strip()

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
            "origin": "https://user.qzone.qq.com",
            "referer": f"https://user.qzone.qq.com/{self.my_qq}/infocenter?via=toolbar",
        }

    def fetch_mood_posts(self, count: int = 20, max_pages: int = 3) -> Tuple[int, List[MoodPost]]:
        """Fetch latest mood posts from infocenter, across pages, best-effort.

        We parse for mood posts by extracting data in HTML blocks:
        - data-uin (host uin)
        - data-tid (tid)
        - data-topicid (topic id)

        abstime is taken from JSON item field if possible; otherwise 0.
        """

        count = int(count) if count else 20
        if count <= 0:
            count = 20
        max_pages = int(max_pages) if max_pages else 1
        if max_pages <= 0:
            max_pages = 1

        posts: List[MoodPost] = []

        for page in range(1, max_pages + 1):
            url = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
            params = {
                "uin": self.my_qq,
                "scope": "0",
                "view": "1",
                "flag": "1",
                "filter": "all",
                "applist": "all",
                "refresh": "0",
                "pagenum": str(page),
                "count": str(count),
                "useutf8": "1",
                "outputhtmlfeed": "1",
                "g_tk": str(self.g_tk),
            }

            res = requests.get(url, headers=self.headers, params=params, timeout=20)
            status = res.status_code
            text = res.text or ""
            if status != 200 or not text:
                if page == 1:
                    return status, []
                break

            payload = _try_extract_json_from_callback(text)
            data_items: List[Dict[str, Any]] = []
            if isinstance(payload, dict):
                d = payload.get("data")
                if isinstance(d, dict):
                    arr = d.get("data")
                    if isinstance(arr, list):
                        data_items = [x for x in arr if isinstance(x, dict)]

            for item in data_items:
                html = str(item.get("html") or "")
                if not html:
                    continue

                m = re.search(
                    r"name=\\\"feed_data\\\"[^>]*\bdata-tid=\\\"([^\\\"]+)\\\"[^>]*\bdata-uin=\\\"(\d+)\\\"[^>]*\bdata-topicid=\\\"([^\\\"]+)\\\"",
                    html,
                )
                if not m:
                    continue

                tid = m.group(1)
                host_uin = m.group(2)
                topic_id = m.group(3)

                if "_" not in topic_id or "__" not in topic_id:
                    continue

                abstime = 0
                try:
                    if "abstime" in item:
                        abstime = int(str(item.get("abstime") or 0))
                except Exception:
                    abstime = 0

                posts.append(MoodPost(host_uin=host_uin, tid=tid, topic_id=topic_id, abstime=abstime))

            if not data_items:
                break

        seen = set()
        out: List[MoodPost] = []
        for p in posts:
            if p.tid in seen:
                continue
            seen.add(p.tid)
            out.append(p)

        return 200, out
