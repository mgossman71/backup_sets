"""Serve the portal against a fake host (for real-browser tests). Usage: serve_fake.py PORT"""
import os, pathlib, sys, tempfile
ROOT = pathlib.Path(tempfile.mkdtemp(prefix="browser-"))
os.environ.update({"HOST_ROOT": str(ROOT / "host"), "STATE_DIR": str(ROOT / "state"),
                   "CONFIG_FILE": str(ROOT / "opt/config.yaml"), "PORTAL_BACKGROUND": "0", "BROWSE_ROOTS": "/mnt"})
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "portal"))
sys.path.insert(0, str(HERE.parent / "portal"))
import fakehost  # noqa: E402
fakehost.build(ROOT / "host")
for i in range(200):  # enough shows that the picker list has to scroll
    fakehost.write_tree(ROOT / f"host/mnt/src/TV/Show {i:03d}", {"e1.mkv": b"s" * (i + 1)})
(ROOT / "opt").mkdir(parents=True)
(ROOT / "opt/config.yaml").write_text(fakehost.CONFIG_TEMPLATE)
(ROOT / "state").mkdir()
from app import create_app  # noqa: E402
create_app().run(host="127.0.0.1", port=int(sys.argv[1]), threaded=True)
