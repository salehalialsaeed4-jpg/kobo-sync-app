"""تجميع الأسماء في مجموعات للمراجعة: فريدة / مقترحة بثقة عالية / تحتاج مراجعة / معتمدة."""
import hashlib
import pandas as pd
from cleaner import normalize_arabic, tokens, similarity, build_dynamic_clusters
from database import list_name_values, get_alias_rows

SPELLING_SIM = 0.85


def _reasons(members, canonical):
    """أسباب كون المجموعة تحتاج انتباهاً؛ قائمة فارغة = فروق إملائية فقط."""
    ct = tokens(canonical)
    reasons = []
    for m in members:
        tm = tokens(m)
        if "".join(tm) == "".join(ct):
            continue
        if len(tm) != len(ct):
            reasons.append('اختلاف عدد الكلمات (اسم أقصر أو أطول)')
        elif min(similarity(a, b) for a, b in zip(tm, ct)) < SPELLING_SIM:
            reasons.append('تشابه جزئي بين الكلمات')
    return sorted(set(reasons))


def build_groups(entity_type):
    rows = list_name_values()[0 if entity_type == 'trainer' else 1]
    if not rows:
        return [], ''
    aliases = get_alias_rows(entity_type)
    names = [r['name'] for r in rows]
    suggested = build_dynamic_clusters(pd.Series(names)).tolist()
    members = []
    for r, sug in zip(rows, suggested):
        al = aliases.get(normalize_arabic(r['name']))
        members.append({'name': r['name'], 'n': r['n'], 'aliased': bool(al),
                        'base': al['canonical_name'] if al else sug})
    by = {}
    for m in members:
        by.setdefault(normalize_arabic(m['base']) or m['base'], []).append(m)
    groups = []
    for ms in by.values():
        counts = {}
        for m in ms:
            counts[m['base']] = counts.get(m['base'], 0) + m['n']
        canonical = max(counts, key=lambda k: (counts[k], len(k)))
        all_aliased = all(m['aliased'] for m in ms)
        if len(ms) == 1 and (not ms[0]['aliased'] or normalize_arabic(ms[0]['base']) == normalize_arabic(ms[0]['name'])):
            status, reasons = 'single', []
        elif all_aliased:
            status, reasons = 'committed', []
        else:
            reasons = _reasons([m['name'] for m in ms], canonical)
            status = 'amber' if reasons else 'green'
        groups.append({'canonical': canonical, 'status': status, 'reasons': reasons,
                       'members': sorted(ms, key=lambda m: -m['n']),
                       'total': sum(m['n'] for m in ms)})
    order = {'amber': 0, 'green': 1, 'committed': 2, 'single': 3}
    groups.sort(key=lambda g: (order[g['status']], g['canonical']))
    for i, g in enumerate(groups):
        g['id'] = f'{entity_type}-{i}'
    sig = hashlib.sha1(repr([(m['name'], m['aliased'], m['base']) for m in members]).encode('utf-8')).hexdigest()[:12]
    return groups, sig


def unresolved_count(entity_type):
    groups, _ = build_groups(entity_type)
    return sum(1 for g in groups if g['status'] == 'amber')
