#!/usr/bin/env python3
# TLS 1.0 / AES256-SHA logging server that mimics Coway_iTrust.
# Logs: did the purifier complete the handshake, and every byte it sends after.
import socket, ssl, sys, time, threading, binascii, os

HERE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(HERE, "coway.pem")
KEY  = os.path.join(HERE, "coway.key")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9090
LOG  = os.path.join(HERE, "capture.log")

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.minimum_version = ssl.TLSVersion.TLSv1
ctx.maximum_version = ssl.TLSVersion.TLSv1_2
try:
    ctx.set_ciphers("ALL:@SECLEVEL=0")   # allow AES256-SHA (TLS1.0)
except ssl.SSLError as e:
    log(f"cipher set failed: {e}")
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE          # server asks for no client cert (matches Coway)
ctx.load_cert_chain(CERT, KEY)

def handle(raw, addr):
    log(f"TCP connect from {addr}")
    try:
        tls = ctx.wrap_socket(raw, server_side=True)
    except ssl.SSLError as e:
        log(f"HANDSHAKE FAILED from {addr}: {e}  -> purifier likely pins/validates the cert")
        try: raw.close()
        except Exception: pass
        return
    log(f"HANDSHAKE OK from {addr}: {tls.version()} / {tls.cipher()}  -> cert accepted!")
    tls.settimeout(30)
    total = 0
    try:
        while True:
            data = tls.recv(4096)
            if not data:
                log(f"{addr} closed by peer after {total} bytes")
                break
            total += len(data)
            log(f"RECV {len(data)}B hex: {binascii.hexlify(data).decode()}")
            log(f"RECV ascii: {data.decode('latin1')!r}")
    except Exception as e:
        log(f"{addr} recv ended: {e}")
    finally:
        try: tls.close()
        except Exception: pass

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(5)
    log(f"listening on 0.0.0.0:{PORT}  cert=Coway_iTrust  (log -> {LOG})")
    while True:
        c, a = s.accept()
        threading.Thread(target=handle, args=(c, a), daemon=True).start()

if __name__ == "__main__":
    main()
