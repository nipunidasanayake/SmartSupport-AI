"""A beginner-friendly customer support chatbot for ShopEase."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError
from httpx import HTTPError, TimeoutException


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DATABASE_DIR = ROOT_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "support_tickets.db"
FAQ_PATH = DATA_DIR / "faq_knowledge_base.csv"
HISTORICAL_TICKETS_PATH = DATA_DIR / "customer_tickets.csv"

CATEGORIES = (
    "Order",
    "Shipping",
    "Payment",
    "Account",
    "Product",
    "Refund",
    "Returns",
    "Subscription",
    "Technical",
    "General",
)
SENTIMENTS = ("Positive", "Neutral", "Negative")
PRIORITIES = ("Low", "Medium", "High")
STATUSES = ("Open", "Pending", "Resolved", "Escalated")
DEFAULT_MODEL = "gemini-4-hydrogen"
GEMINI_REQUEST_TIMEOUT_MS = 45_000
FAQ_MATCH_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "ago",
        "can",
        "could",
        "for",
        "from",
        "how",
        "i",
        "is",
        "it",
        "like",
        "my",
        "of",
        "the",
        "this",
        "three",
        "to",
        "was",
        "what",
        "when",
        "would",
    }
)

load_dotenv(ROOT_DIR / ".env")


def render_visual_theme() -> None:
    """Apply a consistent, responsive visual system to the Streamlit app."""
    st.markdown(
        """
        <style>
        :root {
            --support-ink: #17263c;
            --support-muted: #64748b;
            --support-blue: #2563eb;
            --support-border: #e2e8f0;
            --support-surface: #ffffff;
        }
        [data-testid="stAppViewContainer"] {
            background: #f4f7fb;
            color: var(--support-ink);
        }
        [data-testid="stHeader"] {
            background: rgba(244, 247, 251, 0.92);
        }
        [data-testid="stMainBlockContainer"] {
            max-width: 1440px;
            padding: 2rem clamp(1rem, 3vw, 2.75rem) 3.5rem;
        }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #10243d 0%, #172f4d 100%);
            border-right: 1px solid #203955;
        }
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {
            color: #f8fafc;
            letter-spacing: -0.02em;
        }
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] .stCaption {
            color: #c3d0df;
        }
        [data-testid="stSidebar"] [data-testid="stTextInput"] input {
            background: #f8fafc;
            color: #17263c;
            border-radius: 9px;
        }
        [data-testid="stSidebar"] [data-testid="stRadio"] label {
            color: #e2eaf3;
        }
        h1, h2, h3 {
            color: var(--support-ink);
            letter-spacing: -0.035em;
        }
        h1 {
            font-size: clamp(1.8rem, 2.5vw, 2.35rem);
            font-weight: 720;
            line-height: 1.15;
        }
        h2, h3 {
            font-weight: 650;
        }
        [data-testid="stMetric"] {
            min-height: 112px;
            padding: 1rem 1.1rem;
            background: var(--support-surface);
            border: 1px solid var(--support-border);
            border-radius: 14px;
            box-shadow: 0 3px 12px rgba(23, 38, 60, 0.045);
        }
        [data-testid="stMetricLabel"] p {
            color: var(--support-muted);
            font-size: 0.82rem;
            font-weight: 600;
        }
        [data-testid="stMetricValue"] {
            color: var(--support-ink);
            font-weight: 700;
        }
        [data-testid="stDataFrame"] {
            overflow: hidden;
            background: var(--support-surface);
            border: 1px solid var(--support-border);
            border-radius: 12px;
        }
        [data-testid="stPlotlyChart"] {
            background: var(--support-surface);
            border-radius: 10px;
        }
        [data-testid="stChatMessage"] {
            padding: 1rem 1.15rem;
            background: var(--support-surface);
            border: 1px solid var(--support-border);
            border-radius: 14px;
        }
        [data-testid="stChatInput"] {
            background: var(--support-surface);
            border: 1px solid #cbd5e1;
            border-radius: 14px;
            box-shadow: 0 5px 18px rgba(23, 38, 60, 0.07);
        }
        [data-testid="stAlert"],
        [data-testid="stExpander"] {
            border-radius: 12px;
        }
        .stButton > button,
        .stDownloadButton > button {
            min-height: 2.65rem;
            border-radius: 9px;
            font-weight: 600;
            transition: border-color 120ms ease, background 120ms ease;
        }
        [data-testid="stBaseButton-primary"] {
            background: var(--support-blue);
            border-color: var(--support-blue);
            color: #ffffff;
        }
        [data-testid="stBaseButton-primary"]:hover {
            background: #1d4ed8;
            border-color: #1d4ed8;
            color: #ffffff;
        }
        div[data-baseweb="select"] > div,
        [data-testid="stTextInput"] input {
            border-radius: 9px;
        }
        @media (max-width: 720px) {
            [data-testid="stMainBlockContainer"] {
                padding-top: 1.2rem;
            }
            [data-testid="stMetric"] {
                min-height: 96px;
                padding: 0.8rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def load_faqs() -> pd.DataFrame:
    """Load the FAQ knowledge base and verify its required columns."""
    required_columns = {"faq_id", "category", "question", "answer"}
    faqs = pd.read_csv(FAQ_PATH)
    if not required_columns.issubset(faqs.columns):
        missing = sorted(required_columns.difference(faqs.columns))
        raise ValueError(f"The FAQ dataset is missing columns: {', '.join(missing)}")
    if faqs.empty:
        raise ValueError("The FAQ knowledge base has no entries.")
    return faqs


def initialize_database() -> None:
    """Create the ticket table and import the sample tickets once."""
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    customer_message TEXT NOT NULL,
                    category TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    ai_response TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    response_source TEXT NOT NULL DEFAULT 'Gemini',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    feedback INTEGER,
                    reopened_count INTEGER NOT NULL DEFAULT 0,
                    resolved_at TEXT,
                    assignee TEXT NOT NULL DEFAULT 'Unassigned',
                    channel TEXT NOT NULL DEFAULT 'Chat'
                )
                """
            )
            ticket_columns = {
                row[1]: row
                for row in connection.execute("PRAGMA table_info(tickets)")
            }
            new_columns = {
                "response_source": "TEXT NOT NULL DEFAULT 'Gemini'",
                "conversation_id": "TEXT NOT NULL DEFAULT ''",
                "feedback": "INTEGER",
                "reopened_count": "INTEGER NOT NULL DEFAULT 0",
                "resolved_at": "TEXT",
                "assignee": "TEXT NOT NULL DEFAULT 'Unassigned'",
                "channel": "TEXT NOT NULL DEFAULT 'Chat'",
            }
            for column, definition in new_columns.items():
                if column not in ticket_columns:
                    connection.execute(
                        f"ALTER TABLE tickets ADD COLUMN {column} {definition}"
                    )
            ticket_count = connection.execute(
                "SELECT COUNT(*) FROM tickets"
            ).fetchone()[0]
            if ticket_count == 0:
                sample_tickets = pd.read_csv(HISTORICAL_TICKETS_PATH)
                sample_tickets.to_sql(
                    "tickets", connection, if_exists="append", index=False
                )


def create_ticket(
    customer_message: str,
    analysis: dict[str, Any],
    response_source: str = "Gemini",
    conversation_id: str = "",
) -> str:
    """Store an inquiry and return its ticket number."""
    ticket_id = f"T{uuid.uuid4().hex[:8].upper()}"
    status = "Escalated" if analysis["escalate"] else "Open"
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO tickets (
                    ticket_id, customer_message, category, sentiment, priority,
                    summary, ai_response, status, created_at, response_source,
                    conversation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
                """,
                (
                    ticket_id,
                    customer_message,
                    analysis["category"],
                    analysis["sentiment"],
                    analysis["priority"],
                    analysis["summary"],
                    analysis["answer"],
                    status,
                    response_source,
                    conversation_id,
                ),
            )
    return ticket_id


def read_tickets() -> pd.DataFrame:
    """Return all tickets, newest first."""
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        return pd.read_sql_query(
            "SELECT * FROM tickets ORDER BY created_at DESC, ticket_id DESC",
            connection,
        )


def read_conversation_messages(conversation_id: str) -> list[dict[str, Any]]:
    """Load chat turns persisted for one browser conversation."""
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT ticket_id, customer_message, category, sentiment, priority,
                   summary, ai_response, status, response_source, feedback
            FROM tickets
            WHERE conversation_id = ?
            ORDER BY rowid
            """,
            (conversation_id,),
        ).fetchall()

    messages: list[dict[str, Any]] = []
    for (
        ticket_id,
        customer_message,
        category,
        sentiment,
        priority,
        summary,
        ai_response,
        status,
        response_source,
        feedback,
    ) in rows:
        messages.extend(
            [
                {"role": "user", "content": customer_message},
                {
                    "role": "assistant",
                    "content": ai_response,
                    "analysis": {
                        "category": category,
                        "sentiment": sentiment,
                        "priority": priority,
                        "summary": summary,
                        "escalate": status == "Escalated",
                        "status": status,
                        "response_source": response_source,
                    },
                    "ticket_id": ticket_id,
                    "response_source": response_source,
                    "feedback": feedback,
                },
            ]
        )
    return messages


def update_ticket(
    ticket_id: str, status: str, assignee: str
) -> None:
    """Update a ticket's workflow status and assigned support agent."""
    if status not in STATUSES:
        raise ValueError(f"Unknown ticket status: {status}")
    if not assignee.strip():
        raise ValueError("Assignee cannot be empty.")
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            current = connection.execute(
                "SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"Ticket {ticket_id} was not found.")
            previous_status = current[0]
            if previous_status == status:
                connection.execute(
                    "UPDATE tickets SET assignee = ? WHERE ticket_id = ?",
                    (assignee.strip(), ticket_id),
                )
                return
            reopened_increment = int(
                previous_status == "Resolved" and status != "Resolved"
            )
            resolved_at = (
                "datetime('now')" if status == "Resolved" else "NULL"
            )
            connection.execute(
                f"""
                UPDATE tickets
                SET status = ?,
                    assignee = ?,
                    reopened_count = reopened_count + ?,
                    resolved_at = {resolved_at}
                WHERE ticket_id = ?
                """,
                (status, assignee.strip(), reopened_increment, ticket_id),
            )


def update_ticket_feedback(ticket_id: str, feedback: int) -> None:
    """Persist a thumbs-down (0) or thumbs-up (1) customer rating."""
    if feedback not in (0, 1):
        raise ValueError("Feedback must be 0 (negative) or 1 (positive).")
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            cursor = connection.execute(
                "UPDATE tickets SET feedback = ? WHERE ticket_id = ?",
                (feedback, ticket_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Ticket {ticket_id} was not found.")


def get_conversation_id() -> str:
    """Get or create a persistent, unguessable ID for this browser conversation."""
    conversation_id = st.query_params.get("conversation_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", conversation_id):
        conversation_id = uuid.uuid4().hex
        st.query_params["conversation_id"] = conversation_id
    return conversation_id


def find_relevant_faqs(
    message: str, faqs: pd.DataFrame, limit: int = 5
) -> pd.DataFrame:
    """Return FAQ entries whose questions share meaningful words with a message."""
    message_words = {
        word
        for word in re.findall(r"[a-z0-9]+", message.lower())
        if len(word) > 2 and word not in FAQ_MATCH_STOPWORDS
    }
    if not message_words:
        return faqs.iloc[0:0]

    matches: list[tuple[int, int]] = []
    for index, row in enumerate(faqs.itertuples(index=False)):
        faq_words = set(
            re.findall(r"[a-z0-9]+", f"{row.category} {row.question}".lower())
        )
        score = len(message_words.intersection(faq_words))
        if score:
            matches.append((score, index))
    matches.sort(key=lambda match: (-match[0], match[1]))
    minimum_score = 2 if matches and matches[0][0] > 1 else 1
    return faqs.iloc[
        [
            index
            for score, index in matches
            if score >= minimum_score
        ][:limit]
    ]


def build_faq_fallback(
    message: str, faqs: pd.DataFrame, reason: str = "Gemini was unavailable"
) -> str | None:
    """Build a clearly labeled FAQ-only reply when Gemini is unavailable."""
    relevant_faqs = find_relevant_faqs(message, faqs)
    if relevant_faqs.empty:
        return None
    faq_answers = "\n".join(
        f"- **{row.category}:** {row.answer}"
        for row in relevant_faqs.itertuples(index=False)
    )
    return (
        f"{reason}. This reply is based only on the ShopEase FAQ. "
        "Sentiment and priority were not AI-analyzed:\n\n"
        f"{faq_answers}"
    )


def clear_pending_message() -> None:
    for key in (
        "pending_message",
        "pending_message_error",
        "process_pending_message",
    ):
        st.session_state.pop(key, None)


def append_service_fallback(
    message: str, conversation_id: str, reason: str
) -> None:
    """Save an Open ticket without using FAQ content when Gemini fails."""
    analysis = {
        "answer": (
            f"{reason}. I couldn't get a Gemini response, so your request "
            "has been saved as an Open ticket for support follow-up."
        ),
        "category": "General",
        "sentiment": "Not analyzed",
        "priority": "Not analyzed",
        "summary": f"Gemini unavailable: {message[:120]}",
        "escalate": False,
    }
    create_ticket(
        message,
        analysis,
        response_source="Service fallback",
        conversation_id=conversation_id,
    )
    clear_pending_message()


def append_timeout_fallback(
    message: str,
    faqs: pd.DataFrame,
    conversation_id: str,
    reason: str,
) -> None:
    """Use FAQ only after a Gemini timeout, otherwise save an Open ticket."""
    relevant_faqs = find_relevant_faqs(message, faqs)
    answer = build_faq_fallback(message, faqs, reason)
    if answer is None:
        append_service_fallback(message, conversation_id, reason)
        return
    analysis = {
        "answer": answer,
        "category": str(relevant_faqs.iloc[0]["category"]),
        "sentiment": "Not analyzed",
        "priority": "Not analyzed",
        "summary": f"FAQ fallback: {message[:120]}",
        "escalate": False,
    }
    create_ticket(
        message,
        analysis,
        response_source="FAQ fallback",
        conversation_id=conversation_id,
    )
    clear_pending_message()


def analyze_inquiry(
    client: genai.Client, model: str, message: str, faqs: pd.DataFrame
) -> dict[str, Any]:
    """Ask Gemini for a customer response and structured ticket analysis."""
    relevant_faqs = find_relevant_faqs(message, faqs)
    prompt_faqs = relevant_faqs if not relevant_faqs.empty else faqs
    faq_context = "\n".join(
        f"- [{row.category}] Q: {row.question} A: {row.answer}"
        for row in prompt_faqs.itertuples(index=False)
    )
    prompt = f"""
You are the customer support assistant for ShopEase, a fictional online store.
Use the company FAQ below as the source of truth. If it does not answer the
question, say so plainly and recommend contacting a human support agent. Do not
promise refunds, deliveries, or actions that are not in the FAQ.

FAQ:
{faq_context}

Customer message:
{message}

Return only a JSON object with exactly these fields:
{{
  "answer": "A concise, empathetic customer-facing response",
  "category": "One of: {", ".join(CATEGORIES)}",
  "sentiment": "One of: {", ".join(SENTIMENTS)}",
  "priority": "One of: {", ".join(PRIORITIES)}",
  "summary": "A short summary of the issue",
  "escalate": false
}}
Set escalate to true when a human should investigate, such as a disputed or
possibly duplicated charge, a security concern, or an unresolved urgent issue.
"""
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )
    if not response.text:
        raise ValueError("Gemini returned an empty response. Please try again.")

    result = json.loads(response.text)
    required_fields = {
        "answer",
        "category",
        "sentiment",
        "priority",
        "summary",
        "escalate",
    }
    if not isinstance(result, dict) or not required_fields.issubset(result):
        raise ValueError("Gemini returned an incomplete ticket analysis.")
    for field in ("answer", "category", "sentiment", "priority", "summary"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ValueError(f"Gemini returned an invalid {field} value.")
        result[field] = result[field].strip()
    if result["category"] not in CATEGORIES:
        raise ValueError("Gemini returned an unknown ticket category.")
    if result["sentiment"] not in SENTIMENTS:
        raise ValueError("Gemini returned an unknown sentiment.")
    if result["priority"] not in PRIORITIES:
        raise ValueError("Gemini returned an unknown priority.")
    if not isinstance(result["escalate"], bool):
        raise ValueError("Gemini returned an invalid escalation recommendation.")
    return result


def show_analysis(analysis: dict[str, Any], ticket_id: str) -> None:
    """Display the ticket details below an assistant response."""
    response_source = analysis.get("response_source")
    source_label = f" · {response_source}" if response_source else ""
    st.caption(
        f"Ticket {ticket_id} · {analysis['summary']}{source_label}"
    )
    columns = st.columns(4)
    columns[0].metric("Category", analysis["category"])
    columns[1].metric("Sentiment", analysis["sentiment"])
    columns[2].metric("Priority", analysis["priority"])
    columns[3].metric(
        "Status",
        analysis.get(
            "status", "Escalated" if analysis["escalate"] else "Open"
        ),
    )
    if analysis["escalate"]:
        st.warning("This inquiry is marked for human follow-up.")


def schedule_pending_message_retry() -> None:
    st.session_state.process_pending_message = True


def start_new_conversation() -> None:
    st.query_params["conversation_id"] = uuid.uuid4().hex
    clear_pending_message()


def render_chat(
    api_key: str,
    model: str,
    faqs: pd.DataFrame,
    conversation_id: str,
) -> None:
    title_column, action_column = st.columns([5, 1])
    with title_column:
        st.title("Support center")
        st.caption(
            "Personalized help for orders, delivery, payments, accounts, and products."
        )
    with action_column:
        st.button(
            "New chat",
            key="new_conversation",
            type="primary",
            on_click=start_new_conversation,
        )

    if not api_key:
        st.info(
            "Add a Gemini API key in the sidebar to get Gemini answers. "
            "Without one, questions are saved as Open tickets. FAQ answers "
            "are used only if a Gemini request times out."
        )

    messages = read_conversation_messages(conversation_id)
    if not messages:
        with st.chat_message("assistant"):
            st.markdown(
                "**Welcome to ShopEase Support**  \n"
                "Tell us what you need help with, and we'll take it from there."
            )

    for item in messages:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])
            if item["role"] == "assistant" and "analysis" in item:
                show_analysis(item["analysis"], item["ticket_id"])
                feedback = st.feedback(
                    "thumbs",
                    key=f"feedback_{item['ticket_id']}",
                    default=item["feedback"],
                )
                if feedback is not None and feedback != item["feedback"]:
                    update_ticket_feedback(item["ticket_id"], feedback)
                    st.caption("Thanks for your feedback.")

    pending_message = st.session_state.get("pending_message")
    message = st.chat_input(
        "How can we help?",
        disabled=pending_message is not None,
        max_chars=2000,
    )
    if message:
        st.session_state.pending_message = message
        st.session_state.pending_message_error = None
        st.session_state.process_pending_message = True

    if st.session_state.get("process_pending_message", False):
        st.session_state.process_pending_message = False
        pending_message = st.session_state.pending_message
        if not api_key:
            append_service_fallback(
                pending_message,
                conversation_id,
                "Gemini API key is missing",
            )
            st.rerun()
        try:
            with st.spinner("Reviewing your question..."):
                client = genai.Client(
                    api_key=api_key,
                    http_options=genai.types.HttpOptions(
                        timeout=GEMINI_REQUEST_TIMEOUT_MS
                    ),
                )
                analysis = analyze_inquiry(client, model, pending_message, faqs)
                create_ticket(
                    pending_message,
                    analysis,
                    conversation_id=conversation_id,
                )
        except TimeoutException:
            append_timeout_fallback(
                pending_message,
                faqs,
                conversation_id,
                "Gemini did not respond within 45 seconds",
            )
            st.rerun()
        except APIError as error:
            if error.code == 504:
                append_timeout_fallback(
                    pending_message,
                    faqs,
                    conversation_id,
                    "Gemini timed out (504)",
                )
                st.rerun()
            else:
                if error.code == 404:
                    reason = "The selected Gemini model is unavailable (404)"
                elif error.code == 503:
                    reason = "Gemini is temporarily unavailable (503)"
                elif error.code == 429:
                    reason = "Gemini rate limit or quota was reached (429)"
                elif error.code in (401, 403):
                    reason = f"Gemini rejected the API key (HTTP {error.code})"
                else:
                    reason = (
                        f"Gemini request failed (HTTP {error.code})"
                        if error.code
                        else "Gemini request failed"
                    )
                append_service_fallback(
                    pending_message, conversation_id, reason
                )
                st.rerun()
        except HTTPError:
            append_service_fallback(
                pending_message,
                conversation_id,
                "Could not connect to Gemini",
            )
            st.rerun()
        except (json.JSONDecodeError, ValueError):
            append_service_fallback(
                pending_message,
                conversation_id,
                "Gemini returned an unusable response",
            )
            st.rerun()
        else:
            st.session_state.pop("pending_message", None)
            st.session_state.pop("pending_message_error", None)
            st.rerun()

    if st.session_state.get("pending_message_error"):
        with st.chat_message("user"):
            st.markdown(st.session_state.pending_message)
        st.error(st.session_state.pending_message_error)
        st.caption("Your question is kept in this chat session.")
        st.button("Retry", on_click=schedule_pending_message_retry)


def render_ticket_history() -> None:
    st.title("Ticket history")
    st.caption("Review customer requests, track progress, and update ownership.")
    tickets = read_tickets()
    all_tickets = tickets
    metric_columns = st.columns(4)
    metric_columns[0].metric("All tickets", len(all_tickets))
    metric_columns[1].metric(
        "Open", int((all_tickets["status"] == "Open").sum())
    )
    metric_columns[2].metric(
        "Escalated", int((all_tickets["status"] == "Escalated").sum())
    )
    metric_columns[3].metric(
        "Resolved", int((all_tickets["status"] == "Resolved").sum())
    )

    status_options = ["All"] + sorted(tickets["status"].dropna().unique().tolist())
    filter_column, search_column, export_column = st.columns(
        [1, 2, 1], vertical_alignment="bottom"
    )
    with filter_column:
        selected_status = st.selectbox("Status", status_options)
    if selected_status != "All":
        tickets = tickets[tickets["status"] == selected_status]
    with search_column:
        search = st.text_input(
            "Search tickets",
            placeholder="Ticket ID, customer message, or summary",
        )
    if search:
        search_match = (
            tickets["ticket_id"].str.contains(
                search, case=False, na=False, regex=False
            )
            | tickets["customer_message"].str.contains(
                search, case=False, na=False, regex=False
            )
            | tickets["summary"].str.contains(
                search, case=False, na=False, regex=False
            )
        )
        tickets = tickets[search_match]

    export_columns = [
        column for column in tickets.columns if column != "conversation_id"
    ]
    with export_column:
        st.download_button(
            "Export CSV",
            data=tickets[export_columns].to_csv(index=False).encode("utf-8"),
            file_name="shopease_tickets.csv",
            mime="text/csv",
            width="stretch",
        )
    if tickets.empty:
        st.info("No tickets match the selected filters. Try changing your search.")
        return

    st.dataframe(
        tickets[
            [
                "ticket_id",
                "created_at",
                "customer_message",
                "priority",
                "status",
            ]
        ].rename(
            columns={
                "ticket_id": "Ticket ID",
                "created_at": "Created",
                "customer_message": "Customer issue",
                "priority": "Priority",
                "status": "Status",
            }
        ),
        column_config={
            "Ticket ID": st.column_config.TextColumn(width="small"),
            "Created": st.column_config.TextColumn(width="small"),
            "Customer issue": st.column_config.TextColumn(width="large"),
            "Priority": st.column_config.TextColumn(width="small"),
            "Status": st.column_config.TextColumn(width="small"),
        },
        width="stretch",
        hide_index=True,
    )

    with st.expander("Ticket details and workflow"):
        selected_ticket_id = st.selectbox(
            "Ticket", tickets["ticket_id"].tolist()
        )
        ticket = tickets.loc[
            tickets["ticket_id"] == selected_ticket_id
        ].iloc[0]
        detail_column, response_column = st.columns(2)
        with detail_column:
            st.markdown("**Customer message**")
            st.write(ticket["customer_message"])
            st.markdown("**Issue summary**")
            st.write(ticket["summary"])
        with response_column:
            st.markdown("**Support response**")
            st.write(ticket["ai_response"])
            feedback_value = ticket["feedback"]
            if pd.isna(feedback_value):
                feedback_label = "Not provided"
            else:
                feedback_label = "Positive" if feedback_value == 1 else "Negative"
            st.caption(
                f"Category: {ticket['category']} · "
                f"Source: {ticket['response_source']} · "
                f"Sentiment: {ticket['sentiment']} · "
                f"Feedback: {feedback_label}"
            )
        st.divider()
        st.markdown("**Update ownership and status**")
        status_column, assignee_column = st.columns(2)
        with status_column:
            new_status = st.selectbox(
                "Status",
                STATUSES,
                index=STATUSES.index(ticket["status"]),
                key=f"ticket_status_{selected_ticket_id}",
            )
        with assignee_column:
            new_assignee = st.text_input(
                "Assignee",
                value=ticket["assignee"],
                key=f"ticket_assignee_{selected_ticket_id}",
            )
        if st.button(
            "Save ticket",
            key=f"save_ticket_{selected_ticket_id}",
            type="primary",
        ):
            update_ticket(selected_ticket_id, new_status, new_assignee)
            st.success("Ticket updated.")
            st.rerun()


def style_dashboard_figure(figure: Any) -> Any:
    figure.update_layout(
        template="plotly_white",
        font={"family": "Arial, sans-serif", "color": "#475569", "size": 12},
        margin={"l": 12, "r": 12, "t": 18, "b": 12},
        height=320,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
        },
    )
    figure.update_xaxes(showgrid=False, linecolor="#e2e8f0")
    figure.update_yaxes(gridcolor="#edf1f6", zeroline=False)
    return figure


def render_dashboard() -> None:
    st.title("Admin dashboard")
    st.caption("A clear view of support volume, service health, and ticket trends.")
    tickets = read_tickets()
    total = len(tickets)
    open_count = int((tickets["status"] == "Open").sum())
    pending_count = int((tickets["status"] == "Pending").sum())
    resolved_count = int((tickets["status"] == "Resolved").sum())
    escalated_count = int((tickets["status"] == "Escalated").sum())
    unresolved_count = open_count + pending_count + escalated_count
    reply_count = int(tickets["ai_response"].fillna("").str.strip().ne("").sum())
    reopened_count = int(tickets["reopened_count"].sum())
    resolved_tickets = tickets.dropna(subset=["resolved_at"])
    if resolved_tickets.empty:
        average_resolution = "No data"
    else:
        resolution_minutes = (
            pd.to_datetime(
                resolved_tickets["resolved_at"], format="mixed", utc=True
            )
            - pd.to_datetime(
                resolved_tickets["created_at"], format="mixed", utc=True
            )
        ).dt.total_seconds().div(60)
        average_minutes = max(0, float(resolution_minutes.mean()))
        average_resolution = (
            f"{int(average_minutes // 60)}h {int(average_minutes % 60)}m"
        )

    metrics = st.columns(4)
    metrics[0].metric("Total tickets", total)
    metrics[1].metric("Open", open_count)
    metrics[2].metric("Pending", pending_count)
    metrics[3].metric("Resolved", resolved_count)
    secondary_metrics = st.columns(5)
    secondary_metrics[0].metric("Replies recorded", reply_count)
    secondary_metrics[1].metric("Reopened", reopened_count)
    secondary_metrics[2].metric("Unresolved", unresolved_count)
    secondary_metrics[3].metric("Avg. resolution time", average_resolution)
    secondary_metrics[4].metric("Escalated", escalated_count)

    status_counts = (
        tickets["status"]
        .value_counts()
        .reindex(STATUSES, fill_value=0)
        .rename_axis("Status")
        .reset_index(name="Tickets")
    )
    priority_counts = (
        tickets["priority"]
        .value_counts()
        .reindex([*PRIORITIES, "Not analyzed"], fill_value=0)
        .rename_axis("Priority")
        .reset_index(name="Tickets")
    )
    daily_counts = (
        tickets.assign(
            created_day=pd.to_datetime(
                tickets["created_at"], format="mixed"
            ).dt.date
        )
        .groupby("created_day")
        .size()
        .reset_index(name="Tickets")
    )

    status_column, priority_column = st.columns(2)
    with status_column:
        with st.container(border=True):
            st.subheader("Tickets by status")
            status_figure = px.bar(
                status_counts,
                x="Status",
                y="Tickets",
                color="Status",
                color_discrete_map={
                    "Open": "#3b82f6",
                    "Pending": "#f59e0b",
                    "Resolved": "#10b981",
                    "Escalated": "#ef4444",
                },
            )
            status_figure.update_layout(showlegend=False)
            st.plotly_chart(
                style_dashboard_figure(status_figure),
                width="stretch",
                config={"displayModeBar": False},
            )
    with priority_column:
        with st.container(border=True):
            st.subheader("Tickets by priority")
            priority_figure = px.pie(
                priority_counts,
                names="Priority",
                values="Tickets",
                hole=0.62,
                color="Priority",
                color_discrete_map={
                    "Low": "#60a5fa",
                    "Medium": "#f59e0b",
                    "High": "#ef4444",
                    "Not analyzed": "#94a3b8",
                },
            )
            st.plotly_chart(
                style_dashboard_figure(priority_figure),
                width="stretch",
                config={"displayModeBar": False},
            )

    category_column, sentiment_column = st.columns(2)
    with category_column:
        with st.container(border=True):
            st.subheader("Tickets by category")
            category_counts = (
                tickets.groupby("category").size().reset_index(name="Tickets")
            )
            category_figure = px.bar(
                category_counts.sort_values("Tickets"),
                x="Tickets",
                y="category",
                orientation="h",
                color_discrete_sequence=["#3b82f6"],
            )
            category_figure.update_layout(showlegend=False)
            st.plotly_chart(
                style_dashboard_figure(category_figure),
                width="stretch",
                config={"displayModeBar": False},
            )
    with sentiment_column:
        with st.container(border=True):
            st.subheader("Tickets by sentiment")
            sentiment_counts = (
                tickets.groupby("sentiment").size().reset_index(name="Tickets")
            )
            sentiment_figure = px.pie(
                sentiment_counts,
                names="sentiment",
                values="Tickets",
                hole=0.62,
                color="sentiment",
                color_discrete_map={
                    "Positive": "#10b981",
                    "Neutral": "#94a3b8",
                    "Negative": "#f97316",
                    "Not analyzed": "#cbd5e1",
                },
            )
            st.plotly_chart(
                style_dashboard_figure(sentiment_figure),
                width="stretch",
                config={"displayModeBar": False},
            )

    with st.container(border=True):
        st.subheader("Ticket volume over time")
        trend_figure = px.line(
            daily_counts,
            x="created_day",
            y="Tickets",
            markers=True,
            color_discrete_sequence=["#2563eb"],
        )
        trend_figure.update_traces(
            line={"width": 3},
            marker={"size": 7, "color": "#2563eb"},
            fill="tozeroy",
            fillcolor="rgba(37, 99, 235, 0.08)",
        )
        st.plotly_chart(
            style_dashboard_figure(trend_figure),
            width="stretch",
            config={"displayModeBar": False},
        )


def main() -> None:
    st.set_page_config(page_title="SmartSupport AI", page_icon="💬", layout="wide")
    render_visual_theme()
    initialize_database()
    faqs = load_faqs()

    st.sidebar.title("SmartSupport AI")
    page = st.sidebar.radio(
        "Navigate", ("Chat", "Ticket history", "Admin dashboard")
    )
    default_key = os.getenv("GEMINI_API_KEY", "")
    api_key = st.sidebar.text_input(
        "Gemini API key",
        value=default_key,
        type="password",
        help="Used only for Gemini requests; it is not saved in the database.",
    ).strip()
    model = st.sidebar.text_input("Gemini model", value=DEFAULT_MODEL).strip()
    if not model:
        st.sidebar.error("Enter a Gemini model name.")
        return
    st.sidebar.caption("Customer messages are sent to Gemini when you submit them.")

    if page == "Chat":
        render_chat(api_key, model, faqs, get_conversation_id())
    elif page == "Ticket history":
        render_ticket_history()
    else:
        render_dashboard()


if __name__ == "__main__":
    main()
