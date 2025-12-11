"""
Log Viewer Utility for Docker Containers
Provides functions to read and format container logs for web display.
"""
import subprocess
from typing import Optional, List
from datetime import datetime


def get_container_logs(
    container_name: str,
    lines: int = 100,
    grep_filter: Optional[str] = None,
    since: Optional[str] = None
) -> str:
    """
    Fetch logs from a Docker container.
    
    Args:
        container_name: Name of the Docker container
        lines: Number of lines to retrieve (default: 100)
        grep_filter: Optional string to filter logs
        since: Optional time duration (e.g., "1h", "30m", "1d")
    
    Returns:
        String containing the logs
    """
    try:
        # Build docker logs command
        cmd = ["docker", "logs", container_name, "--tail", str(lines)]
        
        if since:
            cmd.extend(["--since", since])
        
        # Execute command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        # Combine stdout and stderr
        logs = result.stdout + result.stderr
        
        # Apply grep filter if provided
        if grep_filter and logs:
            filtered_lines = [
                line for line in logs.split('\n')
                if grep_filter.lower() in line.lower()
            ]
            logs = '\n'.join(filtered_lines)
        
        return logs if logs else "No logs available"
        
    except subprocess.TimeoutExpired:
        return "Error: Log retrieval timed out"
    except FileNotFoundError:
        return "Error: Docker command not found. Are you running in a Docker environment?"
    except Exception as e:
        return f"Error retrieving logs: {str(e)}"


def format_logs_html(
    logs: str,
    service_name: str,
    container_name: str,
    lines: int,
    grep_filter: Optional[str] = None
) -> str:
    """
    Format logs as HTML for browser display.
    
    Args:
        logs: Raw log content
        service_name: Name of the service (e.g., "NewsAPI", "Scheduler")
        container_name: Docker container name
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
            <p><strong>Container:</strong> {container_name}</p>
            <p><strong>Lines:</strong> {lines}</p>
            {filter_info}
            <p><strong>Last Updated:</strong> {timestamp}</p>
        </div>
        
        <div class="controls">
            <a href="?lines=50">Last 50</a>
            <a href="?lines=100">Last 100</a>
            <a href="?lines=500">Last 500</a>
            <a href="?lines=1000">Last 1000</a>
            <a href="?since=1h">Last Hour</a>
            <a href="?since=24h">Last 24h</a>
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
