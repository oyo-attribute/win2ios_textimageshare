import http.server
import urllib.parse
import socket
import subprocess
import html
import base64
import threading
import webbrowser
import pystray
import re
import io
import os
import tempfile
import email
import email.policy
import email.header
from email.parser import BytesParser
import shutil
import traceback
import mimetypes
import time
from PIL import Image, ImageDraw, ImageGrab

# HEIC形式の画像をPillowで読み込めるようにする
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    print("pillow-heif がインストールされていません。HEIC画像の受信に失敗する可能性があります。")

PORT = 8000
SAVE_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "Win2ios_Files")
os.makedirs(SAVE_DIR, exist_ok=True)

last_ios_type = "TEXT"
last_ios_data = ""
last_win_type = "TEXT"
last_win_data = ""
httpd = None 

def make_safe_filename(filename, max_len=100):
    safe = re.sub(r'[\\/*?:"<>|\r\n]', "_", str(filename or ""))
    safe = safe.strip("._ ") or "file"
    if len(safe) <= max_len:
        return safe
    base, ext = os.path.splitext(safe)
    ext = ext[:10]
    keep = max(1, max_len - len(ext))
    return base[:keep] + ext

def get_multipart_boundary(content_type):
    match = re.search(r'boundary=(["\']?)([^"\';?\s]+)\1', content_type or "", re.I)
    return match.group(2) if match else None

def extract_plain_from_corrupt_header(header_bytes):
    """iOSショートカットのバグで Content-Disposition 内に混入したプレーンテキストを回収。"""
    header = header_bytes.decode("utf-8", errors="ignore")
    # 正常な multipart ヘッダー (name="file"; filename=...) は除外
    if re.search(r'name="file"\s*;', header, re.I):
        return ""
    match = re.search(
        r'Content-Disposition:[^\n]*name="file(.+?)(?:\r\nContent-Type:|\Z)',
        header,
        re.S | re.I,
    )
    if not match:
        return ""
    extracted = match.group(1).strip()
    # iOSバグで name="file[本文]"; filename="..." のようになっているため、末尾の filename= 部分を削る
    extracted = re.sub(r'";\s*filename="?[^"]+"?$', '', extracted, flags=re.I).strip()
    return extracted.strip('"')

def parse_multipart_parts(raw_data, content_type):
    """iOS由来の壊れた multipart でも本文を取り出せるパーサー。"""
    boundary = get_multipart_boundary(content_type)
    if not boundary:
        return []

    delim = b"--" + boundary.encode("ascii", "ignore")
    parts = []
    for segment in raw_data.split(delim)[1:]:
        if segment.startswith(b"--") or not segment.strip():
            continue
        segment = segment.lstrip(b"\r\n")

        ct_match = re.search(br"Content-Type:\s*([^\r\n]+)\r\n\r\n", segment, re.I)
        if ct_match:
            body_start = ct_match.end()
            header_section = segment[:ct_match.start()]
            payload = segment[body_start:]
            c_type = ct_match.group(1).decode("utf-8", errors="ignore").split(";")[0].strip()
        else:
            rtf_pos = segment.find(b"{\\rtf")
            if rtf_pos >= 0:
                header_end = segment.rfind(b"\r\n\r\n", 0, rtf_pos)
                if header_end >= 0:
                    header_section = segment[:header_end]
                    payload = segment[header_end + 4:]
                else:
                    header_section = segment[:rtf_pos]
                    payload = segment[rtf_pos:]
                c_type = "text/rtf"
            else:
                sep = segment.find(b"\r\n\r\n")
                if sep < 0:
                    continue
                header_section = segment[:sep]
                payload = segment[sep + 4:]
                c_type = "text/plain"

        payload = payload.rstrip(b"\r\n")
        header_text = header_section.decode("utf-8", errors="ignore")
        filename = ""
        fn_match = re.search(r'filename="([^"]*)"', header_text, re.I)
        if fn_match:
            filename = fn_match.group(1)
        elif 'name="file' in header_text and b"{\\rtf" in segment:
            filename = "clipboard.rtf"

        parts.append({
            "payload": payload,
            "filename": filename.strip(),
            "content_type": c_type,
            "plain_prefix": extract_plain_from_corrupt_header(header_section),
        })
    return parts

def decode_ios_text_payload(payload, plain_prefix=""):
    txt_data = payload.decode("utf-8", errors="ignore")
    rtf_idx = txt_data.find("{\\rtf1")
    plain_in_body = txt_data[:rtf_idx].strip() if rtf_idx >= 0 else ""
    plain_parts = "\n".join(x for x in [plain_prefix.strip(), plain_in_body] if x).strip()

    if rtf_idx >= 0:
        rtf_part = strip_rtf(txt_data[rtf_idx:]).strip()
        if plain_parts and rtf_part:
            if len(plain_parts) >= len(rtf_part):
                return plain_parts
            if plain_parts not in rtf_part:
                return plain_parts + "\n" + rtf_part
            return rtf_part
        if rtf_part:
            return rtf_part
        if plain_parts:
            return plain_parts

    garbage_pattern = r'(?:";\s*)?filename="[^"]+"(?:\s*[\r\n]+Content-Type:\s*[a-zA-Z0-9/+-]+)?[\r\n]*$'
    garbage_match = re.search(garbage_pattern, txt_data, re.IGNORECASE)
    if garbage_match:
        txt_data = txt_data[:garbage_match.start()].strip()
    if plain_parts:
        return plain_parts
    return txt_data.strip()

def is_likely_clipboard_text(payload, filename, c_type):
    c_type_lower = (c_type or "").lower()
    fname_lower = (filename or "").lower()
    if c_type_lower.startswith("image/"):
        return False
    if payload.startswith((b"\x89PNG", b"\xff\xd8", b"GIF8", b"%PDF", b"PK\x03\x04")):
        return False
    if len(payload) >= 12 and payload[4:12] in (b"ftypheic", b"ftypmif1"):
        return False
    if c_type_lower == "text/rtf" or b"{\\rtf" in payload[:8192]:
        return True
    if c_type_lower.startswith("text/") and (not filename or fname_lower.startswith("clipboard") or len(filename) > 100):
        return True
    try:
        decoded_head = payload[:100].decode("utf-8", errors="ignore").strip()
        if decoded_head.startswith("http://") or decoded_head.startswith("https://"):
            return True
    except Exception:
        pass
    if not filename and (c_type_lower.startswith("text/") or payload[:4096].decode("utf-8", errors="ignore").isprintable()):
        return True
    return False

def strip_rtf(text):
    if not isinstance(text, str): return text
    start_idx = text.find('{\\rtf')
    if start_idx == -1 or start_idx > 20: return text.strip()
    text = text[start_idx:]
    pattern = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)", re.I)
    destinations = frozenset(['colortbl', 'fonttbl', 'stylesheet', 'info', 'bkmkstart', 'bkmkend', 'pict', 'shp', 'shpgrp', 'shpinst', 'shppict', 'shprslt', 'shptxt', 'generator', 'pgp', 'pgptbl', 'xmlnstbl', 'userprops', 'maln', 'mmath', 'mmathPict', 'datafield', 'do', 'ftncn', 'ftnsep', 'ftnsepc', 'hl', 'hlfr', 'hlinkbase', 'hlloc', 'hlsrc', 'keycode', 'keywords', 'listlevel', 'listname', 'listoverride', 'listtable', 'listtext', 'macc', 'maccPr', 'xmlattrname', 'xmlattrvalue', 'xmlclose', 'xmlname', 'xmlopen', 'sn', 'sp', 'sv', 'tc', 'title', 'txe', 'ud', 'upr', 'wgrffmtfilter', 'windowcaption', 'writereservation', 'writereservhash', 'xe', 'xform', 'operator', 'panose', 'password', 'passwordhash', 'comment', 'doccomm', 'docvar', 'dptxbxtext', 'factoidname', 'falt', 'fchars', 'ffdeftext', 'ffentrymcr', 'ffexitmcr', 'ffformat', 'ffhelptext', 'ffl', 'ffname', 'ffstattext', 'field', 'file', 'filetbl', 'fldinst', 'fldrslt', 'fldtype', 'fname', 'fontemb', 'fontfile', 'footer', 'footerf', 'footerl', 'footerr', 'footnote', 'formfield', 'g', 'gridtbl', 'htmltag', 'list', 'macPict', 'private', 'propname', 'protend', 'protstart', 'protusertbl', 'pxe', 'result', 'revtbl', 'revtim', 'rsidtbl', 'rxe', 'datastore', 'defchp', 'defpap'])
    stack, ignorable, ucskip, curskip, out = [], False, 1, 0, []
    for match in pattern.finditer(text):
        word, arg, hex_val, char, brace, tchar = match.groups()
        if brace:
            curskip = 0
            if brace == '{': stack.append((ucskip, ignorable))
            elif brace == '}':
                if stack: ucskip, ignorable = stack.pop()
        elif char:
            curskip = 0
            if char == '~':
                if not ignorable: out.append('\xa0')
            elif char in '{}\\':
                if not ignorable: out.append(char)
            elif char == '*': ignorable = True
        elif word:
            curskip = 0
            if word in ('u', 'uc'):
                if word == 'uc': ucskip = int(arg) if arg else 1
                elif word == 'u':
                    if not ignorable:
                        c = int(arg)
                        if c < 0: c += 0x10000
                        out.append(chr(c))
                    curskip = ucskip
            elif word in destinations: ignorable = True
            elif ignorable: pass
            elif word in ('par', 'line', 'row'): out.append('\n')
            elif word == 'tab': out.append('\t')
            elif word in ('emdash', 'endash', 'bullet', 'lquote', 'rquote', 'ldblquote', 'rdblquote'):
                mapping = {'emdash': '\u2014', 'endash': '\u2013', 'bullet': '\u2022', 'lquote': '\u2018', 'rquote': '\u2019', 'ldblquote': '\u201C', 'rdblquote': '\u201D'}
                out.append(mapping[word])
        elif hex_val:
            if curskip > 0: curskip -= 1
            elif not ignorable:
                c = int(hex_val, 16)
                out.append(chr(c) if c < 128 else '')
        elif tchar:
            if curskip > 0: curskip -= 1
            elif not ignorable: out.append(tchar)
    return ''.join(out).strip()

def process_received_image(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')
        with io.BytesIO() as output:
            img.save(output, format="PNG")
            return output.getvalue()
    except Exception as e:
        print(f"画像処理エラー: {e}")
        return None

def get_from_clipboard_mixed():
    try:
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            with io.BytesIO() as output:
                img.save(output, format="PNG")
                return "IMAGE", output.getvalue()
        elif isinstance(img, list) and len(img) > 0:
            path = img[0]
            if isinstance(path, str):
                if path.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.heic')):
                    with open(path, 'rb') as f:
                        return "IMAGE", f.read()
                elif os.path.isfile(path):
                    return "FILE", path
    except Exception:
        pass

    try:
        ps_script = """
        Add-Type -AssemblyName System.Windows.Forms
        if ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) {
            $files = [System.Windows.Forms.Clipboard]::GetFileDropList()
            if ($files.Count -gt 0) {
                Write-Output ("FILE::" + $files[0])
                exit
            }
        }
        if ([System.Windows.Forms.Clipboard]::ContainsText()) {
            $text = [System.Windows.Forms.Clipboard]::GetText()
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
            Write-Output ("TEXT::" + [Convert]::ToBase64String($bytes))
            exit
        }
        Write-Output ("NONE::")
        """
        result = subprocess.check_output(
            ['powershell', '-Sta', '-NoProfile', '-NonInteractive', '-Command', ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5
        ).strip().decode('utf-8', errors='ignore')

        if result.startswith("FILE::"):
            filepath = result[6:]
            if os.path.exists(filepath):
                if filepath.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.heic')):
                    with open(filepath, 'rb') as f:
                        return "IMAGE", f.read()
                return "FILE", filepath
        elif result.startswith("TEXT::"):
            b64_text = result[6:]
            if b64_text:
                return "TEXT", base64.b64decode(b64_text).decode('utf-8')
    except Exception:
        pass
    return "NONE", None

def copy_to_clipboard(text):
    if not text or not text.strip():
        return
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        ps_script = f"""
        $text = Get-Content -Raw -Encoding UTF8 '{temp_path.replace("'", "''")}'
        Set-Clipboard -Value $text
        """
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

def set_clipboard_image(image_bytes):
    try:
        fd, temp_path = tempfile.mkstemp(suffix='.png')
        with os.fdopen(fd, 'wb') as f:
            f.write(image_bytes)
        ps_script = f"""
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        try {{
            $img = [System.Drawing.Image]::FromFile('{temp_path}')
            [System.Windows.Forms.Clipboard]::SetImage($img)
            $img.Dispose()
        }} catch {{}}
        """
        subprocess.run(['powershell', '-Sta', '-NoProfile', '-NonInteractive', '-Command', ps_script], check=False, creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        os.remove(temp_path)
    except Exception:
        pass

def get_ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

SERVER_IP = get_ip_address()

# ---------------------------------------------------------
# 🌟 【完成版】データ透視エンジン（中身から真の姿を判定）
# ---------------------------------------------------------
def process_payload(payload, filename, c_type, plain_prefix=""):
    global last_ios_type, last_ios_data
    raw_filename = (filename or "").strip()
    filename = make_safe_filename(raw_filename) if raw_filename else ""
    fname_lower = filename.lower()
    c_type_lower = c_type.lower()
    
    guessed_ext = mimetypes.guess_extension(c_type_lower.split(';')[0]) or '.dat'
    if guessed_ext == '.jpe': guessed_ext = '.jpg'
    if guessed_ext == '.htm': guessed_ext = '.html'
    
    # マジックバイト（ファイル先頭の指紋）で中身を透視
    is_png = payload.startswith(b'\x89PNG')
    is_jpeg = payload.startswith(b'\xff\xd8')
    is_gif = payload.startswith(b'GIF8')
    is_heic = payload[4:12] == b'ftypheic' or payload[4:12] == b'ftypmif1'
    is_pdf = payload.startswith(b'%PDF')
    is_zip = payload.startswith(b'PK\x03\x04') # DOCX, XLSXなどもZIP形式
    
    payload_head = payload[:1024].lower()
    is_html = b'<html' in payload_head or b'<!doctype html' in payload_head
    
    # 判定フラグ
    is_image = is_png or is_jpeg or is_gif or is_heic or c_type_lower.startswith('image/') or fname_lower.endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.heic'))
    
    # テキストかどうかの判定（テキストベースのファイルも含むが、ファイル名がある場合はファイルとして扱う）
    is_text_type = c_type_lower.startswith('text/') and not is_html
    
    # クリップボード由来テキスト（iOSの壊れた filename も含む）
    is_clipboard_rtf = is_likely_clipboard_text(payload, raw_filename, c_type)
    
    # 1. 画像の処理（ファイルとして送られてもクリップボードへ！）
    if is_image:
        valid_img = process_received_image(payload)
        if valid_img:
            set_clipboard_image(valid_img)
            last_ios_type, last_ios_data = "IMAGE", valid_img
            return True
    
    # 1.5. Safari共有によるHTMLファイル → URLを抽出してテキストとしてクリップボードへ
    elif is_html and raw_filename and fname_lower.endswith('.html'):
        try:
            html_text = payload.decode("utf-8", errors="ignore")
            url_found = ""
            # canonical URL を探す
            m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html_text, re.I)
            if not m:
                m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']', html_text, re.I)
            if m:
                url_found = m.group(1)
            # og:url を探す
            if not url_found:
                m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
                if not m:
                    m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']', html_text, re.I)
                if m:
                    url_found = m.group(1)
            if url_found and url_found.startswith("http"):
                copy_to_clipboard(url_found)
                last_ios_type, last_ios_data = "TEXT", url_found
                return True
        except Exception:
            pass
            
    # 2. テキストの処理
    elif is_clipboard_rtf:
        try:
            clean_txt = decode_ios_text_payload(payload, plain_prefix)
            if clean_txt:
                copy_to_clipboard(clean_txt)
                last_ios_type, last_ios_data = "TEXT", clean_txt
                return True
        except Exception:
            pass
    elif (c_type_lower.startswith('text/') or payload[:4096].decode("utf-8", errors="ignore").isprintable()) and not raw_filename:
        try:
            clean_txt = decode_ios_text_payload(payload, plain_prefix)
            if clean_txt:
                copy_to_clipboard(clean_txt)
                last_ios_type, last_ios_data = "TEXT", clean_txt
                return True
        except Exception:
            pass
            
    # 3. ファイルの処理（上記以外はすべてファイルとして保存！）
    ext = guessed_ext
    if is_pdf: ext = '.pdf'
    elif is_zip: 
        ext = '.zip'
        if fname_lower.endswith('.xlsx') or 'spreadsheetml' in c_type_lower: ext = '.xlsx'
        elif fname_lower.endswith('.docx') or 'wordprocessingml' in c_type_lower: ext = '.docx'
    elif is_html: ext = '.html'
    elif '.' in filename: ext = '.' + filename.split('.')[-1]
    
    # iPhoneが勝手につけた「ファイル」や「クリップボード」という名前なら書き換える
    if not filename or filename.lower() == "file" or filename.startswith("clipboard"):
        filename = f"ReceivedFile_{int(time.time())}{ext}"
        
    filepath = os.path.join(SAVE_DIR, filename)
    try:
        with open(filepath, 'wb') as f:
            f.write(payload)
        subprocess.Popen(['explorer', '/select,', filepath])
        last_ios_type, last_ios_data = "FILE", filepath
        return True
    except OSError as e:
        print(f"ファイル保存エラー: {e}")
        return False

class ShareHandler(http.server.SimpleHTTPRequestHandler):
    def address_string(self): return self.client_address[0]
    def log_message(self, format, *args): pass
    def handle(self):
        try: super().handle()
        except Exception: pass

    def do_GET(self):
        global last_ios_type, last_ios_data, last_win_type, last_win_data
        try:
            ctype, cdata = get_from_clipboard_mixed()
            if ctype != "NONE":
                last_win_type, last_win_data = ctype, cdata

            if self.path == '/':
                def format_html_content(item_type, item_data, fallback):
                    if item_type == "IMAGE":
                        b64 = base64.b64encode(item_data).decode('utf-8')
                        return f'<img src="data:image/png;base64,{b64}" />'
                    elif item_type == "FILE":
                        fname = os.path.basename(str(item_data))
                        ext = fname.split('.')[-1].lower() if '.' in fname else ''
                        icon = "📄"
                        if ext in ['csv', 'xls', 'xlsx']: icon = "📊"
                        elif ext in ['pdf']: icon = "📕"
                        elif ext in ['html', 'htm']: icon = "🌐"
                        elif ext in ['zip', 'rar', '7z']: icon = "📦"
                        elif ext in ['py', 'js', 'txt']: icon = "📝"
                        
                        return f'''
                        <div class="file-box">
                            <div class="file-icon">{icon}</div>
                            <div class="file-info">
                                <strong>ファイルを受信しました</strong><br>
                                <span class="file-name">{html.escape(fname)}</span><br>
                                <span class="file-path">{html.escape(str(item_data))}</span>
                            </div>
                        </div>
                        '''
                    elif item_type == "TEXT" and item_data:
                        return f'<div class="content-area">{html.escape(item_data)}</div>'
                    return f'<div class="placeholder">{html.escape(fallback)}</div>'

                ios_html = format_html_content(last_ios_type, last_ios_data, "まだデータを受信していません。")
                win_html = format_html_content(last_win_type, last_win_data, "PCクリップボードは空です。")
                
                html_content = f"""
                <!DOCTYPE html>
                <html lang="ja">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Win2ios Share UI</title>
                    <style>
                        :root {{ --bg: #f8fafc; --text: #334155; --primary: #0ea5e9; --card: #ffffff; }}
                        body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 2rem; }}
                        .container {{ max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }}
                        .header {{ text-align: center; margin-bottom: 2rem; }}
                        .header h1 {{ margin: 0; color: #0f172a; display: flex; align-items: center; justify-content: center; gap: 10px; }}
                        .badge {{ background: var(--primary); color: white; padding: 0.2rem 0.6rem; border-radius: 999px; font-size: 0.8rem; font-weight: bold; vertical-align: middle; }}
                        .save-path {{ color: #64748b; font-size: 0.9rem; margin-top: 0.5rem; }}
                        .card {{ background: var(--card); border-radius: 12px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); padding: 1.5rem; }}
                        .card h3 {{ margin-top: 0; border-bottom: 2px solid #f1f5f9; padding-bottom: 10px; color: #0f172a; }}
                        .content-area {{ background: #1e293b; color: #f8fafc; padding: 1rem; border-radius: 8px; font-family: Consolas, monospace; white-space: pre-wrap; word-break: break-all; max-height: 400px; overflow-y: auto; }}
                        .file-box {{ background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 8px; padding: 1rem; display: flex; align-items: center; gap: 1rem; }}
                        .file-icon {{ font-size: 2.5rem; }}
                        .file-info strong {{ color: var(--primary); font-size: 1.1rem; }}
                        .file-name {{ font-weight: bold; color: #334155; }}
                        .file-path {{ font-size: 0.85rem; color: #64748b; word-break: break-all; }}
                        .placeholder {{ color: #94a3b8; font-style: italic; text-align: center; padding: 2rem; }}
                        img {{ max-width: 100%; border-radius: 8px; box-shadow: 0 2px 4px rgb(0 0 0 / 0.1); }}
                        @media (max-width: 768px) {{ .container {{ grid-template-columns: 1fr; }} body {{ padding: 1rem; }} }}
                    </style>
                </head>
                <body>
                    <div class="header">
                        <h1>Win2ios 共有サーバー <span class="badge">v5.1</span></h1>
                        <div class="save-path">📁 ファイル保存先: {html.escape(SAVE_DIR)}</div>
                    </div>
                    <div class="container">
                        <div class="card">
                            <h3>📱 iOSから受信したデータ</h3>
                            <div>{ios_html}</div>
                        </div>
                        <div class="card">
                            <h3>💻 Windowsのクリップボード</h3>
                            <div>{win_html}</div>
                        </div>
                    </div>
                </body>
                </html>
                """
                encoded_html = html_content.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded_html)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(encoded_html)
                
            elif self.path == '/api/sync':
                # GET時のレスポンス（PCからiOSへ）
                if last_win_type == "IMAGE":
                    self.send_response(200)
                    self.send_header('Content-type', 'image/png')
                    self.send_header('Content-Disposition', 'inline; filename="clipboard.png"') # iPhoneに画像と教える！
                    self.send_header('Content-Length', str(len(last_win_data)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(last_win_data)
                elif last_win_type == "FILE" and os.path.isfile(str(last_win_data)):
                    filepath = last_win_data
                    file_size = os.path.getsize(filepath)
                    filename = os.path.basename(filepath)
                    ascii_name = filename.encode('ascii', 'ignore').decode('ascii') or "download.dat"
                    safe_filename = urllib.parse.quote(filename)
                    mime_type, _ = mimetypes.guess_type(filepath)
                    if not mime_type: mime_type = 'application/octet-stream'
                    self.send_response(200)
                    self.send_header('Content-type', mime_type)
                    self.send_header('Content-Disposition', f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{safe_filename}')
                    self.send_header('Content-Length', str(file_size))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    with open(filepath, 'rb') as f: shutil.copyfileobj(f, self.wfile)
                else:
                    resp_text = str(last_win_data) if last_win_type in ("TEXT", "FILE") else ""
                    encoded = resp_text.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain; charset=utf-8')
                    self.send_header('Content-Disposition', 'inline; filename="clipboard.txt"') # iPhoneにテキストと教える！
                    self.send_header('Content-Length', str(len(encoded)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(encoded)
            else:
                self.send_error(404)
        except Exception as e:
            print(f"GETエラー: {e}")
            try: self.send_error(500)
            except: pass

    def do_POST(self):
        global last_ios_type, last_ios_data, last_win_type, last_win_data
        try:
            if self.path == '/api/sync':
                
                resp_type, resp_data = get_from_clipboard_mixed()
                if resp_type == "NONE":
                    resp_type, resp_data = last_win_type, last_win_data
                    
                content_length = int(self.headers.get('Content-Length', 0))
                content_type = self.headers.get('Content-Type', '')

                handled = False
                if content_length > 0:
                    received_data = self.rfile.read(content_length)
                    
                    if content_type.startswith('multipart/'):
                        for part in parse_multipart_parts(received_data, content_type):
                            handled = process_payload(
                                part["payload"],
                                part["filename"],
                                part["content_type"],
                                part.get("plain_prefix", ""),
                            )
                            if handled:
                                break

                        if not handled:
                            try:
                                header_bytes = b"".join(
                                    f"{k}: {v}\r\n".encode("utf-8", errors="ignore")
                                    for k, v in self.headers.items()
                                )
                                msg = BytesParser(policy=email.policy.default).parsebytes(
                                    header_bytes + b"\r\n" + received_data
                                )
                                for part in msg.walk():
                                    if part.is_multipart():
                                        continue
                                    payload = part.get_payload(decode=True)
                                    if not payload:
                                        continue
                                    filename = part.get_filename() or ""
                                    if filename:
                                        decoded = email.header.decode_header(filename)
                                        filename = "".join(
                                            seq.decode(charset or "utf-8", errors="ignore")
                                            if isinstance(seq, bytes)
                                            else str(seq)
                                            for seq, charset in decoded
                                        )
                                    handled = process_payload(
                                        payload,
                                        make_safe_filename(filename).strip(),
                                        part.get_content_type(),
                                    )
                                    if handled:
                                        break
                            except Exception as e:
                                print(f"multipart fallback parse error: {e}")
                    
                    if not handled:
                        handled = process_payload(received_data, "", content_type)

                last_win_type, last_win_data = get_from_clipboard_mixed()

                # iOSへのレスポンス送信（POST後）
                if resp_type == "IMAGE":
                    self.send_response(200)
                    self.send_header('Content-type', 'image/png')
                    self.send_header('Content-Disposition', 'inline; filename="clipboard.png"') # 確実化
                    self.send_header('Content-Length', str(len(resp_data)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(resp_data)
                elif resp_type == "FILE" and os.path.isfile(str(resp_data)):
                    filepath = resp_data
                    file_size = os.path.getsize(filepath)
                    filename = os.path.basename(filepath)
                    ascii_name = filename.encode('ascii', 'ignore').decode('ascii') or "download.dat"
                    safe_filename = urllib.parse.quote(filename)
                    mime_type, _ = mimetypes.guess_type(filepath)
                    if not mime_type: mime_type = 'application/octet-stream'
                    self.send_response(200)
                    self.send_header('Content-type', mime_type)
                    self.send_header('Content-Disposition', f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{safe_filename}')
                    self.send_header('Content-Length', str(file_size))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    with open(filepath, 'rb') as f: shutil.copyfileobj(f, self.wfile)
                else:
                    r_text = str(resp_data) if resp_type in ("TEXT", "FILE") else ""
                    encoded = r_text.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain; charset=utf-8')
                    self.send_header('Content-Disposition', 'inline; filename="clipboard.txt"') # 確実化
                    self.send_header('Content-Length', str(len(encoded)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(encoded)
            else:
                self.send_error(404)
                
        except Exception as e:
            print("=== POST処理中にエラーが発生しました ===")
            traceback.print_exc()
            try:
                self.send_response(500)
                self.send_header('Connection', 'close')
                self.end_headers()
            except: pass

def start_server():
    global httpd
    httpd = http.server.ThreadingHTTPServer(("", PORT), ShareHandler)
    httpd.serve_forever()

def open_browser(icon, item): webbrowser.open(f"http://localhost:{PORT}")
def exit_action(icon, item):
    icon.stop()
    if httpd: httpd.shutdown()

def create_image():
    image = Image.new('RGB', (64, 64), color=(0, 120, 212))
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 52, 52), outline="white", width=4)
    draw.rectangle((24, 24, 40, 40), fill="white")
    return image

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    icon = pystray.Icon("Win2ios")
    icon.menu = pystray.Menu(pystray.MenuItem("モニタを開く", open_browser), pystray.MenuItem("終了", exit_action))
    icon.icon = create_image()
    icon.title = f"Win2ios 待機中 ({SERVER_IP}:{PORT})"
    icon.run()