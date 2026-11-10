import json
import os
import time
import logging
import threading
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote
import psycopg2
import psycopg2.extras
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger('db')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS candidates (
 id SERIAL PRIMARY KEY,
 full_name TEXT NOT NULL,
 normalized_name TEXT NOT NULL,
 province TEXT NOT NULL,
 normalized_province TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(normalized_name, normalized_province)
);

CREATE TABLE IF NOT EXISTS submissions (
 id SERIAL PRIMARY KEY,
 source_id TEXT NOT NULL UNIQUE,
 source_uuid TEXT UNIQUE,
 submission_time TEXT,
 end_time TEXT,
 trainer_name_original TEXT NOT NULL DEFAULT '',
 reviewer_name_original TEXT NOT NULL DEFAULT '',
 province TEXT NOT NULL DEFAULT '',
 percentage DOUBLE PRECISION,
 raw_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_submissions_end ON submissions(end_time);
CREATE INDEX IF NOT EXISTS idx_submissions_province ON submissions(province);
CREATE INDEX IF NOT EXISTS idx_submissions_trainer ON submissions(trainer_name_original);

CREATE TABLE IF NOT EXISTS name_aliases (
 id SERIAL PRIMARY KEY,
 entity_type TEXT NOT NULL CHECK(entity_type IN ('trainer','reviewer')),
 original_name TEXT NOT NULL,
 normalized_original TEXT NOT NULL,
 canonical_name TEXT NOT NULL,
 approved_at TEXT NOT NULL,
 UNIQUE(entity_type, normalized_original)
);

CREATE TABLE IF NOT EXISTS sync_state (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_logs (
 id SERIAL PRIMARY KEY,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 status TEXT NOT NULL,
 before_count INTEGER NOT NULL DEFAULT 0,
 after_count INTEGER NOT NULL DEFAULT 0,
 inserted_count INTEGER NOT NULL DEFAULT 0,
 fetched_count INTEGER NOT NULL DEFAULT 0,
 last_id TEXT,
 message TEXT
);

CREATE TABLE IF NOT EXISTS processing_runs (
 id SERIAL PRIMARY KEY,
 created_at TEXT NOT NULL,
 province TEXT NOT NULL,
 date_from TEXT,
 date_to TEXT,
 row_count INTEGER NOT NULL,
 result_json TEXT NOT NULL
);

-- أمان: تفعيل RLS بلا سياسات = منع الوصول عبر واجهة Supabase REST العامة. اتصال التطبيق المباشر لا يتأثر.
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE name_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing_runs ENABLE ROW LEVEL SECURITY;
'''

def _clean_db_url():
    """يقرأ DATABASE_URL وينظّفه: مسافات/اقتباسات/CRLF، ويضيف sslmode و connect_timeout."""
    raw = (os.environ.get('DATABASE_URL') or '').strip().strip('"').strip("'").strip()
    if not raw:
        raise RuntimeError('متغير البيئة DATABASE_URL غير معرّف.')
    if raw.startswith('postgres://'):
        raw = 'postgresql://' + raw[len('postgres://'):]
    u = urlparse(raw)
    q = dict(parse_qsl(u.query))
    q.setdefault('sslmode', 'require')
    q.setdefault('connect_timeout', '10')
    q.setdefault('keepalives', '1')
    return urlunparse(u._replace(query=urlencode(q))), u


def _explain(err, u):
    """يحوّل أخطاء الاتصال الشائعة إلى رسالة عربية واضحة تدل على الحل."""
    host = u.hostname or ''
    user = unquote(u.username or '')
    msg = str(err)
    if host.startswith('db.') and host.endswith('.supabase.co'):
        return ('الرابط يستخدم الاتصال المباشر لـ Supabase (IPv6 فقط) وهذه الاستضافة لا تدعم IPv6. '
                'استخدم رابط Session pooler من Supabase > Connect، شكله: '
                'postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres')
    if host.endswith('.pooler.supabase.com') and '.' not in user:
        return ('مع الـ pooler يجب أن يكون اسم المستخدم postgres.<project-ref> وليس postgres فقط.')
    if 'password authentication failed' in msg:
        return 'كلمة مرور قاعدة البيانات خاطئة (تأكد من ترميز الرموز الخاصة مثل @ إلى %40).'
    if 'Tenant or user not found' in msg:
        return 'اسم المستخدم أو المنطقة (region) في رابط الـ pooler غير صحيح، انسخه كما هو من Supabase.'
    return msg


def _open_connection(retries=4):
    url, parsed = _clean_db_url()
    last = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        except psycopg2.OperationalError as e:
            last = e
            log.warning('DB connect attempt %s/%s failed: %s', attempt, retries, str(e).strip().splitlines()[0])
            # أخطاء الإعداد لا فائدة من إعادة المحاولة عليها
            if any(k in str(e) for k in ('password authentication failed', 'Tenant or user not found', 'Network is unreachable')):
                break
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(_explain(last, parsed)) from last


class PostgresConnectionWrapper:
    def __init__(self):
        self.conn = None
        self.cursor = None

    def __enter__(self):
        self.conn = _open_connection()
        self.cursor = self.conn.cursor()
        return self

    def execute(self, query, params=None):
        self.cursor.execute(query, params or ())
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            try:
                if exc_type:
                    self.conn.rollback()
                else:
                    self.conn.commit()
                self.cursor.close()
            finally:
                self.conn.close()

def connect():
    return PostgresConnectionWrapper()

_init_lock = threading.Lock()
_initialized = False

def init_db():
    with connect() as con:
        con.execute(SCHEMA)
        con.execute("INSERT INTO sync_state(key, value) VALUES('last_id', '0') ON CONFLICT (key) DO NOTHING")

def ensure_db():
    """تهيئة الجداول مرة واحدة؛ تُعاد المحاولة عند أي طلب إذا فشلت سابقاً (بدل إسقاط التطبيق عند الإقلاع)."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if not _initialized:
            init_db()
            _initialized = True

def db_ok():
    try:
        with connect() as con:
            con.execute('SELECT 1')
        return True, ''
    except Exception as e:
        return False, str(e)

def count_submissions():
    with connect() as con:
        row = con.execute('SELECT COUNT(*) FROM submissions').fetchone()
        return row['count'] if row else 0

def get_state(key, default=''):
    with connect() as con:
        row = con.execute('SELECT value FROM sync_state WHERE key=%s', (key,)).fetchone()
        return row['value'] if row else default

def set_state(con, key, value):
    con.execute('''
        INSERT INTO sync_state(key, value) VALUES(%s, %s)
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
    ''', (key, str(value)))

def add_candidate(full_name, province, normalized_name, normalized_province):
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        try:
            cur = con.execute('''
                INSERT INTO candidates(full_name, normalized_name, province, normalized_province, created_at)
                VALUES(%s, %s, %s, %s, %s) RETURNING id
            ''', (full_name, normalized_name, province, normalized_province, now))
            row = cur.fetchone()
            return row['id'], None
        except Exception:
            con.conn.rollback()
            row = con.execute('''
                SELECT id, full_name, province FROM candidates 
                WHERE normalized_name=%s AND normalized_province=%s
            ''', (normalized_name, normalized_province)).fetchone()
            return None, dict(row) if row else {'full_name': full_name, 'province': province}

def get_aliases(entity_type):
    with connect() as con:
        rows = con.execute('SELECT normalized_original, canonical_name FROM name_aliases WHERE entity_type=%s', (entity_type,)).fetchall()
        return {r['normalized_original']: r['canonical_name'] for r in rows}

def save_alias(entity_type, original_name, normalized_original, canonical_name):
    with connect() as con:
        con.execute('''
            INSERT INTO name_aliases(entity_type, original_name, normalized_original, canonical_name, approved_at)
            VALUES(%s, %s, %s, %s, %s)
            ON CONFLICT(entity_type, normalized_original) DO UPDATE SET
            original_name=EXCLUDED.original_name, canonical_name=EXCLUDED.canonical_name, approved_at=EXCLUDED.approved_at
        ''', (entity_type, original_name, normalized_original, canonical_name, datetime.now(timezone.utc).isoformat()))

def list_name_values():
    with connect() as con:
        trainers = con.execute("SELECT trainer_name_original AS name, COUNT(*) AS n FROM submissions WHERE trainer_name_original <> '' GROUP BY trainer_name_original ORDER BY name").fetchall()
        reviewers = con.execute("SELECT reviewer_name_original AS name, COUNT(*) AS n FROM submissions WHERE reviewer_name_original <> '' GROUP BY reviewer_name_original ORDER BY name").fetchall()
    return [dict(r) for r in trainers], [dict(r) for r in reviewers]

def _unprefix(d):
    """group/field -> field عندما يكون الاسم القصير فريداً، ليطابق processing.py أسماء الأعمدة المتوقعة."""
    from collections import Counter
    short = {k: k.rsplit('/', 1)[-1] for k in d}
    cnt = Counter(short.values())
    return {(short[k] if cnt[short[k]] == 1 else k): v for k, v in d.items()}

def get_submissions(province='all', date_from='', date_to=''):
    sql = 'SELECT raw_json, trainer_name_original, reviewer_name_original, province AS prov, percentage AS pct, end_time AS endt FROM submissions WHERE 1=1'
    args = []
    if province and province != 'all':
        sql += ' AND province=%s'
        args.append(province)
    if date_from:
        sql += " AND NULLIF(SUBSTR(end_time, 1, 10), '')::DATE >= %s::DATE"
        args.append(date_from)
    if date_to:
        sql += " AND NULLIF(SUBSTR(end_time, 1, 10), '')::DATE <= %s::DATE"
        args.append(date_to)
    sql += ' ORDER BY end_time, CAST(source_id AS INTEGER)'
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = _unprefix(json.loads(r['raw_json']))
        # توحيد الأعمدة: لو اختلفت أسماء الحقول بين نسخ الاستمارة نُكمل من الأعمدة المطبَّعة المحفوظة عند المزامنة
        for key, val in (('الاسم الثلاثي للمدرب', r['trainer_name_original']),
                         ('اسم المقيّم (الاسم والكنية)', r['reviewer_name_original']),
                         ('المحافظة', r['prov']), ('percentage', r['pct']), ('end', r['endt'])):
            if d.get(key) in (None, '') and val not in (None, ''):
                d[key] = val
        out.append(d)
    return out

def list_filter_values():
    with connect() as con:
        provinces = [r['province'] for r in con.execute("SELECT DISTINCT province FROM submissions WHERE province <> '' ORDER BY province").fetchall()]
        dates = [r['sub'] for r in con.execute("SELECT DISTINCT SUBSTR(end_time, 1, 10) AS sub FROM submissions WHERE end_time <> '' ORDER BY 1").fetchall()]
    return provinces, dates

def save_processing_run(province, date_from, date_to, rows):
    payload = json.dumps(rows, ensure_ascii=False, default=str)
    with connect() as con:
        cur = con.execute('''
            INSERT INTO processing_runs(created_at, province, date_from, date_to, row_count, result_json)
            VALUES(%s, %s, %s, %s, %s, %s) RETURNING id
        ''', (datetime.now(timezone.utc).isoformat(), province, date_from, date_to, len(rows), payload))
        return cur.fetchone()['id']

def get_processing_run(run_id):
    with connect() as con:
        row = con.execute('SELECT * FROM processing_runs WHERE id=%s', (run_id,)).fetchone()
        return dict(row) if row else None

def list_candidate_keys():
    with connect() as con:
        rows = con.execute('SELECT normalized_name, normalized_province FROM candidates').fetchall()
    return {(r['normalized_name'], r['normalized_province']) for r in rows}