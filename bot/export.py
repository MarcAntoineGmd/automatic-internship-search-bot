"""Export every stored match as CSV (Excel/Sheets) or a self-contained HTML table."""
from __future__ import annotations

import csv
import html
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import JobRow
from .notify import PROVINCE_NAMES, WORKPLACE_LABEL

COLUMNS = ["ID", "Score", "Priority", "Company", "Title", "Location", "Province", "Term", "Type", "Arrangement",
           "Posted", "Discovered", "My status", "Source", "Apply URL"]


def _date(dt: datetime | None, tz: str) -> str:
    if not dt:
        return ""
    try:
        return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:
        return dt.strftime("%Y-%m-%d")


def _row(j: JobRow, tz: str) -> list[str]:
    kind = {"co-op": "Co-op", "internship": "Internship", "student": "Student", "new_grad": "New grad"}.get(
        j.internship_type or "", "")
    return [str(j.id), str(j.score), j.priority, j.company, j.title, j.location or "",
            ", ".join(PROVINCE_NAMES.get(p, p) for p in j.provinces) or ("Canada" if j.country == "Canada" else ""),
            j.term or "", kind, WORKPLACE_LABEL.get(j.workplace or "", ""), _date(j.date_posted, tz),
            _date(j.date_discovered, tz), j.application_status, ", ".join([j.source] + j.also_seen_on), j.url or ""]


def to_csv(jobs: list[JobRow], tz: str = "America/Toronto") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for j in jobs:
        w.writerow(_row(j, tz))
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows accents (Montréal) correctly


def to_html(jobs: list[JobRow], tz: str = "America/Toronto", now: datetime | None = None) -> bytes:
    now = now or datetime.now(ZoneInfo(tz))
    head = "".join(f"<th onclick='sortBy({i})'>{html.escape(c)}</th>" for i, c in enumerate(COLUMNS[:-1]))
    body = []
    for j in jobs:
        cells = _row(j, tz)
        url = cells.pop()
        cells[4] = f"<a href='{html.escape(url, quote=True)}' target='_blank' rel='noopener'>{html.escape(cells[4])}</a>" \
            if url else html.escape(cells[4])
        tds = "".join(f"<td>{c}</td>" if i == 4 else f"<td>{html.escape(c)}</td>" for i, c in enumerate(cells))
        body.append(f"<tr class='{html.escape(j.priority.lower())}'>{tds}</tr>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Internship Radar export</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:16px;background:#fff;color:#1f2328}}
h1{{font-size:20px;margin:0 0 4px}} p{{margin:0 0 12px;color:#57606a}}
input{{padding:6px 10px;width:min(420px,100%);margin-bottom:12px;border:1px solid #d0d7de;border-radius:6px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;font-size:13px;min-width:900px}}
th,td{{border-bottom:1px solid #d8dee4;padding:6px 8px;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#f6f8fa;cursor:pointer;white-space:nowrap}}
td:nth-child(5){{min-width:240px}} td:nth-child(6){{max-width:220px}} td:nth-child(11),td:nth-child(12){{white-space:nowrap}}
tr.high td:nth-child(3){{color:#1a7f37;font-weight:600}} tr.medium td:nth-child(3){{color:#9a6700}}
tr.low td:nth-child(3){{color:#6e7781}} a{{color:#0969da}}
@media (prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}} th{{background:#161b22}}
th,td{{border-color:#30363d}} a{{color:#4493f8}} input{{background:#0d1117;color:#e6edf3;border-color:#30363d}}}}
</style></head><body>
<h1>Canadian SWE Internship Radar — {len(jobs)} stored matches</h1>
<p>Exported {html.escape(now.strftime('%Y-%m-%d %H:%M'))}. Click a column to sort; type to filter.</p>
<input id="q" placeholder="Filter (company, title, city, term…)" oninput="filter()">
<div class="wrap"><table id="t"><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<script>
function filter(){{const q=document.getElementById('q').value.toLowerCase();
for(const r of document.querySelectorAll('#t tbody tr'))r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';}}
let dir=1,last=-1;function sortBy(i){{const tb=document.querySelector('#t tbody');const rows=[...tb.rows];
dir=(i===last)?-dir:(i===1?-1:1);last=i;const num=i<2;
rows.sort((a,b)=>{{const x=a.cells[i].textContent,y=b.cells[i].textContent;
return (num?(Number(x)-Number(y)):x.localeCompare(y))*dir;}});rows.forEach(r=>tb.appendChild(r));}}
</script></body></html>"""
    return page.encode("utf-8")
