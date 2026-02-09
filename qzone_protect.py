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


def _extract_data_array_from_callback(text: str) -> str:
    """Extract the `data:[ ... ]` array body from a _Callback(...) JS-literal response.

    This endpoint often returns JS object literal (unquoted keys, single quotes, undefined),
    so we cannot rely on json.loads. We only need to locate the `data:[ ... ]` segment
    and return the inner content (without the surrounding brackets).
    """

    if not text:
        return ""

    s = text
    m = re.search(r"\bdata\s*:\s*\[", s)
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


def _iter_html_blobs_from_data_array(arr_body: str, limit: int = 200) -> List[str]:
    """Extract html:'...'</li> blobs from the array body using anchor slicing.

    We slice from `html:` to the next `,opuin:` when present (matches real payloads)
    and decode common escapes.
    """

    if not arr_body:
        return []

    out: List[str] = []
    for m in re.finditer(r"\bhtml\s*:\s*", arr_body):
        tail = arr_body[m.end() :]
        end_m = re.search(r",\s*opuin\s*:\s*", tail)
        if not end_m:
            # fallback: try to end at next `,\s*uin:` (some items may differ)
            end_m = re.search(r",\s*uin\s*:\s*", tail)
        if not end_m:
            continue

        blob = tail[: end_m.start()].strip().rstrip(",")
        if len(blob) >= 2 and blob[0] in ("'", '"') and blob[-1] == blob[0]:
            html = blob[1:-1]
        else:
            html = blob

        html = html.replace("\\x3C", "<").replace("\\x3E", ">")
        html = html.replace("\\/", "/")
        html = html.replace("\\\"", '"').replace("\\'", "'")
        html = html.replace("\\x22", '"')

        out.append(html)
        if limit > 0 and len(out) >= limit:
            break

    return out


def _try_extract_json_from_callback(text: str) -> Optional[dict]:
    # Best-effort strict JSON parse; many responses are JS-literal and will fail.
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
        html_blobs = 0
        topic_hits = 0
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

            # Path A: strict JSON (rare)
            if isinstance(payload, dict):
                data = payload.get("data")
                if not isinstance(data, dict):
                    self.last_errors.append(f"page={pagenum} missing_data")
                    break

                arr = data.get("data")
                if not isinstance(arr, list):
                    head = raw_text[:1200].replace("\n", " ").replace("\r", " ")
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

                    m = re.search(r"<i[^>]*\bname=\\\"feed_data\\\"[^>]*>", html, re.I)
                    tag = m.group(0) if m else ""
                    if not tag:
                        m = re.search(r"<i[^>]*\bname=\"feed_data\"[^>]*>", html, re.I)
                        tag = m.group(0) if m else ""
                    if not tag:
                        m = re.search(r"<i[^>]*\bname='feed_data'[^>]*>", html, re.I)
                        tag = m.group(0) if m else ""

                    tid = ""
                    topic_id = ""
                    if tag:
                        mm = re.search(r"\bdata-tid=\\\"([^\\\"]+)\\\"", tag)
                        if mm:
                            tid = mm.group(1)
                        mm = re.search(r"\bdata-topicid=\\\"([^\\\"]+)\\\"", tag)
                        if mm:
                            topic_id = mm.group(1)

                        if not tid:
                            mm = re.search(r"\bdata-tid=\"([^\"]+)\"", tag)
                            if mm:
                                tid = mm.group(1)
                        if not topic_id:
                            mm = re.search(r"\bdata-topicid=\"([^\"]+)\"", tag)
                            if mm:
                                topic_id = mm.group(1)

                        if not tid:
                            mm = re.search(r"\bdata-tid='([^']+)'", tag)
                            if mm:
                                tid = mm.group(1)
                        if not topic_id:
                            mm = re.search(r"\bdata-topicid='([^']+)'", tag)
                            if mm:
                                topic_id = mm.group(1)

                    if not tid or not topic_id:
                        continue

                    topic_hits += 1

                    abstime = 0
                    try:
                        if "abstime" in item:
                            abstime = int(str(item.get("abstime") or 0))
                    except Exception:
                        abstime = 0

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

                continue

            # Path B: JS-literal callback (common)
            head = raw_text[:1200].replace("\n", " ").replace("\r", " ")
            if "<!DOCTYPE html" in raw_text[:2000] or "<html" in raw_text[:2000]:
                self.last_errors.append(f"page={pagenum} invalid_payload html_page head={head}")
                if pagenum == 1:
                    return res.status_code, []
                break

            arr_body = _extract_data_array_from_callback(raw_text)
            if not arr_body:
                self.last_errors.append(f"page={pagenum} js_literal data_array_not_found head={head}")
                if pagenum == 1:
                    return res.status_code, []
                break

            html_list = _iter_html_blobs_from_data_array(arr_body, limit=200)
            html_blobs += len(html_list)
            for html in html_list:
                html_items += 1

                m = re.search(
                    r"<i[^>]*\bname=\\\"feed_data\\\"[^>]*>",
                    html,
                    re.I,
                )
                tag = m.group(0) if m else ""
                if not tag:
                    m = re.search(r"<i[^>]*\bname=\"feed_data\"[^>]*>", html, re.I)
                    tag = m.group(0) if m else ""
                if not tag:
                    m = re.search(r"<i[^>]*\bname='feed_data'[^>]*>", html, re.I)
                    tag = m.group(0) if m else ""

                tid = ""
                topic_id = ""

                if tag:
                    mm = re.search(r"\bdata-tid=\\\"([^\\\"]+)\\\"", tag)
                    if mm:
                        tid = mm.group(1)
                    mm = re.search(r"\bdata-topicid=\\\"([^\\\"]+)\\\"", tag)
                    if mm:
                        topic_id = mm.group(1)

                    if not tid:
                        mm = re.search(r"\bdata-tid=\"([^\"]+)\"", tag)
                        if mm:
                            tid = mm.group(1)
                    if not topic_id:
                        mm = re.search(r"\bdata-topicid=\"([^\"]+)\"", tag)
                        if mm:
                            topic_id = mm.group(1)

                    if not tid:
                        mm = re.search(r"\bdata-tid='([^']+)'", tag)
                        if mm:
                            tid = mm.group(1)
                    if not topic_id:
                        mm = re.search(r"\bdata-topicid='([^']+)'", tag)
                        if mm:
                            topic_id = mm.group(1)

                if not tid or not topic_id:
                    continue

                topic_hits += 1

                abstime = 0
                m_ab = re.search(r"\babstime\s*:\s*'?([0-9]{6,})'?", arr_body[m.start() : m.start() + 2000])
                if m_ab:
                    try:
                        abstime = int(m_ab.group(1))
                    except Exception:
                        abstime = 0

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
                f"pages={pages} count={count} feeds_items={feeds_items} html_items={html_items} html_blobs={html_blobs} "
                f"topic_hits={topic_hits} comment_hits={comment_hits} out={len(out)} errors={len(self.last_errors)}"
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
