# pyncm → pymusiclibrary 依赖替换设计

日期：2026-08-05
状态：已获用户批准（方案 A）

## 背景

当前 `src/musicvault/adapters/providers/pyncm_client.py` 基于 `pyncm`（PyPI `pyncm>=1.7.1`，已不可用）。替换为
[NeteaseCloudMusic_PythonSDK](https://github.com/2061360308/NeteaseCloudMusic_PythonSDK) 的 Python 绑定
（PyPI 包名 `pymusiclibrary`，最新 0.0.4，已确认 PyPI wheel 内含 win_amd64 预编译原生库，且包含全部所需 API 方法）。

**目标**：不改任何调用方行为 —— `NeteaseClient` 保持现有 20 个公开方法的签名与返回语义，调用方仅改 import。

## 方案 A（已选）

- 新建 `src/musicvault/adapters/providers/netease_client.py`，类 `NeteaseClient`。
- 删除 `pyncm_client.py`。
- 更新 5 个文件中的 import（`services/run_service.py`、`services/sync_service.py`、
  `services/process_service.py`、`cli/main.py` ×3、`cli/playlist.py` ×2）。
- `pyproject.toml`：移除 `pyncm>=1.7.1`，添加 `pymusiclibrary>=0.0.4`；更新 `uv.lock`。
- 更新 `CLAUDE.md` 架构注释与 `README.md` 依赖列表。

## 客户端内部结构

```
NeteaseClient
├── _cookie: str                        # 包装层持有，供新线程实例注入
├── _local = threading.local()          # 每线程懒创建 SDK 实例
│   └── _api() → NeteaseCloudMusicApi   # 首用创建 + set_cookie(dict)，复用同一线程实例
├── _call(fn)                           # 统一调用入口：响应检查 → 非 200 抛 NcmApiError
├── _retry_api()                        # 保留现有重试/退避/日志（捕获 NcmApiError/OSError/TimeoutError）
└── _parse_cookie_str(str) → dict       # "k=v; k2=v2" → dict（兼容 ";;" 分隔）
```

关键决策：

- **线程安全**：`ProcessService._process_file` 在 worker 线程调用 `get_track_lyrics` / `get_album_info`，
  而 SDK 明确要求 API 对象不能跨线程使用。每个线程通过 `threading.local` 持有独立
  `NeteaseCloudMusicApi` 实例（SDK 官方推荐用法）。CLI 进程生命周期内实例数 = 工作线程数，
  进程退出时由 OS 回收，不做显式 destroy。
- **Cookie 流转**：`login_with_cookie(str)` 解析字符串 → 存 `_cookie` → 注入当前线程实例；
  后续新线程实例自动注入。`extract_cookie()` 返回登录响应的 `body["cookie"]` 完整字符串
  （SDK 中 `Response.data` 即 `body` 的别名；无则回退 `Response.cookies` 头），
  比现状仅存 MUSIC_U/__csrf 更稳。
- **错误语义**：SDK 解析失败返回 `status=500`。统一抛 `NcmApiError`（RuntimeError 子类），
  `_retry_api` 捕获后按现有退避序列（0, 1, 3 秒，最多 3 次）重试。

## 接口映射

| NeteaseClient 方法 | SDK 调用 | 响应提取 |
|---|---|---|
| `login_with_cookie` | 解析 cookie → `set_cookie(dict)` → `get_login_status()` | — |
| `login_via_phone(phone, password/captcha, ctcode)` | `login_cellphone(phone=, password=, captcha=, countrycode=ctcode)` | `body["profile"]` → LoginResult；记录 `body["cookie"]` |
| `login_via_email(email, password)` | `login(email=, password=)` | 同上 |
| `send_sms_code(phone, ctcode)` | `captcha_sent(phone=, ctcode=)` | `status==200` 且 `body.code==200` |
| `get_qrcode_unikey` | `login_qr_key()` | `body.data.unikey` |
| `get_qrcode_url(unikey)` | 无 SDK 调用 | `f"https://music.163.com/login?codekey={unikey}"` |
| `check_qrcode(unikey)` | `verify_qrcodestatus(qr=unikey)` | `body.code`（800/801/802/803，此方法不抛错） |
| `extract_cookie` | 用登录响应记录的 cookie | 完整 cookie 字符串 |
| `get_login_status` | `login_status()` | `body.data.profile` / `body.data.account` 多层回退 |
| `list_user_playlists(uid)` | `user_playlist(uid=)` | `body.playlist` |
| `get_playlist_info(pid)` | `playlist_detail(id=)` | `body.playlist` → {id, name, trackCount} |
| `get_playlist_tracks(pid)` | `playlist_detail(id=)`；tracks 为空再 `playlist_track_all(id=)` | `body.playlist.tracks` / `body.songs` |
| `get_tracks_download_urls(ids)` | 分块后 `song_url_v1(id="1,2,3", level=下载质量)` | `body.data` → `{id: url}`（url 为 None 跳过） |
| `get_tracks_detail(ids)` | 分块后 `song_detail(ids="1,2,3")` | `body.songs` → Track（含 alias 拆分） |
| `get_album_info(aid)` | `album(id=)` | `body.album` |
| `get_track_lyrics(tid)` | `lyric_new(id=)`（保留 0.3s 限速） | 6 字段各取 `.lyric`：lrc/tlyric/romalrc/yrc/ytlrc/yromalrc |

要点：

- 批量接口利用 SDK 逗号拼接（`song_detail`/`song_url_v1` 均支持多 id），`_chunk_ids` 分块保留
  （默认 200 / 500 不变）。
- 质量词汇 `standard|higher|exhigh|hires|lossless` 与 SDK `level` 完全一致，无需映射。
- `encodeType=flac` 舍弃（v1 接口无此参数，无损/hires 级别天然返回 flac）。
- `_API_CALL_GAP`（0.3s）限速保留。
- 响应提取沿用现有"多层回退"防御式写法（`resp.get("x") or (resp.get("data") or {}).get("x")`）。

## 测试与验证

1. 新增 `tests/test_netease_client.py`：mock `NeteaseCloudMusicApi`，验证 cookie 解析/注入、
   响应提取回退、非 200 → NcmApiError、分块与逗号拼接、线程本地实例隔离。
2. 只读冒烟测试（用户已确认）：临时脚本读 `config.json` cookie，调用
   `login_with_cookie → get_login_status → get_playlist_info → get_playlist_tracks →
   get_tracks_download_urls → get_track_lyrics`，全部只读、不写状态，通过后删除脚本。
3. 现有 7 个测试文件与客户端无关，应保持全绿。

## 非目标

- 不改动 CLI 交互流程、服务层逻辑、错误提示文案。
- 不引入 SDK 的缓存/代理等功能。
- 不做 pyncm 兼容层（`poll_qrcode`、`login_via_email` 虽未被调用，仍保留以实现完整迁移）。
