"""Export every stored match as CSV (Excel/Sheets) or a self-contained HTML table."""
from __future__ import annotations

import csv
import html
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import JobRow
from .notify import PROVINCE_NAMES, WORKPLACE_LABEL
from .tags import PROVINCE_STYLE, SEASON_STYLE, season_of, season_tag

COLUMNS = ["ID", "Score", "Priority", "Company", "Title", "Location", "Province", "Term", "Type", "Arrangement",
           "Posted", "Discovered", "My status", "Source", "Apply URL"]


def _date(dt: datetime | None, tz: str) -> str:
    if not dt:
        return ""
    try:
        return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:
        return dt.strftime("%Y-%m-%d")


STATUS_COL = COLUMNS.index("My status")


def _columns(include_status: bool) -> list[str]:
    return COLUMNS if include_status else [c for i, c in enumerate(COLUMNS) if i != STATUS_COL]


def _row(j: JobRow, tz: str, include_status: bool = True) -> list[str]:
    kind = {"co-op": "Co-op", "internship": "Internship", "student": "Student", "new_grad": "New grad"}.get(
        j.internship_type or "", "")
    row = [str(j.id), str(j.score), j.priority, j.company, j.title, j.location or "",
            ", ".join(PROVINCE_NAMES.get(p, p) for p in j.provinces) or ("Canada" if j.country == "Canada" else ""),
            j.term or "", kind, WORKPLACE_LABEL.get(j.workplace or "", ""), _date(j.date_posted, tz),
            _date(j.date_discovered, tz), j.application_status, ", ".join([j.source] + j.also_seen_on), j.url or ""]
    return row if include_status else [c for i, c in enumerate(row) if i != STATUS_COL]


def to_csv(jobs: list[JobRow], tz: str = "America/Toronto", include_status: bool = True) -> bytes:
    """include_status=False drops your personal application status (for files shared with others)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_columns(include_status))
    for j in jobs:
        w.writerow(_row(j, tz, include_status))
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows accents (Montréal) correctly


def to_html(jobs: list[JobRow], tz: str = "America/Toronto", now: datetime | None = None,
            include_status: bool = True) -> bytes:
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    head = "".join(f"<th onclick='sortBy({i})'>{html.escape(c)}</th>" for i, c in enumerate(_columns(include_status)[:-1]))
    body = []
    recent = now.astimezone(timezone.utc) - timedelta(hours=24)
    new_count = 0
    counts = {"QC": 0, "ON": 0, "OTHER": 0, "Winter": 0, "Spring": 0, "Summer": 0, "Fall": 0, "NONE": 0}
    for j in jobs:
        cells = _row(j, tz, include_status)
        url = cells.pop()
        is_new = bool(j.date_discovered and j.date_discovered >= recent)
        new_count += is_new
        badge = "<span class='badge'>NEW</span> " if is_new else ""
        cells[4] = badge + (f"<a href='{html.escape(url, quote=True)}' target='_blank' rel='noopener'>{html.escape(cells[4])}</a>"
                            if url else html.escape(cells[4]))
        # Province + session badges (blue QC, red ON, …) and data attributes for the filter buttons.
        provs = [p for p in j.provinces if p]
        prov_keys = [p for p in provs if p in ("QC", "ON")] or ["OTHER"]
        for k in prov_keys:
            counts[k] += 1
        season = season_of(j.term) or "NONE"
        counts[season] += 1
        cells[6] = " ".join(
            f"<span class='prov p-{html.escape(p)}'>{PROVINCE_STYLE.get(p, ('⚪', p, 0))[0]} {html.escape(p)}</span>"
            for p in provs[:3]) or html.escape(cells[6] or "—")
        cells[7] = f"<span class='season s-{season}'>{html.escape(season_tag(j.term))}</span>"
        tds = "".join(f"<td>{c}</td>" if i in (4, 6, 7) else f"<td>{html.escape(c)}</td>" for i, c in enumerate(cells))
        body.append(f"<tr class='{html.escape(j.priority.lower())}' data-prov='{' '.join(prov_keys)}' "
                    f"data-season='{season}'>{tds}</tr>")
    def chip(group: str, value: str, label: str, n: int | None = None) -> str:
        count = f" ({n})" if n is not None else ""
        active = " active" if value == "" else ""
        return (f"<button class='chip{active}' data-group='{group}' data-value='{value}' "
                f"onclick=\"pick('{group}','{value}',this)\">{html.escape(label)}{count}</button>")
    chips = ("<div class='chips'><span>Province :</span>" + chip("prov", "", "Toutes")
             + chip("prov", "QC", "🔵 Québec", counts["QC"]) + chip("prov", "ON", "🔴 Ontario", counts["ON"])
             + chip("prov", "OTHER", "⚪ Ailleurs", counts["OTHER"]) + "</div>"
             + "<div class='chips'><span>Session :</span>" + chip("season", "", "Toutes")
             + "".join(chip("season", k, f"{SEASON_STYLE[k][0]} {SEASON_STYLE[k][1]}", counts[k])
                       for k in ("Winter", "Spring", "Summer", "Fall") if k != "Spring" or counts[k])
             + chip("season", "NONE", "❔ Non précisée", counts["NONE"]) + "</div>")
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
.badge{{background:#1a7f37;color:#fff;border-radius:4px;padding:0 5px;font-size:11px;font-weight:600}}
.prov,.season{{display:inline-block;border-radius:10px;padding:1px 7px;font-size:12px;white-space:nowrap;margin:0 2px 2px 0;
background:#eaeef2;color:#1f2328}}
.p-QC{{background:#ddf4ff;color:#0550ae}} .p-ON{{background:#ffebe9;color:#a40e26}}
.s-Winter{{background:#ddf4ff;color:#0550ae}} .s-Summer{{background:#fff8c5;color:#7d4e00}}
.s-Fall{{background:#fff1e5;color:#953800}} .s-Spring{{background:#dafbe1;color:#116329}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px}} .chips span{{color:#57606a;font-size:13px;min-width:70px}}
.chip{{border:1px solid #d0d7de;background:#f6f8fa;border-radius:16px;padding:4px 10px;font-size:13px;cursor:pointer;color:inherit}}
.chip.active{{background:#0969da;border-color:#0969da;color:#fff}}
@media (prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}} th{{background:#161b22}}
th,td{{border-color:#30363d}} a{{color:#4493f8}} input{{background:#0d1117;color:#e6edf3;border-color:#30363d}}
.chip{{background:#161b22;border-color:#30363d}} .prov,.season{{background:#30363d;color:#e6edf3}}
.p-QC,.s-Winter{{background:#0c2d6b;color:#a5d6ff}} .p-ON{{background:#5d0f12;color:#ffb3b3}}
.s-Summer{{background:#4b3b00;color:#f8e3a1}} .s-Fall{{background:#5a2a00;color:#ffc680}}}}
</style></head><body>
<h1>Canadian SWE Internship Radar — {len(jobs)} stored matches</h1>
<p>Updated {html.escape(now.strftime('%Y-%m-%d %H:%M'))} · {new_count} new in the last 24 h · Click a column to sort; type to filter.</p>
{chips}
<input id="q" placeholder="Filter (company, title, city, term…)" oninput="filter()">
<div class="wrap"><table id="t"><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<script>
const sel={{prov:'',season:''}};
function pick(g,v,b){{sel[g]=v;for(const c of document.querySelectorAll(`.chip[data-group="${{g}}"]`))c.classList.toggle('active',c===b);filter();}}
function filter(){{const q=document.getElementById('q').value.toLowerCase();
for(const r of document.querySelectorAll('#t tbody tr')){{
const ok=r.textContent.toLowerCase().includes(q)&&(!sel.prov||r.dataset.prov.split(' ').includes(sel.prov))&&(!sel.season||r.dataset.season===sel.season);
r.style.display=ok?'':'none';}}}}
let dir=1,last=-1;function sortBy(i){{const tb=document.querySelector('#t tbody');const rows=[...tb.rows];
dir=(i===last)?-dir:(i===1?-1:1);last=i;const num=i<2;
rows.sort((a,b)=>{{const x=a.cells[i].textContent,y=b.cells[i].textContent;
return (num?(Number(x)-Number(y)):x.localeCompare(y))*dir;}});rows.forEach(r=>tb.appendChild(r));}}
</script></body></html>"""
    return page.encode("utf-8")


def write_html_export(jobs: list[JobRow], path: str | Path, tz: str = "America/Toronto",
                      now: datetime | None = None) -> Path:
    """Write the HTML list atomically, so a browser refresh never sees a half-written file."""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(to_html(jobs, tz, now))
    os.replace(tmp, target)
    return target
