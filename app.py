import os, json, hmac, secrets
from io import BytesIO
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify
import pandas as pd
from database import ensure_db, db_ok, connect, count_submissions, get_state, add_candidate, list_name_values, save_alias, get_submissions, list_filter_values, save_processing_run, get_processing_run, list_candidate_keys
from kobo_sync import sync_from_kobo
from processing import build_merged, excel_bytes
from cleaner import normalize_arabic

app=Flask(__name__)
app.secret_key=os.environ.get('FLASK_SECRET_KEY') or os.urandom(32)
app.config['MAX_CONTENT_LENGTH']=8*1024*1024
try:
    ensure_db()
except Exception as _e:
    app.logger.error('Database not ready at boot (will retry on requests): %s', _e)

@app.before_request
def load_role():
    if request.endpoint in ('login','static','health'):
        return
    try:
        ensure_db()
    except Exception as e:
        app.logger.error('Database unavailable: %s', e)
        return ('قاعدة البيانات غير متاحة حالياً. حاول بعد قليل. إذا استمرت المشكلة فراجع إعداد DATABASE_URL.', 503)
    if request.method=='POST':
        expected=session.get('_csrf_token','')
        supplied=request.form.get('csrf_token','')
        if not expected or not hmac.compare_digest(expected,supplied):
            return 'طلب غير صالح. أعد تحميل الصفحة وحاول مجدداً.',400
    if not session.get('role'):
        return redirect(url_for('login'))

@app.context_processor
def inject_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token']=secrets.token_urlsafe(32)
    return {'csrf_token':session['_csrf_token']}

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        role=request.form.get('role','')
        password=request.form.get('password','')
        expected=os.environ.get('ENTRY_PASSWORD' if role=='entry' else 'ADMIN_PASSWORD','')
        if role in ('entry','admin') and expected and hmac.compare_digest(password,expected):
            session.clear(); session['role']=role
            return redirect(url_for('entry' if role=='entry' else 'dashboard'))
        flash('بيانات الدخول غير صحيحة أو لم يتم ضبط كلمة المرور في إعدادات التطبيق.','danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapped(*a,**kw):
            if session.get('role') not in roles:
                flash('ليس لديك صلاحية الوصول لهذه الصفحة.','warning')
                return redirect(url_for('login'))
            return fn(*a,**kw)
        return wrapped
    return deco

@app.route('/')
def home():
    return redirect(url_for('entry' if session.get('role')=='entry' else 'dashboard'))

@app.route('/entry',methods=['GET','POST'])
@roles_required('entry','admin')
def entry():
    provinces=['دمشق وريفها','حلب','حماه','حمص','إدلب','اللاذقية','طرطوس','دير الزور','الرقة','الحسكة','درعا','القنيطرة','السويداء']
    if request.method=='POST':
        name=' '.join(request.form.get('full_name','').split())
        province=request.form.get('province','').strip()
        if len(name.split())<3 or not province:
            flash('أدخل الاسم الثلاثي والمحافظة.','warning')
        else:
            nid=normalize_arabic(name); np=normalize_arabic(province)
            new_id,duplicate=add_candidate(name,province,nid,np)
            if duplicate:
                flash(f'تنبيه: المرشح مسجل مسبقاً باسم {duplicate["full_name"]} في {duplicate["province"]}.','warning')
            else:
                flash('تم تسجيل المرشح بنجاح.','success')
        return redirect(url_for('entry'))
    with connect() as con:
        candidates=[dict(r) for r in con.execute('SELECT * FROM candidates ORDER BY id DESC LIMIT 100')]
    return render_template('entry.html',provinces=provinces,candidates=candidates)

@app.route('/admin')
@roles_required('admin')
def dashboard():
    with connect() as con:
        logs=[dict(r) for r in con.execute('SELECT * FROM sync_logs ORDER BY id DESC LIMIT 8')]
    return render_template('dashboard.html',total=count_submissions(),last_id=get_state('last_id','0'),last_sync=get_state('last_success_at','لم تتم المزامنة بعد'),logs=logs)

@app.route('/sync',methods=['POST'])
@roles_required('admin')
def sync():
    try:
        result=sync_from_kobo()
        flash(f"اكتملت المزامنة: قبل {result['before']}، بعد {result['after']}، استجابات جديدة {result['inserted']}، تمت مراجعة {result['fetched']}.",'success')
    except Exception as e:
        flash(f'فشلت المزامنة: {e}','danger')
    return redirect(url_for('dashboard'))

@app.route('/names',methods=['GET','POST'])
@roles_required('admin')
def names():
    if request.method=='POST':
        typ=request.form.get('entity_type'); original=request.form.get('original_name','').strip(); canonical=' '.join(request.form.get('canonical_name','').split())
        if typ in ('trainer','reviewer') and original and canonical:
            save_alias(typ,original,normalize_arabic(original),canonical)
            flash('تم حفظ الاسم المعتمد.','success')
        else: flash('تحقق من الاسم ونوعه.','warning')
        return redirect(url_for('names'))
    trainers,reviewers=list_name_values()
    from cleaner import build_dynamic_clusters
    for group in (trainers,reviewers):
        if group:
            suggested=build_dynamic_clusters(pd.Series([x['name'] for x in group])).tolist()
            for item,proposal in zip(group,suggested): item['suggested']=proposal
    return render_template('names.html',trainers=trainers,reviewers=reviewers)

@app.route('/process',methods=['GET','POST'])
@roles_required('admin')
def process():
    provinces,dates=list_filter_values()
    if request.method=='POST':
        province=request.form.get('province','all'); date_from=request.form.get('date_from',''); date_to=request.form.get('date_to','')
        try:
            raw=get_submissions(province,date_from,date_to)
            if not raw: raise ValueError('لا توجد استجابات ضمن النطاق المحدد.')
            from database import get_aliases
            from processing import first_col, COL
            df=pd.DataFrame(raw)
            ta=first_col(df,COL['trainer']); ra=first_col(df,COL['reviewer'])
            pa=first_col(df,COL['province']); ea=first_col(df,COL['end']); sa=first_col(df,COL['score'])
            trainer_alias=get_aliases('trainer'); reviewer_alias=get_aliases('reviewer')
            canonical_trainers=df[ta].map(lambda x: trainer_alias.get(normalize_arabic(str(x)),x)) if ta else pd.Series(['']*len(df))
            provinces_series=df[pa].fillna('').astype(str) if pa else pd.Series(['']*len(df))
            registered=list_candidate_keys()
            registration_status=['مسجل' if (normalize_arabic(str(n)),normalize_arabic(str(p))) in registered else 'غير مسجل' for n,p in zip(canonical_trainers,provinces_series)]
            preview=df.copy()
            if ta: preview[ta]=canonical_trainers
            if ra: preview[ra]=df[ra].map(lambda x: reviewer_alias.get(normalize_arabic(str(x)),x))
            preview['حالة تسجيل المرشح']=registration_status
            session['pending_scope']={'province':province,'date_from':date_from,'date_to':date_to}
            return render_template('preview.html',row_count=len(df),table=preview.to_html(index=False,classes='data-table',border=0,na_rep=''),province=province,date_from=date_from,date_to=date_to)
        except Exception as e:
            flash(f'تعذر عرض البيانات: {e}','danger')
    return render_template('process.html',provinces=provinces,dates=dates)

@app.route('/merge',methods=['POST'])
@roles_required('admin')
def merge():
    scope=session.get('pending_scope')
    if not scope:
        flash('حدد نطاق المعالجة وراجعه أولاً.','warning')
        return redirect(url_for('process'))
    try:
        raw=get_submissions(scope['province'],scope['date_from'],scope['date_to'])
        if not raw: raise ValueError('لا توجد استجابات ضمن النطاق المحدد.')
        merged=build_merged(pd.DataFrame(raw))
        run_id=save_processing_run(scope['province'],scope['date_from'],scope['date_to'],merged.to_dict('records'))
        session.pop('pending_scope',None)
        return redirect(url_for('results',run_id=run_id))
    except Exception as e:
        flash(f'تعذرت عملية الدمج: {e}','danger')
        return redirect(url_for('process'))

@app.route('/results/<int:run_id>')
@roles_required('admin')
def results(run_id):
    run=get_processing_run(run_id)
    if not run: flash('نتيجة المعالجة غير موجودة.','warning'); return redirect(url_for('process'))
    df=pd.DataFrame(json.loads(run['result_json']))
    return render_template('results.html',run=run,table=df.to_html(index=False,classes='data-table',border=0,na_rep=''))

@app.route('/export/<int:run_id>/<kind>')
@roles_required('admin')
def export(run_id,kind):
    run=get_processing_run(run_id)
    if not run: return 'النتيجة غير موجودة',404
    df=pd.DataFrame(json.loads(run['result_json']))
    stamp=datetime.now().strftime('%Y%m%d_%H%M')
    if kind=='xlsx':
        return send_file(excel_bytes(df),as_attachment=True,download_name=f'interview_results_{stamp}.xlsx',mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    if kind=='pdf':
        from pdf_report import build_pdf_report
        try:
            buf=build_pdf_report(df)
            return send_file(buf,as_attachment=True,download_name=f'interview_results_{stamp}.pdf',mimetype='application/pdf')
        except Exception as e:
            flash(f'تعذر إنشاء PDF: {e}','danger'); return redirect(url_for('results',run_id=run_id))
    return 'نوع الملف غير مدعوم',404

@app.route('/health')
def health():
    ok,err=db_ok()
    if not ok:
        return jsonify(status='degraded',database='postgres',error=err),503
    return jsonify(status='ok',database='postgres',submissions=count_submissions())

if __name__=='__main__':
    app.run(debug=False,port=int(os.environ.get('PORT',8000)))
