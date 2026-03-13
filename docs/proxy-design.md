# 代理与账号轮换机制 — 完整设计文档

> 分支：`feature/proxy-refactor`
> 整理时间：2026-03-13
> 作者：nanobot 自动梳理

---

## 一、整体架构概览

项目中存在**两套相互独立的代理机制**，分别服务于不同场景：

```
代理体系
├── A. AI 请求代理（PROXY_URL）
│   └── 作用：让 OpenAI/大模型 API 请求走代理
│
└── B. 爬虫代理池轮换（PROXY_POOL + RotationPool）
    └── 作用：让 Playwright 浏览器请求走代理，规避闲鱼风控
```

---

## 二、A. AI 请求代理（`src/config.py`）

### 配置方式

```env
PROXY_URL=http://127.0.0.1:7890
```

### 实现逻辑

```python
# src/config.py
if PROXY_URL:
    os.environ['HTTP_PROXY'] = PROXY_URL
    os.environ['HTTPS_PROXY'] = PROXY_URL

client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
# openai 内部的 httpx 自动读取环境变量中的代理
```

### 特点与问题

| 项目 | 说明 |
|------|------|
| 实现方式 | 写入全局环境变量，httpx 自动读取 |
| 生效范围 | **整个进程**，包括所有线程/协程 |
| ⚠️ 问题 | 多任务并发时，不同任务无法使用不同 AI 代理 |
| ⚠️ 问题 | 无法动态切换，启动后固定 |

---

## 三、B. 爬虫代理池轮换

### 3.1 配置方式

支持两种配置来源，**任务级配置优先于环境变量**：

**环境变量（全局默认）：**
```env
PROXY_ROTATION_ENABLED=false
PROXY_ROTATION_MODE=per_task        # per_task | on_failure
PROXY_POOL=http://p1:8080,http://p2:8080
PROXY_ROTATION_RETRY_LIMIT=2
PROXY_BLACKLIST_TTL=300
```

**任务级配置（`config.json` 中单个任务）：**
```json
{
  "task_name": "我的任务",
  "proxy_rotation": {
    "enabled": true,
    "mode": "on_failure",
    "proxy_pool": ["http://p1:8080", "http://p2:8080"],
    "retry_limit": 3,
    "blacklist_ttl_sec": 600
  }
}
```

### 3.2 核心类：`RotationPool`（`src/rotation.py`）

```
RotationPool
├── items: List[RotationItem]       # 所有代理条目
├── blacklist: Dict[str, float]     # 黑名单（代理地址 → 过期时间戳）
├── blacklist_ttl: int              # 黑名单有效期（秒）
│
├── available_items()               # 返回未被拉黑的代理列表
├── pick_random()                   # 随机选一个可用代理
└── mark_bad(item, reason)          # 将代理加入黑名单
```

**黑名单清理机制：**
- 每次调用 `available_items()` 时自动清理过期黑名单
- 过期时间 = 加入时间 + `blacklist_ttl`
- `blacklist_ttl=0` 时永不拉黑（禁用黑名单）

### 3.3 轮换模式

#### 模式一：`per_task`（每次任务换一个代理）

```
任务启动
  └─→ pick_random() 选一个代理
        └─→ 用该代理运行整个任务
              ├─→ 成功 → 记录成功，代理保留
              └─→ 失败 → mark_bad() 拉黑，重试（最多 retry_limit 次）
```

#### 模式二：`on_failure`（失败时才切换代理）

```
任务启动
  └─→ 不主动选代理，直接运行
        └─→ 失败时 → pick_random() 选新代理 → 重试
              └─→ 再失败 → mark_bad() 拉黑 → 再选 → 直到 retry_limit 耗尽
```

### 3.4 代理在 Playwright 中的注入方式

```python
# scraper.py 中 run_task() 的核心逻辑（简化）
async with async_playwright() as p:
    browser = await p.chromium.launch(
        proxy={"server": proxy_item.value} if proxy_item else None
    )
    context = await browser.new_context(**context_options)
```

代理直接传入 Playwright 的 `launch()` 参数，**不影响全局环境变量**，多任务并发安全。

---

## 四、账号轮换机制（与代理并行）

### 4.1 配置方式

```env
ACCOUNT_ROTATION_ENABLED=false
ACCOUNT_ROTATION_MODE=per_task      # per_task | on_failure
ACCOUNT_STATE_DIR=state             # 存放多个 .json 登录态文件的目录
ACCOUNT_ROTATION_RETRY_LIMIT=2
ACCOUNT_BLACKLIST_TTL=300
```

### 4.2 实现逻辑

账号轮换复用同一套 `RotationPool`，但轮换的是**登录态文件路径**而非代理地址：

```python
# rotation.py
def load_state_files(state_dir: str) -> List[str]:
    """扫描目录下所有 .json 文件作为账号池"""
    return sorted([
        os.path.join(state_dir, f)
        for f in os.listdir(state_dir)
        if f.endswith(".json")
    ])

# scraper.py 中
account_pool = RotationPool(
    items=load_state_files(account_state_dir),
    blacklist_ttl=account_blacklist_ttl,
    name="account"
)
account_item = account_pool.pick_random()
# 用 account_item.value（文件路径）加载 cookies/storage_state
```

### 4.3 账号与代理的组合关系

```
任务运行时
  ├─→ 账号轮换（选登录态文件）
  └─→ 代理轮换（选代理地址）
        └─→ 两者独立选取，互不干扰
              └─→ 组合使用：账号A + 代理1，账号B + 代理2，...
```

---

## 五、熔断保护机制（`src/failure_guard.py`）

### 5.1 作用

当任务**连续失败达到阈值**时，自动暂停任务，避免无限重试触发风控。

### 5.2 配置

```env
TASK_FAILURE_THRESHOLD=3           # 连续失败多少次触发熔断
TASK_FAILURE_PAUSE_SECONDS=86400   # 熔断后暂停多少秒（默认24小时）
TASK_FAILURE_GUARD_PATH=logs/task-failure-guard.json  # 状态持久化文件
TASK_FAILURE_TZ=Asia/Shanghai      # 时区（用于每日通知去重）
```

### 5.3 状态机

```
正常运行
  └─→ 失败 → consecutive_failures++
        ├─→ < threshold → 继续重试
        └─→ >= threshold → 熔断
              └─→ paused_until = now + pause_seconds
                    ├─→ 暂停期间：每天最多通知一次
                    └─→ 自动恢复条件：
                          ├─→ paused_until 到期
                          └─→ cookies 文件被更新（mtime 变化）→ 立即恢复
```

### 5.4 特殊快速熔断

以下错误**1次失败即触发熔断**（不等待 threshold）：
- `"未找到可用的代理地址"` — 代理池为空
- `"未找到可用的登录状态文件"` — 账号池为空

### 5.5 持久化格式

```json
{
  "version": 1,
  "tasks": {
    "我的任务": {
      "consecutive_failures": 2,
      "paused_until": null,
      "last_failure_reason": "风控拦截",
      "last_failure_at": "2026-03-13T10:00:00",
      "last_success_at": "2026-03-13T09:00:00",
      "last_notified_date": null,
      "cookie_path": "state/account1.json",
      "cookie_mtime": 1741824000.0
    }
  }
}
```

---

## 六、完整调用链路

```
app.py（FastAPI）
  └─→ 触发任务
        └─→ scraper.run_task(task_config)
              │
              ├─→ 1. FailureGuard.should_skip_start()
              │       ├─→ 熔断中？→ 跳过，每日通知一次
              │       └─→ 正常 → 继续
              │
              ├─→ 2. _get_rotation_settings(task_config)
              │       └─→ 读取账号/代理轮换配置（任务级 > 环境变量）
              │
              ├─→ 3. 构建 RotationPool
              │       ├─→ account_pool = RotationPool(load_state_files(...))
              │       └─→ proxy_pool = RotationPool(parse_proxy_pool(...))
              │
              ├─→ 4. 轮换循环（最多 retry_limit 次）
              │       ├─→ account_item = account_pool.pick_random()
              │       ├─→ proxy_item = proxy_pool.pick_random()
              │       │
              │       ├─→ 启动 Playwright
              │       │     ├─→ browser.launch(proxy=proxy_item.value)
              │       │     └─→ context.add_cookies(account_item.value)
              │       │
              │       ├─→ 执行爬取逻辑
              │       │     ├─→ 搜索商品
              │       │     ├─→ 解析结果
              │       │     ├─→ AI 分析（走 PROXY_URL 代理）
              │       │     └─→ 推送通知
              │       │
              │       ├─→ 成功 → FailureGuard.record_success() → 退出循环
              │       └─→ 失败 → account_pool.mark_bad() / proxy_pool.mark_bad()
              │                   → 重试
              │
              └─→ 5. 全部重试耗尽 → FailureGuard.record_failure() → 熔断
```

---

## 七、已知问题与优化建议

### 问题 1：AI 代理污染全局环境变量 ⚠️

**现状：**
```python
os.environ['HTTP_PROXY'] = PROXY_URL  # 影响整个进程
```

**影响：** 多任务并发时，所有任务的 AI 请求都走同一个代理，无法按任务隔离。

**建议：**
```python
# 改为 httpx 显式传入，不污染全局
import httpx
http_client = httpx.AsyncClient(proxy=PROXY_URL)
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, http_client=http_client)
```

---

### 问题 2：代理池为空时无明确提示 ⚠️

**现状：** `proxy_pool.pick_random()` 返回 `None`，调用方需自行判断。

**建议：** 在 `RotationPool` 中增加 `is_exhausted()` 方法，并在 `run_task` 入口提前检查。

---

### 问题 3：`on_failure` 模式缺少退避策略 ⚠️

**现状：** 失败后立即重试，高频场景可能加速触发风控。

**建议：** 增加指数退避：
```python
await asyncio.sleep(2 ** retry_count)  # 1s, 2s, 4s...
```

---

### 问题 4：账号与代理无绑定关系 ℹ️

**现状：** 账号和代理独立随机选取，可能出现同一账号配不同代理的情况。

**建议（可选）：** 支持账号-代理绑定配置：
```json
{
  "account_proxy_pairs": [
    {"state": "state/account1.json", "proxy": "http://p1:8080"},
    {"state": "state/account2.json", "proxy": "http://p2:8080"}
  ]
}
```

---

## 八、配置速查表

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `PROXY_URL` | — | AI 请求代理（全局） |
| `PROXY_ROTATION_ENABLED` | `false` | 是否启用爬虫代理轮换 |
| `PROXY_ROTATION_MODE` | `per_task` | 轮换模式：`per_task` / `on_failure` |
| `PROXY_POOL` | — | 代理池，逗号分隔 |
| `PROXY_ROTATION_RETRY_LIMIT` | `2` | 代理重试次数上限 |
| `PROXY_BLACKLIST_TTL` | `300` | 代理黑名单有效期（秒） |
| `ACCOUNT_ROTATION_ENABLED` | `false` | 是否启用账号轮换 |
| `ACCOUNT_ROTATION_MODE` | `per_task` | 账号轮换模式 |
| `ACCOUNT_STATE_DIR` | `state` | 登录态文件目录 |
| `ACCOUNT_ROTATION_RETRY_LIMIT` | `2` | 账号重试次数上限 |
| `ACCOUNT_BLACKLIST_TTL` | `300` | 账号黑名单有效期（秒） |
| `TASK_FAILURE_THRESHOLD` | `3` | 熔断触发阈值 |
| `TASK_FAILURE_PAUSE_SECONDS` | `86400` | 熔断暂停时长（秒） |
