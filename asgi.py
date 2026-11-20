"""Production entry point: the site plus the remote MCP endpoint at /mcp (see app/mcp_http.py).

Run with: uvicorn asgi:app
"""
from app import create_app
from app.mcp_http import create_asgi_app

app = create_asgi_app(create_app())
