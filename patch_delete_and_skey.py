from __future__ import annotations

from pathlib import Path


def main() -> None:
    p = Path(__file__).with_name("main.py")
    s = p.read_text(encoding="utf-8")

    if '@filter.command("删除")' not in s:
        post_marker = '@filter.command("post")'
        post_idx = s.find(post_marker)
        if post_idx == -1:
            raise SystemExit("cannot find post command")
        next_dec = s.find("\n    @filter.", post_idx + len(post_marker))
        if next_dec == -1:
            raise SystemExit("cannot find insertion point")

        chunk = (
            "\n\n"
            "    @filter.command(\"删除\")\n"
            "    async def delete(self, event: AstrMessageEvent):\n"
            "        \"\"\"删除一条说说。\n\n"
            "        用法：/删除 tid\n"
            "        \"\"\"\n"
            "        text = (event.message_str or \"\").strip()\n"
            "        for prefix in (\"/删除\", \"删除\"):\n"
            "            if text.startswith(prefix):\n"
            "                text = text[len(prefix):].strip()\n"
            "                break\n\n"
            "        tid = (text or \"\").strip()\n"
            "        if not tid:\n"
            "            yield event.plain_result(\"用法：/删除 tid（tid 可从 /post 成功回显里复制）\")\n"
            "            return\n\n"
            "        if not self.my_qq or not self.cookie:\n"
            "            yield event.plain_result(\"配置缺失：my_qq 或 cookie 为空\")\n"
            "            return\n\n"
            "        try:\n"
            "            poster = QzonePoster(self.my_qq, self.cookie)\n"
            "            status, result = await asyncio.to_thread(poster.delete_by_tid, tid)\n"
            "            logger.info(\n"
            "                \"[Qzone] delete 返回 | status=%s ok=%s code=%s msg=%s head=%s\",\n"
            "                status,\n"
            "                result.ok,\n"
            "                result.code,\n"
            "                result.message,\n"
            "                result.raw_head,\n"
            "            )\n\n"
            "            if status == 200 and result.ok:\n"
            "                yield event.plain_result(f\"✅ 已删除说说 tid={tid}\")\n"
            "            else:\n"
            "                hint = result.message or \"删除失败（可能 cookie/风控/验证码/权限）\"\n"
            "                yield event.plain_result(f\"❌ 删除失败：status={status} code={result.code} msg={hint}\")\n"
            "        except Exception as e:\n"
            "            logger.error(f\"[Qzone] 删除说说异常: {e}\")\n"
            "            logger.error(traceback.format_exc())\n"
            "            yield event.plain_result(f\"❌ 异常：{e}\")\n\n"
            "    @filter.llm_tool(name=\"qz_delete\")\n"
            "    async def llm_tool_qz_delete(self, event: AstrMessageEvent, tid: str, confirm: bool = False):\n"
            "        \"\"\"删除QQ空间说说。\n\n"
            "        Args:\n"
            "            tid(string): 说说的 tid\n"
            "            confirm(boolean): 是否确认直接删除；false 时只返回待删除的 tid\n"
            "        \"\"\"\n"
            "        t = (tid or \"\").strip()\n"
            "        if not t:\n"
            "            yield event.plain_result(\"tid 为空\")\n"
            "            return\n\n"
            "        if not confirm:\n"
            "            yield event.plain_result(f\"待删除（未执行）：tid={t}\")\n"
            "            return\n\n"
            "        if not self.my_qq or not self.cookie:\n"
            "            yield event.plain_result(\"配置缺失：my_qq 或 cookie 为空\")\n"
            "            return\n\n"
            "        try:\n"
            "            poster = QzonePoster(self.my_qq, self.cookie)\n"
            "            status, result = await asyncio.to_thread(poster.delete_by_tid, t)\n"
            "            logger.info(\n"
            "                \"[Qzone] llm_tool delete 返回 | status=%s ok=%s code=%s msg=%s head=%s\",\n"
            "                status,\n"
            "                result.ok,\n"
            "                result.code,\n"
            "                result.message,\n"
            "                result.raw_head,\n"
            "            )\n"
            "            if status == 200 and result.ok:\n"
            "                yield event.plain_result(f\"✅ 已删除说说 tid={t}\")\n"
            "            else:\n"
            "                hint = result.message or \"删除失败（可能 cookie/风控/验证码/权限）\"\n"
            "                yield event.plain_result(f\"❌ 删除失败：status={status} code={result.code} msg={hint}\")\n"
            "        except Exception as e:\n"
            "            logger.error(f\"[Qzone] llm_tool 删除说说异常: {e}\")\n"
            "            logger.error(traceback.format_exc())\n"
            "            yield event.plain_result(f\"❌ 异常：{e}\")\n"
        )

        s = s[:next_dec] + chunk + s[next_dec:]

    # Attach qz_delete tool to llm request
    hook = "ts.add_tool(tool)"
    if hook in s and 'get_tool("qz_delete")' not in s:
        s = s.replace(hook, hook + "\n            ts.add_tool(mgr.get_tool(\"qz_delete\"))")

    # Make /post success show tid (first occurrence only)
    needle = 'yield event.plain_result("✅ 已发送说说")'
    if needle in s:
        rep = (
            'tid_info = f" tid={result.tid}" if getattr(result, "tid", "") else ""\n'
            '                yield event.plain_result(f"✅ 已发送说说{tid_info}")'
        )
        s = s.replace(needle, rep, 1)

    p.write_text(s, encoding="utf-8")


if __name__ == "__main__":
    main()
