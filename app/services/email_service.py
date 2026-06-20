import logging

from app.core.config import settings

logger = logging.getLogger("chat.email")


async def send_verification_code(email: str, code: str) -> None:
    if settings.EMAIL_HOST == "smtp.example.com":
        logger.warning("SMTP not configured — printing verification code to stdout")
        print(f"[EMAIL] Verification code for {email}: {code}", flush=True)
        return

    import aiosmtplib
    from email.mime.text import MIMEText

    body = f"""
Welcome to Chat App!

Your verification code is: {code}

This code will expire in 10 minutes.

If you did not create an account, please ignore this email.
"""

    msg = MIMEText(body.strip(), "plain")
    msg["Subject"] = "Verify your Chat App account"
    msg["From"] = settings.EMAIL_USER
    msg["To"] = email

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.EMAIL_HOST,
            port=settings.EMAIL_PORT,
            username=settings.EMAIL_USER,
            password=settings.EMAIL_PASS,
            start_tls=True,
        )
        logger.info("Verification code sent to %s", email)
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", email, exc)
        print(f"[EMAIL] Verification code for {email}: {code}", flush=True)
