class AuthError(Exception):
    pass


class TokenExpired(AuthError):
    pass


class BadSignature(AuthError):
    pass
