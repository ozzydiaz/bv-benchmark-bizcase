import openpyxl
wb = openpyxl.load_workbook('RVTools_export_VCP003_2026-01-05_13.14.03.xlsx', data_only=True, read_only=True)
ws = wb['vDisk']
rows = list(ws.iter_rows(values_only=True))
h = rows[0]
ci_vm = h.index('VM'); ci_ps = h.index('Powerstate'); ci_cap = h.index('Capacity MiB'); ci_tmpl = h.index('Template')
seen = {}
for row in rows[1:]:
    if row[ci_tmpl] is True: continue
    vm = row[ci_vm]
    cap = row[ci_cap]
    ps = str(row[ci_ps] or '').lower()
    if ps == 'poweredon':
        if vm not in seen: seen[vm] = []
        if isinstance(cap, (int, float)): seen[vm].append(int(cap))

for vm, caps in list(seen.items())[:5]:
    total_gb = sum(c / 953.67 for c in caps)
    print(f'  {vm}: {len(caps)} disk(s), caps={caps} MiB, total={total_gb:.0f} GB')

print(f'\n  Total unique powered-on VMs with disk data: {len(seen)}')
all_caps_mib = [c for caps in seen.values() for c in caps]
print(f'  Total disk entries: {len(all_caps_mib)}')
from collections import Counter
# Distribution of disk sizes in GiB (rounded to nearest 100)
buckets = Counter()
for c in all_caps_mib:
    gb = c / 953.67
    if gb <= 32: buckets['<=32 GB'] += 1
    elif gb <= 64: buckets['<=64 GB'] += 1
    elif gb <= 128: buckets['<=128 GB'] += 1
    elif gb <= 256: buckets['<=256 GB'] += 1
    elif gb <= 512: buckets['<=512 GB'] += 1
    elif gb <= 1024: buckets['<=1 TB'] += 1
    elif gb <= 2048: buckets['<=2 TB'] += 1
    else: buckets['>2 TB'] += 1
for k, v in buckets.most_common():
    print(f'    {k:12s}: {v:5,} disks')
