import logging
import random
import time

from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.infra.DutUtilsTool import run_ssh_command
from ngts.tests_nvos.cluster.ansible_playbooks_tool import AnsiblePlaybooksTool
from ngts.tests_nvos.cluster.cluster_consts import AnsiblePlaybooksConsts as Ansible
from ngts.tools.align_components.align_fw_components import create_json_dict, get_provisioning, get_switch_info
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


def test_align_cluster_recipe(setup_name, fw_versions_json_file, ansible_inventory_file):
    """
    NEW IMPLEMENTATION: Align cluster using new nvidia.nvlink Ansible collections.

    Executes all alignment playbooks in sequence. JSON file must have both
    'prod' and 'dev' sections. Firmware playbooks automatically extract from
    correct sections.
    """
    with allure.step("Load component versions from JSON"):
        switch_info = get_switch_info(setup_name)
        json_dict = create_json_dict(fw_versions_json_file)

        provisioning_type = get_provisioning(switch_info)  # for logging

        logger.info(f"Switch provisioning type: {provisioning_type}")
        logger.info(f"Inventory file: {ansible_inventory_file}")
        logger.info("NOTE: Firmware playbooks will use BOTH prod and dev versions")

    # Select ansible machine
    ansible_machine = random.choice(Ansible.ANSIBLE_MACHINES)
    username = Ansible.ANSIBLE_MACHINES_CREDENTIALS[ansible_machine]['user']
    password = Ansible.ANSIBLE_MACHINES_CREDENTIALS[ansible_machine]['pass']

    logger.info(f"Ansible machine: {ansible_machine}")
    logger.info(f"User: {username}")

    # Track failed playbooks
    failed_playbooks = []

    # Execute playbooks in order
    for playbook_key in Ansible.ALIGNMENT_PLAYBOOKS_ORDER[setup_name]:
        try:
            with allure.step(f"Executing playbook: {playbook_key}"):
                logger.info(f"\n{'=' * 80}")
                logger.info(f"Starting playbook: {playbook_key}")
                logger.info(f"{'=' * 80}\n")

                # Sleep between playbooks
                logger.info("Sleeping for 20 seconds between playbooks execution")
                time.sleep(20)

                # Execute playbook
                fetch_and_install(
                    playbook_key,
                    json_dict,
                    ansible_inventory_file,
                    ansible_machine,
                    username,
                    password,
                    setup_name=setup_name,
                )

                logger.info(f"Playbook '{playbook_key}' completed successfully\n")

        except Exception as e:
            failed_playbooks.append(playbook_key)
            logger.error(f"Playbook '{playbook_key}' failed: {e}")

    # Final assertion
    assert failed_playbooks == [], f"Playbooks {failed_playbooks} failed - see logs"

    logger.info("\n" + "=" * 80)
    logger.info("CLUSTER ALIGNMENT COMPLETED SUCCESSFULLY")
    logger.info("=" * 80 + "\n")


def fetch_and_install(playbook_key, json_dict, inventory_file, ansible_machine, username, password,
                      setup_name=None):
    """
    NEW IMPLEMENTATION: Download components and run playbook.

    Handles BOTH prod and dev provisioning sections.
    Firmware components (BMC/CPLD/HMC) require both prod AND dev versions.

    Args:
        playbook_key: Key from PLAYBOOKS dict (e.g., 'SOFTWARE_INSTALL')
        json_dict: Parsed JSON with 'prod' and 'dev' sections
        inventory_file: Path to inventory file
        ansible_machine: Ansible server hostname/IP
        username: SSH username
        password: SSH password
        setup_name: Optional lab/setup id (selects per-setup playbook variants when configured)
    """
    downloaded_files = []
    component_paths = {}

    try:
        # Get component mappings for this playbook
        component_mappings = Ansible.get_component_mappings(playbook_key)

        logger.info(f"Processing playbook: {playbook_key}")
        logger.info(f"Number of parameters: {len(component_mappings)}")

        # Step 1: Download/prepare all components
        for mapping in component_mappings:
            component = mapping['component']
            prov_section = mapping['provisioning']
            param = mapping['param']

            with allure.step(f"Prepare: json['{prov_section}']['{component}'] → {param}"):
                # Extract from correct JSON section
                package_path = json_dict[prov_section][component]['latest']['path']
                package_name = json_dict[prov_section][component]['latest']['filename']

                logger.info(f"Parameter: {param}")
                logger.info(f"  Source: json['{prov_section}']['{component}']")
                logger.info(f"  Path: {package_path}")
                logger.info(f"  Filename: {package_name}")

                # SMART LOGIC: Auto-detect if we need to download based on path
                # If path starts with http:// or https:// → download to /tmp
                # Otherwise → use as local file path directly

                if package_path.startswith("http://") or package_path.startswith("https://"):
                    # HTTP/HTTPS URL - download to /tmp
                    download_path = f"/tmp/{package_name}"

                    with allure.step(f"Downloading {package_name} to /tmp"):
                        # SOLUTION: Use base64 auth header to avoid special character issues
                        # Password contains: } ' ) which get mangled through SSH layers
                        # Base64 has ONLY safe characters: A-Z a-z 0-9 + / =
                        import base64
                        auth_string = f"{NvosConst.SONIC_SERVICE_ACCOUNT}:{NvosConst.SONIC_SERVICE_ACCOUNT_API_KEY}"
                        auth_b64 = base64.b64encode(auth_string.encode()).decode()

                        download_package = f"""
url="{package_path}"
out="{download_path}"
temp_out="{download_path}.part"

rm -f "$temp_out"

code=$(curl -fL -sS \\
  --retry 3 --retry-delay 2 --retry-all-errors \\
  --connect-timeout 15 --max-time 1800 \\
  -H "Authorization: Basic {auth_b64}" \\
  -o "$temp_out" -w '%{{http_code}}' "$url")
rc=$?

if [ $rc -eq 0 ] && [ "$code" = "200" ]; then
  mv "$temp_out" "$out"
  echo "Download successful"
else
  rm -f "$temp_out"
  echo "Download failed (curl_rc=$rc http_code=$code)"
fi
"""
                        # LOG THE COMMAND (with credentials redacted for security)
                        safe_command = download_package.replace(auth_b64, "***REDACTED***")
                        logger.info(f"Download command to execute:\n{safe_command}")
                        logger.info("Using base64 auth (redacted)")

                        download_successful = False
                        for attempt in range(1, 4):
                            logger.info(f"Download attempt {attempt}/3 for {package_name}")
                            output = run_ssh_command(download_package, ansible_machine, username, password)

                            # FIX: Check for None output before string operations
                            if output is None:
                                logger.error("SSH command failed - no output received")
                                continue

                            if "Download successful" in output:
                                logger.info(f"Download successful on attempt {attempt}")
                                download_successful = True
                                break
                            else:
                                logger.warning(f"Download failed on attempt {attempt}: {output}")
                                if attempt < 3:
                                    time.sleep(10)

                        assert download_successful, f"Download failed: {package_name}"

                        # Verify
                        verify_cmd = f'ls -lh {download_path}'
                        output = run_ssh_command(verify_cmd, ansible_machine, username, password)
                        assert download_path in output, f"File not found: {download_path}"
                        logger.info(f"Verified: {output}")

                        downloaded_files.append(download_path)
                        component_paths[param] = download_path

                else:
                    # Local file path - use directly (no download)
                    logger.info(f"Using local file path: {package_path}")
                    component_paths[param] = package_path

        # Step 2: Run playbook with all parameters
        with allure.step(f"Running playbook '{playbook_key}' with {len(component_paths)} parameters"):
            logger.info(f"Playbook parameters: {component_paths}")

            # FIX: Pass ansible_machine to ensure playbook runs on same host as downloads
            status = AnsiblePlaybooksTool.run_playbook_by_key(
                playbook_key,
                inventory_file,
                component_paths,
                ansible_machine=ansible_machine,
                username=username,
                password=password,
                setup_name=setup_name,
            )

            assert status, f"Playbook '{playbook_key}' failed - Check logs"
            logger.info(f"Playbook '{playbook_key}' completed successfully")

        return status

    finally:
        # Step 3: Cleanup
        if downloaded_files:
            with allure.step("Cleanup downloaded files"):
                for file_path in downloaded_files:
                    try:
                        logger.info(f"Deleting {file_path}")
                        run_ssh_command(f"rm -rf {file_path}", ansible_machine, username, password)
                    except Exception as e:
                        logger.warning(f"Failed to delete {file_path}: {e}")
