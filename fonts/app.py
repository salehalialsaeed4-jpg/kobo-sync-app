import os, json, hmac, secrets, time
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify, g
import pandas as pd
from database import (ensure_db, db_ok, connect, count_submissions, get_state, add_candidate, list_name_values,
                      get_submissions, list_provinces, list_dates_by_province, list_scope_rows, save_processing_run, get_processing_run,
                      list_candidate_keys, workflow_snapshot, set_states, now_iso, fail_stale_syncs,
                      get_primary_choices, save_primary_choices, apply_name_changes, last_name_batch,
                      undo_last_name_batch)
from kobo_sync import sync_from_kobo, check_kobo
from processing import (build_merged, excel_bytes, candidate_groups, validate_primaries, weights_for,
                        fmt_pct, candidate_key, MAX_REVIEWERS)
from cleaner import normalize_arabic
from names_service import build_groups, unresolved_count

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32)
if not os.environ.get('FLASK_SECRET_KEY'):
    app.logger.warning('FLASK_SECRET_KEY غير مضبوط: ستنتهي الجلسات عند كل إعادة تشغيل.')
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.json.ensure_ascii = False
try:
    ensure_db()
except Exception as _e:
    app.logger.error('Database not ready at boot (will retry on requests): %s', _e)

PROVINCES = ['دمشق وريفها', 'حلب', 'حماه', 'حمص', 'إدلب', 'اللاذقية', 'طرطوس', 'دير الزور',
             'الرقة', 'الحسكة', 'درعا', 'القنيطرة', 'السويداء']

# ---------------------------------------------------------------- المسار الموجّه
STEPS = [
    (1, 'sync',    'المزامنة',        'dashboard'),
    (2, 'scope',   'تحديد النطاق',    'process'),
    (3, 'names',   'معالجة الأسماء',  'names'),
    (4, 'preview', 'المعالجة والدمج', 'preview'),
    (5, 'results', 'النتائج',         'results'),
]
ENDPOINT_STEP = {'dashboard': 1, 'sync': 1, 'kobo_check': 1,
                 'process': 2, 'process_data': 2,
                 'names': 3, 'names_commit': 3, 'names_undo': 3, 'names_continue': 3,
                 'preview': 4, 'merge': 4, 'results': 5, 'export': 5}


def step_url(n, snap):
    if n == 5:
        return url_for('results', run_id=snap['merged_run']) if snap['merged_run'] else url_for('dashboard')
    return url_for(STEPS[n - 1][3])


def compute_steps(snap):
    done = {1: snap['submissions'] > 0 and bool(snap['last_success_at']),
            2: bool(snap['scope'] and 'provinces' in snap['scope']), 3: snap['names_ok'],
            4: snap['merged_run'] is not None}
    unlocked = {1: True, 2: done[1], 3: done[1] and done[2], 4: done[1] and done[2] and done[3],
                5: done[4]}
    return done, unlocked


def first_open_step(snap):
    done, unlocked = compute_steps(snap)
    for n in (1, 2, 3, 4):
        if not done[n]:
            return n
    return 5


def _snap():
    if 'wf' not in g:
        g.wf = workflow_snapshot()
    return g.wf


def step_required(n):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            snap = _snap()
            _, unlocked = compute_steps(snap)
            if not unlocked[n]:
                target = first_open_step(snap)
                flash(f'أكمل خطوة «{STEPS[target - 1][2]}» أولاً قبل متابعة هذه الخطوة.', 'warning')
                return redirect(step_url(target, snap))
            return fn(*a, **kw)
        return wrapped
    return deco


# ---------------------------------------------------------------- جلسة وحماية
@app.before_request
def load_role():
    if request.endpoint in ('login', 'static', 'health'):
        return
    try:
        ensure_db()
    except Exception as e:
        app.logger.error('Database unavailable: %s', e)
        return ('قاعدة البيانات غير متاحة حالياً. حاول بعد قليل. إذا استمرت المشكلة فراجع إعداد DATABASE_URL.', 503)
    if request.method == 'POST':
        expected = session.get('_csrf_token', '')
        supplied = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
        if not expected or not hmac.compare_digest(expected, supplied):
            if request.is_json or request.headers.get('X-CSRF-Token') is not None:
                return jsonify(ok=False, error='انتهت صلاحية الجلسة. أعد تحميل الصفحة.'), 400
            return 'طلب غير صالح. أعد تحميل الصفحة وحاول مجدداً.', 400
    if not session.get('role'):
        return redirect(url_for('login'))


@app.context_processor
def inject_globals():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_urlsafe(32)
    ctx = {'csrf_token': session['_csrf_token'], 'steps': None}
    if session.get('role') == 'admin' and request.endpoint not in ('login', 'static', 'health', None):
        try:
            snap = _snap()
            done, unlocked = compute_steps(snap)
            cur = ENDPOINT_STEP.get(request.endpoint)
            ctx['steps'] = [{'n': n, 'label': label, 'done': done.get(n, False), 'open': unlocked[n],
                             'current': n == cur, 'url': step_url(n, snap) if unlocked[n] else None}
                            for n, _, label, _ in STEPS]
        except Exception as e:
            app.logger.error('workflow snapshot failed: %s', e)
    return ctx


_FAILS = {}
MAX_FAILS, LOCK_SECONDS = 5, 300


def _client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote_addr or '?')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = _client_ip()
        fails, until = _FAILS.get(ip, (0, 0))
        if until > time.time():
            wait = int((until - time.time()) // 60) + 1
            flash(f'تم إيقاف المحاولات مؤقتاً. حاول بعد {wait} دقيقة.', 'danger')
            return render_template('login.html'), 429
        role = request.form.get('role', '')
        password = request.form.get('password', '')
        expected = os.environ.get('ENTRY_PASSWORD' if role == 'entry' else 'ADMIN_PASSWORD', '')
        if role in ('entry', 'admin') and expected and hmac.compare_digest(password, expected):
            _FAILS.pop(ip, None)
            session.clear(); session['role'] = role
            return redirect(url_for('entry' if role == 'entry' else 'dashboard'))
        fails += 1
        _FAILS[ip] = (fails, time.time() + LOCK_SECONDS if fails >= MAX_FAILS else 0)
        flash('بيانات الدخول غير صحيحة أو لم يتم ضبط كلمة المرور في إعدادات التطبيق.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            if session.get('role') not in roles:
                flash('ليس لديك صلاحية الوصول لهذه الصفحة.', 'warning')
                return redirect(url_for('login'))
            return fn(*a, **kw)
        return wrapped
    return deco


@app.route('/')
def home():
    return redirect(url_for('entry' if session.get('role') == 'entry' else 'dashboard'))


# ---------------------------------------------------------------- تسجيل المرشحين
@app.route('/entry', methods=['GET', 'POST'])
@roles_required('entry', 'admin')
def entry():
    if request.method == 'POST':
        name = ' '.join(request.form.get('full_name', '').split())
        province = request.form.get('province', '').strip()
        if len(name.split()) < 3 or province not in PROVINCES:
            flash('أدخل الاسم الثلاثي والمحافظة.', 'warning')
        else:
            nid = normalize_arabic(name); np_ = normalize_arabic(province)
            new_id, duplicate = add_candidate(name, province, nid, np_)
            if duplicate:
                flash(f'تنبيه: المرشح مسجل مسبقاً باسم {duplicate["full_name"]} في {duplicate["province"]}.', 'warning')
            else:
                flash('تم تسجيل المرشح بنجاح.', 'success')
        return redirect(url_for('entry'))
    with connect() as con:
        candidates = [dict(r) for r in con.execute('SELECT * FROM candidates ORDER BY id DESC LIMIT 100')]
        total = con.execute('SELECT COUNT(*) AS c FROM candidates').fetchone()['c']
    return render_template('entry.html', provinces=PROVINCES, candidates=candidates, total=total)


# ---------------------------------------------------------------- 1) المزامنة
@app.route('/admin')
@roles_required('admin')
def dashboard():
    fail_stale_syncs()
    with connect() as con:
        logs = [dict(r) for r in con.execute('SELECT * FROM sync_logs ORDER BY id DESC LIMIT 8')]
    snap = _snap()
    return render_template('dashboard.html', total=snap['submissions'], last_id=get_state('last_id', '0'),
                           last_sync=snap['last_success_at'], logs=logs,
                           can_continue=snap['submissions'] > 0 and bool(snap['last_success_at']))


@app.route('/sync', methods=['POST'])
@roles_required('admin')
def sync():
    try:
        fail_stale_syncs()
        result = sync_from_kobo(full=request.form.get('full') == '1')
        if result['inserted'] or result['updated']:
            # بيانات جديدة قد تحمل أسماء جديدة: تُعاد مراجعة الأسماء
            set_states(wf_names_ok_at='', data_changed_at=now_iso())
        flash(f"اكتملت المزامنة: قبل {result['before']}، بعد {result['after']}، جديدة {result['inserted']}، معدَّلة {result['updated']}، تمت مراجعة {result['fetched']}.", 'success')
    except Exception as e:
        flash(f'فشلت المزامنة: {e}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/kobo-check')
@roles_required('admin')
def kobo_check():
    try:
        return jsonify(status='ok', **check_kobo())
    except Exception as e:
        return jsonify(status='error', error=str(e)), 502


# ---------------------------------------------------------------- 2) النطاق
def _scope_summary(scope, provinces=None):
    provinces = provinces if provinces is not None else list_provinces()
    names = {p['code']: p['name'] for p in provinces}
    sel = scope.get('provinces') or []
    label = 'كل المحافظات' if not sel else '، '.join(names.get(c, c) for c in sel)
    return label


@app.route('/process', methods=['GET', 'POST'])
@roles_required('admin')
@step_required(2)
def process():
    provinces = list_provinces()
    codes = {p['code'] for p in provinces}
    if request.method == 'POST':
        sel = [c for c in request.form.getlist('provinces') if c]
        date_from = request.form.get('date_from', ''); date_to = request.form.get('date_to', '')
        if any(c not in codes for c in sel):
            flash('محافظة غير صالحة.', 'warning')
        elif date_from and date_to and date_from > date_to:
            flash('تاريخ البداية بعد تاريخ النهاية.', 'warning')
        else:
            scope = {'provinces': [] if len(sel) == len(codes) else sel, 'date_from': date_from, 'date_to': date_to}
            _, total = list_scope_rows(scope, limit=1)
            if not total:
                flash('لا توجد استجابات ضمن النطاق المحدد.', 'warning')
            else:
                # نطاق جديد = أسماء ونتائج تُراجع من جديد (الأسماء المعتمدة سابقاً تبقى محفوظة)
                set_states(wf_scope=json.dumps(scope, ensure_ascii=False), wf_names_ok_at='', wf_merged_run='')
                return redirect(url_for('names'))
    scope = _snap()['scope'] or {}
    return render_template('process.html', provinces=provinces, dates_json=json.dumps(list_dates_by_province()),
                           scope=scope, scope_json=json.dumps(scope, ensure_ascii=False))


@app.route('/process/data')
@roles_required('admin')
@step_required(2)
def process_data():
    scope = {'provinces': [c for c in request.args.getlist('provinces') if c],
             'date_from': request.args.get('date_from', ''), 'date_to': request.args.get('date_to', '')}
    rows, total = list_scope_rows(scope)
    return jsonify(rows=rows, total=total)


# ---------------------------------------------------------------- 3) معالجة الأسماء (ضمن النطاق)
@app.route('/names')
@roles_required('admin')
@step_required(3)
def names():
    scope = _snap()['scope']
    data = {}
    sigs = []
    pending = 0
    for typ in ('trainer', 'reviewer'):
        groups, sig = build_groups(typ, scope)
        data[typ] = groups
        sigs.append(sig)
        pending += sum(1 for x in groups if x['status'] == 'amber')
    return render_template('names.html', data_json=json.dumps({'groups': data, 'signature': '-'.join(sigs)}, ensure_ascii=False),
                           pending=pending, last_batch=last_name_batch(), scope=scope, scope_label=_scope_summary(scope),
                           counts={t: len(data[t]) for t in data})


@app.route('/names/commit', methods=['POST'])
@roles_required('admin')
@step_required(3)
def names_commit():
    payload = request.get_json(silent=True) or {}
    changes = payload.get('changes')
    if not isinstance(changes, list) or not changes or len(changes) > 5000:
        return jsonify(ok=False, error='لا توجد تغييرات صالحة للاعتماد.'), 400
    trainers, reviewers = list_name_values(_snap()['scope'])
    known = {'trainer': {r['name'] for r in trainers}, 'reviewer': {r['name'] for r in reviewers}}
    items = []
    for c in changes:
        if not isinstance(c, dict):
            return jsonify(ok=False, error='صيغة غير صالحة.'), 400
        typ = c.get('entity_type'); original = c.get('original'); canonical = c.get('canonical')
        if typ not in known or not isinstance(original, str) or not isinstance(canonical, str):
            return jsonify(ok=False, error='صيغة غير صالحة.'), 400
        canonical = ' '.join(canonical.split())
        norm = normalize_arabic(original)
        if original not in known[typ] or not canonical or len(canonical) > 200 or not norm:
            return jsonify(ok=False, error=f'اسم غير صالح: {original[:40]}'), 400
        items.append({'entity_type': typ, 'original': original, 'normalized': norm, 'canonical': canonical})
    batch_id = apply_name_changes(items)
    return jsonify(ok=True, count=len(items), batch_id=batch_id)


@app.route('/names/undo', methods=['POST'])
@roles_required('admin')
@step_required(3)
def names_undo():
    bid = undo_last_name_batch()
    if bid is None:
        return jsonify(ok=False, error='لا يوجد اعتماد للتراجع عنه.'), 404
    return jsonify(ok=True, batch_id=bid)


@app.route('/names/continue', methods=['POST'])
@roles_required('admin')
@step_required(3)
def names_continue():
    scope = _snap()['scope']
    left = unresolved_count('trainer', scope) + unresolved_count('reviewer', scope)
    if left:
        flash(f'بقيت {left} مجموعة تحتاج مراجعتك قبل المتابعة.', 'warning')
        return redirect(url_for('names'))
    set_states(wf_names_ok_at=now_iso())
    return redirect(url_for('preview'))


# ---------------------------------------------------------------- 4) المعالجة والدمج
def _preview_context(scope):
    raw = get_submissions(scope)
    if not raw:
        raise ValueError('لا توجد استجابات ضمن النطاق المحدد.')
    df = pd.DataFrame(raw)
    groups = candidate_groups(df)
    registered = list_candidate_keys()
    saved = get_primary_choices()
    for gr in groups:
        gr['registered'] = gr['key'] in registered
        gr['count'] = len(gr['reviewers'])
        gr['too_many'] = gr['count'] > MAX_REVIEWERS
        pn = normalize_arabic(saved.get(gr['key'], ''))
        gr['primary'] = next((r['name'] for r in gr['reviewers'] if normalize_arabic(r['name']) == pn), '') if pn else ''
        gr['weights'] = [fmt_pct(w) for w in weights_for(min(gr['count'], MAX_REVIEWERS))] if gr['count'] else []
    return df, groups, saved


@app.route('/preview')
@roles_required('admin')
@step_required(4)
def preview():
    scope = _snap()['scope']
    try:
        df, groups, _ = _preview_context(scope)
    except Exception as e:
        flash(f'تعذر عرض البيانات: {e}', 'danger')
        return redirect(url_for('process'))
    need = [x for x in groups if x['count'] > 1]
    reviewer_names = sorted({r['name'] for x in need for r in x['reviewers']})
    return render_template('preview.html', groups=groups, scope=scope, scope_label=_scope_summary(scope), row_count=len(df),
                           reviewer_names=reviewer_names, max_reviewers=MAX_REVIEWERS,
                           weight_table={n: [fmt_pct(w) for w in weights_for(n)] for n in range(1, MAX_REVIEWERS + 1)},
                           weights_num={n: weights_for(n) for n in range(1, MAX_REVIEWERS + 1)},
                           unregistered=sum(1 for x in groups if not x['registered']))


@app.route('/merge', methods=['POST'])
@roles_required('admin')
@step_required(4)
def merge():
    scope = _snap()['scope']
    try:
        df, groups, saved = _preview_context(scope)
        by_key = {x['key']: x for x in groups}
        choices = dict(saved)
        try:
            count = int(request.form.get('count', '0'))
        except ValueError:
            count = 0
        for i in range(count):
            t = request.form.get(f't_{i}', ''); p = request.form.get(f'p_{i}', ''); r = request.form.get(f'r_{i}', '')
            key = candidate_key(t, p)
            gr = by_key.get(key)
            if gr and r and normalize_arabic(r) in {normalize_arabic(x['name']) for x in gr['reviewers']}:
                choices[key] = r
        problems = validate_primaries(groups, choices)
        if problems:
            flash(' | '.join(problems[:3]) + (f' (+{len(problems) - 3} أخرى)' if len(problems) > 3 else ''), 'danger')
            save_primary_choices({k: v for k, v in choices.items() if k in by_key})
            return redirect(url_for('preview'))
        save_primary_choices({k: v for k, v in choices.items() if k in by_key})
        merged = build_merged(df, choices)
        sel = scope.get('provinces') or []
        label = 'all' if not sel else _scope_summary(scope)
        run_id = save_processing_run(label, scope.get('date_from', ''), scope.get('date_to', ''), merged.to_dict('records'))
        set_states(wf_merged_run=run_id)
        return redirect(url_for('results', run_id=run_id))
    except Exception as e:
        flash(f'تعذرت عملية الدمج: {e}', 'danger')
        return redirect(url_for('preview'))


# ---------------------------------------------------------------- 5) النتائج
@app.route('/results/<int:run_id>')
@roles_required('admin')
@step_required(5)
def results(run_id):
    run = get_processing_run(run_id)
    if not run:
        flash('نتيجة المعالجة غير موجودة.', 'warning'); return redirect(url_for('process'))
    df = pd.DataFrame(json.loads(run['result_json']))
    snap = _snap()
    stale = bool(snap['data_changed_at'] and run['created_at'] < snap['data_changed_at'])
    rows = []
    final_col = next((c for c in df.columns if c.startswith('النتيجة النهائية')), None)
    for rec in df.to_dict('records'):
        revs = []
        for i in range(1, MAX_REVIEWERS + 1):
            nm = rec.get(f'اسم المقيّم {i}'); sc = rec.get(f'نتيجة المقيّم {i}')
            if nm and not (isinstance(nm, float) and pd.isna(nm)):
                revs.append({'name': nm, 'score': sc})
        score = rec.get(final_col) if final_col else None
        rows.append({'name': rec.get('اسم المدرب', ''), 'province': rec.get('المحافظة', ''), 'date': rec.get('التاريخ', ''),
                     'reviewers': revs, 'notes': rec.get('الملاحظات الكلية', '') or '', 'score': score,
                     'rec': rec.get('النتيجة والتوصية', ''),
                     'cat': 'none' if score is None else ('bad' if score < 50 else 'reserve' if score < 60 else 'cond' if score <= 80 else 'good')})
    rows.sort(key=lambda r: -(r['score'] if r['score'] is not None else -1))
    return render_template('results.html', run=run, rows=rows, stale=stale, single_province=(run['province'] != 'all' and '،' not in run['province']))


@app.route('/export/<int:run_id>/<kind>')
@roles_required('admin')
@step_required(5)
def export(run_id, kind):
    run = get_processing_run(run_id)
    if not run: return 'النتيجة غير موجودة', 404
    df = pd.DataFrame(json.loads(run['result_json']))
    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    if kind == 'xlsx':
        return send_file(excel_bytes(df), as_attachment=True, download_name=f'interview_results_{stamp}.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    if kind == 'pdf':
        from pdf_report import build_pdf_report
        try:
            buf = build_pdf_report(df)
            return send_file(buf, as_attachment=True, download_name=f'interview_results_{stamp}.pdf', mimetype='application/pdf')
        except Exception as e:
            flash(f'تعذر إنشاء PDF: {e}', 'danger'); return redirect(url_for('results', run_id=run_id))
    return 'نوع الملف غير مدعوم', 404


@app.route('/health')
def health():
    ok, err = db_ok()
    if not ok:
        return jsonify(status='degraded', database='postgres', error=err), 503
    return jsonify(status='ok', database='postgres', submissions=count_submissions())


if __name__ == '__main__':
    app.run(debug=False, port=int(os.environ.get('PORT', 8000)))
