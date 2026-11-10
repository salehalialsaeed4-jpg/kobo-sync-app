import re
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

FINAL_COL = 'النتيجة النهائية (المتوسط الموزون)'

# ---------------------------------------------------------------- الأوزان
MAX_REVIEWERS = 5
# وزن المقيّم المعتمد حسب عدد المقيّمين؛ يُقسَّم الباقي بالتساوي على الثانويين.
PRIMARY_WEIGHT = {1: 1.0, 2: 0.70, 3: 0.50, 4: 0.40, 5: 0.35}


def weights_for(n, primary_index=0):
    """يعيد قائمة أوزان بطول n، والمعتمد في الموضع primary_index. المجموع = 1."""
    if n < 1:
        return []
    if n > MAX_REVIEWERS:
        raise ValueError(f'الحد الأقصى {MAX_REVIEWERS} مقيّمين لكل مرشح.')
    if n == 1:
        return [1.0]
    p = PRIMARY_WEIGHT[n]
    other = (1.0 - p) / (n - 1)
    return [p if i == primary_index else other for i in range(n)]


def fmt_pct(w):
    v = round(w * 100, 2)
    return f'{v:g}%'


def candidate_key(trainer, province):
    return (normalize_arabic(trainer), normalize_arabic(province))


# ---------------------------------------------------------------- تحضير البيانات
def first_col(df, names):
    return next((n for n in names if n in df.columns), None)


def clean(v):
    if pd.isna(v): return ''
    return re.sub(r'\s+',' ',str(v)).strip()


def _cols(df):
    cols = {
        'trainer': first_col(df, COL['trainer']), 'reviewer': first_col(df, COL['reviewer']),
        'province': first_col(df, COL['province']), 'end': first_col(df, COL['end']),
        'submitted': first_col(df, COL['submitted']), 'score': first_col(df, COL['score']),
    }
    required = [('اسم المرشح','trainer'),('اسم المقيّم','reviewer'),('المحافظة','province'),('percentage','score'),('end','end')]
    missing = [label for label, k in required if not cols[k]]
    if missing:
        raise ValueError('أعمدة الاستمارة غير مكتملة: ' + ', '.join(missing))
    return cols


def prepare(df):
    """يطبّق الأسماء المعتمدة ويُبقي أحدث تقييم لكل (مرشح + محافظة + مقيّم). يعيد (work, cols)."""
    cols = _cols(df)
    trainer_alias = get_aliases('trainer'); reviewer_alias = get_aliases('reviewer')
    work = df.copy()
    work['_trainer_raw'] = work[cols['trainer']].map(clean)
    work['_reviewer_raw'] = work[cols['reviewer']].map(clean)
    work['_province'] = work[cols['province']].map(clean)
    work['_trainer'] = work['_trainer_raw'].map(lambda x: trainer_alias.get(normalize_arabic(x), x))
    work['_reviewer'] = work['_reviewer_raw'].map(lambda x: reviewer_alias.get(normalize_arabic(x), x))
    work['_time'] = pd.to_datetime(work[cols['submitted']], errors='coerce') if cols['submitted'] else pd.NaT
    work['_time'] = work['_time'].fillna(pd.to_datetime(work[cols['end']], errors='coerce'))
    work['_order'] = range(len(work))
    work = work[(work['_trainer'] != '') & (work['_reviewer'] != '')].copy()
    if work.empty:
        raise ValueError('لا توجد استجابات تحتوي على اسم مرشح ومقيّم صالحين.')
    # هوية الدمج تشمل المحافظة لمنع دمج شخصين متشابهَي الاسم في محافظتين مختلفتين.
    key = ['_trainer', '_province', '_reviewer']
    dup = work.duplicated(key, keep=False)
    if dup.any() and work.loc[dup, '_time'].isna().any():
        raise ValueError('هناك تقييمات مكررة بلا توقيت موثوق؛ لا يمكن اختيار أحدث استجابة بأمان.')
    work = work.sort_values(['_time', '_order'], kind='stable', na_position='first').drop_duplicates(key, keep='last')
    return work, cols


def candidate_groups(df):
    """مجموعات المرشحين للمعاينة: [{trainer, province, reviewers:[{name, score}], key}] مرتبة بالمحافظة ثم الاسم."""
    work, cols = prepare(df)
    out = []
    for (trainer, province), group in work.groupby(['_trainer', '_province'], sort=False, dropna=False):
        group = group.sort_values(['_time', '_order'], kind='stable', na_position='first')
        reviewers = []
        for _, r in group.iterrows():
            score = pd.to_numeric(r.get(cols['score']), errors='coerce')
            dt = r['_time']
            reviewers.append({'name': r['_reviewer'], 'score': None if pd.isna(score) else round(float(score), 2),
                              'date': '' if pd.isna(dt) else dt.strftime('%Y-%m-%d')})
        out.append({'trainer': trainer, 'province': province, 'reviewers': reviewers,
                    'key': candidate_key(trainer, province)})
    out.sort(key=lambda g: (g['province'], g['trainer']))
    return out


def recommendation(avg):
    if avg is None: return 'لا توجد نتيجة رقمية صالحة — يلزم مراجعة البيانات.'
    if avg < 50: return 'غير مقبول — لا يُعتمد للتدريب ضمن المشروع.'
    if avg < 60: return 'غير مقبول حاليًا — يُدرج في بنك الاحتياط لإعادة الدراسة مستقبلًا.'
    if avg <= 80: return 'يُوصى بدراسة تفصيلية للمدرب تمهيدًا للاعتماد المشروط.'
    return 'يُوصى بدراسة تفصيلية للمدرب تمهيدًا للاعتماد الفعلي ضمن المشروع.'


def validate_primaries(groups, primaries):
    """يعيد قائمة مشاكل (نصوص) تمنع الدمج: أكثر من 5 مقيّمين، أو لا معتمد لمرشح متعدد المقيّمين."""
    problems = []
    for g in groups:
        n = len(g['reviewers'])
        if n > MAX_REVIEWERS:
            problems.append(f'«{g["trainer"]}» ({g["province"]}) لديه {n} مقيّمين، والحد الأقصى {MAX_REVIEWERS}؛ راجع توحيد أسماء المقيّمين.')
        elif n > 1:
            chosen = primaries.get(g['key'])
            names = {normalize_arabic(r['name']) for r in g['reviewers']}
            if not chosen or normalize_arabic(chosen) not in names:
                problems.append(f'لم يُحدَّد المقيّم المعتمد للمرشح «{g["trainer"]}» ({g["province"]}).')
    return problems


# ---------------------------------------------------------------- الدمج
def build_merged(df, primaries):
    """primaries: {(trainer_norm, province_norm): reviewer_name}. يعيد جدول النتائج بمتوسط موزون."""
    work, cols = prepare(df)
    groups = candidate_groups(df)
    problems = validate_primaries(groups, primaries)
    if problems:
        raise ValueError(' | '.join(problems[:5]) + (f' (+{len(problems)-5})' if len(problems) > 5 else ''))
    notes_candidates = [
      'ملاحظات حول الأداء','ملاحظات الخبرة التدريبية','التفاصيل إن وجدت',
      'خلال مسيرتك التدريبية هل ما زلت تحتفظ باسم مدرب ترك فيك أثراً وهو موجود إلى الآن؟',
      'الرأي العام للمقيّم (اكتب رأيك العام بالمرشح)','q6','q9','impact_trainer','q10'
    ]
    note_cols = list(dict.fromkeys(c for c in notes_candidates if c in work.columns))
    end_col, score_col = cols['end'], cols['score']
    result = []
    for (trainer, province), group in work.groupby(['_trainer', '_province'], sort=False, dropna=False):
        group = group.sort_values(['_time', '_order'], kind='stable', na_position='first')
        key = candidate_key(trainer, province)
        rows = [r for _, r in group.iterrows()]
        n = len(rows)
        # المعتمد أولاً ثم الباقون بترتيب الوقت
        if n > 1:
            chosen = normalize_arabic(primaries[key])
            rows.sort(key=lambda r: 0 if normalize_arabic(r['_reviewer']) == chosen else 1)
        weights = weights_for(n, 0)
        row = {
          'اسم المدرب': trainer,
          'التاريخ': '، '.join(dict.fromkeys(pd.to_datetime(group[end_col], errors='coerce').dropna().dt.strftime('%Y-%m-%d').tolist())),
          'المحافظة': province,
          'الملاحظات الكلية': ''
        }
        note_lines = []; total = 0.0
        for i, (r, w) in enumerate(zip(rows, weights), 1):
            score = pd.to_numeric(r.get(score_col), errors='coerce')
            if pd.isna(score):
                raise ValueError(f'تقييم «{r["_reviewer"]}» للمرشح «{trainer}» بلا نتيجة رقمية صالحة.')
            score = float(score)
            total += score * w
            label = (f'{r["_reviewer"]} (معتمد · {fmt_pct(w)})' if (n > 1 and i == 1) else f'{r["_reviewer"]} ({fmt_pct(w)})')
            row[f'نتيجة المقيّم {i}'] = round(score, 2)
            row[f'اسم المقيّم {i}'] = label
            parts = []
            for c in note_cols:
                val = clean(r.get(c, ''))
                if val and val not in parts: parts.append(val)
            if parts: note_lines.append(f'مقيّم {i} ({r["_reviewer"]}): ' + ' | '.join(parts))
        row['الملاحظات الكلية'] = '\n'.join(note_lines)
        avg = round(total, 2)
        row[FINAL_COL] = avg
        row['النتيجة والتوصية'] = recommendation(avg)
        result.append(row)
    cols = ['اسم المدرب', 'التاريخ', 'المحافظة']
    for i in range(1, MAX_REVIEWERS + 1):
        cols += [f'اسم المقيّم {i}', f'نتيجة المقيّم {i}']
    cols += [FINAL_COL, 'النتيجة والتوصية', 'الملاحظات الكلية']
    df_out = pd.DataFrame(result)
    present = [c for c in cols if c in df_out.columns]
    return df_out[present]


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
            cell.fill=PatternFill('solid',fgColor='125A78')
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
