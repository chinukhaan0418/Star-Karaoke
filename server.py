#!/usr/bin/env python3

"""Stars Karaoke: website + booking API + admin API."""

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.realpath(os.path.join(ROOT, "public"))
DB = os.environ.get("DB_PATH", os.path.join(ROOT, "stars.db"))
PORT = int(os.environ.get("PORT", "8000"))

TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"
SESSION_HOURS = 12

CORS_EXTRA = [
    x.strip()
    for x in os.environ.get("CORS_ORIGINS", "").split(",")
    if x.strip()
]


def origin_ok(origin):
    return bool(origin) and (
        re.fullmatch(
            r"http://(localhost|127\.0\.0\.1):55\d\d",
            origin
        )
        or origin in CORS_EXTRA
    )


STATUSES = ("pending", "confirmed", "cancelled")

# bump this whenever terms.html / privacy.html change
TERMS_VERSION = "2026-11-10-2"

# Room layout: (id, name, kind, hourly price).
# Applied once (pragma user_version) - after that edit rooms in the admin.
ROOM_LAYOUT = [
    (1, "VIP++ #1", "vip++", 50000),
    (2, "VIP++ #2", "vip++", 50000),
    (3, "VIP++ #3", "vip++", 50000),
    (4, "Room #1", "normal", 30000),
    (5, "Room #2", "normal", 30000),
    (6, "VIP+ #1", "vip+", 40000),
    (7, "Room #3", "normal", 30000),
]

KINDS = ("normal", "vip+", "vip++")

# Initial hourly prices. After the first run prices live in the
# rooms table (rooms.price) and are edited from the admin page.
ROOM_HOURLY_PRICES = {
    "Room #1": 30000,
    "Room #2": 30000,
    "VIP #1": 50000,
    "VIP #2": 50000
}

DEFAULT_PRICE = 30000
MAX_PRICE = 10_000_000

# Opening hours: open 17:00, close 04:00 the next morning
OPEN_HOUR = 17
CLOSE_HOUR = 4


# ==========================================
# BANK DETAILS
# ==========================================

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "star_karaoke_admin")

BANK_NAME = os.environ.get("BANK_NAME", "Голомт банк")
BANK_ACCOUNT = os.environ.get("BANK_ACCOUNT", "MN61001500 1505260219")
BANK_HOLDER = os.environ.get("BANK_HOLDER", "нарантуяа")


class ApiError(Exception):

    def __init__(self, code, msg):
        self.code = code
        self.msg = msg


def db():
    c = sqlite3.connect(
        DB,
        timeout=10,
        isolation_level=None
    )

    c.row_factory = sqlite3.Row
    c.execute("pragma foreign_keys=on")

    return c


def now():
    return datetime.now(
        timezone(timedelta(hours=8))
    ).replace(tzinfo=None)


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_bytes(16)

    h = hashlib.scrypt(
        pw.encode(),
        salt=salt,
        n=2**14,
        r=8,
        p=1
    )

    return salt.hex() + "$" + h.hex()


def check_pw(pw, stored):
    salt, h = stored.split("$")

    t = hashlib.scrypt(
        pw.encode(),
        salt=bytes.fromhex(salt),
        n=2**14,
        r=8,
        p=1
    )

    return hmac.compare_digest(t.hex(), h)


DUMMY = hash_pw("x")
hits = {}
hits_lock = threading.Lock()


def limited(key, limit, window):
    t = time.time()

    with hits_lock:
        # keep the dict from growing forever
        if len(hits) > 5000:
            for k in [
                k for k, v in hits.items()
                if not v or t - v[-1] > 3600
            ]:
                del hits[k]

        hits[key] = [
            x for x in hits.get(key, [])
            if t - x < window
        ]

        if len(hits[key]) >= limit:
            return True

        hits[key].append(t)

    return False


# ==========================================
# DATABASE INITIALIZATION
# ==========================================

def has_col(c, table, col):
    return any(
        r["name"] == col
        for r in c.execute("pragma table_info(" + table + ")")
    )


def init():
    c = db()

    c.executescript("""
        create table if not exists admins(
            id integer primary key,
            username text unique not null,
            pw text not null,
            role text not null check(role in('owner','staff')),
            created text
        );

        create table if not exists sessions(
            token text primary key,
            admin_id integer not null
                references admins(id) on delete cascade,
            expires real not null
        );

        create table if not exists rooms(
            id integer primary key,
            name text not null,
            capacity integer not null default 10,
            active integer not null default 1
        );

        create table if not exists bookings(
            id integer primary key,
            room_id integer not null references rooms(id),
            name text not null,
            phone text not null,
            guests integer not null,
            start text not null,
            end text not null,
            status text not null default 'pending',
            note text default '',
            created text not null
        );

        create index if not exists b_room
        on bookings(room_id, start);
    """)

    # migrations: price per room, price snapshot per booking
    if not has_col(c, "rooms", "price"):
        c.execute(
            "alter table rooms add column price integer "
            "not null default %d" % DEFAULT_PRICE
        )

        for name, price in ROOM_HOURLY_PRICES.items():
            c.execute(
                "update rooms set price=? where name=?",
                (price, name)
            )

    if not has_col(c, "bookings", "total"):
        c.execute("alter table bookings add column total integer")

    if not has_col(c, "bookings", "terms_accepted_at"):
        c.execute(
            "alter table bookings add column terms_accepted_at text"
        )
        c.execute(
            "alter table bookings add column terms_version text"
        )

    if not has_col(c, "bookings", "age_confirmed"):
        c.execute(
            "alter table bookings add column age_confirmed integer"
        )

    c.execute(
        """
        create table if not exists subscribers(
            id integer primary key,
            email text not null unique,
            token text not null unique,
            consent_at text not null,
            terms_version text,
            unsubscribed_at text
        )
        """
    )

    if not has_col(c, "rooms", "kind"):
        c.execute(
            "alter table rooms add column kind text "
            "not null default 'normal'"
        )

    if c.execute("pragma user_version").fetchone()[0] < 1:
        for rid, name, kind, price in ROOM_LAYOUT:
            if c.execute(
                "select 1 from rooms where id=?", (rid,)
            ).fetchone():
                c.execute(
                    "update rooms set name=?,kind=?,price=?,active=1 "
                    "where id=?",
                    (name, kind, price, rid)
                )
            else:
                c.execute(
                    "insert into rooms(id,name,kind,price) "
                    "values(?,?,?,?)",
                    (rid, name, kind, price)
                )

        c.execute("pragma user_version=1")

    # v2: rename rooms by type (VIP++ #1, Room #1, ...) once
    if c.execute("pragma user_version").fetchone()[0] < 2:
        for rid, name, kind, price in ROOM_LAYOUT:
            c.execute(
                "update rooms set name=? where id=?",
                (name, rid)
            )

        c.execute("pragma user_version=2")

    if not c.execute("select 1 from admins").fetchone():
        pw = (
            os.environ.get("ADMIN_PASSWORD")
            or secrets.token_urlsafe(9)
        )

        c.execute(
            """
            insert into admins(username,pw,role,created)
            values(?,?,'owner',?)
            """,
            (ADMIN_USERNAME, hash_pw(pw), now().isoformat())
        )

        print(
            "\n*** First owner account created: "
            "username '" + ADMIN_USERNAME + "', password '" + pw + "' ***"
        )

        print("*** Log in at /admin.html and change it. ***\n")

    c.close()


def clean(v, mx):
    v = str(v or "").strip()
    return v[:mx]


def conflict(c, room_id, start, end, skip=0):
    return c.execute(
        """
        select 1 from bookings
        where room_id=?
        and status!='cancelled'
        and id!=?
        and start<?
        and end>?
        """,
        (room_id, skip, end, start)
    ).fetchone() is not None


def to_int(v, msg):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(400, msg)


# ==========================================
# HTTP HANDLER
# ==========================================

class H(BaseHTTPRequestHandler):

    server_version = "Stars"

    # drop slow / stalled connections instead of hanging a thread forever
    timeout = 15

    def log_message(self, *a):
        pass

    def ip(self):
        if TRUST_PROXY and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[0].strip()

        return self.client_address[0]

    def out(self, code, data, ctype="application/json", extra=None):
        b = (
            data
            if isinstance(data, bytes)
            else json.dumps(data, ensure_ascii=False).encode()
        )

        self.send_response(code)

        texty = (
            ctype.startswith("text/")
            or ctype in (
                "application/json",
                "application/javascript",
                "image/svg+xml"
            )
        )

        self.send_header(
            "Content-Type",
            ctype + ("; charset=utf-8" if texty else "")
        )

        self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()"
        )
        # Only our own files may load or connect anywhere. This blocks
        # session-replay, analytics and ad scripts and Google Fonts.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )

        origin = self.headers.get("Origin")

        if origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization"
            )

            self.send_header(
                "Access-Control-Allow-Methods",
                "GET, POST, PATCH, DELETE, OPTIONS"
            )

        self.send_header(
            "Cache-Control",
            "no-store"
            if self.path.startswith("/api/")
            else "public, max-age=300"
        )

        self.end_headers()

        if self.command != "HEAD":
            self.wfile.write(b)

    def json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Буруу хүсэлт")

        # a negative length would make rfile.read() block forever
        if n < 0:
            raise ApiError(400, "Буруу хүсэлт")

        if n > 10000:
            raise ApiError(413, "Хэт том хүсэлт")

        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "Буруу өгөгдөл")

        if not isinstance(d, dict):
            raise ApiError(400, "Буруу өгөгдөл")

        return d

    def handle_any(self, method):
        u = urlparse(self.path)

        try:
            if u.path.startswith("/api/"):
                self.out(
                    *self.api(
                        method,
                        u.path[5:].strip("/").split("/"),
                        parse_qs(u.query)
                    )
                )

            elif method in ("GET", "HEAD"):
                self.static(u.path)

            else:
                raise ApiError(405, "Method not allowed")

        except ApiError as e:
            self.out(e.code, {"error": e.msg})

        except Exception as e:
            print("ERROR", repr(e))
            self.out(500, {"error": "Серверийн алдаа"})

    do_OPTIONS = lambda s: s.out(204, b"", "text/plain")
    do_GET = lambda s: s.handle_any("GET")
    do_HEAD = lambda s: s.handle_any("HEAD")
    do_POST = lambda s: s.handle_any("POST")
    do_PATCH = lambda s: s.handle_any("PATCH")
    do_DELETE = lambda s: s.handle_any("DELETE")

    def static(self, path):
        path = "/index.html" if path == "/" else unquote(path)

        try:
            full = os.path.realpath(
                os.path.join(PUBLIC, path.lstrip("/"))
            )
        except ValueError:
            return self.out(404, b"Not found", "text/plain")

        if (
            not full.startswith(PUBLIC + os.sep)
            or not os.path.isfile(full)
        ):
            return self.out(404, b"Not found", "text/plain")

        with open(full, "rb") as fh:
            body = fh.read()

        self.out(
            200,
            body,
            mimetypes.guess_type(full)[0]
            or "application/octet-stream"
        )

    def admin(self, c):
        header = self.headers.get("Authorization") or ""

        if not header.startswith("Bearer "):
            raise ApiError(401, "Нэвтэрнэ үү")

        token = header[7:]

        r = c.execute(
            """
            select a.id,a.username,a.role
            from sessions s
            join admins a on a.id=s.admin_id
            where s.token=? and s.expires>?
            """,
            (
                hashlib.sha256(token.encode()).hexdigest(),
                time.time()
            )
        ).fetchone()

        if not r:
            raise ApiError(401, "Нэвтэрнэ үү")

        return r

    def api(self, m, p, q):
        c = db()

        try:
            return self.routes(c, m, p, q)
        finally:
            c.close()

    # ==========================================
    # API ROUTES
    # ==========================================

    def routes(self, c, m, p, q):

        # PUBLIC: ROOMS

        if p == ["rooms"] and m == "GET":
            return 200, [
                dict(r)
                for r in c.execute(
                    """
                    select id,name,capacity,price,kind
                    from rooms
                    where active=1
                    order by id
                    """
                )
            ]

        # PUBLIC: AVAILABILITY

        if p == ["availability"] and m == "GET":

            try:
                day = datetime.strptime(
                    q["date"][0],
                    "%Y-%m-%d"
                )

                rid = int(q["room_id"][0])

            except (KeyError, ValueError, IndexError):
                raise ApiError(400, "Огноо эсвэл өрөө буруу")

            # one night = this day 04:00 -> next day 04:00
            s = day.strftime("%Y-%m-%d 04:00")

            e = (
                day + timedelta(days=1)
            ).strftime("%Y-%m-%d 04:00")

            rows = c.execute(
                """
                select start,end
                from bookings
                where room_id=?
                and status!='cancelled'
                and start<?
                and end>?
                order by start
                """,
                (rid, e, s)
            )

            return 200, [dict(r) for r in rows]

        # PUBLIC: CREATE BOOKING

        if p == ["bookings"] and m == "POST":

            if limited("book" + self.ip(), 8, 3600):
                raise ApiError(
                    429,
                    "Хэт олон хүсэлт. Түр хүлээнэ үү"
                )

            d = self.json()

            name = clean(d.get("name"), 80)
            phone = clean(d.get("phone"), 20)
            note = clean(d.get("note"), 300)

            if str(d.get("consent")) != "1":
                raise ApiError(
                    400,
                    "Үйлчилгээний нөхцөл, нууцлалын бодлогыг зөвшөөрнө үү"
                )

            if str(d.get("age21")) != "1":
                raise ApiError(
                    400,
                    "Захиалагч 21 ба түүнээс дээш настай байх ёстой"
                )

            if not name:
                raise ApiError(400, "Нэрээ оруулна уу")

            # optional e-mail, stored ONLY when marketing was ticked
            email = clean(d.get("email"), 120).lower()
            marketing = str(d.get("marketing")) == "1"

            if marketing and not re.fullmatch(
                r"[^@\s]{1,64}@[^@\s]{1,100}\.[^@\s]{2,}", email
            ):
                raise ApiError(400, "И-мэйл хаяг буруу байна")

            if not re.fullmatch(r"\+?[0-9 \-]{6,18}", phone):
                raise ApiError(400, "Утасны дугаар буруу байна")

            try:
                start = datetime.strptime(
                    f"{d.get('date')} {d.get('time')}",
                    "%Y-%m-%d %H:%M"
                )

                hours = int(d.get("hours"))
                guests = int(d.get("guests"))
                rid = int(d.get("room_id"))

            except (ValueError, TypeError):
                raise ApiError(400, "Огноо, цаг эсвэл тоо буруу байна")

            if not 1 <= hours <= 8:
                raise ApiError(400, "Хугацаа 1-8 цаг байна")

            # The chosen date is the night's START date. A time between
            # 00:00 and 03:59 therefore means the next calendar day
            # (e.g. date 11-10 + time 01:00 = 11-11 01:00).
            if start.hour < CLOSE_HOUR:
                start += timedelta(days=1)

            if start < now():
                raise ApiError(400, "Өнгөрсөн цагт захиалах боломжгүй")

            if start > now() + timedelta(days=180):
                raise ApiError(400, "Хэт хол огноо байна")

            end = start + timedelta(hours=hours)

            # OPENING HOURS: 17:00 -> 04:00 (next day) only.
            # A start between 00:00 and 03:59 belongs to the previous
            # evening's session. The booking must also finish by 04:00.
            if start.hour >= OPEN_HOUR:
                closing = (start + timedelta(days=1)).replace(
                    hour=CLOSE_HOUR, minute=0, second=0, microsecond=0
                )
            elif start.hour < CLOSE_HOUR:
                closing = start.replace(
                    hour=CLOSE_HOUR, minute=0, second=0, microsecond=0
                )
            else:
                raise ApiError(
                    400,
                    "Захиалгыг зөвхөн 17:00-04:00 цагийн хооронд авна"
                )

            if end > closing:
                raise ApiError(
                    400,
                    "Захиалга 04:00 цагаас хэтрэхгүй байх ёстой "
                    "(үйлчилгээ 17:00-04:00)"
                )

            c.execute("begin immediate")

            try:
                room = c.execute(
                    """
                    select *
                    from rooms
                    where id=? and active=1
                    """,
                    (rid,)
                ).fetchone()

                if not room:
                    raise ApiError(400, "Өрөө олдсонгүй")

                if not 1 <= guests <= room["capacity"]:
                    raise ApiError(
                        400,
                        f"Хүний тоо 1-{room['capacity']} байна"
                    )

                f = "%Y-%m-%d %H:%M"

                start_text = start.strftime(f)
                end_text = end.strftime(f)

                if conflict(c, rid, start_text, end_text):
                    raise ApiError(409, "Энэ цагт өрөө захиалгатай байна")

                hourly_price = room["price"]
                total_amount = hourly_price * hours

                cur = c.execute(
                    """
                    insert into bookings(
                        room_id,name,phone,guests,
                        start,end,note,created,total,
                        terms_accepted_at,terms_version,
                        age_confirmed
                    )
                    values(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        rid,
                        name,
                        phone,
                        guests,
                        start_text,
                        end_text,
                        note,
                        now().isoformat(),
                        total_amount,
                        now().isoformat(),
                        TERMS_VERSION,
                        1
                    )
                )

                booking_id = cur.lastrowid

                if marketing:
                    row = c.execute(
                        "select id from subscribers where email=?",
                        (email,)
                    ).fetchone()

                    if row is None:
                        c.execute(
                            "insert into subscribers"
                            "(email,token,consent_at,terms_version) "
                            "values(?,?,?,?)",
                            (
                                email,
                                secrets.token_urlsafe(24),
                                now().isoformat(),
                                TERMS_VERSION
                            )
                        )
                    else:
                        # they ticked the box again: opt back in
                        c.execute(
                            "update subscribers set unsubscribed_at=null,"
                            "consent_at=?,terms_version=? where id=?",
                            (now().isoformat(), TERMS_VERSION, row["id"])
                        )

                c.execute("commit")

            except BaseException:
                c.execute("rollback")
                raise

            return 201, {
                "id": booking_id,
                "status": "pending",
                "room": room["name"],
                "hours": hours,
                "hourly_price": hourly_price,
                "total_amount": total_amount,
                "bank_name": BANK_NAME,
                "bank_account": BANK_ACCOUNT,
                "bank_holder": BANK_HOLDER
            }

        # UNSUBSCRIBE (POST only, so mail scanners that pre-open links
        # cannot unsubscribe people; also works for List-Unsubscribe-Post)
        if p == ["unsubscribe"] and m == "POST":
            if limited("unsub" + self.ip(), 30, 3600):
                raise ApiError(
                    429,
                    "Хэт олон хүсэлт. Түр хүлээнэ үү"
                )

            token = (q.get("token") or [""])[0]

            if not token:
                token = str(self.json().get("token") or "")

            if 0 < len(token) <= 100:
                c.execute(
                    "update subscribers set unsubscribed_at=? "
                    "where token=? and unsubscribed_at is null",
                    (now().isoformat(), token)
                )

            return 200, {"ok": True}

        # ADMIN LOGIN

        if p == ["admin", "login"] and m == "POST":

            k = "login" + self.ip()

            with hits_lock:
                recent = list(hits.get(k, []))

            if len(recent) >= 5 and time.time() - recent[-1] < 600:
                raise ApiError(429, "Түр хүлээгээд дахин оролдоно уу")

            d = self.json()

            r = c.execute(
                "select * from admins where username=?",
                (clean(d.get("username"), 40),)
            ).fetchone()

            ok = check_pw(
                str(d.get("password") or ""),
                r["pw"] if r else DUMMY
            )

            if not (r and ok):
                limited(k, 99, 600)
                raise ApiError(401, "Нэр эсвэл нууц үг буруу")

            with hits_lock:
                hits.pop(k, None)

            c.execute(
                "delete from sessions where expires<?",
                (time.time(),)
            )

            tok = secrets.token_urlsafe(32)

            c.execute(
                "insert into sessions values(?,?,?)",
                (
                    hashlib.sha256(tok.encode()).hexdigest(),
                    r["id"],
                    time.time() + SESSION_HOURS * 3600
                )
            )

            return 200, {
                "token": tok,
                "username": r["username"],
                "role": r["role"]
            }

        # ADMIN ROUTES

        if p and p[0] == "admin":

            me = self.admin(c)
            owner = me["role"] == "owner"

            if p == ["admin", "me"] and m == "GET":
                return 200, dict(me)

            # BOOKINGS LIST

            if p == ["admin", "bookings"] and m == "GET":

                sql = """
                    select b.*, r.name room, r.price room_price
                    from bookings b
                    join rooms r on r.id=b.room_id
                    where 1=1
                """

                a = []

                if q.get("status") and q["status"][0] in STATUSES:
                    sql += " and b.status=?"
                    a.append(q["status"][0])

                if q.get("from"):
                    sql += " and b.start>=?"
                    a.append(q["from"][0][:10] + " 00:00")

                # past bookings (start before the given day)
                if q.get("before"):
                    sql += " and b.start<?"
                    a.append(q["before"][0][:10] + " 00:00")

                # newest request first (booking number = request order)
                rows = c.execute(
                    sql + " order by b.id desc limit 500",
                    a
                ).fetchall()

                result = []

                for r in rows:
                    booking = dict(r)

                    start_time = datetime.strptime(
                        booking["start"], "%Y-%m-%d %H:%M"
                    )

                    end_time = datetime.strptime(
                        booking["end"], "%Y-%m-%d %H:%M"
                    )

                    hours = round(
                        (end_time - start_time).total_seconds() / 3600
                    )

                    total = booking.get("total")

                    # bookings made before the price snapshot existed
                    if total is None:
                        total = hours * booking["room_price"]

                    booking["hours"] = hours
                    booking["hourly_price"] = (
                        total // hours if hours else booking["room_price"]
                    )
                    booking["total_amount"] = total

                    result.append(booking)

                return 200, result

            # UPDATE BOOKING STATUS

            if (
                len(p) == 3
                and p[1] == "bookings"
                and m == "PATCH"
            ):

                st = self.json().get("status")

                if st not in STATUSES:
                    raise ApiError(400, "Буруу төлөв")

                bid = to_int(p[2], "ID буруу")

                c.execute("begin immediate")

                try:
                    b = c.execute(
                        "select * from bookings where id=?",
                        (bid,)
                    ).fetchone()

                    if not b:
                        raise ApiError(404, "Олдсонгүй")

                    if (
                        st != "cancelled"
                        and conflict(
                            c,
                            b["room_id"],
                            b["start"],
                            b["end"],
                            b["id"]
                        )
                    ):
                        raise ApiError(409, "Цаг давхцаж байна")

                    c.execute(
                        "update bookings set status=? where id=?",
                        (st, b["id"])
                    )

                    c.execute("commit")

                except BaseException:
                    c.execute("rollback")
                    raise

                return 200, {"ok": True}

            # DELETE BOOKING

            if (
                len(p) == 3
                and p[1] == "bookings"
                and m == "DELETE"
            ):

                bid = to_int(p[2], "ID буруу")

                cur = c.execute(
                    "delete from bookings where id=?",
                    (bid,)
                )

                if cur.rowcount == 0:
                    raise ApiError(404, "Олдсонгүй")

                return 200, {"ok": True}

            # CHANGE PASSWORD

            if p == ["admin", "password"] and m == "POST":

                d = self.json()

                row = c.execute(
                    "select pw from admins where id=?",
                    (me["id"],)
                ).fetchone()

                if not check_pw(str(d.get("old") or ""), row["pw"]):
                    raise ApiError(400, "Хуучин нууц үг буруу")

                new_pw = str(d.get("new") or "")

                if len(new_pw) < 8:
                    raise ApiError(400, "Нууц үг хамгийн багадаа 8 тэмдэгт")

                c.execute(
                    "update admins set pw=? where id=?",
                    (hash_pw(new_pw), me["id"])
                )

                # log out every other device that used the old password
                current = hashlib.sha256(
                    (self.headers.get("Authorization") or "")[7:].encode()
                ).hexdigest()

                c.execute(
                    "delete from sessions where admin_id=? and token!=?",
                    (me["id"], current)
                )

                return 200, {"ok": True}

            # OWNER ONLY

            if not owner:
                raise ApiError(403, "Зөвхөн эзэмшигч хийх боломжтой")

            # ADMIN ACCOUNTS LIST

            if p == ["admin", "admins"] and m == "GET":

                return 200, [
                    dict(r)
                    for r in c.execute(
                        """
                        select id,username,role,created
                        from admins
                        order by id
                        """
                    )
                ]

            # CREATE ADMIN ACCOUNT

            if p == ["admin", "admins"] and m == "POST":

                d = self.json()

                u = clean(d.get("username"), 40)
                pw = str(d.get("password") or "")
                role = d.get("role", "staff")

                if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", u):
                    raise ApiError(400, "Нэр: 3-40 латин үсэг/тоо")

                if len(pw) < 8:
                    raise ApiError(400, "Нууц үг хамгийн багадаа 8 тэмдэгт")

                if role not in ("owner", "staff"):
                    raise ApiError(400, "Буруу эрх")

                try:
                    c.execute(
                        """
                        insert into admins(username,pw,role,created)
                        values(?,?,?,?)
                        """,
                        (u, hash_pw(pw), role, now().isoformat())
                    )

                except sqlite3.IntegrityError:
                    raise ApiError(409, "Энэ нэр бүртгэлтэй байна")

                return 201, {"ok": True}

            # DELETE ADMIN ACCOUNT

            if (
                len(p) == 3
                and p[1] == "admins"
                and m == "DELETE"
            ):

                admin_id = to_int(p[2], "ID буруу")

                if admin_id == me["id"]:
                    raise ApiError(400, "Өөрийгөө устгах боломжгүй")

                c.execute(
                    "delete from admins where id=?",
                    (admin_id,)
                )

                return 200, {"ok": True}

            # ROOMS LIST

            if p == ["admin", "rooms"] and m == "GET":

                return 200, [
                    dict(r)
                    for r in c.execute(
                        "select * from rooms order by id"
                    )
                ]

            # UPDATE ROOM

            if (
                len(p) == 3
                and p[1] == "rooms"
                and m == "PATCH"
            ):

                d = self.json()

                room_id = to_int(p[2], "ID буруу")

                r = c.execute(
                    "select * from rooms where id=?",
                    (room_id,)
                ).fetchone()

                if not r:
                    raise ApiError(404, "Олдсонгүй")

                cap = to_int(
                    d.get("capacity", r["capacity"]),
                    "Багтаамж буруу"
                )

                price = to_int(
                    d.get("price", r["price"]),
                    "Үнэ буруу"
                )

                if not 0 <= price <= MAX_PRICE:
                    raise ApiError(400, "Үнэ буруу")

                kind = d.get("kind", r["kind"])

                if kind not in KINDS:
                    raise ApiError(400, "Төрөл буруу")

                c.execute(
                    """
                    update rooms
                    set name=?,capacity=?,active=?,price=?,kind=?
                    where id=?
                    """,
                    (
                        clean(d.get("name", r["name"]), 60) or r["name"],
                        max(1, min(cap, 200)),
                        1 if d.get("active", r["active"]) else 0,
                        price,
                        kind,
                        r["id"]
                    )
                )

                return 200, {"ok": True}

        raise ApiError(404, "Олдсонгүй")


# ==========================================
# START SERVER
# ==========================================

if __name__ == "__main__":

    init()

    print(
        f"Stars Karaoke running on http://localhost:{PORT}"
    )

    ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        H
    ).serve_forever()