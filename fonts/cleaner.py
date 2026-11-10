import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

import pandas as pd

MISSING_VALUES = {"", "nan", "none", "nat", "null", "<na>", "n/a"}


def is_missing(value):
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold() in MISSING_VALUES


def normalize_arabic(text):
    """تطبيع للمقارنة فقط؛ لا يغيّر الاسم الأصلي إلا عند اختيار اسم مرجعي."""
    if is_missing(text):
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().casefold()
    t = re.sub(r"[\u064B-\u065F\u0670\u06D6-\u06ED]", "", t)
    t = t.replace("ـ", "")
    t = t.translate(str.maketrans({
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
        "ى": "ي", "ؤ": "و", "ئ": "ي",
    }))
    # ة/ه في آخر الكلمة: اختلاف شائع في الكتابة العربية العامية
    t = re.sub(r"ة(?=\s|$)", "ه", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"[_\d]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(text):
    return normalize_arabic(text).split()


def token_forms(word):
    """أشكال مقارنة لمعالجة تكرار الحروف و(الـ) التعريف."""
    forms = {word}
    # تجاهل أل التعريف للمقارنة فقط، لا لتغيير الاسم المعروض
    if word.startswith("ال") and len(word) > 4:
        forms.add(word[2:])
    # اختزال الحروف المتجاورة المكررة: التللاوي -> التلاوي
    forms |= {re.sub(r"(.)\1+", r"\1", form) for form in list(forms)}
    # تطبيق اختزال الحروف بعد حذف أل أيضاً
    for form in list(forms):
        if form.startswith("ال") and len(form) > 4:
            stripped = form[2:]
            forms.add(stripped)
            forms.add(re.sub(r"(.)\1+", r"\1", stripped))
    return forms


def similarity(a, b):
    if a == b:
        return 1.0
    return max(SequenceMatcher(None, x, y).ratio()
               for x in token_forms(a) for y in token_forms(b))


def best_ordered_alignment(short, long, threshold=0.78):
    """هل يمكن محاذاة كل كلمات الاسم الأقصر بالترتيب داخل الأطول؟"""
    if len(short) >= len(long):
        return False
    def walk(i, j):
        if i == len(short):
            return True
        for k in range(j, len(long)):
            s = similarity(short[i], long[k])
            if short[i] == long[k] or (min(len(short[i]), len(long[k])) >= 4 and s >= threshold):
                if walk(i + 1, k + 1):
                    return True
        return False
    return walk(0, 0)


def names_match(a, b, threshold=0.82):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    # اختلاف المسافات فقط، مثل «عبدالله» و«عبد الله».
    if "".join(ta) == "".join(tb):
        return True

    if len(ta) == len(tb):
        scores = [similarity(x, y) for x, y in zip(ta, tb)]
        exact_count = sum(x == y for x, y in zip(ta, tb))
        # اختلاف إملائي في كلمة مع تطابق بقية الاسم، مثل زادا/زاده
        if min(scores) >= 0.72 and sum(scores) / len(scores) >= threshold:
            # للاسم الثلاثي نطلب كلمتين متطابقتين تماماً أو تشابهاً قوياً جداً
            if len(ta) >= 3 and exact_count < 2 and sum(scores) / len(scores) < 0.92:
                return False
            # للاسم الثنائي: يلزم تطابق كلمة واحدة على الأقل لتقليل التصادم
            if len(ta) == 2 and exact_count < 1 and sum(scores) / len(scores) < 0.93:
                return False
            return True
        return False

    short, long = (ta, tb) if len(ta) < len(tb) else (tb, ta)
    # الاسم القصير لا يدمج تلقائياً مع الطويل إلا إذا تطابقت جميع كلماته بالترتيب
    return best_ordered_alignment(short, long, threshold=0.84)


def build_dynamic_clusters(series, fuzzy_threshold=0.82, short_to_long_threshold=0.84):
    values = series.map(lambda x: "" if is_missing(x) else str(x).strip())
    counts = Counter(v for v in values if v)
    names = list(counts)
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def canonical_key(name):
        return (-len(tokens(name)), -counts[name], len(name), normalize_arabic(name), name)

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        winner, loser = sorted((ra, rb), key=canonical_key)
        parent[loser] = winner

    # Compare all distinct values, not just previously accepted representatives.
    # This prevents ordering in the spreadsheet from controlling the result.
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if names_match(a, b, threshold=fuzzy_threshold):
                union(a, b)

    groups = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)

    mapping = {}
    for members in groups.values():
        canonical = sorted(members, key=canonical_key)[0]
        for member in members:
            mapping[member] = canonical

    return values.map(lambda v: mapping.get(v, v) if v else "").astype(str)


def build_name_mapping(series, fuzzy_threshold=0.82, short_to_long_threshold=0.84):
    original = series.map(lambda x: "" if is_missing(x) else str(x).strip())
    unified = build_dynamic_clusters(series, fuzzy_threshold, short_to_long_threshold)
    counts = original[original != ""].value_counts()
    rows = []
    for name, count in counts.items():
        mapped_values = unified[original == name]
        mapped = mapped_values.iloc[0] if not mapped_values.empty else name
        if name != mapped:
            rows.append({
                "الاسم_الأصلي": name,
                "الاسم_الموحد_المقترح": mapped,
                "عدد_التكرار": int(count),
                "الحالة": "تم التوحيد تلقائياً",
            })
    return pd.DataFrame(rows, columns=[
        "الاسم_الأصلي", "الاسم_الموحد_المقترح", "عدد_التكرار", "الحالة"
    ])


def clean_text_contextually(value):
    if is_missing(value):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return "" if len(text) < 2 else text


def clean_kobo_data(df):
    cleaned = df.copy()
    for column in ("اسم المقيّم", "الاسم الثلاثي للمدرب"):
        if column in cleaned.columns:
            cleaned[column] = build_dynamic_clusters(cleaned[column])
    # تنظيف الفراغات في الأعمدة النصية فقط، دون تغيير الأعمدة الرقمية والتواريخ.
    for column in cleaned.columns:
        if pd.api.types.is_object_dtype(cleaned[column]) or pd.api.types.is_string_dtype(cleaned[column]):
            cleaned[column] = cleaned[column].apply(clean_text_contextually)
    return cleaned
