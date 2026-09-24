# hd2-mod-update

修复《绝地潜兵 2》**「Lua 注入型」mod** 在游戏更新后集体失效的工作流 skill。

适用于通过 **Bingus Shared Loader** 载入、以 `.patch_N` 归档分发的 mod。

## 为什么会集体失效

每个注入型 addon 里都嵌着一个**构建锁**：

```
module_sha256 = 当时 data/game/game.dll 的 SHA-256
```

游戏一打补丁，锁就全部失效，addon 会**拒绝写内存**，只在日志里留一条：

```
unsupported_game_dll_no_write
```

**不会写坏数据 —— 这是设计成这样的。**

## 三类失效

| 类 | 本质 | 日志特征 | 修法 |
|---|---|---|---|
| **A** 构建锁 | `module_sha256`（文本）或 bytecode mod 里大写的 DLL/EXE SHA-256 | `unsupported_game_dll_no_write`<br>`startup_failed_no_write` | **纯离线**刷锁，几分钟搞定，覆盖约 80% 情况 |
| **B** 指针槽位 | `owner_rva`（game.dll 里的指针槽 RVA）、`candidate_slot` | `not_found` / `anchor_mismatch`<br>`slot_mismatch` | 进游戏跑**只读探针**取新 RVA，再重写锚点 |
| **C** 表模板 | 记录序号 / 哈希映射 / 行字节指纹 | `template_mismatch`<br>`ldld_block_not_found` | 同上，用探针 dump 当前表后重算常量 |

**判定顺序**：先看有没有 A 类日志（一条都别放过）→ 没有 A 才考虑 B/C。

## 使用

```bash
# 0) 拿新锁（先把游戏更新完并完全退出）
python tools/hd2modupdate.py --root <mod工程根> --game-dir "<...>/Helldivers 2"
python tools/hd2modupdate.py --root <mod工程根> --game-dir "<...>/Helldivers 2" --apply --build

# 1) 成品包也一并刷
python tools/hd2modupdate.py --patch MyMod.zip --game-dir "<...>/Helldivers 2" --apply

# 2) 校验：结构 / 资源名哈希 / 锁
python tools/hd2update.py inspect MyMod.zip
python tools/hd2update.py verify  MyMod.zip     # 退出码 0 且「旧锁=0」才算 OK

# 3) 部署（保持 patch_N 链连续，改前自动备份）
python tools/deploy_patch.py MyMod/data/9ba626afa44a3aa3.patch_0 --game-dir "<...>/Helldivers 2" --apply
```

`hd2update.py` 的子命令：

| 命令 | 用途 |
|---|---|
| `inspect` | 看归档结构、资源名哈希、addon 数 |
| `verify` | 校验成品是否可用（结构 / 资源名哈希未变 / 无旧锁） |
| `lock` | 刷新构建锁，**安全、不破坏结构** |
| `variants` | 一次刷同一 mod 的多个保留版本 |
| `probe build/parse` | 生成只读探针 / 解析探针日志 |
| `anchors` | 用探针数据重写 `owner_rva` / `candidate_slot` / 表常量 |

## 两条铁律

1. **不要改归档里的资源 name-hash** —— 它等于 mod 的身份证，改名会让 Loader 认不出来。
2. **换锁是等长替换，不要重新打包** —— 重新打包会改变内部分层，可能直接让归档加载失败。

## 实战案例

`references/` 里是 5 篇实战记录，含 **25327279 → 25480438** 这次真实更新的完整过程：

- `01-升级机制与三类失效.md`
- `02-归档格式与换锁.md`
- `03-只读探针与锚点重算.md`
- `04-实战-25327279到25480438.md`
- `05-排错日志对照表.md`

## 版本

当前 **0.1.1**，详见 [CHANGELOG.md](hd2-mod-update/CHANGELOG.md)。

## 许可

见 [hd2-mod-update/LICENSE](hd2-mod-update/LICENSE)。
