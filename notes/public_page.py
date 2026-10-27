"""
The page students see. Plain HTML written here (no React app, no script files, no API address), so nothing on it shows
what system is behind it. It carries the college name, the class and its notes, and nothing else.
"""
import secrets
from html import escape

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from . import files

# the style sheet of the page, written inline so the page needs no other files
CSS = """
:root{--ink:#14262b;--muted:#5f7378;--line:#e1e8ea;--bg:#f3f6f7;--card:#fff;--brand:#0f5c73;--brand2:#14869f;--soft:#e3f1f5;--shadow:0 1px 2px rgba(16,40,48,.06),0 6px 18px rgba(16,40,48,.07);--chip:#fff}
@media(prefers-color-scheme:dark){:root{--ink:#e7f0f2;--muted:#9db3b8;--line:#243b41;--bg:#0c171a;--card:#13242a;--brand:#5fc0d8;--brand2:#2d8ea8;--soft:#183139;--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 18px rgba(0,0,0,.25);--chip:#13242a}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",sans-serif}
.wrap{max-width:44rem;margin:0 auto;padding:0 1rem}
.hero{background:linear-gradient(135deg,#0b4a5e 0%,#0f6f89 55%,#16a0b5 100%);color:#fff;padding:1.2rem 0 4.2rem;border-radius:0 0 28px 28px;position:relative;overflow:hidden}
@media(prefers-color-scheme:dark){.hero{background:linear-gradient(135deg,#06222b 0%,#0b3d4d 60%,#0f5a70 100%)}}
.hero:before,.hero:after{content:"";position:absolute;border-radius:50%;background:rgba(255,255,255,.07)}
.hero:before{width:15rem;height:15rem;right:-5rem;top:-6rem}.hero:after{width:9rem;height:9rem;right:3rem;bottom:-5rem}
.hero .wrap{position:relative;z-index:1}
.college{font-weight:700;font-size:.82rem;letter-spacing:.06em;text-transform:uppercase;opacity:.92}
.addr{font-size:.76rem;opacity:.7;margin-top:.1rem}
h1{font-size:1.8rem;line-height:1.2;font-weight:800;margin:1.1rem 0 .3rem;letter-spacing:-.01em}
.sub{opacity:.88;font-size:.95rem}
.stats{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.9rem}
.stats span{background:rgba(255,255,255,.16);border-radius:99px;padding:.22rem .75rem;font-size:.8rem;font-weight:600;backdrop-filter:blur(4px)}
.timer{margin:-2.7rem auto 0;max-width:44rem;padding:0 1rem;position:relative;z-index:2}
.timer .box{background:var(--card);border-radius:16px;padding:.8rem 1rem;box-shadow:var(--shadow);display:flex;gap:.8rem;align-items:center;border:1px solid var(--line)}
.clock{flex:none;width:2.6rem;height:2.6rem;border-radius:12px;background:var(--soft);color:var(--brand);display:grid;place-items:center}
.timer .t1{font-size:.78rem;color:var(--muted)}
.timer .t2{font-weight:700;font-variant-numeric:tabular-nums;font-size:1.02rem}
.tools{margin:1rem 0 .2rem;display:grid;gap:.6rem}
.sbox{position:relative}.sbox svg{position:absolute;left:.85rem;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.search{width:100%;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:12px;padding:.7rem .9rem .7rem 2.5rem;font:inherit;box-shadow:var(--shadow)}
.search:focus{outline:2px solid var(--brand2);outline-offset:1px}
.chips{display:flex;gap:.45rem;overflow-x:auto;padding:.1rem .05rem .4rem;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;border:1px solid var(--line);background:var(--chip);color:var(--ink);padding:.32rem .8rem;border-radius:99px;font:600 .82rem/1.2 inherit;text-decoration:none;cursor:pointer}
.chip.on{background:var(--brand);border-color:var(--brand);color:#fff}
@media(prefers-color-scheme:dark){.chip.on{color:#06222b}}
h2{display:flex;align-items:center;gap:.5rem;font-size:.82rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:1.5rem 0 .6rem}
h2:after{content:"";flex:1;height:1px;background:var(--line)}
h2 small{font-weight:700;background:var(--soft);color:var(--brand);border-radius:99px;padding:.05rem .5rem;letter-spacing:0}
.note{display:flex;gap:.85rem;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:.9rem;margin:0 0 .7rem;box-shadow:var(--shadow)}
.ft{flex:none;width:3rem;height:3.4rem;border-radius:10px;display:grid;place-items:center;font-weight:800;font-size:.66rem;letter-spacing:0;color:#fff;background:#64808a}
.ft.pdf{background:#d9443b}.ft.doc{background:#2b6fd4}.ft.xls{background:#1f9254}.ft.ppt{background:#e0742a}.ft.img{background:#8b5cc6}.ft.txt{background:#64808a}.ft.memo{background:linear-gradient(135deg,#0f6f89,#16a0b5)}
.main{min-width:0;flex:1}
.note h3{margin:0;font-size:1.04rem;line-height:1.35;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:.78rem;margin-top:.15rem}
.body{margin:.5rem 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font-size:.95rem}
.dl{display:inline-flex;align-items:center;gap:.5rem;margin-top:.7rem;padding:.55rem .95rem;border-radius:10px;background:var(--brand);color:#fff;text-decoration:none;font-weight:700;font-size:.9rem;min-height:44px;max-width:100%;transition:transform .12s,filter .12s}
@media(prefers-color-scheme:dark){.dl{color:#06222b}}
.dl:hover,.dl:focus-visible{filter:brightness(1.1);transform:translateY(-1px);outline:3px solid var(--soft);outline-offset:2px}
.dl svg{flex:none}.dl span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dl small{font-weight:500;opacity:.85;white-space:nowrap}
.empty,.msg{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:2.2rem 1.2rem;text-align:center;color:var(--muted);box-shadow:var(--shadow)}
.msg{margin:3rem auto 0;max-width:30rem}.msg h1{color:var(--ink);font-size:1.3rem;margin:.2rem 0 .5rem}
.none{display:none;margin-top:1rem}
.foot{color:var(--muted);font-size:.76rem;text-align:center;padding:2rem 1rem 2.5rem}
.closed .dl{display:none}
@media(prefers-reduced-motion:no-preference){.note{animation:up .35s ease both}@keyframes up{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}}
@media(max-width:30rem){h1{font-size:1.55rem}.note{padding:.8rem}.ft{width:2.6rem;height:3rem}}
"""

# small download arrow icon (SVG) used on the Download buttons
DL_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 20h16"/></svg>'


def _respond(body, status=200, nonce=None):
    """Wrap HTML in a response with strict security headers: no caching, no search-engine indexing, no framing,
    and scripts allowed only when they carry this page's one-time `nonce`."""
    response = HttpResponse(body, status=status, content_type="text/html; charset=utf-8")
    script = f" script-src 'nonce-{nonce}';" if nonce else ""
    response["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline';" + script + " base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    return response


def _shell(title, inner, nonce=None, script=""):
    """The full HTML page around `inner`: head, the CSS, and an optional inline script that carries the nonce."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{escape(title)}</title><style>{CSS}</style></head><body>{inner}"
        + (f'<script nonce="{nonce}">{script}</script>' if script else "")
        + "</body></html>"
    )


def _header(sub_title, sub, stats=()):
    """The coloured top part of the page: college name and address, the class title and small count pills."""
    college = escape(getattr(settings, "COLLEGE_NAME", ""))
    address = escape(getattr(settings, "COLLEGE_ADDRESS", ""))
    pills = "".join(f"<span>{escape(x)}</span>" for x in stats)
    return (f'<header class="hero"><div class="wrap"><div class="college">{college}</div>'
            f'<div class="addr">{address}</div><h1>{escape(sub_title)}</h1><div class="sub">{escape(sub)}</div>'
            f'{f"<div class=stats>{pills}</div>" if pills else ""}</div></header>')


def message_page(title, text, status):
    """A plain page with a heading and one sentence, used for errors such as an expired or unknown link."""
    inner = f'<div class="wrap"><div class="msg"><h1>{escape(title)}</h1><p>{escape(text)}</p></div></div>'
    return _respond(_shell(title, inner), status)


def _when(dt):
    """A date and time in the local time zone, like "5 Oct 2026, 2:30 PM"."""
    return timezone.localtime(dt).strftime("%d %b %Y, %I:%M %p").lstrip("0")


# clock icon (SVG) shown beside the countdown
CLOCK = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9 2h6"/></svg>'
# file extension -> colour/label group of the file icon (unknown types fall back to "txt")
KINDS = {"pdf": "pdf", "doc": "doc", "docx": "doc", "xls": "xls", "xlsx": "xls", "csv": "xls", "ppt": "ppt", "pptx": "ppt",
         "png": "img", "jpg": "img", "jpeg": "img", "txt": "txt"}


def _kind(name):
    """Return (icon group, short label) for a file name, e.g. "a.pdf" gives ("pdf", "PDF")."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return KINDS.get(ext, "txt"), (ext[:4].upper() or "FILE")


def _initials(name):
    """First letters of the first two words of a name (not used by notes_page)."""
    return "".join(w[0] for w in str(name).split()[:2]).upper() or "T"


def notes_page(share, notes):
    """Build the whole notes page for a class link: header, countdown, search box, subject chips and the notes grouped by subject."""
    nonce = secrets.token_urlsafe(16)
    cls = share.classroom
    end_ms = int(share.expires_at.timestamp() * 1000)  # when the link ends, in milliseconds, for the countdown script
    groups = {}  # subject name -> its notes (notes without a subject go under "General")
    for n in notes:
        groups.setdefault(n.subject.name if n.subject else "General", []).append(n)
    parts, chips = [], ['<button class="chip on" data-s="">All</button>']
    for i, (subject, items) in enumerate(groups.items()):
        chips.append(f'<button class="chip" data-s="g{i}">{escape(subject)}</button>')
        parts.append(f'<section data-g="g{i}"><h2>{escape(subject)} <small>{len(items)}</small></h2>')
        for n in items:
            body = f'<p class="body">{escape(n.body)}</p>' if n.body else ""
            if n.file_name:  # a note with a file gets a Download button; a text-only note shows a NOTE tile
                cls_name, label = _kind(n.file_name)
                link = (f'<a class="dl" href="/s/{escape(share.token)}/f/{n.id}/" download rel="noopener">{DL_ICON}'
                        f'<span>Download</span> <small>{escape(files.human_size(n.file_size))}</small></a>')
                file_line = f'<div class="meta">{escape(n.file_name)}</div>'
            else:
                cls_name, label, link, file_line = "memo", "NOTE", "", ""
            parts.append(
                f'<article class="note" data-t="{escape((n.title + " " + (n.body or "")).lower())}"><div class="ft {cls_name}">{label}</div>'
                f'<div class="main"><h3>{escape(n.title)}</h3>'
                f'<div class="meta">{escape(n.uploaded_by.display_name)} · {escape(_when(n.created_at))}</div>{file_line}{body}{link}</div></article>'
            )
        parts.append("</section>")
    if not parts:
        content = '<div class="empty">No notes have been shared yet. Check again later.</div>'
        tools = ""
        stats = ()
    else:
        content = "".join(parts) + '<div class="empty none" id="nomatch">Nothing matches your search.</div>'
        tools = (f'<div class="tools"><div class="sbox"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg><input class="search" id="q" type="search" placeholder="Search the notes" aria-label="Search the notes" autocomplete="off"></div>'
                 f'<div class="chips" id="chips">{"".join(chips) if len(groups) > 1 else ""}</div></div>')
        stats = (f"{len(notes)} note{'s' if len(notes) != 1 else ''}", f"{len(groups)} subject{'s' if len(groups) != 1 else ''}")
    inner = (
        _header(cls.label, "Notes shared by your teachers", stats)
        + f'<div class="timer" id="t" data-end="{end_ms}"><div class="box"><div class="clock">{CLOCK}</div><div>'
        f'<div class="t1">Open until {escape(_when(share.expires_at))}</div><div class="t2" id="left"></div></div></div></div>'
        f'<main class="wrap" id="list">{tools}{content}</main>'
        '<p class="foot">Keep this link to yourself. It stops working at the time shown above.</p>'
    )
    # small script: live countdown, and search / subject filter done in the browser (it is allowed by the nonce)
    script = """
(function(){var end=+document.getElementById('t').dataset.end,el=document.getElementById('left');
function p(n){return n<10?'0'+n:n}
function tick(){var s=Math.floor((end-Date.now())/1000);
if(s<=0){document.getElementById('list').className='wrap closed';el.textContent='This link has closed. Reload the page.';
document.getElementById('list').insertAdjacentHTML('afterbegin','<div class="empty" style="margin-bottom:1rem">The time for this link is over.</div>');return}
var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
el.textContent=(d?d+' d ':'')+(d||h?h+' h ':'')+m+' min '+p(s%60)+' s left';setTimeout(tick,1000)}
tick();
var q=document.getElementById('q'),chips=document.getElementById('chips'),cur='';
function apply(){if(!q)return;var t=q.value.trim().toLowerCase(),any=false,secs=document.querySelectorAll('[data-g]');
secs.forEach(function(sec){var shown=0;sec.querySelectorAll('.note').forEach(function(n){var ok=(!cur||sec.dataset.g===cur)&&(!t||n.dataset.t.indexOf(t)>-1);n.style.display=ok?'':'none';if(ok)shown++});
sec.style.display=shown?'':'none';if(shown)any=true});
document.getElementById('nomatch').style.display=any?'none':'block'}
if(q){q.addEventListener('input',apply)}
if(chips){chips.addEventListener('click',function(e){var b=e.target.closest('.chip');if(!b)return;cur=b.dataset.s;
chips.querySelectorAll('.chip').forEach(function(c){c.classList.toggle('on',c===b)});apply()})}
})();
"""
    return _respond(_shell(f"Notes · {cls.label}", inner, nonce, script), 200, nonce)
