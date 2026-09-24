#!/usr/bin/env python3
"""hd2update - repair Helldivers 2 Lua mods after a game update.

Three kinds of breakage happen when HD2 patches:

  A. build lock        every addon pins `module_sha256` (SHA-256 of data/game/game.dll) and
                       refuses to write on a mismatch.  Fixable OFFLINE.        -> `lock`
  B. owner RVA         addons that follow a pointer slot in game.dll store the slot's RVA
                       (and a `candidate_slot` offset).  Needs ONE in-game run -> `probe`
  C. table templates   record indices, maps and row bytes shift when the game's data files
                       change.  Needs the probe dump.                          -> `anchors`

Commands
  inspect <artifact>                      结构/资源名哈希/addon 名/锁 一览
  lock    <artifact...>                   刷新构建锁（离线，安全；结构不破坏）
  verify  <artifact>                      校验制品完好、资源名哈希未变、无旧锁
  variants <dir|zip...> [--dry-run]       一次刷同一模组的多个保留版本（效果不同的那些）
  probe build    [--out DIR]              生成只读探针 addon + 可直接安装的 patch
  probe parse    <OwnerScout.log> [-o j]  解析探针日志 -> JSON（新 RVA、表地址/内容、区段）
  anchors <file...> --probe j [-o]        用探针结果重写 owner_rva / candidate_slot / 行模板常量

artifact = <file.patch_N> | <mod.zip> | <mod 目录（含 data/9ba626afa44a3aa3.patch_0 或 Addon/…）>

Examples
  python hd2update.py lock MyMod.zip --game-dir "C:/.../Helldivers 2"
  python hd2update.py inspect MyMod.zip
  python hd2update.py probe build --out out/probe
  python hd2update.py probe parse "%LOCALAPPDATA%/CowboyBingus/Helldivers2/Logs/OwnerScout.log"
  python hd2update.py anchors src/spec.lua src/reference.json --probe probe.json \
        --component WeaponDataComponent --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import types
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hd2archive as H  # noqa: E402

CARRIER = '9ba626afa44a3aa3'
PATCH_NAME = CARRIER + '.patch_0'
HISTORY = json.loads((HERE / 'history.json').read_text(encoding='utf-8'))
HEX64 = re.compile(rb'[0-9a-fA-F]{64}')

KNOWN_DLL = {h.lower() for b in HISTORY['builds'] for h in (b['game_dll_sha256'],)}
KNOWN_EXE = {h.lower() for b in HISTORY['builds'] for h in (b['exe_sha256'],)}
KNOWN_BUILD_IDS = {b['build'] for b in HISTORY['builds']}
CURRENT = HISTORY['builds'][-1]                      # history.json 里最后一条 = 目标构建
CURRENT_DLL = CURRENT['game_dll_sha256'].lower()
CURRENT_EXE = CURRENT['exe_sha256'].lower()
# 常见锁写法；只替换“值”，不动变量名
TEXT_LOCKS = [
    re.compile(rb'(module_sha256\s*[:=]\s*["\'])([0-9a-fA-F]{64})(["\'])'),
    re.compile(rb'(game_dll_sha256\s*[:=]\s*["\'])([0-9a-fA-F]{64})(["\'])'),
    re.compile(rb'(game_sha256\s*[:=]\s*["\'])([0-9a-fA-F]{64})(["\'])'),
    re.compile(rb'(dll_sha256\s*[:=]\s*["\'])([0-9a-fA-F]{64})(["\'])'),
]
BUILD_IDS = [
    re.compile(rb'(build\s*[:=]\s*["\']?)(\d{8})(["\']?)'),
    re.compile(rb'(gameBuild\s*[:=]\s*)(\d{8})'),
    re.compile(rb'(game_build\s*[:=]\s*["\']?)(\d{8})'),
]


# --------------------------------------------------------------------------- io
def backup_copy(path: Path, backup_dir: Path) -> Path:
    """备份到 backup_dir，**镜像原路径**，避免不同目录同名 zip 互相覆盖。"""
    dest = backup_dir.joinpath(*path.parts[1:])          # 去掉盘符
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest


class Artifact:
    """A patch file, a zip containing one, or a directory containing one."""

    def __init__(self, path: Path):
        self.path = path
        self.kind = 'patch'
        self.inner = None
        if path.is_dir():
            self.kind = 'dir'
            for cand in (path / 'data' / PATCH_NAME, path / 'Addon' / PATCH_NAME):
                if cand.exists():
                    self.inner = cand
                    break
            if self.inner is None:
                found = sorted(path.rglob(CARRIER + '.patch_*'))
                if not found:
                    raise SystemExit('目录里找不到 %s：%s' % (PATCH_NAME, path))
                self.inner = found[0]
        elif path.suffix.lower() == '.zip':
            self.kind = 'zip'
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.endswith(PATCH_NAME) or CARRIER + '.patch_' in n]
                if not names:
                    raise SystemExit('zip 里找不到 %s：%s' % (PATCH_NAME, path))
                self.inner = names[0]

    def read(self) -> bytes:
        if self.kind == 'patch':
            return self.path.read_bytes()
        if self.kind == 'dir':
            return self.inner.read_bytes()
        with zipfile.ZipFile(self.path) as z:
            return z.read(self.inner)

    def write(self, data: bytes, backup_dir: Path | None = None):
        stamp = time.strftime('%Y%m%d-%H%M%S')
        if self.kind == 'patch':
            if backup_dir:
                backup_copy(self.path, backup_dir)
            self.path.write_bytes(data)
        elif self.kind == 'dir':
            if backup_dir:
                backup_copy(self.inner, backup_dir)
            self.inner.write_bytes(data)
        else:
            if backup_dir:
                backup_copy(self.path, backup_dir)
            tmp = self.path.with_suffix('.zip.tmp')
            with zipfile.ZipFile(self.path) as src, zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as dst:
                for item in src.infolist():
                    body = src.read(item.filename)
                    if item.filename == self.inner:
                        body = data
                    dst.writestr(item, body)
            tmp.replace(self.path)

    def describe(self) -> str:
        return '%s (%s%s)' % (self.path, self.kind, ' -> ' + str(self.inner) if self.inner else '')


def stale_locks(found, cur_dll: str | None = None, cur_exe: str | None = None) -> list[str]:
    """在 lock_report 的结果里挑出「已知、但不是目标构建」的锁。

    注意：不能只判断 `in KNOWN_DLL`——目标构建自己也在 KNOWN_DLL 里，
    那样会把已经修好的制品误报成过期（旧版工具的 false positive）。
    """
    cur_dll = (cur_dll or CURRENT_DLL).lower()
    cur_exe = (cur_exe or CURRENT_EXE).lower()
    bad = []
    for l in sorted(found):
        # l 有两种形态：裸哈希（来自 module_sha256="…"）或带前缀 dll:/bytecode:（来自"裸 hex"写法）。
        # 判定必须用【裸哈希】v，不能拿带前缀的 l 去查 KNOWN_DLL —— 否则纯 hex 写法的 mod
        # 永远判不出旧锁（真实踩坑：genji_threefold / warp_pack_safe）。
        v = l.split(':', 1)[1] if l.startswith(('dll:', 'bytecode:')) else l
        if v in KNOWN_DLL or v in KNOWN_EXE:
            if v != cur_dll and v != cur_exe and v not in (CURRENT_DLL, CURRENT_EXE):
                bad.append(l)
    return bad


def stale_builds(builds, new_build: str | None = None) -> list[str]:
    cur = new_build or CURRENT['build']
    return sorted(b for b in builds if b != cur)


def addon_of(body: bytes) -> str:
    m = re.search(rb'-- HD2-Addon:\s*(\S+)', body)
    return m.group(1).decode() if m else '?'


def lock_report(body: bytes):
    found, builds = set(), set()
    for rx in TEXT_LOCKS:
        for m in rx.finditer(body):
            found.add(m.group(2).decode().lower())
    for m in HEX64.finditer(body):
        v = m.group(0).decode().lower()
        # 已经被 TEXT_LOCKS 记为裸哈希的，不要再记一条带前缀的，否则同一个锁会被算两次
        if (v in KNOWN_DLL or v in KNOWN_EXE) and v not in found:
            found.add(('bytecode:' if v in KNOWN_EXE else 'dll:') + v)
    for rx in BUILD_IDS:
        for m in rx.finditer(body):
            if m.group(2).decode() in KNOWN_BUILD_IDS:
                builds.add(m.group(2).decode())
    return found, builds


# --------------------------------------------------------------------------- lock
def refresh_body(body: bytes, new_dll: str, new_exe: str | None, new_build: str | None):
    stats = dict(sha=0, exe=0, build=0, bytecode=0)

    def sha_sub(m):
        old = m.group(2).decode().lower()
        if old == new_dll.lower():
            return m.group(0)
        stats['sha'] += 1
        return m.group(1) + new_dll.encode() + m.group(3)

    out = body
    for rx in TEXT_LOCKS:
        out = rx.sub(sha_sub, out)
    # 纯 64-hex 常量（bytecode 包装里的 DLL/EXE 锁，通常全大写）
    if new_exe:
        def hex_sub(m):
            v = m.group(0).decode().lower()
            if v in KNOWN_DLL and v != new_dll.lower():
                stats['bytecode'] += 1
                return new_dll.upper().encode()
            if v in KNOWN_EXE and v != new_exe.lower():
                stats['bytecode'] += 1
                return new_exe.upper().encode()
            return m.group(0)
        out = HEX64.sub(hex_sub, out)
    if new_build:
        def build_sub(m):
            old = m.group(2).decode()
            # 只替换「历史上真实出现过的 HD2 构建号」，避免误伤模组自己的 8 位数字
            if old == new_build or old not in KNOWN_BUILD_IDS:
                return m.group(0)
            stats['build'] += 1
            return m.group(1) + new_build.encode() + m.group(3)
        for rx in BUILD_IDS:
            out = rx.sub(build_sub, out)
    return out, stats


VARIANT_SKIP = ('backup', 'revision-backups', 'docs-', 'requested', 'intent', 'publish',
                'handoff', 'skill', 'toolkit', 'modupdate-tool', '一键', '修复')


def expand_variants(paths) -> list[Path]:
    """目录 -> 递归取里面所有 zip（跳过备份/文档/打包目录）；文件 -> 原样。"""
    out, seen = [], set()
    for p in (Path(x) for x in paths):
        cands = sorted(p.rglob('*.zip')) if p.is_dir() else [p]
        for c in cands:
            low = str(c).lower()
            if any(k in low for k in VARIANT_SKIP) or c in seen:
                continue
            seen.add(c)
            out.append(c)
    return out


def cmd_variants(args) -> int:
    """一次刷新「同一个模组的多个保留版本」——效果不同、各自独立，必须逐个刷。

    只读扫描时按 (文件, addon, 旧锁, 旧 build) 打印；--apply 时逐个原地刷新并回读校验。
    """
    new_dll = args.new_dll.lower() if args.new_dll else CURRENT_DLL
    new_exe = args.new_exe.lower() if args.new_exe else CURRENT_EXE
    new_build = args.new_build or CURRENT['build']
    items = expand_variants(args.path)
    if not items:
        raise SystemExit('没有找到制品')
    backup = Path(args.backup) if args.backup else Path('variant-lock-backup-' + time.strftime('%Y%m%d-%H%M%S'))
    stale_total = 0
    for p in items:
        try:
            raw = Artifact(p).read()
            a = H.parse(raw)
        except Exception as ex:
            print('%-52s 读取失败: %s' % (p.name[:50], ex))
            continue
        rows = []
        for e in a['entries']:
            locks, builds = lock_report(e['body'])
            rows.append((addon_of(e['body']), stale_locks(locks, new_dll, new_exe),
                         stale_builds(builds, new_build)))
        bad = [r for r in rows if r[1] or r[2]]
        tag = '需要刷新' if bad else '已是最新'
        print('%-46s %-8s %s' % (p.name[:44], tag, ' | '.join(
            '%s%s%s' % (r[0], ' 旧锁=%d' % len(r[1]) if r[1] else '',
                        ' 旧build=%s' % ','.join(r[2]) if r[2] else '') for r in rows)))
        if not bad:
            continue
        stale_total += 1
        if args.dry_run:
            continue
        ns = types.SimpleNamespace(artifact=[str(p)], game_dir=None,
                                   new_dll=new_dll, new_exe=new_exe, new_build=new_build,
                                   dry_run=False, backup=str(backup))
        rc = cmd_lock(ns)
        if rc:
            print('    !! 刷新失败 rc=%d' % rc)
    if stale_total:
        print('')
        print('%d/%d 个制品需要刷新%s' % (stale_total, len(items),
              '' if args.dry_run else '（已完成，备份在 %s）' % backup))
    else:
        print('')
        print('全部 %d 个制品都已是最新' % len(items))
    return 0


def cmd_lock(args) -> int:
    new_dll = args.new_dll.lower() if args.new_dll else None
    new_exe = args.new_exe.lower() if args.new_exe else None
    new_build = args.new_build
    if args.game_dir:
        gd = Path(args.game_dir)
        dll = gd / 'data' / 'game' / 'game.dll'
        exe = gd / 'bin' / 'helldivers2.exe'
        if not new_dll and dll.exists():
            new_dll = hashlib.sha256(dll.read_bytes()).hexdigest()
        if not new_exe and exe.exists():
            new_exe = hashlib.sha256(exe.read_bytes()).hexdigest()
    if not new_dll:
        raise SystemExit('需要 --game-dir 或 --new-dll')

    for target in args.artifact:
        art = Artifact(Path(target))
        raw = art.read()
        a = H.parse(raw)
        pairs, names_before, changed = [], [], 0
        for e in a['entries']:
            names_before.append(e['name'])
            body, st = refresh_body(e['body'], new_dll, new_exe, new_build)
            if st['sha'] or st['exe'] or st['build'] or st['bytecode']:
                changed += 1
                print('  %-44s sha=%d bytecode=%d build=%d' % (addon_of(e['body']), st['sha'] + st['exe'],
                                                              st['bytecode'], st['build']))
            pairs.append((e['name'], H.envelope(body)))          # 关键：保留原 name 哈希
        if not changed:
            print('%-52s 已是最新，无需修改' % art.path.name)
            continue
        new_raw = H.make_archive_raw(pairs)
        chk = H.parse(new_raw)
        assert [e['name'] for e in chk['entries']] == names_before, '资源名哈希必须保持不变'
        assert chk['count'] == a['count']
        print('%-52s %d -> %d bytes（%d 个 addon 更新）' % (art.path.name, len(raw), len(new_raw), changed))
        if args.dry_run:
            continue
        art.write(new_raw, Path(args.backup) if args.backup else None)
        # 回读校验
        back = H.parse(art.read())
        assert [e['name'] for e in back['entries']] == names_before
        for e in back['entries']:
            locks, builds = lock_report(e['body'])
            bad = stale_locks(locks, new_dll, new_exe)
            assert not bad, (addon_of(e['body']), bad)
            assert not stale_builds(builds, new_build), (addon_of(e['body']), sorted(builds))
        print('    ok: 回读校验通过（结构/资源名哈希/锁）')
    return 0


# --------------------------------------------------------------------- inspect/verify
def cmd_inspect(args) -> int:
    for target in args.artifact:
        art = Artifact(Path(target))
        raw = art.read()
        a = H.parse(raw)
        print('== %s  %d bytes  entries=%d' % (art.describe(), len(raw), a['count']))
        for e in a['entries']:
            locks, builds = lock_report(e['body'])
            print('   addon=%-42s len=%-8d name_hash=%d' % (addon_of(e['body']), len(e['body']), e['name']))
            if locks:
                print('        锁: %s' % ', '.join(sorted(locks)))
            if builds:
                print('        build 号: %s' % ', '.join(sorted(builds)))
    return 0


def cmd_verify(args) -> int:
    rc = 0
    for target in args.artifact:
        art = Artifact(Path(target))
        raw = art.read()
        a = H.parse(raw)
        print('== %s' % art.describe())
        for e in a['entries']:
            locks, builds = lock_report(e['body'])
            old = stale_locks(locks)
            oldb = stale_builds(builds)
            print('   addon=%-42s 旧锁=%-3d %s%s' % (
                addon_of(e['body']), len(old), 'OK' if not old else '需要 lock',
                '  旧 build=' + ','.join(oldb) if oldb else ''))
            if not old:
                for l in sorted(locks):
                    if l in KNOWN_DLL or l in KNOWN_EXE:
                        print('        当前锁: %s' % l)
            rc |= 1 if (old or oldb) else 0
    return rc


# --------------------------------------------------------------------------- probe
def cmd_probe(args) -> int:
    if args.probe_cmd == 'build':
        out = Path(args.out or 'out/probe')
        out.mkdir(parents=True, exist_ok=True)
        rc = __import__('subprocess').call([sys.executable, str(HERE / 'probe' / 'gen_scout.py')],
                                           cwd=str(HERE / 'probe'))
        lua = HERE / 'probe' / 'owner_scout.lua'
        if not lua.exists():
            print('探针 Lua 未生成（先看 probe/gen_scout.py 的报错）'); return 1
        body = lua.read_bytes()
        addon = addon_of(body)
        patch = H.make_archive({addon: H.envelope(body)})
        (out / PATCH_NAME).write_bytes(patch)
        (out / (PATCH_NAME + '.stream')).write_bytes(b'')
        (out / (PATCH_NAME + '.gpu_resources')).write_bytes(b'')
        print('探针已生成: %s' % (out / PATCH_NAME))
        print('安装：退出游戏后，把它放到  data\\%s.patch_<下一个空槽位>（连同两个空文件）' % CARRIER)
        print('然后在游戏里待 1~2 分钟，日志：%%LOCALAPPDATA%%\\CowboyBingus\\Helldivers2\\Logs\\OwnerScout.log')
        return 0
    # parse
    log = Path(args.log)
    tables, found, sections, start = {}, {}, [], {}
    chunks = {}
    for line in log.read_text(encoding='utf-8', errors='replace').splitlines():
        for tag in ('scout_start', 'scout_FOUND_DIRECT', 'scout_section', 'table_found', 'table_bytes',
                    'table_dump_complete', 'scout_finish', 'scout_old_rva_valid'):
            if tag not in line:
                continue
            if tag == 'scout_start':
                m = re.search(r'base=0x([0-9A-Fa-f]+)', line)
                if m:
                    start['base'] = int(m.group(1), 16)
            elif tag == 'scout_FOUND_DIRECT':
                m = re.search(r'new_rva=0x([0-9A-Fa-f]+).*?component=(\S+)', line)
                if m:
                    found[m.group(2)] = dict(new_rva=int(m.group(1), 16))
            elif tag == 'scout_old_rva_valid':
                m = re.search(r'owner=0x([0-9A-Fa-f]+) slot=0x([0-9A-Fa-f]+) table=0x([0-9A-Fa-f]+) map=(\S+)', line)
                if m:
                    found.setdefault(m.group(4), {}).update(owner=int(m.group(1), 16),
                                                            table=int(m.group(3), 16))
            elif tag == 'scout_section':
                m = re.search(r'rva=0x([0-9A-Fa-f]+) size=0x([0-9A-Fa-f]+)', line)
                if m:
                    sections.append(dict(rva=int(m.group(1), 16), size=int(m.group(2), 16)))
            elif tag == 'table_found':
                m = re.search(r'name=(\S+) address=0x([0-9A-Fa-f]+) size=(\d+)', line)
                if m:
                    tables.setdefault(m.group(1), {}).update(address=int(m.group(2), 16),
                                                             size=int(m.group(3)))
            elif tag == 'table_bytes':
                m = re.search(r'name=(\S+) address=0x([0-9A-Fa-f]+) offset=(\d+) hex=([0-9a-f]+)', line)
                if m:
                    chunks.setdefault((m.group(1), int(m.group(2), 16)), {})[int(m.group(3))] = \
                        bytes.fromhex(m.group(4))
            elif tag == 'table_dump_complete':
                m = re.search(r'name=(\S+) address=0x([0-9A-Fa-f]+) bytes=(\d+)', line)
                if m:
                    key = (m.group(1), int(m.group(2), 16))
                    data = b''.join(chunks.get(key, {}).get(o, b'') for o in sorted(chunks.get(key, {})))
                    tables.setdefault(m.group(1), {}).update(dump=data.hex())
            break
    for (name, addr), _ in chunks.items():
        data = b''.join(chunks[(name, addr)][o] for o in sorted(chunks[(name, addr)]))
        tables.setdefault(name, {})['hex'] = data.hex()
    out = Path(args.out or 'probe.json')
    out.write_text(json.dumps(dict(start=start, sections=sections, found=found, tables=tables),
                              indent=1), encoding='utf-8')
    print('解析完成: %s\n  found(组件->新 RVA): %d\n  表: %d\n  区段: %d' %
          (out, len(found), len(tables), len(sections)))
    return 0


# -------------------------------------------------------------------------- anchors
CONST_PATTERNS = [
    (re.compile(r'(owner_rva\s*[:=]\s*)(\d+)'), 'dec'),
    (re.compile(r'(owner_rva\s*[:=]\s*)(0x[0-9A-Fa-f]+)'), 'hex'),
    (re.compile(r'(candidate_slot\s*[:=]\s*)(-?\d+)'), 'dec'),
    (re.compile(r'(record_index\s*[:=]\s*)(\d+)'), 'dec'),
]


def cmd_anchors(args) -> int:
    probe = json.loads(Path(args.probe).read_text(encoding='utf-8'))
    comp = args.component or ''
    info = probe.get('found', {}).get(comp)
    if not info:
        print('探针结果里没有组件 %r；可用的有: %s' % (comp, ', '.join(sorted(probe.get('found', {})))))
        return 1
    new_rva = info.get('new_rva')
    new_slot = None
    if info.get('table') is not None and info.get('owner') is not None:
        new_slot = info['table'] - info['owner']
    print('组件 %s: new_rva=%s candidate_slot=%s' % (comp, hex(new_rva) if new_rva else '?', new_slot))
    for f in args.file:
        p = Path(f)
        text = p.read_text(encoding='utf-8')
        orig = text
        for rx, kind in CONST_PATTERNS:
            def repl(m):
                key = m.group(1)
                if 'owner_rva' in key and new_rva is not None:
                    return key + (('0x%X' % new_rva) if kind == 'hex' else str(new_rva))
                if 'candidate_slot' in key and new_slot is not None:
                    return key + str(new_slot)
                return m.group(0)
            text = rx.sub(repl, text)
        if text == orig:
            print('  %-52s 无变化' % p.name)
            continue
        print('  %-52s 已更新 %d 处常量' % (p.name, sum(1 for a, b in zip(orig.splitlines(), text.splitlines()) if a != b)))
        if not args.dry_run:
            if args.backup:
                bdir = Path(args.backup)
                bdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, bdir / p.name)
            p.write_text(text, encoding='utf-8')
    return 0


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='hd2update', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    for name, fn in (('inspect', cmd_inspect), ('verify', cmd_verify)):
        p = sub.add_parser(name)
        p.add_argument('artifact', nargs='+')
        p.set_defaults(func=fn)

    p = sub.add_parser('variants', help='一次检查/刷新同一模组的多个保留版本')
    p.add_argument('path', nargs='+', help='zip / patch / 目录（目录会取其中的 *.zip）')
    p.add_argument('--dry-run', action='store_true', help='只扫描不写')
    p.add_argument('--backup', help='备份目录')
    p.add_argument('--new-dll'), p.add_argument('--new-exe'), p.add_argument('--new-build')
    p.set_defaults(func=cmd_variants)

    p = sub.add_parser('lock', help='刷新构建锁')
    p.add_argument('artifact', nargs='+')
    p.add_argument('--game-dir', help='游戏根目录（自动读取新 game.dll / helldivers2.exe 哈希）')
    p.add_argument('--new-dll'), p.add_argument('--new-exe'), p.add_argument('--new-build')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--backup', help='备份目录')
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser('probe')
    p.add_argument('probe_cmd', choices=['build', 'parse'])
    p.add_argument('log', nargs='?', help='probe parse: OwnerScout.log')
    p.add_argument('--out')
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser('anchors', help='用探针结果重写锚点常量')
    p.add_argument('file', nargs='+')
    p.add_argument('--probe', required=True)
    p.add_argument('--component', help='探针里 scout_FOUND_DIRECT 的组件名')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--backup')
    p.set_defaults(func=cmd_anchors)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
