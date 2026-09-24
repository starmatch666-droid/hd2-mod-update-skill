#!/usr/bin/env python3
"""hd2modupdate - 游戏更新后，一键把你的 HD2 Lua mod 工程更新到新构建。

这就是我们自用的 update_mods.py 的通用版：
  * 不再写死 OLD_SHA / OLD_BUILD：从 history.json 里自动识别"哪些 64 位哈希 / 8 位构建号
    是历史构建的锁"，逐个替换成新值；
  * 自动发现工程：给一个根目录，递归找出所有"mod 工程"（含 build.py / src/entry.lua /
    src/reference.json 的目录）；
  * 先把要改的文件整份备份，再原地替换（默认 dry-run，加 --apply 才写盘）；
  * 可选 --build：更新完后自动跑每个工程的构建脚本重新出包；
  * 输出 update-report.json。

用法
  python hd2modupdate.py --root D:\\MyMods --game-dir "C:\\...\\Helldivers 2"          # 预览
  python hd2modupdate.py --root D:\\MyMods --game-dir "C:\\...\\Helldivers 2" --apply  # 落盘
  python hd2modupdate.py --root D:\\MyMods --game-dir "..." --apply --build            # 落盘并重建
  python hd2modupdate.py --root D:\\MyMods --list                                      # 只列工程
  python hd2modupdate.py --patch MyMod.zip --game-dir "..." --apply                    # 直接改成品包

为什么安全：每个 addon 运行时都会自己校验 owner_rva / 表结构 / 行模板，锁只决定"要不要写"。
只刷新锁，绝不会把过期数据写成坏数据——最差情况是日志里一条 no_write。
如果日志出现 owner_rva 或模板不匹配，那需要跑只读探针重新采表（见同目录 README 的 B/C 类）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import hd2archive as H
except ImportError:
    H = None
CARRIER = '9ba626afa44a3aa3'
PATCH_NAME = CARRIER + '.patch_0'

HISTORY = json.loads((HERE / 'history.json').read_text(encoding='utf-8'))
KNOWN_DLL = {b['game_dll_sha256'].lower(): b['build'] for b in HISTORY['builds']}
KNOWN_EXE = {b['exe_sha256'].lower(): b['build'] for b in HISTORY['builds']}
KNOWN_BUILD = {b['build'] for b in HISTORY['builds']}
HEX64 = re.compile(r'[0-9a-fA-F]{64}')
BUILD_NUM = re.compile(r'(?<!\d)(\d{8})(?!\d)')

TEXT_SUFFIX = {'.lua', '.json', '.py', '.md', '.txt', '.ps1', '.bat', '.yml', '.yaml', '.toml'}
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', 'data', 'installation', 'pydeps',
             'outputs', 'release-assets', 'dl', 'distribution'}
SKIP_SUFFIX = {'.zip', '.7z', '.rar', '.png', '.jpg', '.jpeg', '.gif', '.mp4', '.mp3', '.dll', '.exe'}
PROJECT_MARKERS = ('build.py', 'src/entry.lua', 'src/reference.json', 'src/spec.lua')


# --------------------------------------------------------------------------- utils
def new_locks(args):
    dll = exe = build = None
    if args.game_dir:
        gd = Path(args.game_dir)
        d = gd / 'data' / 'game' / 'game.dll'
        e = gd / 'bin' / 'helldivers2.exe'
        if d.exists():
            dll = hashlib.sha256(d.read_bytes()).hexdigest()
        if e.exists():
            exe = hashlib.sha256(e.read_bytes()).hexdigest()
    dll = (args.new_dll or dll or '').lower() or None
    exe = (args.new_exe or exe or '').lower() or None
    build = args.new_build or None
    if args.game_dir and not build:
        m = re.search(r'"buildid"\s*"(\d+)"', (Path(args.game_dir) / 'steamapps' / 'appmanifest_553850.acf').read_text(encoding='utf-8', errors='replace')) \
            if (Path(args.game_dir) / 'steamapps' / 'appmanifest_553850.acf').exists() else None
        build = m.group(1) if m else None
    if args.old_dll:
        KNOWN_DLL[args.old_dll.lower()] = '?'
    if args.old_exe:
        KNOWN_EXE[args.old_exe.lower()] = '?'
    for spec in args.old_build:
        for v in spec.replace(';', ',').split(','):
            v = v.strip()
            if v:
                KNOWN_BUILD.add(v)
    if not dll:
        raise SystemExit('需要 --game-dir 或 --new-dll')
    return dll, exe, build


def replace_locks(text: str, dll: str, exe: str | None, build: str | None, skip: set):
    """Replace historical build locks in one text blob. Returns (new_text, stats)."""
    stats = {'dll': 0, 'exe': 0, 'build': 0}

    def hex_sub(m):
        v = m.group(0)
        low = v.lower()
        if low in skip:
            return v
        if low in KNOWN_DLL and low != dll:
            stats['dll'] += 1
            return dll.upper() if v.isupper() else dll
        if exe and low in KNOWN_EXE and low != exe:
            stats['exe'] += 1
            return exe.upper() if v.isupper() else exe
        return v

    out = HEX64.sub(hex_sub, text)
    if build:
        def num_sub(m):
            v = m.group(1)
            if v in KNOWN_BUILD and v != build:
                stats['build'] += 1
                return build
            return v
        out = BUILD_NUM.sub(num_sub, out)
    return out, stats


def text_files(root: Path, skip: set | None = None):
    for f in sorted(root.rglob('*')):
        if not f.is_file():
            continue
        if f.suffix.lower() in SKIP_SUFFIX or f.suffix.lower() not in TEXT_SUFFIX:
            continue
        if any(part in (skip or SKIP_DIRS) for part in f.relative_to(root).parts):
            continue
        yield f


def discover_projects(root: Path):
    found = []
    for marker in PROJECT_MARKERS:
        for p in root.rglob(marker):
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            proj = p.parent if marker.startswith('src/') else p.parent
            if proj not in found:
                found.append(proj)
    # 去掉互为父子关系的重复项（保留最外层）
    out = []
    for p in sorted(found, key=lambda x: len(x.parts)):
        if not any(p == q or q in p.parents for q in out):
            out.append(p)
    return out


# --------------------------------------------------------------------------- artifact
def update_artifact(path: Path, dll: str, exe: str | None, build: str | None, apply: bool, backup: Path):
    raw = path.read_bytes()
    a = H.parse(raw)
    names_before, pairs, stats = [], [], dict(dll=0, exe=0, build=0, addons=0)
    for e in a['entries']:
        names_before.append(e['name'])
        text = e['body'].decode('utf-8', 'replace')
        new, st = replace_locks(text, dll, exe, build, set())
        for k in st:
            stats[k] += st[k]
        if st['dll'] or st['exe'] or st['build']:
            stats['addons'] += 1
        pairs.append((e['name'], H.envelope(new.encode('utf-8'))))
    if not stats['addons']:
        return stats
    if apply:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup / path.name)
        path.write_bytes(H.make_archive_raw(pairs))
        chk = H.parse(path.read_bytes())
        assert [x['name'] for x in chk['entries']] == names_before, '资源名哈希必须保持不变'
    return stats


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='hd2modupdate', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', help='工程根目录（递归找 mod 工程）')
    ap.add_argument('--patch', action='append', default=[], help='直接更新成品包（patch/zip/dir），可重复')
    ap.add_argument('--game-dir', help='游戏根目录（自动取新锁）')
    ap.add_argument('--new-dll'), ap.add_argument('--new-exe'), ap.add_argument('--new-build')
    ap.add_argument('--old-dll'), ap.add_argument('--old-exe'),
    ap.add_argument('--old-build', action='append', default=[],
                    help='history.json 里没有的旧构建号，可重复；支持逗号分隔')
    ap.add_argument('--apply', action='store_true', help='真正写盘（默认 dry-run）')
    ap.add_argument('--build', action='store_true', help='更新后运行每个工程的构建脚本')
    ap.add_argument('--only', help='只处理名字包含该子串的工程')
    ap.add_argument('--list', action='store_true', help='只列出发现的工程')
    ap.add_argument('--yes', action='store_true', help='不交互')
    args = ap.parse_args(argv)

    if not args.root and not args.patch:
        ap.error('至少要给 --root 或 --patch')
    if args.game_dir and not Path(args.game_dir).exists():
        ap.error('--game-dir 不存在: %s' % args.game_dir)

    if args.list and args.root:
        projs = discover_projects(Path(args.root).resolve())
        print('发现 %d 个工程:' % len(projs))
        for p in projs:
            print('  %s' % p)
        return 0

    dll, exe, build = new_locks(args)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = HERE / ('backup-pre-%s-%s' % (build or 'new', stamp))
    print('新锁: game.dll=%s%s%s' % (dll[:16] + '...', '  exe=%s...' % exe[:16] if exe else '',
                                     '  build=%s' % build if build else ''))
    mode = 'APPLY' if args.apply else 'DRY-RUN'
    print('模式: %s   备份目录: %s\n' % (mode, backup))

    report = []

    # 1) 成品包
    for spec in args.patch:
        p = Path(spec)
        if p.is_dir():
            cands = [p / 'data' / PATCH_NAME, p / 'Addon' / PATCH_NAME]
            p = next((c for c in cands if c.exists()), p)
        st = update_artifact(p, dll, exe, build, args.apply, backup / p.name)
        print('%-56s dll=%d exe=%d build=%d %s' % (p.name, st['dll'], st['exe'], st['build'],
                                                   '已更新' if st['addons'] and args.apply else
                                                   ('待更新' if st['addons'] else '无需修改')))
        report.append(dict(kind='artifact', path=str(p), **st))

    # 2) 工程树
    if args.root:
        projs = discover_projects(Path(args.root).resolve())
        if args.only:
            projs = [p for p in projs if args.only in str(p)]
        print('工程 %d 个:' % len(projs))
        for proj in projs:
            files_touched, total = [], dict(dll=0, exe=0, build=0)
            for f in text_files(proj):
                try:
                    text = f.read_text(encoding='utf-8')
                except (UnicodeDecodeError, OSError):
                    continue
                new, st = replace_locks(text, dll, exe, build, set())
                if st['dll'] or st['exe'] or st['build']:
                    files_touched.append(f.relative_to(proj).as_posix())
                    for k in st:
                        total[k] += st[k]
                    if args.apply:
                        bak = backup / proj.name / f.relative_to(proj)
                        bak.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, bak)
                        f.write_text(new, encoding='utf-8')
            ref = proj / 'src' / 'reference.json'
            stamped = False
            if ref.exists() and build and args.apply:
                try:
                    data = json.loads(ref.read_text(encoding='utf-8'))
                    if isinstance(data, dict) and data.get('gameBuild') != int(build):
                        data['gameBuild'] = int(build)
                        ref.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                        stamped = True
                except (json.JSONDecodeError, OSError):
                    pass
            print('  %-44s dll=%-2d exe=%-2d build=%-2d %s%s' % (
                proj.name[:44], total['dll'], total['exe'], total['build'],
                ('%d 个文件' % len(files_touched)) if files_touched else '无需修改',
                '  [stamp]' if stamped else ''))
            report.append(dict(kind='project', path=str(proj), stamped=stamped,
                               files=files_touched, **total))
            if args.build:
                script = proj / 'build.py'
                if script.exists():
                    r = subprocess.run([sys.executable, str(script)], cwd=str(proj),
                                       capture_output=True, text=True, encoding='utf-8', errors='replace')
                    tail = [l for l in (r.stdout or '').strip().splitlines() if l.strip()]
                    print('      build: %s' % ('OK ' + tail[-1][-60:] if r.returncode == 0
                                               else 'FAIL ' + (r.stderr or '')[-80:].replace('\n', ' ')))
                bundle = proj / 'rebuild_bundle.py'
                if bundle.exists():
                    r = subprocess.run([sys.executable, str(bundle)], cwd=str(proj),
                                       capture_output=True, text=True, encoding='utf-8', errors='replace')
                    print('      bundle: %s' % ('OK' if r.returncode == 0 else 'FAIL'))

    (HERE / ('update-report-%s.json' % stamp)).write_text(
        json.dumps(dict(new=dict(game_dll_sha256=dll, exe_sha256=exe, build=build),
                        mode=mode, items=report), ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print('\n报告: update-report-%s.json' % stamp)
    if not args.apply:
        print('（这是预览。确认无误后加 --apply 落盘）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
