"""The notebook plugin (plugins/notebook): the file it keeps, a line from outside the book,
yesterday's copy, and what it refuses to save."""

import importlib.util
import json
import os
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _plugin():
    spec = importlib.util.spec_from_file_location("notebook_plugin_under_test", os.path.join(HERE, "plugins", "notebook", "plugin.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    class Ctx:
        data_dir = tempfile.mkdtemp()
    p = m.Plugin()
    p.start(Ctx())
    return m, p


def test_empty_then_saved_and_read_back():
    _, p = _plugin()
    assert p.read() is None
    assert p.routes()[("GET", "/state")]({}) == ({"state": None}, 200)
    state = {"v": 1, "skin": "spiral", "pages": [{"date": "Mon", "entries": [{"id": "a", "text": "نان بخر", "done": False}], "strokes": [], "stickers": []}]}
    assert p.routes()[("POST", "/state")]({"body": {"state": state}}) == ({"ok": True, "rev": 1}, 200)
    assert p.read() == {**state, "rev": 1}
    assert oct(os.stat(p.path).st_mode & 0o777) == "0o600"
    assert "نان بخر" in open(p.path, encoding="utf-8").read()          # kept readable, not \\u-escaped


def test_refuses_what_is_not_a_notebook():
    _, p = _plugin()
    for bad in (None, [], {"pages": "x"}, {"pages": [1]}):
        body, code = p.routes()[("POST", "/state")]({"body": {"state": bad}})
        assert code == 400 and "error" in body
    assert p.read() is None


def test_a_line_from_outside_lands_on_the_last_written_page_with_its_project():
    m, p = _plugin()
    got = p.add_line("  fix the   sync  #Flix ")
    assert got["page"] == 1 and got["entry"]["text"] == "fix the sync" and got["entry"]["project"] == "flix"
    s = p.read()
    assert len(s["pages"]) % 2 == 0 and len(s["pages"]) >= 5
    assert s["pages"][0]["date"] and s["pages"][0]["entries"][0]["done"] is False
    for i in range(m.LINES_A_PAGE):
        p.now = lambda i=i: 1_800_000_000 + i
        p.add_line(f"line {i}")
    assert p.add_line("به سجاد پیام بده #pixie")["page"] == 2      # the first page is full: the next one
    assert p.read()["pages"][1]["entries"][-1]["project"] == "pixie"
    assert m.counts(p.read()) == (m.LINES_A_PAGE + 2, 0)


def test_note_on_telegram_and_status():
    _, p = _plugin()
    note = p.telegram()["/note"]
    assert "0 open" in note(1, "")
    assert note(1, "buy milk") == "Written on page 1."
    assert note(1, "#home water the plants") == "Written on page 1 under home."
    s = p.read(); s["pages"][0]["entries"][0]["done"] = True; p.write(s)
    assert p.status() == {"line": "1 open · 1 crossed out", "ok": True}
    body, code = p.routes()[("POST", "/line")]({"body": {"text": "   "}})
    assert code == 400


def test_the_first_write_of_a_day_keeps_the_file_as_it_stood():
    m, p = _plugin()
    p.now = lambda: 1_800_000_000
    p.write({"pages": [{"entries": [{"id": "a", "text": "one"}]}]})
    kept = os.path.join(os.path.dirname(p.path), "kept")
    assert not os.path.isdir(kept) or not os.listdir(kept)                  # nothing stood before the first write
    p.write({"pages": [{"entries": [{"id": "a", "text": "two"}]}]})
    assert len(os.listdir(kept)) == 1
    assert json.load(open(os.path.join(kept, os.listdir(kept)[0])))["pages"][0]["entries"][0]["text"] == "one"
    p.write({"pages": [{"entries": [{"id": "a", "text": "three"}]}]})
    assert json.load(open(os.path.join(kept, os.listdir(kept)[0])))["pages"][0]["entries"][0]["text"] == "one"   # once a day
    for d in range(1, m.KEEP_DAYS + 6):
        p.now = lambda d=d: 1_800_000_000 + d * 86400
        p.write({"pages": [{"entries": [{"id": "a", "text": f"day {d}"}]}]})
    assert len(os.listdir(kept)) == m.KEEP_DAYS


def test_a_window_that_is_behind_gets_the_file_back_instead_of_overwriting_it():
    _, p = _plugin()
    save = p.routes()[("POST", "/state")]
    mine = {"pages": [{"entries": [{"id": "a", "text": "mine"}]}]}
    assert save({"body": {"state": mine, "rev": 0}}) == ({"ok": True, "rev": 1}, 200)
    p.add_line("from telegram")                                     # the file moves on: rev 2
    body, code = save({"body": {"state": {"pages": [{"entries": [{"id": "a", "text": "mine, edited"}]}]}, "rev": 1}})
    assert code == 409 and body["rev"] == 2
    assert [e["text"] for e in body["state"]["pages"][0]["entries"]] == ["mine", "from telegram"]
    assert [e["text"] for e in p.read()["pages"][0]["entries"]] == ["mine", "from telegram"]     # nothing was written
    merged = {"pages": [{"entries": [{"id": "a", "text": "mine, edited"}, body["state"]["pages"][0]["entries"][1]]}]}
    assert save({"body": {"state": merged, "rev": 2}}) == ({"ok": True, "rev": 3}, 200)
    assert save({"body": {"state": merged}})[1] == 200               # no rev: an older window still saves
