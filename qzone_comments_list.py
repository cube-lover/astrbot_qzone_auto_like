# qzone_comments_list.py
# QQ空间评论列表抓取（基于 infocenter feeds 的 Callback 数据里内嵌的 comments-list HTML）

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from .qzone_comment import _get_gtk, _pick_skey_for_gtk


@dataclass
class CommentItem:
    comment_id: str
    comment_uin: str
    nick: str
    content: str


class QzoneCommentLister:
    def __init__(self, my_qq: str, cookie: str):
        self.my_qq = str(my_qq).strip()
        cookie = (cookie or "").strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        self.cookie = cookie

        skey = _pick_skey_for_gtk(cookie)
        if not skey:
            raise ValueError("cookie 缺少 p_skey/skey/media_p_skey（无法计算 g_tk）")
        self.g_tk = _get_gtk(skey)

        self.headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            ),
            "cookie": cookie,
            "origin": "https://user.qzone.qq.com",
            "referer": f"https://user.qzone.qq.com/{self.my_qq}/infocenter?via=toolbar",
        }

    def list_comments_from_infocenter_callback(self, topic_id: str, max_items: int = 50) -> Tuple[int, List[CommentItem]]:
        """Fetch infocenter feeds and parse comments from embedded HTML.

        This is a pragmatic approach: the infocenter API response contains a huge HTML snippet
        for each feed; within it there's <li class="comments-item" ... data-tid="..." data-uin="...">.
        We parse those attributes as comment id/uin.
        """

        tid = (topic_id or "").strip()
        if not tid:
            return 0, []

        url = "https://h5.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
        params = {
            "uin": self.my_qq,
            "scope": "0",
            "view": "1",
            "flag": "1",
            "refresh": "1",
            "count": "20",
            "g_tk": str(self.g_tk),
        }

        res = requests.get(url, headers=self.headers, params=params, timeout=20)
        text = res.text or ""

        # Find the block that contains our topicId
        pos = text.find(tid)
        if pos < 0:
            return res.status_code, []

        # Slice around to limit regex cost
        seg = text[max(0, pos - 20000) : pos + 20000]

        items: List[CommentItem] = []
        # comments root items contain: data-tid="<commentId>" data-uin="<uin>" data-nick="..."
        for m in re.finditer(
            r"comments-item[^>]*data-type=\\\"commentroot\\\"[^>]*data-tid=\\\"(\d+)\\\"[^>]*data-uin=\\\"(\d+)\\\"[^>]*data-nick=\\\"([^\\\"]*)\\\"",
            seg,
            re.I,
        ):
            cid, uin, nick = m.group(1), m.group(2), m.group(3)
            items.append(CommentItem(comment_id=cid, comment_uin=uin, nick=nick, content=""))
            if max_items > 0 and len(items) >= max_items:
                break

        return res.status_code, items
