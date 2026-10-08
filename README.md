# AutoBangumi CloudDrive2 Plugin

AutoBangumi 的本地 CloudDrive2（CD2）离线回退插件。当 qBittorrent
任务长时间无速度时，插件可将磁力链接提交给 CD2，待云端下载和复制完成后，
把文件同步回 qBittorrent 目录并执行校验、恢复做种。

本仓库只包含 CD2 插件，不包含播放器扩展、AutoBangumi 核心源码、真实配置、
下载数据或数据库。

## 功能

- 独立保存 `config/cd2.json`，不把 CD2 密码写入 AutoBangumi 主配置。
- 检测卡住的 Bangumi 分类任务并提交 CD2 离线下载。
- 跟踪已提交任务，将完成内容复制到本地并同步回 qBittorrent。
- 支持手动选择种子并强制执行 CD2 修复。
- 完成后触发 qBittorrent 重新校验并恢复做种。
- 复制到本地时，把上传调度改成复制先于备份。备份任务继续保留，离线下载队列不改。
- WebUI 配置页、连接测试及中英文界面。

## 更新

### 2026-10-08

- qB 打上交接标签时如果连接被掐断，插件会重试这次请求。空的断线错误也会再试。补丁在插件启动时装上，不改 AutoBangumi 的下载器源码。
- 只复制 115 上还在的文件。一个路径不存在时不再让整批复制失败。
- 同一次扫描里，115 离线列表失败只警告一次。
- 复制任务排到备份任务前面。CloudDrive2 默认先跑备份，备份占满上传名额时，复制到本地会一直等待。插件只改上传调度顺序，不暂停、不删除备份，也不改动离线下载。
- 复制开始后出现的下载，是这次复制在读取云盘，随复制一起获得名额。

### 此前

- 完成的离线任务按 info hash 对应到自己的种子，不再按相近文件名配对。
- 暂停的复制定期小批恢复，已完成的本地中转复制任务会从列表里删掉。

## 目录

```text
overlay/extensions/cd2/                 后端插件、启动入口和测试
overlay/webui/src/extensions/cd2/       WebUI 设置组件
config/cd2.example.json                 无敏感信息的配置示例
```

## 安装

将 `overlay` 下的目录合并到 AutoBangumi 源码挂载目录：

```bash
cp -a overlay/. /volume1/docker/autobangumi/app/
```

Docker Compose 至少需要挂载插件和 CD2 本地中转目录：

```yaml
volumes:
  - /volume1/docker/autobangumi/app/extensions:/extensions
  - /volume1/videos/cd2-offline:/cd2-offline

environment:
  - PYTHONPATH=/extensions/cd2/backend
```

启动 AutoBangumi 前执行 `/extensions/cd2/backend/ab_entry.py`。源码挂载部署
可以在 entrypoint 中设置：

```bash
export AB_APP_DIR=/app
export PYTHONPATH="/extensions/cd2/backend:${PYTHONPATH}"
exec python /extensions/cd2/backend/ab_entry.py
```

WebUI 需要在项目的本地扩展注册器中调用
`registerCd2Extension(registry)`，然后重新构建 WebUI。

## 配置

复制示例配置到 AutoBangumi 的持久化配置目录：

```bash
cp config/cd2.example.json /volume1/docker/autobangumi/config/cd2.json
```

随后修改 `host`、`username` 和 `password`。请勿提交真实的
`config/cd2.json`。

## 测试

```bash
PYTHONPATH=overlay/extensions/cd2/backend \
pytest overlay/extensions/cd2/tests -q
```

## 兼容性

该插件通过启动时 bootstrap 注册设置、定时任务和 API，不修改
AutoBangumi 的 `module/` 核心源码。上游内部接口发生变化时仍可能需要适配。

## 安全说明

- 仓库只提供占位配置。
- API 返回配置时会遮蔽密码。
- 日志不应输出真实密码。
- 如果误提交凭据，请立即在 CD2 中轮换密码并清理 Git 历史。

## 许可证

MIT
