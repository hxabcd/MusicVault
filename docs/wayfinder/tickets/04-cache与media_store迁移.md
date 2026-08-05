---
type: wayfinder:grilling
status: open
parent: ../2026-08-05-musicvault-refactor-map.md
depends_on:
  - 01-重构范围与验收边界
  - 03-SQLite最小schema与迁移
---

## Question

现有 downloads、状态索引和 library 文件如何迁移为 cache、media_store/<track_id> 和可重建 library，迁移失败或重复执行时如何保证安全？
