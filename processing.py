import json, re
import pandas as pd
from database import get_aliases
from cleaner import normalize_arabic

COL = {
 'trainer': ('الاسم الثلاثي للمدرب','full_name'),
 'reviewer': ('اسم المقيّم (الاسم والكنية)','اسم المقيّم','evaluator_name'),
 'province': ('المحافظة','w0'),
 'end': ('end',),
 'submitted': ('_submission_time',),
 'id': ('_id','id'),
 'score': ('percentage',),
}

def first_col(df, names):
    return next((n for n in names if n in df.columns), None)

def clean(v):
    if pd.isna(v): return ''
    return re.sub(r'\s+',' ',str(v)).strip()

def build_merged(df):
    trainer_col=first_col(df,COL['trainer']); reviewer_col=first_col(df,COL['reviewer'])
    province_col=first_col(df,COL['province']); end_col=first_col(df,COL['end'])
    submitted_col=first_col(df,COL['submitted']); score_col=first_col(df,COL['score'])
    if not all((trainer_col,reviewer_col,province_col,score_col,end_col)):
        missing=[x for x,v in [('اسم المرشح',trainer_col),('اسم المقيّم',reviewer_col),('المحافظة',province_col),('percentage',score_col),('end',end_col)] if not v]
        raise ValueError('أعمدة الاستمارة غير مكتملة: '+', '.join(missing))
    trainer_alias=get_aliases('trainer'); reviewer_alias=get_aliases('reviewer')
    work=df.copy()
    work['_trainer_raw']=work[trainer_col].map(clean)
    work['_reviewer_raw']=work[reviewer_col].map(clean)
    work['_province']=work[province_col].map(clean)
    work['_trainer']=work['_trainer_raw'].map(lambda x: trainer_alias.get(normalize_arabic(x),x))
    work['_reviewer']=work['_reviewer_raw'].map(lambda x: reviewer_alias.get(normalize_arabic(x),x))
    work['_time']=pd.to_datetime(work[submitted_col],errors='coerce') if submitted_col else pd.NaT
    work['_time']=work['_time'].fillna(pd.to_datetime(work[end_col],errors='coerce'))
    work['_order']=range(len(work))
    work=work[(work['_trainer']!='')&(work['_reviewer']!='')].copy()
    if work.empty: raise ValueError('لا توجد استجابات تحتوي على اسم مرشح ومقيّم صالحين.')
    # هوية الدمج تشمل المحافظة لمنع دمج شخصين متشابهَي الاسم في محافظتين مختلفتين.
    key=['_trainer','_province','_reviewer']
    dup=work.duplicated(key,keep=False)
    if dup.any() and work.loc[dup,'_time'].isna().any():
        raise ValueError('هناك تقييمات مكررة بلا توقيت موثوق؛ لا يمكن اختيار أحدث استجابة بأمان.')
    work=work.sort_values(['_time','_order'],kind='stable',na_position='first').drop_duplicates(key,keep='last')
    notes_candidates=[
      'ملاحظات حول الأداء','ملاحظات الخبرة التدريبية','التفاصيل إن وجدت',
      'خلال مسيرتك التدريبية هل ما زلت تحتفظ باسم مدرب ترك فيك أثراً وهو موجود إلى الآن؟',
      'الرأي العام للمقيّم (اكتب رأيك العام بالمرشح)','q6','q9','impact_trainer','q10'
    ]
    note_cols=list(dict.fromkeys(c for c in notes_candidates if c in work.columns))
    result=[]
    for (trainer,province),group in work.groupby(['_trainer','_province'],sort=False,dropna=False):
        group=group.sort_values(['_time','_order'],kind='stable',na_position='first')
        reviewers=group['_reviewer'].drop_duplicates().tolist()
        scores=[]; row={
          'اسم المدرب':trainer,
          'التاريخ':'، '.join(dict.fromkeys(pd.to_datetime(group[end_col],errors='coerce').dropna().dt.strftime('%Y-%m-%d').tolist())),
          'المحافظة':province,
          'الملاحظات الكلية':''
        }
        note_lines=[]
        for i,reviewer in enumerate(reviewers,1):
            r=group[group['_reviewer']==reviewer].iloc[-1]
            score=pd.to_numeric(r.get(score_col),errors='coerce')
            row[f'نتيجة المقيّم {i}']=None if pd.isna(score) else round(float(score),2)
            row[f'اسم المقيّم {i}']=reviewer
            if not pd.isna(score): scores.append(float(score))
            parts=[]
            for c in note_cols:
                val=clean(r.get(c,''))
                if val and val not in parts: parts.append(val)
            if parts: note_lines.append(f'مقيّم {i} ({reviewer}): '+' | '.join(parts))
        row['الملاحظات الكلية']='\n'.join(note_lines)
        avg=sum(scores)/len(scores) if scores else None
        row['النتيجة النهائية (المتوسط الحسابي)']=round(avg,2) if avg is not None else None
        if avg is None: rec='لا توجد نتيجة رقمية صالحة — يلزم مراجعة البيانات.'
        elif avg<50: rec='غير مقبول — لا يُعتمد للتدريب ضمن المشروع.'
        elif avg<60: rec='غير مقبول حاليًا — يُدرج في بنك الاحتياط لإعادة الدراسة مستقبلًا.'
        elif avg<=80: rec='يُوصى بدراسة تفصيلية للمدرب تمهيدًا للاعتماد المشروط.'
        else: rec='يُوصى بدراسة تفصيلية للمدرب تمهيدًا للاعتماد الفعلي ضمن المشروع.'
        row['النتيجة والتوصية']=rec
        result.append(row)
    return pd.DataFrame(result)

def excel_bytes(df):
    from io import BytesIO
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    buf=BytesIO()
    with pd.ExcelWriter(buf,engine='openpyxl') as writer:
        df.to_excel(writer,index=False,sheet_name='نتائج المقابلات')
        ws=writer.sheets['نتائج المقابلات']; ws.freeze_panes='A2'; ws.sheet_view.rightToLeft=True
        for cell in ws[1]:
            cell.font=Font(name='Arial',bold=True,color='FFFFFF')
            cell.fill=PatternFill('solid',fgColor='164E63')
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
        ws.row_dimensions[1].height=34
        for col in ws.columns:
            letter=get_column_letter(col[0].column)
            maxlen=max((len(str(c.value or '')) for c in col[:100]),default=12)
            ws.column_dimensions[letter].width=min(max(maxlen+3,14),48)
            for c in col[1:]: c.alignment=Alignment(vertical='top',wrap_text=True)
        if ws.max_row>1 and ws.max_column:
            ref=ws.dimensions
            tab=Table(displayName='InterviewResults',ref=ref)
            tab.tableStyleInfo=TableStyleInfo(name='TableStyleMedium2',showRowStripes=True,showColumnStripes=False)
            ws.add_table(tab)
    buf.seek(0); return buf
