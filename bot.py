import re, math, base64, os, json, time, hmac, hashlib, sqlite3, asyncio, logging
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp, LabeledPrice, PreCheckoutQuery

try:
    import psycopg2
except ImportError:  # DATABASE_URL bo'lmasa kerak emas
    psycopg2 = None

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IntegrityErr = (sqlite3.IntegrityError,) + ((psycopg2.IntegrityError,) if psycopg2 else ())
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
URL = (os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
if not URL:
    raise SystemExit("WEBAPP_URL topilmadi: Render > Environment bo'limiga WEBAPP_URL qo'shing")
PORT = int(os.environ.get("PORT", 8080))
DB = os.environ.get("DB_PATH", "poletgram.db")
HERE = os.path.dirname(os.path.abspath(__file__))
TZ_OFFSET = float(os.environ.get("TZ_OFFSET_HOURS", 5))  # vaqt mintaqasi (O'zbekiston UTC+5)
MIN_WAIT = 10  # vazifani tekshirishdan oldin kutish (soniya)

START_TEXT = """🌐 Instagram va Telegram sahifalaringizni tekinga rivojlantiring ❤️‍🔥

Vazifalar bajarib, do'stlaringizni taklif qilib pul yig'ing va istaganingizga ega bo'ling 😍

Kanalimiz : @poletgram"""

# [nom, 100 ta uchun narx ($), tavsif, minimal miqdor, ikonka, tez kunda(1/0)]
SERVICES = {
    "Instagram": [
        ["Video Prasmotr", 0.05, "Instagram video va reellar uchun", 500, "play", 0],
        ["Video Share (Ulashish)", 0.15, "Video va reellarni ulashish", 10, "share", 0],
        ["Postga Like", 0.10, "Post va reellar uchun likelar", 10, "heart", 0],
        ["Profilga Obunachi", 0.30, "Profilingiz obunachilarini oshirish", 10, "user", 1],
    ],
    "Telegram": [
        ["Post Prasmotr", 0.04, "Telegram post va videolar uchun", 100, "play", 0],
        ["Post Reaksiya", 0.08, "Postlarga reaksiya", 10, "heart", 0],
        ["Kanalga Obunachi", 0.30, "Kanal obunachilarini oshirish", 10, "user", 1],
    ],
    "TikTok": [
        ["Video Prasmotr", 0.05, "TikTok videolar uchun", 500, "play", 0],
        ["Videoga Like", 0.12, "TikTok videolar uchun likelar", 10, "heart", 0],
        ["Video Rivoj", 0.30, "Videoni trendga chiqarish", 100, "share", 0],
        ["Profilga Obunachi", 0.35, "Profilingiz obunachilarini oshirish", 10, "user", 1],
    ],
}
# Balans to'ldirish paketlari. 1000 $ = 20 000 so'm. Stars narxi: so'm / SOM_PER_STAR
# (Telegram'da 1 Star ≈ $0.02 ≈ 250 so'm). Kerak bo'lsa SOM_PER_STAR muhit o'zgaruvchisi bilan o'zgartiring.
SOM_PER_STAR = float(os.environ.get("SOM_PER_STAR", 250))
PACKAGES = [
    {"amount": 500, "bonus": 0, "tag": "std"},
    {"amount": 1000, "bonus": 100, "tag": "bonus"},
    {"amount": 2000, "bonus": 500, "tag": "bonus"},
    {"amount": 4000, "bonus": 1500, "tag": "hot"},
    {"amount": 8000, "bonus": 8000, "tag": "vip"},
]


def stars_of(p):
    return max(1, math.ceil(p["amount"] * 20 / SOM_PER_STAR))


def pkg_list():
    return [dict(p, total=p["amount"] + p["bonus"], stars=stars_of(p), som=p["amount"] * 20) for p in PACKAGES]


TYPES = {"link", "youtube", "ad", "bot", "bonus"}

bot = Bot(TOKEN)
dp = Dispatcher()
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY,username TEXT,name TEXT,balance DOUBLE PRECISION DEFAULT 0,ref_by BIGINT,ref_paid INTEGER DEFAULT 0,created BIGINT);
CREATE TABLE IF NOT EXISTS tasks(id BIGSERIAL PRIMARY KEY,type TEXT,title TEXT,sub TEXT,url TEXT,reward DOUBLE PRECISION,active INTEGER DEFAULT 1,daily INTEGER DEFAULT 0);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS daily INTEGER DEFAULT 0;
CREATE TABLE IF NOT EXISTS tlog(user_id BIGINT,task_id BIGINT,dayn BIGINT,n INTEGER,PRIMARY KEY(user_id,task_id,dayn));
CREATE TABLE IF NOT EXISTS done(user_id BIGINT,task_id BIGINT,ts BIGINT,PRIMARY KEY(user_id,task_id));
CREATE TABLE IF NOT EXISTS started(user_id BIGINT,task_id BIGINT,ts BIGINT,PRIMARY KEY(user_id,task_id));
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY,amount DOUBLE PRECISION,uses INTEGER);
CREATE TABLE IF NOT EXISTS promo_used(user_id BIGINT,code TEXT,PRIMARY KEY(user_id,code));
CREATE TABLE IF NOT EXISTS orders(id BIGSERIAL PRIMARY KEY,user_id BIGINT,platform TEXT,service TEXT,link TEXT,qty BIGINT,cost DOUBLE PRECISION,ts BIGINT,status TEXT DEFAULT 'new');
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY,user_id BIGINT,stars INTEGER,amount DOUBLE PRECISION,ts BIGINT);
CREATE INDEX IF NOT EXISTS users_ref ON users(ref_by);
CREATE INDEX IF NOT EXISTS users_uname ON users(lower(username));
"""

UPSERT = {"settings": ("k", ["v"]), "promos": ("code", ["amount", "uses"])}


class Row(dict):
    """Ham nom (r["x"]), ham indeks (r[0]) bilan ishlaydi."""

    def __init__(self, names, vals):
        super().__init__(zip(names, vals))
        self._v = tuple(vals)

    def __getitem__(self, k):
        return self._v[k] if isinstance(k, int) else dict.__getitem__(self, k)


class Cur:
    def __init__(self, cur):
        self.rowcount = cur.rowcount
        self.lastrowid = None
        self._rows = []
        if cur.description:
            names = [d[0] for d in cur.description]
            self._rows = [Row(names, r) for r in cur.fetchall()]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class PG:
    """sqlite3 ga o'xshash interfeys: Postgres (Neon) uchun."""

    def __init__(self, url):
        self.url = url
        self.conn = None
        self.dirty = False
        self._connect()

    def _connect(self):
        self.conn = psycopg2.connect(self.url, connect_timeout=15, keepalives=1,
                                     keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
        self.dirty = False

    @staticmethod
    def _tr(sql):
        s = sql.strip()
        m = re.match(r"INSERT OR REPLACE INTO (\w+)", s)
        if m:
            pk, cols = UPSERT[m.group(1)]
            s = "INSERT INTO" + s[len("INSERT OR REPLACE INTO"):] + f" ON CONFLICT ({pk}) DO UPDATE SET " + \
                ",".join(f"{c}=EXCLUDED.{c}" for c in cols)
        elif s.startswith("INSERT OR IGNORE INTO"):
            s = "INSERT INTO" + s[len("INSERT OR IGNORE INTO"):] + " ON CONFLICT DO NOTHING"
        elif re.match(r"INSERT INTO (tasks|orders)\b", s):
            s += " RETURNING id"
        return s.replace("?", "%s")

    def execute(self, sql, params=()):
        q = self._tr(sql)
        write = not q.upper().startswith("SELECT")
        for attempt in (0, 1):
            cur = None
            try:
                cur = self.conn.cursor()
                cur.execute(q, tuple(params))
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if attempt:
                    raise
                logging.warning("DB aloqasi uzildi, qayta ulanyapman")
                try:
                    self.conn.close()
                except Exception:
                    pass
                self._connect()
            except Exception:
                self.conn.rollback()
                self.dirty = False
                raise
        res = Cur(cur)
        cur.close()
        if q.endswith("RETURNING id") and res._rows:
            res.lastrowid = res._rows[0][0]
        if write:
            self.dirty = True
        elif not self.dirty:
            self.conn.commit()
        return res

    def commit(self):
        self.conn.commit()
        self.dirty = False


if DATABASE_URL:
    if psycopg2 is None:
        raise SystemExit("psycopg2-binary o'rnatilmagan: requirements.txt ni yangilang")
    db = PG(DATABASE_URL)
    _c = db.conn.cursor()
    _c.execute(PG_SCHEMA)
    db.conn.commit()
    _c.close()
    print("Baza: Postgres (DATABASE_URL)")
else:
    db = sqlite3.connect(DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT,name TEXT,balance REAL DEFAULT 0,ref_by INTEGER,ref_paid INTEGER DEFAULT 0,created INTEGER);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,title TEXT,sub TEXT,url TEXT,reward REAL,active INTEGER DEFAULT 1,daily INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tlog(user_id INTEGER,task_id INTEGER,dayn INTEGER,n INTEGER,PRIMARY KEY(user_id,task_id,dayn));
CREATE TABLE IF NOT EXISTS done(user_id INTEGER,task_id INTEGER,ts INTEGER,PRIMARY KEY(user_id,task_id));
CREATE TABLE IF NOT EXISTS started(user_id INTEGER,task_id INTEGER,ts INTEGER,PRIMARY KEY(user_id,task_id));
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY,amount REAL,uses INTEGER);
CREATE TABLE IF NOT EXISTS promo_used(user_id INTEGER,code TEXT,PRIMARY KEY(user_id,code));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,platform TEXT,service TEXT,link TEXT,qty INTEGER,cost REAL,ts INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT);
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY,user_id INTEGER,stars INTEGER,amount REAL,ts INTEGER);
""")
    try:
        db.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'new'")
        db.commit()
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE tasks ADD COLUMN daily INTEGER DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass
    print("Baza: SQLite (" + DB + ")")


def setting(k, d):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else d


def touch(u, ref=None):
    name = "@" + u["username"] if u.get("username") else (u.get("first_name") or "User")
    r = db.execute("SELECT id FROM users WHERE id=?", (u["id"],)).fetchone()
    new = None
    if r is None:
        if ref == u["id"] or not db.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
            ref = None
        db.execute("INSERT INTO users(id,username,name,balance,ref_by,created) VALUES(?,?,?,0,?,?)",
                   (u["id"], u.get("username"), name, ref, int(time.time())))
        new = ref
    else:
        db.execute("UPDATE users SET username=?,name=? WHERE id=?", (u.get("username"), name, u["id"]))
    db.commit()
    return new


# ---------- Telegram initData tekshiruvi ----------
def check(init):
    try:
        d = dict(parse_qsl(init, keep_blank_values=True))
        h = d.pop("hash", "")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d["auth_date"]) > 86400:
            return None
        u = json.loads(d["user"])
        u["_sp"] = d.get("start_param", "")
        return u
    except Exception:
        return None


async def safe_send(uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


def tell(uid, text):
    asyncio.create_task(safe_send(uid, text))


def jr(d, s=200):
    return web.json_response(d, status=s)


def need(req):
    u = check(req.headers.get("X-Init", ""))
    if not u:
        raise web.HTTPUnauthorized(text='{"error":"Ilovani Telegram orqali oching"}', content_type="application/json")
    sp = u.get("_sp", "")
    ref = int(sp[4:]) if sp.startswith("ref_") and sp[4:].isdigit() else None
    r = touch(u, ref)
    if r:
        tell(r, "👋 Do'stingiz qo'shildi! U birinchi vazifani bajarganda mukofot olasiz.")
    return u["id"]


async def body(req):
    try:
        return await req.json()
    except Exception:
        return {}


def me(uid):
    r = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c = db.execute("SELECT COUNT(*) t,COALESCE(SUM(ref_paid),0) a FROM users WHERE ref_by=?", (uid,)).fetchone()
    return {"id": r["id"], "name": r["name"], "balance": round(r["balance"], 2), "refs": c["t"], "active": c["a"]}


def local_now():
    return time.time() + TZ_OFFSET * 3600


def today():
    return int(local_now() // 86400)


def reset_in():
    """Kun yangilanishigacha qolgan soniyalar (har kuni yarim tunda)."""
    return int(86400 - local_now() % 86400)


def task_count(uid, t):
    r = db.execute("SELECT n FROM tlog WHERE user_id=? AND task_id=? AND dayn=?", (uid, t["id"], today())).fetchone()
    return r["n"] if r else 0


def task_blocked(uid, t):
    if t["daily"]:
        if task_count(uid, t) >= t["daily"]:
            return "Bugungi limit tugadi. Ertaga qayta urinib ko'ring"
    elif db.execute("SELECT 1 FROM done WHERE user_id=? AND task_id=?", (uid, t["id"])).fetchone():
        return "Bu vazifa allaqachon bajarilgan"
    return None


def get_task(i):
    return db.execute("SELECT * FROM tasks WHERE id=? AND active=1", (i,)).fetchone()


async def notify_admins(text):
    for a in ADMINS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


async def pay_ref(uid):
    r = db.execute("SELECT ref_by,ref_paid FROM users WHERE id=?", (uid,)).fetchone()
    if r["ref_by"] and not r["ref_paid"]:
        amt = float(setting("ref", "20"))
        db.execute("UPDATE users SET ref_paid=1 WHERE id=?", (uid,))
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, r["ref_by"]))
        db.commit()
        try:
            await bot.send_message(r["ref_by"], f"🎉 Do'stingiz faol bo'ldi! Balansingizga +{amt:g} $ qo'shildi.")
        except Exception:
            pass


# ---------- API ----------
routes = web.RouteTableDef()


@routes.post("/api/init")
async def api_init(req):
    uid = need(req)
    done = {r[0] for r in db.execute("SELECT task_id FROM done WHERE user_id=?", (uid,))}
    cnt = {r[0]: r[1] for r in db.execute("SELECT task_id,n FROM tlog WHERE user_id=? AND dayn=?", (uid, today()))}
    ts = [{"id": t["id"], "type": t["type"], "title": t["title"], "sub": t["sub"], "reward": t["reward"],
           "daily": t["daily"] or 0, "cnt": cnt.get(t["id"], 0),
           "done": (cnt.get(t["id"], 0) >= t["daily"]) if t["daily"] else (t["id"] in done)}
          for t in db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id")]
    return jr({"me": me(uid), "tasks": ts, "svc": SERVICES, "ref": float(setting("ref", "20")),
                "reset_in": reset_in(), "admin": uid in ADMINS, "pkgs": pkg_list(), "ui": json.loads(setting("ui", "{}")),
                "bgv": setting("bg_ver", ""), "theme": json.loads(setting("theme", "{}"))})


@routes.post("/api/task/start")
async def api_start(req):
    uid = need(req)
    t = get_task((await body(req)).get("id"))
    if not t:
        return jr({"error": "Vazifa topilmadi"}, 404)
    err = task_blocked(uid, t)
    if err:
        return jr({"error": err}, 400)
    db.execute("INSERT OR IGNORE INTO started VALUES(?,?,?)", (uid, t["id"], int(time.time())))
    db.commit()
    return jr({"url": t["url"]})


@routes.post("/api/task/claim")
async def api_claim(req):
    uid = need(req)
    t = get_task((await body(req)).get("id"))
    if not t:
        return jr({"error": "Vazifa topilmadi"}, 404)
    err = task_blocked(uid, t)
    if err:
        return jr({"error": err}, 400)
    if t["type"] != "bonus":
        s = db.execute("SELECT ts FROM started WHERE user_id=? AND task_id=?", (uid, t["id"])).fetchone()
        if not s or time.time() - s["ts"] < MIN_WAIT:
            return jr({"error": "Avval vazifani bajaring (kamida 10 soniya)"}, 400)
    err = task_blocked(uid, t)
    if err:
        return jr({"error": err}, 400)
    if t["daily"]:
        if task_count(uid, t):
            db.execute("UPDATE tlog SET n=n+1 WHERE user_id=? AND task_id=? AND dayn=?", (uid, t["id"], today()))
        else:
            db.execute("INSERT INTO tlog VALUES(?,?,?,1)", (uid, t["id"], today()))
    else:
        try:
            db.execute("INSERT INTO done VALUES(?,?,?)", (uid, t["id"], int(time.time())))
        except IntegrityErr:
            return jr({"error": "Bu vazifa allaqachon bajarilgan"}, 400)
    db.execute("DELETE FROM started WHERE user_id=? AND task_id=?", (uid, t["id"]))
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (t["reward"], uid))
    db.commit()
    await pay_ref(uid)
    return jr({"me": me(uid)})


@routes.post("/api/order")
async def api_order(req):
    uid = need(req)
    b = await body(req)
    try:
        it = next(x for x in SERVICES[b["platform"]] if x[0] == b["service"])
        qty = int(b["qty"])
    except Exception:
        return jr({"error": "Noto'g'ri so'rov"}, 400)
    if it[5]:
        return jr({"error": "Bu xizmat tez kunda ishga tushadi"}, 400)
    price = it[1]
    link = str(b.get("link", "")).strip()[:300]
    if qty < it[3] or qty > 1000000 or not link:
        return jr({"error": f"Havolani kiriting. Minimal miqdor: {it[3]}"}, 400)
    cost = max(0.01, round(qty / 100 * price, 2))
    if db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (cost, uid, cost)).rowcount == 0:
        return jr({"error": "Balans yetarli emas"}, 400)
    db.execute("INSERT INTO orders(user_id,platform,service,link,qty,cost,ts) VALUES(?,?,?,?,?,?,?)",
               (uid, b["platform"], b["service"], link, qty, cost, int(time.time())))
    db.commit()
    u = me(uid)
    await notify_admins(f"🛒 Yangi buyurtma\n{u['name']} (ID {uid})\n{b['platform']} · {b['service']} × {qty}\n{link}\nNarx: {cost} $")
    return jr({"me": u})


@routes.post("/api/send")
async def api_send(req):
    uid = need(req)
    b = await body(req)
    try:
        amt = round(float(b.get("amount", 0)), 2)
    except Exception:
        amt = 0
    to = str(b.get("to", "")).strip().lstrip("@").lower()
    r = db.execute("SELECT id FROM users WHERE lower(username)=?", (to,)).fetchone()
    if not r:
        return jr({"error": "Foydalanuvchi topilmadi (u botdan foydalangan bo'lishi kerak)"}, 400)
    if r["id"] == uid or amt <= 0:
        return jr({"error": "Noto'g'ri summa yoki qabul qiluvchi"}, 400)
    if db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amt, uid, amt)).rowcount == 0:
        return jr({"error": "Balans yetarli emas"}, 400)
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, r["id"]))
    db.commit()
    try:
        await bot.send_message(r["id"], f"💸 Sizga {me(uid)['name']} dan {amt:g} $ o'tkazildi.")
    except Exception:
        pass
    return jr({"me": me(uid)})


@routes.post("/api/promo")
async def api_promo(req):
    uid = need(req)
    code = str((await body(req)).get("code", "")).strip().upper()
    p = db.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
    if not p or p["uses"] <= 0:
        return jr({"error": "Promokod noto'g'ri yoki tugagan"}, 400)
    try:
        db.execute("INSERT INTO promo_used VALUES(?,?)", (uid, code))
    except IntegrityErr:
        return jr({"error": "Bu promokodni ishlatgansiz"}, 400)
    db.execute("UPDATE promos SET uses=uses-1 WHERE code=?", (code,))
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (p["amount"], uid))
    db.commit()
    return jr({"me": me(uid), "amount": p["amount"]})


@routes.post("/api/top")
async def api_top(req):
    need(req)
    rows = db.execute("SELECT u.name n,COUNT(r.id) c FROM users u JOIN users r ON r.ref_by=u.id AND r.ref_paid=1 "
                      "GROUP BY u.id ORDER BY c DESC LIMIT 10").fetchall()
    return jr({"top": [{"n": r["n"], "c": r["c"]} for r in rows]})


HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_ui(raw):
    if not isinstance(raw, dict):
        raise ValueError
    out = {}
    for k, c in list(raw.items())[:80]:
        if not isinstance(c, dict) or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", str(k)):
            continue
        o = {}
        if isinstance(c.get("label"), str) and c["label"].strip():
            o["label"] = c["label"].strip()[:40]
        for f in ("bg", "bg2", "fg"):
            if isinstance(c.get(f), str) and HEX.match(c[f]):
                o[f] = c[f]
        for f, lo, hi in (("z", 40, 200), ("r", 0, 60), ("o", -100, 100)):
            if c.get(f) is not None:
                try:
                    o[f] = max(lo, min(hi, int(c[f])))
                except (TypeError, ValueError):
                    pass
        if c.get("hide") is True:
            o["hide"] = True
        if o:
            out[str(k)] = o
    return out


THEME_KEYS = {"a1", "a2", "a3", "s1", "s2", "s3", "s4", "b1", "b2", "sh", "tx", "tx2", "tx3", "ni", "pbg", "hd", "on", "on2"}


def clean_theme(raw):
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in THEME_KEYS and isinstance(v, str) and HEX.match(v)}


def admin_data():
    u = db.execute("SELECT COUNT(*) c,COALESCE(SUM(balance),0) b FROM users").fetchone()
    return {
        "stats": {"users": u["c"], "bal": round(u["b"], 2), "orders": db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]},
        "tasks": [dict(id=x["id"], type=x["type"], title=x["title"], sub=x["sub"], url=x["url"], reward=x["reward"], daily=x["daily"] or 0)
                  for x in db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id")],
        "promos": [dict(code=x["code"], amount=x["amount"], uses=x["uses"]) for x in db.execute("SELECT * FROM promos")],
        "orders": [dict(id=x["id"], user=x["user_id"], platform=x["platform"], service=x["service"], link=x["link"],
                        qty=x["qty"], cost=x["cost"], status=x["status"])
                   for x in db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20")],
        "ref": float(setting("ref", "20")),
        "ui": json.loads(setting("ui", "{}")),
        "bgv": setting("bg_ver", ""),
    }


async def broadcast(txt):
    for r in db.execute("SELECT id FROM users").fetchall():
        await safe_send(r["id"], txt)
        await asyncio.sleep(0.05)


@routes.post("/api/admin/{act}")
async def api_admin(req):
    uid = need(req)
    if uid not in ADMINS:
        return jr({"error": "Ruxsat yo'q"}, 403)
    act = req.match_info["act"]
    b = await body(req)
    try:
        if act == "data":
            pass
        elif act == "task_add":
            typ = str(b.get("type"))
            if typ not in TYPES or not str(b.get("title", "")).strip():
                raise ValueError
            daily = max(0, min(1000, int(b.get("daily") or 0)))
            db.execute("INSERT INTO tasks(type,title,sub,url,reward,daily) VALUES(?,?,?,?,?,?)",
                       (typ, str(b["title"]).strip()[:100], str(b.get("sub", ""))[:150], str(b.get("url", "")).strip()[:300],
                        float(b["reward"]), daily))
        elif act == "task_del":
            db.execute("UPDATE tasks SET active=0 WHERE id=?", (int(b["id"]),))
        elif act == "promo_add":
            db.execute("INSERT OR REPLACE INTO promos VALUES(?,?,?)", (str(b["code"]).strip().upper(), float(b["amount"]), int(b["uses"])))
        elif act == "promo_del":
            db.execute("DELETE FROM promos WHERE code=?", (str(b["code"]),))
        elif act == "ref":
            db.execute("INSERT OR REPLACE INTO settings VALUES('ref',?)", (str(float(b["amount"])),))
        elif act == "bal":
            who = str(b["user"]).strip().lstrip("@")
            r = db.execute("SELECT id FROM users WHERE id=? OR lower(username)=?", (int(who) if who.isdigit() else -1, who.lower())).fetchone()
            if not r:
                return jr({"error": "Foydalanuvchi topilmadi"}, 400)
            db.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(b["amount"]), r["id"]))
        elif act == "order_done":
            o = db.execute("SELECT * FROM orders WHERE id=?", (int(b["id"]),)).fetchone()
            if o and o["status"] != "done":
                db.execute("UPDATE orders SET status='done' WHERE id=?", (o["id"],))
                tell(o["user_id"], f"✅ Buyurtmangiz bajarildi: {o['platform']} · {o['service']} × {o['qty']}")
        elif act == "ui_save":
            db.execute("INSERT OR REPLACE INTO settings VALUES('ui',?)", (json.dumps(clean_ui(b.get("ui"))),))
        elif act == "ui_reset":
            db.execute("DELETE FROM settings WHERE k='ui'")
        elif act == "bg_save":
            img = str(b.get("img", ""))
            m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$", img)
            if not m or len(img) > 700000:
                raise ValueError
            if len(base64.b64decode(m.group(2), validate=True)) > 520000:
                raise ValueError
            for k, v in (("bg_img", m.group(2)), ("bg_mime", m.group(1)), ("bg_ver", str(int(time.time()))),
                         ("theme", json.dumps(clean_theme(b.get("theme"))))):
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
        elif act == "bg_reset":
            db.execute("DELETE FROM settings WHERE k IN ('bg_img','bg_mime','bg_ver','theme')")
        elif act == "post":
            txt = str(b.get("text", "")).strip()
            if not txt:
                raise ValueError
            asyncio.create_task(broadcast(txt))
        else:
            return jr({"error": "Noma'lum amal"}, 400)
    except (KeyError, ValueError, TypeError):
        return jr({"error": "Ma'lumotlar noto'g'ri"}, 400)
    db.commit()
    return jr({"data": admin_data()})


@routes.post("/api/topup")
async def api_topup(req):
    uid = need(req)
    try:
        i = int((await body(req))["pkg"])
        p = PACKAGES[i]
    except Exception:
        return jr({"error": "Noto'g'ri paket"}, 400)
    stars = stars_of(p)
    try:
        url = await bot.create_invoice_link(
            title=f"{p['amount']:,} $ balans",
            description=f"Poletgram balansini to'ldirish: {p['amount'] + p['bonus']:,} $ ({stars} Stars)",
            payload=f"pkg:{i}:{uid}", provider_token="", currency="XTR",
            prices=[LabeledPrice(label=f"{p['amount']:,} $", amount=stars)])
    except Exception:
        logging.exception("invoice")
        return jr({"error": "To'lovni yaratib bo'lmadi"}, 500)
    return jr({"url": url})


async def bg_img(req):
    r = db.execute("SELECT v FROM settings WHERE k='bg_img'").fetchone()
    if not r:
        return web.Response(status=404)
    return web.Response(body=base64.b64decode(r["v"]), content_type=setting("bg_mime", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def health(req):
    return web.Response(text="ok")


async def keepalive():
    import aiohttp
    await asyncio.sleep(60)
    while True:
        try:
            async with aiohttp.ClientSession() as ses:
                await ses.get(URL + "/health", timeout=aiohttp.ClientTimeout(total=20))
        except Exception:
            pass
        await asyncio.sleep(540)


async def index(req):
    return web.FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-store"})


# ---------- Bot ----------
@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    a = command.args or ""
    ref = int(a[4:]) if a.startswith("ref_") and a[4:].isdigit() else None
    r = touch({"id": m.from_user.id, "username": m.from_user.username, "first_name": m.from_user.first_name}, ref)
    if r:
        tell(r, "👋 Do'stingiz qo'shildi! U birinchi vazifani bajarganda mukofot olasiz.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Poletgram'ni ochish", web_app=WebAppInfo(url=URL))]])
    await m.answer(START_TEXT, reply_markup=kb)


def is_admin(m):
    return m.from_user.id in ADMINS


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛠 Admin panel", web_app=WebAppInfo(url=URL))]])
    await m.answer("Admin panel mini ilovaning Profil bo'limida ham bor.", reply_markup=kb)


@dp.message(Command("addtask"))
async def cmd_addtask(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    p = [x.strip() for x in (command.args or "").split("|")]
    try:
        typ, title, sub, url, rew = p[0].lower(), p[1], p[2], p[3], float(p[4])
        daily = int(p[5]) if len(p) > 5 and p[5] else 0
        assert typ in TYPES and title
    except Exception:
        return await m.answer("Format: /addtask tur | sarlavha | tavsif | havola | mukofot\nTurlar: link, youtube, ad, bot, bonus")
    cur = db.execute("INSERT INTO tasks(type,title,sub,url,reward,daily) VALUES(?,?,?,?,?,?)", (typ, title, sub, url, rew, daily))
    db.commit()
    await m.answer(f"✅ Vazifa qo'shildi. ID: {cur.lastrowid}")


@dp.message(Command("tasks"))
async def cmd_tasks(m: Message):
    if not is_admin(m):
        return
    rows = db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id").fetchall()
    await m.answer("\n\n".join(f"#{t['id']} [{t['type']}] {t['title']}\n{t['reward']:g} $ · {t['url']}" for t in rows) or "Vazifalar yo'q",
                   disable_web_page_preview=True)


@dp.message(Command("deltask"))
async def cmd_deltask(m: Message, command: CommandObject):
    if is_admin(m) and (command.args or "").strip().isdigit():
        db.execute("UPDATE tasks SET active=0 WHERE id=?", (int(command.args),))
        db.commit()
        await m.answer("🗑 O'chirildi")


@dp.message(Command("addpromo"))
async def cmd_addpromo(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    try:
        c, a, n = (command.args or "").split()
        db.execute("INSERT OR REPLACE INTO promos VALUES(?,?,?)", (c.upper(), float(a), int(n)))
        db.commit()
        await m.answer(f"✅ {c.upper()} — {a} $ × {n} marta")
    except Exception:
        await m.answer("Format: /addpromo KOD summa limit")


@dp.message(Command("setref"))
async def cmd_setref(m: Message, command: CommandObject):
    try:
        v = float(command.args)
    except Exception:
        return
    if is_admin(m):
        db.execute("INSERT OR REPLACE INTO settings VALUES('ref',?)", (str(v),))
        db.commit()
        await m.answer(f"✅ Do'st uchun mukofot: {v:g} $")


@dp.message(Command("addbal"))
async def cmd_addbal(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    try:
        uid, a = (command.args or "").split()
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (float(a), int(uid)))
        db.commit()
        await m.answer("✅ Bajarildi")
    except Exception:
        await m.answer("Format: /addbal user_id summa")


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_admin(m):
        return
    u = db.execute("SELECT COUNT(*) c,COALESCE(SUM(balance),0) b FROM users").fetchone()
    o = db.execute("SELECT COUNT(*) c FROM orders").fetchone()
    await m.answer(f"👥 Foydalanuvchilar: {u['c']}\n💰 Umumiy balans: {u['b']:.2f} $\n🛒 Buyurtmalar: {o['c']}")


@dp.message(Command("post"))
async def cmd_post(m: Message, command: CommandObject):
    if not is_admin(m) or not command.args:
        return
    ok = 0
    for r in db.execute("SELECT id FROM users").fetchall():
        try:
            await bot.send_message(r["id"], command.args)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ {ok} ta foydalanuvchiga yuborildi")


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    try:
        _, i, uid = sp.invoice_payload.split(":")
        p = PACKAGES[int(i)]
        ok = sp.currency == "XTR" and sp.total_amount == stars_of(p) and int(uid) == m.from_user.id
    except Exception:
        ok = False
    if not ok:
        await notify_admins(f"⚠️ Shubhali to'lov: {sp.invoice_payload} · {sp.total_amount} {sp.currency} · {m.from_user.id}")
        return
    total = p["amount"] + p["bonus"]
    try:
        db.execute("INSERT INTO payments VALUES(?,?,?,?,?)",
                   (sp.telegram_payment_charge_id, m.from_user.id, sp.total_amount, total, int(time.time())))
    except IntegrityErr:
        return
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (total, m.from_user.id))
    db.commit()
    await m.answer(f"✅ To'lov qabul qilindi! Balansingizga +{total:,} $ qo'shildi.")
    await notify_admins(f"💳 To'lov: {m.from_user.full_name} (ID {m.from_user.id}) · {sp.total_amount} ⭐ → {total:,} $")


async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.add_routes(routes)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/bg", bg_img)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Poletgram", web_app=WebAppInfo(url=URL)))
    asyncio.create_task(keepalive())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
