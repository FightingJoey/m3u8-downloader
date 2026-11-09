# Pornhub 视频页 → m3u8-downloader

在原版 [m3u8-downloader](https://github.com/Momo707577045/m3u8-downloader) 基础上，同时支持两种输入：

1. **URL 含 `m3u8`**：当作播放列表，走原前端直接解析 / 分片下载（不调 `/api/resolve`）
2. **URL 不含 `m3u8`**：当作 Pornhub 视频页，后端用 `download_ph_1080.py` 抓 **1080P `index-v1-a1.m3u8`**，再走原分片下载 + `mux-mp4.js`；合成后的 MP4 **用原视频标题命名**

## 怎么跑

```bash
cd m3u8-downloader
python3 ph_server.py
```

浏览器打开：<http://127.0.0.1:8765/>

1. 粘贴链接（二选一）：
   - 视频页：`https://cn.pornhub.com/view_video.php?viewkey=XXXX`（URL 中无 `m3u8`）
   - 或直接粘贴 `.m3u8` / 含 `m3u8` 的播放列表 URL
2. 点 **「转码为MP4下载」**（或「原格式下载」）
3. 视频页模式：等待「已取到 1080P 播放列表」，分片下载后合成；MP4 文件名优先用页面原标题（`og:title` 等），缺省才用 viewkey

## 原理

| 步骤 | 谁做 |
|------|------|
| 打开视频页、选 1080P `*index-v1-a1.m3u8` | `download_ph_1080.py`（`http_find_1080` / `resolve_1080_media_playlist`） |
| 把分片 URL 改写为同源 `/proxy` | `ph_server.py` |
| 解析 m3u8、多线程下分片、AES、mux 成 MP4 | 原版 `index.html` + `mux-mp4.js` 等 |

API：

- `GET/POST /api/resolve?url=<Pornhub页>` → `{ ok, playlist_url, media_url, segment_count, title, viewkey, ... }`
- `GET /proxy?job=…&u=…` → 带浏览器级 UA/Cookie/Referer 拉 CDN 分片
- `GET /cache/<job>_index-v1-a1.m3u8` → 改写后的播放列表

## 依赖

- Python 3.10+（标准库即可；取链不强制 Playwright）
- 本机浏览器访问 `127.0.0.1:8765`

可选：系统安装 `ffmpeg` 仅在你单独跑 `download_ph_1080.py` CLI 时需要；网页合成走浏览器里的 `mux-mp4.js`。

## 文件

- `ph_server.py` — 静态站 + 取链 API + CDN 代理
- `download_ph_1080.py` — Pornhub 1080P 取链（从工作区拷入）
- `index.html` — 按 URL 是否含 `m3u8` 分流；视频页走 `/api/resolve`；MP4 用 API 返回的 `title` 命名
- `cache/` — 解析得到的原始/改写 m3u8（本地缓存）

## 注意

- 视频下架、无 1080、地区限制时，`/api/resolve` 会返回错误文案。
- 任务约 1 小时过期；过期后重新点解析即可。
- 请勿把本改动推送到上游仓库，除非作者要求。

## 已修复：解析成功但全部分片失败

**根因**：`/api/resolve` 返回的 `playlist_url` 是相对路径（`/cache/…m3u8`）。原版 `applyURL` 在处理以 `/` 开头的分片时，用 `baseURL.split('/')` 取「主机名」，把文件名当成了主机，拼出 `//xxx_index-v1-a1.m3u8/proxy?…`，浏览器请求错误主机，因而全部失败。

**修复**：
1. `index.html`：`applyURL` 改用 `new URL(target, base)`；解析成功后把 playlist 转成绝对 URL。
2. `ph_server.py`：`/proxy` 对 `u` 只 unquote 一次（避免破坏 CDN 签名里的 `%3D`）；按 URL path 后缀设 Content-Type（不再因路径含 `.mp4/` 误判）；`/cache/*.m3u8` 显式带 CORS。

## 播放列表格式说明

Pornhub 页面里的 `mediaDefinitions` 会在两种签名间轮换：

1. **正确（本工具优先）**：`ev-h.phncdn.com` + `seg-N-v1-a1.ts?validfrom=...&hash=...`（MPEG-TS）
2. **错误/不兼容**：`hv-h.phncdn.com` + `?h=...&e=...&f=1`，展开后常为 `seg-*.m4s` + `#EXT-X-MAP`（fMP4）

`download_ph_1080.py` / `ph_server.py` 会：
- 解析 `mediaDefinitions`，**优先选 validfrom+hash**
- 若当次页面只有 h/e，**自动重试拉页**直到拿到 validfrom
- 解析后校验播放列表正文；拒绝在仍有更好候选时使用 fMP4 / h-e 变体
- 改写给前端的 `/proxy` URL 完整保留 query（含 `%2F` / `%3D`），避免签名损坏

## Docker

```bash
# 构建并启动（默认 http://127.0.0.1:8765/）
docker compose up -d --build

# 或不用 Compose
docker build -t m3u8-downloader-ph .
docker run --rm -p 8765:8765 m3u8-downloader-ph
```

换端口：`PH_PORT=9000 docker compose up -d --build`（映射到宿主机 9000）。

说明：镜像只含 Python 标准库桥接服务，**未**安装 Playwright/Chromium；取链走 HTTP。公开暴露前请自行加反向代理鉴权，勿当作开放下载站。

