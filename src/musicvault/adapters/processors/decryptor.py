from __future__ import annotations

from pathlib import Path

from ncmdump import NeteaseCloudMusicFile

from musicvault.core.models import DownloadedTrack


class Decryptor:
    """`.ncm` 解密器"""

    def decrypt_if_needed(self, item: DownloadedTrack, output_dir: Path) -> Path:
        """按需解密文件，非 `.ncm` 文件直接返回原始路径"""
        src = Path(item.source_file)

        if not item.is_ncm:
            return src

        output_dir.mkdir(parents=True, exist_ok=True)

        ncm_file = NeteaseCloudMusicFile(src)
        ncm_file.decrypt()
        return ncm_file.dump_music(output_dir / src.stem)
