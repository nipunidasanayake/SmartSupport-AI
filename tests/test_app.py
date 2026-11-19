import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from google.genai.errors import APIError
from httpx import HTTPError
from streamlit.testing.v1 import AppTest

import app


CHAT_SCRIPT = """
import app
app.render_chat(
    "test-key",
    "gemini-4-hydrogen",
    app.load_faqs(),
    "test-conversation",
)
"""

CHAT_WITHOUT_KEY_SCRIPT = """
import app
app.render_chat(
    "",
    "gemini-4-hydrogen",
    app.load_faqs(),
    "test-conversation",
)
"""


class SmartSupportAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "tickets.db")
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE tickets (
                    ticket_id TEXT PRIMARY KEY,
                    customer_message TEXT NOT NULL,
                    category TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    ai_response TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
        self.database_dir_patch = patch.object(
            app, "DATABASE_DIR", Path(self.temp_dir.name)
        )
        self.database_path_patch = patch.object(
            app, "DATABASE_PATH", self.database_path
        )
        self.database_dir_patch.start()
        self.database_path_patch.start()
        self.addCleanup(self.database_dir_patch.stop)
        self.addCleanup(self.database_path_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        app.initialize_database()

    def create_ticket(self, conversation_id: str = "test-conversation") -> str:
        return app.create_ticket(
            "I need help with my order.",
            {
                "answer": "Please check your tracking link.",
                "category": "Order",
                "sentiment": "Neutral",
                "priority": "Low",
                "summary": "Order tracking request",
                "escalate": False,
            },
            conversation_id=conversation_id,
        )

    def test_default_model_is_the_verified_model(self) -> None:
        self.assertEqual(app.DEFAULT_MODEL, "gemini-4-hydrogen")

    def test_existing_database_migrates_and_keeps_old_tickets(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(tickets)")
            }
            count = connection.execute(
                "SELECT COUNT(*) FROM tickets"
            ).fetchone()[0]
        self.assertTrue(
            {
                "response_source",
                "conversation_id",
                "feedback",
                "reopened_count",
                "resolved_at",
                "assignee",
                "channel",
            }.issubset(columns)
        )
        self.assertEqual(count, 50)

    def test_ticket_status_and_feedback_are_persisted(self) -> None:
        ticket_id = self.create_ticket()
        app.update_ticket(ticket_id, "Pending", "Alex")
        app.update_ticket(ticket_id, "Resolved", "Alex")
        app.update_ticket(ticket_id, "Open", "Alex")
        app.update_ticket_feedback(ticket_id, 1)

        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT status, assignee, reopened_count, resolved_at, feedback
                FROM tickets WHERE ticket_id = ?
                """,
                (ticket_id,),
            ).fetchone()
        self.assertEqual(row, ("Open", "Alex", 1, None, 1))

    def test_conversation_history_is_scoped_and_restored(self) -> None:
        ticket_id = self.create_ticket("thread-one")
        self.create_ticket("thread-two")
        app.update_ticket_feedback(ticket_id, 0)

        messages = app.read_conversation_messages("thread-one")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["ticket_id"], ticket_id)
        self.assertEqual(messages[1]["feedback"], 0)
        self.assertEqual(messages[1]["response_source"], "Gemini")

    def test_504_faq_fallback_creates_open_ticket(self) -> None:
        message = "I cannot log into my account after resetting my password."
        with (
            patch.object(app.genai, "Client", return_value=object()),
            patch.object(
                app,
                "analyze_inquiry",
                side_effect=APIError(504, {"message": "deadline expired"}),
            ),
        ):
            test_app = AppTest.from_string(CHAT_SCRIPT).run()
            test_app.chat_input[0].set_value(message).run()

        self.assertEqual(test_app.exception, [])
        self.assertEqual(len(test_app.feedback), 1)
        test_app.feedback[0].set_value(1).run()
        self.assertEqual(test_app.exception, [])
        self.assertEqual(len(test_app.feedback), 1)
        test_app.feedback[0].set_value(1).run()
        self.assertEqual(test_app.exception, [])
        messages = app.read_conversation_messages("test-conversation")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["response_source"], "FAQ fallback")
        self.assertEqual(messages[1]["analysis"]["category"], "Account")
        self.assertEqual(messages[1]["analysis"]["status"], "Open")
        self.assertEqual(
            messages[1]["analysis"]["sentiment"], "Not analyzed"
        )
        self.assertEqual(messages[1]["feedback"], 1)
        self.assertEqual(messages[1]["feedback"], 1)

    def test_new_chat_clears_pending_message_and_starts_new_conversation(
        self,
    ) -> None:
        test_app = AppTest.from_string(
            """
import app
app.render_chat(
    "test-key",
    "gemini-4-hydrogen",
    app.load_faqs(),
    app.get_conversation_id(),
)
"""
        ).run()
        previous_id = test_app.query_params.get("conversation_id")
        test_app.session_state["pending_message"] = "stuck request"
        test_app.session_state["pending_message_error"] = "request failed"
        test_app.session_state["process_pending_message"] = False

        test_app.button[0].click().run()

        self.assertNotEqual(
            test_app.query_params.get("conversation_id"), previous_id
        )
        self.assertNotIn("pending_message", test_app.session_state)
        self.assertNotIn("pending_message_error", test_app.session_state)
        self.assertNotIn("process_pending_message", test_app.session_state)

    def test_missing_api_key_saves_ticket_instead_of_disabling_chat(self) -> None:
        test_app = AppTest.from_string(CHAT_WITHOUT_KEY_SCRIPT).run()
        self.assertFalse(test_app.chat_input[0].disabled)

        test_app.chat_input[0].set_value(
            "qzxv blorptyz unknown problem"
        ).run()

        self.assertEqual(test_app.exception, [])
        messages = app.read_conversation_messages("test-conversation")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["response_source"], "Service fallback")
        self.assertIn("saved as an Open ticket", messages[1]["content"])
        self.assertEqual(messages[1]["analysis"]["status"], "Open")
        self.assertEqual(
            messages[1]["analysis"]["sentiment"], "Not analyzed"
        )

    def test_api_error_saves_ticket_and_releases_pending_message(self) -> None:
        message = "qzxv blorptyz unknown problem"
        with (
            patch.object(app.genai, "Client", return_value=object()),
            patch.object(
                app,
                "analyze_inquiry",
                side_effect=APIError(401, {"message": "unauthorized"}),
            ),
        ):
            test_app = AppTest.from_string(CHAT_SCRIPT).run()
            test_app.chat_input[0].set_value(message).run()

        self.assertEqual(test_app.exception, [])
        messages = app.read_conversation_messages("test-conversation")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["response_source"], "Service fallback")
        self.assertIn("rejected the API key", messages[1]["content"])
        self.assertEqual(messages[1]["analysis"]["status"], "Open")
        self.assertNotIn("pending_message", test_app.session_state)

    def test_network_error_saves_ticket(self) -> None:
        message = "qzxv blorptyz unknown problem"
        with (
            patch.object(app.genai, "Client", return_value=object()),
            patch.object(
                app,
                "analyze_inquiry",
                side_effect=HTTPError("connection failed"),
            ),
        ):
            test_app = AppTest.from_string(CHAT_SCRIPT).run()
            test_app.chat_input[0].set_value(message).run()

        self.assertEqual(test_app.exception, [])
        messages = app.read_conversation_messages("test-conversation")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["response_source"], "Service fallback")
        self.assertIn("Could not connect to Gemini", messages[1]["content"])

    def test_successful_gemini_response_is_saved_and_displayed(self) -> None:
        message = "Where is my order?"
        analysis = {
            "answer": "Use your tracking link to check its status.",
            "category": "Shipping",
            "sentiment": "Neutral",
            "priority": "Low",
            "summary": "Customer asks for an order update",
            "escalate": False,
        }
        with (
            patch.object(app.genai, "Client", return_value=object()),
            patch.object(app, "analyze_inquiry", return_value=analysis),
        ):
            test_app = AppTest.from_string(CHAT_SCRIPT).run()
            test_app.chat_input[0].set_value(message).run()

        self.assertEqual(test_app.exception, [])
        messages = app.read_conversation_messages("test-conversation")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], message)
        self.assertEqual(messages[1]["content"], analysis["answer"])
        self.assertEqual(messages[1]["analysis"]["status"], "Open")

    def test_ticket_history_and_dashboard_render(self) -> None:
        history_script = """
import app
app.render_ticket_history()
"""
        dashboard_script = """
import app
app.render_dashboard()
"""
        self.create_ticket()
        history_app = AppTest.from_string(history_script).run()
        dashboard_app = AppTest.from_string(dashboard_script).run()
        self.assertEqual(history_app.exception, [])
        self.assertEqual(dashboard_app.exception, [])


if __name__ == "__main__":
    unittest.main()
