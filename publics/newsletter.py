import logging
from datetime import datetime

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string

from .models import NewsletterSubscriber

logger = logging.getLogger(__name__)

TEMPLATE_MAPPING = {
    "standard": "emails/standard.html",
    "promo": "emails/promo.html",
    "update": "emails/update.html",
}

BATCH_SIZE = 50  # most SMTP servers cap the number of recipients per message


def send_newsletter(subject: str, content: str, template_type: str = "standard"):
    """
    Sends a newsletter to every active subscriber in BCC batches over one SMTP connection.

    Returns (sent_count, total_recipients). Raises ValueError when there are no subscribers.
    A failure part-way keeps the count of what was already delivered, so the caller can record
    an honest recipient_count instead of claiming the whole list was reached.
    """
    recipients = list(
        NewsletterSubscriber.objects.filter(is_active=True).values_list("email", flat=True)
    )
    if not recipients:
        raise ValueError("No active subscribers found to broadcast to.")

    template_file = TEMPLATE_MAPPING.get(template_type, TEMPLATE_MAPPING["standard"])
    html_content = render_to_string(template_file, {
        "subject": subject,
        "content": content,
        "date": datetime.now().strftime("%B %Y"),
    })

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
    sent = 0
    try:
        connection = get_connection()
        for start in range(0, len(recipients), BATCH_SIZE):
            chunk = recipients[start:start + BATCH_SIZE]
            message = EmailMultiAlternatives(
                subject=subject,
                body=content,
                from_email=from_email,
                to=[from_email or "no-reply@aika.com"],
                bcc=chunk,
                connection=connection,
            )
            message.attach_alternative(html_content, "text/html")
            message.send(fail_silently=False)
            sent += len(chunk)
    except Exception:
        logger.exception("Newsletter send failed after %s of %s recipients", sent, len(recipients))

    return sent, len(recipients)
