import win2ios_fileshare as w
with open(r'debug_received_data.bin', 'rb') as f:
    t = f.read().decode('utf-8', errors='ignore')

rtf_start = t.find('{\\rtf1')
if rtf_start != -1:
    rtf_text = t[rtf_start:]
    print('--- STRIPPED ---')
    print(w.strip_rtf(rtf_text).encode('utf-8', errors='ignore').decode('utf-8'))
