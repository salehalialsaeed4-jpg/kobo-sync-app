# استخدام صورة بايثون خفيفة ورسمية
FROM python:3.11-slim

# تثبيت الحزم الأساسية التي قد تحتاجها بعض المكتبات مثل psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt-get/lists/*

# إعداد مجلد العمل داخل الحاوية
WORKDIR /app

# نسخ ملف المتطلبات أولاً للاستفادة من الكاش
COPY requirements.txt .

# تثبيت التبعيات
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع إلى الحاوية
COPY . .

# إنشاء مجلد الخطوط إن لم يكن موجوداً
RUN mkdir -p fonts

# المنفذ الذي تستمع عليه الحاوية (Hugging Face Spaces تعتمد المنفذ 7860 افتراضياً)
ENV PORT=7860
EXPOSE 7860

# تشغيل التطبيق باستخدام خادم Gunicorn جاهز للإنتاج
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "2", "--timeout", "120", "app:app"]