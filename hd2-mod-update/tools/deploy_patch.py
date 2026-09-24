#!/usr/bin/env python3
"""deploy_patch - 把一个 addon patch 部署进 HD2 的 data/ 槽位链。

规则（踩过的坑都在里面）：
  * 改 data/ 前必须游戏已退出（脚本自己检查）；
  * 槽位链必须连续：新槽位 = 当前最大槽位 + 1，不留空洞；
  * 已有同款 addon 就原地替换（按资源名哈希匹配，而不是文件名）；
  * 任何被覆盖/新建的文件先备份到 data/_deploy_backup_<时间>/；
  * 顺带写空的 .stream / .gpu_resources（缺了 loader 可能不认）。

用法:
  python deploy_patch.py MyMod/data/9ba626afa44a3aa3.patch_0 --game-dir "<HD2>"            # 预览
  python deploy_patch.py MyMod/data/9ba626afa44a3aa3.patch_0 --game-dir "<HD2>" --apply
  python deploy_patch.py MyMod.zip --game-dir "<HD2>" --apply --slot 42                    # 指定槽位
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
import subprocess
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hd2archive as H  # noqa: E402

CARRIER = '9ba626afa44a3aa3'
PATCH_NAME = CARRIER + '.patch_0'


def load_patch(path: Path) -> bytes:
    if path.is_dir():
        for c in (path / 'data' / PATCH_NAME, path / 'Addon' / PATCH_NAME):
            if c.exists():
                return c.read_bytes()
        hits = sorted(path.rglob(CARRIER + '.patch_*'))
        if not hits:
            raise SystemExit('目录里找不到 %s' % PATCH_NAME)
        return hits[0].read_bytes()
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as z:
            inner = [n for n in z.namelist() if n.endswith(PATCH_NAME)]
            if not inner:
                raise SystemExit('zip 里找不到 %s' % PATCH_NAME)
            return z.read(inner[0])
    return path.read_bytes()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('patch')
    ap.add_argument('--game-dir', required=True)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--slot', type=int, help='强制装到该槽位（默认：下一个空槽）')
    args = ap.parse_args(argv)

    data = Path(args.game_dir) / 'data'
    if not data.exists():
        raise SystemExit('找不到 data 目录: %s' % data)
    if (data / 'game' / 'game.dll').exists() is False:
        raise SystemExit('这不像 HD2 根目录（缺 data/game/game.dll）')

    raw = load_patch(Path(args.patch))
    src = H.parse(raw)
    src_names = [e['name'] for e in src['entries']]

    slots = {}
    for p in data.glob(CARRIER + '.patch_*'):
        if not p.name.split('patch_')[-1].isdigit():
            continue
        try:
            slots[int(p.name.split('patch_')[-1])] = p
        except ValueError:
            pass
    if not slots:
        raise SystemExit('data/ 里没有任何 %s.patch_N —— 确认游戏目录/加载器已装' % CARRIER)

    target_slot, action = None, 'install'
    for n, p in sorted(slots.items()):
        try:
            names = [e['name'] for e in H.parse(p.read_bytes())['entries']]
        except Exception:
            continue
        if names == src_names:
            target_slot, action = n, 'replace'
            break
    if target_slot is None:
        target_slot = args.slot if args.slot is not None else max(slots) + 1
        if target_slot in slots:
            action = 'replace'
    target = data / ('%s.patch_%d' % (CARRIER, target_slot))

    # 连续性检查：不允许在链中间留洞
    holes = [n for n in range(0, max(slots) + 1) if n not in slots]
    print('现有槽位: %d..%d  洞: %s' % (min(slots), max(slots), holes or '无'))
    print('%s -> slot %d (%d bytes, %d addon)' % (action, target_slot, len(raw), src['count']))
    for e in src['entries']:
        m = re.search(rb'-- HD2-Addon: (\S+)', e['body'])
        print('    %s' % (m.group(1).decode() if m else '?'))
    if holes:
        print('⚠ 链上有洞，新的 addon 必须装进洞里才能被加载（--slot 指定）')
    if not args.apply:
        print('（预览；加 --apply 落盘）')
        return 0
    # 落盘前必须确认游戏没在跑
    try:
        out = subprocess.run(['tasklist'], capture_output=True, text=True, errors='replace').stdout.lower()
        if 'helldivers2' in out:
            raise SystemExit('游戏正在运行：请先完全退出游戏再部署（data/ 不能热改）')
    except FileNotFoundError:
        pass

    backup = data / ('_deploy_backup_' + time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(exist_ok=True)
    for sfx in ('', '.stream', '.gpu_resources'):
        p = Path(str(target) + sfx)
        if p.exists():
            shutil.copy2(p, backup / p.name)
    target.write_bytes(raw)
    Path(str(target) + '.stream').write_bytes(b'')
    Path(str(target) + '.gpu_resources').write_bytes(b'')
    back = H.parse(target.read_bytes())
    assert [e['name'] for e in back['entries']] == src_names
    print('已部署: %s   备份: %s' % (target.name, backup))
    return 0


if __name__ == '__main__':
    sys.exit(main())
