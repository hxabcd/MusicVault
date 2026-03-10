# MusicVault

一个面向网易云收藏歌单的本地同步与整理工具（CLI 版）

> [!NOTE]
> 此项目使用 Vibe Coding，大部分代码由 AI (Codex) 编写
>
> 此项目为本人自用

## 当前已实现

- 登录状态读取（优先 cookie）
- 拉取用户歌单并识别“我喜欢的音乐”作为同步源
- 与本地状态文件比对，仅下载新增歌曲
- 下载后自动尝试解密 `.ncm`
- 按音质分流到 `lossless/` 与 `lossy/`
- 使用 `mutagen` 写入元数据（标题/歌手/专辑/封面）；`lossless` 额外内嵌歌词
- 为 `lossy` 输出 GB2312 编码歌词文件（兼容部分播放器）
- 仅使用网易云返回翻译（`tlyric`）：`lossless` 同时间轴下一行追加翻译，`lossy` 同行前置翻译

## 安装

```bash
pip install -e .
```

该版本使用直接依赖导入，运行前请确保依赖已安装完整。

## 用法

```bash
musicvault run --cookie "MUSIC_U=...; __csrf=..." --workspace ./workspace
```

也可使用默认配置文件 `./config.json`（适合存放不常变参数，如 `cookie`）：

```bash
musicvault run
```

仅同步下载：

```bash
musicvault sync
```

仅处理本地下载：

```bash
musicvault process
```

可选参数：

- `--config`：配置文件路径（默认 `./config.json`）
- `--playlist-id`：显式指定歌单 ID（不填默认取“我喜欢的音乐”）
- `--force`：强制重处理已处理文件（忽略已处理索引）
- `--no-translation`：关闭网易云翻译合并（默认开启）

参数优先级：命令行 > 配置文件 > 内置默认值。

配置文件可选项：

- `text_cleaning.enabled`：是否在请求响应后递归清洗字符串脏字符（默认 `true`）
- `workers.download`：下载并发数（默认自动按 CPU 推断）
- `workers.process`：处理并发数（默认自动按 CPU 推断）
- `workers.ffmpeg_threads`：ffmpeg 线程数（默认自动按 CPU 推断）
- `lyrics.lossy_lrc_encodings`：有损 `.lrc` 编码回退顺序（默认 `["gb2312","gb18030","utf-8-sig"]`）

## 目录结构

运行后默认在 `workspace/` 下生成：

- `downloads/`：原始下载（含可能的 `.ncm`）
- `library/lossless/`：无损结果
- `library/lossy/`：有损结果
- `state/synced_tracks.json`：已同步曲目 ID
- `state/processed_files.json`：已处理文件索引（用于跳过重复处理）

## 说明

- 受网易云接口和账号权限影响，部分歌曲可能无法获取直链。
