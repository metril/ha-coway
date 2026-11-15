#!/usr/bin/env python3
# Transparent MITM relay for the Coway 400S port-9090 link, on the user's own net.
# Purifier --TLS--> us (Coway_iTrust cert) --TLS--> real Coway cloud. Logs 02..03 frames both ways.
import socket, ssl, sys, time, threading, binascii, os

HERE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(HERE, "coway.pem")
KEY  = os.path.join(HERE, "coway.key")
LISTEN_PORT = 9090
UPSTREAM = ("3.37.118.114", 9090)   # real Coway cloud (AWS Seoul)
LOG = os.path.join(HERE, "frames.log")

def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def show(direction, data):
    # split on ETX to print whole 02..03 frames readably
    log(f"{direction} {len(data)}B hex={binascii.hexlify(data).decode()}")
    log(f"{direction} ascii={data.decode('latin1')!r}")

# server side (faces purifier): present Coway_iTrust, any legacy cipher, no client cert
srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
srv.minimum_version = ssl.TLSVersion.TLSv1
srv.maximum_version = ssl.TLSVersion.TLSv1_2
srv.set_ciphers("ALL:@SECLEVEL=0")
srv.verify_mode = ssl.CERT_NONE
srv.load_cert_chain(CERT, KEY)

# client side (faces real cloud): accept its self-signed cert, old TLS
cli = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
cli.minimum_version = ssl.TLSVersion.TLSv1
cli.maximum_version = ssl.TLSVersion.TLSv1_2
cli.set_ciphers("ALL:@SECLEVEL=0")
cli.check_hostname = False
cli.verify_mode = ssl.CERT_NONE

def pump(src, dst, direction):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            show(direction, data)
            dst.sendall(data)
    except Exception as e:
        log(f"{direction} ended: {e}")
    finally:
        for s in (src, dst):
            try: s.close()
            except Exception: pass

def handle(raw, addr):
    log(f"TCP connect from {addr}")
    try:
        down = srv.wrap_socket(raw, server_side=True)
    except ssl.SSLError as e:
        log(f"HANDSHAKE(purifier) FAILED {addr}: {e}")
        raw.close(); return
    log(f"HANDSHAKE(purifier) OK {addr}: {down.version()}/{down.cipher()[0]}")
    try:
        up_raw = socket.create_connection(UPSTREAM, timeout=10)
        up = cli.wrap_socket(up_raw, server_hostname="iocgwo.coway.co.kr")
        up.settimeout(None)   # blocking; cloud idles ~10s between keepalives
        down.settimeout(None)
        log(f"UPSTREAM OK {UPSTREAM}: {up.version()}/{up.cipher()[0]}")
    except Exception as e:
        log(f"UPSTREAM connect FAILED: {e} -- relaying to nowhere (device->us only)")
        up = None
    if up is None:
        # fall back to log-only
        try:
            while True:
                d = down.recv(4096)
                if not d: break
                show("P->X", d)
        except Exception as e:
            log(f"device-only ended: {e}")
        return
    t1 = threading.Thread(target=pump, args=(down, up, "P->C"), daemon=True)  # purifier->cloud
    t2 = threading.Thread(target=pump, args=(up, down, "C->P"), daemon=True)  # cloud->purifier
    t1.start(); t2.start(); t1.join(); t2.join()
    log(f"session {addr} closed")

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", LISTEN_PORT)); s.listen(5)
    log(f"MITM relay up on 0.0.0.0:{LISTEN_PORT} -> {UPSTREAM} (log {LOG})")
    while True:
        c, a = s.accept()
        threading.Thread(target=handle, args=(c, a), daemon=True).start()

if __name__ == "__main__":
    main()
