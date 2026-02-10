import time
from typing import Any, Optional

from astrbot.api import logger


class QzCookieAutoFetcher:
    """Auto fetch cookies for user.qzone.qq.com from Napcat (AIOCQHTTP).

    This class is intentionally isolated. It never throws and never logs full cookies.
    """

    DOMAIN = "user.qzone.qq.com"

    def __init__(self, enabled: bool = False, cooldown_sec: int = 120):
        self.enabled = bool(enabled)
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
