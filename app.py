# -*- coding: utf-8 -*-
import os
import io
import json
import math
import random
import string
import secrets
import time
import threading
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from supabase import create_client, Client
import qrcode
import base64
import requests

try:
    from PIL import Image, ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

load_dotenv()

app = Flask(__name__)

# --- SECURITY: no insecure hardcoded fallback. App refuses to start without a real secret. ---
if not os.getenv('SECRET_KEY'):
    raise RuntimeError('SECRET_KEY environment variable is required and must not be left unset.')
app.secret_key = os.environ['SECRET_KEY']

app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# SECURITY: cookie only sent over HTTPS. Render serves HTTPS by default, so this should be True
# in production. Set FLASK_ENV=production in Render's environment variables.
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_ENV') == 'production'

# --- Supabase setup ---
# SUPABASE_SERVICE_KEY must be the "service_role" key (Project Settings > API on Supabase).
# It bypasses Row Level Security, so it must NEVER be exposed to the frontend or committed to git.
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_SERVICE_KEY = os.environ['SUPABASE_SERVICE_KEY']
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

PRODUCT_IMAGE_BUCKET = 'product-images'   # public bucket
SLIP_BUCKET = 'payment-slips'             # private bucket

# --- In-memory cache to avoid hammering the DB on every request (same idea as before) ---
_orders_cache = None
_cache_time = 0
CACHE_DURATION = 15  # seconds


# ============================================================
# Helpers: data access
# ============================================================

# Same idea as the orders cache: products/categories/delivery-times barely change,
# but every visitor to the customer page was re-fetching all three from Supabase on
# every single request. A short cache removes that DB round trip almost every time,
# and admin writes below force an immediate refresh so edits still show up right away.
CATALOG_CACHE_DURATION = 20  # seconds
_products_cache, _products_cache_time = None, 0
_categories_cache, _categories_cache_time = None, 0
_delivery_cache, _delivery_cache_time = None, 0


def get_products_raw(force_refresh=False):
    global _products_cache, _products_cache_time
    now = time.time()
    if not force_refresh and _products_cache is not None and (now - _products_cache_time) < CATALOG_CACHE_DURATION:
        return _products_cache
    try:
        res = supabase.table('products').select('*').execute()
        _products_cache, _products_cache_time = res.data, now
        return _products_cache
    except Exception:
        return _products_cache if _products_cache is not None else []


def get_categories_raw(force_refresh=False):
    global _categories_cache, _categories_cache_time
    now = time.time()
    if not force_refresh and _categories_cache is not None and (now - _categories_cache_time) < CATALOG_CACHE_DURATION:
        return _categories_cache
    try:
        res = supabase.table('categories').select('*').execute()
        _categories_cache, _categories_cache_time = res.data, now
        return _categories_cache
    except Exception:
        return _categories_cache if _categories_cache is not None else []


def get_delivery_times_raw(force_refresh=False):
    global _delivery_cache, _delivery_cache_time
    now = time.time()
    if not force_refresh and _delivery_cache is not None and (now - _delivery_cache_time) < CATALOG_CACHE_DURATION:
        return _delivery_cache
    try:
        res = supabase.table('delivery_times').select('*').execute()
        _delivery_cache, _delivery_cache_time = res.data, now
        return _delivery_cache
    except Exception:
        return _delivery_cache if _delivery_cache is not None else []


_settings_cache, _settings_cache_time = None, 0
SETTINGS_CACHE_DURATION = 15  # seconds — short, since admin expects the cutoff time to take effect quickly


def get_settings_raw(force_refresh=False):
    global _settings_cache, _settings_cache_time
    now = time.time()
    if not force_refresh and _settings_cache is not None and (now - _settings_cache_time) < SETTINGS_CACHE_DURATION:
        return _settings_cache
    try:
        res = supabase.table('settings').select('*').execute()
        _settings_cache = {row['key']: row.get('value', '') for row in res.data}
        _settings_cache_time = now
        return _settings_cache
    except Exception as e:
        print('Error fetching settings:', e)
        return _settings_cache if _settings_cache is not None else {}


def get_setting(key, default=''):
    return get_settings_raw().get(key, default)


def set_setting(key, value):
    # BUG FIX: without on_conflict='key', upsert() falls back to the table's primary key
    # (its auto id column), which is never 'order_cutoff_time' etc. So every save was
    # INSERTING a brand-new row instead of updating the existing one — the old row was
    # still there too, and get_settings_raw() would land on whichever row Supabase
    # happened to return last, so a save looked successful but didn't reliably take effect.
    supabase.table('settings').upsert({'key': key, 'value': value}, on_conflict='key').execute()
    get_settings_raw(force_refresh=True)


def get_order_cutoff_time():
    """Returns a datetime.time, or None if no cutoff is configured / it's invalid."""
    raw = str(get_setting('order_cutoff_time', '')).strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%H:%M').time()
    except ValueError:
        return None


# ============================================================
# LINE Login + Messaging API
# ============================================================
# ค่าเหล่านี้อ่านจาก environment variable บนเซิร์ฟเวอร์เท่านั้น — ห้ามฝังค่าจริงในไฟล์นี้เด็ดขาด
LINE_LOGIN_CHANNEL_ID = os.getenv('LINE_LOGIN_CHANNEL_ID', '')
LINE_LOGIN_CHANNEL_SECRET = os.getenv('LINE_LOGIN_CHANNEL_SECRET', '')
LINE_CHANNEL_ID = os.getenv('LINE_CHANNEL_ID', '')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_LOGIN_ENABLED = bool(LINE_LOGIN_CHANNEL_ID and LINE_LOGIN_CHANNEL_SECRET)
LINE_MESSAGING_ENABLED = bool(LINE_CHANNEL_ACCESS_TOKEN)


def line_push_message(line_user_id, text):
    """ส่งข้อความหา LINE user คนเดียว (push message) — เงียบๆ ถ้า config ไม่ครบหรือ error กันคำสั่งหลักพัง"""
    if not LINE_MESSAGING_ENABLED or not line_user_id:
        return False
    try:
        res = requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
            },
            json={'to': line_user_id, 'messages': [{'type': 'text', 'text': text[:5000]}]},
            timeout=8,
        )
        if res.status_code != 200:
            print('LINE push error:', res.status_code, res.text)
        return res.status_code == 200
    except Exception as e:
        print('LINE push exception:', e)
        return False


def notify_admin_line(text):
    """แจ้งเตือนแอดมิน (คนเดียว) ผ่าน LINE — ใช้ line_user_id ที่ผูกไว้ในตาราง settings"""
    admin_line_id = get_setting('admin_line_user_id', '')
    if admin_line_id:
        line_push_message(admin_line_id, text)


ORDER_STATUS_TH = {
    'confirmed': 'ยืนยันออเดอร์แล้ว 🧾',
    'preparing': 'กำลังเตรียมอาหาร 👩‍🍳',
    'ready': 'พร้อมให้รับแล้ว ✅',
    'completed': 'เสร็จสิ้นแล้ว 🎉',
}


def notify_customer_order_status(order_id, order_status):
    """แจ้งลูกค้าเจ้าของออเดอร์ (ถ้าล็อกอินด้วย LINE ตอนสั่ง) ว่าสถานะเปลี่ยน"""
    try:
        res = supabase.table('orders').select('line_user_id').eq('order_id', order_id).limit(1).execute()
        if not res.data:
            return
        line_user_id = res.data[0].get('line_user_id')
        if not line_user_id:
            return  # ลูกค้าคนนี้สั่งแบบ guest ไม่ได้ login ไว้ — ไม่มีที่ให้ส่งแจ้งเตือน
        status_text = ORDER_STATUS_TH.get(order_status, order_status)
        line_push_message(line_user_id, f"📦 อัปเดตออเดอร์ #{order_id}\nสถานะ: {status_text}")
    except Exception as e:
        print('notify_customer_order_status error:', e)


@app.route('/api/line/login-url', methods=['GET'])
def api_line_login_url():
    """สร้างลิงก์ให้ลูกค้ากด 'Login ด้วย LINE' — ฝัง state กันปลอมคำขอ (CSRF) ไว้ใน session"""
    if not LINE_LOGIN_ENABLED:
        return jsonify({'error': 'LINE Login ยังไม่ได้ตั้งค่า'}), 503
    state = secrets.token_urlsafe(16)
    session['line_login_state'] = state
    redirect_uri = request.args.get('redirect_uri') or (request.url_root.rstrip('/') + '/api/line/callback')
    session['line_login_redirect_uri'] = redirect_uri
    params = {
        'response_type': 'code',
        'client_id': LINE_LOGIN_CHANNEL_ID,
        'redirect_uri': redirect_uri,
        'state': state,
        'scope': 'profile openid',
        'bot_prompt': 'aggressive',  # ชวนเพิ่มเพื่อน OA ต่อทันทีหลัง login เสร็จ
    }
    from urllib.parse import urlencode
    return jsonify({'login_url': 'https://access.line.me/oauth2/v2.1/authorize?' + urlencode(params)})


@app.route('/api/line/callback', methods=['GET'])
def api_line_callback():
    """LINE เด้งกลับมาที่นี่หลังลูกค้ากด login/ยินยอม — แลก code เป็น token แล้วดึงโปรไฟล์"""
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    frontend_url = '/'  # ปรับเป็นหน้าจริงถ้าต้องการ redirect ไปหน้าเฉพาะ

    if error or not code or state != session.get('line_login_state'):
        return f"<script>window.location.href='{frontend_url}?line_login=failed';</script>"

    redirect_uri = session.get('line_login_redirect_uri') or (request.url_root.rstrip('/') + '/api/line/callback')
    try:
        token_res = requests.post(
            'https://api.line.me/oauth2/v2.1/token',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri,
                'client_id': LINE_LOGIN_CHANNEL_ID,
                'client_secret': LINE_LOGIN_CHANNEL_SECRET,
            },
            timeout=8,
        )
        token_data = token_res.json()
        if token_res.status_code != 200:
            print('LINE token exchange error:', token_data)
            return f"<script>window.location.href='{frontend_url}?line_login=failed';</script>"

        profile_res = requests.get(
            'https://api.line.me/v2/profile',
            headers={'Authorization': f"Bearer {token_data['access_token']}"},
            timeout=8,
        )
        profile = profile_res.json()

        session['line_user_id'] = profile.get('userId')
        session['line_display_name'] = profile.get('displayName', '')
        session['line_picture_url'] = profile.get('pictureUrl', '')
        session.pop('line_login_state', None)
        session.pop('line_login_redirect_uri', None)
        return f"<script>window.location.href='{frontend_url}?line_login=success';</script>"
    except Exception as e:
        print('LINE callback exception:', e)
        return f"<script>window.location.href='{frontend_url}?line_login=failed';</script>"


@app.route('/api/line/me', methods=['GET'])
def api_line_me():
    """หน้าเว็บเรียกเช็คว่าตอนนี้ล็อกอินด้วย LINE อยู่ไหม เอาไว้โชว์ชื่อ/รูปมุมบนขวา"""
    if session.get('line_user_id'):
        return jsonify({
            'logged_in': True,
            'display_name': session.get('line_display_name', ''),
            'picture_url': session.get('line_picture_url', ''),
        })
    return jsonify({'logged_in': False, 'login_enabled': LINE_LOGIN_ENABLED})


@app.route('/api/line/logout', methods=['POST'])
def api_line_logout():
    session.pop('line_user_id', None)
    session.pop('line_display_name', None)
    session.pop('line_picture_url', None)
    return jsonify({'success': True})


@app.route('/api/admin/line/start-link', methods=['POST'])
def api_admin_line_start_link():
    """แอดมินกดปุ่ม 'เชื่อมต่อ LINE แจ้งเตือน' ในหน้าแอดมิน → ได้โค้ด 6 หลัก ใช้ครั้งเดียว หมดอายุ 5 นาที
    เอาไปพิมพ์ส่งในแชท LINE OA ของร้าน — กันไม่ให้ลูกค้าทั่วไปตั้งตัวเองเป็นแอดมินได้เองจากการทักแชทเข้ามาเฉยๆ"""
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if not LINE_MESSAGING_ENABLED:
        return jsonify({'error': 'ยังไม่ได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN บนเซิร์ฟเวอร์'}), 503
    code = ''.join(random.choices(string.digits, k=6))
    set_setting('admin_line_link_code', code)
    set_setting('admin_line_link_expires', (datetime.now() + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'))
    return jsonify({'code': code, 'expires_in_minutes': 5})


@app.route('/api/line/webhook', methods=['POST'])
def api_line_webhook():
    """LINE ยิง event มาที่นี่ (follow / unfollow / message) — ต้องตั้ง URL นี้ไว้ใน Messaging API settings"""
    body = request.get_data(as_text=True)
    signature = request.headers.get('X-Line-Signature', '')

    if LINE_CHANNEL_SECRET:
        import hashlib, hmac
        expected = base64.b64encode(hmac.new(LINE_CHANNEL_SECRET.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        if not hmac.compare_digest(expected, signature):
            return jsonify({'error': 'Invalid signature'}), 403

    events = (request.json or {}).get('events', [])
    for event in events:
        try:
            source_user_id = event.get('source', {}).get('userId')
            if not source_user_id:
                continue

            if event.get('type') == 'message' and event.get('message', {}).get('type') == 'text':
                text = event['message']['text'].strip()
                # เทียบกับโค้ด 6 หลักที่แอดมินขอไว้ (ถ้ายังไม่หมดอายุ) — ใช้ผูก LINE ของแอดมินเข้ากับระบบ
                pending_code = get_setting('admin_line_link_code', '')
                expires_str = get_setting('admin_line_link_expires', '')
                if pending_code and text == pending_code and expires_str:
                    try:
                        expires_at = datetime.strptime(expires_str, '%Y-%m-%d %H:%M:%S')
                        if datetime.now() <= expires_at:
                            set_setting('admin_line_user_id', source_user_id)
                            set_setting('admin_line_link_code', '')
                            line_push_message(source_user_id, '✅ เชื่อมต่อรับแจ้งเตือนออเดอร์ใหม่สำเร็จแล้วค่ะ')
                    except ValueError:
                        pass
        except Exception as e:
            print('LINE webhook event error:', e)

    return jsonify({'success': True})


def get_orders(force_refresh=False):
    global _orders_cache, _cache_time
    now = time.time()
    if not force_refresh and _orders_cache is not None and (now - _cache_time) < CACHE_DURATION:
        return _orders_cache
    try:
        res = supabase.table('orders').select('*').order('created_at', desc=True).execute()
        _orders_cache = res.data
        _cache_time = now
        return _orders_cache
    except Exception as e:
        print('Error fetching orders:', e)
        return _orders_cache if _orders_cache is not None else []


def normalize_time_format(time_val):
    t_str = str(time_val).strip().lstrip("'")
    if not t_str:
        return ''
    if '.' in t_str and ':' not in t_str:
        parts = t_str.split('.')
        if len(parts) >= 2:
            return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    if ':' in t_str:
        parts = t_str.split(':')
        if len(parts) >= 2:
            return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    try:
        num = float(t_str)
        hour = int(num)
        return f"{hour:02d}:00"
    except ValueError:
        pass
    return t_str



# ออเดอร์ที่ลูกค้ากดเสร็จสิ้นแล้ว (completed) จะถูกลบทิ้งอัตโนมัติหลังผ่านไปเท่านี้นาที
# กันไม่ให้ออเดอร์เก่าๆ ค้างปนกับออเดอร์ปัจจุบันในหน้าสถานะของลูกค้า
COMPLETED_ORDER_RETENTION_MINUTES = 90  # 1.5 ชม. — ปรับได้ตามต้องการ (1-2 ชม.)

# ออเดอร์ที่จ่ายเงินแล้วและสถานะยัง "ยืนยันแล้ว" (confirmed) ค้างอยู่ แต่ผ่านวันรับอาหาร
# (delivery_date) ไปแล้วเกินกี่วัน ถือว่าค้าง/ถูกลืม ไม่มีใครมากดเปลี่ยนสถานะต่อ ให้ลบทิ้งอัตโนมัติ
STALE_CONFIRMED_ORDER_DAYS = 1


def archive_and_delete_order(row):
    """ย้ายออเดอร์ไปเก็บที่ตาราง orders_archive ก่อน แล้วค่อยลบออกจาก orders — กู้คืนได้เสมอถ้าพลาด
    ไม่ใช่ลบทิ้งถาวรเหมือนเดิมอีกต่อไป (เดิมใช้ .delete() ตรงๆ ทำให้ออเดอร์จริงหายไปแบบกู้คืนไม่ได้)"""
    try:
        archive_row = {k: v for k, v in row.items() if k != 'id'}  # ตัด id เดิมทิ้ง กัน primary key ชนกัน ให้ตารางออกเลขใหม่เอง
        archive_row['archived_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        supabase.table('orders_archive').insert(archive_row).execute()
        supabase.table('orders').delete().eq('order_id', row['order_id']).execute()
    except Exception as e:
        # ถ้า archive ไม่สำเร็จ (เช่นยังไม่ได้สร้างตาราง orders_archive) ห้ามลบต้นฉบับเด็ดขาด กันข้อมูลหายซ้ำ
        print('Archive failed, skip delete to be safe:', e)


def cleanup_pending_orders():
    try:
        orders = get_orders(force_refresh=True)
        now = datetime.now()
        for row in orders:
            payment_status = str(row.get('payment_status', '')).lower()
            order_status = str(row.get('order_status', '')).lower()
            created_at_str = str(row.get('created_at', ''))
            completed_at_str = str(row.get('completed_at', '') or '')

            if payment_status == 'pending' and created_at_str:
                try:
                    created_at = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S')
                    if now - created_at > timedelta(minutes=10):
                        supabase.table('orders').delete().eq('order_id', row['order_id']).execute()
                        continue
                except Exception:
                    pass

            # ย้าย (archive) ออเดอร์ที่ "เสร็จสิ้น" แล้วเกิน COMPLETED_ORDER_RETENTION_MINUTES
            if order_status == 'completed' and completed_at_str:
                try:
                    completed_at = datetime.strptime(completed_at_str, '%Y-%m-%d %H:%M:%S')
                    if now - completed_at > timedelta(minutes=COMPLETED_ORDER_RETENTION_MINUTES):
                        archive_and_delete_order(row)
                        continue
                except Exception:
                    pass

            # ย้าย (archive) ออเดอร์ที่ค้างสถานะ "ยืนยันแล้ว" (จ่ายเงินแล้วแต่ไม่มีใครกดเปลี่ยนสถานะต่อ)
            # นานเกิน STALE_CONFIRMED_ORDER_DAYS วันหลังวันรับอาหารที่ระบุไว้
            if order_status == 'confirmed' and payment_status == 'paid':
                delivery_date_str = str(row.get('delivery_date', '') or '')[:10]
                if delivery_date_str:
                    try:
                        delivery_date = datetime.strptime(delivery_date_str, '%Y-%m-%d').date()
                        if (now.date() - delivery_date).days >= STALE_CONFIRMED_ORDER_DAYS:
                            archive_and_delete_order(row)
                    except Exception:
                        pass
        get_orders(force_refresh=True)
    except Exception as e:
        print('Cleanup error:', e)


# PERFORMANCE: cleanup_pending_orders() used to run synchronously on every single
# homepage visit, doing a full-table fetch (x2) + row-by-row deletes before the page
# could render. That gets slower as orders pile up, and it re-ran on every visitor,
# every refresh, and every cron ping. Now it runs in the background, at most once
# every 5 minutes, so the page never waits on it.
CLEANUP_INTERVAL_SECONDS = 300
_last_cleanup_time = 0
_cleanup_running = False


def maybe_run_cleanup():
    global _last_cleanup_time, _cleanup_running
    now = time.time()
    if _cleanup_running or (now - _last_cleanup_time) < CLEANUP_INTERVAL_SECONDS:
        return
    _last_cleanup_time = now
    _cleanup_running = True

    def _run():
        global _cleanup_running
        try:
            cleanup_pending_orders()
        finally:
            _cleanup_running = False

    threading.Thread(target=_run, daemon=True).start()


# PERFORMANCE: phone photos can be several MB each; every customer loads every product
# image on the menu grid, so unresized originals make the page feel slow on mobile data.
PRODUCT_IMAGE_MAX_DIMENSION = 1000  # px — plenty for the menu grid, even zoomed in
PRODUCT_IMAGE_JPEG_QUALITY = 80


def _compress_product_image(file_bytes):
    """Re-encode as a resized JPEG. Returns (bytes, content_type, ext), or the original
    bytes unchanged if Pillow isn't available or the file isn't an image it can read."""
    if not _PIL_AVAILABLE:
        return file_bytes, None, None
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)  # phone photos carry rotation in EXIF, not pixels
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img.thumbnail((PRODUCT_IMAGE_MAX_DIMENSION, PRODUCT_IMAGE_MAX_DIMENSION))
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=PRODUCT_IMAGE_JPEG_QUALITY, optimize=True)
        return out.getvalue(), 'image/jpeg', '.jpg'
    except Exception:
        return file_bytes, None, None  # not a readable image (or corrupt) — upload as-is


def upload_product_image(file):
    """Upload to the PUBLIC bucket. Returns a public URL, or '' on failure."""
    if not file or file.filename == '':
        return ''
    ext = os.path.splitext(secure_filename(file.filename))[1] or '.jpg'
    file_bytes = file.read()
    content_type = file.content_type or 'image/jpeg'

    compressed_bytes, new_content_type, new_ext = _compress_product_image(file_bytes)
    if new_ext:
        file_bytes, content_type, ext = compressed_bytes, new_content_type, new_ext

    path = f"prod_{int(datetime.now().timestamp())}_{secrets.token_hex(4)}{ext}"
    supabase.storage.from_(PRODUCT_IMAGE_BUCKET).upload(
        path, file_bytes, {'content-type': content_type}
    )
    return supabase.storage.from_(PRODUCT_IMAGE_BUCKET).get_public_url(path)


def delete_product_image(image_url):
    if not image_url:
        return
    try:
        # public URL looks like .../object/public/product-images/<path>
        path = image_url.split(f'{PRODUCT_IMAGE_BUCKET}/')[-1]
        supabase.storage.from_(PRODUCT_IMAGE_BUCKET).remove([path])
    except Exception:
        pass


def upload_slip(file, order_id):
    """Upload to the PRIVATE bucket. Returns the storage path (not a public URL)."""
    ext = os.path.splitext(secure_filename(file.filename or 'slip.jpg'))[1] or '.jpg'
    path = f"slip_{order_id}_{int(datetime.now().timestamp())}{ext}"
    file_bytes = file.read()
    content_type = file.content_type or 'image/jpeg'
    supabase.storage.from_(SLIP_BUCKET).upload(
        path, file_bytes, {'content-type': content_type}
    )
    return path, file_bytes


# ============================================================
# Helpers: validation & pricing (server is the source of truth)
# ============================================================

def parse_money(value, field='ราคา'):
    """Return a float >= 0, or None when blank. Raises ValueError with a Thai message on bad input."""
    if value is None or str(value).strip() == '':
        return None
    try:
        num = float(str(value).strip())
    except ValueError:
        raise ValueError(f'{field}ต้องเป็นตัวเลข')
    if not math.isfinite(num) or num < 0:
        raise ValueError(f'{field}ต้องไม่ติดลบ')
    return round(num, 2)


def parse_options(raw):
    """products.options may be a JSON string or an already-decoded list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


def calc_extra_price(product, selected_names):
    """Sum add-on prices from the product's OWN options in the DB.
    The client only says which choices were picked; any price it sends is ignored.
    Radio groups count at most one choice; checkbox groups can count several."""
    selected = {str(s) for s in (selected_names or []) if s is not None}
    extra = 0.0
    for group in parse_options(product.get('options')):
        if not isinstance(group, dict):
            continue
        choices = [c for c in (group.get('choices') or []) if isinstance(c, dict)]
        picked = [c for c in choices if str(c.get('name')) in selected]
        if group.get('type') != 'checkbox':
            picked = picked[:1]
        for c in picked:
            try:
                extra += max(float(c.get('price') or 0), 0.0)
            except (TypeError, ValueError):
                pass
    return extra


def mask_phone(phone):
    p = str(phone or '')
    return f"{p[:3]}****{p[-3:]}" if len(p) >= 7 else '***'


# --- PromptPay QR generation (unchanged, this logic was already correct) ---

def format_field(id_str, value):
    length = f"{len(value):02d}"
    return f"{id_str}{length}{value}"


def calculate_crc16(data):
    crc = 0xFFFF
    for char in data.encode('ascii'):
        crc ^= (char << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def generate_promptpay_payload(phone_or_id, amount=None):
    target = phone_or_id.replace('-', '').strip()
    if len(target) == 10 and target.startswith('0'):
        target = '0066' + target[1:]
        target_field = format_field('01', target)
    elif len(target) == 13:
        target_field = format_field('02', target)
    else:
        target_field = format_field('01', target)

    aid = 'A000000677010111'
    merchant_info = format_field('00', aid) + target_field
    tag_29 = format_field('29', merchant_info)

    payload = '000201'
    payload += '010212' if amount and float(amount) > 0 else '010211'
    payload += tag_29
    payload += '5303764'
    if amount and float(amount) > 0:
        amt_str = f"{float(amount):.2f}"
        payload += format_field('54', amt_str)
    payload += '5802TH'
    payload_to_crc = payload + '6304'
    crc = calculate_crc16(payload_to_crc)
    return payload_to_crc + crc


# ============================================================
# FRONTEND ROUTES
# ============================================================

@app.route('/')
def index():
    maybe_run_cleanup()
    return render_template('index.html')


@app.route('/api/payment/slip', methods=['POST'])
@app.route('/api/upload-slip', methods=['POST'])
@app.route('/api/slip/upload', methods=['POST'])
def api_slip_fallback():
    return api_upload_slip()


@app.route('/admin')
def admin_page():
    if not session.get('admin_logged_in'):
        return render_template('admin.html', logged_in=False)
    return render_template('admin.html', logged_in=True)


# ============================================================
# CUSTOMER APIs
# ============================================================

@app.route('/api/products', methods=['GET'])
def api_get_products():
    active_products = []
    for r in get_products_raw():
        if str(r.get('status', '')) != 'active':
            continue
        r = dict(r)  # copy — this endpoint mutates fields below, cache stays untouched
        options_raw = r.get('options')
        if isinstance(options_raw, str):
            try:
                r['options'] = json.loads(options_raw) if options_raw else []
            except Exception:
                r['options'] = []
        elif options_raw is None:
            r['options'] = []
        try:
            r['price'] = float(r.get('price', 0))
        except Exception:
            r['price'] = 0.0
        sale_price_val = r.get('sale_price')
        r['sale_price'] = float(sale_price_val) if sale_price_val not in (None, '') else None
        active_products.append(r)
    return jsonify(active_products)


@app.route('/api/categories', methods=['GET'])
def api_get_categories():
    return jsonify(get_categories_raw())


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Public, read-only settings the customer page needs (currently just the daily order cutoff)."""
    cutoff = get_order_cutoff_time()
    return jsonify({'order_cutoff_time': cutoff.strftime('%H:%M') if cutoff else ''})


@app.route('/api/delivery-times', methods=['GET'])
def api_get_delivery_times():
    active_times = []
    for r in get_delivery_times_raw():
        status = str(r.get('status', 'active')).lower().strip()
        if status in ['', 'active', 'open', 'true']:
            r = dict(r)
            r['time'] = normalize_time_format(r.get('time', ''))
            active_times.append(r)
    return jsonify(active_times)


@app.route('/api/order', methods=['POST'])
def api_create_order():
    data = request.json or {}
    customer_name = str(data.get('customer_name') or '').strip()[:100]
    phone = str(data.get('phone', '')).strip().zfill(10)
    delivery_date = data.get('delivery_date')
    delivery_time = data.get('delivery_time')
    items = data.get('items', [])

    if not customer_name or not phone or not delivery_date or not delivery_time or not items:
        return jsonify({'error': 'Missing required fields'}), 400

    # --- SECURITY: never trust the client's date. Front-end already blocks picking a
    # past date, but that's trivially bypassed by calling this endpoint directly, so
    # re-check here too. ---
    try:
        parsed_delivery_date = datetime.strptime(str(delivery_date)[:10], '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid delivery_date'}), 400
    if parsed_delivery_date < datetime.now().date():
        return jsonify({'error': 'ไม่สามารถเลือกวันที่ผ่านมาแล้วได้'}), 400

    # --- ปิดรับออเดอร์ตามเวลาที่แอดมินตั้งไว้ (ถ้ามี) — เช็คฝั่ง server เสมอ กันลูกค้าเลี่ยงด้วยการยิง API ตรงๆ
    # หลังเวลานี้ของ "วันนี้" (ตามเวลาเครื่อง server) จะสั่งออเดอร์ใหม่ไม่ได้ จนกว่าจะขึ้นวันถัดไป (เที่ยงคืนรีเซ็ตให้อัตโนมัติ)
    cutoff_time = get_order_cutoff_time()
    if cutoff_time and datetime.now().time() >= cutoff_time:
        return jsonify({'error': f'หมดเวลารับออเดอร์ประจำวันนี้แล้ว (ปิดรับเวลา {cutoff_time.strftime("%H:%M")} น.) กรุณาสั่งใหม่ในวันถัดไปนะคะ'}), 400

    # --- SECURITY: everything that affects money is computed server-side.
    # The client only tells us WHICH product / quantity / option names were picked.
    # Prices (base, sale, add-ons) always come from the products table, so a tampered
    # request cannot use negative quantities or fake extra_price to pay less. ---
    if not isinstance(items, list) or not items or len(items) > 50:
        return jsonify({'error': 'Invalid items'}), 400
    if any(not isinstance(i, dict) for i in items):
        return jsonify({'error': 'Invalid items'}), 400

    product_ids = list({str(i.get('id') or i.get('product_id') or '') for i in items})
    product_ids = [pid for pid in product_ids if pid]
    if not product_ids:
        return jsonify({'error': 'Invalid items'}), 400

    prod_res = supabase.table('products').select('*').in_('id', product_ids).execute()
    products_by_id = {str(p['id']): p for p in prod_res.data}

    total = 0.0
    validated_items = []
    for item in items:
        pid = str(item.get('id') or item.get('product_id') or '')
        product = products_by_id.get(pid)
        if not product:
            return jsonify({'error': f'Product not found: {pid}'}), 400
        if str(product.get('status', 'active')).lower() != 'active':
            return jsonify({'error': f"เมนู \"{product.get('name')}\" ปิดขายแล้ว กรุณาลบออกจากตะกร้า"}), 400

        try:
            qty = int(item.get('qty', 1))
        except (TypeError, ValueError):
            qty = 0
        if qty < 1 or qty > 99:
            return jsonify({'error': 'จำนวนสินค้าไม่ถูกต้อง'}), 400

        sale_price = product.get('sale_price')
        real_price = float(sale_price) if sale_price not in (None, '') else float(product.get('price', 0))
        extra_price = calc_extra_price(product, item.get('selected_options'))
        total += (real_price + extra_price) * qty

        validated_items.append({
            'id': pid,
            'name': product.get('name'),
            'price': real_price,
            'extra_price': extra_price,
            'qty': qty,
            # display-only text for the admin (what the customer picked / wrote)
            'options': str(item.get('options') or '')[:300],
            'customNote': str(item.get('customNote') or '')[:200],
        })

    total = round(total, 2)
    if total <= 0:
        return jsonify({'error': 'Invalid order total'}), 400

    # --- SECURITY FIX: order_id is now a hard-to-guess random token instead of a 4-digit number,
    # since /api/order/status/<order_id> has no login and previously leaked any order it could find. ---
    order_id = f"ORD-{secrets.token_hex(5).upper()}"
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    order_row = {
        'order_id': order_id,
        'customer_name': customer_name,
        'phone': phone,
        'delivery_date': delivery_date,
        'delivery_time': delivery_time,
        'items': json.dumps(validated_items),
        'total': total,
        'payment_status': 'pending',
        'order_status': 'confirmed',
        'slip_url': '',
        'created_at': created_at,
        'paid_at': None,
        'trans_ref': None,
        'line_user_id': session.get('line_user_id'),  # None ถ้าลูกค้าไม่ได้ login ด้วย LINE (สั่งแบบ guest)
    }
    supabase.table('orders').insert(order_row).execute()
    get_orders(force_refresh=True)
    notify_admin_line(f"🔔 ออเดอร์ใหม่ #{order_id}\nลูกค้า: {customer_name}\nยอด: ฿{total}\nรับ: {delivery_date} {delivery_time}")
    return jsonify({'order_id': order_id, 'total': total})


@app.route('/api/payment/qr', methods=['POST'])
def api_payment_qr():
    data = request.json or {}
    amount = float(data.get('amount', 0))
    promptpay_no = os.getenv('PROMPTPAY_NUMBER', '0812345678')
    qr_data = generate_promptpay_payload(promptpay_no, amount)

    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buffered = io.BytesIO()
    img.save(buffered, format='PNG')
    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')

    return jsonify({
        'promptpay_number': promptpay_no,
        'amount': amount,
        'qr_image': f"data:image/png;base64,{img_str}"
    })


@app.route('/api/slip', methods=['POST'])
def api_upload_slip():
    order_id = request.form.get('order_id')
    file = request.files.get('slip')
    if not order_id or not file:
        return jsonify({'error': 'Missing order_id or slip file'}), 400

    orders = get_orders(force_refresh=True)
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        return jsonify({'error': 'Order not found'}), 404

    expected_amount = float(target_order.get('total', 0))

    slip_path, file_content = upload_slip(file, order_id)

    thunder_url = os.getenv('THUNDER_API_URL')
    thunder_key = os.getenv('THUNDER_API_KEY')
    verified = False
    trans_ref = None
    paid_amount = 0.0
    slip_date = ''
    api_status = None
    sender_num = ''
    sender_name_th = ''
    sender_name_en = ''
    sender_bank_short = ''

    if thunder_url and thunder_key:
        try:
            encoded_image = base64.b64encode(file_content).decode('utf-8')
            headers = {
                'Authorization': f'Bearer {thunder_key}',
                'Content-Type': 'application/json'
            }
            payload = {
                'image': encoded_image,
                'checkDuplicate': True
            }
            resp = requests.post(thunder_url, headers=headers, json=payload, timeout=15)
            res_json = resp.json()
            is_success = (
                res_json.get('success') is True or
                str(res_json.get('status', '')).lower() in ['verified', 'success', 'true', 'ok', '200', '200.0'] or
                res_json.get('code') in [200, '200', 200.0]
            )
            if resp.status_code == 200 and is_success:
                data_field = res_json.get('data', res_json)
                api_status = res_json.get('status')
                if not data_field:
                    # Thunder returned status:200 (call succeeded) but data is null/empty —
                    # this is their "สลิปไม่มีข้อมูล" case: image opened fine, but no
                    # readable slip/QR data inside it (not a real slip, no QR, QR unreadable).
                    verified = False
                else:
                    trans_ref = data_field.get('transRef') or res_json.get('transRef')
                    slip_date = data_field.get('date', '')  # ISO 8601 date from the slip itself, per Thunder v1 spec
                    raw_amount = data_field.get('amount', 0)
                    if isinstance(raw_amount, dict):
                        raw_amount = raw_amount.get('amount', 0)
                    try:
                        paid_amount = float(raw_amount) if raw_amount else 0.0
                    except (ValueError, TypeError):
                        paid_amount = 0.0

                    receiver_info = data_field.get('receiver', {})
                    receiver_account = receiver_info.get('account', {})
                    rcv_name_th = receiver_account.get('name', {}).get('th', '')
                    rcv_name_en = receiver_account.get('name', {}).get('en', '')
                    rcv_proxy = receiver_account.get('proxy', {}).get('account', '')
                    rcv_bank_acc = receiver_account.get('bank', {}).get('account', '')
                    rcv_target_num = rcv_proxy if rcv_proxy else rcv_bank_acc

                    # Sender (payer) info — same shape as receiver, per Thunder v1 spec.
                    # Previously never read at all, so it never reached the orders table.
                    sender_info = data_field.get('sender', {})
                    sender_account = sender_info.get('account', {})
                    sender_name_th = sender_account.get('name', {}).get('th', '')
                    sender_name_en = sender_account.get('name', {}).get('en', '')
                    sender_bank_short = sender_info.get('bank', {}).get('short', '')
                    sender_proxy = sender_account.get('proxy', {}).get('account', '')
                    sender_bank_acc = sender_account.get('bank', {}).get('account', '')
                    sender_num = sender_proxy if sender_proxy else sender_bank_acc


                    shop_number = os.getenv('PROMPTPAY_NUMBER', '').strip()
                    shop_name = os.getenv('SHOP_ACCOUNT_NAME', '').strip()

                    verified = True
                    if shop_number:
                        clean_shop = shop_number.replace('-', '').strip()
                        clean_rcv = str(rcv_target_num).replace('-', '').strip()
                        shop_digits = ''.join(filter(str.isdigit, clean_shop))
                        rcv_digits = ''.join(filter(str.isdigit, clean_rcv))
                        if shop_digits and rcv_digits:
                            common_digits = sum(1 for a, b in zip(shop_digits[-4:], rcv_digits[-4:]) if a == b)
                            if len(shop_digits) >= 4 and common_digits == 0 and shop_digits not in rcv_digits and rcv_digits not in shop_digits:
                                verified = False

                    if shop_name and verified:
                        prefixes = ['นาย', 'นาง', 'น.ส.', 'นางสาว', 'mr.', 'ms.', 'mrs.']
                        shop_lower = shop_name.lower()
                        rcv_th_lower = rcv_name_th.lower()
                        rcv_en_lower = rcv_name_en.lower()
                        for p in prefixes:
                            shop_lower = shop_lower.replace(p, '')
                            rcv_th_lower = rcv_th_lower.replace(p, '')
                            rcv_en_lower = rcv_en_lower.replace(p, '')
                        shop_parts = shop_lower.split()
                        rcv_parts = rcv_th_lower.split()
                        name_matched = False
                        if shop_parts and rcv_parts:
                            first_name_match = shop_parts[0] in rcv_parts[0] or rcv_parts[0] in shop_parts[0]
                            last_name_match = True
                            if len(shop_parts) > 1 and len(rcv_parts) > 1:
                                last_name_match = shop_parts[1][0] == rcv_parts[1][0]
                            if first_name_match and last_name_match:
                                name_matched = True
                        if not name_matched and (shop_name.lower() not in rcv_name_th.lower() and shop_name.lower() not in rcv_name_en.lower()):
                            short_shop_name = shop_name.split()[0] + ' ' + shop_name.split()[-1][0] if len(shop_name.split()) > 1 else shop_name
                            if short_shop_name.lower() not in rcv_name_th.lower():
                                verified = False
        except Exception as e:
            print('Slip Verification Error:', e)
            verified = False
    else:
        verified = False

    if verified and paid_amount > 0:
        if abs(paid_amount - expected_amount) > 0.05:
            verified = False

    if verified and trans_ref:
        for o in orders:
            existing_trans_ref = str(o.get('trans_ref', '')).strip()
            if existing_trans_ref and existing_trans_ref == str(trans_ref).strip():
                verified = False
                supabase.table('orders').delete().eq('order_id', order_id).execute()
                get_orders(force_refresh=True)
                return jsonify({
                    'success': False,
                    'message': '❌ สลิปนี้ถูกใช้งานไปแล้วในระบบ ไม่สามารถนำกลับมาใช้ซ้ำได้'
                }), 400

    if verified:
        update_data = {
            'payment_status': 'paid',
            'order_status': 'confirmed',
            'slip_url': slip_path,
            'paid_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'slip_date': slip_date,
            'api_status': str(api_status) if api_status is not None else '',
            'paid_amount': paid_amount,
            'sender_account': sender_num,
            'sender_name': sender_name_th or sender_name_en,
            'sender_bank': sender_bank_short,
            'receiver_account': rcv_target_num,
        }
        if trans_ref:
            update_data['trans_ref'] = str(trans_ref)
        supabase.table('orders').update(update_data).eq('order_id', order_id).execute()
        get_orders(force_refresh=True)
        # แจ้งลูกค้าเรื่อง "จ่ายเงินสำเร็จ" แยกจากสถานะออเดอร์ (order_status ยังเป็น confirmed เหมือนเดิม ไม่ได้เปลี่ยน)
        order_row_for_line = next((o for o in orders if o.get('order_id') == order_id), None)
        if order_row_for_line and order_row_for_line.get('line_user_id'):
            line_push_message(order_row_for_line['line_user_id'], f"✅ ตรวจสอบสลิปสำเร็จ ออเดอร์ #{order_id} ชำระเงินเรียบร้อยแล้วค่ะ")
        return jsonify({'success': True, 'message': 'Payment verified successfully'})
    else:
        supabase.table('orders').delete().eq('order_id', order_id).execute()
        get_orders(force_refresh=True)
        return jsonify({
            'success': False,
            'message': 'ยอดเงินในสลิปไม่ถูกต้อง\nหรือสลิปนี้ถูกใช้งานไปแล้ว\nหรือสลิปไม่ได้โอนเข้าบัญชีร้าน'
        }), 400


@app.route('/api/order/status/<order_id>', methods=['GET'])
def api_order_status(order_id):
    orders = get_orders()
    target = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target:
        return jsonify({'error': 'Order not found'}), 404
    # No login here, so return a whitelist only (never slip_url / trans_ref / items / raw row).
    return jsonify({
        'order_id': target.get('order_id'),
        'customer_name': target.get('customer_name'),
        'phone': mask_phone(target.get('phone')),
        'payment_status': target.get('payment_status'),
        'order_status': target.get('order_status'),
        'delivery_date': target.get('delivery_date'),
        'delivery_time': target.get('delivery_time'),
        'total': target.get('total'),
        'completed_at': target.get('completed_at'),
    })


# ============================================================
# ADMIN APIs
# ============================================================

@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    env_user = os.environ['ADMIN_USERNAME']
    env_pass = os.environ['ADMIN_PASSWORD']
    if username == env_user and password == env_pass:
        session['admin_logged_in'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Invalid credentials'}), 401


@app.route('/api/admin/logout', methods=['POST'])
def api_admin_logout():
    session.pop('admin_logged_in', None)
    return jsonify({'success': True})


@app.route('/api/admin/check-auth', methods=['GET'])
def api_admin_check_auth():
    if session.get('admin_logged_in'):
        return jsonify({'authenticated': True})
    return jsonify({'authenticated': False}), 401


@app.route('/api/admin/slip-url/<path:slip_path>', methods=['GET'])
def api_admin_slip_url(slip_path):
    """Admin-only: generate a short-lived signed URL to view a private slip image."""
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    signed = supabase.storage.from_(SLIP_BUCKET).create_signed_url(slip_path, 3600)
    return jsonify(signed)


@app.route('/api/admin/dashboard', methods=['GET'])
def api_admin_dashboard():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    orders = get_orders()
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_sales = sum(float(o.get('total', 0)) for o in orders if str(o.get('created_at', '')).startswith(today_str) and str(o.get('payment_status', '')).lower() == 'paid')
    today_orders = sum(1 for o in orders if str(o.get('created_at', '')).startswith(today_str))
    paid_orders = sum(1 for o in orders if str(o.get('created_at', '')).startswith(today_str) and str(o.get('payment_status', '')).lower() == 'paid')
    pending_delivery = sum(1 for o in orders if str(o.get('order_status', '')).lower() in ['confirmed', 'preparing'])

    today_date = datetime.now().date()
    chart_labels = []
    chart_data = []
    for i in range(6, -1, -1):
        d = today_date - timedelta(days=i)
        d_str = d.strftime('%Y-%m-%d')
        chart_labels.append(d.strftime('%d/%m'))
        day_total = sum(float(o.get('total', 0)) for o in orders if str(o.get('created_at', '')).startswith(d_str) and str(o.get('payment_status', '')).lower() == 'paid')
        chart_data.append(day_total)

    item_counts = {}
    for o in orders:
        items_raw = o.get('items', '[]')
        try:
            items = json.loads(items_raw) if isinstance(items_raw, str) else (items_raw or [])
            for itm in items:
                name = itm.get('name', 'Unknown')
                qty = int(itm.get('qty', 1))
                item_counts[name] = item_counts.get(name, 0) + qty
        except Exception:
            pass
    top_menus = [{'name': k, 'count': v} for k, v in sorted(item_counts.items(), key=lambda x: x[1], reverse=True)[:5]]

    return jsonify({
        'today_sales': today_sales,
        'today_orders': today_orders,
        'paid_orders': paid_orders,
        'pending_delivery': pending_delivery,
        'chart_labels': chart_labels,
        'chart_data': chart_data,
        'top_menus': top_menus
    })


@app.route('/api/admin/products', methods=['GET', 'POST', 'PUT', 'DELETE'])
def api_admin_products():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        records = []
        for r in get_products_raw():
            r = dict(r)
            options_raw = r.get('options')
            if isinstance(options_raw, str):
                try:
                    r['options'] = json.loads(options_raw) if options_raw else []
                except Exception:
                    r['options'] = []
            records.append(r)
        return jsonify(records)

    elif request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'กรุณากรอกชื่อเมนู'}), 400
        try:
            price = parse_money(request.form.get('price'), 'ราคา')
            sale_price = parse_money(request.form.get('sale_price'), 'ราคาโปรโมชัน')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if price is None:
            return jsonify({'error': 'กรุณากรอกราคา'}), 400
        if sale_price is not None and sale_price >= price:
            return jsonify({'error': 'ราคาโปรโมชันต้องน้อยกว่าราคาปกติ'}), 400

        description = request.form.get('description', '')
        category = request.form.get('category', '')
        status = request.form.get('status', 'active')
        options_json = request.form.get('options', '[]')
        file = request.files.get('image')

        # upload only after validation passes, so a rejected form doesn't leave an orphan image
        image_url = upload_product_image(file) if file else ''

        prod_id = f"PROD-{int(datetime.now().timestamp())}"
        row = {
            'id': prod_id, 'name': name, 'description': description, 'category': category,
            'price': price, 'sale_price': sale_price, 'image_url': image_url, 'status': status,
            'created_date': datetime.now().strftime('%Y-%m-%d'), 'options': options_json,
        }
        supabase.table('products').insert(row).execute()
        get_products_raw(force_refresh=True)
        return jsonify({'success': True})

    elif request.method == 'PUT':
        body = request.get_json(silent=True) if request.is_json else None
        prod_id = (
            request.form.get('id') or request.form.get('product_id') or
            request.args.get('id') or
            (body.get('id') if body else None) or
            (body.get('product_id') if body else None)
        )
        if not prod_id:
            return jsonify({'error': 'Missing product id'}), 400

        existing_res = supabase.table('products').select('*').eq('id', prod_id).execute()
        if not existing_res.data:
            return jsonify({'error': 'Product not found'}), 404
        current = existing_res.data[0]

        # Only fields actually sent are updated (form-data from the edit modal, or JSON from the status toggle).
        incoming = body if request.is_json else request.form
        incoming = incoming or {}

        update_data = {}
        for field in ['name', 'description', 'category', 'status']:
            if field in incoming:
                update_data[field] = incoming[field]
        if 'options' in incoming:
            opt_val = incoming['options']
            update_data['options'] = json.dumps(opt_val) if isinstance(opt_val, (list, dict)) else opt_val

        try:
            if 'price' in incoming:
                new_price = parse_money(incoming['price'], 'ราคา')
                if new_price is None:
                    return jsonify({'error': 'กรุณากรอกราคา'}), 400
                update_data['price'] = new_price
            if 'sale_price' in incoming:
                # blank => None => clears the promotion (this used to write '' into a numeric column)
                update_data['sale_price'] = parse_money(incoming['sale_price'], 'ราคาโปรโมชัน')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        # Only cross-check prices when one of them is being changed (so toggling status never fails on old data)
        if 'price' in update_data or 'sale_price' in update_data:
            final_price = update_data.get('price', float(current.get('price') or 0))
            final_sale = update_data['sale_price'] if 'sale_price' in update_data else current.get('sale_price')
            if final_sale not in (None, '') and float(final_sale) >= float(final_price):
                return jsonify({'error': 'ราคาโปรโมชันต้องน้อยกว่าราคาปกติ'}), 400

        file = request.files.get('image')
        if file and file.filename != '':
            delete_product_image(current.get('image_url'))
            update_data['image_url'] = upload_product_image(file)

        if not update_data:
            return jsonify({'error': 'No fields to update'}), 400

        supabase.table('products').update(update_data).eq('id', prod_id).execute()
        get_products_raw(force_refresh=True)
        return jsonify({'success': True})

    elif request.method == 'DELETE':
        data = request.json or {}
        prod_id = data.get('id') or request.args.get('id')
        if not prod_id:
            return jsonify({'error': 'Missing product id'}), 400
        existing_res = supabase.table('products').select('*').eq('id', prod_id).execute()
        if existing_res.data:
            delete_product_image(existing_res.data[0].get('image_url'))
        supabase.table('products').delete().eq('id', prod_id).execute()
        get_products_raw(force_refresh=True)
        return jsonify({'success': True})


@app.route('/api/admin/categories', methods=['GET', 'POST', 'DELETE'])
def api_admin_categories():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        return jsonify(get_categories_raw())

    elif request.method == 'POST':
        data = request.json or {}
        name = data.get('name')
        if not name:
            return jsonify({'success': False, 'message': 'Missing category name'}), 400
        cat_id = f"CAT-{int(datetime.now().timestamp())}"
        supabase.table('categories').insert({'id': cat_id, 'name': name}).execute()
        get_categories_raw(force_refresh=True)
        return jsonify({'success': True})

    elif request.method == 'DELETE':
        data = request.json or {}
        cat_id = data.get('id')
        if not cat_id:
            return jsonify({'success': False, 'message': 'Missing id'}), 400
        supabase.table('categories').delete().eq('id', cat_id).execute()
        get_categories_raw(force_refresh=True)
        return jsonify({'success': True})


@app.route('/api/admin/archive', methods=['GET'])
def api_admin_archive():
    """ออเดอร์ที่ถูกย้ายเข้า archive อัตโนมัติ (เสร็จสิ้นเกิน 90 นาที หรือค้างสถานะยืนยันนานเกินไป)
    ให้แอดมินย้อนดูได้ว่า 'วันนี้ออเดอร์ของใครบ้าง' แม้จะถูกเคลียร์ออกจากลิสต์หลักไปแล้ว"""
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        res = supabase.table('orders_archive').select('*').order('created_at', desc=True).limit(500).execute()
        return jsonify(res.data)
    except Exception as e:
        # ตารางอาจยังไม่ถูกสร้าง (ยังไม่ได้รัน SQL create table orders_archive) — ตอบเป็นลิสต์ว่างแทน error 500
        print('api_admin_archive error:', e)
        return jsonify([])


@app.route('/api/admin/orders', methods=['GET', 'PUT'])
def api_admin_orders():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        return jsonify(get_orders())

    elif request.method == 'PUT':
        data = request.json or {}
        order_id = data.get('order_id')
        new_status = data.get('order_status')
        if not order_id:
            return jsonify({'error': 'Missing order_id'}), 400
        update_fields = {'order_status': new_status}
        # ใช้ completed_at คำนวณฝั่งลูกค้าว่า "เสร็จสิ้น" มานานแค่ไหนแล้ว เพื่อซ่อนออเดอร์เก่าออกจากลิสต์อัตโนมัติ
        # เคลียร์ทิ้งเป็น None ถ้าสถานะถูกเปลี่ยนออกจาก completed (เผื่อกดพลาดแล้วกลับสถานะ)
        update_fields['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if new_status == 'completed' else None
        res = supabase.table('orders').update(update_fields).eq('order_id', order_id).execute()
        if res.data:
            get_orders(force_refresh=True)
            notify_customer_order_status(order_id, new_status)
            return jsonify({'success': True})
        return jsonify({'error': 'Order not found'}), 404


@app.route('/api/admin/delivery-times', methods=['GET', 'POST', 'PUT', 'DELETE'])
def api_admin_delivery_times():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        records = []
        for r in get_delivery_times_raw():
            r = dict(r)
            r['time'] = normalize_time_format(r.get('time', ''))
            records.append(r)
        return jsonify(records)

    elif request.method == 'POST':
        data = request.json or {}
        time_val = data.get('time')
        status = data.get('status', 'active')
        t_id = f"TIME-{int(datetime.now().timestamp())}"
        supabase.table('delivery_times').insert({'id': t_id, 'time': time_val, 'status': status}).execute()
        get_delivery_times_raw(force_refresh=True)
        return jsonify({'success': True})

    elif request.method == 'PUT':
        data = request.json or {}
        t_id = data.get('id')
        new_status = data.get('status')
        res = supabase.table('delivery_times').update({'status': new_status}).eq('id', t_id).execute()
        if res.data:
            get_delivery_times_raw(force_refresh=True)
            return jsonify({'success': True})
        return jsonify({'error': 'Delivery time not found'}), 404

    elif request.method == 'DELETE':
        data = request.json or {}
        t_id = data.get('id')
        supabase.table('delivery_times').delete().eq('id', t_id).execute()
        get_delivery_times_raw(force_refresh=True)
        return jsonify({'success': True})


@app.route('/api/admin/settings', methods=['GET', 'PUT'])
def api_admin_settings():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        cutoff = get_order_cutoff_time()
        return jsonify({
            'order_cutoff_time': cutoff.strftime('%H:%M') if cutoff else '',
            'line_notify_linked': bool(get_setting('admin_line_user_id', '')),
        })

    data = request.json or {}
    raw_cutoff = str(data.get('order_cutoff_time', '')).strip()
    if raw_cutoff:
        try:
            datetime.strptime(raw_cutoff, '%H:%M')
        except ValueError:
            return jsonify({'error': 'รูปแบบเวลาไม่ถูกต้อง กรุณาระบุเป็น HH:MM'}), 400
    set_setting('order_cutoff_time', raw_cutoff)
    return jsonify({'success': True})


@app.route('/api/admin/reports', methods=['GET'])
def api_admin_reports():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    period = request.args.get('period', 'today')
    orders = get_orders()
    today = datetime.now().date()

    if period == 'today':
        target_date_str = today.strftime('%Y-%m-%d')
        filtered_orders = [o for o in orders if str(o.get('created_at', '')).startswith(target_date_str) and str(o.get('payment_status', '')).lower() == 'paid']
    elif period == '7days':
        start_date = today - timedelta(days=6)
        filtered_orders = [o for o in orders if o.get('created_at') and str(o.get('created_at', ''))[:10] >= start_date.strftime('%Y-%m-%d') and str(o.get('payment_status', '')).lower() == 'paid']
    elif period == '30days':
        start_date = today - timedelta(days=29)
        filtered_orders = [o for o in orders if o.get('created_at') and str(o.get('created_at', ''))[:10] >= start_date.strftime('%Y-%m-%d') and str(o.get('payment_status', '')).lower() == 'paid']
    else:
        filtered_orders = []

    total_sales = sum(float(o.get('total', 0)) for o in filtered_orders)
    total_orders = len(filtered_orders)

    chart_labels = []
    chart_data = []
    if period == 'today':
        chart_labels = ['วันนี้']
        chart_data = [total_sales]
    elif period == '7days':
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            d_str = d.strftime('%Y-%m-%d')
            chart_labels.append(d.strftime('%d/%m'))
            day_total = sum(float(o.get('total', 0)) for o in filtered_orders if str(o.get('created_at', '')).startswith(d_str))
            chart_data.append(day_total)
    elif period == '30days':
        # 6 real buckets of 5 days each, covering today-29 .. today
        start = today - timedelta(days=29)
        buckets = [0.0] * 6
        for o in filtered_orders:
            try:
                od = datetime.strptime(str(o.get('created_at', ''))[:10], '%Y-%m-%d').date()
            except ValueError:
                continue
            idx = (od - start).days // 5
            if 0 <= idx < 6:
                buckets[idx] += float(o.get('total', 0))
        for k in range(6):
            b_start = start + timedelta(days=k * 5)
            b_end = b_start + timedelta(days=4)
            chart_labels.append(f"{b_start.strftime('%d/%m')}-{b_end.strftime('%d/%m')}")
            chart_data.append(buckets[k])

    item_counts = {}
    for o in filtered_orders:
        items_raw = o.get('items', '[]')
        try:
            items = json.loads(items_raw) if isinstance(items_raw, str) else (items_raw or [])
            for itm in items:
                name = itm.get('name', 'Unknown')
                qty = int(itm.get('qty', 1))
                item_counts[name] = item_counts.get(name, 0) + qty
        except Exception:
            pass
    top_menus = [{'name': k, 'count': v} for k, v in sorted(item_counts.items(), key=lambda x: x[1], reverse=True)[:5]]

    return jsonify({
        'total_sales': total_sales,
        'total_orders': total_orders,
        'chart_labels': chart_labels,
        'chart_data': chart_data,
        'top_menus': top_menus
    })


if __name__ == '__main__':
    app.run(debug=True, port=5000)
