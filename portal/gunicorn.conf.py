import os

_state = os.environ.get("STATE_DIR", "/var/lib/backup_sets")
bind = "0.0.0.0:" + os.environ.get("PORT", "8443")
# One process: in-memory login rate limiting and the background threads assume it.
workers = 1
worker_class = "gthread"
threads = 8
timeout = 120
accesslog = "-"
_cert, _key = f"{_state}/tls/cert.pem", f"{_state}/tls/key.pem"
if os.path.exists(_cert) and os.path.exists(_key):
    certfile, keyfile = _cert, _key
