# qzone_nl_router.py
# Natural language router for Qzone plugin commands.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class NLRoute:
    action: str
    n: int = 1


_NUM_MAP = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _parse_int(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return None
    if s in _NUM_MAP:
        return _NUM_MAP[s]
    return None


def route_nl(text: str) -> Optional[NLRoute]:
    """Route natural language to an internal action.

    Only intended for messages that already start with the Chinese comma prefix: '，'.
    """

    t = (text or "").strip()
    if not t.startswith("，"):
        return None

    body = t[1:].strip()
    if not body:
        return None

    # Delete latest comment
    if re.search(r"(删除|删)(刚刚|刚才|上条|上一条).{0,6}评论", body):
        return NLRoute(action="del_comment", n=1)

    # Comment: first/second... (第N条)
    if "评论" in body:
        m = re.search(r"第\s*([0-9]+|[一二两三四五六七八九十])\s*条", body)
        if m:
            n = _parse_int(m.group(1))
            if n and n > 0:
                return NLRoute(action="comment", n=n)

        # Comment: first/second... (第一条/第二条)
        m = re.search(r"([0-9]+|[一二两三四五六七八九十])\s*条", body)
        if m:
            n = _parse_int(m.group(1))
            if n and n > 0:
                return NLRoute(action="comment", n=n)

        # Default: comment latest
        return NLRoute(action="comment", n=1)

    return None
