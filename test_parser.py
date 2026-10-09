import email.parser
import email.policy

with open(r'debug_received_data.bin', 'rb') as f:
    received_data = f.read()

# Mock headers exactly as they would be
header_bytes = b"Content-Type: multipart/form-data; boundary=6EF59190-F906-431E-B4F0-C040332CFC47-28868-000003AEFF8D58F3\r\n"
msg_bytes = header_bytes + b"\r\n" + received_data

msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(msg_bytes)

for i, part in enumerate(msg.walk()):
    if part.is_multipart(): continue
    print(f"--- PART {i} ---")
    payload = part.get_payload(decode=True)
    if payload:
        print("Payload starts with:", payload[:50])
        print("Payload ends with:", payload[-50:])
    else:
        print("No payload")
