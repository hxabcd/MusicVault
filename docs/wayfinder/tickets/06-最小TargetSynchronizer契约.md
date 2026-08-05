---
type: wayfinder:grilling
status: open
parent: ../2026-08-05-musicvault-refactor-map.md
depends_on:
  - 03-SQLite最小schema与迁移
  - 05-preset_api_v1接口
---

## Question

最小 TargetSynchronizer 的 prepare、sync_item/apply、finalize 生命周期如何定义，哪些能力由同步框架提供，哪些能力由 Preset 脚本提供？
