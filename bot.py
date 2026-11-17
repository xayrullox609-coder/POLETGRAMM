import os, json, time, hmac, hashlib, sqlite3, asyncio, logging
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
URL = os.environ["WEBAPP_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 8080))
DB = os.environ.get("DB_PATH", "poletgram.db")
HERE = os.path.dirname(os.path.abspath(__file__))
MIN_WAIT = 10  # vazifani tekshirishdan oldin kutish (soniya)

START_TEXT = """🌐 Instagram va Telegram sahifalaringizni tekinga rivojlantiring ❤️‍🔥

Vazifalar bajarib, do'stlaringizni taklif qilib pul yig'ing va istaganingizga ega bo'ling 😍

Kanalimiz : @poletgram"""

SERVICES = {
    "Instagram": [["Prasmotr", 0.05], ["Like", 0.10], ["Share", 0.15]],
    "Telegram": [["Prasmotr", 0.04], ["Reaksiya", 0.08]],
    "TikTok": [["Prasmotr", 0.05], ["Like", 0.12], ["Video rivoj", 0.30]],
}
TYPES = {"link", "youtube", "ad", "bot", "bonus"}

bot = Bot(TOKEN)
dp = Dispatcher()
db = sqlite3.connect(DB, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT,name TEXT,balance REAL DEFAULT 0,ref_by INTEGER,ref_paid INTEGER DEFAULT 0,created INTEGER);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,title TEXT,sub TEXT,url TEXT,reward REAL,active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS done(user_id INTEGER,task_id INTEGER,ts INTEGER,PRIMARY KEY(user_id,task_id));
CREATE TABLE IF NOT EXISTS started(user_id INTEGER,task_id INTEGER,ts INTEGER,PRIMARY KEY(user_id,task_id));
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY,amount REAL,uses INTEGER);
CREATE TABLE IF NOT EXISTS promo_used(user_id INTEGER,code TEXT,PRIMARY KEY(user_id,code));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,platform TEXT,service TEXT,link TEXT,qty INTEGER,cost REAL,ts INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT);
""")


def setting(k, d):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else d


def touch(u, ref=None):
    name = "@" + u["username"] if u.get("username") else (u.get("first_name") or "User")
    r = db.execute("SELECT id FROM users WHERE id=?", (u["id"],)).fetchone()
    if r is None:
        if ref == u["id"] or not db.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
            ref = None
        db.execute("INSERT INTO users(id,username,name,balance,ref_by,created) VALUES(?,?,?,0,?,?)",
                   (u["id"], u.get("username"), name, ref, int(time.time())))
    else:
        db.execute("UPDATE users SET username=?,name=? WHERE id=?", (u.get("username"), name, u["id"]))
    db.commit()


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
        return json.loads(d["user"])
    except Exception:
        return None


def jr(d, s=200):
    return web.json_response(d, status=s)


def need(req):
    u = check(req.headers.get("X-Init", ""))
    if not u:
        raise web.HTTPUnauthorized(text='{"error":"Ilovani Telegram orqali oching"}', content_type="application/json")
    touch(u)
    return u["id"]


async def body(req):
    try:
        return await req.json()
    except Exception:
        return {}


def me(uid):
    r = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return {"id": r["id"], "name": r["name"], "balance": round(r["balance"], 2)}


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
    ts = [{"id": t["id"], "type": t["type"], "title": t["title"], "sub": t["sub"], "reward": t["reward"],
           "done": t["id"] in done} for t in db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id")]
    return jr({"me": me(uid), "tasks": ts, "svc": SERVICES, "ref": float(setting("ref", "20"))})


@routes.post("/api/task/start")
async def api_start(req):
    uid = need(req)
    t = get_task((await body(req)).get("id"))
    if not t:
        return jr({"error": "Vazifa topilmadi"}, 404)
    db.execute("INSERT OR IGNORE INTO started VALUES(?,?,?)", (uid, t["id"], int(time.time())))
    db.commit()
    return jr({"url": t["url"]})


@routes.post("/api/task/claim")
async def api_claim(req):
    uid = need(req)
    t = get_task((await body(req)).get("id"))
    if not t:
        return jr({"error": "Vazifa topilmadi"}, 404)
    if t["type"] != "bonus":
        s = db.execute("SELECT ts FROM started WHERE user_id=? AND task_id=?", (uid, t["id"])).fetchone()
        if not s or time.time() - s["ts"] < MIN_WAIT:
            return jr({"error": "Avval vazifani bajaring (kamida 10 soniya)"}, 400)
    try:
        db.execute("INSERT INTO done VALUES(?,?,?)", (uid, t["id"], int(time.time())))
    except sqlite3.IntegrityError:
        return jr({"error": "Bu vazifa allaqachon bajarilgan"}, 400)
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (t["reward"], uid))
    db.commit()
    await pay_ref(uid)
    return jr({"me": me(uid)})


@routes.post("/api/order")
async def api_order(req):
    uid = need(req)
    b = await body(req)
    try:
        price = dict(SERVICES[b["platform"]])[b["service"]]
        qty = int(b["qty"])
    except Exception:
        return jr({"error": "Noto'g'ri so'rov"}, 400)
    link = str(b.get("link", "")).strip()[:300]
    if qty < 100 or qty > 1000000 or not link:
        return jr({"error": "Havola va miqdorni to'g'ri kiriting"}, 400)
    cost = round(qty / 100 * price, 2)
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
    except sqlite3.IntegrityError:
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


async def index(req):
    return web.FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-store"})


# ---------- Bot ----------
@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    a = command.args or ""
    ref = int(a[4:]) if a.startswith("ref_") and a[4:].isdigit() else None
    touch({"id": m.from_user.id, "username": m.from_user.username, "first_name": m.from_user.first_name}, ref)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Poletgram'ni ochish", web_app=WebAppInfo(url=URL))]])
    await m.answer(START_TEXT, reply_markup=kb)


def is_admin(m):
    return m.from_user.id in ADMINS


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m):
        return
    await m.answer(
        "🛠 Admin buyruqlari\n\n"
        "/addtask tur | sarlavha | tavsif | havola | mukofot\n"
        "  turlar: link, youtube, ad, bot, bonus\n"
        "  Misol: /addtask youtube | Videoni ko'ring | Videoni oxirigacha ko'ring | https://youtu.be/xxx | 1\n"
        "  Misol: /addtask bot | Botga start bosing | @bot ni ishga tushiring | https://t.me/bot?start=x | 2\n"
        "/tasks — vazifalar ro'yxati\n"
        "/deltask ID — vazifani o'chirish\n"
        "/addpromo KOD summa limit\n"
        "/setref summa — do'st uchun mukofot\n"
        "/addbal user_id summa\n"
        "/stats — statistika\n"
        "/post matn — hammaga xabar yuborish")


@dp.message(Command("addtask"))
async def cmd_addtask(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    p = [x.strip() for x in (command.args or "").split("|")]
    try:
        typ, title, sub, url, rew = p[0].lower(), p[1], p[2], p[3], float(p[4])
        assert typ in TYPES and title
    except Exception:
        return await m.answer("Format: /addtask tur | sarlavha | tavsif | havola | mukofot\nTurlar: link, youtube, ad, bot, bonus")
    cur = db.execute("INSERT INTO tasks(type,title,sub,url,reward) VALUES(?,?,?,?,?)", (typ, title, sub, url, rew))
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


async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.add_routes(routes)
    app.router.add_get("/", index)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Poletgram", web_app=WebAppInfo(url=URL)))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
