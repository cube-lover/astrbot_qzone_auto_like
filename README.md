# QQ空间秒赞插件

AstrBot 插件：自动侦测并点赞 QQ 空间动态（强后台日志版）。

> 安全提示：本插件需要你自己提供 QQ 空间 Cookie（登录态）。请勿把 Cookie 提交到仓库或发给他人。

## 功能

- 后台轮询抓取 QQ 空间 feeds
- 发现新的 mood 动态后自动点赞
- 支持手动指令：输入一次命令立即执行一轮点赞（不必等待轮询）
- 在 AstrBot 后台日志输出详细调试信息（便于定位：cookie失效/风控/验证码等）
- 去重：可将已处理记录保存到 `data/liked_records.json`
- WebUI 配置开关：`enabled` / `auto_start`
- 可选配置默认目标空间：`target_qq`
- 保留命令控制：`/qz_start`、`/qz_stop`、`/qz_status`、`/点赞`

## 效果截图

按顺序：

![success_1](success_1.png)

![success_2](success_2.png)

## 交流群

不懂的进 QQ 交流群：`460973561`。

进群前请先给本仓库点 `Star`，不点 `Star` 不给进。

## 安装

1. 将整个插件目录放入 AstrBot 插件目录（目录名建议：`qzone_auto_like`）
2. 重启或重载 AstrBot
3. 打开 WebUI 插件配置，填写 `my_qq` 和 `cookie`

## 配置项（WebUI）

必填：
- `my_qq`：空间所属 QQ 号
- `cookie`：QQ 空间 Cookie（必须包含 `p_skey=...`）

### 如何获取 Cookie（Chrome / Edge）

1. 浏览器打开并登录 QQ 空间：`https://user.qzone.qq.com/<你的QQ号>`
2. 按 `F12` 打开开发者工具，进入 `Network(网络)`
3. 刷新页面（`F5`），在请求列表里找到 `feeds_html_act_all`（或任意 `user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/...` 请求）
4. 点开该请求 → `Headers(标头)` → `Request Headers(请求标头)` → 复制 `cookie: ...` 的整行内容
5. 粘贴到本插件配置里的 `cookie`

示例图：

![cookie_example](cookie_example.png)

提示：
- Cookie 属于登录态，千万不要发到群里/仓库/截图里
- 失效后重新按以上步骤复制最新 Cookie

开关：
- `enabled`：总开关（相当于按钮）
- `auto_start`：Bot 启动完成后，若 enabled=true 则自动启动后台任务

调参：
- `poll_interval_sec`：轮询间隔（秒）
- `like_delay_min_sec` / `like_delay_max_sec`：点赞前随机延迟范围
- `max_feeds_count`：每次拉取动态数量
- `persist_liked`：是否持久化去重记录

可选：
- `target_qq`：默认目标QQ空间（留空=自己的空间；也可用 `/点赞 ...` 临时切换并立即执行）

## 命令

- `/qz_start`：启动后台任务（同时会把 enabled 置为 true）
- `/qz_stop`：停止后台任务（同时会把 enabled 置为 false）
- `/qz_status`：查看运行状态、enabled/auto_start、目标空间、缓存数量
- `/点赞 @某人 [次数]`：立即点赞对方空间的动态（默认强制：忽略历史去重记录；默认 10，上限 100）
- `/点赞 QQ号 [次数]`：立即点赞指定 QQ 空间的动态（默认强制：忽略历史去重记录；默认 10，上限 100）
- `/点赞 @某人 [次数] noforce`：关闭强制（恢复按 liked_cache 去重）
- `/点赞 QQ号 [次数] noforce`：关闭强制（恢复按 liked_cache 去重）
- `/qz_clear_liked`：清空已点赞去重记录（liked_cache），用于“重新跑一遍”

提示：如果目标空间拉取失败，后台日志可能出现 `need login`，通常是 Cookie 不完整/失效，或触发风控/验证。

## 后台日志说明

你会看到类似日志：

- `[HH:MM:SS] 正在侦测...`
- `[Qzone] feeds 返回 | status=... text_len=... keys=...`
- `[Qzone] like 返回 | status=... resp_head=...`

如果 `feeds status != 200` 或 `like 返回内容不是 "code":0`，通常是：
- Cookie 失效
- 触发风控/验证码
- 频率过高

## 开源许可

MIT License
