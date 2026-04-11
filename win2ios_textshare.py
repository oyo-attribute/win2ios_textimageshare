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
from PIL import Image, ImageDraw

# 使用するポート番号
PORT = 8000

# iOS ↔ Win 用の変数
text_ios_to_win = "まだiOSからテキストを受信していません。"
text_win_to_ios = "PCでコピーしたテキストがここに表示され、iOSへ送られます。"
httpd = None # サーバー停止用

def strip_rtf(text):
    if not isinstance(text, str):
        return text
        
    # RTFの開始シグネチャを探す（BOM等の見えない文字が含まれている場合を考慮）
    start_idx = text.find('{\\rtf')
    if start_idx == -1 or start_idx > 20:
        return text.strip()
        
    text = text[start_idx:]

    pattern = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)", re.I)
    destinations = frozenset([
        'colortbl', 'fonttbl', 'stylesheet', 'info', 'bkmkstart', 'bkmkend', 'pict', 
        'shp', 'shpgrp', 'shpinst', 'shppict', 'shprslt', 'shptxt', 'generator', 
        'pgp', 'pgptbl', 'xmlnstbl', 'userprops', 'maln', 'mmath', 'mmathPict', 
        'datafield', 'do', 'ftncn', 'ftnsep', 'ftnsepc', 'hl', 'hlfr', 'hlinkbase', 
        'hlloc', 'hlsrc', 'keycode', 'keywords', 'listlevel', 'listname', 'listoverride', 
        'listtable', 'listtext', 'macc', 'maccPr', 'xmlattrname', 'xmlattrvalue', 
        'xmlclose', 'xmlname', 'xmlopen', 'sn', 'sp', 'sv', 'tc', 'title', 'txe', 'ud', 
        'upr', 'wgrffmtfilter', 'windowcaption', 'writereservation', 'writereservhash', 
        'xe', 'xform', 'operator', 'panose', 'password', 'passwordhash', 'comment', 
        'doccomm', 'docvar', 'dptxbxtext', 'factoidname', 'falt', 'fchars', 'ffdeftext', 
        'ffentrymcr', 'ffexitmcr', 'ffformat', 'ffhelptext', 'ffl', 'ffname', 
        'ffstattext', 'field', 'file', 'filetbl', 'fldinst', 'fldrslt', 'fldtype', 
        'fname', 'fontemb', 'fontfile', 'footer', 'footerf', 'footerl', 'footerr', 
        'footnote', 'formfield', 'g', 'gridtbl', 'htmltag', 'list', 'macPict', 
        'private', 'propname', 'protend', 'protstart', 'protusertbl', 'pxe', 
        'result', 'revtbl', 'revtim', 'rsidtbl', 'rxe', 'datastore', 'defchp', 'defpap'
    ])
    
    stack = []
    ignorable = False
    ucskip = 1
    curskip = 0
    out = []
    
    for match in pattern.finditer(text):
        word, arg, hex_val, char, brace, tchar = match.groups()
        if brace:
            curskip = 0
            if brace == '{':
                stack.append((ucskip, ignorable))
            elif brace == '}':
                if stack:
                    ucskip, ignorable = stack.pop()
        elif char:
            curskip = 0
            if char == '~':
                if not ignorable: out.append('\xa0')
            elif char in '{}\\':
                if not ignorable: out.append(char)
            elif char == '*':
                ignorable = True
        elif word:
            curskip = 0
            if word in ('u', 'uc'):
                if word == 'uc':
                    ucskip = int(arg) if arg else 1
                elif word == 'u':
                    if not ignorable:
                        c = int(arg)
                        if c < 0: c += 0x10000
                        out.append(chr(c))
                    curskip = ucskip
            elif word in destinations:
                ignorable = True
            elif ignorable:
                pass
            elif word in ('par', 'line', 'row'):
                out.append('\n')
            elif word == 'tab':
                out.append('\t')
            elif word == 'emdash':
                out.append('\u2014')
            elif word == 'endash':
                out.append('\u2013')
            elif word == 'bullet':
                out.append('\u2022')
            elif word == 'lquote':
                out.append('\u2018')
            elif word == 'rquote':
                out.append('\u2019')
            elif word == 'ldblquote':
                out.append('\u201C')
            elif word == 'rdblquote':
                out.append('\u201D')
        elif hex_val:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                c = int(hex_val, 16)
                out.append(chr(c) if c < 128 else '')
        elif tchar:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                out.append(tchar)
    return ''.join(out).strip()

def get_from_clipboard():
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
            creationflags=subprocess.CREATE_NO_WINDOW
        ).strip()
        
        if result:
            return base64.b64decode(result).decode('utf-8')
        return ""
    except Exception:
        return ""

def copy_to_clipboard(text):
    if not text or not text.strip():
        return
    try:
        b64_text = base64.b64encode(text.encode('utf-8')).decode('utf-8')
        ps_script = f"""
        $bytes = [Convert]::FromBase64String('{b64_text}')
        $decoded_text = [System.Text.Encoding]::UTF8.GetString($bytes)
        Set-Clipboard -Value $decoded_text
        """
        subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script], 
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
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
    def address_string(self):
        return self.client_address[0]

    def handle(self):
        try:
            super().handle()
        except Exception:
            pass

    def do_GET(self):
        global text_ios_to_win, text_win_to_ios
        if self.path == '/':
            current_clip = get_from_clipboard()
            if current_clip:
                text_win_to_ios = current_clip

            safe_ios_text = html.escape(text_ios_to_win)
            safe_win_text = html.escape(text_win_to_ios)
            
            html_content = f"""
            <!DOCTYPE html>
            <html lang="ja">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Win2ios Text Share</title>
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background-color: #eef2f5; color: #333; }}
                    .header {{ text-align: center; margin-bottom: 20px; color: #005a9e; }}
                    .container {{ display: flex; gap: 20px; max-width: 1200px; margin: 0 auto; align-items: flex-start; }}
                    .left-panel, .right-panel {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }}
                    .left-panel {{ flex: 1; }} .right-panel {{ flex: 1; }}
                    h3 {{ margin-top: 0; padding-bottom: 10px; border-bottom: 2px solid #f0f0f0; font-size: 1.2rem; }}
                    .current-text {{ background: #282c34; color: #abb2bf; padding: 15px; border-radius: 6px; white-space: pre-wrap; word-wrap: break-word; font-size: 1rem; border: 1px solid #ddd; max-height: 65vh; overflow-y: auto; font-family: Consolas, monospace; }}
                    @media (max-width: 768px) {{ .container {{ flex-direction: column; }} .left-panel, .right-panel {{ width: 100%; box-sizing: border-box; }} }}
                </style>
            </head>
            <body>
                <div class="header">
                    <h2>Win2ios テキスト共有サーバー (常駐モード)</h2>
                    <p>このページを閉じても、タスクトレイからいつでも呼び出せます。</p>
                </div>
                <div class="container">
                    <div class="left-panel">
                        <h3>📱 iOSから受信したテキスト</h3>
                        <div class="current-text">{safe_ios_text}</div>
                    </div>
                    <div class="right-panel">
                        <h3>💻 Windowsのクリップボード</h3>
                        <div class="current-text" style="background: #f0f0f0; color: #333;">{safe_win_text}</div>
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
            current_clip = get_from_clipboard()
            encoded_response = (current_clip if current_clip else text_win_to_ios).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded_response)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(encoded_response)
        else:
            self.send_error(404)

    def do_POST(self):
        global text_ios_to_win, text_win_to_ios
        if self.path == '/api/sync':
            current_win_clip = get_from_clipboard()
            response_text = current_win_clip if current_win_clip else text_win_to_ios
            
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                received_data = self.rfile.read(content_length).decode('utf-8')
                text_ios_to_win = strip_rtf(received_data)
                copy_to_clipboard(text_ios_to_win)
            
            encoded_response = response_text.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded_response)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(encoded_response)
        else:
            self.send_error(404)

# --- タスクトレイ常駐用の処理 ---

def start_server():
    global httpd
    httpd = http.server.ThreadingHTTPServer(("", PORT), ShareHandler)
    httpd.serve_forever()

def open_browser(icon, item):
    webbrowser.open(f"http://localhost:{PORT}")

def exit_action(icon, item):
    icon.stop()
    if httpd:
        httpd.shutdown()

def create_image():
    # 動的に青いアイコンを生成
    image = Image.new('RGB', (64, 64), color=(0, 120, 212))
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 52, 52), outline="white", width=4)
    draw.rectangle((24, 24, 40, 40), fill="white")
    return image

if __name__ == "__main__":
    # サーバーをバックグラウンドスレッドで起動
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # タスクトレイアイコンの設定と起動
    icon = pystray.Icon("Win2ios")
    icon.menu = pystray.Menu(
        pystray.MenuItem("ブラウザでモニタを開く", open_browser),
        pystray.MenuItem("終了", exit_action)
    )
    icon.icon = create_image()
    icon.title = f"Win2ios 待機中 ({SERVER_IP}:{PORT})"
    
    # ここで常駐ループに入ります
    icon.run()