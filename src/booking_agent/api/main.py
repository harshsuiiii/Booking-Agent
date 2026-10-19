"""ASGI entry point for uvicorn."""

from booking_agent.dotenv import load_dotenv

load_dotenv()

from booking_agent.api.app import create_app  # noqa: E402

app = create_app()
