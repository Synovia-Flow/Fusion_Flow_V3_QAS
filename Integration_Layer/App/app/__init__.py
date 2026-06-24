"""Minimal Fusion Flow V3 QAS application factory."""

from __future__ import annotations

from flask import Flask, jsonify


def create_app() -> Flask:
    """Create the minimal QAS Flask app.

    Keep this intentionally small while FLOW V3 services are rebuilt inside this
    repository. New blueprints should be added only when a local service exists.
    """
    app = Flask(__name__)

    @app.get("/health")
    def health() -> tuple[dict[str, str], int]:
        return jsonify({"status": "ok", "app": "Fusion_Flow_V3_QAS"}), 200

    return app
