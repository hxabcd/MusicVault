# 目标安全策略与 Manifest Spec

状态：后续扩展
范围：本次仅确定删除策略，manifest 延期

## Problem Statement

单向同步仍然需要处理目标端多余对象。如果系统直接删除目标目录中的未知文件，可能误删用户手动管理的内容。

仅依赖本地数据库也不足以识别外部设备中哪些对象由某个 preset 管理，因为目标可能被移动、重建或脱离当前 workspace。

## Solution

为目标定义删除策略：

- `append`：只新增和更新，不删除；
- `managed`：只删除当前 preset 明确管理的过期对象；
- `mirror`：目标端严格镜像源状态。

外部 preset 默认使用 `append`。内置 playlist_links 可以使用 `managed`，因为其目标对象由系统创建并可重建。

未来通过目标端 manifest 保存管理边界。manifest 记录 preset、目标 ID、对象稳定 key、目标位置、fingerprint、脚本/API 版本和最近同步时间。

删除必须同时满足：对象属于当前 preset 管理范围、目标端有对应管理记录、preset 允许删除、对象不再属于期望状态。

## User Stories

1. 作为用户，我希望普通外部同步不会删除目标端未知文件。
2. 作为用户，我希望内置歌单链接可以清理已经失去归属的链接。
3. 作为 preset 作者，我希望明确选择 append、managed 或 mirror。
4. 作为项目维护者，我希望删除只作用于当前 preset 管理的对象。
5. 作为用户，我希望 dry-run 显示将要删除的目标对象。
6. 作为项目维护者，我希望目标迁移后仍能恢复管理边界。
7. 作为用户，我希望数据库重建不会导致系统删除未知文件。
8. 作为项目维护者，我希望 manifest 能记录目标对象的 fingerprint。
9. 作为用户，我希望目标端手动添加的文件默认被保留。
10. 作为项目维护者，我希望危险的 mirror 策略显式声明并可审查。

## Implementation Decisions

- 当前只支持单向 MusicVault → 目标端。
- 外部目标默认 append。
- 内置 playlist_links 可以使用 managed。
- mirror 不作为默认行为。
- 未知对象不得自动删除。
- 删除操作必须是可见、可预览和可记录的。
- 目标端 manifest 作为后续扩展，不作为本次最小闭环的前置条件。
- manifest 与本地数据库未来采用双重记录，但不能替代目标端实际探测。

## Testing Decisions

- 测试 append 不删除目标端多余文件。
- 测试 managed 只删除已登记对象。
- 测试未知文件、用户文件和已管理过期文件的差异。
- 测试 dry-run 的删除清单。
- 测试 mirror 需要显式配置。
- manifest 实现阶段增加迁移、损坏和目标移动测试。

## Out of Scope

- 本次实现 manifest。
- 双向同步。
- 冲突解决和目标端反向导入。
- 远程设备的删除确认协议。

## Further Notes

本 spec 的核心目标是防止误删；manifest 是实现目标迁移和数据库重建时安全识别的后续手段。

