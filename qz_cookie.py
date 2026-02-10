import time
from typing import Any, Optional

from astrbot.api import logger


def _extract_cookie_value(cookie: str, key: str) -> str:
    if not cookie:
        return ""
    for item in cookie.split(";"):
        item = item.strip()
        if item.startswith(key + "="):
            return item.split("=", 1)[1]
    return ""


def sanitize_cookie_for_log(cookie_str: str) -> str:
    if not cookie_str:
        return ""
    has_p_skey = bool(_extract_cookie_value(cookie_str, "p_skey"))
    has_uin = bool(_extract_cookie_value(cookie_str, "uin"))
    return f"<cookie:redacted has_uin={has_uin} has_p_skey={has_p_skey}>"


def maybe_cookie_invalid(status: int, code: Optional[int], msg: str, head: str) -> bool:
    if status in (401, 403):
        return True
    m = (msg or "").lower()
    h = (head or "").lower()
    for kw in ("need login", "login", "cookie", "skey", "p_skey", "verify", "\u9a8c\u8bc1\u7801", "\u8bf7\u767b\u5f55"):
        if kw in m or kw in h:
            return True
    if "<html" in h and ("login" in h or "verify" in h):
        return True
    try:
        if code is not None and int(code) in (3000, 3001, 4001):
            return True
    except Exception:
        pass
    return False


class QzCookieAutoFetcher:
    """Best-effort cookie auto fetcher for AIOCQHTTP/Napcat.

    Captures `event.bot` (CQHttp client) and calls `get_cookies(domain=...)` to obtain
    current cookies for Qzone.

    This module is intentionally small and isolated so it can be disabled without affecting
    other features.
    """

    DOMAIN = "user.qzone.qq.com"

    def __init__(self, enabled: bool = False, on_fail: bool = True, cooldown_sec: int = 120):
        self.enabled = bool(enabled)
        self.on_fail = bool(on_fail)
        self.cooldown_sec = max(5, int(cooldown_sec or 120))
        self._client: Any = None
        self._last_fetch_ts = 0.0

    def capture_bot(self, event: Any) -> None:
        if self._client is not None:
            return
        if event is None:
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        self._client = bot

    async def refresh(self, *, reason: str = "") -> Optional[str]:
        if not self.enabled or not self._client:
            return None

        now = time.time()
        if now - float(self._last_fetch_ts or 0.0) < float(self.cooldown_sec):
            return None

        self._last_fetch_ts = now
        try:
            resp = await self._client.get_cookies(domain=self.DOMAIN)
            cookies_str = ""
            if isinstance(resp, dict):
                cookies_str = str(resp.get("cookies") or "").strip()
            if not cookies_str:
                logger.warning(f"[Qzone] auto cookie fetch failed: empty cookies (reason={reason})")
                return None
            logger.info(f"[Qzone] auto cookie fetched ok (reason={reason})")
            return cookies_str
        except Exception as e:
            logger.warning(f"[Qzone] auto cookie fetch exception (reason={reason}): {e}")
            return None
