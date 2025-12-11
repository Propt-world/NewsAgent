"""
Request Logging Middleware
Logs every HTTP request and response for debugging and monitoring.
"""
import time
import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log all incoming HTTP requests and outgoing responses.
    Captures: method, path, status code, response time, client IP, user agent.
    """
    
    async def dispatch(self, request: Request, call_next):
        # Start timer
        start_time = time.time()
        
        # Extract request details
        method = request.method
        path = request.url.path
        query_params = str(request.query_params) if request.query_params else ""
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        
        # Log incoming request
        logger.info(f"→ {method} {path}{f'?{query_params}' if query_params else ''} | IP: {client_ip}")
        
        # Process request
        try:
            response: Response = await call_next(request)
            
            # Calculate response time
            process_time = (time.time() - start_time) * 1000  # Convert to ms
            
            # Log response
            status_code = response.status_code
            log_level = logging.INFO
            
            # Use different log levels based on status code
            if status_code >= 500:
                log_level = logging.ERROR
            elif status_code >= 400:
                log_level = logging.WARNING
            
            logger.log(
                log_level,
                f"← {method} {path} | Status: {status_code} | Time: {process_time:.2f}ms | IP: {client_ip}"
            )
            
            # Add response time header
            response.headers["X-Process-Time"] = f"{process_time:.2f}ms"
            
            return response
            
        except Exception as e:
            # Log errors
            process_time = (time.time() - start_time) * 1000
            logger.error(
                f"✗ {method} {path} | ERROR: {str(e)} | Time: {process_time:.2f}ms | IP: {client_ip}",
                exc_info=True
            )
            raise
