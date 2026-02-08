# qzone_feed_fetch.py
# Fetch QQ空间 feed list and extract mood posts for commenting.

from __future__ import annotations

import json
import random
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
        # NOTE: Qzone often returns JS object literal (single quotes, unquoted keys, undefined), not strict JSON.
        # We'll fall back to a light-weight extractor elsewhere when json.loads fails.
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


def _extract_feed_items_from_js_callback(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []

    # Extract friend_data / host_data arrays from JS callback body without full JS parsing.
    # We only need each item's html + abstime.
    s = text

    def _find_array(var_name: str) -> str:
        m = re.search(r"\b" + re.escape(var_name) + r"\s*:\s*\[", s)
        if not m:
            return ""
        i = m.end()  # position after '['
        depth = 1
        in_str = False
        esc = False
        quote = ""
        j = i
        while j < len(s):
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    in_str = False
            else:
                if ch in ("\"", "'"):
                    in_str = True
                    quote = ch
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        return s[i:j]
            j += 1
        return ""

    arr = _find_array("friend_data") or _find_array("host_data")
    if not arr:
        return []

    items: List[Dict[str, Any]] = []
    abstime_pat = re.compile(r"\babstime\s*:\s*'?([0-9]{6,})'?")

    # The old regex `html:'...'` is too fragile for real payloads.
    # Instead, slice from `html:` to the next `,opuin:` (present in items) and decode afterward.
    for m in re.finditer(r"\bhtml\s*:\s*", arr):
        end_m = re.search(r",\s*opuin\s*:\s*", arr[m.end() :])
        if not end_m:
            continue

        html_blob = arr[m.end() : m.end() + end_m.start()]
        html_blob = html_blob.strip().rstrip(",")

        if len(html_blob) >= 2 and html_blob[0] in ("'", '"') and html_blob[-1] == html_blob[0]:
            html = html_blob[1:-1]
        else:
            html = html_blob

        html = html.replace("\\x3C", "<").replace("\\x3E", ">")
        html = html.replace("\\/", "/")
        html = html.replace("\\\"", '"').replace("\\'", "'")
        html = html.replace("\\x22", '"')

        tail = arr[m.end() : m.end() + 2000]
        am = abstime_pat.search(tail)
        abstime = am.group(1) if am else ""

        items.append({"html": html, "abstime": abstime})
        if len(items) >= 200:
            break

    return items


class QzoneFeedFetcher:
    def __init__(self, my_qq: str, cookie: str):
        self.my_qq = str(my_qq).strip()
        self.last_diag: str = ""
        self.last_sample_html_head: str = ""

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
            "referer": f"https://user.qzone.qq.com/{self.my_qq}/main",
        }

    def fetch_mood_posts(self, count: int = 20, max_pages: int = 3) -> Tuple[int, List[MoodPost]]:
        """Fetch latest mood posts from your own space (main page feed), across pages.

        Uses feeds_html_act_all with uin=loginQQ and hostuin=targetQQ (here we use my_qq).
        This matches what the browser loads on /<uin>/main.
        """

        count = int(count) if count else 20
        if count <= 0:
            count = 20
        max_pages = int(max_pages) if max_pages else 1
        if max_pages <= 0:
            max_pages = 1

        posts: List[MoodPost] = []
        start = 0

        for _ in range(max_pages):
            url = (
                "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds_html_act_all"
                f"?uin={self.my_qq}&hostuin={self.my_qq}"
                "&scope=0&filter=all&flag=1&refresh=0&firstGetGroup=0&mixnocache=0&scene=0"
                f"&begintime=undefined&icServerTime=&start={start}&count={count}"
                "&sidomain=qzonestyle.gtimg.cn&useutf8=1&outputhtmlfeed=1&refer=2"
                f"&r={random.random()}&g_tk={self.g_tk}"
            )

            res = requests.get(url, headers=self.headers, timeout=20)
            status = res.status_code
            text = res.text or ""
            if status != 200 or not text:
                if start == 0:
                    return status, []
                break

            payload = _try_extract_json_from_callback(text)
            data_items: List[Dict[str, Any]] = []
            if isinstance(payload, dict):
                d = payload.get("data")
                if isinstance(d, dict):
                    if isinstance(d.get("data"), dict):
                        d = d.get("data")
                    for k in ("friend_data", "host_data"):
                        arr = d.get(k)
                        if isinstance(arr, list):
                            data_items = [x for x in arr if isinstance(x, dict)]
                            if data_items:
                                break

            extracted_items = 0
            if not data_items:
                data_items = _extract_feed_items_from_js_callback(text)
                extracted_items = len(data_items)

            feed_data_tag_hits = 0
            self_posts = 0

            if data_items:
                try:
                    raw_html = str(data_items[0].get("html") or "")
                    sample = raw_html[:260].replace("\n", " ").replace("\r", " ")
                    self.last_sample_html_head = sample
                except Exception:
                    self.last_sample_html_head = ""

            if not data_items:
                head = (text or "")[:500].replace("\n", " ").replace("\r", " ")
                data_obj = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data_obj, dict) and isinstance(data_obj.get("data"), dict):
                    data_obj = data_obj.get("data")
                keys = []
                if isinstance(data_obj, dict):
                    keys = sorted(list(data_obj.keys()))
                types = {}
                if isinstance(data_obj, dict):
                    for k in ("friend_data", "host_data", "about_data", "firstpage_data"):
                        v = data_obj.get(k)
                        types[k] = type(v).__name__
                raise RuntimeError(
                    "feeds_html_act_all parse failed: no data_items; "
                    f"data_keys={keys}; data_types={types}; head={head}"
                )

            for item in data_items:
                html = str(item.get("html") or "")
                if not html:
                    continue

                tag = ""
                m = re.search(r"<i[^>]*\bname=\"feed_data\"[^>]*>", html)
                if m:
                    tag = m.group(0)

                if not tag:
                    m = re.search(r"<i[^>]*\bname='feed_data'[^>]*>", html)
                    if m:
                        tag = m.group(0)

                if not tag:
                    m = re.search(r"<i[^>]*\bname=\\\"feed_data\\\"[^>]*>", html)
                    if m:
                        tag = m.group(0)

                if not tag:
                    m = re.search(r"<i[^>]*\bname=\\'feed_data\\'[^>]*>", html)
                    if m:
                        tag = m.group(0)

                if not tag:
                    continue

                feed_data_tag_hits += 1

                tid = ""
                host_uin = ""
                topic_id = ""

                for pat, key in (
                    (r"\bdata-tid=\\\"([^\\\"]+)\\\"", "tid"),
                    (r"\bdata-uin=\\\"(\d+)\\\"", "uin"),
                    (r"\bdata-topicid=\\\"([^\\\"]+)\\\"", "topic"),
                    (r"\bdata-tid=\"([^\"]+)\"", "tid"),
                    (r"\bdata-uin=\"(\d+)\"", "uin"),
                    (r"\bdata-topicid=\"([^\"]+)\"", "topic"),
                    (r"\bdata-tid='([^']+)'", "tid"),
                    (r"\bdata-uin='(\d+)'", "uin"),
                    (r"\bdata-topicid='([^']+)'", "topic"),
                ):
                    mm = re.search(pat, tag)
                    if not mm:
                        continue
                    if key == "tid" and not tid:
                        tid = mm.group(1)
                    elif key == "uin" and not host_uin:
                        host_uin = mm.group(1)
                    elif key == "topic" and not topic_id:
                        topic_id = mm.group(1)

                if not tid or not host_uin or not topic_id:
                    continue

                if host_uin != self.my_qq:
                    continue

                self_posts += 1

                if "_" not in topic_id or "__" not in topic_id:
                    continue

                abstime = 0

                # Prefer data-abstime from feed_data tag; it's present in the embedded HTML and avoids JS-literal parsing quirks.
                m_ab = re.search(r"\bdata-abstime=\\\"(\d+)\\\"", tag)
                if not m_ab:
                    m_ab = re.search(r"\bdata-abstime=\"(\d+)\"", tag)
                if not m_ab:
                    m_ab = re.search(r"\bdata-abstime='(\d+)'", tag)
                if m_ab:
                    try:
                        abstime = int(m_ab.group(1))
                    except Exception:
                        abstime = 0

                if not abstime:
                    try:
                        if "abstime" in item:
                            abstime = int(str(item.get("abstime") or 0))
                    except Exception:
                        abstime = 0

                posts.append(MoodPost(host_uin=host_uin, tid=tid, topic_id=topic_id, abstime=abstime))

            start += count

        seen = set()
        out: List[MoodPost] = []
        for p in posts:
            if p.tid in seen:
                continue
            seen.add(p.tid)
            out.append(p)

        try:
            msg = (
                "[Qzone][feed_fetch] "
                f"status=200 extracted_items={extracted_items} feed_data_tag_hits={feed_data_tag_hits} "
                f"self_posts={self_posts} out_posts={len(out)}"
            )
            self.last_diag = msg
        except Exception:
            self.last_diag = ""

        return 200, out
