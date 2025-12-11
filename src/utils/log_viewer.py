"""
Log Viewer Utility for Application Logs
Provides functions to capture and format application logs for web display.
"""
import logging
import sys
import io
from typing import Optional, List, Deque
from datetime import datetime
from collections import deque
import threading

# In-memory log storage
class InMemoryLogHandler(logging.Handler):
    """Custom logging handler that stores logs in memory."""
    
    def __init__(self, maxlen=5000):
        super().__init__()
        self.log_buffer: Deque[str] = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        
    def emit(self, record):
        try:
            msg = self.format(record)
            with self.lock:
                self.log_buffer.append(msg)
        except Exception:
            self.handleError(record)
    
    def get_logs(self, lines: int = 100, grep_filter: Optional[str] = None) -> str:
        """Retrieve logs from the buffer."""
        with self.lock:
            # Get last N lines
            log_lines = list(self.log_buffer)[-lines:]
            
            # Apply grep filter if provided
            if grep_filter:
                log_lines = [
                    line for line in log_lines
                    if grep_filter.lower() in line.lower()
                ]
            
            return '\n'.join(log_lines) if log_lines else "No logs available"
    
    def clear(self):
        """Clear the log buffer."""
        with self.lock:
            self.log_buffer.clear()

# Global log handler instance
_log_handler = None

def setup_log_handler(logger_name: str = None):
    """
    Set up the in-memory log handler for capturing application logs.
    Call this once during application startup.
    
    Args:
        logger_name: Name of the logger to attach to (None for root logger)
    """
    global _log_handler
    
    if _log_handler is None:
        _log_handler = InMemoryLogHandler(maxlen=5000)
        
        # Format logs with timestamp and level
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        _log_handler.setFormatter(formatter)
        
        # Attach to logger
        logger = logging.getLogger(logger_name)
        logger.addHandler(_log_handler)
        logger.setLevel(logging.DEBUG)
    
    return _log_handler

def get_application_logs(
    lines: int = 100,
    grep_filter: Optional[str] = None
) -> str:
    """
    Fetch logs from the in-memory log handler.
    
    Args:
        lines: Number of lines to retrieve (default: 100)
        grep_filter: Optional string to filter logs
    
    Returns:
        String containing the logs
    """
    global _log_handler
    
    if _log_handler is None:
        return "Log handler not initialized. Logs are not being captured."
    
    try:
        return _log_handler.get_logs(lines=lines, grep_filter=grep_filter)
    except Exception as e:
        return f"Error retrieving logs: {str(e)}"


def format_logs_html(
    logs: str,
    service_name: str,
    lines: int,
    grep_filter: Optional[str] = None
) -> str:
    """
    Format logs as HTML for browser display.
    
    Args:
        logs: Raw log content
        service_name: Name of the service (e.g., "NewsAPI", "Scheduler")
        lines: Number of lines displayed
        grep_filter: Filter applied (if any)
    
    Returns:
        HTML formatted string
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    filter_info = f"<p><strong>Filter:</strong> {grep_filter}</p>" if grep_filter else ""
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{service_name} Logs</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            body {{
                font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
                background: #1e1e1e;
                color: #d4d4d4;
                padding: 20px;
                line-height: 1.6;
            }}
            .header {{
                background: #2d2d30;
                padding: 20px;
                border-radius: 8px;
                margin-bottom: 20px;
                border-left: 4px solid #007acc;
            }}
            .header h1 {{
                color: #4ec9b0;
                font-size: 24px;
                margin-bottom: 10px;
            }}
            .header p {{
                color: #858585;
                font-size: 14px;
                margin: 5px 0;
            }}
            .controls {{
                background: #252526;
                padding: 15px;
                border-radius: 8px;
                margin-bottom: 20px;
            }}
            .controls a {{
                display: inline-block;
                background: #007acc;
                color: white;
                padding: 8px 16px;
                text-decoration: none;
                border-radius: 4px;
                margin-right: 10px;
                margin-bottom: 5px;
                font-size: 14px;
                transition: background 0.3s;
            }}
            .controls a:hover {{
                background: #005a9e;
            }}
            .log-container {{
                background: #1e1e1e;
                border: 1px solid #3e3e42;
                border-radius: 8px;
                padding: 20px;
                overflow-x: auto;
            }}
            .log-content {{
                white-space: pre-wrap;
                word-wrap: break-word;
                font-size: 13px;
                color: #cccccc;
            }}
            .log-line {{
                padding: 2px 0;
                border-bottom: 1px solid #2d2d30;
            }}
            .error {{
                color: #f48771;
            }}
            .warning {{
                color: #dcdcaa;
            }}
            .info {{
                color: #4fc1ff;
            }}
            .success {{
                color: #4ec9b0;
            }}
            .timestamp {{
                color: #858585;
            }}
            .footer {{
                margin-top: 20px;
                text-align: center;
                color: #858585;
                font-size: 12px;
            }}
        </style>
        <script>
            // Auto-refresh every 30 seconds
            setTimeout(function() {{
                location.reload();
            }}, 30000);
            
            // Highlight log levels
            window.onload = function() {{
                const logContent = document.querySelector('.log-content');
                if (logContent) {{
                    let html = logContent.innerHTML;
                    html = html.replace(/ERROR|CRITICAL|FATAL/gi, '<span class="error">$&</span>');
                    html = html.replace(/WARNING|WARN/gi, '<span class="warning">$&</span>');
                    html = html.replace(/INFO/gi, '<span class="info">$&</span>');
                    html = html.replace(/SUCCESS|OK/gi, '<span class="success">$&</span>');
                    html = html.replace(/\\d{{4}}-\\d{{2}}-\\d{{2}}[T ]\\d{{2}}:\\d{{2}}:\\d{{2}}/g, '<span class="timestamp">$&</span>');
                    logContent.innerHTML = html;
                }}
            }};
        </script>
    </head>
    <body>
        <div class="header">
            <h1>📋 {service_name} Logs</h1>
            <p><strong>Lines Shown:</strong> {lines}</p>
            {filter_info}
            <p><strong>Last Updated:</strong> {timestamp}</p>
        </div>
        
        <div class="controls">
            <a href="?lines=50">Last 50</a>
            <a href="?lines=100">Last 100</a>
            <a href="?lines=500">Last 500</a>
            <a href="?lines=1000">Last 1000</a>
            <a href="?lines=5000">All (5000)</a>
        </div>
        
        <div class="log-container">
            <div class="log-content">{logs}</div>
        </div>
        
        <div class="footer">
            Auto-refreshes every 30 seconds | <a href="?" style="color: #007acc;">Refresh Now</a>
        </div>
    </body>
    </html>
    """
    
    return html
