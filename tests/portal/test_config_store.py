"""config.yaml: reading, validation, and writing (comments kept, atomic, backup copies)."""
import os

import pytest

import fakehost
from app import config_store as cs
from conftest import CONFIG, HOST


def save(changes=None, force=True, version=None, **top):
    cfg = cs.load()
    backups = [dict(b) for b in cfg["backups"]]
    for i, ch in (changes or {}).items():
        backups[i].update(ch)
    body = {"backups": backups, "required_mount_points": cfg["required_mount_points"],
            "rsync_opts": cfg["rsync_opts"]}
    body.update(top)
    return cs.save(body, version or cfg["version"], user="tester", force=force)


def validate(backups, mounts=("/mnt/disk1", "/mnt/disk2"), opts="-av --stats"):
    return cs.validate(cs.normalize_backups(backups), list(mounts), opts)


B = lambda src="/mnt/src/TV", dst="/mnt/disk1", **kw: dict(source=src, destination=dst, **kw)  # noqa: E731


# --------------------------------------------------------------------------- load
def test_load(host):
    cfg = cs.load()
    assert cfg["rsync_opts"] == "-av --stats"
    assert cfg["required_mount_points"] == ["/mnt/disk1", "/mnt/disk2"]
    assert cfg["backups"][0] == {"source": "/mnt/src/TV", "destination": "/mnt/disk1", "mode": "all",
                                 "folders": [], "include_root_files": True}
    assert len(cfg["version"]) == 16


def test_load_selection_modes(host):
    CONFIG.write_text(CONFIG.read_text().replace(
        "    destination: /mnt/disk1\n",
        "    destination: /mnt/disk1\n    exclude:\n      - Big Show\n", 1).replace(
        "    destination: /mnt/disk2\n",
        "    destination: /mnt/disk2\n    include: [Film A]\n    include_root_files: false\n", 1))
    tv, movies = cs.load()["backups"]
    assert (tv["mode"], tv["folders"]) == ("exclude", ["Big Show"])
    assert (movies["mode"], movies["folders"], movies["include_root_files"]) == ("include", ["Film A"], False)


def test_both_lists_is_reported_invalid(host):
    CONFIG.write_text(CONFIG.read_text().replace(
        "    destination: /mnt/disk1\n", "    destination: /mnt/disk1\n    include: [a]\n    exclude: [b]\n", 1))
    b = cs.load()["backups"][0]
    assert b["mode"] == "both"
    errors, _ = cs.validate([b], ["/mnt/disk1"], "-a")
    assert any("both include and exclude" in e for e in errors)


# --------------------------------------------------------------------------- save
def test_unchanged_save_keeps_file_byte_for_byte(host):
    before = CONFIG.read_text()
    save()
    assert CONFIG.read_text() == before


def test_save_selection_keeps_comments_and_layout(host):
    before = CONFIG.read_text()
    save({0: {"mode": "exclude", "folders": ["Big Show", "Show [x]"]}})
    after = CONFIG.read_text()
    expected = before.replace(
        "    destination: /mnt/disk1\n",
        "    destination: /mnt/disk1\n    exclude:\n      - Big Show\n      - Show [x]\n", 1)
    assert after == expected
    assert cs.load()["backups"][0]["folders"] == ["Big Show", "Show [x]"]


def test_selection_on_last_entry_keeps_trailing_comment_block(host):
    save({1: {"mode": "include", "folders": ["Film A"], "include_root_files": False}})
    text = CONFIG.read_text()
    assert text.endswith(
        "    destination: /mnt/disk2\n    include:\n      - Film A\n    include_root_files: false\n\n"
        "# To add new backups from different sources, add more entries:\n"
        "#  - source: /mnt/OtherVol/Photos\n#    destination: /mnt/usb_chassis/disk5\n")


def test_clearing_selection_restores_original(host):
    before = CONFIG.read_text()
    save({0: {"mode": "exclude", "folders": ["Big Show"]}, 1: {"mode": "include", "folders": ["Film A"]}})
    save({0: {"mode": "all", "folders": []}, 1: {"mode": "all", "folders": []}})
    assert CONFIG.read_text() == before


def test_save_add_and_reorder_entries(host):
    cfg = cs.load()
    new = [cfg["backups"][1], cfg["backups"][0], B("/mnt/src/Movies", "/mnt/disk1")]
    cs.save({"backups": new, "required_mount_points": cfg["required_mount_points"],
             "rsync_opts": cfg["rsync_opts"]}, cfg["version"], force=True)
    assert [(b["source"], b["destination"]) for b in cs.load()["backups"]] == [
        ("/mnt/src/Movies", "/mnt/disk2"), ("/mnt/src/TV", "/mnt/disk1"), ("/mnt/src/Movies", "/mnt/disk1")]
    assert "# To add new backups" in CONFIG.read_text()


def test_save_mount_points_and_rsync_opts(host):
    save(required_mount_points=["/mnt/disk1"], rsync_opts="  -a   --stats ")
    cfg = cs.load()
    assert cfg["required_mount_points"] == ["/mnt/disk1"]
    assert cfg["rsync_opts"] == "-a --stats"


def test_save_is_atomic_and_keeps_permissions(host):
    os.chmod(CONFIG, 0o640)
    save({0: {"mode": "exclude", "folders": ["Big Show"]}})
    assert oct(os.stat(CONFIG).st_mode & 0o777) == "0o640"
    assert not (CONFIG.parent / "config.yaml.tmp").exists()


def test_each_save_keeps_a_copy_of_the_previous_file(host):
    before = CONFIG.read_text()
    save({0: {"mode": "exclude", "folders": ["Big Show"]}})
    copies = cs.list_backup_copies()
    assert len(copies) == 1 and copies[0].endswith(".tester")
    assert (CONFIG.parent / "config_backups" / copies[0]).read_text() == before


def test_backup_copies_are_capped(host, monkeypatch):
    d = CONFIG.parent / "config_backups"
    d.mkdir()
    for i in range(40):
        (d / f"config.yaml.2026010{i:02d}-000000.old").write_text("x")
    save()
    assert len(os.listdir(d)) == cs.KEEP_COPIES


def test_conflict_when_file_changed_since_load(host):
    cfg = cs.load()
    CONFIG.write_text(CONFIG.read_text() + "\n# edited by hand\n")
    with pytest.raises(cs.ConflictError):
        cs.save({"backups": cfg["backups"], "required_mount_points": cfg["required_mount_points"],
                 "rsync_opts": cfg["rsync_opts"]}, cfg["version"])
    assert CONFIG.read_text().endswith("# edited by hand\n")


def test_warnings_need_force(host):
    with pytest.raises(cs.ValidationError) as e:
        save(force=False, rsync_opts="-a --stats --delete")
    assert e.value.errors == [] and e.value.warnings
    assert cs.load()["rsync_opts"] == "-av --stats"
    save(force=True, rsync_opts="-a --stats --delete")
    assert cs.load()["rsync_opts"] == "-a --stats --delete"


def test_errors_block_save_even_with_force(host):
    before = CONFIG.read_text()
    with pytest.raises(cs.ValidationError):
        save({0: {"source": "/mnt/src/Nope"}}, force=True)
    assert CONFIG.read_text() == before
    assert cs.list_backup_copies() == []


# --------------------------------------------------------------------------- validation
def test_valid_config_has_no_errors(host):
    assert validate([B(), B("/mnt/src/Movies", "/mnt/disk2")]) == ([], [])


@pytest.mark.parametrize("backups,fragment", [
    ([B("relative/TV")], "absolute paths"),
    ([B(dst="disk1")], "absolute paths"),
    ([B("/")], "source cannot be /"),
    ([B("/mnt/src/Nope")], "source does not exist"),
    ([B(dst="/mnt/nope")], "destination does not exist"),
    ([B("/mnt/src/TV", "/mnt/src/TV")], "overlaps the source"),
    ([B("/mnt/src", "/mnt/src/TV")], "overlaps the source"),
    ([B("/mnt/src/TV", "/mnt/disk1"), B("/mnt/src/TV", "/mnt/disk1")], "same folder as backup #1"),
    ([B(mode="include", folders=[])], "no folders are selected"),
    ([B(mode="exclude", folders=["../etc"])], "invalid folder name"),
    ([B(mode="exclude", folders=["a/b"])], "invalid folder name"),
    ([B(mode="exclude", folders=[".."])], "invalid folder name"),
    ([B(mode="include", folders=["bad\nname"])], "invalid folder name"),
    ([B(mode="weird", folders=["x"])], "unknown folder selection mode"),
])
def test_validation_errors(host, backups, fragment):
    errors, _ = validate(backups)
    assert any(fragment in e for e in errors), errors


def test_mount_point_must_be_absolute(host):
    errors, _ = validate([B()], mounts=["mnt/disk1"])
    assert any("absolute path" in e for e in errors)


def test_unmounted_mount_point_is_a_warning(host):
    errors, warnings = validate([B()], mounts=["/mnt/disk1", "/mnt/unmounted"])
    assert errors == [] and any("/mnt/unmounted is not currently mounted" in w for w in warnings)


def test_destination_outside_required_mounts_warns(host):
    _, warnings = validate([B(dst="/mnt/unmounted")])
    assert any("not under a required mount point" in w for w in warnings)


def test_include_of_missing_folder_warns(host):
    errors, warnings = validate([B(mode="include", folders=["Big Show", "Not There"])])
    assert errors == [] and any("Not There" in w for w in warnings)


def test_exclude_of_missing_folder_is_fine(host):
    assert validate([B(mode="exclude", folders=["Long Gone"])]) == ([], [])


@pytest.mark.parametrize("opts", ["-a -e ssh", "-a --rsh=ssh", "-a --rsync-path=/tmp/x", "-a -M--fake",
                                  "-a --files-from=/etc/passwd", "-a --log-file=/etc/cron.d/x", "-a somehost:/x",
                                  "-a 'unclosed",
                                  # clustered / attached short options (regression: these used to pass)
                                  "-avessh", "-ave", "-avMfoo", "-essh",
                                  "-a --remove-source-files", "-a --exclude-from=/etc/shadow"])
def test_dangerous_rsync_options_rejected(host, opts):
    errors, _ = validate([B()], opts=opts)
    assert errors, opts


@pytest.mark.parametrize("opts,fragment", [("-a --delete", "--delete"), ("-v --stats", "archive mode"),
                                           ("-a --delete-excluded", "deselected folders will be deleted")])
def test_rsync_option_warnings(host, opts, fragment):
    errors, warnings = validate([B()], opts=opts)
    assert errors == [] and any(fragment in w for w in warnings)


def test_normalize_backups():
    out = cs.normalize_backups([
        {"source": " /mnt/src/TV/ ", "destination": "/mnt//disk1", "mode": "exclude", "folders": ["a", "a", "b"]},
        {"source": "/mnt/src/TV", "destination": "/mnt/disk1", "mode": "exclude", "folders": []},
        {"source": "/mnt/src/TV", "destination": "/mnt/disk1", "mode": "all", "folders": ["ignored"]},
    ])
    assert out[0] == {"source": "/mnt/src/TV", "destination": "/mnt/disk1", "mode": "exclude",
                      "folders": ["a", "b"], "include_root_files": True}
    assert out[1]["mode"] == "all"
    assert out[2]["folders"] == []
