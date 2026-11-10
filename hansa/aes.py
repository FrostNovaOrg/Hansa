"""AES encryption (FIPS-197), and CBC with PKCS7 padding for Bark.

Standard library only; keys never leave this process. State bytes and round
keys follow FIPS-197's column order. This module deliberately has no decryptor.
"""

SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")


def _xtime(value):
    return ((value << 1) ^ (0x11b if value & 0x80 else 0)) & 255


class AES:
    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("AES needs a 16, 24 or 32 byte key")
        rounds = len(key) // 4 + 6
        expanded = list(key)
        rcon = 1
        while len(expanded) < 16 * (rounds + 1):
            word = expanded[-4:]
            if len(expanded) % len(key) == 0:
                word = [SBOX[x] for x in word[1:] + word[:1]]
                word[0] ^= rcon
                rcon = _xtime(rcon)
            elif len(key) == 32 and len(expanded) % 32 == 16:
                word = [SBOX[x] for x in word]
            for value in word:
                expanded.append(expanded[-len(key)] ^ value)
        self._round_keys = [expanded[i:i + 16] for i in range(0, len(expanded), 16)]

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("AES needs a 16 byte block")
        state = [a ^ b for a, b in zip(block, self._round_keys[0])]
        for number, round_key in enumerate(self._round_keys[1:], 1):
            state = [SBOX[state[((col + row) % 4) * 4 + row]] for col in range(4) for row in range(4)]
            if number < len(self._round_keys) - 1:
                for col in range(0, 16, 4):
                    a, b, c, d = state[col:col + 4]
                    total = a ^ b ^ c ^ d
                    state[col:col + 4] = [a ^ total ^ _xtime(a ^ b), b ^ total ^ _xtime(b ^ c),
                                         c ^ total ^ _xtime(c ^ d), d ^ total ^ _xtime(d ^ a)]
            state = [a ^ b for a, b in zip(state, round_key)]
        return bytes(state)


def cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    if len(iv) != 16:
        raise ValueError("AES-CBC needs a 16 byte IV")
    cipher = AES(key)
    padding = 16 - len(plaintext) % 16
    padded = plaintext + bytes([padding]) * padding
    out = bytearray()
    previous = iv
    for offset in range(0, len(padded), 16):
        previous = cipher.encrypt_block(bytes(a ^ b for a, b in zip(padded[offset:offset + 16], previous)))
        out.extend(previous)
    return bytes(out)
