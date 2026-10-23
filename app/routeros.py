"""Client minimal pour l'API MikroTik RouterOS (ports 8728 / 8729 en SSL).

Aucune dépendance externe : uniquement la bibliothèque standard Python.
Compatible RouterOS 6.43+ et 7.x (connexion en clair) ainsi que les anciennes
versions (connexion par défi MD5).
"""
import hashlib
import socket
import ssl


class RouterOSError(Exception):
    """Erreur renvoyée par le routeur ou problème de connexion."""


def _encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    return b"\xF0" + n.to_bytes(4, "big")


class RouterOS:
    def __init__(self, host, username, password, port=8728, use_ssl=False, timeout=8):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.sock = None

    # --- connexion -------------------------------------------------------
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise RouterOSError(f"Routeur injoignable ({self.host}:{self.port}) : {e}") from e
        if self.use_ssl:
            ctx = ssl.create_default_context()
            # Les routeurs MikroTik utilisent presque toujours un certificat auto-signé.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(self.timeout)
        self.sock = raw
        self._login()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _login(self):
        replies = self._talk(["/login", f"=name={self.username}", f"=password={self.password}"])
        done = replies[-1][1]
        if "ret" in done:  # ancien RouterOS (< 6.43) : défi MD5
            challenge = bytes.fromhex(done["ret"])
            digest = hashlib.md5(b"\x00" + self.password.encode() + challenge).hexdigest()
            self._talk(["/login", f"=name={self.username}", f"=response=00{digest}"])

    # --- protocole -------------------------------------------------------
    def _send_sentence(self, words):
        data = b""
        for w in words:
            b = w.encode("utf-8")
            data += _encode_length(len(b)) + b
        data += b"\x00"
        self.sock.sendall(data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RouterOSError("Connexion fermée par le routeur")
            buf += chunk
        return buf

    def _read_length(self):
        c = self._recv_exact(1)[0]
        if c < 0x80:
            return c
        if c < 0xC0:
            return ((c & 0x3F) << 8) | self._recv_exact(1)[0]
        if c < 0xE0:
            return ((c & 0x1F) << 16) | int.from_bytes(self._recv_exact(2), "big")
        if c < 0xF0:
            return ((c & 0x0F) << 24) | int.from_bytes(self._recv_exact(3), "big")
        return int.from_bytes(self._recv_exact(4), "big")

    def _read_sentence(self):
        words = []
        while True:
            n = self._read_length()
            if n == 0:
                return words
            words.append(self._recv_exact(n).decode("utf-8", errors="replace"))

    def _talk(self, words):
        try:
            self._send_sentence(words)
            replies = []
            while True:
                sentence = self._read_sentence()
                if not sentence:
                    continue
                kind, attrs = sentence[0], {}
                for w in sentence[1:]:
                    if w.startswith("="):
                        key, _, val = w[1:].partition("=")
                        attrs[key] = val
                replies.append((kind, attrs))
                if kind == "!trap":
                    raise RouterOSError(attrs.get("message", "Erreur RouterOS"))
                if kind == "!fatal":
                    raise RouterOSError("Erreur fatale RouterOS")
                if kind == "!done":
                    return replies
        except (OSError, ssl.SSLError) as e:
            raise RouterOSError(f"Échange interrompu avec {self.host} : {e}") from e

    # --- API publique ------------------------------------------------------
    def query(self, command, *args):
        """Exécute une commande et renvoie la liste des enregistrements (!re)."""
        return [attrs for kind, attrs in self._talk([command, *args]) if kind == "!re"]

    def pipeline(self, commands, window=100):
        """Envoie beaucoup de commandes d'un coup, sans attendre chaque réponse (étiquettes .tag).

        Sur une liaison lente (tunnel, autre continent), 1000 ajouts prennent ainsi quelques
        secondes au lieu de plusieurs minutes. Renvoie, pour chaque commande, None (réussie)
        ou le message d'erreur du routeur.
        """
        results = [None] * len(commands)
        done = set()
        sent = 0
        try:
            while len(done) < len(commands):
                while sent < len(commands) and sent - len(done) < window:
                    self._send_sentence(list(commands[sent]) + [f".tag={sent}"])
                    sent += 1
                sentence = self._read_sentence()
                if not sentence:
                    continue
                kind = sentence[0]
                tag = next((w[5:] for w in sentence[1:] if w.startswith(".tag=")), None)
                if kind == "!fatal":
                    raise RouterOSError("Erreur fatale RouterOS")
                if tag is None or not tag.isdigit():
                    continue
                i = int(tag)
                if kind == "!trap":
                    msg = next((w[len("=message="):] for w in sentence[1:] if w.startswith("=message=")), "Erreur RouterOS")
                    results[i] = msg
                elif kind == "!done":
                    done.add(i)
        except (OSError, ssl.SSLError) as e:
            raise RouterOSError(f"Échange interrompu avec {self.host} : {e}") from e
        return results

    def count(self, command):
        """Nombre d'éléments renvoyé par une commande print avec count-only."""
        replies = self._talk([command, "=count-only="])
        return int(replies[-1][1].get("ret", 0))
