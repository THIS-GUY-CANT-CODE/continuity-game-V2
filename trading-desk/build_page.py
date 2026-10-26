"""Splice the latest brief into the plan page template -> page/desk.html"""
from pathlib import Path
HERE = Path(__file__).resolve().parent
tpl = (HERE / "page" / "template.html").read_text()
dec = (HERE / "state" / "latest_decision.html").read_text()
full = (HERE / "state" / "latest_brief.html").read_text()
out = tpl.replace("<!--DECISION-->", dec).replace("<!--BRIEF-->", full)
(HERE / "page" / "desk.html").write_text(out)
print("page/desk.html written", len(out), "bytes")
