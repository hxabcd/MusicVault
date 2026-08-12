"""Decryptor 补充单测：非 ncm 直通、ncm 解密成功/失败与文件缺失。

ncmdump 的 NeteaseCloudMusicFile 以 fake 替身注入，不产生真实解密副作用。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from musicvault.adapters.processors import decryptor as decryptor_module
from musicvault.adapters.processors.decryptor import Decryptor
from musicvault.domain.models import DownloadedTrack, Track


def _downloaded(path: Path, is_ncm: bool) -> DownloadedTrack:
    return DownloadedTrack(
        track=Track(id=1, name="测试", artists=["歌手"], album="专辑", raw={}),
        source_file=str(path),
        is_ncm=is_ncm,
    )


class _FakeNcm:
    """模拟 NeteaseCloudMusicFile：记录调用并落盘一个解密产物。"""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.decrypted = False
        self.dump_target: Path | None = None

    def decrypt(self) -> "_FakeNcm":
        self.decrypted = True
        return self

    def dump_music(self, path: Path) -> Path:
        self.dump_target = Path(path)
        result = Path(path).with_suffix(".mp3")
        result.write_bytes(b"decrypted-data")
        return result


class TestDecryptIfNeeded:
    def test_non_ncm_returns_source_unchanged(self, tmp_path, monkeypatch) -> None:
        """非 .ncm 文件直通返回原始路径，不触碰解密器。"""
        src = tmp_path / "1.mp3"
        src.write_bytes(b"mp3-data")
        monkeypatch.setattr(
            decryptor_module, "NeteaseCloudMusicFile", Mock(side_effect=AssertionError("不应调用解密器"))
        )

        result = Decryptor().decrypt_if_needed(_downloaded(src, is_ncm=False), tmp_path / "out")

        assert result == src

    def test_non_ncm_does_not_create_output_dir(self, tmp_path, monkeypatch) -> None:
        src = tmp_path / "1.mp3"
        src.write_bytes(b"mp3-data")
        output = tmp_path / "never-created"

        Decryptor().decrypt_if_needed(_downloaded(src, is_ncm=False), output)

        assert not output.exists()

    def test_ncm_decrypts_and_dumps(self, tmp_path, monkeypatch) -> None:
        """ncm 文件按 stem 解密到输出目录。"""
        src = tmp_path / "1.ncm"
        src.write_bytes(b"ncm-data")
        output = tmp_path / "decrypted"
        fake = _FakeNcm(src)
        monkeypatch.setattr(decryptor_module, "NeteaseCloudMusicFile", Mock(return_value=fake))

        result = Decryptor().decrypt_if_needed(_downloaded(src, is_ncm=True), output)

        assert fake.decrypted is True
        assert fake.dump_target == output / "1"
        assert result == output / "1.mp3"
        assert output.is_dir()

    def test_decrypt_failure_propagates(self, tmp_path, monkeypatch) -> None:
        """解密过程抛出的异常（如损坏文件）向上传播。"""
        src = tmp_path / "1.ncm"
        src.write_bytes(b"corrupt-ncm")

        def _broken(path) -> _FakeNcm:
            fake = _FakeNcm(path)

            def _fail() -> None:
                raise RuntimeError("解密失败：文件损坏")

            fake.decrypt = _fail
            return fake

        monkeypatch.setattr(decryptor_module, "NeteaseCloudMusicFile", Mock(side_effect=_broken))

        with pytest.raises(RuntimeError, match="文件损坏"):
            Decryptor().decrypt_if_needed(_downloaded(src, is_ncm=True), tmp_path / "out")

    def test_missing_ncm_file_raises_file_not_found(self, tmp_path, monkeypatch) -> None:
        """源文件缺失时构造 NeteaseCloudMusicFile 即失败（模拟真实 SDK 行为）。"""
        missing = tmp_path / "ghost.ncm"
        monkeypatch.setattr(decryptor_module, "NeteaseCloudMusicFile", Mock(side_effect=FileNotFoundError(missing)))

        with pytest.raises(FileNotFoundError):
            Decryptor().decrypt_if_needed(_downloaded(missing, is_ncm=True), tmp_path / "out")
