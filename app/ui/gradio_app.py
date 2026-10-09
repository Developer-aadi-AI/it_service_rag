"""Gradio chat prototype on top of the Phase 2 conversation services.

Features: multi-turn chat with memory, sources and recommendations panel,
contact form, "anything else?" flow, irrelevant-input counter, idle check and
auto-close (polled with gr.Timer), feedback buttons, transcript download and
email, notification sounds with an on/off toggle.

    from app.ui.gradio_app import build_demo
    demo, head = build_demo(services)
    demo.launch(head=head)
"""
from __future__ import annotations

import tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

import gradio as gr
from pydantic import ValidationError

from app.config import PROJECT_ROOT
from app.conversation.store import SessionClosed
from app.leads.service import LeadInput
from app.services import Services
from app.transcripts.builder import build_transcript, transcript_filename

SOUND_JS_PATH = PROJECT_ROOT / "app" / "static" / "widget" / "notification-sounds.js"
_PLAY_JS = "(enabled, kind) => { if (window.vgSound) window.vgSound.setEnabled(enabled); if (enabled && kind && window.vgPlay) window.vgPlay(kind); }"
_SENT_JS = "(enabled) => { if (window.vgSound) window.vgSound.setEnabled(enabled); if (enabled && window.vgPlay) window.vgPlay('sent'); }"


def _head() -> str:
    return f"<script>{SOUND_JS_PATH.read_text(encoding='utf-8')}</script>"


def _as_chat(messages) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in messages if m.kind != "form_submission"]


def _sources_md(result) -> str:
    rag = result.rag
    if not rag or not rag.sources:
        return "_No sources for this reply._"
    return "\n\n".join(f"**[{s.number}] [{s.title}]({s.url})** - {s.source_type}" + (f" | {s.platform}" if s.platform else "")
                       for s in rag.sources)


def _recs_md(recs) -> str:
    if not recs:
        return "_No recommendation for this reply._"
    p = recs["primary"]
    lines = [f"**Best fit:** [{p['name']}]({p['url']})" + (f"  \n{p['description']}" if p.get("description") else "")]
    if recs["related"]:
        lines.append("**Related:** " + ", ".join(f"[{r['name']}]({r['url']})" for r in recs["related"]))
    return "\n\n".join(lines)


def _status_md(services: Services, session, extra: str = "") -> str:
    s = services.settings
    remaining = max(0, s.irrelevant_limit - session.irrelevant_count)
    state = {"active": "Active", "awaiting_idle_response": "Waiting for you (idle check sent)",
             "closed": f"Closed ({session.close_reason})"}[session.status]
    text = (f"**Session:** {state}  \n**Off-topic attempts left:** {remaining} of {s.irrelevant_limit}  \n"
            f"**Idle check after:** {s.idle_check_after_seconds}s, then closes after {s.idle_close_after_seconds}s")
    return text + (f"  \n{extra}" if extra else "")


def build_demo(services: Services) -> tuple[gr.Blocks, str]:
    manager = services.manager

    def new_chat(sound):
        r = manager.start_session(sound_enabled=bool(sound))
        s = r.session
        return (s.session_id, r.reply.seq, _as_chat(s.messages), "_Ask a question to see sources._",
                "_Recommendations appear here._", _status_md(services, s), gr.update(visible=False),
                gr.update(visible=False), "", "message_received" if sound else "")

    def respond(message, sid, chat, sound):
        message = (message or "").strip()
        if not message or not sid:
            return (chat, "", gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), 0, "")
        try:
            r = manager.handle_message(sid, message)
        except SessionClosed:
            s = manager.get(sid)
            return (chat, "", gr.update(), gr.update(), _status_md(services, s, "_This chat has ended - start a new chat._"),
                    gr.update(visible=False), gr.update(visible=True), s.messages[-1].seq, "")
        except ValueError as exc:
            chat = chat + [{"role": "assistant", "content": str(exc)}]
            return chat, "", gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), 0, ""
        s = r.session
        show_form = bool(r.action and r.action.get("type") == "contact_form")
        show_feedback = s.closed
        extra = f"**Sentiment:** {r.sentiment.label}" if r.sentiment else ""
        return (_as_chat(s.messages), "", _sources_md(r), _recs_md(r.recommendations), _status_md(services, s, extra),
                gr.update(visible=show_form), gr.update(visible=show_feedback), s.messages[-1].seq,
                ("session_ended" if s.closed else "message_received") if s.sound_enabled else "")

    def submit_form(name, email, phone, company, msg, sid):
        try:
            data = LeadInput(name=name, email=email, phone=phone or None, company=company or None, message=msg)
        except ValidationError as exc:
            errs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return gr.update(), gr.update(visible=True), f"Please check: {errs}", gr.update(), 0, ""
        try:
            r = manager.submit_contact(sid, data)
        except SessionClosed:
            return gr.update(), gr.update(visible=False), "This chat has ended - start a new chat.", gr.update(), 0, ""
        s = r.session
        return (_as_chat(s.messages), gr.update(visible=False), "", _status_md(services, s), s.messages[-1].seq,
                "message_received" if s.sound_enabled else "")

    def give_feedback(rating, sid):
        msg = manager.record_feedback(sid, rating)
        s = manager.get(sid)
        return _as_chat(s.messages), gr.update(visible=False), msg.seq

    def poll(sid, last_seq):
        if not sid:
            return gr.update(), gr.update(), gr.update(), last_seq, ""
        s, new = manager.messages_after(sid, int(last_seq or 0))
        if not new:
            return gr.update(), gr.update(), gr.update(), last_seq, ""
        kind = {"idle_check": "idle_check", "session_end": "session_ended"}.get(new[-1].kind, "message_received")
        return (_as_chat(s.messages), _status_md(services, s), gr.update(visible=s.closed), new[-1].seq,
                kind if s.sound_enabled else "")

    def set_sound(sound, sid):
        if sid:
            manager.set_preferences(sid, sound_enabled=bool(sound))

    def end_chat(sid):
        r = manager.close_by_user(sid)
        s = r.session
        return _as_chat(s.messages), _status_md(services, s), gr.update(visible=True), s.messages[-1].seq, \
            "session_ended" if s.sound_enabled else ""

    def download(sid):
        s = manager.get(sid)
        path = Path(tempfile.gettempdir()) / transcript_filename(s)
        path.write_text(build_transcript(s), encoding="utf-8")
        return gr.update(value=str(path), visible=True)

    def email_transcript(address, sid):
        try:
            manager.send_email(sid, "transcript", address or None)
        except (ValueError, RuntimeError) as exc:
            return f"Could not send: {exc}"
        return "The transcript is on its way to your inbox."

    def outbox():
        folder = services.settings.outbox_path
        files = sorted(folder.glob("*.eml"))[-8:] if folder.exists() else []
        if not files:
            return f"_No emails yet (backend: {services.settings.email_backend})._"
        rows = []
        for f in reversed(files):
            msg = BytesParser(policy=policy.default).parsebytes(f.read_bytes())
            rows.append(f"- **{msg['Subject']}** to {msg['To']} - `{f.name}`")
        return "\n".join(rows)

    with gr.Blocks(title="D Group AI Assistant") as demo:
        gr.Markdown("## D Group AI Assistant - Phase 2 prototype")
        sid = gr.State("")
        last_seq = gr.State(0)
        sound_kind = gr.Textbox(visible=False)
        with gr.Row():
            with gr.Column(scale=3):
                chat = gr.Chatbot(height=470, label="Chat")
                msg = gr.Textbox(placeholder="Ask about D Group services, pricing, support, portfolio...", show_label=False)
                with gr.Row():
                    send = gr.Button("Send", variant="primary")
                    new_btn = gr.Button("New chat")
                    end_btn = gr.Button("End chat")
                gr.Examples(["What do you guys do?", "How much does the Shopify Gold plan cost?",
                             "And how long does it take to deliver?", "What about BigCommerce?",
                             "I need a mobile app for my business, what do you recommend?",
                             "This is so frustrating, my store keeps crashing!", "I want to talk to your sales team",
                             "Tell me a joke"], inputs=msg)
                with gr.Group(visible=False) as form:
                    gr.Markdown("### Connect with the D Group team")
                    f_name = gr.Textbox(label="Full name *")
                    f_email = gr.Textbox(label="Email *")
                    f_phone = gr.Textbox(label="Phone")
                    f_company = gr.Textbox(label="Company")
                    f_msg = gr.Textbox(label="How can we help? *", lines=3)
                    f_submit = gr.Button("Submit", variant="primary")
                    f_status = gr.Markdown()
                with gr.Group(visible=False) as feedback_box:
                    gr.Markdown("### How was your experience today?")
                    with gr.Row():
                        fb = {r: gr.Button(r) for r in ("Great", "OK", "Poor")}
            with gr.Column(scale=2):
                sound = gr.Checkbox(value=True, label="Notification sounds")
                status_md = gr.Markdown()
                gr.Markdown("### Sources")
                sources_md = gr.Markdown()
                gr.Markdown("### Recommended for you")
                recs_md = gr.Markdown()
                with gr.Accordion("Transcript & email", open=False):
                    dl_btn = gr.Button("Download transcript (.txt)")
                    dl_file = gr.File(visible=False, label="Transcript")
                    email_in = gr.Textbox(label="Email address")
                    email_btn = gr.Button("Email me the transcript")
                    email_status = gr.Markdown()
                    outbox_btn = gr.Button("Show sent emails (outbox)")
                    outbox_md = gr.Markdown()
        timer = gr.Timer(5)

        play = dict(fn=None, inputs=[sound, sound_kind], outputs=None, js=_PLAY_JS)
        start_outputs = [sid, last_seq, chat, sources_md, recs_md, status_md, form, feedback_box, f_status, sound_kind]
        demo.load(new_chat, [sound], start_outputs)
        new_btn.click(new_chat, [sound], start_outputs).then(**play)

        turn_outputs = [chat, msg, sources_md, recs_md, status_md, form, feedback_box, last_seq, sound_kind]
        for trigger in (send.click, msg.submit):
            trigger(fn=None, inputs=[sound], outputs=None, js=_SENT_JS).then(
                respond, [msg, sid, chat, sound], turn_outputs).then(**play)
        f_submit.click(submit_form, [f_name, f_email, f_phone, f_company, f_msg, sid],
                       [chat, form, f_status, status_md, last_seq, sound_kind]).then(**play)
        for rating, btn in fb.items():
            btn.click(lambda s, r=rating: give_feedback(r, s), [sid], [chat, feedback_box, last_seq])
        end_btn.click(end_chat, [sid], [chat, status_md, feedback_box, last_seq, sound_kind]).then(**play)
        timer.tick(poll, [sid, last_seq], [chat, status_md, feedback_box, last_seq, sound_kind]).then(**play)
        sound.change(set_sound, [sound, sid], None)
        dl_btn.click(download, [sid], [dl_file])
        email_btn.click(email_transcript, [email_in, sid], [email_status])
        outbox_btn.click(outbox, None, [outbox_md])
    return demo, _head()
