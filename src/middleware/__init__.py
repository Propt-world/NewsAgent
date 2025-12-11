"""
Middleware package for NewsAgent services.
"""
from src.middleware.request_logger import RequestLoggingMiddleware

__all__ = ["RequestLoggingMiddleware"]
