"""离线测试用的 bcrypt 桩实现。

仅用于「本机缺少 bcrypt 时跑单元测试」，采用 PBKDF2 做可用的哈希校验，
保证 hash_password / verify_password 的行为（含"错误密码返回 False"）可被真实验证。
生产环境请务必安装真正的 bcrypt：pip install bcrypt
"""
import base64, hashlib, hmac, os

_PREFIX = "$2b$12$"

def gensalt(rounds: int = 12):
    return os.urandom(16)

def _derive(password: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, 1200, dklen=23)

def hashpw(password: bytes, salt: bytes) -> bytes:
    digest = base64.b64encode(_derive(password, salt)).decode()
    return (_PREFIX + base64.b64encode(salt).decode() + digest).encode()

def checkpw(password: bytes, hashed: bytes) -> bool:
    if not isinstance(hashed, bytes) or not hashed.startswith(_PREFIX.encode()):
        raise ValueError("invalid salt")
    body = hashed.decode()[len(_PREFIX):]
    salt = base64.b64decode(body[:24])
    expected = base64.b64encode(_derive(password, salt)).decode()
    return hmac.compare_digest(body[24:], expected)
