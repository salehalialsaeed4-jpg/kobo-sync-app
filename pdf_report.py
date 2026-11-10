"""تقرير PDF لنتائج وتوصيات مقابلات مدرّبي محافظة واحدة.

المحتوى: ترويسة بالشعار + عنوان، مقدمة معتمدة، ثم جدول (م - اسم المدرب - المحافظة - التوصية)
مرتباً من المقبولين إلى المرفوضين. الشكل الهندسي للشعار يظهر كعلامة مائية في كل الصفحات.

المتطلبات: reportlab + arabic-reshaper + python-bidi، والخط IBM Plex Sans Arabic في fonts/ (ترخيص OFL).
"""
import os
import re
from html import escape
from io import BytesIO

import arabic_reshaper
import pandas as pd
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_RIGHT, TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.pdfgen.canvas import FILL_EVEN_ODD
from reportlab.platypus import (BaseDocTemplate, Frame, NextPageTemplate, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
FONT = 'PlexArabic'
FONT_BOLD = 'PlexArabic-Bold'

# ألوان هوية النظام (من ملف تسجيل الدخول)
CYAN = colors.HexColor('#28A3CC')
TEAL = colors.HexColor('#1D7799')
TEAL_DEEP = colors.HexColor('#125A78')
GOLD = colors.HexColor('#C9A227')
GOLD_LIGHT = colors.HexColor('#E4C874')
INK = colors.HexColor('#0B3A52')
MUTED = colors.HexColor('#5C808F')
LINE = colors.HexColor('#C9D6DD')

# مسارات الشكل الهندسي (إحداثيات 0..100، المحور y للأسفل) — من تصميم الشعار المعتمد
SHAPE_LINES = ("M 0,50 L 16.75,66.37 L 20.49,62.5 L 8.38,49.87 L 50.13,8.51 L 91.49,49.87 L 79.38,62.5 "
               "L 83.12,66.37 L 99.87,50.26 L 49.87,0 Z M 19.2,68.94 L 28.87,77.96 L 30.67,75.9 "
               "L 24.48,68.94 L 50.26,43.69 L 75.39,68.94 L 69.2,75.9 L 71.13,78.09 L 80.67,69.46 "
               "L 50,38.4 Z M 30.93,80.93 L 35.7,86.08 L 36.98,86.47 L 37.89,84.92 L 34.15,80.93 "
               "L 50,65.21 L 65.72,80.93 L 61.86,85.05 L 62.89,86.47 L 64.05,86.21 L 68.94,81.19 "
               "L 50,62.11 Z")
SHAPE_DIAMOND = "M 49.87,78.61 L 39.3,89.43 L 50.13,100 L 60.57,89.18 Z"

# ترتيب الفئات: من المقبولين إلى المرفوضين
CATEGORY_ORDER = ['good', 'cond', 'reserve', 'bad', 'review']
CATEGORIES = {
    'good':    {'bg': colors.HexColor('#DCEFD5'), 'fg': colors.HexColor('#2F6B1F')},
    'cond':    {'bg': colors.HexColor('#DDEBF7'), 'fg': colors.HexColor('#1F4E79')},
    'reserve': {'bg': colors.HexColor('#FFF0C2'), 'fg': colors.HexColor('#8A6500')},
    'bad':     {'bg': colors.HexColor('#F8D7CC'), 'fg': colors.HexColor('#9C2B12')},
    'review':  {'bg': colors.HexColor('#E5E7EB'), 'fg': colors.HexColor('#374151')},
}


def _register_fonts():
    if FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT, os.path.join(FONT_DIR, 'IBMPlexSansArabic-Regular.ttf')))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, os.path.join(FONT_DIR, 'IBMPlexSansArabic-Bold.ttf')))
        pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD)


def ar_line(text):
    """سطر واحد كامل للرسم المباشر على الـ canvas."""
    return get_display(arabic_reshaper.reshape(str(text)), base_dir='R')


def ar_par(text):
    """نص عادي لـ Paragraph مع wordWrap='RTL': تشكيل وعكس لكل كلمة على حدة."""
    return ar_markup([(text, False)])


def ar_markup(segments):
    """segments: [(نص, عريض؟)] بحيث تنتهي حدود المقاطع عند مسافات فقط. ينتج نصاً صالحاً لـ Paragraph."""
    out = []
    for text, bold in segments:
        for token in re.split(r'(\s+)', str(text)):
            if not token:
                continue
            if token.isspace():
                out.append(token)
                continue
            shaped = escape(get_display(arabic_reshaper.reshape(token), base_dir='R'))
            out.append(f'<b>{shaped}</b>' if bold else shaped)
    return ''.join(out)



def rtl_flow(text, style, width):
    """نص عربي متعدد الأسطر بترتيب كلمات صحيح.
    نقسم النص المنطقي إلى أسطر حسب العرض المتاح، ثم يُحوَّل كل سطر كاملاً (تشكيل + bidi)
    فيظهر بالترتيب الصحيح من اليمين لليسار، وتقع علامات الترقيم في مكانها."""
    width = max(width - 2, 10)
    lines, cur = [], []
    for word in str(text).split():
        trial = ' '.join(cur + [word])
        if cur and pdfmetrics.stringWidth(ar_line(trial), style.fontName, style.fontSize) > width:
            lines.append(' '.join(cur))
            cur = [word]
        else:
            cur.append(word)
    if cur:
        lines.append(' '.join(cur))
    if not lines:
        lines = ['']
    ltr = ParagraphStyle(style.name + '_v', parent=style, wordWrap=None, alignment=style.alignment
                         if style.alignment != TA_JUSTIFY else TA_RIGHT)
    return [Paragraph(escape(ar_line(line)), ltr) for line in lines]


def category_of(avg):
    if avg is None or pd.isna(avg):
        return 'review'
    avg = float(avg)
    if avg < 50:
        return 'bad'
    if avg < 60:
        return 'reserve'
    if avg <= 80:
        return 'cond'
    return 'good'


def extract_scope(merged_df):
    """يستخرج المحافظات والتواريخ الفريدة من جدول الدمج (قد تحتوي الخلية أكثر من قيمة مفصولة بفاصلة)."""
    def unique_values(column):
        if column not in merged_df.columns:
            return []
        values = []
        for cell in merged_df[column].dropna().astype(str):
            for part in re.split(r'[،,]', cell):
                part = part.strip()
                if part and part.lower() != 'nan' and part not in values:
                    values.append(part)
        return values
    return unique_values('المحافظة'), sorted(unique_values('التاريخ'))


# ---------------------------------------------------------------- الرسم الهندسي
def _points(d):
    """تحويل مسار M/L/Z إلى قوائم نقاط (بدون استخدام حالات مخفية)."""
    result, current = [], []
    tokens = re.findall(r'[MLZ]|-?\d+\.?\d*', d)
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ('M', 'L'):
            current.append((float(tokens[i + 1]), float(tokens[i + 2])))
            i += 3
        elif t == 'Z':
            result.append(current)
            current = []
            i += 1
        else:
            i += 1
    return result


def _draw_poly_path(c, d, cx, cy, k):
    """يرسم المسار مع قلب المحور y؛ (cx, cy) مركز الرسم بالنقاط، k = حجم الوحدة."""
    path = c.beginPath()
    for sub in _points(d):
        for idx, (x, y) in enumerate(sub):
            px, py = cx + (x - 50) * k, cy - (y - 50) * k
            path.moveTo(px, py) if idx == 0 else path.lineTo(px, py)
        path.close()
    c.drawPath(path, stroke=0, fill=1, fillMode=FILL_EVEN_ODD)


def draw_shape(c, cx, cy, size, line_color, diamond_color, alpha=None):
    c.saveState()
    k = size / 100.0
    c.setFillColor(line_color, alpha=alpha)
    _draw_poly_path(c, SHAPE_LINES, cx, cy, k)
    c.setFillColor(diamond_color, alpha=alpha)
    _draw_poly_path(c, SHAPE_DIAMOND, cx, cy, k)
    c.restoreState()


def draw_logo(c, cx, cy, diameter):
    """الشعار: حلقة + الشكل الهندسي + «إعداد وتطوير القادة» (بدون سطر المكتب)."""
    r = diameter / 2
    c.saveState()
    c.setFillColor(colors.white)
    c.circle(cx, cy, r, stroke=0, fill=1)
    ring_w = diameter * 0.045
    c.setStrokeColor(TEAL)
    c.setLineWidth(ring_w)
    c.circle(cx, cy, r - ring_w * 1.1, stroke=1, fill=0)
    shape_size = diameter * 0.38
    draw_shape(c, cx, cy + diameter * 0.10, shape_size, TEAL, GOLD)
    text = ar_line('إعداد وتطوير القادة')
    fs = diameter * 0.54 / pdfmetrics.stringWidth(text, FONT, 1)
    c.setFont(FONT, fs)
    c.setFillColor(TEAL)
    c.drawCentredString(cx, cy - diameter * 0.255, text)
    c.restoreState()


def draw_watermark(c):
    w, h = A4
    draw_shape(c, w / 2, h / 2 - 10 * mm, 118 * mm, TEAL, GOLD, alpha=0.07)


# ---------------------------------------------------------------- الصفحات
class _NumberedCanvas(canvas.Canvas):
    """ترقيم «صفحة x من y» في التذييل."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages = []

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            w, _ = A4
            self.setStrokeColor(LINE)
            self.setLineWidth(0.6)
            self.line(18 * mm, 15 * mm, w - 18 * mm, 15 * mm)
            self.setFont(FONT, 9)
            self.setFillColor(MUTED)
            self.drawCentredString(w / 2, 9.5 * mm, ar_line(f'صفحة {self._pageNumber} من {total}'))
            super().showPage()
        super().save()


MARGIN = 18 * mm
BAND_H = 44 * mm


def _make_page_callbacks(title):
    def first_page(c, doc):
        w, h = A4
        draw_watermark(c)
        c.saveState()
        clip = c.beginPath()
        clip.rect(0, h - BAND_H, w, BAND_H)
        c.clipPath(clip, stroke=0, fill=0)
        c.linearGradient(0, 0, w, 0, (TEAL_DEEP, TEAL), extend=False)
        c.restoreState()
        c.saveState()
        c.setFillColor(GOLD)
        c.rect(0, h - BAND_H - 1.8 * mm, w, 1.8 * mm, stroke=0, fill=1)
        c.restoreState()

        logo_d = 31 * mm
        logo_cx = w - MARGIN - logo_d / 2
        logo_cy = h - BAND_H / 2
        draw_logo(c, logo_cx, logo_cy, logo_d)

        # العنوان على يسار الشعار، محاذى لليمين، ويصغّر تلقائياً إن طال
        right = logo_cx - logo_d / 2 - 7 * mm
        avail = right - MARGIN
        line = ar_line(title)
        fs = 21
        while pdfmetrics.stringWidth(line, FONT_BOLD, fs) > avail and fs > 12:
            fs -= 0.5
        c.saveState()
        c.setFillColor(colors.white)
        c.setFont(FONT_BOLD, fs)
        c.drawRightString(right, logo_cy - 2 * mm, line)
        c.setFillColor(GOLD_LIGHT)
        c.rect(right - 38 * mm, logo_cy - 7.5 * mm, 38 * mm, 0.9 * mm, stroke=0, fill=1)
        c.restoreState()

    def later_pages(c, doc):
        w, h = A4
        draw_watermark(c)
        c.saveState()
        c.setFillColor(TEAL)
        c.rect(0, h - 11 * mm, w, 11 * mm, stroke=0, fill=1)
        c.setFillColor(GOLD)
        c.rect(0, h - 12.4 * mm, w, 1.4 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont(FONT_BOLD, 10)
        c.drawRightString(w - MARGIN, h - 7.6 * mm, ar_line(title))
        c.restoreState()

    return first_page, later_pages


# ---------------------------------------------------------------- بناء التقرير
def build_pdf_report(merged_df):
    """merged_df: جدول الدمج. يُشترط أن يخص محافظة واحدة، وإلا يُرفع ValueError."""
    _register_fonts()
    provinces, dates = extract_scope(merged_df)
    if len(provinces) == 0:
        raise ValueError('لا توجد محافظة في جدول الدمج.')
    if len(provinces) > 1:
        raise ValueError('هذا التقرير مخصص لمحافظة واحدة فقط، لكن الجدول يضم: '
                         + '، '.join(provinces) + '. اختر محافظة واحدة من الفلاتر ثم أعد الدمج.')
    province = provinces[0]
    date_text = '، '.join(dates) if dates else '—'
    date_word = 'بتواريخ' if len(dates) > 1 else 'بتاريخ'
    title = f'نتائج وتوصيات مقابلات مدرّبي محافظة {province}'

    w, h = A4
    content_w = w - 2 * MARGIN

    st_body = ParagraphStyle('b', fontName=FONT, fontSize=12, leading=23, alignment=TA_JUSTIFY,
                             textColor=INK, wordWrap='RTL')
    st_greet = ParagraphStyle('g', parent=st_body, fontName=FONT_BOLD, alignment=TA_RIGHT)
    st_cell = ParagraphStyle('c', fontName=FONT, fontSize=10.5, leading=16, alignment=TA_RIGHT,
                             textColor=INK, wordWrap='RTL')
    st_cell_b = ParagraphStyle('cb', parent=st_cell, fontName=FONT_BOLD)
    st_center = ParagraphStyle('ce', parent=st_cell, alignment=TA_CENTER)
    st_head = ParagraphStyle('th', fontName=FONT_BOLD, fontSize=11, leading=16, alignment=TA_CENTER,
                             textColor=colors.white, wordWrap='RTL')

    # الترتيب: من المقبولين إلى المرفوضين، وداخل كل فئة من المتوسط الأعلى إلى الأدنى
    df = merged_df.copy()
    avg_col = 'النتيجة النهائية (المتوسط الحسابي)'
    df['_avg'] = pd.to_numeric(df[avg_col], errors='coerce')
    df['_cat'] = df['_avg'].map(category_of)
    df['_rank'] = df['_cat'].map({k: i for i, k in enumerate(CATEGORY_ORDER)})
    df = df.sort_values(['_rank', '_avg'], ascending=[True, False], kind='stable', na_position='last')

    story = [NextPageTemplate('later'), Spacer(1, 3 * mm)]

    # ---------- المقدمة (النص المعتمد) ----------
    story.extend(rtl_flow('السلام عليكم ورحمة الله وبركاته،', st_greet, content_w))
    story.append(Spacer(1, 2 * mm))
    story.extend(rtl_flow(
        f'نضع بين أيديكم الجدول الآتي، ويتضمن نتائج المقابلات التي أُجريت مع مدرّبي محافظة {province} '
        f'{date_word} {date_text}، وما انتهت إليه من توصيات.', st_body, content_w))
    story.append(Spacer(1, 2 * mm))
    story.extend(rtl_flow('وذلك للاطلاع واتخاذ ما يلزم وفق الأصول.', st_body, content_w))
    story.append(Spacer(1, 6 * mm))

    # ---------- الجدول (الأعمدة معكوسة ليبدأ من اليمين) ----------
    col_w = [70 * mm, 30 * mm, 62 * mm, 12 * mm]  # [التوصية, المحافظة, الاسم, م] من اليسار لليمين
    col_w = [x * content_w / sum(col_w) for x in col_w]
    PAD = 16  # الحشو الأفقي داخل الخلية
    rows = [[rtl_flow(t, st_head, cw - PAD) for t, cw in zip(('التوصية', 'المحافظة', 'اسم المدرب', 'م'), col_w)]]
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), TEAL_DEEP),
        ('LINEBELOW', (0, 0), (-1, 0), 2, GOLD),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, LINE),
        ('BOX', (0, 0), (-1, -1), 0.6, LINE),
    ]
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        cat = CATEGORIES[r['_cat']]
        rec_style = ParagraphStyle('rec', parent=st_cell, textColor=cat['fg'])
        rows.append([
            rtl_flow(str(r.get('النتيجة والتوصية', '') or ''), rec_style, col_w[0] - PAD),
            rtl_flow(str(r.get('المحافظة', '') or '—'), st_center, col_w[1] - PAD),
            rtl_flow(str(r.get('اسم المدرب', '') or ''), st_cell_b, col_w[2] - PAD),
            rtl_flow(str(i), st_center, col_w[3] - PAD),
        ])
        # خلفيات شبه شفافة كي تبقى العلامة المائية ظاهرة خلف الجدول
        style.append(('BACKGROUND', (0, i), (0, i),
                      colors.Color(cat['bg'].red, cat['bg'].green, cat['bg'].blue, alpha=0.88)))
        if i % 2 == 0:
            style.append(('BACKGROUND', (1, i), (-1, i), colors.Color(0.90, 0.95, 0.97, alpha=0.55)))
    table = Table(rows, colWidths=col_w, repeatRows=1)
    table.setStyle(TableStyle(style))
    story.append(table)

    first_cb, later_cb = _make_page_callbacks(title)
    buffer = BytesIO()
    doc = BaseDocTemplate(buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=20 * mm, bottomMargin=20 * mm, title=title,
                          author='إعداد وتطوير القادة')
    first = PageTemplate(id='first', onPage=first_cb,
                         frames=[Frame(MARGIN, 20 * mm, content_w, h - BAND_H - 9 * mm - 20 * mm, id='f1',
                                       leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])
    later = PageTemplate(id='later', onPage=later_cb,
                         frames=[Frame(MARGIN, 20 * mm, content_w, h - 20 * mm - 20 * mm, id='f2',
                                       leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])
    doc.addPageTemplates([first, later])
    doc.build(story, canvasmaker=_NumberedCanvas)
    buffer.seek(0)
    return buffer
