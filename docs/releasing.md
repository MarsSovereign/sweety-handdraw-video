# 发布一个稳定版本

`skills/sweety-handdraw-video/VERSION` 是版本源。版本已公开后不移动原标签；修复发布使用递增版本。

1. 修改 VERSION、首页当前版本和“需要固定本版时”的 ZIP 链接、包内 README 当前版本；新增 `docs/releases/v<版本>.md`。新版安装包尚未发布时，这些变更应与标签一起发布。
2. 运行 `python3 tools/release.py check`。该命令只核对本地版本与入口，不声称远程附件已经存在。
3. 运行 README 中的回归和离线冒烟检查，提交变更，在干净的该提交创建匹配的注释标签。
4. 将代码与指定标签推送。`Publish tagged skill` 会等待 macOS / Linux 的完整 Verify 工作流通过，再从标签所指提交打包。它检查 ZIP 内 VERSION、整包和逐文件哈希后，上传 ZIP、`SHA256SUMS.txt`、`package-sha256.json`，最后公开 Release。
5. 回读远程提交、标签与 Release 附件，下载 ZIP 和 SHA256SUMS.txt 实测校验。解压到空目录，读取 VERSION 并运行 `scripts/validate_package.py`。用户已有安装副本不会因仓库更新而自动替换。

发布工作流支持手动输入**已有标签**重跑。已有公开 Release 时只验证附件哈希，不覆盖其文件；草稿允许重新上传同一标签的构建结果。远程 CI 未完成、失败或安装包未能实际下载时，应如实报告状态。
