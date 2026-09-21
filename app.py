# -*- coding: utf-8 -*-
import os
import io
import json
import random
import string
import secrets
import time
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from supabase import create_client, Client
import qrcode
import base64
import requests

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


def upload_product_image(file):
    """Upload to the PUBLIC bucket. Returns a public URL, or '' on failure."""
    if not file or file.filename == '':
        return ''
    ext = os.path.splitext(secure_filename(file.filename))[1] or '.jpg'
    path = f"prod_{int(datetime.now().timestamp())}_{secrets.token_hex(4)}{ext}"
    file_bytes = file.read()
    content_type = file.content_type or 'image/jpeg'
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
    cleanup_pending_orders()
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
    res = supabase.table('products').select('*').eq('status', 'active').execute()
    active_products = []
    for r in res.data:
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
    res = supabase.table('categories').select('*').execute()
    return jsonify(res.data)


@app.route('/api/delivery-times', methods=['GET'])
def api_get_delivery_times():
    res = supabase.table('delivery_times').select('*').execute()
    active_times = []
    for r in res.data:
        status = str(r.get('status', 'active')).lower().strip()
        if status in ['', 'active', 'open', 'true']:
            r['time'] = normalize_time_format(r.get('time', ''))
            active_times.append(r)
    return jsonify(active_times)


@app.route('/api/order', methods=['POST'])
def api_create_order():
    data = request.json or {}
    customer_name = data.get('customer_name')
    phone = str(data.get('phone', '')).strip().zfill(10)
    delivery_date = data.get('delivery_date')
    delivery_time = data.get('delivery_time')
    items = data.get('items', [])

    if not customer_name or not phone or not delivery_date or not delivery_time or not items:
        return jsonify({'error': 'Missing required fields'}), 400

    # --- SECURITY FIX: prices are looked up server-side from the Products table.
    # The client can send whatever price it wants in the request body; we ignore it.
    # Only the product id and quantity/options from the client are trusted. ---
    product_ids = [str(item.get('id') or item.get('product_id') or '') for item in items]
    product_ids = [pid for pid in product_ids if pid]
    if not product_ids:
        return jsonify({'error': 'Invalid items'}), 400

    prod_res = supabase.table('products').select('*').in_('id', product_ids).execute()
    products_by_id = {p['id']: p for p in prod_res.data}

    total = 0.0
    validated_items = []
    for item in items:
        pid = str(item.get('id') or item.get('product_id') or '')
        product = products_by_id.get(pid)
        if not product:
            return jsonify({'error': f'Product not found: {pid}'}), 400

        sale_price = product.get('sale_price')
        real_price = float(sale_price) if sale_price not in (None, '') else float(product.get('price', 0))
        # extra_price (for paid options/add-ons) is still trusted from the client for now —
        # validating it against a server-side options list is a good next hardening step,
        # but out of scope for this pass since option pricing wasn't modeled server-side before either.
        extra_price = float(item.get('extra_price', 0))
        qty = int(item.get('qty', 1))
        line_total = (real_price + extra_price) * qty
        total += line_total

        validated_items.append({
            'id': pid,
            'name': product.get('name'),
            'price': real_price,
            'extra_price': extra_price,
            'qty': qty,
        })

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
    return jsonify(target)


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
        res = supabase.table('products').select('*').execute()
        records = res.data
        for r in records:
            options_raw = r.get('options')
            if isinstance(options_raw, str):
                try:
                    r['options'] = json.loads(options_raw) if options_raw else []
                except Exception:
                    r['options'] = []
        return jsonify(records)

    elif request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description', '')
        price = request.form.get('price', 0)
        sale_price = request.form.get('sale_price') or None
        category = request.form.get('category', '')
        status = request.form.get('status', 'active')
        options_json = request.form.get('options', '[]')
        file = request.files.get('image')

        image_url = upload_product_image(file) if file else ''

        prod_id = f"PROD-{int(datetime.now().timestamp())}"
        row = {
            'id': prod_id, 'name': name, 'description': description, 'category': category,
            'price': price, 'sale_price': sale_price, 'image_url': image_url, 'status': status,
            'created_date': datetime.now().strftime('%Y-%m-%d'), 'options': options_json,
        }
        supabase.table('products').insert(row).execute()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        prod_id = (
            request.form.get('id') or request.form.get('product_id') or
            request.args.get('id') or
            (request.json.get('id') if request.is_json else None) or
            (request.json.get('product_id') if request.is_json else None)
        )
        if not prod_id:
            return jsonify({'error': 'Missing product id'}), 400

        existing_res = supabase.table('products').select('*').eq('id', prod_id).execute()
        if not existing_res.data:
            return jsonify({'error': 'Product not found'}), 404
        current = existing_res.data[0]

        update_data = {}
        if request.content_type and ('multipart/form-data' in request.content_type or 'form' in request.content_type):
            update_data['name'] = request.form.get('name', current.get('name'))
            update_data['description'] = request.form.get('description', current.get('description'))
            update_data['category'] = request.form.get('category', current.get('category'))
            update_data['price'] = request.form.get('price', current.get('price'))
            update_data['sale_price'] = request.form.get('sale_price', current.get('sale_price'))
            update_data['status'] = request.form.get('status', current.get('status'))
            update_data['options'] = request.form.get('options', current.get('options'))

            file = request.files.get('image')
            if file and file.filename != '':
                delete_product_image(current.get('image_url'))
                update_data['image_url'] = upload_product_image(file)
        else:
            data = request.json or {}
            for field in ['status', 'price', 'sale_price', 'name', 'description', 'category']:
                if field in data:
                    update_data[field] = data[field]
            if 'options' in data:
                opt_val = data['options']
                update_data['options'] = json.dumps(opt_val) if isinstance(opt_val, (list, dict)) else opt_val

        supabase.table('products').update(update_data).eq('id', prod_id).execute()
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
        return jsonify({'success': True})


@app.route('/api/admin/categories', methods=['GET', 'POST', 'DELETE'])
def api_admin_categories():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        res = supabase.table('categories').select('*').execute()
        return jsonify(res.data)

    elif request.method == 'POST':
        data = request.json or {}
        name = data.get('name')
        if not name:
            return jsonify({'success': False, 'message': 'Missing category name'}), 400
        cat_id = f"CAT-{int(datetime.now().timestamp())}"
        supabase.table('categories').insert({'id': cat_id, 'name': name}).execute()
        return jsonify({'success': True})

    elif request.method == 'DELETE':
        data = request.json or {}
        cat_id = data.get('id')
        if not cat_id:
            return jsonify({'success': False, 'message': 'Missing id'}), 400
        supabase.table('categories').delete().eq('id', cat_id).execute()
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
        res = supabase.table('delivery_times').select('*').execute()
        records = res.data
        for r in records:
            r['time'] = normalize_time_format(r.get('time', ''))
        return jsonify(records)

    elif request.method == 'POST':
        data = request.json or {}
        time_val = data.get('time')
        status = data.get('status', 'active')
        t_id = f"TIME-{int(datetime.now().timestamp())}"
        supabase.table('delivery_times').insert({'id': t_id, 'time': time_val, 'status': status}).execute()
        return jsonify({'success': True})

    elif request.method == 'PUT':
        data = request.json or {}
        t_id = data.get('id')
        new_status = data.get('status')
        res = supabase.table('delivery_times').update({'status': new_status}).eq('id', t_id).execute()
        if res.data:
            return jsonify({'success': True})
        return jsonify({'error': 'Delivery time not found'}), 404

    elif request.method == 'DELETE':
        data = request.json or {}
        t_id = data.get('id')
        supabase.table('delivery_times').delete().eq('id', t_id).execute()
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
        for i in range(5, -1, -1):
            d = today - timedelta(days=i * 5)
            chart_labels.append(d.strftime('%d/%m'))
            chart_data.append(total_sales / 6)

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
