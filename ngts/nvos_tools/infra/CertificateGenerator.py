import logging
import os
import random
import string
import subprocess
import tempfile
from typing import Optional, Union, List

from ngts.nvos_tools.infra.OpenSslCmdBuilder import OpenSslCmdBuilder

YEAR = 365
DEFAULT_DN = 'NVOS'
CA_CN = 'NVOS-CA'


class CertificateGenerator:

    def generate_cert(self, cert_location: str, cert_name: str, cert_subj_cn: str = '', ip: str = '', dn: str = '',
                      new_ca_path: str = '', new_ca_name: str = '', p12_pass: str = '', existing_ca_public: str = '',
                      existing_ca_private: str = '', expiration_years: int = 10, san_uris: List[str] = [],
                      stdout_func=logging.info, expiration_days: Optional[int] = None):
        """
        create new x509 ca/cert with given properties

        :param expiration_days: leaf validity in days (overrides expiration_years; CA stays long-lived).

        manual steps:
        -------------
        CA_FILENAME="ca"
        CA_CN="NVOS-CA"
        CERT_DN="juliet-126"
        CERT_IP="10.7.148.126"
        CERT_FILENAME="cert"
        P12_PASS="mypass"
        EXP=3650
        SPIFFE="spiffe://myspiffe.org/example"

        openssl genrsa -out $CA_FILENAME.key 2048
        openssl req -new -x509 -days $EXP -key $CA_FILENAME.key -subj /C=CN/ST=GD/L=SZ-Inc/CN=$CA_CN -out $CA_FILENAME.crt
        openssl req -newkey rsa:2048 -nodes -keyout $CERT_FILENAME.key -subj /C=CN/ST=GD/L=SZ-Inc/CN=$CERT_DN -out $CERT_FILENAME.csr
        openssl x509 -req -in $CERT_FILENAME.csr -CA $CA_FILENAME.crt -CAkey $CA_FILENAME.key -CAcreateserial -out $CERT_FILENAME.crt -days $EXP -extfile <(printf "subjectAltName=DNS:$CERT_DN,IP:$CERT_IP,URI:$SPIFFE")
        openssl x509 -in $CERT_FILENAME.crt -out $CERT_FILENAME.pem -outform PEM
        openssl pkcs12 -export -out $CERT_FILENAME.p12 -in $CERT_FILENAME.pem -inkey $CERT_FILENAME.key -keypbe AES-256-CBC -certpbe AES-256-CBC -passout pass:$P12_PASS

        chmod +r ./*
        -------------
        """

        stdout_func('validate parameters')
        self._validate_cert_location(cert_location)
        target_device_props = [ip, dn]
        assert len(
            [prop for prop in target_device_props if prop]) > 0, "none of [ip, dn] given. at least one must be given"
        assert cert_name, "empty string as cert name not allowed"
        assert expiration_years > 0, f"given certificate expiration not allowed: {expiration_years}. must give positive number"

        existing_ca_props = [existing_ca_public, existing_ca_private]
        existing_ca_props = [prop for prop in existing_ca_props if prop]
        new_ca_props = [new_ca_path, new_ca_name]
        new_ca_props = [prop for prop in new_ca_props if prop]
        assert (new_ca_props and not existing_ca_props) or (existing_ca_props and not new_ca_props), \
            f"must give info for new OR existing CA (but not both)"

        cert_expiration = expiration_days if expiration_days is not None else expiration_years * YEAR
        assert cert_expiration > 0, \
            f"given certificate expiration (days) not allowed: {cert_expiration}. must give positive number"
        ca_expiration = max(expiration_years * YEAR, cert_expiration)

        ######################

        # Prepare CA
        ca_private_path, ca_public_path = self._prepare_ca(existing_ca_private, existing_ca_public, ca_expiration,
                                                           new_ca_name, new_ca_path, new_ca_props, stdout_func)
        stdout_func('verify CA with itself')
        self._openssl_verify_cert_with_ca(ca_public_path, ca_public_path, stdout_func)

        # Generate and sign cert using CA
        cert_csr_path, cert_private_path = self._gen_cert_csr_and_private_key(cert_location, cert_name, cert_subj_cn, stdout_func)
        cert_crt_public_path = self._issue_and_sign_public_cert(ca_private_path, ca_public_path, cert_csr_path,
                                                                cert_location, cert_name, dn, cert_expiration, ip, san_uris,
                                                                stdout_func)
        stdout_func('verify generated cert crt with CA')
        self._openssl_verify_cert_with_ca(ca_public_path, cert_crt_public_path, stdout_func)

        # Convert public cert to PEM format
        cert_public_pem_path = self._convert_crt_to_pem(cert_crt_public_path, cert_location, cert_name, stdout_func)
        stdout_func('verify generated cert pem with CA')
        self._openssl_verify_cert_with_ca(ca_public_path, cert_public_pem_path, stdout_func)

        # Create p12 bundle of the cert
        self._create_cert_p12_bundle(cert_location, cert_name, cert_private_path, cert_public_pem_path, p12_pass,
                                     stdout_func)

    def generate_cert_chain(self, cert_location: str, cert_name: str, cert_subj_cn: str = '', ip: str = '', dn: str = '',
                            new_ca_path: str = '', new_ca_name: str = '', p12_pass: str = '', existing_ca_public: str = '',
                            existing_ca_private: str = '', expiration_years: int = 10, san_uris: List[str] = [], stdout_func=logging.info):
        """
        create new x509 root CA with intermidiate CA and cert with given properties

        manual steps:
        -------------
        ------------
        INTERCA_FILENAME="interCa"
        CA_FILENAME="rootCa"
        CA_CN="NVOS-CA"
        CERT_DN="$2"
        CERT_IP="$1"
        CERT_FILENAME="leafCert"
        P12_PASS=$3
        EXP=3650
        SPIFFE="spiffe://myspiffe.org/example"

        Root CA
        openssl genrsa -out $CA_FILENAME.key 2048
        openssl req -new -x509 -days $EXP -key $CA_FILENAME.key -subj /C=CN/ST=GD/L=SZ-Inc/CN=$CA_CN -out $CA_FILENAME.crt

        Intermediate CA
        openssl req -newkey rsa:2048 -nodes -keyout $INTERCA_FILENAME.key -subj /C=CN/ST=GD/L=SZ-Inc/CN=interCA -out $INTERCA_FILENAME.csr
        openssl x509 -req -in $INTERCA_FILENAME.csr -CA $CA_FILENAME.crt -CAkey $CA_FILENAME.key -CAcreateserial -out $INTERCA_FILENAME.crt -days $EXP -extfile <(printf "[ server ]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid,issuer:always") -extensions server

        Leaf Cert
        openssl req -newkey rsa:2048 -nodes -keyout $CERT_FILENAME.key -subj /C=CN/ST=GD/L=SZ-Inc/CN=leafCert -out $CERT_FILENAME.csr
        openssl x509 -req -in $CERT_FILENAME.csr -CA $INTERCA_FILENAME.crt -CAkey $INTERCA_FILENAME.key -CAcreateserial -out $CERT_FILENAME.crt -days $EXP -extfile <(printf "subjectAltName=DNS:$CERT_DN,IP:$CERT_IP,URI:$SPIFFE")

        openssl x509 -in $CERT_FILENAME.crt -out $CERT_FILENAME.pem -outform PEM
        openssl pkcs12 -export -out $CERT_FILENAME.p12 -in $CERT_FILENAME.pem -inkey $CERT_FILENAME.key -passout pass:$P12_PASS

        Create chain
        cat $CERT_FILENAME.crt $INTERCA_FILENAME.crt > chain.pem
        openssl pkcs12 -export -out chain.p12 -in chain.pem -inkey $CERT_FILENAME.key -passout pass:$P12_PASS

        chmod +r ./*
        """

        stdout_func('validate parameters')
        self._validate_cert_location(cert_location)
        target_device_props = [ip, dn]
        assert len(
            [prop for prop in target_device_props if prop]) > 0, "none of [ip, dn] given. at least one must be given"
        assert cert_name, "empty string as cert name not allowed"
        assert expiration_years > 0, f"given certificate expiration not allowed: {expiration_years}. must give positive number"

        existing_ca_props = [existing_ca_public, existing_ca_private]
        existing_ca_props = [prop for prop in existing_ca_props if prop]
        new_ca_props = [new_ca_path, new_ca_name]
        new_ca_props = [prop for prop in new_ca_props if prop]
        assert (new_ca_props and not existing_ca_props) or (existing_ca_props and not new_ca_props), \
            f"must give info for new OR existing CA (but not both)"

        expiration = expiration_years * YEAR

        ######################

        # Prepare rCA
        rCA_private_path, rCA_public_path = self._prepare_ca(existing_ca_private, existing_ca_public, expiration,
                                                             new_ca_name, new_ca_path, new_ca_props, stdout_func)
        stdout_func('verify CA with itself')
        self._openssl_verify_cert_with_ca(rCA_public_path, rCA_public_path, stdout_func)

        # Generate iCA and sign cert using rCA

        iCA_name = 'interCA'
        iCA_csr_path, iCA_private_path = self._gen_cert_csr_and_private_key(cert_location, iCA_name, iCA_name, stdout_func)
        iCA_public_path = self._issue_and_sign_iCA(rCA_private_path, rCA_public_path, iCA_csr_path, cert_location, iCA_name, expiration, stdout_func)

        cert_csr_path, cert_private_path = self._gen_cert_csr_and_private_key(cert_location, cert_name, cert_subj_cn, stdout_func)
        cert_crt_public_path = self._issue_and_sign_public_cert(iCA_private_path, iCA_public_path, cert_csr_path,
                                                                cert_location, cert_name, dn, expiration, ip, san_uris,
                                                                stdout_func)
        stdout_func('verify generated cert crt with CA')
        # self._openssl_verify_cert_with_ca(iCA_public_path, cert_crt_public_path, stdout_func)

        # Combine public cert and iCA cert into chain
        chain_public_output = self._run(f'cat {cert_crt_public_path} {iCA_public_path}', stdout_func)
        chain_name = 'chain'
        chain_public_path = os.path.join(cert_location, f'{chain_name}.pem')

        if not os.path.exists(chain_public_path):
            with open(chain_public_path, 'w') as chain_file:
                chain_file.write(chain_public_output)
                chain_file.flush()

        # Create p12 bundle of the cert chain
        self._create_cert_p12_bundle(cert_location, chain_name, cert_private_path, chain_public_path, p12_pass, stdout_func)

    def generate_ca(self, new_ca_dir, new_ca_name, expiration, stdout_func=logging.info):
        self._validate_new_ca_location(new_ca_dir)
        assert new_ca_name, "empty string as CA name not allowed"

        stdout_func('generate new CA')
        ca_private_filename = f'{new_ca_name}.key'
        ca_private_path = os.path.join(new_ca_dir, ca_private_filename)
        ca_public_filename = f'{new_ca_name}.crt'
        ca_public_path = os.path.join(new_ca_dir, ca_public_filename)
        gen_new_ca_private_key_cmd_builder = OpenSslCmdBuilder()\
            .subcommand('genrsa')\
            .out(ca_private_path)\
            .positional_arg(2048)
        gen_new_ca_private_key_cmd = gen_new_ca_private_key_cmd_builder.get_command_string()
        self._run(gen_new_ca_private_key_cmd, stdout_func)
        self._chmod_for_reading(ca_private_path, stdout_func)
        gen_new_ca_public_key_builder = OpenSslCmdBuilder()\
            .subcommand('req')\
            .new()\
            .x509()\
            .days(expiration)\
            .key(ca_private_path)\
            .subject(C='CN', ST='GD', L='SZ-Inc', CN=CA_CN)\
            .out(ca_public_path)
        gen_new_ca_public_key = gen_new_ca_public_key_builder.get_command_string()
        self._run(gen_new_ca_public_key, stdout_func)
        self._chmod_for_reading(ca_private_path, stdout_func)

        stdout_func('verify CA with itself')
        self._openssl_verify_cert_with_ca(ca_public_path, ca_public_path, stdout_func)

        return ca_private_path, ca_public_path

    def revoke_cert(self, dest_dir: str, crl_name: str, cert_name: str, stdout_func=logging.info, use_new_crl: bool = False, ca_dest: str = "", create_empty: bool = False, ca_name: str = "ca", revoke_cert_name: str = "cert.crt") -> str:
        """
        @param cert_dir: path to the directory containing the certificate to be revoked
        @param crl_name: name of the CRL file
        @param ca_key_path: path to the CA private key
        @param ca_path: path to the CA public key
        @param cert_path: path to the certificate to be revoked
        @param stdout_func: function to print output
        @param use_new_crl: whether to use a new CRL or not
        @param ca_name: name of the CA file (without extension)
        @param revoke_cert_name: name of the certificate file to revoke (with extension)

        This function revokes a certificate and generates a CRL file, if the certificate was revoked before, it will append the new certificate to the CRL file
        openssl ca -revoke {cert_path} -keyfile {ca_key_path} -cert {ca_path}
        openssl ca -gencrl -keyfile {ca_key_path} -cert {ca_path} -out {crl_path}

        @return: path to the revoked certificate list
        """
        stdout_func('Validate cert exists')
        cert_dir = os.path.join(dest_dir, cert_name)
        cert_path = os.path.join(cert_dir, f'{revoke_cert_name}')
        if not ca_dest:
            ca_dest = cert_dir
        ca_key_path = os.path.join(ca_dest, f'{ca_name}.key')
        ca_path = os.path.join(ca_dest, f'{ca_name}.crt')
        self._validate_cert_location(cert_path)
        self._validate_cert_location(ca_key_path)
        self._validate_cert_location(ca_path)

        demo_dir = os.path.join(dest_dir, 'demoCA')
        os.makedirs(demo_dir, exist_ok=True)

        stdout_func('Create index and crlnumber for certificate to be revoked')
        crl_db_path = os.path.join(demo_dir, 'index.txt')
        if not os.path.exists(crl_db_path) or use_new_crl:
            with open(crl_db_path, 'w') as f:
                f.write('')

        crlnumber_path = os.path.join(demo_dir, 'crlnumber')
        if not os.path.exists(crlnumber_path):
            random_crl_number = ''.join([random.choice(string.digits) for _ in range(10)])
            with open(crlnumber_path, 'w') as f:
                f.write(f'{random_crl_number}\n')

        config_path = self._prepare_tmp_config(demo_dir)

        stdout_func(f'Revoke certificate {crl_name}')
        # This updates index.txt with the serial number of the certificate to be revoked
        if not create_empty:
            revoke_cert_cmd_builder = OpenSslCmdBuilder()\
                .subcommand('ca')\
                .revoke(cert_path)\
                .keyfile(ca_key_path)\
                .cert(ca_path)\
                .create_serial()\
                .config(config_path)
            revoke_cert_cmd = revoke_cert_cmd_builder.get_command_string()
            self._run(revoke_cert_cmd, stdout_func)

        crl_path = os.path.join(cert_dir, f'{crl_name}.crl')
        generate_crl_cmd_builder = OpenSslCmdBuilder()\
            .subcommand('ca')\
            .gencrl()\
            .keyfile(ca_key_path)\
            .cert(ca_path)\
            .out(crl_path)\
            .config(config_path)
        generate_crl_cmd = generate_crl_cmd_builder.get_command_string()
        self._run(generate_crl_cmd, stdout_func)
        self._validate_cert_location(crl_path)

        return crl_path

    def _validate_cert_location(self, cert_location):
        assert os.path.exists(cert_location), f"given location for cert doesn't exist: {cert_location}"

    def _validate_new_ca_location(self, new_ca_dir):
        self._verify_file(new_ca_dir, 'new CA', True)

    def _prepare_ca(self, existing_ca_private, existing_ca_public, expiration, new_ca_name, new_ca_path, new_ca_props,
                    stdout_func):
        stdout_func('prepare CA')
        if new_ca_props:
            ca_private_path, ca_public_path = self.generate_ca(new_ca_path, new_ca_name, expiration, stdout_func)
        else:
            stdout_func('use given existing CA')
            ca_private_path, ca_public_path = existing_ca_private, existing_ca_public

        stdout_func('verify CA files exist')
        self._verify_file(ca_public_path, 'existing CA public key')
        self._verify_file(ca_private_path, 'existing CA private key')
        return ca_private_path, ca_public_path

    def _gen_cert_csr_and_private_key(self, cert_location, cert_name, cn, stdout_func):
        stdout_func('generate certificate csr and private key')
        cert_csr_filename = f'{cert_name}.csr'
        cert_csr_path = os.path.join(cert_location, cert_csr_filename)
        cert_private_filename = f'{cert_name}.key'
        cert_private_path = os.path.join(cert_location, cert_private_filename)
        cn = cn or DEFAULT_DN
        gen_cert_key_cmd = f'openssl req -newkey rsa:2048 -nodes -keyout {cert_private_path} -subj /C=CN/ST=GD/L=SZ-Inc/CN={cn} -out {cert_csr_path}'
        self._run(gen_cert_key_cmd, stdout_func)
        self._verify_file(cert_csr_path, 'generated certificate csr')
        self._verify_file(cert_private_path, 'generated certificate key')
        self._chmod_for_reading(cert_csr_path, stdout_func)
        self._chmod_for_reading(cert_private_path, stdout_func)
        return cert_csr_path, cert_private_path

    def _issue_and_sign_public_cert(self, ca_private_path, ca_public_path, cert_csr_path, cert_location, cert_name, dn,
                                    expiration, ip, san_uris, stdout_func):
        stdout_func('generate and sign/issue the public certificate')
        cert_public_filename = f'{cert_name}.crt'
        cert_public_path = os.path.join(cert_location, cert_public_filename)

        # target_device_props = {'DNS': dn, 'IP': ip, 'URI': spiffe}
        # target_device_props = {k: v for k, v in target_device_props.items() if v}
        # subject_alt_name = ','.join([f'{k}:{v}' for k, v in target_device_props.items()])

        target_device_props = [('DNS', dn), ('IP', ip)]
        for uri in san_uris:
            target_device_props.append(('URI', uri))
        target_device_props = [tupl for tupl in target_device_props if tupl[1]]
        subject_alt_name = ','.join([f'{tupl[0]}:{tupl[1]}' for tupl in target_device_props])

        tmp_extfile = self._prepare_tmp_extfile(f'subjectAltName={subject_alt_name}')
        gen_and_issue_cert_public_cmd = (
            f'openssl x509 -req -in {cert_csr_path} -CA {ca_public_path} -CAkey {ca_private_path} '
            f'-CAcreateserial -out {cert_public_path} -days {expiration} -extfile {tmp_extfile}'
        )
        self._run(gen_and_issue_cert_public_cmd, stdout_func)
        self._verify_file(cert_public_path, 'generated certificate crt')
        self._chmod_for_reading(cert_public_path, stdout_func)
        return cert_public_path

    def _issue_and_sign_iCA(self, rCA_private_path, rCA_public_path, iCA_csr_path, cert_location, cert_name, expiration, stdout_func):
        stdout_func('generate and sign/issue the intermidiate certificate')
        iCA_public_filename = f'{cert_name}.crt'
        iCA_public_path = os.path.join(cert_location, iCA_public_filename)

        tmp_content = '[ server ]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid,issuer:always'
        tmp_extfile = self._prepare_tmp_extfile(tmp_content)

        gen_and_issue_cert_public_cmd = f'openssl x509 -req -in {iCA_csr_path} -CA {rCA_public_path} -CAkey {rCA_private_path} -CAcreateserial -out {iCA_public_path} -days {expiration} -extfile {tmp_extfile} -extensions server'
        self._run(gen_and_issue_cert_public_cmd, stdout_func)
        self._verify_file(iCA_public_path, 'generated iCA crt')
        self._chmod_for_reading(iCA_public_path, stdout_func)
        return iCA_public_path

    def _prepare_tmp_extfile(self, tmp_extfile_content: str):
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp_file:
            tmp_file.write(tmp_extfile_content)
            tmp_file.flush()
            tmp_extfile = tmp_file.name
        return tmp_extfile

    def _prepare_tmp_config(self, db_dir: str, original_config: str = '/etc/ssl/openssl.cnf'):
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_config:
            with open(original_config, 'r') as orig:
                temp_config.write(orig.read())
            temp_config.write(f"\n[ CA_default ]\ndatabase = {db_dir}/index.txt")
            temp_config.write(f"\n crlnumber = {db_dir}/crlnumber")
            temp_config.flush()
        return temp_config.name

    def _convert_crt_to_pem(self, cert_crt_public_path, cert_location, cert_name, stdout_func):
        stdout_func('convert public crt to PEM')
        cert_public_pem_filename = f'{cert_name}.pem'
        cert_public_pem_path = os.path.join(cert_location, cert_public_pem_filename)
        convert_crt_to_pem_cmd_builder = OpenSslCmdBuilder()\
            .subcommand('x509')\
            .in_file(cert_crt_public_path)\
            .out(cert_public_pem_path)\
            .outform('PEM')
        convert_crt_to_pem_cmd = convert_crt_to_pem_cmd_builder.get_command_string()
        self._run(convert_crt_to_pem_cmd, stdout_func)
        self._verify_file(cert_public_pem_path, 'generated certificate crt')
        self._chmod_for_reading(cert_public_pem_path, stdout_func)
        return cert_public_pem_path

    def _create_cert_p12_bundle(self, cert_location, cert_name, cert_private_path, cert_public_pem_path, p12_pass,
                                stdout_func):
        stdout_func('create p12 bundle of the generated certificate')
        p12_bundle_filename = f'{cert_name}.p12'
        p12_bundle_path = os.path.join(cert_location, p12_bundle_filename)
        create_p12_bundle_cmd_builder = OpenSslCmdBuilder()\
            .subcommand('pkcs12')\
            .export()\
            .out(p12_bundle_path)\
            .in_file(cert_public_pem_path)\
            .inkey(cert_private_path)\
            .passout('pass', p12_pass)\
            .keypbe('AES-256-CBC')\
            .certpbe('AES-256-CBC')
        create_p12_bundle_cmd = create_p12_bundle_cmd_builder.get_command_string()
        self._run(create_p12_bundle_cmd, stdout_func)
        self._verify_file(p12_bundle_path, 'generated certificate crt')
        self._chmod_for_reading(p12_bundle_path, stdout_func)

    def _openssl_verify_cert_with_ca(self, ca_file, cert_file, stdout_func):
        verify_cmd_builder = OpenSslCmdBuilder()\
            .subcommand('verify')\
            .CAfile(ca_file)\
            .positional_arg(cert_file)
        verify_cmd = verify_cmd_builder.get_command_string()
        self._run(verify_cmd, stdout_func)

    def _verify_file(self, file, purpose, is_dir=False):
        assert os.path.exists(file), f"given {purpose} path doesn't exist: {file}"
        if is_dir:
            assert os.path.isdir(file), f"given {purpose} path isn't a directory: {file}"
        else:
            assert os.path.isfile(file), f"{purpose} isn't a file: {file}"

    def _chmod_for_reading(self, file, stdout_func):
        chmod_cmd = f'chmod +r {file}'
        self._run(chmod_cmd, stdout_func)

    def _run(self, cmd: Union[str, list], stdout_func, validate: bool = True) -> str:
        cmd_str, cmd_list = (cmd, cmd.split(' ')) if isinstance(cmd, str) else (
            ' '.join([str(item) for item in cmd]), cmd)

        stdout_func(f'run: {cmd_str}')
        # Run the bash script
        result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=10)

        # Print the output
        stdout_func(result.stdout)

        # Print any error messages
        if validate and result.returncode != 0:
            stdout_func("Returned code is not 0. Errors:")
            stdout_func(result.stderr)
            raise ValueError(f'error has occurred\nout: {result.stdout}\nerr: {result.stderr}')

        return result.stdout


class CertificateGeneratorOnRemoteHost(CertificateGenerator):
    def __init__(self, engine):
        super().__init__()
        self.__engine = engine

    def _validate_cert_location(self, cert_location):
        self._run(f'mkdir -p {cert_location}')

    def _validate_new_ca_location(self, new_ca_dir):
        self._run(f'mkdir -p {new_ca_dir}')

    def _verify_file(self, file, purpose, is_dir=False):
        self._run(f'ls -l {file}')

    def _prepare_tmp_extfile(self, tmp_extfile_content: str):
        tmp_extfile = '/tmp/extfile'
        self._run(f'echo """{tmp_extfile_content}""" > {tmp_extfile}')
        return tmp_extfile

    def _run(self, cmd: str, stdout_func=logging.info, validate: bool = True) -> str:
        return self.__engine.run_cmd(cmd, validate=validate)


def __try_generator():
    # TODO: configure as desired
    cert_location: str = '/auto/sysgwork/alonn/playground/certs/spiffe/spif1/cert'
    cert_name: str = 'cert-with-2-spifs'
    ip: str = '10.7.144.58'
    dn: str = 'gorilla-58'
    subject_cn = dn
    expiration_years: int = 10
    new_ca_path: str = ''  # '/auto/sysgwork/alonn/playground/certs/spiffe/spif1/ca'
    new_ca_name: str = ''  # 'ca'
    existing_ca_public: str = '/auto/sysgwork/alonn/playground/certs/spiffe/spif1/ca/ca.crt'
    existing_ca_private: str = '/auto/sysgwork/alonn/playground/certs/spiffe/spif1/ca/ca.key'
    p12_pass: str = 'secret2'
    san_uris: List[str] = ['spiffe://alon-trusted.domain/users/ceos/alon', 'spiffe://alon-trusted.domain/users/ceos/lital']

    CertificateGenerator().generate_cert(cert_location, cert_name, subject_cn, ip, dn, new_ca_path, new_ca_name, p12_pass,
                                         existing_ca_public, existing_ca_private, expiration_years, san_uris, print)


if __name__ == '__main__':
    __try_generator()
