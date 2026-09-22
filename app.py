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


def cleanup_pending_orders():
    try:
        orders = get_orders(force_refresh=True)
        now = datetime.now()
        for row in orders:
            payment_status = str(row.get('payment_status', '')).lower()
            created_at_str = str(row.get('created_at', ''))
            if payment_status == 'pending' and created_at_str:
                try:
                    created_at = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S')
                    if now - created_at > timedelta(minutes=10):
                        supabase.table('orders').delete().eq('order_id', row['order_id']).execute()
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
    }
    supabase.table('orders').insert(order_row).execute()
    get_orders(force_refresh=True)
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

    if thunder_url and thunder_key:
        try:
            encoded_image = base64.b64encode(file_content).decode('utf-8')
            headers = {
                'Authorization': f'Bearer {thunder_key}',
                'Content-Type': 'application/json'
            }
            payload = {
                'image': encoded_image,
                'matchAmount': float(expected_amount),
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
                trans_ref = data_field.get('transRef') or res_json.get('transRef')
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
        }
        if trans_ref:
            update_data['trans_ref'] = str(trans_ref)
        supabase.table('orders').update(update_data).eq('order_id', order_id).execute()
        get_orders(force_refresh=True)
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
        res = supabase.table('orders').update({'order_status': new_status}).eq('order_id', order_id).execute()
        if res.data:
            get_orders(force_refresh=True)
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
