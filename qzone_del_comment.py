# qzone_del_comment.py
# QQ空间删评（taotao / emotion_cgi_delcomment_ugc）

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

from .qzone_comment import (
    _extract_cookie_value,
    _get_gtk,
    _pick_skey_for_gtk,
    _try_extract_json,
)


@dataclass
class DelCommentResult:
    ok: bool
    code: Optional[int]
    message: str
    raw_head: str


class QzoneCommentDeleter:
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
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        }

    def delete_comment(self, topic_id: str, comment_id: str, comment_uin: str = "") -> Tuple[int, DelCommentResult]:
        topic = (topic_id or "").strip()
        cid = (comment_id or "").strip()
        if not topic:
            return 0, DelCommentResult(False, None, "empty topicId", "")
        if not cid:
            return 0, DelCommentResult(False, None, "empty commentId", "")

        # commentUin is required by browser payload; default to self uin if missing.
        cuin = (comment_uin or "").strip()
        if not cuin:
            cuin = self.my_qq
        # Accept both 'o123' and '123'
        if cuin.startswith("o") and cuin[1:].isdigit():
            cuin = cuin[1:]

        url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
            f"/cgi-bin/emotion_cgi_delcomment_ugc?&g_tk={self.g_tk}"
        )

        data = {
            "g_tk": str(self.g_tk),
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "hostUin": self.my_qq,
            "uin": self.my_qq,
            "topicId": topic,
            "feedsType": "100",
            "commentId": cid,
            "commentUin": cuin,
            "format": "fs",
            "ref": "feeds",
            "paramstr": "1",
            "qzreferrer": f"https://user.qzone.qq.com/{self.my_qq}/infocenter?via=toolbar",
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
            return res.status_code, DelCommentResult(ok, code, msg, head)

        return res.status_code, DelCommentResult(False, None, "non-json response", head)
