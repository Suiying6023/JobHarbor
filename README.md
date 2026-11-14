# JobHarbor

BOSS 岗位数据工具，提供 CLI 和 Python API。

- 移动端搜索、分页和完整岗位详情。
- 匿名网页状态刷新与招聘者活跃记录。
- SQLite 存储、正文版本和独立观察时间。
- 可恢复任务、请求限速、JSONL 导入导出与备份。

环境：Python 3.11+、uv；匿名刷新需要 Chrome。

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe ".[browser]"
Copy-Item config.example.yaml config.local.yaml
.venv\Scripts\boss-data.exe --config config.local.yaml init
.venv\Scripts\boss-data.exe --config config.local.yaml doctor
```

移动端功能需按[凭据配置](docs/authentication.md)填写自己的有效会话。

[使用说明](docs/usage.md)
