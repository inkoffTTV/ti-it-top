"""Совместимость: utils.logging_setup -> utils.logging."""
from utils.logging import install_secret_redaction, setup_logging as configure_logging

__all__ = ["configure_logging", "install_secret_redaction"]
