import json
os, os
import psycopg2
import psycopg2.extras
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent

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
'''

class PostgresConnectionWrapper:
    def __init__(self):
        self.conn = None
        self.cursor = None

    def __enter__(self):
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            raise RuntimeError('متغير البيئة DATABASE_URL غير معرف أو غير صالح.')
        self.conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
        self.cursor = self.conn.cursor()
        return self

    def execute(self, query, params=None):
        # محاكاة توافقية طريقة التنفيذ مع التعامل مع تحويل الفواصل إن أمكن
        self.cursor.execute(query, params or ())
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
            self.cursor.close()
            self.conn.close()

def connect():
    return PostgresConnectionWrapper()

def init_db():
    with connect() as con:
        con.execute(SCHEMA)
        con.execute("INSERT INTO sync_state(key, value) VALUES('last_id', '0') ON CONFLICT (key) DO NOTHING")

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
            # حدوث تكرار بناءً على القيود الفريدة Unique Constraint
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

def get_submissions(province='all', date_from='', date_to=''):
    sql = 'SELECT raw_json FROM submissions WHERE 1=1'
    args = []
    if province and province != 'all':
        sql += ' AND province=%s'
        args.append(province)
    if date_from:
        sql += " AND SUBSTR(end_time, 1, 10)::DATE >= %s::DATE"
        args.append(date_from)
    if date_to:
        sql += " AND SUBSTR(end_time, 1, 10)::DATE <= %s::DATE"
        args.append(date_to)
    sql += ' ORDER BY end_time, CAST(source_id AS INTEGER)'
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [json.loads(r['raw_json']) for r in rows]

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