import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pymongo import MongoClient

from src.configs.settings import settings

def get_active_recipients():
    """
    Connects to MongoDB and fetches a list of active email addresses.
    """
    try:
        client = MongoClient(settings.DATABASE_URL)
        db = client[settings.MONGO_DB_NAME]
        collection = db["email_recipients"]

        # Fetch only active emails
        results = collection.find({"is_active": True})

        emails = [r["email"] for r in results]
        client.close()
        return emails

    except Exception as e:
        print(f"[EMAIL] Database Error: Could not fetch recipients: {e}")
        return []

def send_error_email(job_id: str, source_url: str, error_details: str, traceback_info: str = None):
    """
    Sends an email notification to all active recipients found in the DB.
    """
    if not settings.SMTP_SERVER or not settings.SMTP_EMAIL:
        print("[EMAIL] SMTP settings not configured. Skipping.")
        return

    # 1. Get Recipients from DB
    recipients = get_active_recipients()

    if not recipients:
        print(f"[EMAIL] No active recipients found in database collection 'email_recipients'. Skipping.")
        return

    subject = f"❌ NewsAgent Failure: Job {job_id}"

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
    msg['From'] = settings.SMTP_EMAIL
    # Join list of emails into a comma-separated string for the 'To' header
    msg['To'] = ", ".join(recipients)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT)
        server.starttls()
        server.login(settings.SMTP_EMAIL, settings.SMTP_PASSWORD)

        # send_message automatically handles sending to all recipients in the 'To' header
        server.send_message(msg)
        server.quit()

        print(f"[EMAIL] Notification sent to {len(recipients)} recipients for Job {job_id}")
    except Exception as e:
        print(f"[EMAIL] Failed to send email: {e}")