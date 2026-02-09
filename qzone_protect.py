# qzone_protect.py
# QQ空间“护评”后台轮询（基于 feeds3_html_more 回包内嵌 HTML 提取评论ID）

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


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

    m = re.search(r"_Callback\s*\(\s*(\{.*\})\s*\)\s*;?\s*$", t, re.S)
    if m:
        body = m.group(1)
        try:
            return json.loads(body)
        except Exception:
            return None

    m = re.search(r"frameElement\.callback\s*\(\s*(\{.*?\})\s*\)", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    return None


@dataclass
class FeedCommentRef:
    topic_id: str
    tid: str
    abstime: int
    comment_id: str
    comment_uin: str


class QzoneProtectScanner:
    def __init__(self, my_qq: str, cookie: str):
        self.my_qq = str(my_qq).strip()
        self.last_diag: str = ""
        self.last_errors: list[str] = []

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

    def scan_recent_comments(self, pages: int = 2, count: int = 10) -> Tuple[int, List[FeedCommentRef]]:
        out: List[FeedCommentRef] = []
        self.last_diag = ""
        self.last_errors = []
        feeds_items = 0
        html_items = 0
        comment_hits = 0
        pages = int(pages) if pages else 1
        if pages <= 0:
            pages = 1
        count = int(count) if count else 10
        if count <= 0:
            count = 10

        for pagenum in range(1, pages + 1):
            url = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
            params = {
                "uin": self.my_qq,
                "scope": "0",
                "view": "1",
                "flag": "1",
                "filter": "all",
                "applist": "all",
                "refresh": "0",
                "pagenum": str(pagenum),
                "count": str(count),
                "useutf8": "1",
                "outputhtmlfeed": "1",
                "g_tk": str(self.g_tk),
            }
            res = requests.get(url, headers=self.headers, params=params, timeout=20)
            if res.status_code != 200:
                if pagenum == 1:
                    return res.status_code, []
                break

            raw_text = res.text or ""
            payload = _try_extract_json_from_callback(raw_text)
            if not isinstance(payload, dict):
                head = raw_text[:260].replace("\n", " ").replace("\r", " ")
                # Detect common cases quickly.
                if "<!DOCTYPE html" in raw_text[:2000] or "<html" in raw_text[:2000]:
                    self.last_errors.append(f"page={pagenum} invalid_payload html_page head={head}")
                else:
                    self.last_errors.append(f"page={pagenum} invalid_payload js_literal_or_other head={head}")
                if pagenum == 1:
                    return res.status_code, []
                break

            data = payload.get("data")
            if not isinstance(data, dict):
                self.last_errors.append(f"page={pagenum} missing_data")
                break

            arr = data.get("data")
            if not isinstance(arr, list):
                # Keep a short head for debugging. This often changes by account/region.
                head = raw_text[:260].replace("\n", " ").replace("\r", " ")
                self.last_errors.append(f"page={pagenum} data.data_not_list type={type(arr).__name__} head={head}")
                break

            feeds_items += len(arr)

            for item in arr:
                if not isinstance(item, dict):
                    continue

                html = str(item.get("html") or "")
                if not html:
                    continue
                html_items += 1

                m = re.search(
                    r"name=\\\"feed_data\\\"[^>]*\bdata-tid=\\\"([^\\\"]+)\\\"[^>]*\bdata-topicid=\\\"([^\\\"]+)\\\"",
                    html,
                )
                if not m:
                    continue

                tid = m.group(1)
                topic_id = m.group(2)

                abstime = 0
                try:
                    if "abstime" in item:
                        abstime = int(str(item.get("abstime") or 0))
                except Exception:
                    abstime = 0

                # comments root items
                for cm in re.finditer(
                    r"comments-item[^>]*data-type=\\\"commentroot\\\"[^>]*data-tid=\\\"(\d+)\\\"[^>]*data-uin=\\\"(\d+)\\\"",
                    html,
                    re.I,
                ):
                    cid = cm.group(1)
                    cuin = cm.group(2)
                    comment_hits += 1
                    out.append(
                        FeedCommentRef(
                            topic_id=topic_id,
                            tid=tid,
                            abstime=abstime,
                            comment_id=cid,
                            comment_uin=cuin,
                        )
                    )

        try:
            self.last_diag = (
                "[Qzone][protect_scan] "
                f"pages={pages} count={count} feeds_items={feeds_items} html_items={html_items} "
                f"comment_hits={comment_hits} out={len(out)} errors={len(self.last_errors)}"
            )
        except Exception:
            self.last_diag = ""

        return 200, out

    @staticmethod
    def filter_within_window(items: List[FeedCommentRef], window_minutes: int) -> List[FeedCommentRef]:
        if window_minutes <= 0:
            return items
        now = int(time.time())
        win = int(window_minutes) * 60
        out: List[FeedCommentRef] = []
        for it in items:
            if it.abstime <= 0:
                continue
            if now - int(it.abstime) <= win:
                out.append(it)
        return out
