import json, os
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from database import connect, get_state, set_state, count_submissions

TIMEOUT = (10, 45)
PAGE_SIZE = 1000

# الحقول المنطقية وأسماء الأعمدة المحتملة لها في Kobo (الاسم الفعلي XML أو العنوان العربي)
FIELD_KEYS = {
    'trainer':  ('الاسم الثلاثي للمدرب', 'full_name'),
    'reviewer': ('اسم المقيّم (الاسم والكنية)', 'اسم المقيّم', 'evaluator_name'),
    'province': ('المحافظة', 'w0'),
    'end':      ('end',),
    'score':    ('percentage',),
}


def _flat(row):
    """يضيف نسخة بدون بادئة المجموعة (group/field -> field) إن لم يوجد تعارض، دون تعديل الأصل."""
    flat = dict(row)
    for k, v in row.items():
        if '/' in k:
            flat.setdefault(k.rsplit('/', 1)[1], v)
    return flat


def _find(row, keys):
    """يعيد (المفتاح المطابق، القيمة) أو (None, '')."""
    for key in keys:
        if key in row and row[key] not in (None, ''):
            return key, row[key]
    return None, ''


def _value(row, *keys):
    return _find(row, keys)[1]


def _iso(v):
    return str(v) if v else ''


def _num(v):
    """يحوّل النتيجة إلى رقم أو None (يقبل '85.5' و '85%' و '٨٥' ولا يفشل على نص فارغ)."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace('%', '').replace(',', '.')
    s = s.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    try:
        return float(s)
    except ValueError:
        return None


def _record(raw_row):
    row = _flat(raw_row)
    sid = _value(row, '_id', 'id')
    if sid in ('', None):
        raise ValueError('استجابة من Kobo بلا _id؛ أوقفت المزامنة حفاظاً على سلامة البيانات.')
    suuid = _value(row, '_uuid', 'uuid')
    sub = _iso(_value(row, '_submission_time'))
    end = _iso(_value(row, *FIELD_KEYS['end'])) or sub  # بديل حتى لا يبقى end_time فارغاً فيكسر الفلترة
    return {
        'source_id': str(sid),
        'source_uuid': str(suuid) if suuid else None,
        'submission_time': sub,
        'end_time': end,
        'trainer_name_original': str(_value(row, *FIELD_KEYS['trainer'])).strip(),
        'reviewer_name_original': str(_value(row, *FIELD_KEYS['reviewer'])).strip(),
        'province': str(_value(row, *FIELD_KEYS['province'])).strip(),
        'percentage': _num(_value(row, *FIELD_KEYS['score'])),
        'raw_json': json.dumps(raw_row, ensure_ascii=False, default=str),  # الأصل كما وصل من Kobo
    }


def _config():
    token = os.environ.get('KOBO_API_TOKEN', '').strip()
    asset = os.environ.get('KOBO_ASSET_UID', '').strip()
    server = os.environ.get('KOBO_SERVER', 'https://eu.kobotoolbox.org').strip().rstrip('/')
    if not token or not asset:
        raise RuntimeError('أضف KOBO_API_TOKEN وKOBO_ASSET_UID إلى متغيرات البيئة أولاً.')
    low = (token + asset).lower()
    if 'your_' in low or 'here' in low:
        raise RuntimeError('قيم KOBO_API_TOKEN/KOBO_ASSET_UID ما زالت قيماً تجريبية؛ ضع القيم الحقيقية في Render > Environment.')
    if urlparse(server).scheme != 'https':
        raise RuntimeError('يجب أن يكون عنوان Kobo عبر HTTPS.')
    return token, asset, server


def _get(url, headers, params=None):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f'تعذر الوصول إلى خادم Kobo: {e.__class__.__name__}') from e
    if r.status_code in (401, 403):
        raise RuntimeError('Kobo رفض الرمز (401/403): تأكد من KOBO_API_TOKEN، ومن أن KOBO_SERVER هو نفس الخادم الذي أنشأت عليه الحساب.')
    if r.status_code == 404:
        raise RuntimeError('Kobo لم يجد الاستمارة (404): تأكد من KOBO_ASSET_UID ومن KOBO_SERVER (eu.kobotoolbox.org أو kf.kobotoolbox.org).')
    r.raise_for_status()
    return r.json()


def check_kobo():
    """فحص آمن للربط: لا يكتب شيئاً، ولا يعيد قيم الإجابات ولا الرمز."""
    token, asset, server = _config()
    headers = {'Authorization': f'Token {token}', 'Accept': 'application/json'}
    payload = _get(f'{server}/api/v2/assets/{asset}/data/', headers, {'limit': 1, 'format': 'json'})
    results = payload.get('results') or []
    out = {'kobo_server': server, 'asset_uid_ok': True,
           'responses_in_kobo': payload.get('count'), 'responses_in_database': count_submissions()}
    if not results:
        out['note'] = 'الاتصال سليم لكن لا توجد استجابات في Kobo بعد.'
        return out
    row = _flat(results[0])
    out['fields_found'] = {name: _find(row, keys)[0] for name, keys in FIELD_KEYS.items()}
    out['fields_missing'] = [n for n, k in out['fields_found'].items() if k is None]
    out['all_keys_in_first_response'] = sorted(results[0].keys())
    if out['fields_missing']:
        out['note'] = 'بعض الحقول لم تُطابَق: قارن أسماءها مع all_keys_in_first_response وعدّل FIELD_KEYS في kobo_sync.py.'
    return out


def sync_from_kobo(full=False):
    token, asset, server = _config()
    endpoint = f'{server}/api/v2/assets/{asset}/data/'
    headers = {'Authorization': f'Token {token}', 'Accept': 'application/json'}
    before = count_submissions()
    last_id = 0 if full else int(get_state('last_id', '0') or 0)
    params = {'limit': PAGE_SIZE, 'query': json.dumps({'_id': {'$gte': last_id}})}
    url = endpoint
    fetched = inserted = updated = 0
    max_id = int(get_state('last_id', '0') or 0)
    started = datetime.now(timezone.utc).isoformat()

    with connect() as con:
        cur = con.execute("INSERT INTO sync_logs(started_at,status,before_count) VALUES(%s,%s,%s) RETURNING id",
                          (started, 'running', before))
        row = cur.fetchone()
        log_id = row['id'] if row else None

    try:
        while url:
            payload = _get(url, headers, params if url == endpoint else None)
            results = payload.get('results')
            if not isinstance(results, list):
                raise RuntimeError('استجابة Kobo غير متوقعة: لم نعثر على results.')
            if not results:
                break
            batch = []
            for item in results:
                rec = _record(item)
                batch.append(rec)
                fetched += 1
                try:
                    max_id = max(max_id, int(rec['source_id']))
                except ValueError:
                    pass

            with connect() as con:
                for r in batch:
                    # إدراج جديد، أو تحديث فقط إذا تغيّر محتوى الاستجابة (تعديل داخل Kobo)
                    cur = con.execute('''
                        INSERT INTO submissions(source_id,source_uuid,submission_time,end_time,
                          trainer_name_original,reviewer_name_original,province,percentage,raw_json,created_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (source_id) DO UPDATE SET
                          source_uuid=EXCLUDED.source_uuid, submission_time=EXCLUDED.submission_time,
                          end_time=EXCLUDED.end_time, trainer_name_original=EXCLUDED.trainer_name_original,
                          reviewer_name_original=EXCLUDED.reviewer_name_original, province=EXCLUDED.province,
                          percentage=EXCLUDED.percentage, raw_json=EXCLUDED.raw_json
                        WHERE submissions.raw_json IS DISTINCT FROM EXCLUDED.raw_json
                        RETURNING (xmax = 0) AS inserted
                    ''', (r['source_id'], r['source_uuid'], r['submission_time'], r['end_time'],
                          r['trainer_name_original'], r['reviewer_name_original'], r['province'],
                          r['percentage'], r['raw_json'], datetime.now(timezone.utc).isoformat()))
                    res = cur.fetchone()
                    if res:
                        if res['inserted']:
                            inserted += 1
                        else:
                            updated += 1
            url = payload.get('next')
            if url and urlparse(url).hostname != urlparse(server).hostname:
                raise RuntimeError('رابط الصفحة التالية من Kobo لا يطابق خادم Kobo المحدد؛ أوقفت الطلب لحماية رمز API.')
            params = None

        after = count_submissions()
        msg = 'اكتملت المزامنة' + (' (كاملة)' if full else '') + (f' — معدَّل: {updated}' if updated else '')
        with connect() as con:
            set_state(con, 'last_id', max_id)
            set_state(con, 'last_success_at', datetime.now(timezone.utc).isoformat())
            con.execute("""
                UPDATE sync_logs SET finished_at=%s, status='success', after_count=%s, inserted_count=%s,
                  fetched_count=%s, last_id=%s, message=%s WHERE id=%s
            """, (datetime.now(timezone.utc).isoformat(), after, inserted, fetched, str(max_id), msg, log_id))
        return {'before': before, 'after': after, 'inserted': inserted, 'updated': updated,
                'fetched': fetched, 'last_id': max_id}

    except Exception as exc:
        with connect() as con:
            con.execute("""
                UPDATE sync_logs SET finished_at=%s, status='failed', after_count=%s, inserted_count=%s,
                  fetched_count=%s, last_id=%s, message=%s WHERE id=%s
            """, (datetime.now(timezone.utc).isoformat(), count_submissions(), inserted, fetched,
                  str(max_id), str(exc)[:1000], log_id))
        raise
