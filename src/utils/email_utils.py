import asyncio
import inspect
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import certifi
from pymongo import AsyncMongoClient, MongoClient

from src.configs.settings import settings


logger = logging.getLogger(__name__)
_async_email_mongo_client = None


def _mongo_client_options() -> dict:
    options = {
        "connectTimeoutMS": int(os.getenv("MONGO_CONNECT_TIMEOUT_MS", "5000")),
        "socketTimeoutMS": int(os.getenv("MONGO_SOCKET_TIMEOUT_MS", "5000")),
        "serverSelectionTimeoutMS": int(
            os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000")
        ),
        "retryWrites": True,
        "retryReads": True,
        "maxPoolSize": int(os.getenv("MONGO_MAX_POOL_SIZE", "100")),
        "minPoolSize": int(os.getenv("MONGO_MIN_POOL_SIZE", "0")),
        "maxIdleTimeMS": int(os.getenv("MONGO_MAX_IDLE_TIME_MS", "50000")),
        "waitQueueTimeoutMS": int(os.getenv("MONGO_WAIT_QUEUE_TIMEOUT_MS", "2000")),
    }

    if "mongodb.net" in settings.DATABASE_URL or settings.DATABASE_URL.startswith(
        "mongodb+srv://"
    ):
        options["tls"] = True
        options["tlsCAFile"] = certifi.where()

    return options


def get_async_email_database():
    global _async_email_mongo_client

    if _async_email_mongo_client is None:
        _async_email_mongo_client = AsyncMongoClient(
            settings.DATABASE_URL,
            **_mongo_client_options(),
        )

    return _async_email_mongo_client[settings.MONGO_DB_NAME]


async def close_async_email_client() -> None:
    global _async_email_mongo_client

    if _async_email_mongo_client is None:
        return

    result = _async_email_mongo_client.close()
    if inspect.isawaitable(result):
        await result
    _async_email_mongo_client = None


def get_active_recipients():
    """
    Connects to MongoDB and fetches a list of active email addresses.
    """
    client = None
    try:
        client = MongoClient(settings.DATABASE_URL, **_mongo_client_options())
        db = client[settings.MONGO_DB_NAME]
        collection = db["email_recipients"]
        results = collection.find({"is_active": True}, {"email": 1})
        return [r["email"] for r in results if r.get("email")]

    except Exception as e:
        print(f"[EMAIL] Database Error: Could not fetch recipients: {e}")
        return []
    finally:
        if client:
            client.close()


async def get_active_recipients_async():
    """
    Fetches active recipient emails without blocking the event loop.
    """
    try:
        db = get_async_email_database()
        cursor = db["email_recipients"].find({"is_active": True}, {"email": 1})
        return [r["email"] async for r in cursor if r.get("email")]

    except Exception as e:
        logger.warning(f"[EMAIL] Database error: could not fetch recipients: {e}")
        return []


def _smtp_is_configured() -> bool:
    if not settings.SMTP_SERVER or not settings.SMTP_EMAIL:
        print("[EMAIL] SMTP settings not configured. Skipping.")
        return False
    return True


def _build_error_email_message(
    recipients,
    job_id: str,
    source_url: str,
    error_details: str,
    traceback_info: str = None,
):
    body = f"""
    <html>
      <body>
        <h3>Job Failed</h3>
        <p><strong>Job ID:</strong> {job_id}</p>
        <p><strong>Source URL:</strong> {source_url}</p>
        <hr>
        <h4>Error Details:</h4>
        <p>{error_details}</p>
    """

    if traceback_info:
        body += f"""
        <hr>
        <h4>Traceback:</h4>
        <pre>{traceback_info}</pre>
        """

    body += """
      </body>
    </html>
    """

    msg = MIMEMultipart()
    msg["From"] = settings.SMTP_EMAIL
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"NewsAgent Failure: Job {job_id}"
    msg.attach(MIMEText(body, "html"))
    return msg


def _send_smtp_message(msg, recipient_count: int, job_id: str) -> None:
    timeout = float(os.getenv("SMTP_TIMEOUT_SECONDS", "10"))
    server = smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT, timeout=timeout)
    try:
        server.starttls()
        server.login(settings.SMTP_EMAIL, settings.SMTP_PASSWORD)
        server.send_message(msg)
        print(f"[EMAIL] Notification sent to {recipient_count} recipients for Job {job_id}")
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_error_email(
    job_id: str,
    source_url: str,
    error_details: str,
    traceback_info: str = None,
) -> None:
    """
    Sends an email notification to all active recipients found in the DB.
    """
    if not _smtp_is_configured():
        return

    recipients = get_active_recipients()

    if not recipients:
        print("[EMAIL] No active recipients found in database collection 'email_recipients'. Skipping.")
        return

    msg = _build_error_email_message(
        recipients,
        job_id=job_id,
        source_url=source_url,
        error_details=error_details,
        traceback_info=traceback_info,
    )

    try:
        _send_smtp_message(msg, len(recipients), job_id)
    except Exception as e:
        print(f"[EMAIL] Failed to send email: {e}")


async def send_error_email_async(
    job_id: str,
    source_url: str,
    error_details: str,
    traceback_info: str = None,
) -> None:
    """
    Sends an error notification without blocking the event loop.

    Recipient lookup uses async MongoDB. SMTP is isolated in a worker thread
    because smtplib itself is synchronous.
    """
    if not _smtp_is_configured():
        return

    recipients = await get_active_recipients_async()

    if not recipients:
        print("[EMAIL] No active recipients found in database collection 'email_recipients'. Skipping.")
        return

    msg = _build_error_email_message(
        recipients,
        job_id=job_id,
        source_url=source_url,
        error_details=error_details,
        traceback_info=traceback_info,
    )

    try:
        await asyncio.to_thread(_send_smtp_message, msg, len(recipients), job_id)
    except Exception as e:
        print(f"[EMAIL] Failed to send email: {e}")
