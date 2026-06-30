"""OWNER-ONLY license key generator. NEVER ship this or keys/ to customers.

Signs license keys with the private key in keys/fragroute_ed25519_private.pem.
The app verifies them with the public key embedded in fragroute_license.py.

Examples:
  py -3 mint_license.py --tier pro  --name "Jane Buyer" --email jane@x.com
  py -3 mint_license.py --tier pro  --name "Sub User"  --days 30        # subscription
  py -3 mint_license.py --tier admin --name "OWNER"                      # your unlock
  py -3 mint_license.py --verify FRG1.xxx.yyy                            # check a key
"""
import argparse
import base64
import json
import time
import uuid
from pathlib import Path

KEY_PREFIX = "FRG1"
PRIV_PATH = Path(__file__).parent / "keys" / "fragroute_ed25519_private.pem"


def _b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _load_priv():
    from cryptography.hazmat.primitives import serialization
    if not PRIV_PATH.exists():
        raise SystemExit("[X] private key missing: %s\n    (run the keypair generator first)" % PRIV_PATH)
    return serialization.load_pem_private_key(PRIV_PATH.read_bytes(), password=None)


def mint(tier, name, email="", days=0, seats=1):
    priv = _load_priv()
    now = int(time.time())
    payload = {"t": tier, "n": name, "c": now, "i": uuid.uuid4().hex[:12], "s": int(seats)}
    if email:
        payload["e"] = email
    if days:
        payload["x"] = now + int(days) * 86400
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = priv.sign(body)
    return "%s.%s.%s" % (KEY_PREFIX, _b64u(body), _b64u(sig)), payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["free", "pro", "admin"], default="pro")
    ap.add_argument("--name", default="Customer")
    ap.add_argument("--email", default="")
    ap.add_argument("--days", type=int, default=0, help="expiry in days (0 = perpetual)")
    ap.add_argument("--seats", type=int, default=1)
    ap.add_argument("--verify", help="verify an existing key instead of minting")
    a = ap.parse_args()

    if a.verify:
        import fragroute_license as L
        print(json.dumps(L.verify_key(a.verify), indent=2))
        return

    key, payload = mint(a.tier, a.name, a.email, a.days, a.seats)
    exp = payload.get("x")
    print("=" * 64)
    print(" TIER : %s" % a.tier)
    print(" NAME : %s" % a.name)
    print(" ID   : %s" % payload["i"])
    print(" EXP  : %s" % (time.strftime("%Y-%m-%d", time.localtime(exp)) if exp else "perpetual"))
    print(" SEATS: %s" % a.seats)
    print("=" * 64)
    print(key)
    print("=" * 64)
    print("Give this key to the customer; they paste it in Account > License.")


if __name__ == "__main__":
    main()
