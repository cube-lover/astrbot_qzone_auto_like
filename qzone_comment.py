# qzone_comment.py
# QQ空间评论/删评（taotao / emotion_cgi_addcomment_ugc, emotion_cgi_delcomment_ugc）

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

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


def _try_extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()

    if t.startswith("{") and t.endswith("}"):
        try:
            return json.loads(t)
        except Exception:
            return None

    m = re.search(r"\b(?:callback|cb)\s*\(\s*(\{.*\})\s*\)\s*;?\s*$", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    m = re.search(r"frameElement\.callback\s*\(\s*(\{.*?\})\s*\)", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    m = re.search(r"\bcb\s*\(\s*(\{.*?\})\s*\)", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    return None


@dataclass
class CommentResult:
    ok: bool
    code: Optional[int]
    message: str
    raw_head: str
    comment_id: str


class QzoneCommenter:
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
            "referer": f"https://user.qzone.qq.com/{self.my_qq}/main",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        }

    def add_comment(self, tid: str, text: str) -> Tuple[int, CommentResult]:
        t = (tid or "").strip()
        content = (text or "").strip()
        if not t:
            return 0, CommentResult(False, None, "empty tid", "", "")
        if not content:
            return 0, CommentResult(False, None, "empty text", "", "")

        url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
            f"/cgi-bin/emotion_cgi_addcomment_ugc?&g_tk={self.g_tk}"
        )

        data: Dict[str, Any] = {
            "hostuin": self.my_qq,
            "tid": t,
            "t1": t,
            "content": content,
            "format": "fs",
            "qzreferrer": f"https://user.qzone.qq.com/{self.my_qq}/main",
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
            cid = str(payload.get("commentid") or payload.get("comment_id") or payload.get("cid") or "")
            ok = code == 0
            return res.status_code, CommentResult(ok, code, msg, head, cid)

        return res.status_code, CommentResult(False, None, "non-json response", head, "")

    def delete_comment(self, tid: str, comment_id: str) -> Tuple[int, CommentResult]:
        t = (tid or "").strip()
        cid = (comment_id or "").strip()
        if not t:
            return 0, CommentResult(False, None, "empty tid", "", "")
        if not cid:
            return 0, CommentResult(False, None, "empty comment_id", "", "")

        url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
            f"/cgi-bin/emotion_cgi_delcomment_ugc?&g_tk={self.g_tk}"
        )

        data: Dict[str, Any] = {
            "hostuin": self.my_qq,
            "tid": t,
            "t1": t,
            "commentid": cid,
            "format": "fs",
            "qzreferrer": f"https://user.qzone.qq.com/{self.my_qq}/main",
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
            ok = code == 0
            return res.status_code, CommentResult(ok, code, msg, head, cid)

        return res.status_code, CommentResult(False, None, "non-json response", head, cid)
