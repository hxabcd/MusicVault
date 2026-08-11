# SourceSnapshot 与 MediaResolver Spec

状态：已完成（2026-08-12）
范围：SourceSnapshot 属于本次基础能力，MediaResolver 先定义边界、按需生成延期

## Problem Statement

一次 sync 可能执行多个 preset。如果不同 preset 在不同时间读取数据库和文件系统，可能看到不一致的曲目、歌单或媒体资产。

同时，外部 preset 可能需要不同的音频格式、码率、歌词或元数据。preset 不应直接调用 ffmpeg 或修改 media_store，否则媒体生成逻辑会分散到各个脚本。

## Solution

一次 sync 开始时创建不可变 `SourceSnapshot`。所有 preset 使用相同快照，快照包含：

- 曲目和歌单关系；
- 可用媒体资产；
- 标准化元数据和歌词；
- 本次同步的 snapshot hash。

新增 `MediaResolver` 作为媒体资源能力边界。preset 声明需要的格式、码率或歌词变体，MediaResolver 负责查找、生成、登记和返回媒体资产。

本次实现可以先复用现有处理链路；MediaResolver 的按需生成和完整资产类型支持作为后续增量能力。

目标 preset 可以基于统一源元数据生成目标专属元数据和歌词表现，但不得修改 source data。

## User Stories

1. 作为用户，我希望同一次 sync 中的所有 preset 看到一致的曲目集合。
2. 作为用户，我希望慢速 preset 不会看到半完成的同步结果。
3. 作为项目维护者，我希望一次运行可以被 source snapshot hash 唯一描述。
4. 作为 preset 作者，我希望声明所需的音频格式和码率。
5. 作为 preset 作者，我希望复用已有媒体资产而不是重复转码。
6. 作为项目维护者，我希望媒体生成逻辑集中管理。
7. 作为 preset 作者，我希望按目标格式渲染元数据和歌词。
8. 作为用户，我希望修改一个目标 preset 不会污染 media_store 中的源元数据。
9. 作为项目维护者，我希望缺失媒体资产时能明确返回生成失败原因。
10. 作为用户，我希望未来新增 preset 时可以从已有 media_store 重新导出。
11. 作为测试作者，我希望可以使用固定快照测试不同 preset 的结果。

## Implementation Decisions

- 每次 sync 只创建一个 SourceSnapshot。
- 所有启用 preset 共享该 SourceSnapshot。
- sync 期间新产生的数据进入下一次 sync，不动态修改当前快照。
- MediaResolver 对外提供媒体需求，不暴露 ffmpeg 细节。
- media_store 是媒体资产持久化位置，preset 不直接写入其内部结构。
- 相同媒体需求由不同 preset 复用同一资产。
- source metadata 统一保存；目标 preset 可以生成目标格式表现。
- snapshot hash、媒体 asset hash 和 preset API 版本可作为后续运行追踪字段。
- 本次优先实现固定快照和现有处理链路接入，按需媒体生成延期。

## Testing Decisions

- 使用固定 source snapshot 测试多个 preset 的一致输入。
- 测试 snapshot 创建后源数据变化不会影响当前运行。
- 测试相同媒体需求只生成或解析一次。
- 测试目标元数据渲染不会修改源 Track 或 media_store 记录。
- 测试缺失资产和生成失败能返回结构化错误。
- 参考现有 preset、organizer 和歌词测试，验证输出内容而不是内部调用顺序。

## Out of Scope

- 完整按需转码系统。
- 视频和非音频资产。
- 目标端反向写回 source metadata。
- 内容寻址存储。
- source snapshot 的跨运行持久化历史。

## Further Notes

SourceSnapshot 解决的是一次运行内的一致性，不等同于未来的 preset 同步历史。后者由独立 spec 定义。

