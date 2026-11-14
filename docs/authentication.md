# 凭据配置

移动端请求需要有效账号会话，按以下步骤填写配置。

## 初始化

在项目根目录复制配置，保持相对路径以配置文件目录为基准：

```powershell
Copy-Item config.example.yaml config.local.yaml
New-Item -ItemType Directory -Force private\mobile
Copy-Item mobile-session.example.json private\mobile\session.local.json
```

模板所有字段为空，填写自己的真实会话信息：

| 字段 | 用途 |
|---|---|
| `t2` | 登录会话 |
| `secretKey` | 账号相关请求签名与解码材料 |
| `client_info` | 客户端信息，保持原会话的 JSON 对象或字符串形态 |
| `curidentity` | 客户端身份 |
| `v`、`app_id` | 原客户端版本及应用参数 |
| `uniqid` | 原客户端会话标识 |
| `user_agent` | 原客户端 User-Agent |

会话失效时更新该文件，再恢复相关任务。

## 代理与匿名目录

配置示例默认 `proxy: null`。需要代理时，在本地配置中填入自己可用的 SOCKS5 地址。移动与匿名各有 `proxy`、`egress_label` 和请求节奏，标签由操作者命名。

匿名目录默认 `private/browser-anonymous`，与登录浏览器分开。检测到账号认证 cookie 时，匿名执行器拒绝使用该目录。

## 数据与任务

JSONL 导出会移除 URL 中的会话和签名参数。完整数据库备份包含原始响应与请求上下文，账号会话和浏览器目录需单独备份。

`doctor` 检查配置、来源库和缓存结果，不发送 BOSS 请求。任务遇到验证码、登录失效或访问限制时停止，处理后使用 `tasks run TASK_ID --resume` 恢复。
