# Preset System Design

## 概述

将当前硬编码的 lossless/lossy 双输出架构替换为可配置的 N 个 preset 系统。每个 preset 定义一套完整的输出规格（音频格式、质量、元数据策略、歌词配置、文件名模板、目录名）。音频文件按 `(format, bitrate)` 去重，相同规格只存一份。

## Preset 数据模型

```python
@dataclass(slots=True)
class Preset:
    name: str                      # 唯一标识符，也是 library 目录名
    quality: str = "hires"         # 网易云音质等级 (standard/higher/exhigh/hires/lossless)
    format: str | None = None      # 输出格式 (flac/mp3/aac/ogg/opus)，None=保持源格式
    bitrate: str | None = None     # 有损码率 (如 "192k")，format=flac 时忽略
    filename_template: str = "{artist} - {name}"

    # 元数据
    embed_cover: bool = True
    cover_max_size: int = 0
    embed_lyrics: bool = True
    metadata_fields: tuple[str, ...] = ()   # () = 全部

    # 歌词
    use_karaoke: bool = False
    include_translation: bool = True
    translation_format: str = "separate"    # separate | inline | notimestamp
    include_romaji: bool = False
    write_lrc_file: bool = True
    lrc_encodings: tuple[str, ...] = ("utf-8",)
```

### 默认预设

新用户开箱即用两个 preset：

| 字段 | archive | portable |
|------|---------|----------|
| quality | hires | hires |
| format | flac | mp3 |
| bitrate | - | 192k |
| filename | `{artist} - {name}` | `{alias} {name} - {artist}` |
| embed_cover | true | false |
| embed_lyrics | true | false |
| use_karaoke | true | false |
| translation_format | separate | inline |
| write_lrc_file | false | true |
| lrc_encodings | utf-8 | utf-8, gb18030 |

### 约束

- `name` 必须唯一，且为合法文件名
- `presets` 列表不能为空（启动校验，空则报错）
- `quality` 取所有 preset 最高值用于下载

## Config 改造

### Config 字段变化

从 Config dataclass 中移除以下字段（下沉到 Preset）：

`filename_lossless`, `filename_lossy`, `lossless_translation_format`, `lossy_translation_format`, `karaoke_lossless`, `karaoke_lossy`, `lossy_bitrate`, `lossy_format`, `lossy_lrc_encodings`, `embed_cover`, `cover_max_size`, `lyrics_embed_in_metadata`, `lyrics_write_lrc_file`, `include_translation`, `include_romaji`

新增：`presets: list[Preset]`

保留的全局字段：`cookie`, `workspace`, `download_workers`, `process_workers`, `ffmpeg_threads`, `ffmpeg_path`, `network_*`, `keep_downloads`, `api_*`, `text_cleaning_*`, `alias_*`, `default_playlist_name`, `force`

### 目录属性变化

```python
@property
def library_dir(self) -> Path:
    return self.workspace_path / "library"

# 按需获取 preset 目录
def preset_dir(self, preset_name: str) -> Path:
    return self.library_dir / preset_name
```

移除 `lossless_dir` / `lossy_dir` 属性，替换为遍历 presets。

### ensure_dirs()

为每个 preset 创建 `library/<name>/`，不再创建固定的 `lossless/`、`lossy/`。

### 无迁移

旧格式 config.json 启动时直接报错退出，用户自行手动转换为新格式。

### config.json 示例

```json
{
    "cookie": "",
    "workspace": "./workspace",
    "presets": [
        {
            "name": "archive",
            "quality": "hires",
            "format": "flac",
            "filename_template": "{artist} - {name}",
            "embed_cover": true,
            "embed_lyrics": true,
            "use_karaoke": true,
            "translation_format": "separate",
            "write_lrc_file": false
        },
        {
            "name": "portable",
            "quality": "hires",
            "format": "mp3",
            "bitrate": "192k",
            "filename_template": "{alias} {name} - {artist}",
            "embed_cover": false,
            "embed_lyrics": false,
            "write_lrc_file": true,
            "lrc_encodings": ["utf-8", "gb18030"]
        }
    ]
}
```

## 音频去重

### 规格键

`(format, bitrate)` 元组决定唯一音频文件。format=None 时用源文件实际格式。

### 文件名生成

```
规格去重后仅一个 → {track_id}.{ext}
同 format 多规格   → {track_id}_{bitrate}.{ext}（全部加后缀，保持一致）

示例：
  specs: {("flac", None)}                              → 12345.flac
  specs: {("mp3", "320k")}                             → 12345.mp3
  specs: {("mp3", "320k"), ("mp3", "192k"), ("flac", None)} → 12345.flac, 12345_320k.mp3, 12345_192k.mp3
```

### Organizer 改造

```python
class Organizer:
    def route_audio(self, src: Path, track: Track, output_dir: Path,
                    audio_specs: set[tuple[str | None, str | None | None]])
        -> dict[tuple[str | None, str | None], Path]
```

输入为去重后的规格集合，输出为 `{spec: canonical_path}` 映射。

## 处理管线

### ProcessService._process_file()

```
1. 解密（如需要）
2. Organizer.route_audio() → {audio_spec: canonical_path}
3. 获取歌词（一次 API 调用）
4. 下载封面（一次，全文件共享）
5. 每个 canonical 文件写全量元数据 + 封面
6. 为每个 preset 构建歌词 → 嵌入 + LRC 文件
```

### 元数据策略

- 所有 canonical 文件写全量元数据字段（无 `is_lossless` 概念）
- `metadata_fields` 作为过滤器控制写入哪些扩展字段。若多个 preset 共享同一 canonical 文件且字段列表不同，取并集（`()` = 全部，并集自然为全部）
- `embed_cover` 和 `embed_lyrics` 按 preset 控制，但共享同一 canonical 文件的多个 preset 中取"最丰富"的值（任意 True 即 True）
- 封面缓存同 session 内同 URL 只下载一次

### 歌词嵌入冲突处理

多个 preset 共享一个 canonical 文件但歌词设置不同时，取"最丰富"的歌词作为嵌入：

优先级：Karaoke(含翻译+罗马音) > Karaoke(含翻译) > Karaoke(原文) > 标准(含翻译+罗马音) > 标准(含翻译) > 标准(原文)

### LRC 文件

每个 `write_lrc_file=True` 的 preset 产出独立 LRC：

```
downloads/12345.archive.lrc
downloads/12345.portable.lrc
downloads/12345.phone.lrc
```

### processed_files.json

```json
{
    "12345": {
        "audios": {
            "FLAC": "downloads/12345.flac",
            "MP3-192k": "downloads/12345_192k.mp3"
        },
        "updated_at": 1234567890
    }
}
```

key 是规格签名 `FORMAT-BITRATE`（FORMAT 大写），不含 preset 名。

## Library 链接

### 目录结构

```
library/
  <preset.name>/
    <playlist>/
      <filename_template>.<ext>
      <filename_template>.lrc
```

### 链接创建逻辑

对每个 preset，从 `audio_map` 找到对应的 canonical 文件，按 `filename_template` 生成链接名，在所有歌单目录下创建硬链接。

### SyncService 变化

- `_handle_playlist_rename()` — 遍历 `library/<preset>/` 而非固定的 lossless/lossy
- `_prune_stale_tracks()` — 同上
- `_reconcile_playlist_assignments()` — 同上
- 移除 `_lossless_link_name()` / `_lossy_link_name()`，替换为通用模板格式化
- `run_sync()` 中 `Downloader(filename_template)` 改为使用第一个 preset 的模板（下载阶段仅用于缓存文件命名）

### RunService.rebuild_index()

扫描 `library/<preset>/<playlist>/` 结构，通过 inode 反查 canonical 文件 → track_id。

## 测试策略

### 更新现有测试

- `test_config_model.py` — preset 格式的 Config 序列化/反序列化
- `test_playlist_reconciliation.py` — mock 改为 preset 结构

### 新增测试

- `test_preset_dedup.py` — 音频规格去重逻辑
- `test_preset_organizer.py` — 多规格路由

### 不受影响的测试

`test_lyrics.py`, `test_lyrics_encoding.py`, `test_models.py`（Lyrics 和 Track 模型不变）
