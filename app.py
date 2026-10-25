"""CampusFind - RBU Lost & Found Management System (Flask + SQLite)."""
import csv, os, re, secrets, sqlite3, time
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, abort, flash, send_from_directory)
from markupsafe import Markup, escape
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
# Storage locations. Override with environment variables when deploying (e.g. a Render persistent disk).
DB = os.environ.get("DATABASE_PATH") or os.path.join(BASE, "database.db")
UPLOADS = os.environ.get("UPLOAD_FOLDER") or os.path.join(BASE, "uploads")   # private: never under /static
CSV_FILE = os.path.join(BASE, "data", "CampusFind_RBU_Synthetic_Demo_119_Records.csv")
os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
ON_RENDER = bool(os.environ.get("RENDER"))

app = Flask(__name__)
if ON_RENDER:   # behind Render's proxy: use the real client IP / scheme (needed for login throttling and secure cookies)
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
_kf = os.path.join(os.path.dirname(os.path.abspath(DB)), ".secret_key")   # fallback key lives next to the database
if not os.environ.get("SECRET_KEY") and not os.path.exists(_kf):
    open(_kf, "w").write(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or open(_kf).read()
app.permanent_session_lifetime = timedelta(minutes=30)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS")) or ON_RENDER,
                  MAX_CONTENT_LENGTH=2 * 1024 * 1024)

BUILDINGS = [("DT", "DIGITAL TOWER (DT)"), ("GBLOCK", "G-BLOCK"), ("ECS", "ECS BLOCK"),
             ("MBA", "MBA"), ("MCA", "MCA"), ("BCA", "BCA"), ("EBLOCK", "E BLOCK")]
BN = dict(BUILDINGS)
FLOORS = ["1st Floor", "2nd Floor", "3rd Floor", "4th Floor", "5th Floor", "6th Floor",
          "7th Floor", "Common Areas", "Not Specified"]
CATS = ["Electronics", "Documents", "Accessories", "Personal", "Wallet / Cards", "Keys", "Bottle",
        "Bag", "Watch / Jewellery", "Clothing", "Stationery / Books", "Other"]
AREAS = ["Classroom", "Lab", "Corridor", "Library", "Canteen", "Reception", "Staircase",
         "Lobby", "Seminar Hall", "Common Area", "Other"]
HIGH_VALUE = {"Electronics", "Documents", "Wallet / Cards", "Watch / Jewellery", "Accessories"}
FLOW_L = ["Reported", "Under Review", "Possible Match", "Verification Required", "Resolved"]
FLOW_F = ["Reported", "Awaiting Custody", "In Custody", "Under Review", "Claim Pending",
          "Approved for Handover", "Returned/Closed"]
ALL_STATUS = list(dict.fromkeys(FLOW_L + FLOW_F + ["Withdrawn", "Closed"]))
ACTIVE_C = ("Pending", "More Evidence Requested", "Under Review")
MSG_POSS = ("Possible match found — verification required. This is not confirmation of ownership. "
            "Please provide any requested evidence and wait for staff verification.")
MSG_OK = ("Your claim has been approved for handover. Follow the private instructions from "
          "authorized campus staff and bring your university ID.")
MSG_NO = ("We could not verify this claim with the information available. Review the private "
          "case page for the decision and next steps.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, student_id TEXT UNIQUE, full_name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student',
  building_code TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reports(report_id INTEGER PRIMARY KEY, rid TEXT UNIQUE NOT NULL,
  reporter_id INTEGER REFERENCES users(user_id), reporter_label TEXT, type TEXT NOT NULL, name TEXT NOT NULL,
  category TEXT NOT NULL, brand TEXT, colour TEXT, description TEXT, private_details TEXT, photo_path TEXT,
  building_code TEXT NOT NULL, floor TEXT, room_area TEXT, event_date TEXT, condition TEXT, custody TEXT,
  storage_location TEXT, status TEXT NOT NULL, internal_note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS claims(claim_id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(report_id),
  lost_report_id INTEGER REFERENCES reports(report_id), claimant_id INTEGER REFERENCES users(user_id),
  claimant_label TEXT, proof TEXT, status TEXT NOT NULL, reason TEXT, reviewer_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, decision_at TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, report_id INTEGER REFERENCES reports(report_id),
  body TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS handovers(handover_id INTEGER PRIMARY KEY, claim_id INTEGER REFERENCES claims(claim_id),
  staff_id INTEGER REFERENCES users(user_id), handover_at TEXT DEFAULT CURRENT_TIMESTAMP, owner_ack INTEGER);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(user_id),
  message TEXT, is_read INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, actor TEXT, action TEXT, entity TEXT, reason TEXT,
  ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_rep ON reports(status, building_code, event_date);
"""

REP_SQL = ("SELECT r.*, COALESCE(u.full_name, r.reporter_label) rep_name, u.email rep_email, u.student_id rep_sid "
           "FROM reports r LEFT JOIN users u ON u.user_id = r.reporter_id ")
CLAIM_SQL = ("SELECT c.*, f.rid f_rid, f.name f_name, f.category f_cat, f.building_code f_b, f.status f_status, "
             "f.private_details f_priv, f.storage_location f_store, l.rid l_rid, l.private_details l_priv, "
             "COALESCE(u.full_name, c.claimant_label) cl_name, u.student_id cl_sid, u.email cl_email "
             "FROM claims c JOIN reports f ON f.report_id = c.report_id "
             "LEFT JOIN reports l ON l.report_id = c.lost_report_id LEFT JOIN users u ON u.user_id = c.claimant_id ")


# ---------- database helpers ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def run(sql, args=()):            # no commit: lets several writes share one transaction
    return db().execute(sql, args).lastrowid


def x(sql, args=()):
    r = run(sql, args)
    db().commit()
    return r


def audit(action, entity, reason=""):
    run("INSERT INTO audit_logs(actor,action,entity,reason) VALUES(?,?,?,?)",
        (g.user["email"] if g.get("user") else "system", action, entity, reason))


def notify(user_id, msg):
    if user_id:
        run("INSERT INTO notifications(user_id,message) VALUES(?,?)", (user_id, msg))


def next_rid(t):
    n = q("SELECT COUNT(*) c FROM reports WHERE type=?", (t,), True)["c"] + 1
    while q("SELECT 1 FROM reports WHERE rid=?", (f"CF-{t[0]}-2026-{n:05d}",), True):
        n += 1
    return f"CF-{t[0]}-2026-{n:05d}"


def release(c):
    """After a claim is rejected/closed/withdrawn, free the items so they are not stuck in 'Under Review'."""
    act = "('Pending','Under Review','More Evidence Requested','Approved')"
    if not q(f"SELECT 1 FROM claims WHERE report_id=? AND claim_id!=? AND status IN {act}", (c["report_id"], c["claim_id"]), True):
        run("UPDATE reports SET status='In Custody' WHERE report_id=? AND status IN ('Under Review','Claim Pending')", (c["report_id"],))
    if c["lost_report_id"] and not q(f"SELECT 1 FROM claims WHERE lost_report_id=? AND claim_id!=? AND status IN {act}", (c["lost_report_id"], c["claim_id"]), True):
        run("UPDATE reports SET status='Under Review' WHERE report_id=? AND status IN ('Possible Match','Verification Required')", (c["lost_report_id"],))


def image_ok(file):
    head = file.stream.read(12)
    file.stream.seek(0)
    return head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


# ---------- matching (Python similarity; a triage hint, never a decision) ----------
def _tok(s):
    return {w for w in re.split(r"\W+", (s or "").lower()) if len(w) > 2}


def _jac(a, b):
    a, b = _tok(a), _tok(b)
    return len(a & b) / len(a | b) if a and b else 0


def score(l, f):
    n, m = l["name"].lower(), f["name"].lower()
    name = 1 if (n == m or n in m or m in n) else _jac(n, m)
    cat = 1 if l["category"] == f["category"] else 0
    loc = 0
    if l["building_code"] == f["building_code"]:
        loc = .4
        if l["floor"] == f["floor"]:
            loc = .7
            if (l["room_area"] or "").lower() == (f["room_area"] or "").lower():
                loc = 1
    try:
        days = abs((datetime.fromisoformat(l["event_date"]) - datetime.fromisoformat(f["event_date"])).days)
    except (TypeError, ValueError):
        days = 99
    return round(name * 40 + cat * 20 + loc * 20 + _jac(l["description"], f["description"]) * 10
                 + max(0, 1 - days / 14) * 10)


# ---------- auth, CSRF, access control ----------
@app.before_request
def load_user():
    g.user = None
    if "uid" in session:
        g.user = q("SELECT * FROM users WHERE user_id=?", (session["uid"],), True)
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("_csrf", ""), session.get("_csrf", "-")):
            abort(400)


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Cache-Control"] = "no-store"
    return r


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            flash("Please log in to continue.")
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def staff_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if g.user["role"] != "admin":
            abort(403)
        return f(*a, **k)
    return w


def in_scope(code):
    """Building incharges only see their own building; main admin (no building) sees all."""
    return not g.user["building_code"] or g.user["building_code"] == code


def scope_sql():
    return (" AND r.building_code=?", [g.user["building_code"]]) if g.user["building_code"] else ("", [])


def safe_next(n):
    return n if n and n.startswith("/") and not n.startswith("//") else None


ICONS = [("laptop|macbook", "💻"), ("wallet|purse", "WALLET"), ("phone|mobile", "📱"),
         ("airpod|earbud|earphone|headphone|tws", "🎧"), ("charger|cable|hdmi|power ?bank|adapter", "🔌"),
         ("key", "🔑"), ("bottle|flask|tumbler", "BOTTLE"), ("watch", "⌚"), ("bag|backpack", "🎒"),
         ("hoodie|jacket", "🧥"), ("calculator", "🧮"), ("notebook|diary", "📓"), ("book", "📘"),
         ("pen|pencil", "🖊️"), ("umbrella", "☂️"), ("id card", "🪪"), ("mouse", "🖱️"),
         ("keyboard", "⌨️"), ("tiffin", "🍱"), ("tablet", "📲"), ("usb|drive", "💾")]
_SVG = {"WALLET": '<svg viewBox="0 0 64 64" width="1em" height="1em"><rect x="6" y="14" width="52" height="38" rx="6" fill="#8a5a2b" stroke="#000" stroke-width="3"/><rect x="38" y="30" width="20" height="14" rx="4" fill="#d9a35f" stroke="#000" stroke-width="3"/><circle cx="46" cy="37" r="3"/></svg>',
        "BOTTLE": '<svg viewBox="0 0 64 64" width="1em" height="1em"><rect x="25" y="4" width="14" height="8" rx="2" fill="#222" stroke="#000" stroke-width="3"/><path d="M26 12h12v6c0 3 8 5 8 12v26a4 4 0 0 1-4 4H22a4 4 0 0 1-4-4V30c0-7 8-9 8-12z" fill="#3aa0ff" stroke="#000" stroke-width="3"/></svg>'}


def icon(name, cat=""):
    for pat, e in ICONS:
        if re.search(pat, (name or "").lower()):
            return Markup(_SVG.get(e, e))
    return Markup(_SVG["WALLET"] if cat == "Wallet / Cards" else "📦")


_OK = {"Resolved", "Returned/Closed", "Approved", "Approved for Handover"}
_BAD = {"Rejected", "Withdrawn", "Closed", "Closed as Duplicate"}
_HOT = {"Possible Match", "Verification Required", "More Evidence Requested"}


@app.context_processor
def inject():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    unread = 0
    if g.get("user"):
        unread = q("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", (g.user["user_id"],), True)["c"]
    return dict(
        csrf=lambda: Markup(f'<input type="hidden" name="_csrf" value="{session["_csrf"]}">'),
        pl=lambda s: Markup('<span class="pill %s">%s</span>' % (
            "sok" if s in _OK else "sbad" if s in _BAD else "shot" if s in _HOT else "swait", escape(s))),
        tp=lambda t: Markup(f'<span class="pill {t}">{t}</span>'),
        icon=icon, BUILDINGS=BUILDINGS, BN=BN, FLOORS=FLOORS, CATS=CATS, AREAS=AREAS, ALL_STATUS=ALL_STATUS,
        unread=unread, HIGH_VALUE=HIGH_VALUE, MSG_POSS=MSG_POSS, MSG_OK=MSG_OK, MSG_NO=MSG_NO)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(413)
def err(e):
    return render_template("error.html", code=e.code), e.code


@app.errorhandler(500)
def err500(e):
    return render_template("error.html", code=500), 500


# ---------- public pages ----------
PAGES = {
    "how": ("How it works", [
        ("If you lost something", ["Log in and open Report Lost.", "Enter item details, last-seen location and private identifiers.",
                                   "Get a tracking ID and follow it in My Reports.", "Staff may suggest a possible match and ask for proof.",
                                   "After verification you get private handover instructions.", "Staff record the handover and close the case."]),
        ("If you found something", ["Log in and open Report Found.", "Enter what you know and where you found it.",
                                    "Say whether you still hold it or gave it to staff.", "Get a tracking ID. Staff record where it is stored.",
                                    "Staff review claims and evidence.", "One claim is approved only after verification, then handover is recorded."])]),
    "privacy": ("Safety and privacy", [("Our promises", [
        "No public catalogue. Guests cannot see any report.", "A university login proves who you are, not that an item is yours.",
        "A tracking ID is not proof of ownership.", "Private identification details are visible only to you and authorized staff.",
        "Exact storage locations are staff-only until a claim is approved.", "Matching only suggests candidates. Staff decide every claim manually.",
        "High-value items need in-person verification.", "Staff decisions and handovers are recorded in an audit log."])]),
    "faq": ("FAQs and help", [
        ("How do I report an item?", "Log in, choose Report Lost or Report Found and fill in the form."),
        ("What is a tracking ID?", "A code like CF-L-2026-00125 that identifies your case. It does not prove ownership."),
        ("What counts as proof?", "Item-specific details such as a scratch, sticker, hidden mark, case detail, distinctive contents or part of a serial number."),
        ("Why isn't my login enough?", "Your account proves your identity only. Staff must verify that the item is yours."),
        ("What happens after I report?", "Staff review the report, may suggest a match and may ask for evidence. Recovery is not guaranteed or instant."),
        ("Who do I contact?", "Visit the university Lost & Found office with your ID, or follow the instructions in your notifications.")]),
}
PAGES["help"] = PAGES["faq"]


def make_page(key):
    return lambda: render_template("page.html", page=PAGES[key])


for _k, _u in (("how", "/how-it-works"), ("privacy", "/privacy"), ("faq", "/faq"), ("help", "/help")):
    app.add_url_rule(_u, _k, make_page(_k))


@app.route("/")
def home():
    return render_template("home.html")


ATTEMPTS = {}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        n, until = ATTEMPTS.get(ip, (0, 0))
        if until and time.time() >= until:
            n = 0
        if time.time() < until:
            flash("Too many attempts. Please try again shortly.")
            return render_template("login.html")
        u = q("SELECT * FROM users WHERE email=?", ((request.form.get("email") or "").strip().lower(),), True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            ATTEMPTS.pop(ip, None)
            session.clear()
            session.permanent = True
            session["uid"] = u["user_id"]
            g.user = u
            audit("login", u["email"])
            db().commit()
            nxt = safe_next(request.args.get("next"))
            if u["role"] == "admin":
                return redirect(nxt if nxt and nxt.startswith("/admin") else url_for("admin"))
            return redirect(nxt or url_for("dashboard"))
        ATTEMPTS[ip] = (n + 1, time.time() + 30 if n + 1 >= 5 else 0)
        flash("Email or password is incorrect.")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        em, sid, pw = f.get("email", "").strip().lower(), f.get("student_id", "").strip(), f.get("password", "")
        ok = (em.endswith("@rbunagpur.in") and pw == f.get("confirm") and len(pw) >= 8 and sid
              and f.get("name", "").strip() and f.get("consent"))
        if ok and not q("SELECT 1 FROM users WHERE email=? OR student_id=?", (em, sid), True):
            uid = x("INSERT INTO users(student_id,full_name,email,password_hash) VALUES(?,?,?,?)",
                    (sid, f["name"].strip(), em, generate_password_hash(pw)))
            session.clear()
            session.permanent = True
            session["uid"] = uid
            return redirect(url_for("dashboard"))
        flash("We could not create an account with these details. Check them and try again (password needs 8+ characters).")
    return render_template("register.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------- student ----------
@app.route("/dashboard")
@login_required
def dashboard():
    if g.user["role"] == "admin":
        return redirect(url_for("admin"))
    uid = g.user["user_id"]
    m = q("SELECT * FROM reports WHERE reporter_id=?", (uid,))
    claims = q(CLAIM_SQL + "WHERE c.claimant_id=?", (uid,))
    wait = {"Possible Match", "Verification Required", "Claim Pending", "Approved for Handover"}
    done = {"Resolved", "Returned/Closed", "Closed"}
    stats = [("Lost reports", sum(r["type"] == "Lost" for r in m), "?type=Lost"),
             ("Found reports", sum(r["type"] == "Found" for r in m), "?type=Found"),
             ("Awaiting verification", sum(r["status"] in wait for r in m), "?w=1"),
             ("Returned / closed", sum(r["status"] in done for r in m), "?w=2")]
    need = [c for c in claims if c["status"] == "More Evidence Requested" or (c["status"] == "Pending" and not c["proof"])]
    return render_template("dashboard.html", stats=stats, need=need, nclaims=len(claims))


@app.route("/reports/<kind>/new", methods=["GET", "POST"])
@login_required
def new_report(kind):
    t = kind.capitalize()
    if t not in ("Lost", "Found"):
        abort(404)
    if g.user["role"] == "admin":
        abort(403)
    if request.method == "POST":
        f = request.form
        name, desc = f.get("name", "").strip(), f.get("description", "").strip()
        b, fl, area, cat = f.get("building"), f.get("floor"), f.get("area"), f.get("category")
        try:
            when = datetime.fromisoformat(f.get("when", ""))
            if when > datetime.now() + timedelta(minutes=5):
                raise ValueError
        except ValueError:
            when = None
        if not (name and desc and b in BN and fl in FLOORS and area in AREAS and cat in CATS and when) or len(name) > 80:
            flash("Please complete the required fields with valid values.")
            return render_template("report_form.html", t=t, f=f)
        room = area + (" – " + f["room_detail"].strip()[:40] if f.get("room_detail", "").strip() else "")
        if q("SELECT 1 FROM reports WHERE reporter_id=? AND type=? AND lower(name)=? AND room_area=? AND event_date=?",
             (g.user["user_id"], t, name.lower(), room, when.date().isoformat()), True):
            flash("This looks like a duplicate of your earlier report.")
            return render_template("report_form.html", t=t, f=f)
        photo = None
        file = request.files.get("photo")
        if file and file.filename:
            ext = file.filename.rsplit(".", 1)[-1].lower()
            if ext not in ("png", "jpg", "jpeg", "webp"):
                flash("Only PNG, JPG or WEBP photos are allowed.")
                return render_template("report_form.html", t=t, f=f)
            if not image_ok(file):
                flash("That file is not a valid image.")
                return render_template("report_form.html", t=t, f=f)
            photo = secrets.token_hex(16) + "." + ext
            file.save(os.path.join(UPLOADS, photo))
        rid = next_rid(t)
        cust = f.get("custody", "")
        status = "Awaiting Custody" if t == "Found" and cust.startswith("Submitted") else "Reported"
        run("INSERT INTO reports(rid,reporter_id,type,name,category,brand,colour,description,private_details,photo_path,"
            "building_code,floor,room_area,event_date,condition,custody,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, g.user["user_id"], t, name, cat, f.get("brand", "").strip()[:40] or "Unknown",
             f.get("colour", "").strip()[:30] or "Unknown", desc[:300], f.get("private", "").strip()[:300] or "None provided",
             photo, b, fl, room, when.date().isoformat(), f.get("condition", "")[:60], cust, status,
             datetime.now().strftime("%Y-%m-%d %H:%M")))
        notify(g.user["user_id"], f"Report {rid} was submitted.")
        audit("report_created", rid)
        db().commit()
        flash(f"Your {t.lower()}-item report has been submitted. Your tracking ID is {rid}. "
              + ("Keep it for reference; it is not proof of ownership." if t == "Lost"
                 else "Follow the custody instructions in your dashboard."))
        return redirect(url_for("report_detail", rid=rid))
    return render_template("report_form.html", t=t, f={})


@app.route("/my-reports")
@login_required
def my_reports():
    a = request.args
    sql, p = REP_SQL + "WHERE r.reporter_id=?", [g.user["user_id"]]
    for key, col in (("type", "r.type"), ("status", "r.status"), ("building", "r.building_code")):
        if a.get(key):
            sql += f" AND {col}=?"
            p.append(a[key])
    if a.get("since"):
        sql += " AND r.event_date>=?"
        p.append(a["since"])
    wait = "('Possible Match','Verification Required','Claim Pending','Approved for Handover')"
    if a.get("w") == "1":
        sql += f" AND r.status IN {wait}"
    if a.get("w") == "2":
        sql += " AND r.status IN ('Resolved','Returned/Closed','Closed')"
    return render_template("my_reports.html", rows=q(sql + " ORDER BY r.report_id DESC", p), a=a)


def get_report(rid):
    r = q(REP_SQL + "WHERE r.rid=?", (rid,), True)
    if not r:
        abort(404)
    owner = r["reporter_id"] == g.user["user_id"]
    if not (owner or (g.user["role"] == "admin" and in_scope(r["building_code"]))):
        abort(403)
    return r, owner


@app.route("/reports/<rid>")
@login_required
def report_detail(rid):
    r, owner = get_report(rid)
    msgs = q("SELECT body, created_at FROM messages WHERE report_id=? ORDER BY id", (r["report_id"],))
    flow = FLOW_L if r["type"] == "Lost" else FLOW_F
    return render_template("report_detail.html", r=r, owner=owner, msgs=msgs, flow=flow)


@app.route("/reports/<rid>/<act>", methods=["POST"])
@login_required
def report_act(rid, act):
    r, owner = get_report(rid)
    if not owner or r["status"] in ("Resolved", "Returned/Closed", "Withdrawn", "Closed"):
        abort(403)
    if act == "withdraw":
        run("UPDATE reports SET status='Withdrawn' WHERE report_id=?", (r["report_id"],))
        for c in q("SELECT * FROM claims WHERE (report_id=? OR lost_report_id=?) AND status IN ('Pending','Under Review','More Evidence Requested')", (r["report_id"],) * 2):
            run("UPDATE claims SET status='Closed as Duplicate', reason='Linked report withdrawn' WHERE claim_id=?", (c["claim_id"],))
            release(c)
        audit("report_withdrawn", rid)
    elif act == "edit" and r["status"] == "Reported" and request.form.get("description", "").strip():
        run("UPDATE reports SET description=? WHERE report_id=?", (request.form["description"].strip()[:300], r["report_id"]))
        audit("report_edited", rid)
    else:
        abort(400)
    db().commit()
    return redirect(url_for("report_detail", rid=rid))


@app.route("/files/<name>")
@login_required
def private_file(name):
    r = q("SELECT * FROM reports WHERE photo_path=?", (name,), True)
    if not r or not (r["reporter_id"] == g.user["user_id"] or (g.user["role"] == "admin" and in_scope(r["building_code"]))):
        abort(403)
    return send_from_directory(UPLOADS, name)


@app.route("/my-claims")
@login_required
def my_claims():
    return render_template("my_claims.html", claims=q(CLAIM_SQL + "WHERE c.claimant_id=? ORDER BY c.claim_id DESC", (g.user["user_id"],)))


@app.route("/claims/<int:cid>/evidence", methods=["POST"])
@login_required
def evidence(cid):
    c = q("SELECT * FROM claims WHERE claim_id=?", (cid,), True)
    if not c or c["claimant_id"] != g.user["user_id"] or c["status"] not in ("Pending", "More Evidence Requested"):
        abort(403)
    proof = request.form.get("proof", "").strip()[:400]
    if not proof:
        abort(400)
    run("UPDATE claims SET proof=?, status='Under Review' WHERE claim_id=?", (proof, cid))
    audit("evidence_submitted", f"CL-{cid:04d}")      # proof text is never logged
    for a in q("SELECT user_id FROM users WHERE role='admin'"):
        notify(a["user_id"], f"Evidence submitted for claim CL-{cid:04d}")
    db().commit()
    flash("Evidence submitted. Staff will review it.")
    return redirect(url_for("my_claims"))


@app.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    if request.method == "POST":
        x("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (request.form.get("id"), g.user["user_id"]))
        return redirect(url_for("notifications"))
    return render_template("notifications.html", rows=q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (g.user["user_id"],)))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        f = request.form
        if f.get("new"):
            if not check_password_hash(g.user["password_hash"], f.get("current", "")) or len(f["new"]) < 8:
                flash("Current password is incorrect, or the new password is shorter than 8 characters.")
                return redirect(url_for("profile"))
            run("UPDATE users SET password_hash=? WHERE user_id=?", (generate_password_hash(f["new"]), g.user["user_id"]))
            audit("password_changed", g.user["email"])
        if f.get("name", "").strip():
            run("UPDATE users SET full_name=? WHERE user_id=?", (f["name"].strip()[:80], g.user["user_id"]))
        db().commit()
        flash("Profile saved.")
        return redirect(url_for("profile"))
    return render_template("profile.html")


# ---------- staff / building incharge ----------
@app.route("/admin")
@staff_required
def admin():
    if g.user["building_code"]:
        return redirect(url_for("admin_building", code=g.user["building_code"]))
    n = lambda sql: q(sql, one=True)[0]
    counts = [("New reports", n("SELECT COUNT(*) FROM reports WHERE status='Reported'"), url_for("admin_reports", status="Reported")),
              ("Pending claims", n("SELECT COUNT(*) FROM claims WHERE status IN ('Pending','Under Review')"), url_for("admin_claims")),
              ("Evidence requests", n("SELECT COUNT(*) FROM claims WHERE status='More Evidence Requested'"), url_for("admin_claims")),
              ("Items in custody", n("SELECT COUNT(*) FROM reports WHERE status='In Custody'"), url_for("admin_storage")),
              ("Handovers awaiting", n("SELECT COUNT(*) FROM reports WHERE status='Approved for Handover'"), url_for("admin_handover"))]
    blds = []
    for code, name in BUILDINGS:
        tot = n(f"SELECT COUNT(*) FROM reports WHERE building_code='{code}'")
        new = n(f"SELECT COUNT(*) FROM reports WHERE building_code='{code}' AND status='Reported'")
        pc = n(f"SELECT COUNT(*) FROM claims c JOIN reports f ON f.report_id=c.report_id WHERE f.building_code='{code}' AND c.status IN ('Pending','Under Review','More Evidence Requested')")
        blds.append((code, name, tot, new, pc))
    return render_template("admin_overview.html", counts=counts, blds=blds)


@app.route("/admin/building/<code>")
@staff_required
def admin_building(code):
    if code not in BN:
        abort(404)
    if not in_scope(code):
        abort(403)
    rows = q(REP_SQL + "WHERE r.building_code=? ORDER BY r.report_id DESC", (code,))
    floors = [(f, [r for r in rows if r["floor"] == f]) for f in FLOORS]
    floors = [(f, rs) for f, rs in floors if rs]
    pc = q("SELECT COUNT(*) c FROM claims c JOIN reports f ON f.report_id=c.report_id WHERE f.building_code=? "
           "AND c.status IN ('Pending','Under Review','More Evidence Requested')", (code,), True)["c"]
    stats = [("Total reports", len(rows)), ("New reports", sum(r["status"] == "Reported" for r in rows)), ("Pending claims", pc),
             ("In custody", sum(r["status"] == "In Custody" for r in rows)),
             ("Awaiting handover", sum(r["status"] == "Approved for Handover" for r in rows))]
    return render_template("admin_building.html", code=code, floors=floors, stats=stats)


@app.route("/admin/reports")
@staff_required
def admin_reports():
    a = request.args
    sc, sp = scope_sql()
    sql, p = REP_SQL + "WHERE 1=1" + sc, sp
    for key, col in (("building", "r.building_code"), ("floor", "r.floor"), ("type", "r.type"), ("status", "r.status")):
        if a.get(key):
            sql += f" AND {col}=?"
            p.append(a[key])
    if a.get("q"):
        sql += " AND (r.name LIKE ? OR r.description LIKE ? OR r.rid LIKE ?)"
        p += [f"%{a['q']}%"] * 3
    return render_template("admin_reports.html", rows=q(sql + " ORDER BY r.report_id DESC", p), a=a)


@app.route("/admin/reports/<rid>", methods=["GET", "POST"])
@staff_required
def admin_report(rid):
    r = q(REP_SQL + "WHERE r.rid=?", (rid,), True)
    if not r:
        abort(404)
    if not in_scope(r["building_code"]):
        abort(403)
    if request.method == "POST":
        f, act = request.form, request.form.get("act")
        allowed = (FLOW_L if r["type"] == "Lost" else FLOW_F) + ["Withdrawn", "Closed"]
        if act == "status" and f.get("status") in allowed and f.get("reason", "").strip():
            run("UPDATE reports SET status=? WHERE report_id=?", (f["status"], r["report_id"]))
            audit(f"status:{r['status']}->{f['status']}", rid, f["reason"].strip())
            notify(r["reporter_id"], f"Status of {rid} changed to {f['status']}.")
        elif act == "message" and f.get("body", "").strip():
            run("INSERT INTO messages(report_id,body) VALUES(?,?)", (r["report_id"], f["body"].strip()[:200]))
            notify(r["reporter_id"], f"New staff message on {rid}.")
            audit("message_sent", rid)
        elif act == "note":
            run("UPDATE reports SET internal_note=? WHERE report_id=?", (f.get("note", "").strip()[:500], r["report_id"]))
            audit("internal_note", rid)
        db().commit()
        return redirect(url_for("admin_report", rid=rid))
    msgs = q("SELECT body, created_at FROM messages WHERE report_id=? ORDER BY id", (r["report_id"],))
    return render_template("admin_report.html", r=r, msgs=msgs, statuses=(FLOW_L if r["type"] == "Lost" else FLOW_F) + ["Withdrawn", "Closed"])


@app.route("/admin/matches", methods=["GET", "POST"])
@staff_required
def admin_matches():
    if request.method == "POST":
        l = q("SELECT * FROM reports WHERE report_id=? AND type='Lost'", (request.form.get("lost"),), True)
        f = q("SELECT * FROM reports WHERE report_id=? AND type='Found'", (request.form.get("found"),), True)
        if not (l and f and in_scope(l["building_code"])):
            abort(403)
        if l["status"] not in ("Reported", "Under Review", "Possible Match") or f["status"] not in ("Reported", "Awaiting Custody", "In Custody", "Under Review"):
            flash("One of these reports is no longer open for matching.")
            return redirect(url_for("admin_matches"))
        if not q("SELECT 1 FROM claims WHERE lost_report_id=? AND report_id=?", (l["report_id"], f["report_id"]), True):
            run("INSERT INTO claims(report_id,lost_report_id,claimant_id,claimant_label,status) VALUES(?,?,?,?,'Pending')",
                (f["report_id"], l["report_id"], l["reporter_id"], l["reporter_label"]))
            run("UPDATE reports SET status='Possible Match' WHERE report_id=?", (l["report_id"],))
            run("UPDATE reports SET status='Under Review' WHERE report_id=?", (f["report_id"],))
            notify(l["reporter_id"], MSG_POSS)
            audit("match_suggested", f"{l['rid']} <-> {f['rid']}", "triage hint only")
            db().commit()
        return redirect(url_for("admin_matches"))
    sc, sp = scope_sql()
    lost = q("SELECT r.* FROM reports r WHERE r.type='Lost' AND r.status IN ('Reported','Under Review','Possible Match')" + sc, sp)
    found = q("SELECT * FROM reports WHERE type='Found' AND status IN ('Reported','Awaiting Custody','In Custody','Under Review')")
    done = {(c["lost_report_id"], c["report_id"]) for c in q("SELECT lost_report_id, report_id FROM claims")}
    pairs = sorted((p for p in ((score(l, f), l, f) for l in lost for f in found) if p[0] >= 55), key=lambda p: -p[0])
    groups = [(c, n, [p for p in pairs if p[1]["building_code"] == c][:15]) for c, n in BUILDINGS]
    return render_template("admin_matches.html", groups=[g_ for g_ in groups if g_[2]], done=done)


@app.route("/admin/claims", methods=["GET", "POST"])
@staff_required
def admin_claims():
    if request.method == "POST":
        c = q(CLAIM_SQL + "WHERE c.claim_id=?", (request.form.get("claim_id"),), True)
        if not c or not in_scope(c["f_b"]):
            abort(403)
        act, reason = request.form.get("act"), request.form.get("reason", "").strip()
        label = f"CL-{c['claim_id']:04d}"
        if c["status"] not in ACTIVE_C:
            abort(400)
        if act != "approve" and not reason:
            flash("A reason is required.")
            return redirect(url_for("admin_claims"))
        if act == "approve":
            if not c["proof"]:
                flash("No evidence submitted yet. Request evidence first.")
                return redirect(url_for("admin_claims"))
            if c["f_cat"] in HIGH_VALUE and not request.form.get("inperson"):
                flash("High-value item: tick the in-person verification box to approve.")
                return redirect(url_for("admin_claims"))
            if q("SELECT 1 FROM claims WHERE report_id=? AND status='Approved'", (c["report_id"],), True):
                flash("Another claim is already approved for this item.")
                return redirect(url_for("admin_claims"))
            run("UPDATE claims SET status='Approved', reviewer_id=?, decision_at=CURRENT_TIMESTAMP WHERE claim_id=?", (g.user["user_id"], c["claim_id"]))
            run("UPDATE reports SET status='Approved for Handover' WHERE report_id=?", (c["report_id"],))
            if c["lost_report_id"]:
                run("UPDATE reports SET status='Verification Required' WHERE report_id=?", (c["lost_report_id"],))
            notify(c["claimant_id"], MSG_OK)
            audit("claim_approved", label, "Approved for handover")
        elif act == "evidence":
            run("UPDATE claims SET status='More Evidence Requested', reason=? WHERE claim_id=?", (reason, c["claim_id"]))
            notify(c["claimant_id"], f"Staff requested more evidence for claim {label}.")
            audit("evidence_requested", label, reason)
        elif act == "reject":
            run("UPDATE claims SET status='Rejected', reason=?, reviewer_id=?, decision_at=CURRENT_TIMESTAMP WHERE claim_id=?", (reason, g.user["user_id"], c["claim_id"]))
            release(c)
            notify(c["claimant_id"], MSG_NO)
            audit("claim_rejected", label, reason)
        elif act == "duplicate":
            run("UPDATE claims SET status='Closed as Duplicate', reason=? WHERE claim_id=?", (reason, c["claim_id"]))
            release(c)
            audit("claim_closed_duplicate", label, reason)
        else:
            abort(400)
        db().commit()
        return redirect(url_for("admin_claims"))
    sc, sp = scope_sql()
    rows = q(CLAIM_SQL + "JOIN reports r ON r.report_id=c.report_id WHERE 1=1" + sc + " ORDER BY c.status IN ('Pending','Under Review','More Evidence Requested') DESC, c.claim_id DESC", sp)
    return render_template("admin_claims.html", rows=rows, ACTIVE_C=ACTIVE_C)


@app.route("/admin/storage", methods=["GET", "POST"])
@staff_required
def admin_storage():
    if request.method == "POST":
        r = q("SELECT * FROM reports WHERE report_id=? AND type='Found'", (request.form.get("report_id"),), True)
        loc = request.form.get("storage", "").strip()[:80]
        if not r or not in_scope(r["building_code"]) or not loc:
            abort(400)
        run("UPDATE reports SET storage_location=?, status=CASE WHEN status IN ('Reported','Awaiting Custody') THEN 'In Custody' ELSE status END WHERE report_id=?", (loc, r["report_id"]))
        audit("storage_recorded", r["rid"])
        db().commit()
        flash("Storage recorded.")
        return redirect(url_for("admin_storage"))
    sc, sp = scope_sql()
    rows = q(REP_SQL + "WHERE r.type='Found' AND r.status IN ('Reported','Awaiting Custody','In Custody','Under Review','Claim Pending')" + sc, sp)
    return render_template("admin_storage.html", rows=rows)


@app.route("/admin/handover", methods=["GET", "POST"])
@staff_required
def admin_handover():
    if request.method == "POST":
        c = q(CLAIM_SQL + "WHERE c.claim_id=?", (request.form.get("claim_id"),), True)
        if not c or not in_scope(c["f_b"]) or c["status"] != "Approved" or c["f_status"] != "Approved for Handover":
            abort(403)
        if not all(request.form.get(k) for k in ("id_ok", "proof_ok", "ack")):
            flash("Complete every verification step first.")
            return redirect(url_for("admin_handover"))
        run("INSERT INTO handovers(claim_id,staff_id,owner_ack) VALUES(?,?,1)", (c["claim_id"], g.user["user_id"]))
        run("UPDATE reports SET status='Returned/Closed' WHERE report_id=?", (c["report_id"],))
        if c["lost_report_id"]:
            run("UPDATE reports SET status='Resolved' WHERE report_id=?", (c["lost_report_id"],))
        run("UPDATE claims SET status='Closed as Duplicate', reason='Item returned to verified owner' WHERE report_id=? AND claim_id!=? AND status IN ('Pending','Under Review','More Evidence Requested')", (c["report_id"], c["claim_id"]))
        notify(c["claimant_id"], "Handover recorded. Your case is closed.")
        audit("handover_recorded", f"CL-{c['claim_id']:04d}", c["f_rid"])
        db().commit()               # one transaction: approve, close item and close competing claims together
        flash("Handover recorded.")
        return redirect(url_for("admin_handover"))
    sc, sp = scope_sql()
    due = q(CLAIM_SQL + "JOIN reports r ON r.report_id=c.report_id WHERE c.status='Approved' AND f.status='Approved for Handover'" + sc, sp)
    log = q("SELECT h.*, u.full_name staff, c.report_id FROM handovers h JOIN users u ON u.user_id=h.staff_id JOIN claims c ON c.claim_id=h.claim_id ORDER BY h.handover_id DESC")
    return render_template("admin_handover.html", due=due, log=log)


@app.route("/admin/audit")
@staff_required
def admin_audit():
    if g.user["building_code"]:
        abort(403)
    return render_template("admin_audit.html", rows=q("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 300"))


# ---------- database creation + seed data ----------
def seed(d):
    def add(sid, name, email, pw, role="student", b=None):
        return d.execute("INSERT INTO users(student_id,full_name,email,password_hash,role,building_code) VALUES(?,?,?,?,?,?)",
                         (sid, name, email, generate_password_hash(pw), role, b)).lastrowid
    add("ADMIN", "Lost & Found Office", "admin@rbunagpur.in", "admin123", "admin")
    stu = add("S001", "Riya Sharma", "student@rbunagpur.in", "student123")
    for code, name in BUILDINGS:
        add("INC-" + code, name + " Incharge", f"incharge.{code.lower()}@rbunagpur.in", "incharge123", "admin", code)
    rows = list(csv.DictReader(open(CSV_FILE, encoding="utf-8-sig")))
    FM = {"1th Floor": "1st Floor", "2th Floor": "2nd Floor", "Common / Facility": "Common Areas"}
    CM = {"Books & Stationery": "Stationery / Books", "Stationery": "Stationery / Books", "Bags": "Bag"}
    LS = {"Active": "Reported", "Claimed": "Verification Required", "Recovered": "Resolved"}
    FS = {"Active": "In Custody", "Claimed": "Claim Pending", "Recovered": "Returned/Closed"}
    cnt = {"Lost": 0, "Found": 0}
    founds = {(r["Item_Name"], r["Building_Code"]) for r in rows if r["Type"] == "Found" and r["Status"] == "Active"}
    given = 0
    for i, r in enumerate(rows):
        t = r["Type"]
        cnt[t] += 1
        st = (LS if t == "Lost" else FS)[r["Status"]]
        uid = None
        if t == "Lost" and r["Status"] == "Active" and given < 4 and (r["Item_Name"], r["Building_Code"]) in founds:
            uid, given = stu, given + 1
        store = f"Lost & Found Office, Cabinet {'ABC'[i % 3]}" if t == "Found" and st != "Returned/Closed" else ""
        rep = d.execute(
            "INSERT INTO reports(rid,reporter_id,reporter_label,type,name,category,brand,colour,description,private_details,"
            "building_code,floor,room_area,event_date,custody,storage_location,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"CF-{t[0]}-2026-{cnt[t]:05d}", uid, r["Reported_By"], t, r["Item_Name"], CM.get(r["Category"], r["Category"]),
             r["Brand"], r["Color"], r["Description"], "On file", r["Building_Code"], FM.get(r["Floor"], r["Floor"]),
             r["Location_Room"], r["Date"], "Submitted to authorized staff" if t == "Found" else "", store, st, r["Date"])).lastrowid
        if t == "Found" and r["Claimant"] and r["Status"] in ("Claimed", "Recovered"):
            d.execute("INSERT INTO claims(report_id,claimant_label,proof,status) VALUES(?,?,?,?)",
                      (rep, r["Claimant"], "Describes the contents and a unique mark on the item.",
                       "Approved" if r["Status"] == "Recovered" else "Pending"))


def init_db():
    d = sqlite3.connect(DB, timeout=15)
    d.executescript(SCHEMA)
    if d.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        seed(d)
    d.commit()
    d.close()


init_db()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=False)      # never enable debug mode in deployment
