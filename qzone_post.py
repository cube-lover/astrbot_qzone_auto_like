# qzone_post.py
# QQ空间发说说（taotao / emotion_cgi_publish_v6）

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests


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


def _try_extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()

    # raw JSON
    if t.startswith("{") and t.endswith("}"):
        try:
            return json.loads(t)
        except Exception:
            return None

    # callback({...}) / cb({...})
    m = re.search(r"\b(?:callback|cb)\s*\(\s*(\{.*\})\s*\)\s*;?\s*$", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    # HTML wrapper: frameElement.callback({...})
    m = re.search(r"frameElement\.callback\s*\(\s*(\{.*?\})\s*\)", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    # HTML wrapper: cb=frameElement.callback; ... cb({...})
    m = re.search(r"\bcb\s*\(\s*(\{.*?\})\s*\)", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    return None


@dataclass
class PublishResult:
    ok: bool
    code: Optional[int]
    message: str
    raw_head: str
    tid: str


class QzonePoster:
    def __init__(self, my_qq: str, cookie: str):
        # Supports publish + delete.
        self.my_qq = str(my_qq).strip()

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
            "origin": "https://user.qzone.qq.com",
            "referer": f"https://user.qzone.qq.com/{self.my_qq}",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        }

    def publish_text(self, content: str) -> Tuple[int, PublishResult]:
        """Publish plain-text mood."""
        text = (content or "").strip()
        if not text:
            return 0, PublishResult(False, None, "empty content", "", "")

        url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
            f"/cgi-bin/emotion_cgi_publish_v6?&g_tk={self.g_tk}"
        )

        data: Dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": self.my_qq,
            "code_version": "1",
            "format": "fs",
            "qzreferrer": f"https://user.qzone.qq.com/{self.my_qq}",
        }

        data["rand"] = str(int(time.time() * 1000)) + str(random.randint(100, 999))

        res = requests.post(url, headers=self.headers, data=data, timeout=20)
        head = (res.text or "")[:300].replace("\n", " ").replace("\r", " ")

        payload = _try_extract_json(res.text or "")
        if isinstance(payload, dict):
            code = None
            try:
                if "code" in payload:
                    code = int(payload.get("code"))
            except Exception:
                code = None
            msg = str(payload.get("message") or payload.get("msg") or "")
            tid = str(payload.get("tid") or payload.get("t1") or payload.get("feedid") or "")
            ok = code == 0
            return res.status_code, PublishResult(ok, code, msg, head, tid)

        return res.status_code, PublishResult(False, None, "non-json response", head, "")

    def delete_by_tid(self, tid: str) -> Tuple[int, PublishResult]:
        """Delete a mood by tid.

        Note: we rely on the tid returned by publish.
        """
        t = (tid or "").strip()
        if not t:
            return 0, PublishResult(False, None, "empty tid", "", "")

        url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
            f"/cgi-bin/emotion_cgi_delete_v6?&g_tk={self.g_tk}"
        )

        data: Dict[str, Any] = {
            "hostuin": self.my_qq,
            "tid": t,
            "t1": t,
            "format": "fs",
            "qzreferrer": f"https://user.qzone.qq.com/{self.my_qq}",
        }

        res = requests.post(url, headers=self.headers, data=data, timeout=20)
        head = (res.text or "")[:300].replace("\n", " ").replace("\r", " ")

        payload = _try_extract_json(res.text or "")
        if isinstance(payload, dict):
            code = None
            try:
                if "code" in payload:
                    code = int(payload.get("code"))
            except Exception:
                code = None
            msg = str(payload.get("message") or payload.get("msg") or "")
            ok = code == 0
            return res.status_code, PublishResult(ok, code, msg, head, t)

        return res.status_code, PublishResult(False, None, "non-json response", head, t)
