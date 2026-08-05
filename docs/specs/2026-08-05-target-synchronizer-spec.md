# TargetSynchronizer Spec

状态：规划中
范围：本次先实现最小闭环，完整目标协调能力延期

## Problem Statement

如果 preset 仅被设计成文件导出器，就无法表达外部播放器、其他设备、远程目录和自定义脚本目标。不同目标可能需要不同的路径、元数据、歌词、连接方式和操作规则。

同时，不应让每个 preset 自己负责数据库、全局并发、错误汇总和运行状态，否则会形成多个互不一致的同步框架。

## Solution

将 preset 的核心抽象定义为 `TargetSynchronizer`。它负责目标策略，通用 Sync Engine 负责运行管理、标准操作执行和结构化结果。

本次实现最小生命周期：

```text
prepare → sync_item/apply → finalize
```

TargetSynchronizer 负责：

- 描述目标；
- 选择源数据；
- 将源曲目映射为目标项；
- 定义目标特有的命名、元数据和歌词规则；
- 调用标准操作或登记自定义操作；
- 返回结构化结果。

未来可以在同一抽象上增加 `observe / plan / reconcile`，支持目标状态读取和通用差异计算，但不作为本次实现前提。

同步方向限定为 MusicVault → 目标端。目标端变化不会反向写入 media_store。

## User Stories

1. 作为用户，我希望内置 playlist_links 作为一个 TargetSynchronizer 工作。
2. 作为 preset 作者，我希望定义本 preset 的目标名称和目标类型。
3. 作为 preset 作者，我希望选择需要同步的歌单和曲目。
4. 作为 preset 作者，我希望控制目标文件名、目录和扩展名。
5. 作为 preset 作者，我希望按目标需求格式化元数据和歌词。
6. 作为 preset 作者，我希望使用标准链接、复制和写入操作。
7. 作为 preset 作者，我希望在标准操作不足时注册自定义行为。
8. 作为项目维护者，我希望 synchronizer 不直接操作数据库内部表。
9. 作为项目维护者，我希望 synchronizer 不自行创建并发线程池和全局日志系统。
10. 作为用户，我希望一个目标同步失败时不影响其他 preset。
11. 作为用户，我希望目标端不被反向导入或覆盖 source data。
12. 作为项目维护者，我希望未来可以在不改变 preset 注册机制的情况下增加 observe 和 reconcile。
13. 作为用户，我希望本次先实现本地可运行闭环，而不是一次实现所有远程设备能力。

## Implementation Decisions

- `TargetSynchronizer` 是 preset 行为的公开抽象。
- 目标策略与通用 Sync Engine 分离。
- synchronizer 不持有 SQLite 连接，不直接调用内部 Service。
- synchronizer 通过公开上下文访问源数据、媒体资产、目标操作和运行信息。
- 最小生命周期包含 prepare、逐项同步或 apply、finalize。
- `prepare` 失败时终止当前 preset。
- 单项同步失败时继续处理其他曲目，并在结果中记录失败。
- 内置 playlist_links 使用该抽象实现本地链接目标。
- 当前只实现单向同步。
- 完整目标观察、目标快照、差异计划和冲突检测作为后续扩展。

## Testing Decisions

- 测试通过公开 registry 加载一个 synchronizer 并执行最小生命周期。
- 使用 fake source、fake media store 和 fake target 操作验证外部结果。
- 测试 prepare 失败、单项失败和 finalize 失败的隔离行为。
- 测试一个 preset 失败不会阻止其他 preset 执行。
- 测试 synchronizer 不能访问内部数据库实现属于架构检查，而不是运行时行为测试。
- 参考现有 dry-run 和 playlist reconciliation 测试，优先验证最终文件和结果。

## Out of Scope

- 双向同步。
- 远程播放器或设备适配器。
- 通用目标状态扫描和冲突解决。
- 自动回滚和跨目标事务。
- preset 运行历史。

## Further Notes

TargetSynchronizer 不是新的超级 Service。它应保持较小，复杂的目标操作通过公开 API 和独立 TargetAdapter 承担。

