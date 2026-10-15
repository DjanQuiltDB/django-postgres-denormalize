"""
Settings for the test project. This is not a template for a real deployment.
"""

import os

import dj_database_url
from django.db.backends.postgresql.psycopg_any import IsolationLevel

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

SECRET_KEY = 'test-project-only-do-not-use-this-anywhere-real'  # nosec B105

DEBUG = True

ALLOWED_HOSTS = []

INSTALLED_APPS = (
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'pgtrigger',
    'postgres_denormalize',
    'tenancy',
    'catalog',
    # This app has no models of its own. Its tests declare their models in isolated registries, and Django needs the
    # app here to find the app label of those models.
    'isolated',
)

DATABASES = {
    'default': {
        **dj_database_url.parse(
            os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/test_db')
        ),
        # The isolation level that system check W303 asks for, so the suite also runs the triggers at that level.
        'OPTIONS': {'isolation_level': IsolationLevel.SERIALIZABLE},
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

USE_TZ = True

POSTGRES_DENORMALIZE = {
    'EXCLUDE_MODELS': ['catalog.ItemAudit'],
    'REDUNDANT_COPY_CHECK': {
        'LEVEL': 'error',
        'ALLOWED': {
            'catalog.Item.home_section': 'A second section that is chosen independently, and that only rarely matches '
            'the section of the category.',
        },
    },
    'ORDERING_CHECK_LEVEL': 'error',
    'ISOLATION_CHECK_LEVEL': 'error',
}
