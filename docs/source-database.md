# 来源数据库

工具自己维护来源 SQLite，消费应用只读岗位事实。内部 owner 为 `JobMarketRadar`，数据合同为 `job-source/1`，与本地品牌 JobHarbor 分开。

## 当前事实与历史

| 表 | 保存内容 |
|---|---|
| `jobs` | 当前岗位、来源 ID、正文及事实投影 |
| `body_versions` | 原文版本，当前 version_id 对应实际字串 |
| `body_acquisitions` | 正文实际取得时间和结果 |
| `mobile_cards`、`source_aliases` | 搜索卡片、请求上下文及身份关联 |
| `status_observations`、`activity_observations` | 独立来源观察历史 |
| `private_sources` | 原始私有响应、签名上下文及迁移快照 |
| `requests`、`tasks` | 请求账本、任务游标和限制 |
| `list_observations` | 来源列表历史 |
| `source_changes`、`consumer_cursors` | 增量变化序列及明确确认的消费位置 |

模型标签、个人画像、评审、备注和沟通记录归消费应用。源库内部表不承载这些分析业务。

## 身份与时间

身份为命名空间加稳定原站 ID。BOSS 公开岗位 ID 是 canonical，移动数字 jobId 是别名；分享路径、请求 ID、已有映射各自核对。相同公司和正文的不同岗位保留独立身份与时间。

正文取得 `fetched_at`、正文变化 `changed_at`、列表发现、状态观察和活跃观察分别维护。匿名观察更新状态与活跃，完整正文时间保持。未知时间保留 null。

同文重取更新取得证据，变化时间保留。NFKC及空白归一相同但原字串不同的正文各自归档；旧输入保存为历史，较新的当前事实继续保留。变化时间表示本端首次观察到该内容版本的时间。

状态分别保存最后有效观察、最新尝试和缺观察时间的旧平台事实。unknown 尝试不覆盖最后有效 open/closed。活跃保存原文、约合天数、来源与当次观察时间。

## 招聘者字段

0.4.1移动归一优先将数字 bossId 写入 `metadata.recruiter_id`，原始 bossId 同时保留。全部行为标签进入 `metadata.recruiter_behavior_labels`。

“今日回复N次”（N>0）归为“今日活跃/0天”，分钟或小时前回复提供相应近期活动；“回复率高”仅保留标签。旧记录按原值读取，重取或明确重放使用新版归一。

发布时间文案和其观察时间一起保存，可靠绝对日期缺失时保持未知。7天、15天、薪资和个人适配由消费应用结合来源时间判断。

## 读取、迁移与消费

SourceStore 默认通过只读 URI 打开数据库。写入需明确 `writable=True`，并核验 owner，混合旧库通过 import-legacy 迁入。

JSONL export 包含当前源合同和公开观察历史，去除私有 URL 参数；SQLite backup 包含完整来源库。账号会话与浏览器目录分别备份。

变化通知按递增序号读取，读取消费流不推进游标，消费应用明确 ack 后更新自己的位置。后续数据获取独立于下游分析速度。
