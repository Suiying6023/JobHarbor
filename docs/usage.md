# 命令与运行

命令为 `boss-data`，Python 包为 `boss_data`。在已激活的安装环境中运行以下示例，或把命令替换为 `.venv\Scripts\boss-data.exe`。JSON 结果写 stdout，任务进度事件写 stderr。

## 安装和本地读取

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe ".[browser]"
Copy-Item config.example.yaml config.local.yaml
.venv\Scripts\boss-data.exe --config config.local.yaml init
.venv\Scripts\boss-data.exe --config config.local.yaml doctor
.venv\Scripts\boss-data.exe --config config.local.yaml status
.venv\Scripts\boss-data.exe --config config.local.yaml list --limit 5
```

相对路径以配置文件目录为基准。在其他目录执行时明确提供配置路径。浏览器功能通过可选 `browser` extra 安装。

账号文件与浏览器目录见[凭据配置](authentication.md)。`doctor` 读取配置、来源库和缓存结果，检查代理监听，不调用 BOSS。`probe` 才显式发送一项业务请求。

## 搜索、详情和观察

以下 `boss:example` 用实际已存来源 ID 替换：

```powershell
boss-data --config config.local.yaml search --query Python后端 --city 100010000 --page 1
boss-data --config config.local.yaml search --query Python后端 --city 100010000 --page 2
boss-data --config config.local.yaml detail --id boss:example --max-body-age-hours 24
boss-data --config config.local.yaml refresh --ids boss:example --mode anonymous --max-requests 1
boss-data --config config.local.yaml refresh --ids boss:example --mode authenticated --max-requests 1
```

搜索/分页使用移动 HTTP 批处理，详情使用卡片或私有归档的签名上下文。移动数字岗位 ID 经分享路径关联公开岗位 ID，冲突保存失败。

`detail` 是显式重取。指定年龄窗口后，已确认完整且仍新鲜的正文可复用；`--force` 明确重取，包括已关闭项。`collect` 默认 `--details-mode missing`，优先未映射、无正文或完整性待确认的卡片；已有完整正文可用 `--details-mode refresh --max-body-age-hours N` 维护。

同文重新取得更新取得时间，变化时间保留。关闭、缓存及同任务已完成详情等跳过不占实际详情请求额度，剩余候选补足额度。不同来源 ID 保持独立。

匿名使用独立无登录目录读取 SSR 状态与活跃；完整正文取得时间保持原值。判定结合 HTTP、实际 URL、岗位身份、H1、状态容器及真实验证信号。

## 有界任务与自主循环

```powershell
boss-data --config config.local.yaml collect --query Python后端 --query 智能体开发 --pages 3 --details-per-page 1 --max-requests 12
boss-data --config config.local.yaml loop --mode collect --query Python后端 --pages 3 --details-per-page 1 --max-requests 60 --max-seconds 7200
boss-data --config config.local.yaml select-refresh --stale-hours 24 --limit 12
boss-data --config config.local.yaml loop --mode anonymous --stale-hours 24 --limit 12 --max-requests 100 --max-seconds 7200
```

程序自动接续查询、分页和维护周期，查询重复槽位保持配置的轮转权重，真实 `has_more=false` 结束后续页。状态维护按源观察年龄选择，独立于岗位评审和个人筛选。

循环需请求/时长限制或明确 `--until-cancel`。时长从首次运行按墙钟计算，包含等待和批休；恢复保留原限制。移动默认间隔35–55秒、每批10–14次、批休360–480秒；匿名默认15–17秒、每12次批休15–19秒，均可在配置中调整。

移动和匿名各有执行锁，账号共享请求时钟，来源库使用 WAL 和短写事务。持续速率按真实时间窗统计，成功请求、唯一岗位、首次正文、同文重取、变化正文各自计量。

## 取消与恢复

```powershell
boss-data --config config.local.yaml tasks list
boss-data --config config.local.yaml tasks show TASK_ID
boss-data --config config.local.yaml tasks cancel TASK_ID
boss-data --config config.local.yaml tasks run TASK_ID --resume --max-requests 12
boss-data --config config.local.yaml tasks run TASK_ID --resume --extend-max-requests 120 --extend-max-seconds 14400
```

开始时进度输出包含任务句柄。取消标志持久保存，等待结束后再次检查，当前已发出的请求保留结果或未知尝试。归档响应可以离线补处理；缺响应的中断保留 unknown，由操作者明确恢复。

验证码、登录失效、限制、传输或协议异常停止受影响任务。HTTP、平台码、消息与端点分别保存；账号和出口按原配置维护。

## 导入、导出与备份

```powershell
boss-data --config config.local.yaml import-legacy --source legacy.sqlite3
boss-data --config config.local.yaml import-private --mobile-directory local-mobile-archive
boss-data --config config.local.yaml export --out exports/current-source
boss-data --config config.local.yaml import --manifest exports/current-source/manifest.json
boss-data --config config.local.yaml database inspect
boss-data --config config.local.yaml database duplicates
boss-data --config config.local.yaml database backup --out private/backups/source.sqlite3
```

旧库用只读 URI 打开，来源事实迁入工具库，分析与个人表留在旧库。旧快照不会回退较新的当前事实，缺失时间保持未知。

export 提供当前源合同、公开状态与活跃历史；backup 保存完整来源数据库，包括版本、取得账本、请求、任务和私有证据。会话文件及浏览器目录单独保存。

字段、时间和消费合同见[来源数据库](source-database.md)。

## 增量消费和离线回放

```powershell
boss-data --config config.local.yaml changes --consumer downstream --limit 100
boss-data --config config.local.yaml ack --consumer downstream --through 100
boss-data --config config.local.yaml replay --file archived-detail.private.json --kind detail --observed-at ACTUAL_OBSERVATION_TIME
```

changes 读取不推进游标，ack 由消费者明确确认。详情回放使用原来的带时区观察时间。回放不发送 BOSS 请求。

```python
from boss_data import BossCli, SourceStore

client = BossCli(config="config.local.yaml")
with SourceStore("private/source.sqlite3") as source:
    record = source.get("boss:example")
    changes = source.changes(after=0, limit=100)
```

SourceStore 默认只读；来源写入由工具执行并核验 owner。模型分析使用来源读取接口，网络动作由 BossCli 负责。
