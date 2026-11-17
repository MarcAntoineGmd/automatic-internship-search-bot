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
from .sectors import SECTORS, sector_label, sectors_for
from .tags import PROVINCE_STYLE, SEASON_STYLE, season_of, season_tag

COLUMNS = ["ID", "Score", "Priority", "Company", "Title", "Sector", "Location", "Province", "Term", "Type", "Arrangement",
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


def _row(j: JobRow, tz: str, include_status: bool = True, sectors: list[str] | None = None) -> list[str]:
    kind = {"co-op": "Co-op", "internship": "Internship", "student": "Student", "new_grad": "New grad"}.get(
        j.internship_type or "", "")
    sectors = sectors if sectors is not None else sectors_for(j.title, j.description, j.company)
    row = [str(j.id), str(j.score), j.priority, j.company, j.title,
           ", ".join(sector_label(k, emoji=False) for k in sectors), j.location or "",
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


def _term_key(term: str | None) -> str:
    return term.replace(" ", "-") if term else "NONE"


def _term_order(key: str) -> tuple:
    if key == "NONE":
        return (9999, 9)
    parts = key.split("-")
    year = int(parts[-1]) if parts[-1].isdigit() else 9998
    season = parts[0] if len(parts) > 1 else ""
    return (year, ["Winter", "Spring", "Summer", "Fall"].index(season) if season in SEASON_STYLE else 5)


def to_html(jobs: list[JobRow], tz: str = "America/Toronto", now: datetime | None = None,
            include_status: bool = True, stale_ids: set[int] | None = None,
            closed_ids: set[int] | None = None) -> bytes:
    """stale_ids: postings no source lists anymore (Database.stale_job_ids); closed_ids: those also confirmed gone
    from the company's own job board (bot/verify.py). Both are greyed out and hidden by default."""
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    stale_ids = stale_ids or set()
    closed_ids = (closed_ids or set()) & stale_ids
    head = "".join(f"<th onclick='sortBy({i})'>{html.escape(c)}</th>" for i, c in enumerate(_columns(include_status)[:-1]))
    body = []
    recent = now.astimezone(timezone.utc) - timedelta(hours=24)
    new_count = 0
    counts: dict[str, dict[str, int]] = {"status": {}, "prov": {}, "term": {}, "sector": {}}
    term_labels: dict[str, str] = {}

    def bump(group: str, key: str) -> None:
        counts[group][key] = counts[group].get(key, 0) + 1

    for j in jobs:
        sectors = sectors_for(j.title, j.description, j.company)
        cells = _row(j, tz, include_status, sectors)
        url = cells.pop()
        is_new = bool(j.date_discovered and j.date_discovered >= recent)
        stale = j.id in stale_ids
        status = "closed" if j.id in closed_ids else ("stale" if stale else "open")
        bump("status", status)
        new_count += is_new and not stale
        if status == "closed":
            badge = ("<span class='closed' title=\"Looked up on the company's job board: the posting is gone\">"
                     "❌ Fermé</span> ")
        elif stale:
            badge = ("<span class='closed' title='No source has listed this posting for 12 h or more, and the company "
                     "site could not confirm it'>⚠️ Peut-être fermé</span> ")
        else:
            badge = "<span class='badge'>NEW</span> " if is_new else ""
        cells[4] = badge + (f"<a href='{html.escape(url, quote=True)}' target='_blank' rel='noopener'>{html.escape(cells[4])}</a>"
                            if url else html.escape(cells[4]))
        cells[5] = " ".join(f"<span class='sector'>{html.escape(sector_label(k))}</span>" for k in sectors)
        provs = [p for p in j.provinces if p]
        prov_keys = [p for p in provs if p in ("QC", "ON")] or ["OTHER"]
        for k in prov_keys:
            bump("prov", k)
        term = _term_key(j.term)
        term_labels[term] = season_tag(j.term)
        bump("term", term)
        for k in sectors:
            bump("sector", k)
        season = season_of(j.term) or "NONE"
        cells[7] = " ".join(
            f"<span class='prov p-{html.escape(p)}'>{PROVINCE_STYLE.get(p, ('⚪', p, 0))[0]} {html.escape(p)}</span>"
            for p in provs[:3]) or html.escape(cells[7] or "—")
        cells[8] = f"<span class='season s-{season}'>{html.escape(season_tag(j.term))}</span>"
        tds = "".join(f"<td>{c}</td>" if i in (4, 5, 7, 8) else f"<td>{html.escape(c)}</td>" for i, c in enumerate(cells))
        body.append(f"<tr class='{html.escape(j.priority.lower())}{' stale' if stale else ''}' "
                    f"data-status='{status}' data-prov='{' '.join(prov_keys)}' "
                    f"data-term='{html.escape(term, quote=True)}' data-sector='{' '.join(sectors)}'>{tds}</tr>")

    def chip(group: str, value: str, label: str, n: int | None = None) -> str:
        count = f" ({n})" if n is not None else ""
        cls = "chip all" if value == "" else "chip"
        return (f"<button class='{cls}' data-group='{group}' data-value='{html.escape(value, quote=True)}' "
                f"onclick=\"toggle('{group}','{html.escape(value, quote=True)}')\">{html.escape(label)}{count}</button>")

    def row(group: str, title: str, all_label: str, items: list[tuple[str, str]]) -> str:
        return (f"<div class='chips'><span>{title} :</span>" + chip(group, "", all_label)
                + "".join(chip(group, k, label, counts[group].get(k, 0)) for k, label in items) + "</div>")

    terms = sorted(counts["term"], key=_term_order)
    chips = (
        row("status", "Statut", "Tous", [("open", "✅ Ouverts"), ("stale", "⚠️ Peut-être fermés"),
                                          ("closed", "❌ Fermés (vérifiés)")])
        + row("prov", "Province", "Toutes", [("QC", "🔵 Québec"), ("ON", "🔴 Ontario"), ("OTHER", "⚪ Ailleurs")])
        + row("term", "Session", "Toutes", [(t, term_labels[t]) for t in terms])
        + row("sector", "Secteur", "Tous", [(k, sector_label(k)) for k in SECTORS if counts["sector"].get(k)])
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Internship Radar export</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:16px;background:#fff;color:#1f2328}}
h1{{font-size:20px;margin:0 0 4px}} p{{margin:0 0 12px;color:#57606a}}
input{{padding:6px 10px;width:min(420px,100%);border:1px solid #d0d7de;border-radius:6px}}
.bar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:4px 0 12px}} #shown{{color:#57606a;font-size:13px}}
.reset{{border:1px solid #d0d7de;background:none;border-radius:6px;padding:5px 10px;cursor:pointer;color:inherit;font-size:13px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;font-size:13px;min-width:900px}}
th,td{{border-bottom:1px solid #d8dee4;padding:6px 8px;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#f6f8fa;cursor:pointer;white-space:nowrap}}
td:nth-child(5){{min-width:240px}} td:nth-child(6){{min-width:130px}} td:nth-child(7){{max-width:220px}}
td:nth-child(12),td:nth-child(13){{white-space:nowrap}}
tr.high td:nth-child(3){{color:#1a7f37;font-weight:600}} tr.medium td:nth-child(3){{color:#9a6700}}
tr.low td:nth-child(3){{color:#6e7781}} a{{color:#0969da}}
.badge{{background:#1a7f37;color:#fff;border-radius:4px;padding:0 5px;font-size:11px;font-weight:600}}
.closed{{background:#eaeef2;color:#57606a;border-radius:4px;padding:0 5px;font-size:11px;font-weight:600}}
tr.stale td{{opacity:.6}} tr.stale a{{text-decoration:line-through}}
.prov,.season,.sector{{display:inline-block;border-radius:10px;padding:1px 7px;font-size:12px;white-space:nowrap;margin:0 2px 2px 0;
background:#eaeef2;color:#1f2328}}
.sector{{background:#f3eefc;color:#5a32a3}}
.p-QC{{background:#ddf4ff;color:#0550ae}} .p-ON{{background:#ffebe9;color:#a40e26}}
.s-Winter{{background:#ddf4ff;color:#0550ae}} .s-Summer{{background:#fff8c5;color:#7d4e00}}
.s-Fall{{background:#fff1e5;color:#953800}} .s-Spring{{background:#dafbe1;color:#116329}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px}} .chips span{{color:#57606a;font-size:13px;min-width:70px}}
.chip{{border:1px solid #d0d7de;background:#f6f8fa;border-radius:16px;padding:4px 10px;font-size:13px;cursor:pointer;color:inherit}}
.chip.active{{background:#0969da;border-color:#0969da;color:#fff}}
@media (prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}} th{{background:#161b22}}
th,td{{border-color:#30363d}} a{{color:#4493f8}} input{{background:#0d1117;color:#e6edf3;border-color:#30363d}}
.chip,.reset{{background:#161b22;border-color:#30363d}} .prov,.season{{background:#30363d;color:#e6edf3}}
.sector{{background:#2b1f45;color:#d2b8ff}}
.p-QC,.s-Winter{{background:#0c2d6b;color:#a5d6ff}} .p-ON{{background:#5d0f12;color:#ffb3b3}}
.s-Summer{{background:#4b3b00;color:#f8e3a1}} .s-Fall{{background:#5a2a00;color:#ffc680}}}}
</style></head><body>
<h1>Canadian SWE Internship Radar — {len(jobs)} stored matches</h1>
<p>Updated {html.escape(now.strftime('%Y-%m-%d %H:%M'))} · {new_count} new in the last 24 h · {counts['status'].get('stale', 0) + counts['status'].get('closed', 0)} closed or possibly closed (hidden by default) · Click several buttons in a row to combine them (e.g. Hiver 2027 + Été 2027); click again to remove one.</p>
{chips}
<div class="bar"><input id="q" placeholder="Filter (company, title, city, term…)" oninput="apply()">
<button class="reset" onclick="reset()">Réinitialiser les filtres</button><span id="shown"></span></div>
<div class="wrap"><table id="t"><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<script>
const DEFAULT={{status:['open'],prov:[],term:[],sector:[]}};
const sel={{}};for(const g in DEFAULT)sel[g]=new Set(DEFAULT[g]);
function load(){{const h=new URLSearchParams(location.hash.slice(1));
for(const g in DEFAULT)if(h.has(g))sel[g]=new Set(h.get(g).split(',').filter(Boolean));
document.getElementById('q').value=h.get('q')||'';}}
function save(){{const h=new URLSearchParams();for(const g in sel)h.set(g,[...sel[g]].join(','));
const q=document.getElementById('q').value;if(q)h.set('q',q);history.replaceState(null,'','#'+h.toString());}}
function toggle(g,v){{if(v==='')sel[g].clear();else if(sel[g].has(v))sel[g].delete(v);else sel[g].add(v);apply();}}
function reset(){{for(const g in DEFAULT)sel[g]=new Set(DEFAULT[g]);document.getElementById('q').value='';apply();}}
function apply(){{for(const c of document.querySelectorAll('.chip')){{const s=sel[c.dataset.group],v=c.dataset.value;
c.classList.toggle('active',v===''?s.size===0:s.has(v));}}
const q=document.getElementById('q').value.toLowerCase();let n=0;
for(const r of document.querySelectorAll('#t tbody tr')){{let ok=r.textContent.toLowerCase().includes(q);
for(const g in sel)if(ok&&sel[g].size)ok=r.dataset[g].split(' ').some(x=>sel[g].has(x));
r.style.display=ok?'':'none';n+=ok;}}
document.getElementById('shown').textContent=n?n+' shown':'No posting matches these filters';save();}}
let dir=1,last=-1;function sortBy(i){{const tb=document.querySelector('#t tbody');const rows=[...tb.rows];
dir=(i===last)?-dir:(i===1?-1:1);last=i;const num=i<2;
rows.sort((a,b)=>{{const x=a.cells[i].textContent,y=b.cells[i].textContent;
return (num?(Number(x)-Number(y)):x.localeCompare(y))*dir;}});rows.forEach(r=>tb.appendChild(r));}}
load();apply();
</script></body></html>"""
    return page.encode("utf-8")


def write_html_export(jobs: list[JobRow], path: str | Path, tz: str = "America/Toronto",
                      now: datetime | None = None, stale_ids: set[int] | None = None,
                      closed_ids: set[int] | None = None) -> Path:
    """Write the HTML list atomically, so a browser refresh never sees a half-written file."""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(to_html(jobs, tz, now, stale_ids=stale_ids, closed_ids=closed_ids))
    os.replace(tmp, target)
    return target
