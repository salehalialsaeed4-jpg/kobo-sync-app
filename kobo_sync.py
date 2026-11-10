import json, os
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from database import connect, get_state, set_state, count_submissions

TIMEOUT=(10,45)
PAGE_SIZE=1000

def _value(row,*keys):
    for key in keys:
        if key in row and row[key] not in (None,''):
            return row[key]
    return ''

def _iso(v):
    if not v: return ''
    return str(v)

def _record(row):
    sid=_value(row,'_id','id')
    if sid in ('',None):
        raise ValueError('استجابة من Kobo بلا _id؛ أوقفت المزامنة حفاظاً على سلامة البيانات.')
    suuid=_value(row,'_uuid','uuid')
    end=_value(row,'end')
    sub=_value(row,'_submission_time')
    return {
      'source_id':str(sid), 'source_uuid':str(suuid) if suuid else None,
      'submission_time':_iso(sub), 'end_time':_iso(end),
      'trainer_name_original':str(_value(row,'الاسم الثلاثي للمدرب','full_name')).strip(),
      'reviewer_name_original':str(_value(row,'اسم المقيّم (الاسم والكنية)','اسم المقيّم','evaluator_name')).strip(),
      'province':str(_value(row,'المحافظة','w0')).strip(),
      'percentage':row.get('percentage'), 'raw_json':json.dumps(row,ensure_ascii=False,default=str)
    }

def sync_from_kobo():
    token=os.environ.get('KOBO_API_TOKEN','').strip()
    asset=os.environ.get('KOBO_ASSET_UID','').strip()
    server=os.environ.get('KOBO_SERVER','https://eu.kobotoolbox.org').rstrip('/')
    if not token or not asset:
        raise RuntimeError('أضف KOBO_API_TOKEN وKOBO_ASSET_UID إلى متغيرات البيئة أولاً.')
    if urlparse(server).scheme!='https':
        raise RuntimeError('يجب أن يكون عنوان Kobo عبر HTTPS.')
    endpoint=f'{server}/api/v2/assets/{asset}/data/'
    headers={'Authorization':f'Token {token}','Accept':'application/json'}
    before=count_submissions()
    last_id=int(get_state('last_id','0') or 0)
    params={'limit':PAGE_SIZE,'query':json.dumps({'_id':{'$gte':last_id}})}
    url=endpoint
    fetched=inserted=0
    max_id=last_id
    started=datetime.now(timezone.utc).isoformat()
    
    with connect() as con:
        cur=con.execute("INSERT INTO sync_logs(started_at,status,before_count) VALUES(%s,%s,%s) RETURNING id",(started,'running',before))
        row = cur.fetchone()
        log_id = row['id'] if row else None

    try:
        while url:
            response=requests.get(url,headers=headers,params=params if url==endpoint else None,timeout=TIMEOUT)
            response.raise_for_status()
            payload=response.json()
            results=payload.get('results')
            if not isinstance(results,list):
                raise RuntimeError('استجابة Kobo غير متوقعة: لم نعثر على results.')
            if not results:
                break
            batch=[]
            for item in results:
                rec=_record(item); batch.append(rec)
                fetched+=1
                try: max_id=max(max_id,int(rec['source_id']))
                except ValueError: pass
            
            with connect() as con:
                for r in batch:
                    cur=con.execute('''
                        INSERT INTO submissions(source_id,source_uuid,submission_time,end_time,
                          trainer_name_original,reviewer_name_original,province,percentage,raw_json,created_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (source_id) DO NOTHING
                    ''', (
                      r['source_id'],r['source_uuid'],r['submission_time'],r['end_time'],r['trainer_name_original'],
                      r['reviewer_name_original'],r['province'],r['percentage'],r['raw_json'],datetime.now(timezone.utc).isoformat()
                    ))
                    if cur.rowcount > 0:
                        inserted+=1
            url=payload.get('next')
            if url:
                next_host=urlparse(url).hostname
                if next_host != urlparse(server).hostname:
                    raise RuntimeError('رابط الصفحة التالية من Kobo لا يطابق خادم Kobo المحدد؛ أوقفت الطلب لحماية رمز API.')
            params=None
            
        after=count_submissions()
        with connect() as con:
            set_state(con,'last_id',max_id)
            set_state(con,'last_success_at',datetime.now(timezone.utc).isoformat())
            con.execute("""
                UPDATE sync_logs SET finished_at=%s, status='success', after_count=%s, inserted_count=%s, fetched_count=%s, last_id=%s, message=%s 
                WHERE id=%s
            """, (datetime.now(timezone.utc).isoformat(), after, inserted, fetched, str(max_id), 'اكتملت المزامنة', log_id))
        return {'before':before,'after':after,'inserted':inserted,'fetched':fetched,'last_id':max_id}
        
    except Exception as exc:
        with connect() as con:
            con.execute("""
                UPDATE sync_logs SET finished_at=%s, status='failed', after_count=%s, inserted_count=%s, fetched_count=%s, last_id=%s, message=%s 
                WHERE id=%s
            """, (datetime.now(timezone.utc).isoformat(), count_submissions(), inserted, fetched, str(max_id), str(exc)[:1000], log_id))
        raise