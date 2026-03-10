from __future__ import annotations

import argparse
import logging
from pathlib import Path

from musicvault.core.config import FileConfig

logger = logging.getLogger(__name__)


def _silence_logs() -> None:
    for name in ("pyncm", "urllib3.connectionpool"):
        muted = logging.getLogger(name)
        muted.setLevel(logging.WARNING)
        muted.propagate = False


def _configure_logs() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    _silence_logs()


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="./config.json", help="配置文件路径（JSON）")
    parser.add_argument("--cookie", default=None, help="网易云 Cookie 字符串")
    parser.add_argument("--workspace", default=None, help="工作目录")
    parser.add_argument("--playlist-id", type=int, default=None, help="指定歌单 ID")
    parser.add_argument("--force", action="store_true", help="强制重处理已处理文件（覆盖 processed 索引）")
    parser.add_argument("--no-translation", action="store_true", help="关闭网易云歌词翻译合并（默认开启）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MusicVault 命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行同步和后处理")
    _add_common_args(run)

    sync = sub.add_parser("sync", help="仅执行同步下载")
    _add_common_args(sync)

    process = sub.add_parser("process", help="仅执行本地后处理")
    _add_common_args(process)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logs()
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config).resolve()
    existed = cfg_path.exists()
    file_cfg = FileConfig.load(cfg_path)
    if existed:
        logger.info("已加载配置文件：%s", cfg_path)
    else:
        logger.info("配置文件不存在，已按默认值自动生成：%s", cfg_path)

    cookie = args.cookie or file_cfg.cookie
    if not cookie:
        logger.error("缺少 cookie：请通过 --cookie 或配置文件提供")
        return 2

    app_cfg = file_cfg.to_app_config(workspace_override=args.workspace)
    options = file_cfg.to_run_options(
        command=args.command,
        playlist_id_override=args.playlist_id,
        no_translation=args.no_translation,
        force_override=args.force,
    )

    from musicvault.adapters.providers.pyncm_client import PyncmClient
    from musicvault.services.run_service import RunService

    service = RunService(
        cfg=app_cfg,
        api=PyncmClient(text_cleaning_enabled=app_cfg.text_cleaning_enabled),
    )
    service.run_pipeline(cookie=cookie, options=options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
