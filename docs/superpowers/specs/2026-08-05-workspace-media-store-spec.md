# Workspace 与 media_store 布局 Spec

状态：已完成（2026-08-12）
范围：本次工程重构

## Problem Statement

当前 downloads 目录同时承担下载缓存、原始文件、canonical 音频、歌词和处理索引依据等多个角色。服务层因此需要依赖文件名、扩展名和 inode 扫描来推断业务关系。

这种布局不利于外部 preset 重新导出，也不利于安全清理和索引重建。

## Solution

将 workspace 拆分为不同生命周期的资源区域：

- `cache/`：临时下载和待处理文件；
- `media_store/`：长期保留的媒体资产；
- `library/`：内置 playlist_links preset 创建的本地目标视图；
- `state.db`：唯一状态来源；
- `logs/`：运行日志和诊断材料。

`media_store` 以 `track_id` 作为一级索引：

```text
media_store/
└── <track_id>/
    ├── audio/
    ├── cover/
    └── lyrics/
```

本次主要实现 audio 资产；cover 和 lyrics 目录作为同一曲目资产聚合模型的扩展位置。

每个媒体资产由 `track_id + asset_type + spec` 唯一定位，数据库额外保存路径、大小、hash、来源和更新时间。

## User Stories

1. 作为用户，我希望下载缓存和长期媒体资产分离，以便清理缓存不会影响已处理音乐。
2. 作为用户，我希望外部 preset 可以直接从 media_store 重新导出，而不重复访问网易云。
3. 作为项目维护者，我希望一个曲目的音频、封面和歌词资产能按 track_id 聚合管理。
4. 作为项目维护者，我希望通过数据库定位媒体资产，而不是解析文件名推断曲目。
5. 作为用户，我希望普通同步不会自动删除长期媒体资产。
6. 作为项目维护者，我希望 library 可以被删除后重新生成。
7. 作为用户，我希望相同规格的媒体资产可以被多个 preset 复用。
8. 作为项目维护者，我希望媒体损坏时可以通过 hash 检测并重新生成。
9. 作为用户，我希望临时下载文件在处理成功后可以自动清理。
10. 作为项目维护者，我希望重建索引时能够区分缓存文件、媒体资产和导出视图。
11. 作为 preset 作者，我希望使用稳定的媒体资产标识，而不是依赖内部目录细节。

## Implementation Decisions

- 将原有 raw media 概念统一命名为 `media_store`。
- `cache` 只保存临时下载和待处理文件。
- `media_store` 是长期标准源，普通 sync 不主动删除其中的资产。
- 删除 media_store 资产必须通过显式清理命令或明确的管理操作。
- `library` 是可重建目标视图，不是源数据主存储。
- 使用 `track_id` 作为媒体资产聚合根。
- 当前音频资产按 `audio_spec` 稳定定位，例如格式和码率组合。
- 数据库保存 SHA-256 等完整性信息，但不使用内容 hash 作为唯一物理路径。
- 不依赖 inode 扫描作为业务关系主来源。
- 硬链接优先；文件系统不支持时是否复制由目标 preset 策略决定。

## Testing Decisions

- 使用临时 workspace 验证 cache、media_store 和 library 的生命周期。
- 测试缓存清理不会删除 media_store 资产。
- 测试相同 media asset 被多个目标复用。
- 测试 hash 不一致时识别为损坏或过期。
- 测试 library 删除后可以根据数据库关系重建。
- 测试无法识别归属的文件不会被自动删除。
- 参考现有 organizer 和 playlist reconciliation 测试，继续验证外部可见的文件结果。

## Out of Scope

- 视频、播客等非音乐资产的完整实现。
- 媒体内容的内容寻址存储。
- media_store 的自动垃圾回收。
- 外部设备和播放器目标。
- cover、lyrics 的完整资产生成流程。

## Further Notes

`audio/`、`cover/`、`lyrics/` 是曲目内部的资源分类，不是 media_store 下按资源类型划分的全局一级目录。这样可以保留 track_id 作为统一索引。

