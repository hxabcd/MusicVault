"""target_api：外部 sync_target 脚本唯一可依赖的版本化公开 API 面。

公开符号定义在 ``v1`` 子模块；脚本应使用
``from musicvault.target_api.v1 import ...`` 导入。
顶层不重导出符号，防止脚本绕过版本化命名空间。
"""

from musicvault.target_api import v1  # noqa: F401 —— 保持 `musicvault.target_api.v1` 属性可用

__all__ = ["v1"]
