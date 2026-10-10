# AutoBangumi CloudDrive2 插件

适配 AutoBangumi 4.x 的本地插件。qBittorrent 卡种时把任务交给 CloudDrive2 离线下载，完成后复制回本地并恢复做种。

`3.x/` 是给 AutoBangumi 3.x 留的归档。4.x 使用仓库根目录这一套，不要再把 `overlay` 合并进主程序。

## 功能

- 连接信息写在插件设置里，并保存到运行目录的 `config/cd2.json`，不写入 AutoBangumi 主配置。
- 密码留空表示不修改。保存后输入框保持空白，已保存的密码不会显示。
- 测试连接在保存按钮旁边，不另开设置项。
- 卡住的番剧任务按 info hash 提交到 CD2 离线下载。
- 下载完成后把文件复制到本地，再同步回 qBittorrent 并恢复做种。
- 下载器操作栏有 CD2 修复，可以对手动选中的任务再执行一次交接。
- 复制上传排在备份前面。只调整 CloudDrive2 的上传顺序，不暂停、不删除任务，也不改离线下载。

## 安装

需要 AutoBangumi 4.0 或更新的 4.x。这个插件没有签名，要先在设置里允许未签名插件（`plugins.allow_unsigned`）。

把仓库根目录里除 `3.x` 以外的文件放到：

```text
config/plugins/local/cd2
```

目录名必须是 `cd2`，和 `plugin.toml` 里的 id 一致。重启容器后，在插件设置里填写地址、账号和路径。

CloudDrive2 本体在单独的容器或群晖套件里。这个插件只是连过去的 gRPC 客户端。4.x 镜像不带 `grpcio`，插件目录里也不能放 `.so`，宿主扫到会拒绝加载整个插件。

插件启动时如果当前 Python 还不能导入客户端，会把 musl 版轮子下载到配置目录的 `cd2-runtime`。这个目录在插件外面，加载检查扫不到，也跟着配置卷保留。官方镜像没有 `libstdc++`，这份 musl 轮子也不依赖它。仓库放到 `config/plugins/local/cd2` 后重启就会自己装好。

```yaml
volumes:
  - /path/to/this-repo:/app/config/plugins/local/cd2
```

页面上的测试连接和修复按钮由插件自己插入，不需要重新编译 WebUI。

## 配置

真实的 `config/cd2.json` 只放在 AutoBangumi 的配置目录。不要把主机地址、用户名、密码或 token 提交到仓库。

## 测试

```bash
PYTHONPATH=backend pytest tests -q
```

## 兼容

4.x 从插件入口加载，不修改 AutoBangumi 的 `module/`。3.x 的 overlay 安装方式写在 `3.x/README.md`。

## 安全说明

- 仓库只包含插件代码。
- 接口沿用 AutoBangumi 的登录状态。
- 如果曾经提交过凭据，先换掉 CD2 密码，再从 Git 历史里清除。

## 许可证

MIT
