# astrbot_qzone_auto_like

AstrBot 插件：自动侦测并点赞 QQ 空间动态（强后台日志版）。

> 安全提示：本插件需要你自己提供 QQ 空间 Cookie（登录态）。请勿把 Cookie 提交到仓库或发给他人。

## 功能

- 后台轮询抓取 QQ 空间 feeds
- 发现新的 mood 动态后自动点赞
- 在 AstrBot 后台日志输出详细调试信息（便于定位：cookie失效/风控/验证码等）
- 去重：可将已处理记录保存到 `data/liked_records.json`
- WebUI 配置开关：`enabled` / `auto_start`
- 保留命令控制：`/qz_start`、`/qz_stop`、`/qz_status`

## 安装

1. 将整个插件目录放入 AstrBot 插件目录（目录名建议：`qzone_auto_like`）
2. 重启或重载 AstrBot
3. 打开 WebUI 插件配置，填写 `my_qq` 和 `cookie`

## 配置项（WebUI）

必填：
- `my_qq`：空间所属 QQ 号
- `cookie`：QQ 空间 Cookie（必须包含 `p_skey=...`）

开关：
- `enabled`：总开关（相当于按钮）
- `auto_start`：Bot 启动完成后，若 enabled=true 则自动启动后台任务

调参：
- `poll_interval_sec`：轮询间隔（秒）
- `like_delay_min_sec` / `like_delay_max_sec`：点赞前随机延迟范围
- `max_feeds_count`：每次拉取动态数量
- `persist_liked`：是否持久化去重记录

## 命令

- `/qz_start`：启动后台任务（同时会把 enabled 置为 true）
- `/qz_stop`：停止后台任务（同时会把 enabled 置为 false）
- `/qz_status`：查看运行状态、enabled/auto_start、缓存数量

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
