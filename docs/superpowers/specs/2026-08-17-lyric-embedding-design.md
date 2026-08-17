# 内嵌歌词（Lyric Embedding）设计决策

日期：2026-08-17

## 背景与动机

用户希望 preset 能把歌词写进音频文件标签（MP3 `USLT` / FLAC `LYRICS`），供目标端（播放器/设备）消费，而不只依赖独立的 `.lrc` 文件。核心约束是**空间占用**：音频文件是共享资产（同一 `(format, bitrate)` 规格多个 preset 共用一份 canonical），把歌词写进标签会改变文件内容，若为内嵌单独复制一份最高音质音频则空间翻倍。

## 需求与术语

**内嵌歌词（Embedded Lyrics）**：preset 声明的处理能力，把渲染后的歌词文本写入音频文件元数据标签，与 `.lrc` 文件并存（同一渲染文本双路输出）。

| 枚举 | 语义 | 空间 |
|---|---|---|
| `LyricEmbed.NONE` | 不内嵌（默认），歌词仅以 `.lrc` 输出 | — |
| `LyricEmbed.OVERRIDE` | 覆盖共享 canonical 音频文件写歌词标签 | **零额外空间** |
| `LyricEmbed.SEPARATE` | 复制 canonical 为独立副本 `<tid>.<preset>.<ext>` 写歌词标签 | 该 preset 一份副本 |

## 设计决策

### 决策 1：OVERRIDE 采用宽松语义（抢占共享文件）

同一音频规格下 `OVERRIDE` 至多一个（多个报错），但**不要求独占该规格**——OVERRIDE preset「抢」共享 canonical 写歌词，同规格的其他 preset 被动共享这个已内嵌的文件。理由：OVERRIDE 是零额外空间的逃生舱，其代价就是共享者被动接受；SEPARATE 才提供「内嵌/纯净并存」的隔离语义。

- 校验点：`ProcessUseCase` 构造时，每个 `(format, bitrate)` 下 `OVERRIDE` 数量 ≤ 1，否则 `PresetLoadError`。

### 决策 2：内嵌消费在内部解决，target API 零新增

用户明确要求：**target 不应感知内嵌逻辑**。第一版实现曾在 `TargetContext` 新增 `embedded_audio(track_id, preset_name)` 并在内置 `HardlinkDistributor` 里做「副本存在即优先」，这违背该原则，已撤销。

替代方案：内嵌副本作为**独立媒体资产**以 `:embedded` 变体规格注册进 SQLite 状态，target 按 preset 声明的资产规格查询，由 resolver 透明命中正确版本。

- `BasePreset.asset_spec` 属性：`SEPARATE` 时返回 `audio_spec_key(...) + ":embedded"`，否则返回 `audio_spec_key(...)`（与现状一致）。
- process 阶段：SEPARATE 副本创建后以 `preset.asset_spec` 注册进 media_assets。
- target 侧：`HardlinkDistributor` 用 `getattr(preset, "asset_spec", None) or audio_spec_key(...)` 查询——对普通/OVERRIDE preset 结果与原来完全相同，对 SEPARATE 命中副本。用鸭子类型 `getattr` 而非 import 枚举，遵守 **target_api 不得 import preset_api** 的架构约束。

### 决策 3：OVERRIDE 内嵌 canonical，distribute 完全无感

OVERRIDE 在 process 阶段把歌词写进共享 canonical 标签，`asset_spec` 不变化，target 按原规格查询即拿到已内嵌文件。零额外空间、零 target 改动，是推荐形态。

### 决策 4：内嵌歌词与 `.lrc` 文件并存

内嵌不替代 `.lrc` 文件——同一渲染文本双路输出（写入标签 + 写入 `<tid>.<preset>.lrc`）。保持向后兼容，`.lrc` 仍由框架按 preset 独立写出。

## 实现要点

- `preset_api/v1.py`：`LyricEmbed` 枚举、`BasePreset.lyric_embed`、`BasePreset.asset_spec` 属性。
- `adapters/processors/metadata_writer.py`：`write_lyrics(audio_file, lyric_text)`，MP3 写 `USLT` 帧、FLAC 写 `LYRICS` Vorbis 注释，只改容器标签块不重编码音频流。
- `application/process_use_case.py`：OVERRIDE 冲突校验；`_embed_lyrics` 按策略执行——OVERRIDE 写 canonical 返回 `None`，SEPARATE 复制副本写标签并返回副本路径；副本以 `preset.asset_spec` 加入 `audio_map` 由 `_record_processed_results` 注册进状态。内嵌失败按 preset 降级（记录告警，不影响 `.lrc` 与该曲目状态）。
- `target_api/builtins.py`：`HardlinkDistributor` 用 `preset.asset_spec` 查询音频资产。

## 边界与已知取舍

- **副本丢失不重建**：`_is_fully_processed` 只检查规格覆盖与 `.lrc` 存在，SEPARATE 副本被删后 distribute 会静默回退到无内嵌的 canonical。若需要「副本纳入完整处理检查、丢失自动重建」可后续补充。
- **外部 sync_target 脚本**：硬编码 `audio_spec_key(preset.format, preset.bitrate)` 查询会命中 canonical（SEPARATE 时无内嵌），需改用 `preset.asset_spec` 才能命中内嵌副本。内置 `hardlink` 已正确。
- **`<tid>.<preset>.<ext>` 副本不进 `_scan_canonical_files`**：扫描按 `stem.split("_")[0] == track_id` 过滤，副本名不匹配，不会被误判为 canonical。

## 测试覆盖

- `LyricEmbed` 枚举值与 `BasePreset` 默认 `NONE`。
- `asset_spec` 随 `lyric_embed` 变化（`OVERRIDE`/`NONE` 不变，`SEPARATE` 加 `:embedded`）。
- `MetadataWriter.write_lyrics`：MP3 `USLT`、FLAC `LYRICS` 写入与替换。
- OVRERIDE 冲突校验（同规格多 `OVERRIDE` 报错；`OVERRIDE` 与普通 preset 共存合法）。
- process：OVERRIDE 写共享 canonical、SEPARATE 复制副本并注册 `:embedded` 资产。
- distribute：SEPARATE preset 经 `asset_spec` 命中副本链接到目标端。

## 文档更新

- `README.md`：功能列表、目录结构、公开 API 表、preset/sync_target 脚本编写指南。
- `AGENTS.md`：preset_api 描述（`LyricEmbed`/`asset_spec`）、workspace 布局（内嵌副本）、测试数。
