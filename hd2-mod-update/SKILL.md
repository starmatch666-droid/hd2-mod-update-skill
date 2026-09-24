---
name: hd2-mod-update
description: 《绝地潜兵2》游戏更新后修复「Lua 注入型」mod（Bingus Shared Loader / .patch_N 归档）的完整工作流。游戏每次更新会让所有 addon 的构建锁失效而集体静默失效，本 skill 覆盖三类失效（A 构建锁 / B 指针槽位 RVA / C 表模板）的判定与修复：离线批量刷锁（源码树与成品包两种形态）、只读探针采表、锚点常量重写、槽位部署与进游戏验证。当用户说「游戏更新后 mod 不生效/失效了」「要更新 mod」「update_mods / 换锁」「日志里 unsupported_game_dll_no_write / not_found / template_mismatch」，或要写/改一个能在游戏更新后自愈的 mod 时使用。
---

# HD2 mod 更新（游戏打补丁后修 mod）

## 0. 先理解为什么会"集体失效"

注入型 addon 里都嵌着一个**构建锁**：`module_sha256` = 当时 `data/game/game.dll` 的 SHA-256。
游戏一更新，锁不匹配，addon 就**拒绝写内存**，只在日志里留一条
`unsupported_game_dll_no_write`（**不会写坏数据**，这是设计成这样的）。

所以更新后的第一件事永远是：**把锁换成新值**。这是纯离线操作，几分钟搞定，覆盖 80% 的情况。
剩下 20% 是指针/表结构真的变了 —— 那才需要进游戏跑一次**只读探针**。

## 1. 三类失效与判定

| 类 | 本质 | 日志特征 | 修法 |
|---|---|---|---|
| **A 构建锁** | `module_sha256`(文本) 或 bytecode mod 里**大写**的 DLL/EXE SHA-256 | `unsupported_game_dll_no_write` / `startup_failed_no_write` | **离线**：`tools/hd2modupdate.py`（源码树或成品包） |
| **B 指针槽位** | `owner_rva`（game.dll 里指向组件指针槽的 RVA）、`candidate_slot`（表相对指针的偏移） | `not_found` / `anchor_mismatch` / `waiting_for_entity_locator` / `slot_mismatch` | 进游戏跑 `tools/hd2update.py probe build` 生成的**只读探针** → 取新 RVA → `anchors` 重写 |
| **C 表模板** | 记录序号 / 哈希映射 / 行字节指纹（`record_index`、`hashmap`、`original`/`anchor_row`） | `template_mismatch` / `ldld_block_not_found` / `guard_failed` | 同上探针 dump 当前表 → 重算这些常量 |

判定顺序：**先看有没有 A 类日志**（一条都不要放过）→ 没有 A 才考虑 B/C。
一条 A 类日志 + 别的都没写 = 只差刷锁。

## 2. 标准工作流

```bash
# 0) 拿新锁（先把游戏更新完、且完全退出游戏）
#    game.dll / helldivers2.exe 的 SHA-256，以及 Steam appmanifest 里的 buildid
python tools/hd2modupdate.py --root <你的mod工程根> --game-dir "<...>/Helldivers 2"            # 预览
python tools/hd2modupdate.py --root <你的mod工程根> --game-dir "<...>/Helldivers 2" --apply --build

# 1) 成品包（Arsenal zip / 已部署的 patch_N）也一并刷
python tools/hd2modupdate.py --patch MyMod.zip --game-dir "<...>/Helldivers 2" --apply

# 2) 校验：结构、资源名哈希、锁
python tools/hd2update.py inspect MyMod.zip
python tools/hd2update.py verify  MyMod.zip

# 3) 部署（保持 data/9ba626afa44a3aa3.patch_* 链连续、改前备份）
python tools/deploy_patch.py MyMod/data/9ba626afa44a3aa3.patch_0 --game-dir "<...>/Helldivers 2" --apply

# 4) 进游戏验证（每次只开一个验证动作，读日志）
#    通过 = <Addon>.log 里出现 applied_runtime_write_verified
#    失败 = 出现 no_write/not_found/template_mismatch → 按第 1 节分流，必要时走探针（第 3 节）
```

**loader 侧的总体检**：`%LOCALAPPDATA%\CowboyBingus\Helldivers2\Logs\BingusSharedLoader.log`
首行版本，然后 `Discovery: N declared entries` 与逐 addon 的 `loaded` ——
**N 明显变小 = 归档结构被写坏了**（见 references/02 的 name-hash 铁律）。

## 3. 需要探针时

```bash
python tools/hd2update.py probe build --out out/probe      # 生成只读探针 addon + 可直接安装的 patch
#   退出游戏 → 把 out/probe/9ba626afa44a3aa3.patch_0 放到 data/ 的下一个空槽位（+ 两个空文件）
#   进游戏（兵营即可）待 1~2 分钟 → 日志 OwnerScout.log
python tools/hd2update.py probe parse "%LOCALAPPDATA%\CowboyBingus\Helldivers2\Logs\OwnerScout.log"
python tools/hd2update.py anchors src/spec.lua src/reference.json --probe probe.json \
       --component WeaponDataComponent --dry-run        # 确认后再去掉 --dry-run
```

探针是**只读**的：只 `VirtualQuery` + `ReadProcessMemory`，不写内存、不调引擎。
它输出新 `owner_rva`（`scout_FOUND_DIRECT`）、区段表（`scout_section`）、以及目标表的完整字节（`table_found`/`table_bytes`）。

## 4. 八条铁律（照做能省掉一整晚）

1. **改 `data/` 前必须确认游戏已退出**（`Get-Process helldivers2`）。
2. **归档写入必须复用原始资源名哈希**：用 `hd2archive.make_archive_raw([(原 name_hash, envelope) ...])`，
   **绝不要**用 `make_archive({'mods/starm/xxx': ...})` 按 addon 名重算 —— 那样 loader 会一个都发现不了
   （现象：`Discovery: N declared entries` 里没有你的 addon）。这是我们踩过的最贵的坑。
3. 换锁是**等长替换**（SHA-256 恒 64 字符、构建号一般 8 位）→ 可原地改字节，偏移不变最安全；
   长度变了就必须重建归档，并回读校验。
4. 任何写盘前**整份备份**；部署 slot 前备份原 `patch_N` 与两个伴生空文件。
5. 槽位链**必须连续**（0..N 无空洞），否则 loader 后面的都不加载。
6. 一次只改一个变量、只验证一个动作；每步都留"预期值 → 写入 → 回读"三段证据。
7. 自己加的 `ffi.cdef` **必须 `pcall`**，且结构体用**唯一名字**（如 `StarmBtMbi`）——
   通用名（`_MEMORY_BASIC_INFORMATION`）会把别人未加保护的 cdef 顶掉，导致对方 addon 加载失败。
8. 刷新锁是安全的（mods 运行时自校验，最差是一条 no_write）；但**不要**顺手把别人的 mod
   升级到新版本 —— 版本升级会改行为，必须单独征得同意。

## 5. 工具与参考

| 文件 | 用途 |
|---|---|
| `tools/hd2modupdate.py` | 离线刷锁（源码树 / 成品包 / zip / 目录），可 `--build` 重建 |
| `tools/hd2update.py` | `inspect` / `verify` / `lock` / `variants` / `probe build|parse` / `anchors` |
| `tools/deploy_patch.py` | 把 patch 部署到 `data/` 的下一个空槽位（备份 + 连续性 + 标记去重） |
| `tools/hd2archive.py` | `.patch_N` / Arsenal 归档读写（正确用法见文件头注释） |
| `tools/history.json` | 已知构建的锁值表（新构建请追加） |
| `tools/probe/` | 只读探针（`gen_scout.py` + 模板 + 指纹）与日志解析 |
| `references/01-升级机制与三类失效.md` | 机制细节与判定 |
| `references/02-归档格式与换锁.md` | 归档布局、name-hash 铁律、等长替换 vs 重建 |
| `references/03-只读探针与锚点重算.md` | RVA/槽位/表模板怎么采、日志标签、锚点重写 |
| `references/04-实战-25327279到25480438.md` | 一次真实更新的完整记录（含踩坑与复盘） |
| `references/05-排错日志对照表.md` | 日志行 → 原因 → 处置 |
