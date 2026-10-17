"""Check what a client would get: fetch the manifest, verify its signature against the
app's certificate, and re-hash every asset.

    python3 scripts/verify.py <server> <certificate.pem> <platform> <runtime> [channel] [current-update-id]

Needs `cryptography` (uv run --with cryptography python3 scripts/verify.py ...).
"""

import base64
import email.parser
import email.policy
import hashlib
import json
import re
import sys
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

server, cert_path, platform, runtime = sys.argv[1:5]
channel = sys.argv[5] if len(sys.argv) > 5 else "production"
current = sys.argv[6] if len(sys.argv) > 6 else None

# Cloudflare blocks the default Python-urllib user agent.
UA = {"User-Agent": "pokket-updates-verify/1"}
headers = {
    **UA,
    "expo-platform": platform,
    "expo-runtime-version": runtime,
    "expo-channel-name": channel,
    "expo-protocol-version": "1",
    "expo-expect-signature": 'sig, keyid="main", alg="rsa-v1_5-sha256"',
}
if current:
    headers["expo-current-update-id"] = current
with urllib.request.urlopen(urllib.request.Request(f"{server}/api/manifest", headers=headers)) as r:
    assert r.headers["expo-protocol-version"] == "1", r.headers
    raw = b"Content-Type: " + r.headers["content-type"].encode() + b"\r\n\r\n" + r.read()

public_key = x509.load_pem_x509_certificate(open(cert_path, "rb").read()).public_key()
message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
parts = {}
for part in message.iter_parts():
    name = part.get_param("name", header="content-disposition")
    body = part.get_payload(decode=True)
    signature = part.get("expo-signature")
    if name in ("manifest", "directive"):
        assert signature, f"{name} is not signed"
        sig = base64.b64decode(re.search(r'sig="([^"]+)"', signature).group(1))
        public_key.verify(sig, body, padding.PKCS1v15(), hashes.SHA256())  # raises if bad
        print(f"{name}: signature OK")
    parts[name] = json.loads(body)

if "directive" in parts:
    print("directive:", parts["directive"])
    sys.exit(0)

manifest = parts["manifest"]
print(f"manifest {manifest['id']} runtime={manifest['runtimeVersion']} "
      f"expoClient.version={manifest['extra']['expoClient'].get('version')}")
for a in [manifest["launchAsset"], *manifest["assets"]]:
    with urllib.request.urlopen(urllib.request.Request(a["url"].replace("https://updates.arkitekt.live", server), headers=UA)) as r:
        data = r.read()
        cache = r.headers["cache-control"]
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    assert digest == a["hash"], f"hash mismatch for {a['key']}"
    assert hashlib.md5(data).hexdigest() == a["key"]
    print(f"  {a['key']}{a['fileExtension']} {a['contentType']} {len(data)}B hash OK ({cache})")
print("all assets OK")
