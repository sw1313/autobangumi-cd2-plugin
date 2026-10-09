# CloudDrive2 Local Extension

CD2 功能完全独立于 upstream 核心代码，合并新版本时 **无需改** `module/` 下的文件。

## 目录

```
extensions/cd2/backend/cd2/   Python 扩展（API、定时任务、config/cd2.json）
webui/src/extensions/cd2/       WebUI 扩展（设置页、独立 API 调用）
config/cd2.json                 CD2 配置（独立文件，不被 config.json 覆盖）
```

## 接入方式（仅改 DevOps，不改 upstream）

容器启动时 `entrypoint.dev.sh` 设置：

```bash
export PYTHONPATH=/extensions/cd2/backend
export PYTHONSTARTUP=/extensions/cd2/backend/cd2/bootstrap_startup.py
```

`bootstrap_startup.py` 在运行时 patch upstream（注册 API、定时任务、加载 `config/cd2.json`）。

`docker-compose.synology.yml` 挂载：

```yaml
- /volume1/docker/autobangumi/app/extensions:/extensions
```

## 配置

- **CD2 设置**：`config/cd2.json`（独立保存）
- **其他设置**：`config.json`（upstream 原有逻辑）
- WebUI 保存时：CD2 走 `PATCH /api/v1/extensions/cd2/config`，主配置走原有 `/api/v1/config/update`
- 首次启动会自动把 `config.json` 里旧的 `cd2` 段迁移到 `cd2.json`

## 合并 upstream 后

只需保留（通常不会冲突）：

- `app/extensions/` 整个目录
- `webui/src/extensions/` 与 `webui/src/local-extensions.ts`
- `docker-compose.synology.yml` 的 `/extensions` 挂载与 `PYTHONPATH`
- `entrypoint.dev.sh` 的 `PYTHONSTARTUP` 两行

**不要**再改 `module/conf/config.py`、`module/api/config.py`、`module/core/context.py` 等 upstream 文件。

## 测试

```bash
PYTHONPATH=/extensions/cd2/backend pytest /extensions/cd2/tests -q
```
