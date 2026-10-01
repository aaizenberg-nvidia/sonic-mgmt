package vars

def pre(name) {
    return true
}
def is_stability_ci() {
    try {
        def branch = params.GERRIT_BRANCH?.replace("origin/", "")?.replace("origin1/", "")
        return branch == "develop" &&  params.GERRIT_CHANGE_SUBJECT == "Stability test Issue: N/A"
    } catch (Throwable ex) {
        print "[WARNING] Failed to determine if build is stability CI: ${ex}"
        return false
    }
}

def set_dpu_bin(topic_map) {
    def DPU_bin_path
    if (topic_map["IMAGE_DPU_BRANCH"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_DPU_BRANCH"]) &&
            topic_map["IMAGE_DPU_VERSION"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_DPU_VERSION"])) {
        error "IMAGE_BRANCH and IMAGE_VERSION cannot be defined together. remove one or both of them from Gerrit topic to continue "
    }

    def dpu_branch = env.GERRIT_BRANCH ? env.GERRIT_BRANCH : "smart-switch-master"
    if (topic_map["IMAGE_DPU_BRANCH"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_DPU_BRANCH"])) {
        dpu_branch = topic_map["IMAGE_DPU_BRANCH"]
        print "DPU image branch name is defined by topic: ${dpu_branch}"
    }

    def dpu_version_name
    if (topic_map["IMAGE_DPU_VERSION"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_DPU_VERSION"])) {
        dpu_version_name = topic_map["IMAGE_DPU_VERSION"]
        print "DPU image version is defined by topic \"IMAGE_DPU_VERSION\"."
    } else {
        def mgmt_tools = NGCITools().ciTools.load_project_lib("${env.SHARED_LIB_FILE}")
        dpu_version_name = mgmt_tools.get_dpu_lastrc_version(dpu_branch)

        // This part should throw exception in the future and align with SONIC case.
        // It was decided to skip DPU tests if the lastrc version is not defined or found
        // We will do it when dpu lastrc methodology will be aligned with sonic
        if (dpu_version_name == null) {
            echo "DPU image version is not defined by lastrc link for branch: ${dpu_branch}, in that case will skip DPU tests"
            echo "Setting SKIP_SONIC_HW_SS_BAT to true"
            env.SKIP_SONIC_HW_SS_BAT = true
            DPU_bin_path = "Not Defined"
        }
    }
    // In the future we should throw exception if DPU_bin_path is not defined and remove this if
    if (DPU_bin_path != "Not Defined") {
        DPU_bin_path = "${env.DPU_VERSION_DIRECTORY}/${dpu_version_name}/dev/Nvidia-bluefield/sonic-nvidia-bluefield.bfb"
        if (! fileExists(DPU_bin_path)) {
         error "ERROR:SONiC bin file not found: ${DPU_bin_path}"
        }
    }
    env.DPU_BIN = DPU_bin_path
    print "DPU_BIN = ${env.DPU_BIN}"
}

def set_sonic_bin(topic_map, project) {
    if (topic_map["IMAGE_BRANCH"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_BRANCH"]) &&
            topic_map["IMAGE_VERSION"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_VERSION"])) {
        error "IMAGE_BRANCH and IMAGE_VERSION cannot be defined together. remove one or both of them from Gerrit topic to continue "
    }

    def sonic_branch = env.GERRIT_BRANCH ? env.GERRIT_BRANCH : "master"
    if (topic_map["IMAGE_BRANCH"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_BRANCH"])) {
        sonic_branch = topic_map["IMAGE_BRANCH"]
        print "SONiC image branch name is defined by topic: ${sonic_branch}"
    }

    def sonic_version_name
    if (topic_map["IMAGE_VERSION"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_VERSION"])) {
        sonic_version_name = topic_map["IMAGE_VERSION"]
        print "SONiC image version  is defined by topic \"IMAGE_VERSION\"."
    } else {
        def mgmt_tools = NGCITools().ciTools.load_project_lib("${env.SHARED_LIB_FILE}")
        sonic_version_name = mgmt_tools.get_sonic_lastrc_version(sonic_branch)
        print "SONiC image version is defined by lastrc link for branch: ${sonic_branch}"
        if (sonic_version_name == null) {
            error "ERROR: SONiC image version is not defined by lastrc link for branch: ${sonic_branch}"
        }
    }

    if (sonic_version_name.contains("_Public")) {
        env.VERSION_DIRECTORY = env.VERSION_DIRECTORY + "/public"
    }

    def sonic_bin_path = "${env.VERSION_DIRECTORY}/${sonic_version_name}/dev/Mellanox/sonic-mellanox.bin"
    env.README_PATH = "${env.VERSION_DIRECTORY}/${sonic_version_name}/dev"
    if (! new File(sonic_bin_path).exists()) {
        print "SONiC bin file not found: ${sonic_bin_path}\nWill try the old convention (without 'dev' folder)"
        sonic_bin_path = sonic_bin_path.replace("/dev/","/")
        env.README_PATH = "${env.VERSION_DIRECTORY}/${sonic_version_name}"
        if (! new File(sonic_bin_path).exists()) {
            error "ERROR:SONiC bin file not found: ${sonic_bin_path}"
        }
    }
    try {
        handleAirUpload("${env.README_PATH}")
        env.SKIP_AIR = false
    } catch (Exception ex) {
        env.SKIP_AIR = true
        if (env.FAIL_ON_AIR_UNSUPPORTED == "true") {
            error "ERROR: AIR upload failed: ${ex.message}"
        } else {
            echo "WARNING: AIR upload failed: ${ex.message}"
        }
    }
    env.SONIC_BIN = sonic_bin_path
    print "SONIC_BIN = ${env.SONIC_BIN}"
}

def get_vault_connection() {
    def configuration = [:]
    configuration.put("vaultUrl",         "https://prod.vault.nvidia.com")
    configuration.put("vaultNamespace",   "nbu-system-sw-sonic")
    configuration.put("vaultCredentialId", "vault-sonic-approle-prod")
    configuration.put("engineVersion", 1)

    def vaultSecrets = [[
        path: "nvidia/nbu/mars/sonic/kv/air_ngc_sonic",
        secretValues: [
            [envVar: "AIR_TOKEN", vaultKey: "service_key"],
        ]
    ]]
    return [configuration: configuration, vaultSecrets: vaultSecrets]
}


def handleAirUpload(String readme_path) {
    def readmeFullPath = "${readme_path}/README"
    echo "readme_path: ${readmeFullPath}"
    sh "cat ${readmeFullPath}"
    def readme_properties = sonic_readme_to_map(readmeFullPath)
    echo "readme_properties: ${readme_properties}"
    def air_image_id = readme_properties.get("AIR_IMAGE_ID", null)
    if(air_image_id == null) {
        error "AIR_IMAGE_ID not found in readme file"
    }
    def vault_connection = get_vault_connection()
    withVault([configuration: vault_connection.configuration, vaultSecrets:  vault_connection.vaultSecrets]) {
        def image_info = devopsAir.getImageById([id: air_image_id])
        echo "image_info: ${image_info}"
        if(image_info) {
            echo "AIR image id found: ${image_info.id}"
            return
        }
        echo "AIR image id not found: ${air_image_id}, will upload it"
        def name = "sonic-vm-${readme_properties.get("VERSION_NAME")}"
        def version = "tmp_${env.JOB_NAME}_${currentBuild.number}"
        def emulation_version = readme_properties.get("SIMX_VERSION")?.replaceFirst(/\s*\(.*$/, "")
        def image_path = "${readme_path}/vm/sonic-mellanox.img.gz"
        def new_local_file_path = "${pwd()}/${name}.img.gz".replace(".img.gz", ".qcow2")
        echo "new_local_file_path: ${new_local_file_path}"
        if (!fileExists(image_path)) {
            error "ERROR: SONiC bin file not found: ${image_path}"
        }
        sh """
           cp ${image_path} ${new_local_file_path}
        """
         
        def imageConfig = [
            cpu_arch : "x86",
            name: name,
            version: version,
            emulation_type: ["SWITCH_ETHERNET"],
            emulation_version : emulation_version,
            provider : "VM",
            default_username : "admin",
            default_password : "YourPaSsWoRd",
            includes_air_agent: true,
            file_to_upload: new_local_file_path
        ]

        withVault([configuration: vault_connection.configuration, vaultSecrets:  vault_connection.vaultSecrets]) {
            def upload_result = devopsAir.uploadAirImage(imageConfig)
   
            sh "rm ${new_local_file_path}"
            if(!upload_result) {
                error "ERROR: Failed to upload AIR image"
            }
            echo "AIR image uploaded successfully ${upload_result.id}"
            update_readme_file(readmeFullPath, [AIR_IMAGE_ID: upload_result.id])
            echo "updated readme file: ${readmeFullPath}"
            sh "cat ${readmeFullPath}"
        }
    }
}


def set_nvos_bin(topic_map, project){
    if (topic_map["IMAGE_NVOS_BRANCH"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_NVOS_BRANCH"]) &&
            topic_map["IMAGE_NVOS_VERSION"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_NVOS_VERSION"])) {
        error "IMAGE_NVOS_BRANCH and IMAGE_NVOS_VERSION cannot be defined together. remove one or both of them from Gerrit topic to continue "
    }

    def nvos_branch = env.DEFAULT_NVOS_BRANCH ? env.DEFAULT_NVOS_BRANCH : "master"
    if (topic_map["IMAGE_NVOS_BRANCH"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_NVOS_BRANCH"])) {
        nvos_branch = topic_map["IMAGE_NVOS_BRANCH"]
        print "NVOS image branch name is defined by topic: ${nvos_branch}"
    } else if (project == "nvos") {
        if (env.GERRIT_BRANCH && NGCITools().ciTools.is_parameter_contains_value(env.GERRIT_BRANCH)) {
            nvos_branch = env.GERRIT_BRANCH.replace("dev_ver", "dev").replace("nvos_ver", "nvos")
            print "NVOS image branch name branch name: ${nvos_branch}"
        }
    }

    def nvos_version_name
    if (topic_map["IMAGE_NVOS_VERSION"] && NGCITools().ciTools.is_parameter_contains_value(topic_map["IMAGE_NVOS_VERSION"])) {
        nvos_version_name = topic_map["IMAGE_NVOS_VERSION"]
        print "NVOS image version  is defined by topic \"IMAGE_VERSION\"."
    } else {
        def mgmt_tools = NGCITools().ciTools.load_project_lib("${env.SHARED_LIB_FILE}")
        nvos_version_name =  mgmt_tools.get_nvos_lastrc_version(nvos_branch)
        print "NVOS image version is defined by lastrc link for branch: ${nvos_branch}"
    }

    def nvos_bin_path = "${env.NVOS_VERSION_DIRECTORY}/${nvos_version_name}/amd64/dev/nvos-amd64-${nvos_version_name}.bin"
    if (! new File(nvos_bin_path).exists()) {
        print "NVOS bin file not found: ${nvos_bin_path}\nWill try the old convention (without 'dev' folder)"
        nvos_bin_path = nvos_bin_path.replace("/dev/","/")
        if (! new File(nvos_bin_path).exists()) {
            error "ERROR: NVOS bin file not found: ${nvos_bin_path}"
        }
    }

    env.NVOS_BIN = nvos_bin_path
    print "NVOS_BIN = ${env.NVOS_BIN}"
}

def sonic_readme_to_map(String filePath) {
    def result = [:]
    if (!fileExists(filePath)) {
        error "README file not found at: ${filePath}"
    }
    def content = readFile(filePath)
    content.split('\n').each { line ->
        def matcher = line =~ /^([^:]+):\s*(.+)$/
        if (matcher.matches()) {
            def key = matcher[0][1].trim()
            def value = matcher[0][2].trim()
            result[key] = value
        }
    }
    return result
}

def update_readme_file(String filePath, Map updates) {
    if (!fileExists(filePath)) {
        error "README file not found at: ${filePath}"
    }
    def content = readFile(filePath)
    def lines = content.split('\n')
    def updatedLines = []
    
    lines.each { line ->
        def matcher = line =~ /^([^:]+):\s*(.+)$/
        if (matcher.matches()) {
            def key = matcher[0][1].trim()
            def originalValue = matcher[0][2].trim()
            
            // Update the value if the key exists in the updates map
            if (updates.containsKey(key)) {
                def newValue = updates[key]
                updatedLines.add("${key}: ${newValue}")
            } else {
                // Keep the original line
                updatedLines.add(line)
            }
        } else {
            // Keep non-matching lines as-is (empty lines, comments, etc.)
            updatedLines.add(line)
        }
    }
    
    // Write the updated content back to the file
    writeFile file: filePath, text: updatedLines.join('\n')
}


def run_step(name) {
    try {
        env.IS_STABILITY_CI = is_stability_ci()
        echo "IS_STABILITY_CI: ${env.IS_STABILITY_CI}"
        if (env.IS_STABILITY_CI && env.IS_STABILITY_CI.toBoolean()) {
            print "Stability CI run detected — adding NBU-sw-ci-sonic@exchange.nvidia.com to the mailing list."
            env.ADD_MAILS_TO_MAILING_LIST = "NBU-sw-ci-sonic@exchange.nvidia.com"
        }
        def topic = (GerritTools.get_topic(env.GERRIT_CHANGE_NUMBER)).replace("\"","")
        def topic_map = [:]
        for (_topic in topic.split(",")) {
            if (_topic.contains("=")) {
                topic_map[_topic.split("=")[0].trim()] = _topic.split("=", 2)[1].trim().replace("\"","")
            }
        }

        def project = "sonic"
        if (env.GERRIT_BRANCH.startsWith("dev-") || env.GERRIT_BRANCH.startsWith("dev_") ||env.GERRIT_BRANCH.startsWith("nvos")){
            project = "nvos"
        }
        if (topic_map["RUN_COMMUNITY_REGRESSION"] && topic_map["RUN_COMMUNITY_REGRESSION"].toBoolean() == true) {
            env.RUN_COMMUNITY_REGRESSION = true
        }

        if (topic.contains("SKIP_SPELLCHECK")){
            print "SKIP_SPELLCHECK is activated, spell check will not run"
            env.SKIP_SPELLCHECK = true
        }

        if (topic.contains("SKIP_BEAUTIFIER")){
            print "SKIP_BEAUTIFIER is activated, beautifier will not run"
            env.SKIP_BEAUTIFIER = true
        }

        set_sonic_bin(topic_map, project)
        set_dpu_bin(topic_map)
        set_nvos_bin(topic_map, project)


        //Copy files to external storage
        env.nfs_dir = "/auto/sw_system_project/devops/sw-r2d2-bot/${env.JOB_NAME}/${currentBuild.number}"

        //copy build moduls dir
        if (!fileExists(env.nfs_dir + "/build")) {
            NGCITools().ciTools.run_sh("mkdir -p ${env.nfs_dir}/build/ci")
            NGCITools().ciTools.run_sh("mkdir -p ${env.nfs_dir}/sonic-mgmt")
            NGCITools().ciTools.run_sh("chmod -R 777 ${env.nfs_dir}")
            NGCITools().ciTools.run_sh("mkdir -p ${env.nfs_dir}/LOGS")
            NGCITools().ciTools.run_sh("chmod 777 ${env.nfs_dir}/LOGS")
            print "copying mgmt repo files to " + env."nfs_dir"
            NGCITools().ciTools.run_sh("cp -rf ./. ${env.nfs_dir}/sonic-mgmt/")
            NGCITools().ciTools.run_sh("cp -r build/. ${env.nfs_dir}/build/")
            NGCITools().ciTools.run_sh("cp -r ${WORKSPACE}/general ${env.nfs_dir}/")
            //Copy bat properties from sonic_devops shared location (used by bat.groovy)
            NGCITools().ciTools.run_sh("cp /auto/sw_system_release/ci/sonic_devops/build/ci/bat_properties_file.txt ${env.nfs_dir}/build/ci/")
            NGCITools().ciTools.run_sh("cp /auto/sw_system_release/ci/nos/nvos/build/common/bat_properties_file.txt ${env.nfs_dir}/build/common/")
        }

        //copy sonic_devops build
        NGCITools().ciTools.run_sh("mkdir -p ${env.nfs_dir}/sonic_devops/build")
        NGCITools().ciTools.run_sh("chmod 777 ${env.nfs_dir}/sonic_devops/build")

        def common_properties_file_path = "/auto/sw_system_release/ci/sonic_devops/build/common/build.properties"
        def mars_property = "MARS_RELEASE_VERSION"
        def parameters = readFile(common_properties_file_path)
        def props = new Properties()
        props.load(new StringReader(parameters))
        print "Reading '${mars_property}' from: '${common_properties_file_path}'"
        env.MARS_RELEASE_VERSION = props.getProperty(mars_property)
        print("MARS_RELEASE_VERSION = ${env.MARS_RELEASE_VERSION}")


    } catch (Throwable ex) {
        NGCITools().ciTools.set_error_in_env(ex, "devops", name)
        return false
    }
    return true
}


def cleanup(name) {
    return true
}

def headline(name) {
    if ("${name}".contains(":")) {
        return "${name}".split(":")[0] + " " + env."${name}_status"
    } else {
        return "${name} " + env."${name}_status"
    }
}


def summary(name) {
    if (env."${name}_status" != "Success") {
        return env."${name}_status" + " - exception: " + env."${name}_error"
    } else {
        return env."${name}_status"
    }
}


return this
