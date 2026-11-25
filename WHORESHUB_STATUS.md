# whoreshub.com 集成状态

## 当前状态

**阶段**: 框架准备完成，等待站点分析证据

**分支**: `cursor/freepornvideos-mp4-download-ab54`（与 freepornvideos 同一 PR）

**已完成**:
- ✅ 占位符模块 (`download_wh.py`)
- ✅ SSRF 防护框架（白名单验证）
- ✅ URL 检测框架（基于假设 `/videos/数字/` 模式）
- ✅ 标题提取通用逻辑
- ✅ 基础测试框架（13 个测试通过）

**待实现**（等待证据）:
- ❌ 实际源提取逻辑
- ❌ 质量选择与探测
- ❌ 会话管理（Cookie/UA 要求）
- ❌ CDN 域名白名单
- ❌ 格式判断（MP4 / HLS / DASH）

## 等待的站点分析证据

### 关键问题需要答案

1. **播放器类型**
   - Video.js / JWPlayer / 自定义？
   - 配置在 HTML 还是 AJAX 返回？

2. **视频格式**
   - Progressive MP4（如 freepornvideos）？
   - HLS m3u8（如 Pornhub）？
   - DASH / 其他？

3. **源配置位置**
   - HTML 内嵌 `<script>` 变量？
   - JSON API 端点？
   - 格式示例？

4. **分辨率信息**
   - 标签准确性（如 2160p 是否实际）？
   - 需要 MP4 探测吗？
   - 多个质量选项？

5. **CDN 与签名**
   - CDN 域名（如 `xxx-cdn.com`）？
   - URL 签名机制（时效性？IP 绑定？）？
   - 防盗链要求（Referer / Origin？）？

6. **Cookie 与 UA**
   - 需要年龄验证 Cookie？
   - 特定 Cookie 名称？
   - UA 要求？

7. **URL 模式**
   - `/videos/数字/` 确认？
   - 其他变体（embed / 语言子域）？

### 期望的证据格式

**最有用的证据**:

1. **完整 HTML 源码**（至少关键部分）：
   ```html
   <!-- 播放器配置部分 -->
   <script>
   var playerConfig = { ... };
   var sources = [ ... ];
   </script>
   ```

2. **网络请求日志**（浏览器开发者工具）：
   - 页面请求 → HTML
   - 可能的 AJAX 请求 → JSON
   - 视频源 URL → MP4 / m3u8
   - CDN 域名示例

3. **实际视频源 URL 示例**：
   ```
   https://cdn-example.com/path/720p.mp4?sig=...&exp=...
   或
   https://cdn-example.com/path/index.m3u8?token=...
   ```

4. **HTTP 响应头**（视频源请求）：
   ```
   Content-Type: video/mp4 或 application/vnd.apple.mpegurl
   Content-Length: ...
   Accept-Ranges: bytes
   ```

**次要但有用**:

- 多个质量选项的 URL 对比
- 页面 HTML `<meta>` 标签（og:title 等）
- Cookie 要求（通过无 Cookie 请求测试）

## 框架设计

### 安全边界

**SSRF 防护**（已实现框架）:
```python
def _is_safe_wh_hostname(hostname: str) -> bool:
    """严格白名单验证"""
    # 拒绝私有/回环/链路本地
    # 仅允许 whoreshub.com + 待确认 CDN
```

**应用点**:
- 初始页面 URL
- 每次重定向目标
- 视频源 URL
- `/proxy` 目标验证

### 共享代码路径

**复用现有组件**:

1. **会话管理**（如需要）:
   ```python
   from download_ph_1080 import UA, _make_cookie, env_proxies
   ```

2. **MP4 探测**（如 progressive MP4）:
   ```python
   from download_fpv import probe_mp4_dimensions, pick_best_source
   ```

3. **代理透传**（ph_server.py）:
   - `/proxy` 已支持 Range 请求
   - 需添加 whoreshub CDN 到白名单

### 格式分支

**A. 若为 Progressive MP4**（如 freepornvideos）:
- 复用 `download_fpv.py` 的探测逻辑
- 返回 `is_mp4: true`
- 前端走直接下载

**B. 若为 HLS m3u8**（如 Pornhub）:
- 复用 `download_ph_1080.py` 的播放列表处理
- 改写分片 URL 为 `/proxy`
- 前端走分片下载 + mux

**C. 若为其他格式**:
- 根据实际情况设计

## 测试策略

### 现有测试（13 个，框架级别）

**SSRF 防护** (6 个):
- localhost / 回环 / 私有 IP 拒绝
- 白名单域名允许

**URL 检测** (3 个):
- 有效 URL 识别
- 无效 URL 拒绝
- ID 提取

**标题提取** (3 个):
- og:title / twitter:title
- 站点后缀清理

**占位符** (1 个):
- 确认"待实现"状态

### 待添加测试（需证据）

**源提取**:
- 从实际 HTML 提取源
- 多质量选项解析
- 格式判断

**质量选择**:
- 实际尺寸 vs 标签
- 标签回退警告
- 最高质量选择

**会话管理**:
- Cookie 保留
- UA 验证
- Referer 要求

**重定向**:
- CDN 重定向验证
- 白名单检查

**Range 支持**（若 MP4）:
- 206 Partial Content
- 416 Range Not Satisfiable
- 流式下载

## 集成计划

### ph_server.py 集成

**resolve_page() 分发**:
```python
def resolve_page(page_url: str) -> dict:
    if is_wh_page(page_url):
        return resolve_wh_job(page_url)
    # ... 现有站点
```

**resolve_wh_job() 实现**（待证据）:
```python
def resolve_wh_job(page_url: str) -> dict:
    got = resolve_wh(page_url)
    if not got.get("ok"):
        return {...}
    
    # 根据格式分支
    if got.get("is_mp4"):
        # MP4 路径（如 freepornvideos）
        return {...}
    else:
        # HLS 路径（如 Pornhub）
        return {...}
```

**_handle_proxy() 白名单**:
```python
# 添加 whoreshub CDN 到允许列表
allowed_cdn = (
    "fpvcdn.com",
    "phncdn.com",
    "pornhd3x.tv",
    # "wh-cdn-domain.com",  # 待确认
)
```

### index.html 集成

**URL 检测**:
```javascript
isWhoresHubPage(u) {
  if (!u) return false
  const host = new URL(u, location.href).hostname.toLowerCase()
  if (host.indexOf('whoreshub.com') > -1) return true
  const s = String(u).toLowerCase()
  return s.indexOf('whoreshub.com') > -1 && s.indexOf('/videos/') > -1
}
```

**resolveVideoPageThen() 分支**:
```javascript
const isWh = this.isWhoresHubPage(pageUrl)
this.tips = isWh
  ? '正在从 whoreshub.com 解析最高质量...'
  : // ... 现有提示
```

### README-PH.md 更新

**支持站点列表**:
- Pornhub（HLS m3u8）
- pornhd3x.tv（HLS m3u8 + PNG 包装）
- freepornvideos.xxx（Progressive MP4）
- **whoreshub.com**（待确认格式）

## 保留功能验证

### 不受影响的模块

- ✅ `download_ph_1080.py`（Pornhub）
- ✅ `download_phd3x.py`（pornhd3x）
- ✅ `download_fpv.py`（freepornvideos）
- ✅ 原 m3u8 下载流程
- ✅ PNG-TS 解包逻辑
- ✅ mux-mp4 合成

### 回归测试清单

**运行现有测试**:
```bash
python3 test_fpv.py        # freepornvideos 单元测试
python3 test_fpv_e2e.py    # freepornvideos E2E 测试
python3 test_wh.py         # whoreshub 框架测试
```

**预期结果**:
- freepornvideos: 34 个测试通过（15 单元 + 19 E2E）
- whoreshub: 13 个框架测试通过
- **总计**: 47 个测试通过

## Docker 集成

### Dockerfile 更新

```dockerfile
# 添加 whoreshub 模块
COPY download_wh.py ./
```

### 构建命令（不变）

```bash
# 构建
export HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890
docker compose build

# 运行
export HTTP_PROXY=http://host.docker.internal:7890 HTTPS_PROXY=http://host.docker.internal:7890
docker compose up -d
```

## 诚实限制声明

### 当前限制

**已知**:
- ❌ 无真实站点访问（403 阻止）
- ❌ 无站点结构证据（HTML / JSON / 网络请求）
- ❌ URL 模式基于假设（需验证）
- ❌ CDN 域名未知
- ❌ 视频格式未确认

**假设**（需验证）:
- URL 模式：`/videos/数字/`
- 主机名：`whoreshub.com` / `www.whoreshub.com`

### 实现后限制（预期）

**根据格式不同**:

**若为 Progressive MP4**:
- moov 窗口 4MB（超过无法完整探测）
- 探测失败回退标签（需警告不准确）
- 签名过期需重新解析

**若为 HLS m3u8**:
- 播放列表格式需支持（MPEG-TS / fMP4）
- 加密视频可能不支持（AES 密钥获取）

**通用**:
- 无真实网络端到端测试（仅离线模拟）
- 无完整视频下载测试（多 GB）
- 白名单限制（未知 CDN 需手动添加）
- 站点结构变化需更新解析

## 下一步

### 等待证据

1. **用户提供站点分析**（来自公开调查）
2. **确认关键问题答案**（见"等待的站点分析证据"）
3. **收到实际 HTML / JSON 示例**

### 实现步骤（收到证据后）

1. **更新 `download_wh.py`**:
   - 实现源提取逻辑
   - 实现质量选择
   - 更新 CDN 白名单
   - 实现会话管理

2. **集成 `ph_server.py`**:
   - `resolve_wh_job()` 实现
   - `/proxy` 白名单更新
   - 格式分支处理

3. **更新 `index.html`**:
   - `isWhoresHubPage()` 实现
   - `resolveVideoPageThen()` 分支

4. **完善测试**:
   - 源提取测试（基于实际 HTML）
   - 质量选择测试
   - 会话管理测试
   - E2E 集成测试

5. **更新文档**:
   - README-PH.md 中文说明
   - Dockerfile 包含 download_wh.py
   - 诚实限制声明

6. **验证保留功能**:
   - 运行所有回归测试
   - 确认 Pornhub / pornhd3x / freepornvideos 不受影响

7. **提交 & 推送**:
   - 在同一分支 `cursor/freepornvideos-mp4-download-ab54`
   - 更新 PR 描述

### 不做的事

- ❌ 不合并 PR（用户明确要求）
- ❌ 不猜测 HTML 结构（等待证据）
- ❌ 不虚构测试数据
- ❌ 不绕过站点防护（TLS / Captcha / 付费墙）
- ❌ 不触碰 pornhd3x SSL 问题

---

**当前状态**: 框架准备完成，等待用户提供站点分析证据后继续实现。

**测试状态**: 13 个框架测试通过，待证据后添加实际功能测试。

**分支**: `cursor/freepornvideos-mp4-download-ab54`（与 freepornvideos 同一 PR，未合并）
