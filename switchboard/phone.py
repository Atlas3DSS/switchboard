"""The one phone allowed in through the gate (switchboard.gate): pairing tokens, the paired device's public key,
login challenges and sessions. State is one mode-0600 JSON file under the hub's state dir, shared by the hub
(makes pairing tokens, unpairs) and the gate (pairs, logs in, checks sessions).

Pairing: the owner asks the local hub for a token and scans it as a QR code. The phone's browser makes a
non-extractable ECDSA P-256 key, sends the public half with the token, and keeps the private half in its own
storage. Pairing again replaces the device and ends its sessions, so only one phone works at a time. Logging in
signs a fresh server challenge with that key. Only hashes of tokens and session ids are stored.
"""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager

from .store import STATE, iso_from_epoch

PATH = os.path.join(STATE, "phone.json")
PAIR_TTL_S = 600
CHALLENGE_TTL_S = 60
SESSION_TTL_S = 24 * 3600
LOGIN_CONTEXT = "switchboard-phone-login"


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


@contextmanager
def _state():
    """Read-modify-write the state file under an exclusive lock (hub and gate are separate processes)."""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            st = json.load(open(PATH))
        except (OSError, ValueError):
            st = {}
        st.setdefault("origin", "")
        st.setdefault("device", None)
        st.setdefault("pairing", None)
        st.setdefault("challenges", {})
        st.setdefault("sessions", {})
        now = time.time()
        st["challenges"] = {k: v for k, v in st["challenges"].items() if v > now}
        st["sessions"] = {k: v for k, v in st["sessions"].items() if v["expires"] > now}
        before = json.dumps(st, sort_keys=True)
        yield st
        if json.dumps(st, sort_keys=True) != before:
            tmp = PATH + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(st, f)
            os.replace(tmp, PATH)


def origin():
    """https://<hostname> the phone opens (set once during setup; never in the code)."""
    with _state() as st:
        return os.environ.get("SB_GATE_ORIGIN") or st["origin"]


def set_origin(url):
    import re
    url = (url or "").strip().rstrip("/")
    if not re.fullmatch(r"https://[\w.-]+(:\d+)?|http://(127\.0\.0\.1|localhost)(:\d+)?", url):
        raise ValueError("the phone address must look like https://sb.example.com")
    with _state() as st:
        st["origin"] = url
    return url


def status():
    with _state() as st:
        d, p = st["device"], st["pairing"]
        return {"origin": os.environ.get("SB_GATE_ORIGIN") or st["origin"], "paired": bool(d), "paired_at": iso_from_epoch(d["paired_at"]) if d else None,
                "browser": d.get("ua") if d else None, "sessions": len(st["sessions"]),
                "pairing_open": bool(p and p["expires"] > time.time()),
                "pairing_expires": iso_from_epoch(p["expires"]) if p and p["expires"] > time.time() else None}


def new_pairing():
    """A single-use token for the QR code; any earlier unused token stops working."""
    token = secrets.token_urlsafe(32)
    with _state() as st:
        st["pairing"] = {"hash": _h(token), "expires": time.time() + PAIR_TTL_S}
    return token, PAIR_TTL_S


def pair(token, spki_b64, ua=""):
    """Bind the phone whose public key this is. Replaces any paired phone and ends every session."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    key = load_der_public_key(base64.b64decode(spki_b64))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("expected an ECDSA P-256 public key")
    with _state() as st:
        p = st["pairing"]
        if not p or p["expires"] < time.time() or not secrets.compare_digest(p["hash"], _h(token or "")):
            raise PermissionError("this pairing code is not valid any more; make a new one in Switchboard on your computer")
        st["pairing"] = None
        st["device"] = {"id": secrets.token_hex(8), "spki": spki_b64, "paired_at": time.time(), "ua": ua[:200]}
        st["sessions"] = {}
        return st["device"]["id"]


def unpair():
    with _state() as st:
        st["device"], st["pairing"], st["sessions"], st["challenges"] = None, None, {}, {}


def challenge():
    nonce = secrets.token_urlsafe(32)
    with _state() as st:
        if not st["device"]:
            raise PermissionError("no phone is paired")
        st["challenges"][_h(nonce)] = time.time() + CHALLENGE_TTL_S
    return nonce


def login_message(nonce, origin):
    return f"{LOGIN_CONTEXT}\n{nonce}\n{origin}".encode()


def login(nonce, sig_b64, origin):
    """Check the paired phone signed this challenge (WebCrypto's raw r||s form); returns a new session id."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    with _state() as st:
        d = st["device"]
        if not d or st["challenges"].pop(_h(nonce or ""), None) is None:  # single use, even when it fails
            raise PermissionError("challenge expired; try again")
        raw = base64.b64decode(sig_b64 or "")
        if len(raw) != 64:
            raise PermissionError("bad signature")
        try:
            load_der_public_key(base64.b64decode(d["spki"])).verify(
                encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                login_message(nonce, origin), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise PermissionError("this browser is not the paired phone")
        sid = secrets.token_urlsafe(32)
        st["sessions"][_h(sid)] = {"device": d["id"], "expires": time.time() + SESSION_TTL_S}
        return sid, SESSION_TTL_S


def session_ok(sid):
    if not sid:
        return False
    with _state() as st:
        s = st["sessions"].get(_h(sid))
        return bool(s and st["device"] and s["device"] == st["device"]["id"])
