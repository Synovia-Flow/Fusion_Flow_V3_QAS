"""
Configuration Classes - Fusion Flow V2
Reads from environment variables (set in .env locally, Render dashboard in production).
"""
import os

from config.db_connection import build_connection_string
from config.db_connection import detect_odbc_driver


class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')
    PERMANENT_SESSION_LIFETIME = 86400 * 7  # 7 days

    # Azure SQL / SQL Server
    AZURE_SQL_SERVER = os.environ.get('AZURE_SQL_SERVER', '')
    AZURE_SQL_DATABASE = os.environ.get('AZURE_SQL_DATABASE', 'Fusion_TSS')
    AZURE_SQL_USERNAME = os.environ.get('AZURE_SQL_USERNAME', '')
    AZURE_SQL_PASSWORD = os.environ.get('AZURE_SQL_PASSWORD', '')
    DB_CONN_STR = os.environ.get('DB_CONN_STR', os.environ.get('ODBC_CONNECTION_STRING', ''))

    # TSS API
    TSS_API_BASE_URL = os.environ.get('TSS_API_BASE_URL', 'https://api.tsstestenv.co.uk/api')
    TSS_API_USERNAME = os.environ.get('TSS_API_USERNAME', '')
    TSS_API_PASSWORD = os.environ.get('TSS_API_PASSWORD', '')

    # Client
    CLIENT_CODE = os.environ.get('CLIENT_CODE', 'BKD')
    CLIENT_NAME = 'Birkdale Sales Ltd'

    # Claude / Anthropic API  (for 3-pass PDF recognition)
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    PDF_RECOGNITION_MODEL = os.environ.get('PDF_RECOGNITION_MODEL', 'claude-sonnet-4-6')
    PDF_RECOGNITION_PASSES = int(os.environ.get('PDF_RECOGNITION_PASSES', '3'))
    PDF_CONFIDENCE_THRESHOLD = float(os.environ.get('PDF_CONFIDENCE_THRESHOLD', '0.75'))

    # File storage (local; swap for Azure Blob URI in production)
    _base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    UPLOAD_FOLDER = os.environ.get(
        'UPLOAD_FOLDER', os.path.join(_base_dir, 'uploads'))
    INBOUND_FOLDER = os.environ.get(
        'INBOUND_FOLDER', os.path.join(_base_dir, 'uploads', 'inbound'))
    MAX_UPLOAD_SIZE_MB = int(os.environ.get('MAX_UPLOAD_SIZE_MB', '50'))

    # SMTP Email (Office 365 via SMTP AUTH)
    SMTP_SERVER   = os.environ.get('SMTP_SERVER',   'smtp.office365.com')
    SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
    SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
    SMTP_SENDER   = os.environ.get('SMTP_SENDER',   'nexus@synoviaintegration.com')
    SMTP_ENABLED  = os.environ.get('SMTP_ENABLED',  'true').lower() == 'true'

    # ODBC Driver (auto-detect)
    ODBC_DRIVER = detect_odbc_driver()

    @property
    def ODBC_CONNECTION_STRING(self):
        return build_connection_string(self, timeout=30, include_retry=True)


class DevelopmentConfig(Config):
    DEBUG = True
    TESTING = False


class ProductionConfig(Config):
    DEBUG = False
    TESTING = False


class TestingConfig(Config):
    TESTING = True
    DEBUG = True


config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
}
