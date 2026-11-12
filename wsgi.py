"""WSGI entry point.

Production (single worker - the poller owns the serial port):
    gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 wsgi:app
Development:
    python wsgi.py
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)
