"""Authentication of a single RPMB read-counter response; no device access."""
import hashlib
import hmac


def verify_counter_response(frame, nonce, key):
    """Return the authenticated counter; never treat an absent key as success."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise ValueError("RPMB: нужен прежний ключ длиной 32 байта")
    if len(frame) != 512 or len(nonce) != 16:
        raise ValueError("RPMB: некорректная длина ответа или nonce")
    if int.from_bytes(frame[510:512], "big") != 0x0200:
        raise RuntimeError("RPMB: неожиданный тип authenticated-ответа")
    if int.from_bytes(frame[508:510], "big") != 0:
        raise RuntimeError("RPMB: счётчик с прежним ключом не подтверждён")
    if not hmac.compare_digest(frame[484:500], nonce):
        raise RuntimeError("RPMB: nonce authenticated-ответа не совпал")
    # JEDEC authenticated region: data through request/response, 284 bytes.
    digest = hmac.new(key, frame[228:512], hashlib.sha256).digest()
    if not hmac.compare_digest(frame[196:228], digest):
        raise RuntimeError("RPMB: HMAC с прежним ключом не совпал")
    return int.from_bytes(frame[500:504], "big")
