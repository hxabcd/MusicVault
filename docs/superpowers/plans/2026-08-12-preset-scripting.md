# 脚本化 preset 体系与四阶段链路 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将项目重构为基于 Python 脚本的 preset 体系：preset 声明音频规格/歌词/元数据，sync_target 按名称引用 preset 并定义分发；处理链路变为 fetch → pull → process → distribute 四阶段，命令收敛为 `sync [--only-distribute|--no-distribute]` + `distribute`。

**Architecture:** 新增 domain 层统一歌词结构化模型（LyricLine/LyricWord）与 SQLite 存储（pull 阶段入库、process 离线消费）；preset_api 扩展为两套公开 API（preset 声明 + sync_target 分发）；旧 `Config.presets` 声明式配置退役，preset 注册表成为唯一来源；SyncUseCase 拆 fetch/pull 两阶段，ProcessUseCase 改为调 preset 歌词函数，distribute 复用 SyncEngine 执行 sync_target 脚本；media_store 扁平化（`<tid>/audio/` → `<tid>/`）。

**Tech Stack:** Python 3.12+、SQLite（现有 schema 版本化）、mutagen、ffmpeg（测试经 fake/mock 隔离）、pytest、ruff（line-length=120）。

## Global Constraints

- 所有新增/修改代码注释与 commit message 使用中文；领域术语遵循 `CONTEXT.md`（曲目（Track）、预设（Preset）、目标同步器（Target Synchronizer）、源快照（Source Snapshot））。
- 依赖方向固定：`presentation → application → domain`、`adapters ──┘`；`domain/` 只允许标准库。
- preset_api 是外部脚本唯一可依赖的公开 API；preset 脚本不得 import 内部模块。
- 测试命令：`python -m pytest tests/ -q`；lint：`python -m ruff check src/ tests/`。
- 每个任务结束时全量测试通过（改造类任务允许更新受影响旧测试）。
- `docs/superpowers/specs/2026-08-12-preset-scripting-design.md` 是唯一需求来源，冲突时以 spec 为准。
- 改造类任务开工前先读目标文件与其现有测试，按现有 fake 模式扩展，不要照搬本计划中的示例命名。

---

### Task 1: domain/lyrics.py 统一歌词模型与序列化

**Files:**
- Create: `src/musicvault/domain/lyrics.py`
- Test: `tests/test_domain_lyrics.py`

**Interfaces:**
- Produces:
  - `LyricWord(start_ms: int, text: str)` — frozen dataclass, slots
  - `LyricLine(start_ms: int, duration_ms: int, text: str, words: tuple[LyricWord, ...] = (), translation: str = "", romaji: str = "")` — frozen dataclass, slots
  - `lyrics_to_json(lines: tuple[LyricLine, ...]) -> str` — 紧凑 JSON（`ensure_ascii=False, separators=(",", ":")`）
  - `lyrics_from_json(payload: str) -> tuple[LyricLine, ...]` — 容忍未知字段与缺失 key，非数组抛 `ValueError`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_domain_lyrics.py
import pytest
from musicvault.domain.lyrics import LyricLine, LyricWord, lyrics_from_json, lyrics_to_json


def test_roundtrip_preserves_all_fields():
    lines = (LyricLine(
        start_ms=1000, duration_ms=3000, text="hello",
        words=(LyricWord(1000, "he"), LyricWord(1500, "llo")),
        translation="你好", romaji="haro",
    ),)
    assert lyrics_from_json(lyrics_to_json(lines)) == lines


def test_empty_lines_roundtrip():
    assert lyrics_from_json(lyrics_to_json(())) == ()


def test_from_json_tolerates_unknown_and_missing_fields():
    payload = '[{"start_ms":1,"duration_ms":0,"text":"x","words":[],"translation":"","romaji":"","unknown":1}]'
    assert lyrics_from_json(payload) == (LyricLine(1, 0, "x"),)


def test_from_json_rejects_malformed():
    with pytest.raises(ValueError):
        lyrics_from_json("not json")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_domain_lyrics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'musicvault.domain.lyrics'`

- [ ] **Step 3: 实现**

```python
# src/musicvault/domain/lyrics.py
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LyricWord:
    start_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class LyricLine:
    start_ms: int
    duration_ms: int
    text: str
    words: tuple[LyricWord, ...] = ()
    translation: str = ""
    romaji: str = ""


def lyrics_to_json(lines: tuple[LyricLine, ...]) -> str:
    return json.dumps(
        [
            {
                "start_ms": line.start_ms,
                "duration_ms": line.duration_ms,
                "text": line.text,
                "words": [{"start_ms": w.start_ms, "text": w.text} for w in line.words],
                "translation": line.translation,
                "romaji": line.romaji,
            }
            for line in lines
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def lyrics_from_json(payload: str) -> tuple[LyricLine, ...]:
    raw = json.loads(payload)
    if not isinstance(raw, list):
        raise ValueError("lyrics payload 必须是行数组")
    lines: list[LyricLine] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"歌词行格式错误：{item}")
        words = tuple(
            LyricWord(start_ms=int(w["start_ms"]), text=str(w["text"]))
            for w in item.get("words") or ()
        )
        lines.append(
            LyricLine(
                start_ms=int(item["start_ms"]),
                duration_ms=int(item.get("duration_ms", 0)),
                text=str(item.get("text", "")),
                words=words,
                translation=str(item.get("translation", "")),
                romaji=str(item.get("romaji", "")),
            )
        )
    return tuple(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_domain_lyrics.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/domain/lyrics.py tests/test_domain_lyrics.py
git commit -m "feat: domain 层统一歌词结构化模型与 JSON 序列化"
```

---

### Task 2: preset_api 渲染工具（standard_lrc/enhanced_lrc/plain_text）

**Files:**
- Create: `src/musicvault/preset_api/render.py`
- Test: `tests/test_preset_render.py`

**Interfaces:**
- Consumes: `LyricLine`/`LyricWord`（Task 1）
- Produces:
  - `standard_lrc(lines: tuple[LyricLine, ...], *, include_translation: bool = False, include_romaji: bool = False) -> str` — 每行 `[mm:ss.xxx]text`；翻译/罗马音作为同时间戳独立行
  - `enhanced_lrc(lines: tuple[LyricLine, ...], *, include_translation: bool = False, include_romaji: bool = False) -> str` — 逐字时间戳渲染 `[s1]字[s2]幕...` + 行尾 `[end]`（duration_ms > 0 时）；无 words 退化为单时间戳行；翻译/罗马音作为同 start 独立行
  - `plain_text(lines: tuple[LyricLine, ...]) -> str` — 每行 text，无时间戳
  - 空行列表 → 空字符串

- [ ] **Step 1: 写失败测试**

```python
# tests/test_preset_render.py
from musicvault.domain.lyrics import LyricLine, LyricWord
from musicvault.preset_api.render import enhanced_lrc, plain_text, standard_lrc


def test_standard_lrc_plain():
    lines = (LyricLine(1000, 0, "hello"), LyricLine(2000, 0, "world"))
    assert standard_lrc(lines) == "[00:01.000]hello\n[00:02.000]world"


def test_standard_lrc_with_translation_and_romaji():
    lines = (LyricLine(1000, 0, "hello", translation="你好", romaji="haro"),)
    assert standard_lrc(lines, include_translation=True, include_romaji=True) == (
        "[00:01.000]hello\n[00:01.000]你好\n[00:01.000]haro"
    )


def test_enhanced_lrc_with_words():
    lines = (LyricLine(1000, 3000, "hello", words=(LyricWord(1000, "he"), LyricWord(1500, "llo"))),)
    assert enhanced_lrc(lines) == "[00:01.000]he[00:01.500]llo[00:04.000]"


def test_enhanced_lrc_falls_back_without_words():
    lines = (LyricLine(1000, 0, "hello"),)
    assert enhanced_lrc(lines) == "[00:01.000]hello"


def test_enhanced_lrc_with_translation():
    lines = (LyricLine(1000, 3000, "你好", words=(LyricWord(1000, "你"), LyricWord(1200, "好")), translation="hello"),)
    assert enhanced_lrc(lines, include_translation=True) == (
        "[00:01.000]你[00:01.200]好[00:04.000]\n[00:01.000]hello"
    )


def test_plain_text():
    lines = (LyricLine(1000, 0, "hello", translation="你好"), LyricLine(2000, 0, "world"))
    assert plain_text(lines) == "hello\nworld"


def test_empty_lines_produce_empty_string():
    assert standard_lrc(()) == ""
    assert enhanced_lrc(()) == ""
    assert plain_text(()) == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_preset_render.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现**

```python
# src/musicvault/preset_api/render.py
from __future__ import annotations

from musicvault.domain.lyrics import LyricLine


def _ms_to_tag(ms: int) -> str:
    minutes = ms // 60000
    seconds = (ms % 60000) / 1000
    return f"{minutes:02d}:{seconds:06.3f}"


def standard_lrc(
    lines: tuple[LyricLine, ...],
    *,
    include_translation: bool = False,
    include_romaji: bool = False,
) -> str:
    result: list[str] = []
    for line in lines:
        tag = f"[{_ms_to_tag(line.start_ms)}]"
        result.append(f"{tag}{line.text}")
        if include_translation and line.translation:
            result.append(f"{tag}{line.translation}")
        if include_romaji and line.romaji:
            result.append(f"{tag}{line.romaji}")
    return "\n".join(result)


def enhanced_lrc(
    lines: tuple[LyricLine, ...],
    *,
    include_translation: bool = False,
    include_romaji: bool = False,
) -> str:
    result: list[str] = []
    for line in lines:
        if line.words:
            out = "".join(f"[{_ms_to_tag(w.start_ms)}]{w.text}" for w in line.words)
            if line.duration_ms > 0:
                out += f"[{_ms_to_tag(line.start_ms + line.duration_ms)}]"
            result.append(out)
        else:
            result.append(f"[{_ms_to_tag(line.start_ms)}]{line.text}")
        tag = f"[{_ms_to_tag(line.start_ms)}]"
        if include_translation and line.translation:
            result.append(f"{tag}{line.translation}")
        if include_romaji and line.romaji:
            result.append(f"{tag}{line.romaji}")
    return "\n".join(result)


def plain_text(lines: tuple[LyricLine, ...]) -> str:
    return "\n".join(line.text for line in lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_preset_render.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/preset_api/render.py tests/test_preset_render.py
git commit -m "feat: preset_api 基于行模型的歌词渲染工具（standard/enhanced/plain）"
```

---

### Task 3: 歌词转换器（原始 payload → LyricLine 列表）

**Files:**
- Modify: `src/musicvault/adapters/processors/lyrics.py`（新增模块级函数，**保留**现有 `StandardLyrics`/`KaraokeLyrics` 类与私有解析工具，删除延后到 Task 12）
- Test: `tests/test_lyrics_converter.py`

**Interfaces:**
- Consumes: `LyricLine`/`LyricWord`（Task 1）；lyrics.py 现有私有工具 `_sanitize_lyrics_text`/`_parse_yrc_line`/`_parse_line`/`_build_translation_map`/`_time_tag_to_ms`/`_ms_to_time_tag`/`_find_translation_fuzzy`/`_is_same_text`
- Produces:
  - `convert_lyrics_payload(payload: dict[str, str]) -> tuple[LyricLine, ...]`
    - YRC 优先（`yrc` 非空时），逐行 `_parse_yrc_line`；翻译/罗马音从 `ytlrc`/`yromalrc` 按 start_ms fuzzy 对齐（`tolerance_ms=200`）
    - 标准 LRC 回退：`lrc` 行逐行解析；重复时间戳行拆成多行；翻译/罗马音按时间戳 map 精确匹配 + fuzzy 回退
    - 无歌词/空 payload → `()`；翻译/罗马音与原文相同（`_is_same_text`）时置空串

- [ ] **Step 1: 写失败测试**

```python
# tests/test_lyrics_converter.py
from musicvault.adapters.processors.lyrics import convert_lyrics_payload
from musicvault.domain.lyrics import LyricLine, LyricWord


def test_yrc_conversion_with_words_and_translation():
    payload = {
        "yrc": "[1000,3000](1000,300,0)你(1500,400,0)好",
        "ytlrc": "[00:01.000]你好翻译",
        "yromalrc": "",
    }
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 1
    line = lines[0]
    assert line.start_ms == 1000
    assert line.duration_ms == 3000
    assert line.text == "你好"
    assert line.words == (LyricWord(1000, "你"), LyricWord(1500, "好"))
    assert line.translation == "你好翻译"
    assert line.romaji == ""


def test_standard_lrc_conversion():
    payload = {"lrc": "[00:01.000]hello\n[00:02.000]world", "tlyric": "[00:01.000]你好", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 2
    assert lines[0] == LyricLine(1000, 0, "hello", translation="你好")
    assert lines[1] == LyricLine(2000, 0, "world")


def test_repeated_timestamp_lines_split_into_rows():
    payload = {"lrc": "[00:01.000][01:31.000]hello", "tlyric": "", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert [line.start_ms for line in lines] == [1000, 91000]
    assert all(line.text == "hello" for line in lines)


def test_empty_payload_returns_empty():
    assert convert_lyrics_payload({}) == ()
    assert convert_lyrics_payload({"lrc": "", "yrc": "", "tlyric": "", "romalrc": ""}) == ()


def test_yrc_fuzzy_translation_alignment():
    payload = {
        "yrc": "[2000,2000](2000,300,0)早(2500,300,0)安",
        "ytlrc": "[00:01.900]おはよう",
        "yromalrc": "",
    }
    lines = convert_lyrics_payload(payload)
    assert lines[0].translation == "おはよう"


def test_metadata_json_lines_are_cleaned():
    payload = {"lrc": '{"t":1000,"c":[{"tx":"x"}]}\n[00:01.000]hello', "tlyric": "", "romalrc": ""}
    lines = convert_lyrics_payload(payload)
    assert len(lines) == 1
    assert lines[0].text == "hello"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_lyrics_converter.py -q`
Expected: FAIL with `ImportError: cannot import name 'convert_lyrics_payload'`

- [ ] **Step 3: 实现（追加到 `adapters/processors/lyrics.py` 末尾）**

```python
def convert_lyrics_payload(payload: dict[str, str]) -> tuple[LyricLine, ...]:
    """将网易云原始歌词 payload 转换为统一结构化行列表。"""
    from musicvault.domain.lyrics import LyricLine, LyricWord

    yrc = _sanitize_lyrics_text(payload.get("yrc") or "")
    if yrc:
        return _convert_yrc_lines(yrc, payload)
    lrc = _sanitize_lyrics_text(payload.get("lrc") or "")
    if not lrc:
        return ()
    return _convert_lrc_lines(lrc, payload)


def _convert_yrc_lines(yrc: str, payload: dict[str, str]) -> tuple[LyricLine, ...]:
    from musicvault.domain.lyrics import LyricLine, LyricWord

    trans_map = _build_translation_map(payload.get("ytlrc") or "")
    romaji_map = _build_translation_map(payload.get("yromalrc") or "")
    lines: list[LyricLine] = []
    for raw_line in yrc.splitlines():
        parsed = _parse_yrc_line(raw_line)
        if not parsed:
            continue
        start_ms, duration_ms, words, text = parsed
        translation = _find_translation_fuzzy(start_ms, trans_map, tolerance_ms=200) or ""
        romaji = _find_translation_fuzzy(start_ms, romaji_map, tolerance_ms=200) or ""
        if _is_same_text(text, translation):
            translation = ""
        if _is_same_text(text, romaji):
            romaji = ""
        lines.append(
            LyricLine(
                start_ms=start_ms,
                duration_ms=duration_ms,
                text=text,
                words=tuple(LyricWord(start_ms=w_start, text=w_text) for w_start, w_text in words),
                translation=translation,
                romaji=romaji,
            )
        )
    return tuple(lines)


def _convert_lrc_lines(lrc: str, payload: dict[str, str]) -> tuple[LyricLine, ...]:
    from musicvault.domain.lyrics import LyricLine

    trans_map = _build_translation_map(payload.get("tlyric") or "")
    romaji_map = _build_translation_map(payload.get("romalrc") or "")
    lines: list[LyricLine] = []
    for raw_line in lrc.splitlines():
        timestamps, text = _parse_line(raw_line)
        if not timestamps or not text:
            continue
        for raw_ts in timestamps:
            start_ms = _time_tag_to_ms(raw_ts)
            if start_ms is None:
                continue
            tag = _ms_to_time_tag(start_ms)
            translation = trans_map.get(tag) or _find_translation_fuzzy(start_ms, trans_map, tolerance_ms=200) or ""
            romaji = romaji_map.get(tag) or _find_translation_fuzzy(start_ms, romaji_map, tolerance_ms=200) or ""
            if _is_same_text(text, translation):
                translation = ""
            if _is_same_text(text, romaji):
                romaji = ""
            lines.append(LyricLine(start_ms=start_ms, duration_ms=0, text=text, translation=translation, romaji=romaji))
    return tuple(lines)
```

注意：`_find_translation_fuzzy` 现有签名 `(start_ms, translation_map, tolerance_ms=500)` — 保持该签名，调用处传 `tolerance_ms=200`（spec 修订值）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_lyrics_converter.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/adapters/processors/lyrics.py tests/test_lyrics_converter.py
git commit -m "feat: 歌词转换器（原始 payload → 统一结构化行，YRC 优先）"
```

---

### Task 4: preset_api/v1.py 公开 API 扩展（枚举/MetadataSpec/BasePreset/双注册类型）

**Files:**
- Modify: `src/musicvault/preset_api/v1.py`
- Test: `tests/test_preset_api.py`

**Interfaces:**
- Consumes: `LyricLine`（Task 1）、`render` 工具（Task 2）
- Produces（全部加入 `__all__`）：
  - `class Quality(Enum)` — STANDARD/HIGHER/EXHIGH/HIRES/LOSSLESS；`Quality.maximum(items: Iterable[Quality]) -> Quality`（按声明顺序取最大，空输入回退 HIRES）
  - `class AudioFormat(Enum)` — FLAC/MP3/AAC/OGG/OPUS
  - `class LyricEncoding(Enum)` — UTF_8="utf-8"/GB18030="gb18030"
  - `class MetadataSpec` — frozen dataclass：`embed_cover: bool = True`、`cover_max_size: int = 0`、`fields: tuple[str, ...] = _FULL_FIELDS`；`full()`（全部字段）/`basic()`（fields=()）/`none()`（embed_cover=False, fields=()）；构造函数可覆盖任意项
  - `class BasePreset` — 类属性 `quality/format/bitrate/lyrics_encodings/metadata` + 方法 `build_lyrics(self, lines) -> str`（默认 `standard_lrc(lines)`）
  - `@dataclass(frozen=True, slots=True) PresetRegistration` — `name/factory/api_version=API_VERSION/enabled=True/source="<runtime>"`；`create() -> Any`（类则实例化、可调用则调用）
  - `@dataclass(frozen=True, slots=True) TargetRegistration` — `name/factory/depends_on: tuple[str, ...] = ()/api_version/enabled/source/target: TargetDescriptor | None = None`
  - `audio_spec_key(fmt: AudioFormat | None, bitrate: str | None) -> str` — 迁移自 `domain/preset.py`（None → "ORIGINAL"；枚举 `.value.upper()`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_preset_api.py
import pytest
from musicvault.domain.lyrics import LyricLine
from musicvault.preset_api.v1 import (
    AudioFormat, BasePreset, LyricEncoding, MetadataSpec, PresetLoadError,
    PresetRegistration, Quality, TargetRegistration, audio_spec_key,
)


def test_quality_maximum():
    assert Quality.maximum([Quality.HIGHER, Quality.HIRES, Quality.EXHIGH]) is Quality.HIRES
    assert Quality.maximum([]) is Quality.HIRES


def test_metadata_spec_presets_and_override():
    assert MetadataSpec.full().embed_cover is True
    assert MetadataSpec.none().embed_cover is False
    assert MetadataSpec.none().fields == ()
    assert MetadataSpec.basic().embed_cover is True
    assert MetadataSpec.basic().fields == ()
    assert MetadataSpec.basic(embed_cover=False).embed_cover is False


def test_base_preset_defaults():
    preset = BasePreset()
    assert preset.quality is Quality.HIRES
    assert preset.format is None
    assert preset.lyrics_encodings == (LyricEncoding.UTF_8,)
    assert preset.metadata == MetadataSpec.basic()
    assert preset.build_lyrics((LyricLine(1000, 0, "hello"),)) == "[00:01.000]hello"


def test_base_preset_subclass_override():
    class MyPreset(BasePreset):
        quality = Quality.LOSSLESS
        format = AudioFormat.FLAC

        def build_lyrics(self, lines):
            return "custom"

    preset = MyPreset()
    assert preset.quality is Quality.LOSSLESS
    assert preset.build_lyrics(()) == "custom"


def test_audio_spec_key():
    assert audio_spec_key(None, None) == "ORIGINAL"
    assert audio_spec_key(AudioFormat.FLAC, None) == "FLAC"
    assert audio_spec_key(AudioFormat.MP3, "192k") == "MP3-192k"


def test_registrations_validate_name():
    with pytest.raises(PresetLoadError):
        PresetRegistration(name="bad name", factory=object)
    with pytest.raises(PresetLoadError):
        TargetRegistration(name="", factory=object)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_preset_api.py -q`
Expected: FAIL with `ImportError`

- [ ] **Step 3: 实现（追加到 `preset_api/v1.py`）**

顶部 import 追加：`from enum import Enum`、`from musicvault.domain.lyrics import LyricLine`。

```python
class Quality(Enum):
    STANDARD = "standard"
    HIGHER = "higher"
    EXHIGH = "exhigh"
    HIRES = "hires"
    LOSSLESS = "lossless"

    @classmethod
    def maximum(cls, items: Iterable["Quality"]) -> "Quality":
        ordered = [cls.STANDARD, cls.HIGHER, cls.EXHIGH, cls.HIRES, cls.LOSSLESS]
        candidates = [item for item in items if isinstance(item, Quality)]
        if not candidates:
            return cls.HIRES
        return max(candidates, key=ordered.index)


class AudioFormat(Enum):
    FLAC = "flac"
    MP3 = "mp3"
    AAC = "aac"
    OGG = "ogg"
    OPUS = "opus"


class LyricEncoding(Enum):
    UTF_8 = "utf-8"
    GB18030 = "gb18030"


_FULL_FIELDS = ("year", "track_number", "disc_number", "genre", "album_artist", "composer", "lyricist")


@dataclass(frozen=True, slots=True)
class MetadataSpec:
    embed_cover: bool = True
    cover_max_size: int = 0
    fields: tuple[str, ...] = _FULL_FIELDS

    @classmethod
    def full(cls) -> "MetadataSpec":
        return cls(embed_cover=True, fields=_FULL_FIELDS)

    @classmethod
    def basic(cls) -> "MetadataSpec":
        return cls(embed_cover=True, fields=())

    @classmethod
    def none(cls) -> "MetadataSpec":
        return cls(embed_cover=False, fields=())


class BasePreset:
    quality: Quality = Quality.HIRES
    format: AudioFormat | None = None
    bitrate: str | None = None
    lyrics_encodings: tuple[LyricEncoding, ...] = (LyricEncoding.UTF_8,)
    metadata: MetadataSpec = MetadataSpec.basic()

    def build_lyrics(self, lines: tuple[LyricLine, ...]) -> str:
        from musicvault.preset_api.render import standard_lrc
        return standard_lrc(lines)


@dataclass(frozen=True, slots=True)
class PresetRegistration:
    name: str
    factory: Any
    api_version: str = API_VERSION
    enabled: bool = True
    source: str = "<runtime>"

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise PresetLoadError(f"preset 名称非法：{self.name}")

    def create(self) -> Any:
        if inspect.isclass(self.factory):
            return self.factory()
        if callable(self.factory):
            return self.factory()
        raise PresetLoadError(f"preset '{self.name}' 的 factory 不可调用：{self.source}")


@dataclass(frozen=True, slots=True)
class TargetRegistration:
    name: str
    factory: Any
    depends_on: tuple[str, ...] = ()
    api_version: str = API_VERSION
    enabled: bool = True
    source: str = "<runtime>"
    target: TargetDescriptor | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise PresetLoadError(f"sync_target 名称非法：{self.name}")
        if self.target is None:
            object.__setattr__(self, "target", TargetDescriptor(identifier=self.name))


def audio_spec_key(fmt: AudioFormat | None, bitrate: str | None) -> str:
    if fmt is None:
        return "ORIGINAL"
    fmt_upper = fmt.value.upper()
    if bitrate:
        return f"{fmt_upper}-{bitrate}"
    return fmt_upper
```

更新 `__all__` 加入全部新符号。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_preset_api.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/preset_api/v1.py tests/test_preset_api.py
git commit -m "feat: preset_api 公开 API 扩展（枚举/MetadataSpec/BasePreset/双注册类型）"
```

---

### Task 5: SQLite lyrics 表与存取方法

**Files:**
- Modify: `src/musicvault/ports/state.py`（Protocol 加 `save_lyrics`/`get_lyrics`）
- Modify: `src/musicvault/adapters/state/sqlite.py`（先读该文件与现有 schema 版本化写法，加 `lyrics` 表）
- Test: `tests/test_state_lyrics.py`（参照现有 sqlite 测试的 fixture 模式）

**Interfaces:**
- Consumes: 现有 `SQLiteState`/`SQLiteStateRepository`
- Produces:
  - `StateRepository.save_lyrics(track_id: int, payload: str, fetched_at: float, *, connection: Any = None) -> None` — upsert
  - `StateRepository.get_lyrics(track_id: int) -> str | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_state_lyrics.py
from musicvault.adapters.state.sqlite import SQLiteState, SQLiteStateRepository


def test_lyrics_upsert_and_read(tmp_path):
    state = SQLiteStateRepository(SQLiteState(tmp_path / "test.db"))
    assert state.get_lyrics(42) is None
    state.save_lyrics(42, '[{"start_ms":1,"duration_ms":0,"text":"x"}]', 123.0)
    state.save_lyrics(42, '[{"start_ms":2,"duration_ms":0,"text":"y"}]', 456.0)  # upsert 覆盖
    assert state.get_lyrics(42) == '[{"start_ms":2,"duration_ms":0,"text":"y"}]'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_state_lyrics.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'save_lyrics'`

- [ ] **Step 3: 实现**

`ports/state.py` Protocol 追加：

```python
def save_lyrics(self, track_id: int, payload: str, fetched_at: float, *, connection: Any = None) -> None: ...
def get_lyrics(self, track_id: int) -> str | None: ...
```

`adapters/state/sqlite.py`：按现有 schema 迁移模式加表（先读文件确认 `_schema`/迁移写法）：

```sql
CREATE TABLE IF NOT EXISTS lyrics (
    track_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL
)
```

Repository 方法（连接辅助以 sqlite.py 现有写法为准）：

```python
def save_lyrics(self, track_id, payload, fetched_at, *, connection=None):
    with self._connection(connection) as conn:
        conn.execute(
            "INSERT INTO lyrics (track_id, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(track_id) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (track_id, payload, fetched_at),
        )

def get_lyrics(self, track_id):
    with self._connection(connection=None) as conn:
        row = conn.execute("SELECT payload FROM lyrics WHERE track_id = ?", (track_id,)).fetchone()
        return row[0] if row else None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_state_lyrics.py -q && python -m pytest tests/ -q`
Expected: PASS（新用例 + 全量回归）

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/ports/state.py src/musicvault/adapters/state/sqlite.py tests/test_state_lyrics.py
git commit -m "feat: SQLite lyrics 表与 save/get 存取"
```

---

### Task 6: PresetRegistry 双注册与依赖注入

**Files:**
- Modify: `src/musicvault/preset_api/v1.py`（PresetRegistry 扩展）
- Test: `tests/test_preset_registry.py`

**Interfaces:**
- Consumes: `PresetRegistration`/`TargetRegistration`/`PresetLoadError`（Task 4）
- Produces（PresetRegistry 新方法，现有 `get`/`load_directories`/`_load_script` 保留）：
  - `register_preset(registration: PresetRegistration) -> PresetRegistration` — 跨类查重名
  - `register_target(registration: TargetRegistration) -> TargetRegistration` — 跨类查重名
  - `preset_registrations(*, enabled_only: bool = False) -> tuple[PresetRegistration, ...]`
  - `target_registrations(*, enabled_only: bool = False) -> tuple[TargetRegistration, ...]`
  - `create_preset(name: str) -> Any`
  - `create_target(name: str) -> Any` — 校验 `depends_on` 缺失（抛 `PresetLoadError` 含依赖名）→ `factory({dep: create_preset(dep), ...})`
  - 现有 `register()`/`registrations()` 保留并重映射为 target 语义（兼容现有 TargetSynchronizer 脚本与调用）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_preset_registry.py
import pytest
from musicvault.preset_api.v1 import (
    PresetLoadError, PresetRegistration, PresetRegistry, TargetRegistration,
)


def test_register_and_create_preset():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="a", factory=dict))
    assert registry.create_preset("a") == {}


def test_target_dependency_injection():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="a", factory=dict))
    captured: dict = {}

    def factory(presets):
        captured["presets"] = presets
        return object()

    registry.register_target(TargetRegistration(name="t", factory=factory, depends_on=("a",)))
    registry.create_target("t")
    assert captured["presets"] == {"a": {}}


def test_missing_dependency_raises():
    registry = PresetRegistry()
    registry.register_target(TargetRegistration(name="t", factory=lambda p: p, depends_on=("nope",)))
    with pytest.raises(PresetLoadError, match="nope"):
        registry.create_target("t")


def test_duplicate_names_rejected_across_kinds():
    registry = PresetRegistry()
    registry.register_preset(PresetRegistration(name="x", factory=dict))
    with pytest.raises(PresetLoadError):
        registry.register_target(TargetRegistration(name="x", factory=dict))


def test_legacy_register_maps_to_target():
    registry = PresetRegistry()
    registry.register("t", factory=lambda: object())
    assert [r.name for r in registry.target_registrations()] == ["t"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_preset_registry.py -q`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: 实现（PresetRegistry 类内）**

```python
def __init__(self) -> None:
    self._registrations: dict[str, PresetRegistration] = {}
    self._target_registrations: dict[str, TargetRegistration] = {}
    self._loading_source: str | None = None

def register_preset(self, registration: PresetRegistration) -> PresetRegistration:
    if registration.api_version != API_VERSION:
        raise PresetLoadError(
            f"preset '{registration.name}' 使用不兼容的 API {registration.api_version}，"
            f"当前支持 {API_VERSION}（来源：{registration.source}）"
        )
    previous = self._registrations.get(registration.name) or self._target_registrations.get(registration.name)
    if previous is not None:
        raise PresetLoadError(f"发现同名 preset '{registration.name}'：{previous.source} 与 {registration.source}")
    self._registrations[registration.name] = registration
    return registration

def register_target(self, registration: TargetRegistration) -> TargetRegistration:
    if registration.api_version != API_VERSION:
        raise PresetLoadError(
            f"sync_target '{registration.name}' 使用不兼容的 API {registration.api_version}，"
            f"当前支持 {API_VERSION}（来源：{registration.source}）"
        )
    previous = self._registrations.get(registration.name) or self._target_registrations.get(registration.name)
    if previous is not None:
        raise PresetLoadError(f"发现同名 sync_target '{registration.name}'：{previous.source} 与 {registration.source}")
    self._target_registrations[registration.name] = registration
    return registration

def preset_registrations(self, *, enabled_only: bool = False) -> tuple[PresetRegistration, ...]:
    values = sorted(self._registrations.values(), key=lambda item: item.name)
    return tuple(item for item in values if item.enabled) if enabled_only else tuple(values)

def target_registrations(self, *, enabled_only: bool = False) -> tuple[TargetRegistration, ...]:
    values = sorted(self._target_registrations.values(), key=lambda item: item.name)
    return tuple(item for item in values if item.enabled) if enabled_only else tuple(values)

def create_preset(self, name: str) -> Any:
    registration = self._registrations.get(name)
    if registration is None:
        raise PresetLoadError(f"未找到 preset：{name}")
    return registration.create()

def create_target(self, name: str) -> Any:
    registration = self._target_registrations.get(name)
    if registration is None:
        raise PresetLoadError(f"未找到 sync_target：{name}")
    missing = [dep for dep in registration.depends_on if dep not in self._registrations]
    if missing:
        raise PresetLoadError(
            f"sync_target '{name}' 依赖的 preset 未注册：{', '.join(missing)}（来源：{registration.source}）"
        )
    presets = {dep: self.create_preset(dep) for dep in registration.depends_on}
    return registration.factory(presets)

def register(self, registration, factory=None, *, api_version=API_VERSION, enabled=True, source=None, target=None):
    # 兼容现有 TargetSynchronizer 脚本：register() 语义 = register_target
    if isinstance(registration, str):
        registration = TargetRegistration(
            name=registration, factory=factory, api_version=api_version, enabled=enabled,
            source=source or self._loading_source or "<runtime>", target=target,
        )
    elif source is not None or target is not None:
        raise TypeError("传入 TargetRegistration 时不能重复指定 source 或 target")
    return self.register_target(registration)

def registrations(self, *, enabled_only: bool = False) -> tuple[PresetRegistration, ...]:
    # 兼容现有调用：返回 target 注册列表
    return self.target_registrations(enabled_only=enabled_only)
```

注意：现有 `register()` 的 `_loading_source` 替换逻辑已并入上面的 str 分支；`get()` 继续按原名查询（现有行为保留）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_preset_registry.py -q && python -m pytest tests/ -q`
Expected: 新测试 PASS；全量回归通过（register()/registrations() 兼容）

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/preset_api/v1.py tests/test_preset_registry.py
git commit -m "feat: PresetRegistry 双注册与 sync_target 依赖注入"
```

---

### Task 7: 内置脚本（ArchivePreset + HardlinkDistributor）+ PresetContext.lyrics_file

**Files:**
- Modify: `src/musicvault/preset_api/v1.py`（PresetContext 加 `media_store_root` 构造参数与 `lyrics_file` 方法）
- Modify: `src/musicvault/preset_api/builtins.py`（重写）
- Test: `tests/test_builtin_hardlink.py`

**Interfaces:**
- Consumes: `BasePreset`/`MetadataSpec`/`Quality`/`AudioFormat`/`TargetRegistration`/`PresetRegistration`/`audio_spec_key`/`PresetLoadError`（Task 4/6）、`enhanced_lrc`（Task 2）、`PresetContext`（现有）
- Produces:
  - `PresetContext.media_store_root: Path | None` 构造参数（`__post_init__` 保留）；`lyrics_file(track_id: int, preset_name: str) -> Path | None` — `media_store/<tid>/{tid}.{preset_name}.lrc` 存在才返回，root 为 None 返回 None
  - `class ArchivePreset(BasePreset)` — `quality=Quality.HIRES`、`format=AudioFormat.FLAC`、`metadata=MetadataSpec.full()`、`build_lyrics` = `enhanced_lrc(lines, include_translation=True, include_romaji=True)`
  - `class HardlinkDistributor` — `__init__(self, preset: BasePreset, preset_name: str, target_root: Path, default_name: str = "未分类")`；`prepare(context)`/`sync_item(track, context)`/`finalize(context)`（幂等语义见下）
  - `register_builtin_presets(registry: PresetRegistry, target_root: str | Path, default_playlist_name: str = "未分类") -> None` — 注册 preset "archive" + target "hardlink"（`depends_on=("archive",)`，工厂闭包注入 `HardlinkDistributor(presets["archive"], "archive", target_root, default_playlist_name)`）

**HardlinkDistributor 幂等语义：**
- `sync_item`：`spec_key = audio_spec_key(preset.format, preset.bitrate)`；`asset = context.media_asset(track.id, spec=spec_key)`，无资产则跳过；`lrc = context.lyrics_file(track.id, preset_name)`；所属歌单 = `{safe_filename(pl.name) for pl in context.playlists if track.id in pl.track_ids}`，空则 `{safe_filename(default_name)}`；先删除目标根下**非所属目录**中 inode 与 canonical（音频/歌词）一致的文件（`st_dev+st_ino` 匹配），再在所属目录建缺失链接（音频 + 歌词，文件名 `format_track_name("{artist} - {name}", track)` + 后缀）
- `finalize`：删除目标根下**不在快照歌单集合且非 default_name** 的目录（rmtree）；快照外孤儿文件一律保留

- [ ] **Step 1: 写失败测试**

```python
# tests/test_builtin_hardlink.py
from pathlib import Path

from musicvault.domain.models import MediaAsset, Playlist, SourceSnapshot, Track
from musicvault.preset_api.builtins import ArchivePreset, HardlinkDistributor, register_builtin_presets
from musicvault.preset_api.v1 import PresetContext, PresetRegistry


class FakeTarget:
    def __init__(self):
        self.links = []

    def link(self, src, dst):
        self.links.append((Path(src), Path(dst)))

    def copy(self, src, dst):
        pass

    def write_text(self, dst, content, encoding="utf-8"):
        pass


def _make_context(snapshot, media_store_root):
    return PresetContext(snapshot=snapshot, target=FakeTarget(), dry_run=False, media_store_root=media_store_root)


def test_archive_preset_declares_flac_full_metadata():
    preset = ArchivePreset()
    assert preset.format.value == "flac"
    assert preset.quality.value == "hires"
    assert preset.metadata.embed_cover is True
    assert preset.build_lyrics(()) == ""


def test_hardlink_links_audio_and_lyrics_to_owned_playlist(tmp_path):
    media = tmp_path / "media_store"
    audio_dir = media / "1"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "1.flac"
    audio.write_bytes(b"FLAC")
    (audio_dir / "1.archive.lrc").write_bytes(b"LRC")
    library = tmp_path / "library"
    library.mkdir()

    snapshot = SourceSnapshot(
        tracks=(Track(id=1, name="song", artists=[], album="", raw={}),),
        playlists=(Playlist(1, "fav", (1,)),),
        media_assets=(MediaAsset(track_id=1, asset_type="audio", spec_key="FLAC", path=audio, size=4),),
    )
    context = _make_context(snapshot, media)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.sync_item(snapshot.tracks[0], context)

    assert len(context.target.links) == 2
    assert context.target.links[0][1] == library / "fav" / "song.flac"
    assert context.target.links[1][1] == library / "fav" / "song.lrc"


def test_hardlink_removes_stale_link_on_playlist_change(tmp_path):
    media = tmp_path / "media_store"
    audio_dir = media / "1"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "1.flac"
    audio.write_bytes(b"FLAC")
    library = tmp_path / "library"
    old_dir = library / "old"
    old_dir.mkdir(parents=True)
    old_link = old_dir / "song.flac"
    import os
    os.link(audio, old_link)

    snapshot = SourceSnapshot(
        tracks=(Track(id=1, name="song", artists=[], album="", raw={}),),
        playlists=(Playlist(1, "fav", (1,)),),
        media_assets=(MediaAsset(track_id=1, asset_type="audio", spec_key="FLAC", path=audio, size=4),),
    )
    context = _make_context(snapshot, media)
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.sync_item(snapshot.tracks[0], context)

    assert not old_link.exists()
    assert (library / "fav" / "song.flac").exists()


def test_hardlink_finalize_removes_stale_playlist_dirs(tmp_path):
    library = tmp_path / "library"
    stale = library / "old_playlist"
    stale.mkdir(parents=True)
    (stale / "x.flac").write_bytes(b"x")
    snapshot = SourceSnapshot(tracks=(), playlists=(Playlist(1, "fav", ()),), media_assets=())
    context = _make_context(snapshot, tmp_path / "media_store")
    distributor = HardlinkDistributor(ArchivePreset(), "archive", library, "未分类")
    distributor.finalize(context)
    assert not stale.exists()
    assert (library / "未分类").exists()  # 未分类目录绝不被 finalize 删除


def test_register_builtin_registers_both_kinds():
    registry = PresetRegistry()
    register_builtin_presets(registry, Path("library"))
    names = {r.name for r in registry.preset_registrations()} | {r.name for r in registry.target_registrations()}
    assert names == {"archive", "hardlink"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_builtin_hardlink.py -q`
Expected: FAIL（ImportError / ArchivePreset 不存在）

- [ ] **Step 3: 实现**

`preset_api/v1.py` — `PresetContext` 追加字段与方法：

```python
@dataclass(slots=True)
class PresetContext:
    snapshot: SourceSnapshot
    target: TargetOperations
    dry_run: bool = False
    target_descriptor: TargetDescriptor | None = None
    media_resolver: MediaResolver | None = None
    media_store_root: Path | None = None
    _executor: OperationExecutor = field(init=False, repr=False)

    def lyrics_file(self, track_id: int, preset_name: str) -> Path | None:
        if self.media_store_root is None:
            return None
        candidate = self.media_store_root / str(track_id) / f"{track_id}.{preset_name}.lrc"
        return candidate if candidate.is_file() else None
```

`preset_api/builtins.py` 重写：

```python
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping

from musicvault.domain.models import TargetDescriptor
from musicvault.preset_api.render import enhanced_lrc
from musicvault.preset_api.v1 import (
    AudioFormat, BasePreset, MetadataSpec, PresetRegistration, PresetRegistry,
    Quality, TargetRegistration, audio_spec_key,
)
from musicvault.shared.utils import format_track_name, safe_filename


class ArchivePreset(BasePreset):
    quality = Quality.HIRES
    format = AudioFormat.FLAC
    metadata = MetadataSpec.full()

    def build_lyrics(self, lines):
        return enhanced_lrc(lines, include_translation=True, include_romaji=True)


class HardlinkDistributor:
    """按歌单目录硬链接分发指定 preset 的音频与歌词（按曲目幂等）。"""

    def __init__(self, preset: BasePreset, preset_name: str, target_root: Path, default_name: str = "未分类") -> None:
        self.preset = preset
        self.preset_name = preset_name
        self.target_root = Path(target_root)
        self.default_name = default_name
        self.filename_template = "{artist} - {name}"

    def prepare(self, context) -> None:
        return None

    def sync_item(self, track, context) -> None:
        spec_key = audio_spec_key(self.preset.format, self.preset.bitrate)
        asset = context.media_asset(track.id, spec=spec_key)
        if asset is None:
            return None
        lrc = context.lyrics_file(track.id, self.preset_name)
        owned_names = {safe_filename(pl.name) for pl in context.playlists if track.id in pl.track_ids}
        if not owned_names:
            owned_names = {safe_filename(self.default_name)}

        self._remove_stale_links(asset.path, lrc, owned_names)
        stem = format_track_name(self.filename_template, track)
        for dirname in owned_names:
            dst_dir = self.target_root / dirname
            dst_dir.mkdir(parents=True, exist_ok=True)
            context.link(asset.path, dst_dir / f"{stem}{asset.path.suffix}")
            if lrc is not None:
                context.link(lrc, dst_dir / f"{stem}.lrc")
        return None

    def _remove_stale_links(self, audio_path: Path, lrc_path: Path | None, owned_names: set[str]) -> None:
        inodes = {inode for inode in (self._inode(audio_path), self._inode(lrc_path)) if inode is not None}
        if not inodes or not self.target_root.is_dir():
            return
        for child in self.target_root.iterdir():
            if not child.is_dir() or child.name in owned_names:
                continue
            for f in list(child.iterdir()):
                if f.is_file() and self._inode(f) in inodes:
                    f.unlink(missing_ok=True)

    @staticmethod
    def _inode(path: Path | None) -> tuple[int, int] | None:
        if path is None:
            return None
        try:
            st = path.stat()
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    def finalize(self, context) -> None:
        snapshot_names = {safe_filename(pl.name) for pl in context.playlists}
        default = safe_filename(self.default_name)
        if not self.target_root.is_dir():
            return None
        for child in list(self.target_root.iterdir()):
            if child.is_dir() and child.name != default and child.name not in snapshot_names:
                shutil.rmtree(child, ignore_errors=True)
        return None


def register_builtin_presets(
    registry: PresetRegistry,
    target_root: str | Path,
    default_playlist_name: str = "未分类",
) -> None:
    target_root_path = Path(target_root)
    registry.register_preset(PresetRegistration(name="archive", factory=ArchivePreset, source="builtin:archive"))

    def hardlink_factory(presets: Mapping[str, object]):
        preset = presets["archive"]
        return HardlinkDistributor(preset, "archive", target_root_path, default_playlist_name)

    registry.register_target(
        TargetRegistration(
            name="hardlink",
            factory=hardlink_factory,
            depends_on=("archive",),
            source="builtin:hardlink",
            target=TargetDescriptor(
                identifier="hardlink",
                target_type="filesystem",
                deletion_policy="append",
            ),
        )
    )
```

注意：`context.media_asset(track.id, spec=...)` 的 spec 参数与现有 `SnapshotMediaResolver` 的匹配规则（`MediaRequest(track_id, asset_type, spec)`）一致，spec_key 为 "FLAC" 时匹配资产表 spec_key 列。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_builtin_hardlink.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/preset_api/builtins.py src/musicvault/preset_api/v1.py tests/test_builtin_hardlink.py
git commit -m "feat: 内置 archive preset 与 hardlink 幂等分发 sync_target"
```

---

### Task 8: NeteaseClient Quality 枚举化

**Files:**
- Modify: `src/musicvault/adapters/providers/netease_client.py`
- Test: `tests/test_netease_client.py`（更新现有构造调用）

**Interfaces:**
- Consumes: `Quality`（Task 4）
- Produces:
  - `NeteaseClient.__init__(..., download_quality: Quality = Quality.HIRES, ...)` — 字段类型从 `str` 改 `Quality`
  - `get_tracks_download_urls` 中 `level=self.download_quality.value`

- [ ] **Step 1: 更新现有测试**

现有测试中 `NeteaseClient(download_quality="hires")` 之类调用改为 `download_quality=Quality.HIRES`；新增断言：

```python
def test_download_quality_enum_value_passed_to_sdk(fake_sdk):
    client = NeteaseClient(download_quality=Quality.LOSSLESS)
    # fake_sdk 记录 song_url_v1 调用的 level 参数
    client.get_tracks_download_urls([1])
    assert fake_sdk.last_level == "lossless"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_netease_client.py -q`
Expected: FAIL（构造签名/枚举断言）

- [ ] **Step 3: 实现**

```python
from musicvault.preset_api.v1 import Quality  # 顶部 import 追加

# __init__ 签名：
# download_quality: Quality = Quality.HIRES
# self.download_quality = download_quality

# get_tracks_download_urls 内：
resp = _retry_api(self._api().song_url_v1, id=ids_csv, level=self.download_quality.value)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_netease_client.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/adapters/providers/netease_client.py tests/test_netease_client.py
git commit -m "refactor: NeteaseClient 下载音质枚举化（Quality）"
```

---

### Task 9: Organizer AudioFormat 枚举化

**Files:**
- Modify: `src/musicvault/adapters/processors/organizer.py`
- Test: `tests/test_organizer.py`（更新现有，读现有测试后适配）

**Interfaces:**
- Consumes: `AudioFormat`（Task 4）
- Produces:
  - `_LOSSY_SUFFIX_MAP`/`_LOSSY_CODEC_MAP` 键改 `AudioFormat`（值不变）；`_format_to_ext(fmt)`/`_spec_to_filename(track_id, fmt, bitrate, same_format_count, source_suffix)` 内部用 `fmt.value`
  - `route_audio(src: Path, track: Track, output_dir: Path, audio_specs: set[tuple[AudioFormat | None, str | None]], force: bool = False) -> dict[tuple[AudioFormat | None, str | None], Path]` — 签名不变（元素类型从 str 改枚举）

- [ ] **Step 1: 更新现有测试**

```python
# 现有 route_audio 用例的 specs 改为枚举：
specs = {(AudioFormat.FLAC, None), (AudioFormat.MP3, "192k")}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_organizer.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
_LOSSY_SUFFIX_MAP = {AudioFormat.MP3: ".mp3", AudioFormat.AAC: ".m4a", AudioFormat.OGG: ".ogg", AudioFormat.OPUS: ".opus"}
_LOSSY_CODEC_MAP = {AudioFormat.MP3: "libmp3lame", AudioFormat.AAC: "aac", AudioFormat.OGG: "libvorbis", AudioFormat.OPUS: "libopus"}

def _format_to_ext(fmt: AudioFormat | None, source_suffix: str) -> str:
    if fmt is None:
        return source_suffix
    return _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt.value}")

def _spec_to_filename(track_id, fmt, bitrate, same_format_count, source_suffix=".mp3"):
    if fmt is None:
        return f"{track_id}{source_suffix}"
    ext = _LOSSY_SUFFIX_MAP.get(fmt, f".{fmt.value}")
    if bitrate:
        return f"{track_id}_{bitrate}{ext}"
    return f"{track_id}{ext}"
```

`route_audio` 内部 `_count_same_formats`、`_transcode_lossy(..., fmt, ...)` 的 codec 查找保持（键已枚举化）；`fmt == "flac"` 分支改为 `fmt is AudioFormat.FLAC`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_organizer.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/adapters/processors/organizer.py tests/test_organizer.py
git commit -m "refactor: Organizer 转码格式枚举化（AudioFormat）"
```

---

### Task 10: MetadataWriter 去嵌入歌词 + MetadataSpec 驱动

**Files:**
- Modify: `src/musicvault/adapters/processors/metadata_writer.py`
- Test: `tests/test_metadata_writer.py`（更新现有）

**Interfaces:**
- Consumes: `MetadataSpec`（Task 4）
- Produces:
  - `MetadataWriter.write(audio_file: Path, track: Track, *, metadata: MetadataSpec, cover_timeout: int = 15) -> None`
  - 移除 `lyric_text`/`embed_lyrics` 参数与 USLT（MP3）/`audio["lyrics"]`+`description`（FLAC）写入分支
  - `embed_cover = metadata.embed_cover`、`cover_max_size = metadata.cover_max_size`、`metadata_fields = frozenset(metadata.fields)`（空集 → `_build_extra_metadata` 现有"返回全部 extra"语义保留）

- [ ] **Step 1: 更新现有测试**

```python
# 现有 metadata.write(..., lyric_text=..., embed_lyrics=..., metadata_fields=...) 调用改为：
metadata.write(audio_file, track, metadata=MetadataSpec.full(), cover_timeout=15)

# 新增断言：
def test_write_none_spec_writes_no_cover_no_extras(tmp_path, track_without_cover):
    # MetadataSpec.none() → 无 APIC/无 year 等 extra；基础标题/艺术家/专辑仍写
def test_write_basic_spec_writes_full_extras(tmp_path, track):
    # MetadataSpec.basic()（fields=()）→ 现有语义：extra 全写（年份/曲号等）
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_metadata_writer.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`write()` 方法体：`_write_mp3(audio_file, track, cover_data, metadata_fields)` / `_write_flac(...)`；删除 `lyric_text`/`embed_lyrics` 分支；`_write_mp3`/`_write_flac` 删除相应参数与 USLT/lyrics 写入段；顶部 import 去掉不再使用的 `USLT`（保留其他 frame 类）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_metadata_writer.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/adapters/processors/metadata_writer.py tests/test_metadata_writer.py
git commit -m "refactor: MetadataWriter 去除内嵌歌词，MetadataSpec 驱动元数据粒度"
```

---

### Task 11: SyncUseCase 拆 fetch/pull 阶段 + media_store 扁平化

**Files:**
- Modify: `src/musicvault/application/sync_use_case.py`
- Test: `tests/test_sync_use_case.py`（更新现有，读现有 fake 模式后扩展）

**Interfaces:**
- Consumes: `convert_lyrics_payload`（Task 3）、`lyrics_to_json`（Task 1）、`StateRepository.save_lyrics`（Task 5）、`Playlist`（现有 models）
- Produces:
  - `run_fetch(cookie: str, playlist_ids: list[int]) -> None` — 登录 → 拉歌单详情/曲目列表 → 检测改名（**仅** `state.upsert_playlist` 更新 SQLite 名称）→ 单独管理单曲详情 → `_record_source_state`；**不下载、不碰 library**、无 dry-run 分支（纯元数据写 SQLite，dry-run 语义由 PipelineUseCase 层处理）
  - `run_pull(cookie: str, playlist_ids: list[int], *, progress=None) -> SyncResult` — `_cleanup_stale_state` → `_diff_tracks` → 下载（质量由 client 决定）→ 下载成功后对每个新曲目 `_save_lyrics(track_id)` → `_prune_stale_tracks`（**仅** canonical+SQLite，删除 library 链接遍历段）→ `_record_source_state`
  - 删除：`run_sync`（调用方改 fetch+pull）、`_handle_playlist_rename`（library 操作段，保留改名登记到 SQLite）、`_reconcile_playlist_assignments`、`_create_track_links`、`_remove_track_links`、`_link_name`、`_resolve_dry_urls` 中的 dry-run 流程简化（dry-run 由 Pipeline 统一处理：fetch 无条件执行——元数据写 SQLite 在 dry-run 下不执行，pull 的 dry-run 只计算）
  - `find_canonical_for_spec(track_id, spec_key) -> Path | None` 保留，路径改为 `media_store/<tid>/`（**扁平化**：`audio_dir = media_store / str(track_id)`）
  - `_prune_stale_tracks`：`audio_dir` 改为 `media_store / str(track_id)`（rmtree 整个 track 目录）；inode 收集后**删除** library 遍历删除段
  - `_save_lyrics(track_id: int) -> None`：

```python
def _save_lyrics(self, track_id: int) -> None:
    try:
        payload = self.api.get_track_lyrics(track_id)
        lines = convert_lyrics_payload(payload)
    except Exception as error:  # noqa: BLE001 - 歌词失败降级，不阻塞下载
        logger.warning("获取歌词失败 track_id=%s：%s", track_id, error)
        lines = ()
    self.recorder.state.save_lyrics(track_id, lyrics_to_json(lines), time.time())
```

- `SyncResult` 保留（downloaded/added/no_url/pruned/track_count/playlist_count）；dry_run_plan 保留（run_pull 内计算）

- [ ] **Step 1: 更新/新增测试**

```python
# 追加到现有测试文件（fake 模式沿用现有）
def test_pull_stores_lyrics(fake_state, fake_api, cfg):
    fake_api.get_track_lyrics.return_value = {"lrc": "[00:01.000]hello"}
    # 运行 run_pull(...) 后：
    assert fake_state.get_lyrics(track_id) is not None
    assert lyrics_from_json(fake_state.get_lyrics(track_id)) == (LyricLine(1000, 0, "hello"),)


def test_pull_lyrics_failure_degrades_to_empty(fake_state, fake_api, cfg):
    fake_api.get_track_lyrics.side_effect = RuntimeError("boom")
    # 运行后断言 get_lyrics(track_id) == lyrics_to_json(())


def test_fetch_does_not_call_download_urls(fake_state, fake_api, cfg):
    # 运行 run_fetch 后断言 fake_api.get_tracks_download_urls 未被调用
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_sync_use_case.py -q`
Expected: FAIL（run_fetch/run_pull 不存在）

- [ ] **Step 3: 实现**

`run_fetch`：由原 `run_sync` 前半段提取（登录、playlist_index 构建、`get_playlist_info`/`get_playlist_tracks`、managed_songs 详情、`_record_source_state`）；改名处理简化为 `self.recorder.state.upsert_playlist(Playlist(pid, new_name, ()))`（library 目录迁移由 distribute 幂等覆盖）。`run_pull`：由原 `run_sync` 后半段提取（`_cleanup_stale_state`、`_diff_tracks`、`_sync_tracks`、`_save_lyrics` 对每个 downloaded track、`_prune_stale_tracks`、`_record_source_state`）。`_sync_tracks` 删除 `track_playlists` 参数与 `item.playlist_ids = ...` 赋值。路径扁平化：`self.paths.media_store / str(track_id)`（删除 `/ "audio"` 段，共 3 处：`find_canonical_for_spec`、`_prune_stale_tracks`、`_cleanup_stale_state` 的 assets 检查用快照路径无需改）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_sync_use_case.py -q && python -m pytest tests/ -q`
Expected: PASS（更新过的旧测试 + 全量）

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/application/sync_use_case.py tests/test_sync_use_case.py
git commit -m "refactor: SyncUseCase 拆 fetch/pull，歌词统一格式入库，media_store 扁平化，移除 library 链接操作"
```

---

### Task 12: ProcessUseCase 重构（离线歌词 + preset 函数 + 移除导出）

**Files:**
- Modify: `src/musicvault/application/process_use_case.py`
- Modify: `src/musicvault/adapters/processors/lyrics.py`（删除 `StandardLyrics`/`KaraokeLyrics` 类与文本级渲染函数）
- Test: `tests/test_process_use_case.py`（更新现有）

**Interfaces:**
- Consumes: `BasePreset`/`audio_spec_key`/`MetadataSpec`/`LyricEncoding`（Task 4）、`lyrics_from_json`（Task 1）、`StateRepository.get_lyrics`（Task 5）、Task 9/10 的 organizer/metadata 新签名
- Produces:
  - `ProcessUseCase.__init__(..., presets: Mapping[str, BasePreset] | None = None, ...)` — 新参数（名称→实例）；None 时从 `cfg.presets` 兼容回退（构造 `{p.name: p}`，Task 17 移除）
  - `run_process(downloaded: list[DownloadedTrack], force: bool, *, progress=None) -> ProcessResult` — **移除 `playlist_index` 参数**
  - `_process_file` 重构（核心逻辑见 Step 3）：解密 → `route_audio`（specs = `{(p.format, p.bitrate) for p in presets.values()}`，输出到 `media_store/<tid>/` **扁平化**）→ `state.get_lyrics(track_id)` → `lyrics_from_json` → 元数据（按 spec 并集 MetadataSpec）→ 歌词文件（每 preset `build_lyrics(lines)`，非空写 `media_store/<tid>/{tid}.{preset_name}.lrc`，编码序列尝试）
  - 删除：`_link_track`、`_process_local`、`_iter_downloads`、`_scan_canonical_files`、`_build_track_playlist_map`、`_resolve_playlist_names`、`_pick_best_lyric`、`_build_lyrics_for_preset`、`KaraokeLyrics`/`StandardLyrics` import
  - `_write_lrc(target, lyric_text, encodings: tuple[LyricEncoding, ...] = (LyricEncoding.UTF_8,))` — 用 `.value` 序列尝试
  - `_filter_pending`/`_mark_processed` 保留（spec 覆盖跳过 + processed 索引，`compute_preset_hash` 不再依赖 `domain/preset.py`——`_mark_processed` 改为 `compute_preset_hash([...preset 声明...])` 或固定时间戳记录，简化：改为 `record_processed(track_id, "preset-script", time.time())`）

- [ ] **Step 1: 更新/新增测试**

```python
# 核心新用例
def test_process_writes_lyrics_file_from_preset(tmp_path, fake_state, fake_api):
    # fake_state.get_lyrics(track_id) 返回 lyrics_to_json((LyricLine(1000, 0, "hello"),))
    # presets={"custom": MyPreset()}，MyPreset.build_lyrics 返回 "custom lrc"
    # 运行 run_process → 断言 media_store/<tid>/<tid>.custom.lrc 内容 == "custom lrc"


def test_process_empty_lyrics_skips_file(tmp_path, fake_state, fake_api):
    # get_lyrics 返回 None → media_store/<tid>/ 下无 .lrc 文件


def test_process_metadata_spec_union(tmp_path, fake_state, fake_api):
    # 两个 preset 共享 spec（如都 FLAC）：embed_cover 并集、fields 并集 → metadata.write 收到并集（fake metadata 记录参数断言）
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_process_use_case.py -q`
Expected: FAIL

- [ ] **Step 3: 实现（`_process_file` 核心）**

```python
def _process_file(self, raw_file, prefetched_track=None, force=False):
    track_info = prefetched_track
    track_id = prefetched_track.id if prefetched_track else None
    if track_info is None:
        track_id = self._guess_track_id(raw_file)
        if track_id is None:
            raise RuntimeError(f"无法推断 track_id：{raw_file.name}")
        track_info = self._safe_track(track_id, raw_file.stem)
    if track_id is None:
        raise RuntimeError(f"无法推断 track_id：{raw_file.name}")

    # 年份回退（保留现有逻辑）
    if not track_info.raw.get("publishTime"):
        ...

    audio_specs = {(p.format, p.bitrate) for p in self.presets.values()}
    track_dir = self.paths.media_store / str(track_id)
    is_canonical = raw_file.parent == track_dir and raw_file.stem.split("_")[0] == str(track_id)

    if is_canonical:
        audio_map = {}
        existing_spec = self._spec_from_canonical(raw_file)
        if existing_spec:
            audio_map[audio_spec_key(*existing_spec)] = raw_file
        for spec in audio_specs:
            key = audio_spec_key(*spec)
            if key not in audio_map:
                result = self.organizer.route_audio(raw_file, track_info, track_dir, {spec}, force=force)
                if spec in result:
                    audio_map[key] = result[spec]
    else:
        downloaded = DownloadedTrack(
            track=track_info, source_file=str(raw_file), is_ncm=raw_file.suffix.lower() == ".ncm"
        )
        decoded = self.decryptor.decrypt_if_needed(downloaded, self.paths.cache / "decoded")
        raw_result = self.organizer.route_audio(decoded, track_info, track_dir, audio_specs, force=force)
        audio_map = {audio_spec_key(fmt, br): p for (fmt, br), p in raw_result.items()}

    payload = self.recorder.state.get_lyrics(track_id)
    lines = lyrics_from_json(payload) if payload else ()

    spec_presets: dict[str, list[BasePreset]] = {}
    for name, preset in self.presets.items():
        spec_presets.setdefault(audio_spec_key(preset.format, preset.bitrate), []).append(preset)

    for spec_key, canon_path in audio_map.items():
        presets_for_spec = spec_presets.get(spec_key, [])
        merged = MetadataSpec(
            embed_cover=any(p.metadata.embed_cover for p in presets_for_spec),
            cover_max_size=max((p.metadata.cover_max_size for p in presets_for_spec), default=0),
            fields=tuple(sorted(set().union(*(set(p.metadata.fields) for p in presets_for_spec)))),
        )
        self.metadata.write(canon_path, track_info, metadata=merged, cover_timeout=self.cfg.network_cover_timeout)

    for preset_name, preset in self.presets.items():
        lyric_text = preset.build_lyrics(lines)
        if not lyric_text:
            continue
        lrc_path = track_dir / f"{track_id}.{preset_name}.lrc"
        _write_lrc(lrc_path, lyric_text, encodings=preset.lyrics_encodings)

    if not is_canonical and not self.cfg.keep_downloads:
        raw_file.unlink(missing_ok=True)
    return audio_map
```

`_write_lrc` 枚举化与 `_mark_processed` 简化（`record_processed(track_id, "preset-script", time.time())`）。`lyrics.py` 中删除 `StandardLyrics`/`KaraokeLyrics` 类与 `_merge_lrc_translation`/`_render_karaoke_*`/`write_gb18030_lrc` 等文本级渲染函数（保留 `_sanitize_lyrics_text`/`_parse_yrc_line`/`_parse_line`/`_build_translation_map`/`_time_tag_to_ms`/`_ms_to_time_tag`/`_find_translation_fuzzy`/`_is_same_text` 供 Task 3 的转换器使用）。先 grep 确认无其他引用（`write_gb18030_lrc` 若被其他模块引用则一并处理）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_process_use_case.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/application/process_use_case.py src/musicvault/adapters/processors/lyrics.py tests/test_process_use_case.py
git commit -m "refactor: ProcessUseCase 离线歌词消费与 preset 歌词函数，移除 library 链接与本地模式"
```

---

### Task 13: SyncEngine + bootstrap（target 注册表 + preset 注入）

**Files:**
- Modify: `src/musicvault/application/sync_engine.py`
- Modify: `src/musicvault/application/bootstrap.py`
- Test: `tests/test_sync_engine.py`（更新）、`tests/test_bootstrap.py`（如有）

**Interfaces:**
- Consumes: `TargetRegistration`/`PresetRegistry`/`Quality`/`PresetContext.media_store_root`（Task 4/6/7）、`register_builtin_presets`（Task 7）
- Produces:
  - `SyncEngine.run(snapshot: SourceSnapshot, registrations: Iterable[TargetRegistration], *, selected: set[str] | None = None, presets: Mapping[str, object] | None = None) -> SyncRunResult` — `_run_preset` 中构造 `PresetContext(..., media_store_root=...)` 并注入：`registration.factory({dep: presets[dep] for dep in registration.depends_on})`；`presets` 为 None 时 `registration.factory({})`（depends_on 非空时抛 `PresetLoadError`）
  - `build_runtime(config)`：`register_builtin_presets(registry, config.library_dir, config.default_playlist_name)`（替换原 `register_builtin_presets(registry, paths.library / "playlist_links")`）→ `load_directories` → state 登记（`kind="preset"`/`"target"` 区分，见 SQLite presets 表 kind 处理——本任务先按现有 `register_preset` 签名调用，kind 区分在 Task 17 的 schema 变更中一并处理或本任务内加列）→ `Runtime(paths, state, presets=registry)`（字段不变）
  - `build_source_client(config, download_quality: Quality | None = None)` — 新参数；None 时从 `config.download_quality`（字符串）转枚举（`Quality(config.download_quality)`，异常回退 `Quality.HIRES`）
  - `build_pipeline(config, source=None, *, dry_run=False)`：构造 `registry = PresetRegistry()` + 注册内置（`config.builtin_scripts_enabled` 时）+ `load_directories` → `presets = {r.name: registry.create_preset(r.name) for r in registry.preset_registrations(enabled_only=True)}` → `download_quality = Quality.maximum(p.quality for p in presets.values())` → source 为 None 时 `build_source_client(config, download_quality)` → `PipelineUseCase(cfg=config, api=source, state=..., dry_run=dry_run, presets=presets, registry=registry, target=FilesystemTarget(WorkspacePaths(config.workspace_path).library))`（Task 14 签名）
  - `build_distribute_pipeline(config, *, dry_run=False)`（`build_target_sync_pipeline` 改名，保留原函数名别名或更新引用）：返回组合含 `Runtime` + `SyncEngine`，`TargetSyncPipeline.run` 传入 `presets` 索引（`{r.name: registry.create_preset(r.name) ...}`）

- [ ] **Step 1: 更新测试**

```python
def test_sync_engine_injects_presets_into_factory():
    # TargetRegistration(factory 记录收到的 presets) + presets={"a": obj}
    # engine.run(snapshot, [registration], presets={"a": obj})
    # 断言 factory 收到的 presets == {"a": obj}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_sync_engine.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`sync_engine.py`：

```python
def run(self, snapshot, registrations, *, selected=None, presets=None):
    ...
    self._run_preset(snapshot, registration, presets or {})

def _run_preset(self, snapshot, registration, presets):
    ...
    missing = [dep for dep in registration.depends_on if dep not in presets]
    if missing:
        return PresetRunResult(name=registration.name, source=registration.source, status=OperationStatus.FAILED,
                               error=f"sync_target '{registration.name}' 依赖的 preset 未提供：{', '.join(missing)}")
    synchronizer = registration.factory({dep: presets[dep] for dep in registration.depends_on})
    context = PresetContext(snapshot=snapshot, target=self.target, dry_run=self.dry_run,
                            target_descriptor=registration.target, media_store_root=self.media_store_root)
    ...
```

`SyncEngine.__init__(self, target: TargetOperations, *, dry_run: bool = False, media_store_root: Path | None = None)`。

`bootstrap.py`：按 Interfaces 描述改写 `build_runtime`/`build_source_client`/`build_pipeline`/`build_distribute_pipeline`；`build_pipeline` 中 `first_template`/`Downloader(filename_template=...)` 移除（downloader 默认模板保留 `"{artist} - {name}"`，其输出仅用于 cache 文件名）。`link_only` 相关代码在 Task 14 删除。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_sync_engine.py -q && python -m pytest tests/test_bootstrap.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/application/sync_engine.py src/musicvault/application/bootstrap.py tests/test_sync_engine.py tests/test_bootstrap.py
git commit -m "feat: SyncEngine 注入 preset 实例，bootstrap 注册内置/推导音质"
```

---

### Task 14: PipelineUseCase 四阶段编排 + sync 选项

**Files:**
- Modify: `src/musicvault/application/pipeline_use_case.py`
- Test: `tests/test_pipeline_use_case.py`（更新现有）

**Interfaces:**
- Consumes: `SyncUseCase.run_fetch`/`run_pull`（Task 11）、`ProcessUseCase.run_process`（Task 12）、`SyncEngine`/`PresetRegistry`（Task 13）、`StateRepository.create_snapshot`
- Produces:
  - `PipelineUseCase.__init__(cfg, api, state, dry_run=False, presets: Mapping[str, BasePreset] | None = None, registry: PresetRegistry | None = None, target: TargetOperations | None = None)` — presets 传 ProcessUseCase；registry+target 用于 distribute 阶段
  - `run_pipeline(cookie: str, *, distribute: bool = True, only_distribute: bool = False, progress=None) -> PipelineResult`
    - `only_distribute`：跳过 fetch/pull/process，直接 distribute；返回结果只含 distribute 信息
    - 否则：`sync_service.run_fetch(cookie, playlist_ids)` → `sync_service.run_pull(...)` → `process_service.run_process(downloaded, force, progress=progress)` → `distribute` 为 True 时 `_run_distribute()`
    - dry-run 下 fetch 不执行（写 SQLite 有副作用）、pull/process 沿用现有 dry-run 语义；distribute 沿用 SyncEngine dry-run
  - `_run_distribute() -> None`：`SyncEngine(target, dry_run=self.dry_run, media_store_root=self.paths.media_store).run(state.create_snapshot(), registry.target_registrations(enabled_only=True), presets=presets)`（target/registry 为 None 时跳过）
  - 删除：`link_only`、`_cleanup_uncategorized_orphans`、`command` 参数、`playlist_index` 传递

- [ ] **Step 1: 更新现有测试**

```python
# 现有 run_pipeline(cookie, command=...) 调用改为 run_pipeline(cookie)；
# 新增：
def test_run_pipeline_only_distribute_skips_download():
    # only_distribute=True 时断言 fake_api.get_tracks_download_urls 未调用、SyncEngine.run 被调


def test_run_pipeline_no_distribute_skips_distribute():
    # distribute=False 时断言 SyncEngine.run 未调用
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pipeline_use_case.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

按 Interfaces 重写 `run_pipeline`（fetch → pull → process → distribute 顺序；`playlist_index = self.sync_service.playlist_index` 不再需要传给 process）。`PipelineResult` 增加 `distributed: int = 0`（distribute 成功 target 数，可选）与 `distribute_status`——保持简单：沿用现有字段，distribute 结果由 CLI 另行渲染（Task 15）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_pipeline_use_case.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/application/pipeline_use_case.py tests/test_pipeline_use_case.py
git commit -m "refactor: PipelineUseCase 四阶段编排（fetch/pull/process/distribute）与 sync 选项"
```

---

### Task 15: CLI 命令收敛

**Files:**
- Modify: `src/musicvault/cli/main.py`
- Modify: `src/musicvault/cli/render.py`（按需）

**Interfaces:**
- `build_parser`：
  - `sync` 加 `--no-distribute`/`--only-distribute`（`argparse` 互斥组）
  - 删除 `pull`/`process` parser（含 `--only-link`）
  - `target-sync` → `distribute`（`--preset`/`--dry-run` 保留）
- `main`：
  - `presets` 命令列出两类：`preset_registrations()`（标 `preset`）与 `target_registrations()`（标 `sync_target`）
  - `target-sync` handler 改 `distribute`；`build_distribute_pipeline(...).run(selected=...)`
  - pipeline 调用：`service.run_pipeline(cookie, distribute=not args.no_distribute, only_distribute=args.only_distribute, progress=...)`
  - 删除 `only_link`/`pipeline_cmd` 分支；`args.command in ("sync", "pull")` 首次登录判断改为 `"sync"`
  - `render_pipeline_result` 调用适配（command 参数简化）
- 依赖 `build_pipeline` 的 handler 检查 `build_target_sync_pipeline` 引用更新

- [ ] **Step 1: 更新测试（先读 `tests/test_cli_main.py` 或现有 cli 测试，按模式更新）**

```python
# sync 帮助包含 --no-distribute/--only-distribute
# pull/process 子命令不存在（parser.parse_args(["pull"]) 报 SystemExit）
# distribute 子命令存在
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_cli_main.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

按 Interfaces 修改 `build_parser`/`main`。注意 `_ensure_cookie` 与首次登录流程中 `("sync", "pull")` 的判断更新为仅 `"sync"`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_cli_main.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/cli/main.py src/musicvault/cli/render.py tests/test_cli_main.py
git commit -m "refactor: CLI 命令收敛（sync 分发选项、distribute 改名、presets 双列表）"
```

---

### Task 16: PlaylistUseCase library 简化 + media_store 扁平化

**Files:**
- Modify: `src/musicvault/application/playlist_use_case.py`

**Interfaces:**
- `remove_playlist`：`deleted_dirs` 循环改为直接 `self.paths.library / dir_name`（rmtree 单个歌单目录）；删除 `cfg.presets` 遍历；未分类 inode 清理改为遍历 `self.paths.library / self.cfg.default_playlist_name`（保留 inode 匹配删除）；canonical 清理路径 `media_store / str(track_id)`（**扁平化**，rmtree 整个 track 目录）
- `remove_song`：`audio_dir` 改为 `media_store / str(song_id)`

- [ ] **Step 1: 更新现有测试（读 `tests/test_playlist_use_case.py`）**

```python
# remove_playlist 断言 library/<歌单名>/ 被删除；canonical 目录 media_store/<tid>/ 被删除
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_playlist_use_case.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

按 Interfaces 修改（删除 `for preset in self.cfg.presets` 循环与 `self.cfg.preset_dir(...)` 调用）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_playlist_use_case.py -q && python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/application/playlist_use_case.py tests/test_playlist_use_case.py
git commit -m "refactor: PlaylistUseCase library 简化与 media_store 扁平化"
```

---

### Task 17: Config 字段移除 + domain/preset.py 退役 + 文档

**Files:**
- Modify: `src/musicvault/core/config.py`
- Delete: `src/musicvault/domain/preset.py`（先 grep 确认 `Preset`/`default_presets`/`validate_presets`/`compute_preset_hash`/`build_audio_specs` 无引用）
- Modify: `docs/AGENTS.md`、`CONTEXT.md`、`README.md`
- Modify: `src/musicvault/adapters/state/sqlite.py`（presets 表加 `kind` 列——先读文件，按现有 schema 版本化方式迁移；`state.register_preset` 调用方（bootstrap）传 `kind`）
- Test: `tests/test_config.py`（更新）

**Interfaces:**
- `Config`：
  - 删除字段：`presets`、`metadata_fields`、`builtin_playlist_links_enabled`
  - 新增字段：`builtin_scripts_enabled: bool = True`（to_dict/from_dict 走 `preset_system.builtin`）
  - `from_dict`：旧 `presets` 数组字段**宽容忽略**（不解析不报错）；`metadata.fields` 忽略；`preset_system.playlist_links` 迁移到 `preset_system.builtin`
  - 删除方法：`preset_dir(name)`
  - `ensure_dirs`：不再创建 preset 目录（只确保五区域）
  - `download_quality: str = "hires"` 字段保留（bootstrap 回退用）
  - `default_presets()`/`validate_presets()` 引用删除
- `bootstrap.build_runtime`：`register_builtin_presets(...)` 受 `config.builtin_scripts_enabled` 控制；state 登记带 `kind`

- [ ] **Step 1: 更新现有测试**

```python
# test_config.py：
# 1) 含旧 presets 数组的配置 JSON 可加载且 presets 被忽略
# 2) preset_system.builtin=false 解析为 builtin_scripts_enabled=False
# 3) preset_dir 方法不存在
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

按 Interfaces 修改 config.py；`_normalize_preset_dict`/`_check_legacy_format` 保留（`_check_legacy_format` 的 `lossy/filenames/cover/lyrics` 旧字段检查保留）；`domain/preset.py` 删除；`sqlite.py` presets 表加 `kind` 列（`TEXT NOT NULL DEFAULT 'target'`，现有行兼容）；bootstrap 的 `state.register_preset(...)` 传 `kind="preset"|"target"`。文档更新：AGENTS.md 架构段（两套脚本、四阶段、命令收敛）、CONTEXT.md 术语（预设新定义、统一歌词格式（Unified Lyrics Format）、fetch/pull/process/distribute）、README.md 命令表。

- [ ] **Step 4: 全量验收**

Run: `python -m pytest tests/ -q && python -m ruff check src/ tests/ && python -m ruff format --check src/ tests/`
Expected: 全量 PASS、lint 通过

Run: `python -m musicvault --help && python -m musicvault sync --help && python -m musicvault distribute --help && python -m musicvault presets --help`
Expected: 冒烟通过，命令列表含 sync/distribute/presets/add/remove/list，无 pull/process/target-sync

- [ ] **Step 5: 提交**

```bash
git add src/musicvault/core/config.py src/musicvault/adapters/state/sqlite.py src/musicvault/application/bootstrap.py docs/AGENTS.md CONTEXT.md README.md tests/test_config.py
git rm src/musicvault/domain/preset.py
git commit -m "refactor: Config 移除声明式 presets，preset 脚本化收尾，文档更新"
```
