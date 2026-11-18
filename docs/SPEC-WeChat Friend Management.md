# SPEC — WeChat Friend Management

**Target Repository:** `neozhu/astrbot-plugin-wechat-native`  
**Base Branch:** `main`  
**Native API:** `agent-wechat v2.7.1`  
**AstrBot:** `>=4.29.3,<5`  
**Language:** Python 3.12+

## 1. Objective

扩展 WeChat Native 插件，实现以下功能：

1. 搜索微信用户。
2. 主动发送好友申请。
3. 查询收到的好友申请。
4. 接受好友申请。
5. 订阅新好友申请事件，并向指定管理员私聊自动发送通知。

复用现有 Native HTTP Client、SSE Dispatcher、管理员权限验证、跨平台通知及持久化机制。

**设计原则：最小改动、最少配置、低 Token 消耗、避免重复实现。**

## 2. LLM Tools

新增 4 个 Function Tools。

| Tool | 参数 | 功能 |
|---|---|---|
| `wechat_search_contact` | `query` | 搜索微信用户 |
| `wechat_send_friend_request` | `recipient_ref`, `message?` | 发送好友申请 |
| `wechat_list_friend_requests` | `search?` | 查询待处理申请 |
| `wechat_accept_friend_request` | `request_id` | 接受好友申请 |

### 2.1 Search Contact

Native API：

`POST /v1/accounts/{accountId}/search/contacts`

Request：

```json
{
  "query": "wechat_id"
}
```

要求：

- 主要支持按微信号精确搜索。
- 返回用户显示名称、好友关系及 `recipientRef`。
- `recipientRef` 仅用于当前账号的后续申请操作。
- 不持久保存过期引用。
- 无结果、已是好友或无法添加时返回明确说明。
- 只搜索不自动发送申请。

### 2.2 Send Friend Request

Native API：

`POST /v1/accounts/{accountId}/contact-requests`

Request：

```json
{
  "recipientRef": "RECIPIENT_REF",
  "message": "你好，很高兴认识你"
}
```

要求：

- 必须由管理员明确要求添加好友。
- 使用搜索接口返回的有效 `recipientRef`。
- 验证引用所属账号，避免跨账号操作。
- 如果目标不明确，要求用户指定。
- 不允许 AI 猜测 `recipientRef`。
- 使用 `Idempotency-Key`，避免重复申请。
- 使用现有 `admin_token_file`。
- 保存 Operation ID，并检查 Native 返回状态。
- 申请提交成功不代表对方已接受。

### 2.3 List Friend Requests

Native API：

`GET /v1/accounts/{accountId}/contact-requests?direction=incoming&actionable=true`

要求：

- 默认仅返回待处理的收到的好友申请。
- 提供姓名、验证消息、申请时间及必要的申请标识。
- 支持按名字过滤。
- 复用 Native 分页接口，不能遗漏后续页面。
- 返回内容控制在合理长度，避免大量结果进入 LLM 上下文。
- 使用现有 Read Token。

### 2.4 Accept Friend Request

Native API：

`GET /v1/accounts/{accountId}/contact-requests/{id}`

`POST /v1/accounts/{accountId}/contact-requests/{id}/accept`

接受请求包含最新版本：

```json
{
  "expectedVersion": "VERSION"
}
```

要求：

- 必须先查询当前申请详情。
- 验证申请属于当前账号，且仍可接受。
- 使用最新 `version`。
- 使用稳定 `Idempotency-Key`。
- 复用现有 Admin Token。
- 操作无需额外二次确认。
- 姓名匹配多个申请时不自动选择。
- Native 返回 `stale_contact_request` 时停止操作并提示重新查询。
- 对不确定 Operation 结果进行查询，不重复提交。
- 只有联系人状态确认后，才能回复「已成功添加好友」；否则回复「申请确认已提交」。

## 3. Friend Request Notification

### 3.1 Native Event

订阅现有 SSE 中的：

`friend_request.updated`

Native v2.7.1 收到好友申请时可产生如下事件：

```json
{
  "kind": "friend_request.updated",
  "data": {
    "requestId": "REQUEST_ID",
    "source": "remote_sync"
  }
}
```

**注意：** 该事件也可能由本机联系人操作或同步变化触发，不能仅凭 `kind` 就认定收到新申请。

### 3.2 Event Processing

处理步骤：

1. 在 `adapter.py` 的现有 `_process_native_event()` 中增加事件分支。
2. 验证事件包含有效 `requestId`。
3. 根据 `source` 区分远端同步和本地操作，忽略本地发送/接受产生的通知事件。
4. 调用 Native API 获取申请详情。
5. 仅处理 `incoming` 且 `actionable` 的申请。
6. 检查是否已通知。
7. 获取该 Native 机器人的好友申请订阅目标。
8. 复用 AstrBot 消息发送接口推送通知。
9. 持久化去重及通知尝试结果。

**不创建第二条 SSE 连接，不使用定时轮询，不调用 LLM。**

### 3.3 Notification Message

通知使用固定纯文本模板：

```text
📩 收到新的微信好友申请

申请人：张三
验证消息：你好，我是张三

可以私聊机器人说：
“查看好友申请”
“通过张三的申请”
```

字段不存在时省略，不输出空值。

### 3.4 Duplicate Handling

- 使用 `native_bot_id + request_id` 作为去重标识。
- 同一申请的重复事件只通知一次。
- 使用现有 `StateStore` 保存去重及发送尝试状态。
- 订阅信息使用 AstrBot Plugin KV。
- 重启后恢复状态，避免重复通知。
- 首次订阅时，将已有待处理申请作为 Baseline，不补发历史通知。
- 通知失败记录状态，不无限自动重试。
- Cursor 必须在相关事件完成必要的持久化处理后推进。
- 不得为解决好友通知问题直接清空或重置现有全局消息 Cursor。

如果发生 `event_cursor_expired`，不得静默丢弃消息事件。保留现有安全处理方式，记录故障并提示管理员；如果要新增恢复能力，必须确保不会破坏原有消息处理语义。

## 4. Subscription Commands

新增 3 个普通 AstrBot Commands，不注册为 LLM Tools。

| Command | Alias | 功能 |
|---|---|---|
| `/订阅好友` | `/friend_subscribe` | 订阅新好友申请 |
| `/取消订阅好友` | `/friend_unsubscribe` | 取消订阅 |
| `/好友订阅状态` | `/friend_status` | 查询订阅状态 |

每个指令支持可选 `native_bot_id`。

要求：

- 仅管理员私聊允许执行。
- 支持现有 AstrBot 平台，包括 Telegram、QQ、LINE、`weixin_oc` 和 WeChat Native。
- 自动获取当前指令的 Session ID。
- 不允许手动指定其他用户的通知目标。
- 只有一个 Native 机器人时自动选择。
- 多机器人时要求提供机器人 ID。
- 每个 Native 机器人最多绑定一个通知目标。
- 新订阅覆盖旧目标。
- 订阅状态与现有断线通知互不影响。
- 普通指令直接执行，不经过 LLM。

复用 `AlertBindingService` 的身份验证、机器人解析和 KV 绑定模式，以及现有 `AlertSender` 的消息路由逻辑。

## 5. Token Optimization

这是本次开发的明确要求。

### 5.1 Tools Exposure

4 个好友管理 Tool 仅供 WeChat Native 管理员私聊使用。

不应在普通群聊、其他平台或普通用户的 LLM 请求中携带这些 Tool Schema。

开发时首先检查目标 AstrBot 版本是否支持按请求过滤 Tools，例如通过现有 `on_llm_request` 机制。

- 支持时：仅在符合权限的会话暴露这 4 个 Tools。
- 不支持时：不得修改全局共享 Tool Manager 来模拟按会话过滤；保留执行时权限检查，并明确记录该版本存在的 Schema Token 开销。
- Tool Description 保持简短。
- 所有真正的权限检查仍在 Python 业务逻辑中执行，不能只依赖 Schema 过滤。

### 5.2 Event Notifications

好友申请通知不进入 LLM，不生成 Agent 对话，不调用模型进行消息格式化。

三个订阅指令也不需要模型参与。

## 6. Permissions

全部好友管理 Tool 必须满足：

- `enable_friend_requests = true`
- 来源是可信的 WeChat Native Event
- 当前会话为私聊
- 当前用户经过 AstrBot 管理员身份验证
- Event、Adapter、Account ID 彼此匹配

对写操作还需确认 Admin Client 可用。

拒绝群聊及非管理员操作。

不得在日志中输出 Token、原始 Ticket、完整申请敏感数据。

搜索和发送必须遵循 Native 账号作用域，不允许跨账号复用 `recipientRef`。

## 7. Configuration

仅增加一个机器人配置项：

```yaml
enable_friend_requests: false
```

默认关闭。

复用现有配置：

- `server_url`
- `account_id`
- `token_file`
- `admin_token_file`

不要新增独立 Token、Webhook、管理员 ID 列表、轮询间隔或其他配置。

开启功能时，若 Admin Token 不可用：

- 搜索和查询仍可执行。
- 发送和接受申请返回清晰错误。
- 订阅功能仍可运行。

## 8. Code Changes

在现有仓库结构上修改：

| 文件 | 变更 |
|---|---|
| `native_client.py` | 新增 4 类 Native API 调用及 Operation 查询复用 |
| `adapter.py` | 扩展 SSE 事件处理及 Admin Client 初始化条件 |
| `main.py` | 注册 4 个 Tools、3 个 Commands |
| `tool_support.py` | 扩展可信事件和管理员私聊校验 |
| `config.py` | 增加 `enable_friend_requests` |
| `state.py` | 持久化好友申请去重及必要的操作记录 |
| `alert_binding.py` | 复用绑定能力，增加独立好友通知订阅 |
| `alert_sender.py` | 支持好友申请通知文本 |
| `friend_requests.py`（新增） | 集中处理好友业务逻辑与 Native 结果转换 |
| `README.md` / `docs/features.md` | 补充配置、工具、指令及使用说明 |

可以新增少量辅助文件，但不能重复实现现有 HTTP Client、SSE Loop、权限框架或持久化层。

## 9. Minimal Testing

**保持测试简单，不构建复杂的测试框架。**

只要求 5 组核心自动化测试：

1. **搜索与发送**：Mock API 搜索成功后，发送申请包含正确的 `recipientRef` 和幂等键。
2. **查询与接受**：能够读取待处理申请，并使用最新 `expectedVersion` 接受。
3. **事件通知与去重**：新申请通知一次；重复事件不再通知；本地发送事件不误报。
4. **订阅指令**：管理员可订阅、查询和解绑；非管理员被拒绝。
5. **重启状态**：订阅及已通知记录持久化，重启不会重复推送。

再执行一次手工验证：

- 使用真实微信账号发送好友申请。
- 检查机器人是否收到事件和通知。
- 通过 AI 查询并接受。
- 确认实际好友关系建立。

其他已有功能使用现有测试进行基本回归即可。

不需要大规模参数化测试、性能压测或完整端到端自动化框架。

## 10. Acceptance Criteria

- [ ] 4 个 LLM Tools 工作正常。
- [ ] 3 个订阅指令工作正常。
- [ ] 新好友申请自动通知订阅管理员。
- [ ] 不创建额外 SSE 连接。
- [ ] 不重复通知同一申请。
- [ ] 订阅及去重状态重启后保留。
- [ ] 非管理员及群聊无法执行好友管理。
- [ ] 发送和接受使用 Native Idempotency-Key。
- [ ] 普通指令及事件通知不调用 LLM。
- [ ] 按请求过滤 Tools 的能力已验证；如不支持，明确说明限制。
- [ ] 5 组核心测试通过。
- [ ] README 与功能文档更新。
- [ ] 现有消息、媒体、表情包、群管理及断线通知功能不受影响。

## 11. Implementation Instructions

请先检查 `main` 分支最新代码和 Native v2.7.1 API 契约，再按以下顺序开发：

1. 阅读现有架构，确认可复用组件。
2. 扩展 Native Client。
3. 实现 4 个 LLM Tools。
4. 实现 3 个订阅 Commands。
5. 在现有 SSE Dispatcher 中接入好友申请事件。
6. 实现通知去重、持久化及 Token 优化。
7. 完成 5 组简化测试。
8. 更新文档。

优先采用最小改动，不重构无关功能。

完成后提供：

- 变更文件清单
- 功能实现摘要
- 测试执行结果
- Token 优化是否真正生效
- 未经真实微信账号验证的部分
- 已知限制

**本次作为一个完整功能一次开发完成，不拆分多个开发阶段。**