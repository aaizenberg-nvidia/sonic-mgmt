class SshHardeningConsts:
    TIMEOUT = 4

    PROTOCOL = 'protocol'
    COMPRESSION = 'compression'
    CIPHERS = 'ciphers'
    MACS = 'MACs'
    KEX_ALGOS = 'kex-algorithms'
    AUTH_KEY_TYPES = 'auth-key-types'
    PUBLIC_KEY_LENGTH_DICT = {
        'ecdsa-sha2-nistp521': 521,
        'ecdsa-sha2-nistp384': 384,
        'ecdsa-sha2-nistp256': 256,
        'ssh-ed25519': 1024,
        'ssh-rsa': 4096
    }

    OPTIONS_FOR_FUNCTIONAL_TEST = [CIPHERS, MACS, KEX_ALGOS]

    # Signaling markers that OpenSSH always includes in the KEXINIT kex_algorithms field
    # (strict-KEX / RFC 8308 ext-info). They are not negotiable algorithms and cannot be
    # removed via KexAlgorithms, so they must be ignored when verifying ssh hardening.
    KEX_SIGNALING_MARKERS = ['kex-strict-s-v00@openssh.com',
                             'kex-strict-c-v00@openssh.com',
                             'ext-info-s',
                             'ext-info-c'
                             ]

    VALUES = {
        PROTOCOL: '2.0',
        COMPRESSION: 'none',
        CIPHERS: ['aes256-ctr', 'aes192-ctr', 'aes128-ctr', 'aes128-gcm@openssh.com', 'aes256-gcm@openssh.com'],
        MACS: ['hmac-sha2-256', 'hmac-sha2-512', 'hmac-sha2-512-etm@openssh.com', 'hmac-sha2-256-etm@openssh.com'],
        AUTH_KEY_TYPES: ['rsa-sha2-512', 'rsa-sha2-256']  # todo: can generate only these 2
        # AUTH_KEY_TYPES: ['ecdsa-sha2-nistp256-cert-v01@openssh.com',
        # 'ecdsa-sha2-nistp384-cert-v01@openssh.com',
        # 'ecdsa-sha2-nistp521-cert-v01@openssh.com', 'rsa-sha2-512-cert-v01@openssh.com',
        # 'rsa-sha2-256-cert-v01@openssh.com', 'rsa-sha2-512', 'rsa-sha2-256']
    }

    DEFAULTS = {
        CIPHERS: ['3des-cbc',
                  'aes128-cbc',
                  'aes192-cbc',
                  'aes256-cbc',
                  'aes128-ctr',
                  'aes192-ctr',
                  'aes256-ctr',
                  'aes128-gcm@openssh.com',
                  'aes256-gcm@openssh.com',
                  'chacha20-poly1305@openssh.com'
                  ],
        MACS: ['hmac-sha1',
               'hmac-sha1-96',
               'hmac-sha2-256',
               'hmac-sha2-512',
               'hmac-md5',
               'hmac-md5-96',
               'umac-64@openssh.com',
               'umac-128@openssh.com',
               'hmac-sha1-etm@openssh.com',
               'hmac-sha1-96-etm@openssh.com',
               'hmac-sha2-256-etm@openssh.com',
               'hmac-sha2-512-etm@openssh.com',
               'hmac-md5-etm@openssh.com',
               'hmac-md5-96-etm@openssh.com',
               'umac-64-etm@openssh.com',
               'umac-128-etm@openssh.com'
               ],
        KEX_ALGOS: ['diffie-hellman-group1-sha1',
                    'diffie-hellman-group14-sha1',
                    'diffie-hellman-group14-sha256',
                    'diffie-hellman-group16-sha512',
                    'diffie-hellman-group18-sha512',
                    'diffie-hellman-group-exchange-sha1',
                    'diffie-hellman-group-exchange-sha256',
                    'ecdh-sha2-nistp256',
                    'ecdh-sha2-nistp384',
                    'ecdh-sha2-nistp521',
                    'curve25519-sha256',
                    'curve25519-sha256@libssh.org',
                    'sntrup761x25519-sha512@openssh.com'
                    ]
    }

    SSH_CMD_FLAGS = {
        CIPHERS: '-c ',
        MACS: '-m ',
        KEX_ALGOS: '-o KexAlgorithms='
    }

    ERROR_PATTERN_PREFIX = 'Unable to negotiate with.*'
    ERROR_PATTERNS = {
        CIPHERS: rf'{ERROR_PATTERN_PREFIX} no matching cipher found.',
        MACS: rf'{ERROR_PATTERN_PREFIX} no matching MAC found.',
        KEX_ALGOS: rf'{ERROR_PATTERN_PREFIX} no matching key exchange method found.'
    }

    INVALID_VALUES = {
        AUTH_KEY_TYPES: ['ssh-dss']  # ['ssh-rsa', 'ssh-dss']  # todo: ssh-rsa (works unexpectedly)
    }
    SHARED_AUTH_KEYS_PATH = '/auto/sw_system_project/NVOS_INFRA/security/verification/ssh_hardening/auth_keys'
    INVALID_AUTH_KEY_PATH = f'{SHARED_AUTH_KEYS_PATH}/invalid_key'
    VALID_AUTH_KEY_PATH = f'{SHARED_AUTH_KEYS_PATH}/valid_key'
