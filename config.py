"""Application configuration."""
import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod-2026")
    FLASK_ENV = os.environ.get("FLASK_ENV", "development")
    DEBUG = FLASK_ENV == "development"
    APP_NAME = "TasPlan Review"
    APP_VERSION = "0.1.0"
