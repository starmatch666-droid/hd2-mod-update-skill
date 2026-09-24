"""Extract complete, byte-contiguous LDLD tables from the read-only scout log."""
from pathlib import Path
import hashlib
import json
import re

root = Path(__file__).parent
log = Path(r'C:\Users\STARM\AppData\Local\CowboyBingus\Helldivers2\Logs\OwnerScout.log')
out = root / 'scout-070-tables'
out.mkdir(exist_ok=True)
chunks = {}
complete = {}
starts = {}
for line in log.open(encoding='utf-8', errors='replace'):
    if 'table_found ' in line:
        m = re.search(r'table_found name=(\S+) address=0x([0-9A-Fa-f]+) size=(\d+)', line)
        if m:
            starts[(m[1], int(m[2], 16))] = int(m[3]) + 24
    elif 'table_bytes ' in line:
        m = re.search(r'table_bytes name=(\S+) address=0x([0-9A-Fa-f]+) offset=(\d+) hex=([0-9a-f]+)', line)
        if m:
            chunks.setdefault((m[1], int(m[2], 16)), {})[int(m[3])] = bytes.fromhex(m[4])
    elif 'table_dump_complete ' in line:
        m = re.search(r'table_dump_complete name=(\S+) address=0x([0-9A-Fa-f]+) bytes=(\d+)', line)
        if m:
            complete[(m[1], int(m[2], 16))] = int(m[3])
index = []
for key, size in complete.items():
    expected = starts.get(key)
    if expected != size:
        raise ValueError(f'size mismatch for {key}: {expected} vs {size}')
    segments = chunks.get(key, {})
    cursor = 0
    data = bytearray()
    for offset, raw in sorted(segments.items()):
        if offset != cursor:
            raise ValueError(f'gap/overlap at {key}: {offset} vs {cursor}')
        data.extend(raw)
        cursor += len(raw)
    if len(data) != size or data[:4] != b'LDLD':
        raise ValueError(f'invalid table {key}: {len(data)} vs {size}')
    name, address = key
    filename = f'{name}-{address:x}.bin'
    (out / filename).write_bytes(data)
    index.append(dict(name=name, address=f'0x{address:x}', size=size,
                      sha256=hashlib.sha256(data).hexdigest(), file=filename))
(out / 'index.json').write_text(json.dumps(index, indent=2), encoding='utf-8')
print(f'found={len(starts)} complete={len(complete)} extracted={len(index)}')
for item in index:
    print(item['name'], item['size'], item['address'])
