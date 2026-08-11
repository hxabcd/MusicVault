# Python Preset 注册与发现 Spec

状态：已完成（2026-08-12）
范围：本次工程重构

## Problem Statement

固定的 lossless/lossy 配置无法表达不同设备、播放器和外部目标的导出行为。未来 preset 需要能够通过 Python 定义复杂规则，并允许自定义操作。

同时，preset 可能来自多个目录，必须解决发现、命名冲突、版本兼容和依赖管理问题。

## Solution

系统提供一个版本化公开 API。内置 preset 和外部脚本都通过注册表登记。

内置提供可开关的 `playlist_links` preset，用链接模拟歌单目录。额外 preset 从配置指定的多个目录中加载。

外部脚本通过统一注册函数向 registry 注册一个或多个 preset。系统加载全部已配置目录，发现同名 preset 时直接失败并指出所有来源。

默认执行所有已发现且启用的 preset；CLI 可以指定 preset 子集。

preset API 以版本命名，例如 `musicvault.preset_api.v1`。外部脚本不得依赖内部 Service、Adapter 或数据库实现。

脚本运行在项目当前 Python 虚拟环境中。第三方依赖必须显式声明，不自动安装。

## User Stories

1. 作为用户，我希望关闭内置 playlist_links preset，以便只使用外部 preset。
2. 作为用户，我希望在配置中指定多个 preset 目录。
3. 作为用户，我希望每个脚本可以定义一个或多个 preset。
4. 作为 preset 作者，我希望使用 Python 表达复杂的筛选、命名和目标操作。
5. 作为项目维护者，我希望重复 preset 名称在启动时明确报错，而不是静默覆盖。
6. 作为项目维护者，我希望错误信息包含脚本路径和冲突名称。
7. 作为用户，我希望新加入的启用 preset 默认参与同步。
8. 作为用户，我希望通过 CLI 只执行指定 preset。
9. 作为 preset 作者，我希望 API 版本变化时能明确知道脚本不兼容。
10. 作为项目维护者，我希望内部重构不会强制修改所有外部脚本。
11. 作为用户，我希望缺少第三方依赖时得到具体的安装提示。
12. 作为项目维护者，我希望脚本发现顺序不影响行为。
13. 作为 preset 作者，我希望脚本可以访问稳定的曲目、媒体资产和目标上下文。
14. 作为用户，我希望一个坏脚本不会悄悄被跳过并造成不完整同步。

## Implementation Decisions

- 配置提供 preset 目录列表和内置 preset 开关。
- 外部脚本通过 `register(registry)` 注册 preset。
- 一个脚本允许注册多个 preset。
- registry 对名称、API 版本和启用状态进行校验。
- 同名 preset 启动失败，禁止按目录顺序静默覆盖。
- 未指定 CLI 子集时执行所有启用 preset。
- 外部脚本从显式版本化 API 导入公共类型和上下文。
- 外部脚本视为可信代码，不提供代码沙箱。
- 第三方依赖由项目环境显式管理，不在运行时自动安装。
- preset 来源、API 版本和脚本摘要可被记录到状态库，但同步历史属于后续 spec。

## Testing Decisions

- 测试从一个目录加载一个脚本并成功注册。
- 测试一个脚本注册多个 preset。
- 测试多个目录加载顺序不影响结果。
- 测试同名 preset 失败并包含冲突来源。
- 测试脚本缺少注册函数时给出明确错误。
- 测试 API 版本不兼容时阻止启动。
- 测试禁用 preset 不被执行。
- 测试 CLI preset 选择只执行目标 preset。
- 参考现有 NeteaseClient mock 测试，使用临时脚本目录和 fake registry。

## Out of Scope

- preset 脚本的沙箱和权限隔离。
- 每个 preset 独立虚拟环境。
- 运行时自动安装依赖。
- 脚本在线分发和远程仓库。
- preset 同步历史界面。

## Further Notes

内置 preset 也应尽量通过同一公开 API 注册，以避免内置行为和外部行为存在两套不兼容机制。

