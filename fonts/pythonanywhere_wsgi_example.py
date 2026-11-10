import os, sys
project = '/home/USERNAME/training_request_app'
if project not in sys.path:
    sys.path.insert(0, project)
os.environ['FLASK_SECRET_KEY'] = 'replace-with-a-long-random-secret'
os.environ['ADMIN_PASSWORD'] = 'replace-admin-password'
os.environ['ENTRY_PASSWORD'] = 'replace-entry-password'
os.environ['KOBO_API_TOKEN'] = 'paste-kobo-token-here'
os.environ['KOBO_ASSET_UID'] = 'paste-kobo-asset-uid-here'
os.environ['KOBO_SERVER'] = 'https://eu.kobotoolbox.org'
os.environ['DATABASE_PATH'] = '/home/USERNAME/training_request_app/data.sqlite3'
from app import app as application
