"""Customer-facing email templates (plain text + simple HTML).

Templates receive only customer-facing data. Nothing internal (scores,
statuses, sentiment, prompts, IDs other than a short reference) is rendered.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape


@dataclass
class Footer:
    email: str = ""
    phone: str = ""
    contact_url: str = ""

    def text(self) -> str:
        parts = [p for p in (self.email, self.phone, self.contact_url) if p]
        return "V Group" + (" | " + " | ".join(parts) if parts else "")

    def html(self) -> str:
        return escape(self.text())


@dataclass
class Rendered:
    subject: str
    text: str
    html: str


def _greeting(name: str | None) -> str:
    return f"Hi {name.split()[0]}," if name else "Hello,"


def _wrap_html(paragraphs: list[str], footer: Footer, extra_html: str = "") -> str:
    body = "".join(f"<p style=\"margin:0 0 14px\">{p}</p>" for p in paragraphs)
    return (
        "<!doctype html><html><body style=\"margin:0;padding:24px;background:#f5f6f8;"
        "font-family:Arial,Helvetica,sans-serif;color:#1f2933;line-height:1.5\">"
        "<div style=\"max-width:560px;margin:0 auto;background:#ffffff;border-radius:8px;padding:28px\">"
        f"{body}{extra_html}"
        "<hr style=\"border:none;border-top:1px solid #e4e7eb;margin:24px 0 12px\">"
        f"<p style=\"font-size:12px;color:#6b7280;margin:0\">{footer.html()}</p>"
        "</div></body></html>"
    )


def _bullets(points: list[str]) -> tuple[str, str]:
    text = "\n".join(f"  - {p}" for p in points)
    html = "<ul style=\"margin:0 0 14px;padding-left:20px\">" + "".join(
        f"<li style=\"margin-bottom:6px\">{escape(p)}</li>" for p in points) + "</ul>"
    return text, html


def _feedback_block(links: dict[str, str]) -> tuple[str, str]:
    text = "How was your chat experience? Choose one:\n" + "\n".join(f"  {label}: {url}" for label, url in links.items())
    buttons = "".join(
        f"<a href=\"{escape(url)}\" style=\"display:inline-block;margin:0 8px 8px 0;padding:9px 18px;"
        f"border-radius:6px;background:#1f4fd1;color:#ffffff;text-decoration:none;font-weight:bold\">"
        f"{escape(label)}</a>" for label, url in links.items())
    html = f"<p style=\"margin:0 0 10px\">How was your chat experience?</p><p>{buttons}</p>"
    return text, html


def summary_email(name: str | None, date_text: str, points: list[str], footer: Footer,
                  transcript_attached: bool, feedback_links: dict[str, str] | None) -> Rendered:
    bullets_text, bullets_html = _bullets(points)
    intro = f"Thank you for chatting with V Group on {date_text}. Here is a short summary of our conversation:"
    attach = "A full transcript of the chat is attached for your records." if transcript_attached else ""
    closing = ("If you have more questions, simply start a new chat on our website or contact us using "
               "the details below.")
    text_parts = [_greeting(name), "", intro, "", bullets_text, ""]
    if attach:
        text_parts += [attach, ""]
    extra_html = ""
    if feedback_links:
        fb_text, fb_html = _feedback_block(feedback_links)
        text_parts += [fb_text, ""]
        extra_html = fb_html
    text_parts += [closing, "", "Best regards,", "The V Group team", "", footer.text()]
    html_pars = [escape(_greeting(name)), escape(intro)]
    html = _wrap_html(html_pars, footer, bullets_html + (f"<p>{escape(attach)}</p>" if attach else "") + extra_html
                      + f"<p>{escape(closing)}</p><p>Best regards,<br>The V Group team</p>")
    return Rendered("Your conversation with V Group - summary", "\n".join(text_parts), html)


def transcript_email(name: str | None, date_text: str, footer: Footer) -> Rendered:
    text = (f"{_greeting(name)}\n\nAs requested, the transcript of your chat with V Group on {date_text} "
            f"is attached as a text file.\n\nBest regards,\nThe V Group team\n\n{footer.text()}")
    html = _wrap_html([escape(_greeting(name)),
                       escape(f"As requested, the transcript of your chat with V Group on {date_text} is "
                              f"attached as a text file."),
                       "Best regards,<br>The V Group team"], footer)
    return Rendered("Your V Group chat transcript", text, html)


def followup_email(name: str | None, date_text: str, reason: str, points: list[str], footer: Footer,
                   transcript_attached: bool) -> Rendered:
    if reason == "idle_timeout":
        opener = (f"Our chat on {date_text} was closed because we didn't hear back from you for a while. "
                  f"No problem - here's a quick recap so you can pick up where you left off:")
        subject = "Following up on your V Group chat"
    else:  # after a contact request
        opener = (f"Thank you for reaching out to V Group on {date_text}. We've received your request, and "
                  f"the relevant team will connect with you shortly. Here's a quick recap of the chat:")
        subject = "We've received your request - V Group"
    bullets_text, bullets_html = _bullets(points)
    attach = "The full chat transcript is attached." if transcript_attached else ""
    closing = "Whenever you're ready, start a new chat on our website or contact us using the details below."
    text = "\n".join([_greeting(name), "", opener, "", bullets_text, "", attach, "", closing, "",
                      "Best regards,", "The V Group team", "", footer.text()])
    html = _wrap_html([escape(_greeting(name)), escape(opener)], footer,
                      bullets_html + (f"<p>{escape(attach)}</p>" if attach else "") + f"<p>{escape(closing)}</p>"
                      "<p>Best regards,<br>The V Group team</p>")
    return Rendered(subject, text, html)


def feedback_email(name: str | None, date_text: str, links: dict[str, str], footer: Footer) -> Rendered:
    fb_text, fb_html = _feedback_block(links)
    intro = f"Thank you for chatting with V Group on {date_text}. We'd love to hear how it went - it takes one click."
    text = "\n".join([_greeting(name), "", intro, "", fb_text, "", "Best regards,", "The V Group team", "",
                      footer.text()])
    html = _wrap_html([escape(_greeting(name)), escape(intro)], footer,
                      fb_html + "<p>Best regards,<br>The V Group team</p>")
    return Rendered("How was your chat with V Group?", text, html)


def team_lead_email(lead, points: list[str]) -> Rendered:
    """Internal notification to the V Group team (not customer-facing)."""
    fields = [("Name", lead.name), ("Email", lead.email), ("Phone", lead.phone or "-"),
              ("Company", lead.company or "-"), ("Interested in", lead.interest or "-"),
              ("Received", lead.created_at), ("Lead ID", lead.lead_id)]
    text = "New contact request from the website chat\n\n" + "\n".join(f"{k}: {v}" for k, v in fields)
    text += f"\n\nMessage:\n{lead.message}\n\nChat summary:\n" + "\n".join(f"  - {p}" for p in points)
    rows = "".join(f"<tr><td style=\"padding:4px 12px 4px 0;color:#6b7280\">{escape(k)}</td>"
                   f"<td style=\"padding:4px 0\">{escape(str(v))}</td></tr>" for k, v in fields)
    _, bullets_html = _bullets(points)
    html = _wrap_html(["<strong>New contact request from the website chat</strong>"], Footer(),
                      f"<table>{rows}</table><p><strong>Message</strong><br>{escape(lead.message)}</p>"
                      f"<p><strong>Chat summary</strong></p>{bullets_html}")
    return Rendered(f"New chat lead: {lead.name}" + (f" - {lead.interest}" if lead.interest else ""), text, html)
