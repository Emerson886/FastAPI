class ExpiredSignatureError(Exception): pass
class InvalidTokenError(Exception): pass
def encode(payload, key, algorithm="HS256"):
    return "stub." + str(payload)
def decode(token, key, algorithms=None):
    raise InvalidTokenError("stub")
