SSH_CERT_AUTH_KEYS_PATH = "/auto/sw_system_project/NVOS_INFRA/security/verification/ssh_cert_auth/"

CERT_VALIDITY_PERIODS = {
    'minute': '+1m',         # 1 minute
    'hour': '+1h',           # 1 hour
    'day': '+1d',            # 1 day
    'week': '+7d',           # 7 days
    'month': '+30d',         # 30 days
    'forever': '-1m:forever'  # forever
}

TEST_PRINCIPALS = [
    'admin',
    'user',
    'guest',
    'monitor',
    'test-user',
    'sasha'
    'guy',
    'incognito',
    'anonymous',
    'basic',
    'kind-of-long-principal',
    'even-longer-principal-even-longer-principal-even-longer-principal-even-longer-principal',
]

BAD_PRINCIPAL = 'bad-principal'
CA_KEY_TYPE_MISMATCH_ERRS = ['does not match key data', 'Invalid config']
