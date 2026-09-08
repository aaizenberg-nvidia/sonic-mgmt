class RemoteAaaType:
    LDAP = 'ldap'
    RADIUS = 'radius'
    TACACS = 'tacacs'
    ALL_TYPES = [LDAP, RADIUS, TACACS]


class RemoteAaaConsts:
    WAIT_TIME_BEFORE_AUTH = 3
    # Answers of the OpenApi server to a request whose credentials the applied config just revoked
    AUTH_REVOKED_BY_APPLY_INDICATORS = ['HTTP 401', '401 Authorization Required']


class ValidValues:
    PRIORITY = [1, 2, 3, 4, 5, 6, 7, 8]
