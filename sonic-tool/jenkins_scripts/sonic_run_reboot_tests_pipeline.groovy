SESSION_IDS = [:]
RUNCMDS = [:]
TOPOLOGYS = [:]
NEIGHBOR_TYPE = [:]
HWSKU = [:]


//Panther Main Hwsku
//Panther A0
HWSKU["arc-switch1004_setup"] = "Mellanox-SN2700-D48C8"
TOPOLOGYS["arc-switch1004_setup"] = "t0-56"

HWSKU["arc-switch1025_setup"] = "Mellanox-SN2700-D48C8"
TOPOLOGYS["arc-switch1025_setup"] = "t0-56"

HWSKU["r-panther-23_setup"] = "Mellanox-SN2700-D48C8"
TOPOLOGYS["r-panther-23_setup"] = "t0-56"

HWSKU["r-panther-45_setup"] = "Mellanox-SN2700-D48C8"
TOPOLOGYS["r-panther-45_setup"] = "t0-56"

//Panther A1
HWSKU["r-panther-01_setup"] = "Mellanox-SN2700-A1-D48C8"
TOPOLOGYS["r-panther-01_setup"] = "t0-56"

HWSKU["r-panther-02_setup"] = "Mellanox-SN2700-A1-D48C8"
TOPOLOGYS["r-panther-02_setup"] = "t0-56"

HWSKU["r-panther-40_setup"] = "Mellanox-SN2700-A1-D48C8"
TOPOLOGYS["r-panther-40_setup"] = "t0-56"

HWSKU["r-panther-42_setup"] = "Mellanox-SN2700-A1-D48C8"
TOPOLOGYS["r-panther-42_setup"] = "t0-56"


/*
//Panther Non Main Hwsku
//Panther A0
HWSKU["arc-switch1004_setup"] = "Mellanox-SN2700-C28D8"
TOPOLOGYS["arc-switch1004_setup"] = "t0"

HWSKU["arc-switch1025_setup"] = "Mellanox-SN2700"
TOPOLOGYS["arc-switch1025_setup"] = "t0"

HWSKU["r-panther-23_setup"] = "Mellanox-SN2700-C28D8"
TOPOLOGYS["r-panther-23_setup"] = "t0"

HWSKU["r-panther-45_setup"] = "Mellanox-SN2700"
TOPOLOGYS["r-panther-45_setup"] = "t0"

//Panther A1
HWSKU["r-panther-01_setup"] = "Mellanox-SN2700-A1-C28D8"
TOPOLOGYS["r-panther-01_setup"] = "t0"

HWSKU["r-panther-02_setup"] = "Mellanox-SN2700-A1"
TOPOLOGYS["r-panther-02_setup"] = "t0"

HWSKU["r-panther-40_setup"] = "Mellanox-SN2700-A1-C28D8"
TOPOLOGYS["r-panther-40_setup"] = "t0"

HWSKU["r-panther-42_setup"] = "Mellanox-SN2700-A1"
TOPOLOGYS["r-panther-42_setup"] = "t0"
*/




//not used for now.
HWSKU["r-panther-47_setup"] = "Mellanox-SN2700-D48C8"
TOPOLOGYS["r-panther-47_setup"] = "t0-56"
HWSKU["r-panther-48_setup"] = "Mellanox-SN2700-D48C8"
TOPOLOGYS["r-panther-48_setup"] = "t0-56"
TOPOLOGYS["mtvr-panther-02_setup"] = "t0"
TOPOLOGYS["mtvr-panther-03_setup"] = "t0"

//
TOPOLOGYS["r-tigris-04_setup"] = "t0-120"
TOPOLOGYS["r-tigris-13_setup"] = "t0"
TOPOLOGYS["r-tigris-25_setup"] = "t0-120"
TOPOLOGYS["r-tigris-26_setup"] = "t0"

TOPOLOGYS["r-tigon-04_setup"] = "t0-64"
TOPOLOGYS["r-tigon-11_setup"] = "t0-64"
TOPOLOGYS["r-tigon-20_setup"] = "t0-64"
TOPOLOGYS["r-tigon-21_setup"] = "t0-64"
TOPOLOGYS["mtvr-tigon-05_setup"] = "t0-64"
TOPOLOGYS["mtvr-tigon-07_setup"] = "t0-64"


TOPOLOGYS["mtvr-leopard-01_setup"] = "t0-56-o8v48"
TOPOLOGYS["mtvr-leopard-09_setup"] = "t0-56-o8v48"
TOPOLOGYS["r-leopard-01_setup"] = "t0-56-o8v48"
TOPOLOGYS["r-leopard-58_setup"] = "t0-56-o8v48"
HWSKU["r-leopard-01_setup"] = "Mellanox-SN4700-O8V48"
HWSKU["r-leopard-58_setup"] = "Mellanox-SN4700-O8V48"
HWSKU["mtvr-leopard-01_setup"] = "Mellanox-SN4700-O8V48"
HWSKU["mtvr-leopard-09_setup"] = "Mellanox-SN4700-O8V48"


// Panther A0 need to use vsonic as neighbor
NEIGHBOR_TYPE["arc-switch1004_setup"] = "vsonic"
NEIGHBOR_TYPE["arc-switch1025_setup"] = "vsonic"
NEIGHBOR_TYPE["r-panther-23_setup"] = "vsonic"
NEIGHBOR_TYPE["r-panther-45_setup"] = "vsonic"
NEIGHBOR_TYPE["r-panther-47_setup"] = "vsonic"
NEIGHBOR_TYPE["r-panther-48_setup"] = "vsonic"
NEIGHBOR_TYPE["mtvr-panther-02_setup"] = "vsonic"
NEIGHBOR_TYPE["mtvr-panther-03_setup"] = "vsonic"

//moose only run cold reboot for 202405
TOPOLOGYS["r-moose-01_setup"] = "t1-lag-c224o8"
TOPOLOGYS["mtvr-moose-04_setup"] = "t0-c256"
HWSKU["r-moose-01_setup"] = "Mellanox-SN5600-C224O8"
HWSKU["mtvr-moose-04_setup"] = "Mellanox-SN5600-C256S1"


def get_vault_creds() {
    def vault_config = [vaultUrl: 'https://prod.vault.nvidia.com',
    vaultNamespace: "nbu-system-sw-sonic",
    vaultCredentialId: 'vault-sonic-approle-prod',
    engineVersion: 1]
    def vault_path_prefix = "nvidia/nbu/mars/sonic/kv"
    def secrets = [
        [path: "${vault_path_prefix}/stm_server",
        engineVersion: 1,
        secretValues: [[envVar: 'STM_USER', vaultKey: 'user'],
        [envVar: 'STM_PASSWORD', vaultKey: 'password']]],
        [path: "${vault_path_prefix}/sonic_server",
        engineVersion: 1,
        secretValues: [[envVar: 'SONIC_SERVER_USER', vaultKey: 'user'],
        [envVar: 'SONIC_SERVER_PASSWORD', vaultKey: 'password']]],
        [path: "${vault_path_prefix}/sonic_mgmt",
        engineVersion: 1,
        secretValues: [[envVar: 'SONIC_MGMT_USER', vaultKey: 'user'],
        [envVar: 'SONIC_MGMT_PASSWORD', vaultKey: 'password']]]
    ]

    return [vault_config, secrets]
}


def get_SetupNameRebootTypeMap(setup_name) {
    if (env.test_config_yaml_file) {
        echo "get reboot types for ${setup_name} from ${env.test_config_yaml_file}"
        def test_config = readYaml(file: env.test_config_yaml_file)
        reboot_types = []
        // get all .db files from sonic-mgmt/${setup_name} folder
        // the file name format is ${reboot_type}.db
        def db_files = findFiles(glob: "sonic-mgmt/${setup_name}/*.db")
        db_files.each { db_file ->
            def db_file_name = db_file.name
            def reboot_type = db_file_name[0..-4]  // Remove .db extension
            reboot_types.add(reboot_type)
        }
        return reboot_types
    }

    reboot_types = []
	if (env.fast_reboot_iterations_number != '0') {
		if (env.fast_reboot_executors.trim()) {
			fast_reboot_executors = env.fast_reboot_executors.split(',')
			if (fast_reboot_executors.contains(setup_name)) {
				reboot_types.add("fast")
			}
		}
	}
	if (env.warm_reboot_iterations_number != '0') {
		if (env.warm_reboot_executors.trim()) {
			warm_reboot_executors = env.warm_reboot_executors.split(',')
			if (warm_reboot_executors.contains(setup_name)) {
				reboot_types.add("warm")
			}
		}
	}
	echo "cold reboot iterations ${env.cold_reboot_iterations_number}"
    if (env.cold_reboot_iterations_number != '0') {
		if (env.cold_reboot_executors.trim()) {
			cold_reboot_executors = env.cold_reboot_executors.split(',')
			if (cold_reboot_executors.contains(setup_name)) {
				reboot_types.add("cold")
			}
		}
	}

    return reboot_types
}


def getSetupNames(){

    setup_names = []
    if (env.test_config_yaml_file) {
        echo "get setup names from ${env.test_config_yaml_file}"
        def test_config = readYaml(file: env.test_config_yaml_file)
        test_config.all_tests.each{ setup ->
            for (t in setup.tests) {
                if (t.containsKey('iterations') && t.iterations.toInteger() > 0) {
                    setup_names.add(setup.setup_name)
                    break
                }
            }
        }
        echo "setup_names: ${setup_names}"
        return setup_names
    }

    if (env.fast_reboot_executors.trim()) {
        fast_reboot_executors = env.fast_reboot_executors.split(',')
	    fast_reboot_executors.each{ value ->
            setup_names.add(value)
		}
    }

    if (env.warm_reboot_executors.trim()) {
        warm_reboot_executors = env.warm_reboot_executors.split(',')
        warm_reboot_executors.each{ value ->
            if (value in setup_names) {
                echo "Already exist"
            } else {
                 setup_names.add(value)
            }
        }
    }

    if (env.cold_reboot_executors.trim()) {
        cold_reboot_executors = env.cold_reboot_executors.split(',')
        cold_reboot_executors.each{ value ->
            if (value in setup_names) {
                echo "Already exist"
            } else {
                 setup_names.add(value)
            }
        }
    }
    return setup_names
}


def addSvcUser() {
    echo "Adding user"
    add_user_cmd = sh (script: "eval \"echo useradd -m ${env.STM_USER} -u \$(id -u) -g \$(id -g)\"", returnStdout: true)
    sh "echo ${env.SONIC_MGMT_PASSWORD} | su -c '${add_user_cmd}'"
    passwd_input = "${env.STM_PASSWORD}\n${env.STM_PASSWORD}"
    change_passwd_cmd = "echo -e \"${passwd_input}\" | passwd ${env.STM_USER}"
    sh "echo ${env.SONIC_MGMT_PASSWORD} | su -c '${change_passwd_cmd}'"
    sh "echo ${env.STM_PASSWORD} | su - ${env.STM_USER}"
}


def cloneRepoAndCheckoutBranch() {
    // Clone sonic-mgmt repo and checkout into branch
    tar = env.custom_tarball
    sh "rm -rf ./*"
    sh "tar -xvf ${tar} -C ./ > /dev/null"
}


def prepareSonicMgmtTarball(custom_tarball) {
    // Create sonic-mgmt tarball
    sh "tar -czvf ${custom_tarball} sonic-mgmt/ > /dev/null"
    sh "chmod 777 ${custom_tarball}"
    sh "cp ${custom_tarball} /.autodirect/sw_regression/system/SONIC/MARS/tarballs/"
}

def prepareRunCmd(setup_name, custom_tarball, base_version = env.base_version, target_version = env.target_version) {
	reboot_types = get_SetupNameRebootTypeMap(setup_name)

    base_versions_list = base_version
    echo "Base version is: ${base_versions_list}"
    echo "Target version is: ${target_version}"

    exec_block_gen_arg = "--meinfo_execution_block_generator=\\\"["
    echo "reboot_types: ${reboot_types} for ${setup_name}"
    reboot_types.each { value ->
        db_file_name = "${value}_reboot.db"
        if (env.test_config_yaml_file) {
            db_file_name = "${setup_name}/${value}.db"
        }
        exec_block_gen_arg = exec_block_gen_arg +
            "{'entry_points': 'SONIC_MGMT', " +
            "'tests_dbs_tarball': 'sonic-mgmt/${db_file_name}'}, "
    }

	exec_block_gen_arg.trim()
	exec_block_gen_arg = exec_block_gen_arg + "]\\\""
	echo "setupname's ${setup_name} exec_block_gen_arg is ${exec_block_gen_arg}"
    // Convert setup name to .setup file name
    setup_file_name = setup_name.replace("_setup", ".setup")

    mars_setup_cli_path = "/.autodirect/sw_tools/Internal/MARS/mars_apps/RELEASE/4_3_11/bin/setup_cli.py"
    tarball_arg = "--meinfo_custom_tarball_name ${custom_tarball}"

    // If no target ver - base_ver = target ver
    if (target_version.trim()) {
        echo "Target version is provided, will be executed test with an upgrade"
    } else {
        echo "Target version is not provided, the test will be executed without the upgrade"
        target_version = base_versions_list
    }
    initial_deploy_version = target_version
    // Override target version if test yaml file is provided and deploy_version
    // is specified in the test yaml file for the given setup
    // This is the image version for initial deployment
    if (env.test_config_yaml_file) {
        try {
            test_config = readYaml(file: env.test_config_yaml_file)
            deploy_version = test_config.all_tests.find { it.setup_name == setup_name }?.deploy_version
            if (deploy_version && deploy_version.trim() != "") {
                echo "Deploy version for ${setup_name} is ${deploy_version}"
                initial_deploy_version = deploy_version
            }
        } catch (Exception e) {
            echo "No deploy version specified for ${setup_name} in ${env.test_config_yaml_file}"
        }
    }

    base_ver_arg = "--meinfo_base_version ${initial_deploy_version}"
    topology = "t0"
	if (TOPOLOGYS.containsKey(setup_name)) {
		topology = TOPOLOGYS[setup_name]
	}

	neighbor_type = "ceos"
	if (NEIGHBOR_TYPE.containsKey(setup_name)) {
		neighbor_type = NEIGHBOR_TYPE[setup_name]
	}

    dest_hwsku_arg = ""
    if (HWSKU.containsKey(setup_name)) {
		dest_hwsku = HWSKU[setup_name]
		dest_hwsku_arg = "--meinfo_dut_hwsku ${dest_hwsku}"
	}

    stm_cmd = "${mars_setup_cli_path} --cmd start --setup ${setup_name} --conf ${setup_file_name} ${tarball_arg} ${base_ver_arg} ${exec_block_gen_arg} ${dest_hwsku_arg} --meinfo_topology ${topology} --meinfo_neighbor_type ${neighbor_type}"
    echo "Running CMD on STM: ${stm_cmd}"


    // Redirect STDERR to STDOUT to have them together in the same stream
    local_cmd = "sshpass -p ${env.STM_PASSWORD} ssh ${env.STM_USER}@mtr-stm-095 -o StrictHostKeyChecking=no \" ${stm_cmd} \" 2>&1"
    echo "Running CMD locally: ${local_cmd}"
	
	RUNCMDS[setup_name] = local_cmd
// 	sleep(1800)
}

def runTestForSetup(setup_name){
    local_cmd = RUNCMDS[setup_name]
    echo "cmd for ${setup_name} is: ${local_cmd}"
    try {
        result = sh (script: local_cmd, returnStdout: true)
        // Get -2 the end element from output(it's always MARS session ID)
        session_id = result.tokenize()[-2]
        echo "Have session ID ${session_id}"
        run_failed = false
    } catch (Throwable exc) {
        run_failed = true
        echo "Failed to run MARS session"
    } finally {
        if (!run_failed) {
            // Add session ID to shared dict - later use it for collect report
            SESSION_IDS[setup_name] = session_id
        }
    }
}


def collectTestResults() {
    // Collect tests results
    echo "Collecting tests results for session IDs data: ${SESSION_IDS}"
    def (vault_config, secrets) = get_vault_creds()
    withVault([configuration: vault_config, vaultSecrets: secrets]) {
        SESSION_IDS.each{setup_name, session_id ->
            echo "Collecting results from setup: ${setup_name} session id: ${session_id}"
            sh "/ngts_venv/bin/python sonic-mgmt/sonic-tool/jenkins_scripts/sonic_run_reboot_tests_pipeline.py --session_id ${session_id} --setup_name ${setup_name}"
        }
    }
}


pipeline {

    agent {
        docker { image 'harbor.mellanox.com/sonic/docker-ngts:1.2.403'
                 args """--entrypoint="" -v /.autodirect/sw_regression/system/SONIC/MARS/tarballs:/.autodirect/sw_regression/system/SONIC/MARS/tarballs/ \
                    -v /auto/sw_regression/system/SONIC/MARS/tarballs:/auto/sw_regression/system/SONIC/MARS/tarballs/ \
                    -v /auto/sw_regression/system/SONIC/MARS/reboot_tests_config.d:/auto/sw_regression/system/SONIC/MARS/reboot_tests_config.d/ \
                    -v /auto/sw_system_release/sonic:/auto/sw_system_release/sonic"""
                 label 'SONIC_LITE'

            }
        }

    stages {
        stage('Preparation') {
            steps {
                script {
                    // Generate timestamp and set custom_tarball name
                    def dateFormatter = java.time.format.DateTimeFormatter.ofPattern('yyyy-MM-dd_HH-mm-ss-SSS')
                    def date_time = java.time.LocalDateTime.now().format(dateFormatter)
                    custom_tarball = "jenkins_reboot_tests_runner_${date_time}.db.1.tgz"
                }
                echo "custom_tarball: ${custom_tarball}"

                cloneRepoAndCheckoutBranch()
                // Prepare .cases and .db files
                sh "/ngts_venv/bin/python sonic-mgmt/sonic-tool/jenkins_scripts/sonic_run_reboot_tests_pipeline.py --do_preparation"
                prepareSonicMgmtTarball(custom_tarball)
                script {
                    def (vault_config, secrets) = get_vault_creds()
                    withVault([configuration: vault_config, vaultSecrets: secrets]) {
                        addSvcUser()
						getSetupNames().each{setup_name ->
							prepareRunCmd(setup_name, custom_tarball)
						}
					}
				}
            }
        }
        stage('Tests execution') {
                steps {
            script {
                parallel getSetupNames().collectEntries { name ->
                    ["Execution ${name}": {
                        // Following code will be executed in parallel for each chosen setup
                        stage(name) {
                            //def (vault_config, secrets) = get_vault_creds()
                            //withVault([configuration: vault_config, vaultSecrets: secrets]) {
                                runTestForSetup(name)
                            //}
                        }
                    }]
                }
            }
        }
        }
        stage('Collect results') {
            steps {
                collectTestResults()
            }
        }
    }
 post {
        always {
            sh "/ngts_venv/bin/python sonic-mgmt/sonic-tool/jenkins_scripts/sonic_run_reboot_tests_pipeline.py --generate_report_email"
            emailext body: '${FILE,path="email_report.html"}',
                    to: "nbu-system-sw-sonic-ver@exchange.nvidia.com",
                    subject: '$PROJECT_NAME #$BUILD_NUMBER results'
        }
    }
}
