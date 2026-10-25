"""Protocole RADIUS minimal (RFC 2865 / 2866 / 3579) pour ADER Roaming.

Seulement ce dont un hotspot MikroTik a besoin : Access-Request (PAP ou CHAP),
Accounting-Request (Start / Interim / Stop), réponses signées, Message-Authenticator,
et les attributs constructeur MikroTik (débit, limite de données).
"""
import hashlib
import hmac
import struct

ACCESS_REQUEST, ACCESS_ACCEPT, ACCESS_REJECT = 1, 2, 3
ACCOUNTING_REQUEST, ACCOUNTING_RESPONSE = 4, 5

USER_NAME, USER_PASSWORD, CHAP_PASSWORD, NAS_IP_ADDRESS = 1, 2, 3, 4
REPLY_MESSAGE, VENDOR_SPECIFIC, SESSION_TIMEOUT = 18, 26, 27
NAS_IDENTIFIER, ACCT_STATUS_TYPE, ACCT_INPUT_OCTETS, ACCT_OUTPUT_OCTETS = 32, 40, 42, 43
ACCT_SESSION_ID, ACCT_SESSION_TIME, ACCT_INPUT_GIGAWORDS, ACCT_OUTPUT_GIGAWORDS = 44, 46, 52, 53
CALLING_STATION_ID, CHAP_CHALLENGE, MESSAGE_AUTHENTICATOR, ACCT_INTERIM_INTERVAL = 31, 60, 80, 85
CLASS = 25

MIKROTIK = 14988
MT_RATE_LIMIT, MT_TOTAL_LIMIT, MT_TOTAL_LIMIT_GIGAWORDS = 8, 17, 18

ACCT_START, ACCT_STOP, ACCT_INTERIM = 1, 2, 3


class RadiusError(Exception):
    pass


class Packet:
    def __init__(self, code, ident, authenticator, attrs):
        self.code, self.ident, self.authenticator = code, ident, authenticator
        self.attrs = attrs  # liste de (type, bytes)

    @classmethod
    def parse(cls, data):
        if len(data) < 20:
            raise RadiusError("paquet trop court")
        code, ident, length = struct.unpack("!BBH", data[:4])
        if length < 20 or length > len(data) or length > 4096:
            raise RadiusError("longueur invalide")
        attrs, pos = [], 20
        while pos < length:
            if pos + 2 > length:
                raise RadiusError("attribut tronqué")
            t, ln = data[pos], data[pos + 1]
            if ln < 2 or pos + ln > length:
                raise RadiusError("attribut invalide")
            attrs.append((t, data[pos + 2:pos + ln]))
            pos += ln
        p = cls(code, ident, data[4:20], attrs)
        p.raw = data[:length]
        return p

    def get(self, t):
        return next((v for k, v in self.attrs if k == t), None)

    def text(self, t):
        v = self.get(t)
        return v.decode("utf-8", "replace") if v is not None else None

    def int(self, t, default=0):
        v = self.get(t)
        return struct.unpack("!I", v)[0] if v is not None and len(v) == 4 else default


def check_message_authenticator(p, secret):
    """Vrai si l'attribut est absent (ancien RouterOS) ou correct ; faux s'il est faux."""
    ma = p.get(MESSAGE_AUTHENTICATOR)
    if ma is None:
        return True
    raw = bytearray(p.raw)
    pos = 20
    while pos < len(raw):
        t, ln = raw[pos], raw[pos + 1]
        if t == MESSAGE_AUTHENTICATOR:
            raw[pos + 2:pos + ln] = b"\0" * 16
        pos += ln
    return hmac.compare_digest(hmac.new(secret, bytes(raw), hashlib.md5).digest(), ma)


def check_accounting(p, secret):
    """L'authentificateur d'un Accounting-Request est signé avec le secret (RFC 2866)."""
    raw = p.raw[:4] + b"\0" * 16 + p.raw[20:]
    return hmac.compare_digest(hashlib.md5(raw + secret).digest(), p.authenticator)


def pap_password(p, secret):
    enc = p.get(USER_PASSWORD)
    if enc is None or len(enc) % 16 or not enc:
        return None
    out, last = b"", p.authenticator
    for i in range(0, len(enc), 16):
        b = hashlib.md5(secret + last).digest()
        chunk = enc[i:i + 16]
        out += bytes(x ^ y for x, y in zip(chunk, b))
        last = chunk
    return out.rstrip(b"\0")


def chap_ok(p, password):
    cp = p.get(CHAP_PASSWORD)
    if cp is None or len(cp) != 17:
        return False
    challenge = p.get(CHAP_CHALLENGE) or p.authenticator
    return hmac.compare_digest(hashlib.md5(cp[:1] + password + challenge).digest(), cp[1:])


def password_ok(p, secret, password):
    """PAP ou CHAP, selon ce que le routeur envoie."""
    password = password.encode()
    if p.get(CHAP_PASSWORD) is not None:
        return chap_ok(p, password)
    got = pap_password(p, secret)
    return got is not None and hmac.compare_digest(got, password)


def attr(t, value):
    if isinstance(value, int):
        value = struct.pack("!I", value)
    elif isinstance(value, str):
        value = value.encode()
    if len(value) > 253:
        value = value[:253]
    return bytes([t, len(value) + 2]) + value


def vsa(vendor, t, value):
    if isinstance(value, int):
        value = struct.pack("!I", value)
    elif isinstance(value, str):
        value = value.encode()
    sub = bytes([t, len(value) + 2]) + value
    return attr(VENDOR_SPECIFIC, struct.pack("!I", vendor) + sub)


def reply(req, code, secret, attrs=b""):
    """Réponse signée, avec Message-Authenticator (exigé par les RouterOS récents)."""
    if code != ACCOUNTING_RESPONSE:
        attrs = attrs + bytes([MESSAGE_AUTHENTICATOR, 18]) + b"\0" * 16
    length = 20 + len(attrs)
    head = struct.pack("!BBH", code, req.ident, length)
    if code != ACCOUNTING_RESPONSE:
        ma = hmac.new(secret, head + req.authenticator + attrs, hashlib.md5).digest()
        attrs = attrs[:-16] + ma
    auth = hashlib.md5(head + req.authenticator + attrs + secret).digest()
    return head + auth + attrs


# --- côté client (tests, et outil de vérification) -----------------------------------------
def build_request(code, ident, secret, attrs, authenticator=None, message_auth=True):
    import os
    body = b"".join(attrs)
    if code == ACCOUNTING_REQUEST:
        head = struct.pack("!BBH", code, ident, 20 + len(body))
        auth = hashlib.md5(head + b"\0" * 16 + body + secret).digest()
        return head + auth + body
    auth = authenticator or os.urandom(16)
    if message_auth:
        body += bytes([MESSAGE_AUTHENTICATOR, 18]) + b"\0" * 16
    head = struct.pack("!BBH", code, ident, 20 + len(body))
    if message_auth:
        ma = hmac.new(secret, head + auth + body, hashlib.md5).digest()
        body = body[:-16] + ma
    return head + auth + body


def pap_encrypt(password, secret, authenticator):
    pw = password.encode()
    pw += b"\0" * (-len(pw) % 16 or (16 if not pw else 0))
    out, last = b"", authenticator
    for i in range(0, len(pw), 16):
        b = hashlib.md5(secret + last).digest()
        c = bytes(x ^ y for x, y in zip(pw[i:i + 16], b))
        out += c
        last = c
    return out
