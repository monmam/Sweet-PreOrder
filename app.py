# -*- coding: utf-8 -*-
import os
import io
import json
import base64
import random
import string
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import gspread
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import qrcode
import requests

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'fallback-secret-key')

# Google API Setup (ใช้ OAuth 2.0 สิทธิ์ทั้ง Sheets และ Drive)
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# --- ตัวแปรสำหรับระบบ Cache ป้องกันปัญหา 429 Quota Exceeded ---
_orders_cache = None
_cache_time = 0
CACHE_DURATION = 15  # กำหนดให้จำข้อมูลไว้ 15 วินาที เพื่อลดการเรียก Google Sheets ถี่เกินไป

def get_oauth_creds():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.path.exists('credentials.json'):
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
                with open('token.json', 'w') as token:
                    token.write(creds.to_json())
    return creds

def get_gspread_client():
    creds = get_oauth_creds()
    if creds:
        return gspread.authorize(creds)
    return None

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

def get_drive_service():
    creds = get_oauth_creds()
    if creds:
        return build('drive', 'v3', credentials=creds)
    return None

def get_sheet(sheet_name):
    client = get_gspread_client()
    sheet_id = os.getenv('GOOGLE_SHEET_ID')
    if client and sheet_id:
        try:
            sh = client.open_by_key(sheet_id)
            return sh.worksheet(sheet_name)
        except Exception:
            pass
    return None

# ฟังก์ชันดึงข้อมูล Orders แบบมี Cache ป้องกัน Error 429
def get_cached_orders(force_refresh=False):
    global _orders_cache, _cache_time
    current_time = time.time()
    
    if not force_refresh and _orders_cache and (current_time - _cache_time) < CACHE_DURATION:
        return _orders_cache
        
    ws = get_sheet('Orders')
    if ws:
        try:
            _orders_cache = ws.get_all_records()
            _cache_time = current_time
            return _orders_cache
        except Exception as e:
            print("Error fetching orders:", e)
            if _orders_cache:
                return _orders_cache # ถ้าติดโควตาหรือเน็ตหลุด ให้ส่งข้อมูลเก่าสำรองไปก่อน
    return []

# --- ฟังก์ชัน Auto-Cleanup เคลียร์ออเดอร์ค้าง pending เกิน 10 นาที ---
def cleanup_pending_orders():
    ws = get_sheet('Orders')
    if not ws:
        return
    try:
        rows = get_cached_orders(force_refresh=True)
        now = datetime.now()
        rows_to_delete = []
        
        for idx, row in enumerate(rows, start=2):
            payment_status = str(row.get('payment_status', '')).lower()
            created_at_str = str(row.get('created_at', ''))
            
            if payment_status == 'pending' and created_at_str:
                try:
                    created_at = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S')
                    if now - created_at > timedelta(minutes=10):
                        rows_to_delete.append(idx)
                except Exception:
                    pass
        
        for row_idx in sorted(rows_to_delete, reverse=True):
            ws.delete_rows(row_idx)
            
        if rows_to_delete:
            get_cached_orders(force_refresh=True) # เคลียร์ Cache ทันทีที่มีการลบข้อมูล
            
    except Exception as e:
        print("Cleanup error:", e)

# --- ฟังก์ชันสร้าง PromptPay Payload ตามมาตรฐาน EMVCo ---
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
    target = phone_or_id.replace("-", "").strip()
    
    if len(target) == 10 and target.startswith("0"):
        target = "0066" + target[1:]
        target_field = format_field("01", target)
    elif len(target) == 13:
        target_field = format_field("02", target)
    else:
        target_field = format_field("01", target)
    
    aid = "A000000677010111"
    merchant_info = format_field("00", aid) + target_field
    tag_29 = format_field("29", merchant_info)
    
    payload = "000201"
    if amount and float(amount) > 0:
        payload += "010212"
    else:
        payload += "010211"
        
    payload += tag_29
    payload += "5303764"
    
    if amount and float(amount) > 0:
        amt_str = f"{float(amount):.2f}"
        payload += format_field("54", amt_str)
        
    payload += "5802TH"
    payload_to_crc = payload + "6304"
    crc = calculate_crc16(payload_to_crc)
    
    return payload_to_crc + crc

# --- FRONTEND ROUTES ---
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

# --- CUSTOMER APIs ---
@app.route('/api/products', methods=['GET'])
def api_get_products():
    ws = get_sheet('Products')
    if not ws:
        return jsonify([])
    rows = ws.get_all_records()
    active_products = []
    for r in rows:
        if str(r.get('status', 'active')).lower() == 'active':
            options_raw = r.get('options', '[]')
            try:
                r['options'] = json.loads(options_raw) if options_raw else []
            except Exception:
                r['options'] = []
            
            try:
                r['price'] = float(r.get('price', 0))
            except:
                r['price'] = 0.0

            sale_price_val = r.get('sale_price', '')
            if sale_price_val != '' and sale_price_val is not None:
                try:
                    r['sale_price'] = float(sale_price_val)
                except:
                    r['sale_price'] = None
            else:
                r['sale_price'] = None

            active_products.append(r)
    return jsonify(active_products)

@app.route('/api/categories', methods=['GET'])
def api_get_categories():
    ws = get_sheet('Categories')
    if not ws:
        return jsonify([])
    rows = ws.get_all_records()
    return jsonify(rows)

@app.route('/api/delivery-times', methods=['GET'])
def api_get_delivery_times():
    ws = get_sheet('DeliveryTimes')
    if not ws:
        return jsonify([])
    rows = ws.get_all_records()
    active_times = []
    for r in rows:
        status = str(r.get('status', 'active')).lower().strip()
        if status in ['', 'active', 'open', 'true', '🟢']:
            r['time'] = normalize_time_format(r.get('time', ''))
            active_times.append(r)
    return jsonify(active_times)

@app.route('/api/order', methods=['POST'])
def api_create_order():
    data = request.json
    customer_name = data.get('customer_name')
    phone = str(data.get('phone', '')).strip().zfill(10)
    delivery_date = data.get('delivery_date')
    delivery_time = data.get('delivery_time')
    items = data.get('items', [])
    
    if not customer_name or not phone or not delivery_date or not delivery_time or not items:
        return jsonify({'error': 'Missing required fields'}), 400

    total = 0
    for item in items:
        base_price = float(item.get('sale_price') if item.get('sale_price') not in [None, ''] else item.get('price', 0))
        extra_price = float(item.get('extra_price', 0))
        qty = int(item.get('qty', 1))
        total += (base_price + extra_price) * qty
    
    random_code = ''.join(random.choice(string.digits) for _ in range(4))
    order_id = f"ORD-{random_code}"
    
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    order_row = [
        order_id, customer_name, phone, delivery_date, delivery_time,
        json.dumps(items), total, 'pending', 'confirmed', '', created_at, '', ''
    ]
    
    ws = get_sheet('Orders')
    if ws:
        ws.append_row(order_row)
        get_cached_orders(force_refresh=True) # เคลียร์ Cache อัปเดตข้อมูลใหม่ทันที
        
    return jsonify({'order_id': order_id, 'total': total})

@app.route('/api/payment/qr', methods=['POST'])
def api_payment_qr():
    data = request.json
    amount = float(data.get('amount', 0))
    promptpay_no = os.getenv('PROMPTPAY_NUMBER', '0812345678')
    
    qr_data = generate_promptpay_payload(promptpay_no, amount)
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
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
        
    ws_orders = get_sheet('Orders')
    orders = get_cached_orders(force_refresh=True)
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    
    if not target_order:
        return jsonify({'error': 'Order not found'}), 404
        
    expected_amount = float(target_order.get('total', 0))
    
    drive = get_drive_service()
    folder_id = os.getenv('GOOGLE_DRIVE_SLIP_FOLDER_ID') or os.getenv('GOOGLE_DRIVE_FOLDER_ID')
    
    file_content = file.read()
    mime_type = file.content_type or 'image/jpeg'
    filename = secure_filename(f"slip_{order_id}_{file.filename}")
    
    file_id = ''
    file_url = ''
    if drive:
        metadata = {'name': filename}
        if folder_id:
            metadata['parents'] = [folder_id]
        media = MediaIoBaseUpload(io.BytesIO(file_content), mimetype=mime_type, resumable=True)
        created_file = drive.files().create(
            body=metadata, 
            media_body=media, 
            fields='id'
        ).execute()
        file_id = created_file.get('id')
        try:
            drive.permissions().create(
                fileId=file_id,
                body={'role': 'reader', 'type': 'anyone'}
            ).execute()
        except Exception:
            pass
        file_url = f"https://lh3.googleusercontent.com/d/{file_id}"

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
            
            print("Thunder API Response:", json.dumps(res_json, ensure_ascii=False, indent=2))
            
            is_success = (
                res_json.get('success') is True or 
                str(res_json.get('status', '')).lower() in ['verified', 'success', 'true', 'ok', '200', '200.0'] or 
                res_json.get('code') in [200, '200', 200.0]
            )
            
            if resp.status_code == 200 and is_success:
                data_field = res_json.get('data', res_json)
                
                # 1. ดึงข้อมูลเลขอ้างอิง (TransRef)
                trans_ref = (
                    data_field.get('transRef') or 
                    res_json.get('transRef')
                )
                
                # 2. ดึงยอดเงินที่ชำระ
                raw_amount = data_field.get('amount', 0)
                if isinstance(raw_amount, dict):
                    raw_amount = raw_amount.get('amount', 0)
                
                try:
                    paid_amount = float(raw_amount) if raw_amount else 0.0
                except (ValueError, TypeError):
                    paid_amount = 0.0

                # 3. ดึงข้อมูลผู้รับ (Receiver) จาก JSON structure ที่ได้มา
                receiver_info = data_field.get('receiver', {})
                receiver_account = receiver_info.get('account', {})
                
                rcv_name_th = receiver_account.get('name', {}).get('th', '')
                rcv_name_en = receiver_account.get('name', {}).get('en', '')
                
                # เลขพร้อมเพย์ หรือ เลขบัญชีปลายทางที่รับเงิน
                rcv_proxy = receiver_account.get('proxy', {}).get('account', '')
                rcv_bank_acc = receiver_account.get('bank', {}).get('account', '')
                rcv_target_num = rcv_proxy if rcv_proxy else rcv_bank_acc
                
                # ข้อมูลร้านค้าจาก Environment Variables
                shop_number = os.getenv('PROMPTPAY_NUMBER', '').strip()
                shop_name = os.getenv('SHOP_ACCOUNT_NAME', '').strip()
                
                # เริ่มต้นกำหนดให้ผ่านการตรวจสอบเบื้องต้น
                verified = True
                
                # ตรวจสอบเลขบัญชี / พร้อมเพย์ปลายทาง (เนื่องจากสลิปธนาคารมักจะ Masking เป็นตัว x ให้เช็คความถูกต้องส่วนที่ไม่ติด x หรือเทียบเบอร์)
                if shop_number:
                    clean_shop = shop_number.replace('-', '').strip()
                    clean_rcv = str(rcv_target_num).replace('-', '').strip()
                    # หากเลขปลายทางในสลิปไม่ตรงกับของร้านเลย (เช่น โอนเข้าคนอื่น) ให้ตีตกทันที
                    # (ข้ามการเช็คเครื่องหมาย x)
                    shop_digits = ''.join(filter(str.isdigit, clean_shop))
                    rcv_digits = ''.join(filter(str.isdigit, clean_rcv))
                    if shop_digits and rcv_digits:
                        # ตรวจสอบตัวเลขที่มีร่วมกัน (ป้องกันกรณี Masking บางส่วน)
                        common_digits = sum(1 for a, b in zip(shop_digits[-4:], rcv_digits[-4:]) if a == b)
                        if len(shop_digits) >= 4 and common_digits == 0 and shop_digits not in rcv_digits and rcv_digits not in shop_digits:
                            verified = False
                            print(f"Validation Failed: Account mismatch. Shop: {shop_digits}, Receiver: {rcv_digits}")

                # ตรวจสอบชื่อบัญชีปลายทาง (รองรับกรณีธนาคารย่อ/ซ่อนนามสกุล)
                if shop_name and verified:
                    # ทำความสะอาดคำนำหน้าและช่องว่างเพื่อเทียบชื่อ
                    prefixes = ["นาย", "นาง", "น.ส.", "นางสาว", "mr.", "ms.", "mrs."]
                    
                    shop_lower = shop_name.lower()
                    rcv_th_lower = rcv_name_th.lower()
                    rcv_en_lower = rcv_name_en.lower()
                    
                    for p in prefixes:
                        shop_lower = shop_lower.replace(p, "")
                        rcv_th_lower = rcv_th_lower.replace(p, "")
                        rcv_en_lower = rcv_en_lower.replace(p, "")
                        
                    shop_parts = shop_lower.split()
                    rcv_parts = rcv_th_lower.split()
                    
                    name_matched = False
                    if shop_parts and rcv_parts:
                        # เช็คว่าชื่อจริงตรงกัน และนามสกุลขึ้นต้นด้วยตัวเดียวกัน (เช่น "โภชนา" กับ "โ")
                        first_name_match = shop_parts[0] in rcv_parts[0] or rcv_parts[0] in shop_parts[0]
                        last_name_match = True
                        if len(shop_parts) > 1 and len(rcv_parts) > 1:
                            last_name_match = shop_parts[1][0] == rcv_parts[1][0]
                            
                        if first_name_match and last_name_match:
                            name_matched = True
                            
                    # ถ้าระบบอัจฉริยะยังไม่ผ่าน ให้เช็คแบบรวมข้อความแบบยืดหยุ่นอีกชั้น
                    if not name_matched and (shop_name.lower() not in rcv_name_th.lower() and shop_name.lower() not in rcv_name_en.lower()):
                        # ตรวจสอบกรณีนามสกุลโดนตัดเหลือตัวย่อตัวเดียว
                        short_shop_name = shop_name.split()[0] + " " + shop_name.split()[-1][0] if len(shop_name.split()) > 1 else shop_name
                        if short_shop_name.lower() not in rcv_name_th.lower():
                            verified = False
                            print(f"Validation Failed: Name mismatch. Shop Name: {shop_name}, Receiver Name: {rcv_name_th} / {rcv_name_en}")
                        
        except Exception as e:
            print("Slip Verification Error:", e)
            verified = False 
    else:
        verified = False

    if verified and paid_amount > 0:
        if abs(paid_amount - expected_amount) > 0.05:
            verified = False

    # --- ตรวจสอบความซ้ำซ้อนของ TransRef กับทุกออเดอร์ในระบบแบบเด็ดขาด ---
    if verified and trans_ref and ws_orders:
        for o in orders:
            existing_trans_ref = str(o.get('trans_ref', '')).strip()
            if existing_trans_ref and existing_trans_ref == str(trans_ref).strip():
                verified = False
                cell = ws_orders.find(order_id)
                if cell:
                    ws_orders.delete_rows(cell.row)
                    get_cached_orders(force_refresh=True)
                return jsonify({
                    'success': False, 
                    'message': '❌ สลิปนี้ถูกใช้งานไปแล้วในระบบ ไม่สามารถนำกลับมาใช้ซ้ำได้'
                }), 400

    if verified and ws_orders:
        cell = ws_orders.find(order_id)
        if cell:
            row_idx = cell.row
            ws_orders.update_cell(row_idx, 8, 'paid')
            ws_orders.update_cell(row_idx, 9, 'confirmed')
            ws_orders.update_cell(row_idx, 10, file_url)
            ws_orders.update_cell(row_idx, 12, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            
            if trans_ref:
                ws_orders.update_cell(row_idx, 13, str(trans_ref))
            
            get_cached_orders(force_refresh=True)
                
        return jsonify({'success': True, 'message': 'Payment verified successfully'})
    else:
        if ws_orders:
            cell = ws_orders.find(order_id)
            if cell:
                ws_orders.delete_rows(cell.row)
                get_cached_orders(force_refresh=True)
            
        return jsonify({
            'success': False, 
            'message': 'ยอดเงินในสลิปไม่ถูกต้อง\nหรือสลิปนี้ถูกใช้งานไปแล้ว\nหรือสลิปไม่ได้โอนเข้าบัญชีร้าน'
        }), 400

@app.route('/api/order/status/<order_id>', methods=['GET'])
def api_order_status(order_id):
    orders = get_cached_orders()
    target = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target:
        return jsonify({'error': 'Order not found'}), 404
    return jsonify(target)

# --- ADMIN APIs ---
@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    env_user = os.getenv('ADMIN_USERNAME', 'admin')
    env_pass = os.getenv('ADMIN_PASSWORD', 'admin123')
    
    if username == env_user and password == env_pass:
        session['admin_logged_in'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Invalid credentials'}), 401

@app.route('/api/admin/logout', methods=['POST'])
def api_admin_logout():
    session.pop('admin_logged_in', None)
    return jsonify({'success': True})

@app.route('/api/admin/dashboard', methods=['GET'])
def api_admin_dashboard():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    orders = get_cached_orders()
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
            items = json.loads(items_raw)
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
        
    ws = get_sheet('Products')
    if not ws:
        return jsonify({'error': 'Sheet not found'}), 500
        
    if request.method == 'GET':
        records = ws.get_all_records()
        for r in records:
            if '' in r:
                del r['']
            options_raw = r.get('options', '[]')
            try:
                r['options'] = json.loads(options_raw) if options_raw else []
            except Exception:
                r['options'] = []
        return jsonify(records)
        
    elif request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description', '')
        price = request.form.get('price', 0)
        sale_price = request.form.get('sale_price', '')
        category = request.form.get('category', '')
        status = request.form.get('status', 'active')
        options_json = request.form.get('options', '[]')
        file = request.files.get('image')
        
        image_url = ''
        if file and file.filename != '':
            drive = get_drive_service()
            folder_id = os.getenv('GOOGLE_DRIVE_PRODUCT_FOLDER_ID') or os.getenv('GOOGLE_DRIVE_FOLDER_ID')
            filename = secure_filename(f"prod_{int(datetime.now().timestamp())}_{file.filename}")
            media = MediaIoBaseUpload(io.BytesIO(file.read()), mimetype=file.content_type, resumable=True)
            metadata = {'name': filename}
            if folder_id:
                metadata['parents'] = [folder_id]
            if drive:
                created = drive.files().create(
                    body=metadata, 
                    media_body=media, 
                    fields='id'
                ).execute()
                file_id = created.get('id')
                try:
                    drive.permissions().create(
                        fileId=file_id,
                        body={'role': 'reader', 'type': 'anyone'}
                    ).execute()
                except Exception:
                    pass
                image_url = f"https://lh3.googleusercontent.com/d/{file_id}"

        prod_id = f"PROD-{int(datetime.now().timestamp())}"
        row = [prod_id, name, description, category, price, sale_price, image_url, status, datetime.now().strftime('%Y-%m-%d'), options_json]
        ws.append_row(row)
        return jsonify({'success': True})

    elif request.method == 'PUT':
        prod_id = (
            request.form.get('id') or 
            request.form.get('product_id') or 
            request.args.get('id') or 
            (request.json.get('id') if request.is_json else None) or
            (request.json.get('product_id') if request.is_json else None)
        )
        
        if not prod_id:
            return jsonify({'error': 'Missing product id'}), 400

        cell = ws.find(prod_id)
        if not cell:
            return jsonify({'error': 'Product not found'}), 404
        
        row_idx = cell.row
        row_values = ws.row_values(row_idx)
        
        current_name = row_values[1] if len(row_values) > 1 else ''
        current_desc = row_values[2] if len(row_values) > 2 else ''
        current_cat = row_values[3] if len(row_values) > 3 else ''
        current_price = row_values[4] if len(row_values) > 4 else ''
        current_sale_price = row_values[5] if len(row_values) > 5 else ''
        current_image = row_values[6] if len(row_values) > 6 else ''
        current_status = row_values[7] if len(row_values) > 7 else 'active'
        current_options = row_values[9] if len(row_values) > 9 else '[]'

        if request.content_type and ('multipart/form-data' in request.content_type or 'form' in request.content_type):
            name = request.form.get('name', current_name)
            description = request.form.get('description', current_desc)
            price = request.form.get('price', current_price)
            sale_price = request.form.get('sale_price', current_sale_price)
            category = request.form.get('category', current_cat)
            status = request.form.get('status', current_status)
            options_json = request.form.get('options', current_options)
            file = request.files.get('image')
            
            image_url = current_image
            if file and file.filename != '':
                drive = get_drive_service()
                if current_image:
                    old_file_id = None
                    if 'lh3.googleusercontent.com/d/' in current_image:
                        old_file_id = current_image.split('/d/')[-1].split('/')[0].split('?')[0]
                    elif 'id=' in current_image:
                        old_file_id = current_image.split('id=')[-1].split('&')[0]
                    
                    if old_file_id and drive:
                        try:
                            drive.files().delete(fileId=old_file_id).execute()
                        except Exception:
                            pass

                folder_id = os.getenv('GOOGLE_DRIVE_PRODUCT_FOLDER_ID') or os.getenv('GOOGLE_DRIVE_FOLDER_ID')
                filename = secure_filename(f"prod_{int(datetime.now().timestamp())}_{file.filename}")
                media = MediaIoBaseUpload(io.BytesIO(file.read()), mimetype=file.content_type, resumable=True)
                metadata = {'name': filename}
                if folder_id:
                    metadata['parents'] = [folder_id]
                if drive:
                    created = drive.files().create(
                        body=metadata, 
                        media_body=media, 
                        fields='id'
                    ).execute()
                    file_id = created.get('id')
                    try:
                        drive.permissions().create(
                            fileId=file_id,
                            body={'role': 'reader', 'type': 'anyone'}
                        ).execute()
                    except Exception:
                        pass
                    image_url = f"https://lh3.googleusercontent.com/d/{file_id}"

            ws.update_cell(row_idx, 2, name)
            ws.update_cell(row_idx, 3, description)
            ws.update_cell(row_idx, 4, category)
            ws.update_cell(row_idx, 5, price)
            ws.update_cell(row_idx, 6, sale_price)
            ws.update_cell(row_idx, 7, image_url)
            ws.update_cell(row_idx, 8, status)
            ws.update_cell(row_idx, 10, options_json)
        else:
            data = request.json or {}
            if 'status' in data:
                ws.update_cell(row_idx, 8, data['status'])
            if 'price' in data:
                ws.update_cell(row_idx, 5, data['price'])
            if 'sale_price' in data:
                ws.update_cell(row_idx, 6, data['sale_price'])
            if 'options' in data:
                opt_val = data['options']
                if isinstance(opt_val, (list, dict)):
                    opt_val = json.dumps(opt_val)
                ws.update_cell(row_idx, 10, opt_val)
            if 'name' in data:
                ws.update_cell(row_idx, 2, data['name'])
            if 'description' in data:
                ws.update_cell(row_idx, 3, data['description'])
            if 'category' in data:
                ws.update_cell(row_idx, 4, data['category'])
                
        return jsonify({'success': True})

    elif request.method == 'DELETE':
        data = request.json or {}
        prod_id = data.get('id') or request.args.get('id')
        cell = ws.find(prod_id)
        if cell:
            row_idx = cell.row
            row_values = ws.row_values(row_idx)
            if len(row_values) > 6:
                image_url = row_values[6]
                file_id = None
                if 'lh3.googleusercontent.com/d/' in image_url:
                    file_id = image_url.split('/d/')[-1].split('/')[0].split('?')[0]
                elif 'id=' in image_url:
                    file_id = image_url.split('id=')[-1].split('&')[0]
                
                if file_id:
                    drive = get_drive_service()
                    if drive:
                        try:
                            drive.files().delete(fileId=file_id).execute()
                        except Exception:
                            pass
            ws.delete_rows(row_idx)
        return jsonify({'success': True})

@app.route('/api/admin/categories', methods=['GET', 'POST', 'DELETE'])
def api_admin_categories():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    ws = get_sheet('Categories')
    if not ws:
        return jsonify({'error': 'Sheet not found'}), 500
        
    if request.method == 'GET':
        return jsonify(ws.get_all_records())
        
    elif request.method == 'POST':
        data = request.json
        name = data.get('name')
        if not name:
            return jsonify({'success': False, 'message': 'Missing category name'}), 400
            
        cat_id = f"CAT-{int(datetime.now().timestamp())}"
        ws.append_row([cat_id, name])
        return jsonify({'success': True})
        
    elif request.method == 'DELETE':
        data = request.json
        cat_id = data.get('id')
        cell = ws.find(cat_id)
        if cell:
            ws.delete_rows(cell.row)
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'Category not found'}), 404

@app.route('/api/admin/orders', methods=['GET', 'PUT'])
def api_admin_orders():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    if request.method == 'GET':
        return jsonify(get_cached_orders())
        
    elif request.method == 'PUT':
        data = request.json
        order_id = data.get('order_id')
        new_status = data.get('order_status')
        ws = get_sheet('Orders')
        if ws:
            cell = ws.find(order_id)
            if cell:
                ws.update_cell(cell.row, 9, new_status)
                get_cached_orders(force_refresh=True)
                return jsonify({'success': True})
        return jsonify({'error': 'Order not found'}), 404

@app.route('/api/admin/delivery-times', methods=['GET', 'POST', 'PUT', 'DELETE'])
def api_admin_delivery_times():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    ws = get_sheet('DeliveryTimes')
    if not ws:
        return jsonify([])
        
    if request.method == 'GET':
        records = ws.get_all_records()
        for r in records:
            r['time'] = normalize_time_format(r.get('time', ''))
        return jsonify(records)
    elif request.method == 'POST':
        data = request.json
        time_val = data.get('time')
        
        if time_val and ':' in str(time_val):
            parts = str(time_val).split(':')
            if len(parts) == 2:
                hour = parts[0].zfill(2)
                minute = parts[1].zfill(2)
                time_val = f"'{hour}:{minute}"

        status = data.get('status', 'active')
        t_id = f"TIME-{int(datetime.now().timestamp())}"
        ws.append_row([t_id, time_val, status])
        return jsonify({'success': True})
    elif request.method == 'PUT':
        data = request.json
        t_id = data.get('id')
        new_status = data.get('status')
        cell = ws.find(t_id)
        if cell:
            ws.update_cell(cell.row, 3, new_status)
            return jsonify({'success': True})
        return jsonify({'error': 'Delivery time not found'}), 404
    elif request.method == 'DELETE':
        data = request.json
        t_id = data.get('id')
        cell = ws.find(t_id)
        if cell:
            ws.delete_rows(cell.row)
        return jsonify({'success': True})

@app.route('/api/admin/reports', methods=['GET'])
def api_admin_reports():
    if not session.get('admin_logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    period = request.args.get('period', 'today')
    orders = get_cached_orders()
    
    today = datetime.now().date()
    filtered_orders = []
    
    if period == 'today':
        target_date_str = today.strftime('%Y-%m-%d')
        filtered_orders = [o for o in orders if str(o.get('created_at', '')).startswith(target_date_str) and str(o.get('payment_status', '')).lower() == 'paid']
    elif period == '7days':
        start_date = today - timedelta(days=6)
        filtered_orders = [o for o in orders if o.get('created_at') and str(o.get('created_at', ''))[:10] >= start_date.strftime('%Y-%m-%d') and str(o.get('payment_status', '')).lower() == 'paid']
    elif period == '30days':
        start_date = today - timedelta(days=29)
        filtered_orders = [o for o in orders if o.get('created_at') and str(o.get('created_at', ''))[:10] >= start_date.strftime('%Y-%m-%d') and str(o.get('payment_status', '')).lower() == 'paid']

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
            d = today - timedelta(days=i*5)
            chart_labels.append(d.strftime('%d/%m'))
            chart_data.append(total_sales / 6)

    item_counts = {}
    for o in filtered_orders:
        items_raw = o.get('items', '[]')
        try:
            items = json.loads(items_raw)
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