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
from PIL import Image, ImageDraw, ImageGrab

# ★ ここを追加：HEIC形式の画像をPillowで読み込めるようにする
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    print("pillow-heif がインストールされていません。HEIC画像の受信に失敗する可能性があります。")

# 使用するポート番号
PORT = 8000

# iOS ↔ Win 用の変数
last_ios_type = "TEXT"
last_ios_data = "まだiOSからデータを受信していません。"
last_win_type = "TEXT"
last_win_data = "PCでコピーしたテキストまたは画像がここに表示され、iOSへ送られます。"
httpd = None 

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
    """受信したデータが正常な画像か確認し、PNGに変換して返す（HEICも対応）"""
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
            if isinstance(path, str) and path.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.heic')):
                with open(path, 'rb') as f:
                    return "IMAGE", f.read()
    except Exception:
        pass

    try:
        ps_script = """
        $text = Get-Clipboard -Raw -ErrorAction SilentlyContinue
        if ($text) {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
            [Convert]::ToBase64String($bytes)
        }
        """
        result = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command', ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).strip()
        if result: return "TEXT", base64.b64decode(result).decode('utf-8')
    except Exception:
        pass
    return "NONE", None

def copy_to_clipboard(text):
    if not text or not text.strip(): return
    try:
        b64_text = base64.b64encode(text.encode('utf-8')).decode('utf-8')
        ps_script = f"""
        $bytes = [Convert]::FromBase64String('{b64_text}')
        $decoded_text = [System.Text.Encoding]::UTF8.GetString($bytes)
        Set-Clipboard -Value $decoded_text
        """
        subprocess.run(['powershell', '-NoProfile', '-Command', ps_script], check=True, creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
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
        subprocess.run(['powershell', '-Sta', '-NoProfile', '-Command', ps_script], check=False, creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

class ShareHandler(http.server.SimpleHTTPRequestHandler):
    def address_string(self): return self.client_address[0]
    def log_message(self, format, *args): pass
    def handle(self):
        try: super().handle()
        except Exception: pass

    def do_GET(self):
        global last_ios_type, last_ios_data, last_win_type, last_win_data
        ctype, cdata = get_from_clipboard_mixed()
        if ctype != "NONE":
            last_win_type, last_win_data = ctype, cdata

        if self.path == '/':
            def format_html_content(item_type, item_data, fallback):
                if item_type == "IMAGE":
                    b64 = base64.b64encode(item_data).decode('utf-8')
                    return f'<img src="data:image/png;base64,{b64}" />'
                elif item_type == "TEXT" and item_data:
                    return html.escape(item_data)
                return html.escape(fallback)

            ios_html = format_html_content(last_ios_type, last_ios_data, "まだデータを受信していません。")
            win_html = format_html_content(last_win_type, last_win_data, "PCクリップボードは空です。")
            
            html_content = f"""
            <!DOCTYPE html>
            <html lang="ja">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Win2ios Text & Image Share</title>
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background-color: #eef2f5; color: #333; }}
                    .header {{ text-align: center; margin-bottom: 20px; color: #005a9e; }}
                    .container {{ display: flex; gap: 20px; max-width: 1200px; margin: 0 auto; align-items: flex-start; }}
                    .left-panel, .right-panel {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }}
                    .left-panel {{ flex: 1; }} .right-panel {{ flex: 1; }}
                    h3 {{ margin-top: 0; padding-bottom: 10px; border-bottom: 2px solid #f0f0f0; font-size: 1.2rem; }}
                    .current-text {{ background: #282c34; color: #abb2bf; padding: 15px; border-radius: 6px; white-space: pre-wrap; word-wrap: break-word; font-size: 1rem; border: 1px solid #ddd; max-height: 65vh; overflow-y: auto; font-family: Consolas, monospace; }}
                    .current-text img {{ max-width: 100%; height: auto; display: block; border-radius: 4px; }}
                    @media (max-width: 768px) {{ .container {{ flex-direction: column; }} .left-panel, .right-panel {{ width: 100%; box-sizing: border-box; }} }}
                </style>
            </head>
            <body>
                <div class="header">
                    <h2>Win2ios 共有サーバー (常駐モード)</h2>
                    <p>このページを閉じても、タスクトレイからいつでも呼び出せます。</p>
                </div>
                <div class="container">
                    <div class="left-panel">
                        <h3>📱 iOSから受信したデータ</h3>
                        <div class="current-text">{ios_html}</div>
                    </div>
                    <div class="right-panel">
                        <h3>💻 Windowsのクリップボード</h3>
                        <div class="current-text" style="background: #f0f0f0; color: #333;">{win_html}</div>
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
            if last_win_type == "IMAGE":
                self.send_response(200)
                self.send_header('Content-type', 'image/png')
                self.send_header('Content-Length', str(len(last_win_data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(last_win_data)
            else:
                resp_text = last_win_data if last_win_type == "TEXT" else ""
                encoded = resp_text.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(encoded)
        else:
            self.send_error(404)

    def do_POST(self):
        global last_ios_type, last_ios_data, last_win_type, last_win_data
        if self.path == '/api/sync':
            
            resp_type, resp_data = get_from_clipboard_mixed()
            if resp_type == "NONE":
                resp_type, resp_data = last_win_type, last_win_data
                
            content_length = int(self.headers.get('Content-Length', 0))
            content_type = self.headers.get('Content-Type', '')

            if content_length > 0:
                received_data = self.rfile.read(content_length)
                handled = False
                
                if content_type.startswith('multipart/'):
                    msg = email.message_from_bytes(f"Content-Type: {content_type}\r\n\r\n".encode('utf-8') + received_data, policy=email.policy.default)
                    for part in msg.walk():
                        if part.is_multipart(): continue
                        c_type = part.get_content_type()
                        if c_type.startswith('image/'):
                            img_data = part.get_payload(decode=True)
                            valid_img = process_received_image(img_data)
                            if valid_img:
                                set_clipboard_image(valid_img)
                                last_ios_type, last_ios_data = "IMAGE", valid_img
                            else:
                                last_ios_type, last_ios_data = "TEXT", "⚠️ [エラー] 画像の読み込みに失敗しました。"
                            handled = True
                            break
                        elif c_type.startswith('text/'):
                            txt_data = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            clean_txt = strip_rtf(txt_data)
                            copy_to_clipboard(clean_txt)
                            last_ios_type, last_ios_data = "TEXT", clean_txt
                            handled = True
                            break
                
                if not handled:
                    is_image = False
                    if 'image/' in content_type.lower():
                        is_image = True
                    else:
                        try:
                            decoded_text = received_data.decode('utf-8')
                        except UnicodeDecodeError:
                            is_image = True
                    
                    if is_image:
                        valid_img = process_received_image(received_data)
                        if valid_img:
                            set_clipboard_image(valid_img)
                            last_ios_type, last_ios_data = "IMAGE", valid_img
                        else:
                            last_ios_type, last_ios_data = "TEXT", "⚠️ [エラー] 画像の形式が非対応か、データが壊れています。"
                    else:
                        clean_txt = strip_rtf(decoded_text)
                        copy_to_clipboard(clean_txt)
                        last_ios_type, last_ios_data = "TEXT", clean_txt

            last_win_type, last_win_data = get_from_clipboard_mixed()

            if resp_type == "IMAGE":
                self.send_response(200)
                self.send_header('Content-type', 'image/png')
                self.send_header('Content-Length', str(len(resp_data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(resp_data)
            else:
                r_text = resp_data if resp_type == "TEXT" else ""
                encoded = r_text.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(encoded)
        else:
            self.send_error(404)

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
    icon.menu = pystray.Menu(pystray.MenuItem("ブラウザでモニタを開く", open_browser), pystray.MenuItem("終了", exit_action))
    icon.icon = create_image()
    icon.title = f"Win2ios 待機中 ({SERVER_IP}:{PORT})"
    icon.run()
