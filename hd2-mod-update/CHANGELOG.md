## 0.1.2 — 2026-09-24

修两个会让旧锁「漏报」和「越修越坏」的 bug，均由 `game_sha256` 这个锁字段名引起。

### 1. `verify` 漏报旧锁（严重）

`TEXT_LOCKS` 只认 `module_sha256` / `game_dll_sha256` / `dll_sha256`。
字段名叫 `game_sha256` 的 mod 落到「裸 64-hex 常量」那条分支，`lock_report` 给它加了
`dll:` 前缀，而 `stale_locks()` 却拿**带前缀的标签**去裸哈希集合里查成员 → 永远查不到
→ **旧锁一律不报，`verify` 说「合格」**。

真实后果：`genji_threefold` / `warp_pack_safe` 两个旧包（锁还是 25327279）通过了 verify。

- `stale_locks()` 改为用**裸哈希**判断（`l.split(':',1)[1]`）。
- `TEXT_LOCKS` 增加 `game_sha256`，让它走文本替换路径。
- `lock_report()` 对同一哈希去重，避免同一个锁被算两次（`旧锁=2`）。

### 2. 大小写：会把 mod 改坏（严重）

文本替换路径用 `new_dll.encode()`（**小写**），而裸 hex 路径用 `new_dll.upper()`（**大写**）。
`game_sha256` 原本走裸 hex 路径，于是刷锁后锁值被**强制转成大写**。

但 mod 里的比对是：

```lua
function api.module_hash(address)
    for i=0,31 do parts[#parts+1]=string.format('%02x',digest[i]) end   -- 小写
end
assert(api.module_hash(game)==SPEC.game_sha256, 'unsupported_game_build_no_write')
```

`"2e2c…" == "2E2C…"` 恒为 false → **断言失败，mod 直接不加载**（比不刷锁还糟：
不刷锁至少只影响这一个 mod，改错大小写会让它从「勉强能跑」变成「彻底不加载」）。

- 加 `game_sha256` 进 `TEXT_LOCKS` 后走小写路径，大小写与原文件一致。
- 已修复受影响的 `Genji-Saber-Knife` / `Warp-Pack-Safe-10m` 两个制品。

### 回归测试

| 用例 | 期望 | 结果 |
|---|---|---|
| `game_sha256` = 旧锁 | 报旧锁、退出码 1 | ✓ `旧锁=1 需要 lock` |
| `game_sha256` = 新锁 | 通过、退出码 0 | ✓ `旧锁=0` |
| 对旧锁文件跑 `lock` | 值保持**小写** | ✓ |
| `game_dll_sha256` 旧包（老路径） | 仍报旧锁 | ✓ `旧锁=1 旧 build=25327279` |

