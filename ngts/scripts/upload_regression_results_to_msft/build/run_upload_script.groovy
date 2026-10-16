def email_message(file_path, artifact_name) {
    echo "Archive email table from ${file_path}"
    archiveArtifacts artifacts: artifact_name, allowEmptyArchive: false
    def table_content = readFile(file: file_path)
    def artifact_page = env.BUILD_URL ? "${env.BUILD_URL}artifact/" : ""
    def artifact_banner = artifact_page ?
        "<p>Full upload tables are available in the <a href=\"${artifact_page}\">Jenkins build artifacts</a>.</p>" :
        "<p>Full upload tables are available as the ${artifact_name} build artifact.</p>"
    env.on_the_fly_head_banners = table_content + artifact_banner
}

def build_container_name(base_name) {
    def suffix = (env.BUILD_TAG ?: env.BUILD_NUMBER ?: "manual")
        .replaceAll("[^A-Za-z0-9_.-]", "-")
    return "${base_name}-${suffix}".take(200)
}


def pre(name) {
    echo "PRE:: Run upload_regression_results_to_msft.py"
    return true
}
def validate_params(params, command) {
    if (command == "export") {
        if (!params.contains("true,true,")){
            throw new RuntimeException("User did not mark approval box, aborting export")
        }
    }
    if (command == "modify") {
        if (!params.matches('^true,[^,]+,.*$')){
            throw new RuntimeException("User did not mark approval box or specified the excel file, aborting modify")
        }
    }
}

def verify_sonic_version(sonic_version) {
    if (!sonic_version) {
        throw new RuntimeException("sonic_version is required but not provided")
    }
}

def verify_mail_addresses(value, name, required) {
    addresses = value ? value.split(',').collect { it.trim() }.findAll { it } : []
    if (required && addresses.isEmpty()) {
        throw new RuntimeException("${name} is required")
    }
    if (!addresses.every { it ==~ /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/ }) {
        throw new RuntimeException("${name} contains an invalid email address")
    }
}

def verify_github_authors(value) {
    authors = value ? value.split(',').collect { it.trim() }.findAll { it } : []
    if (authors.isEmpty()) {
        throw new RuntimeException("regression_mail_github_authors is required")
    }
    if (!authors.every { it ==~ /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/ }) {
        throw new RuntimeException("regression_mail_github_authors contains an invalid GitHub login")
    }
}

def validate_env_file_value(name, value) {
    def text = value == null ? "" : value.toString()
    if (text.contains("\n") || text.contains("\r") || text.contains("\u0000")) {
        throw new RuntimeException("${name} contains a character unsupported by Docker env-file")
    }
    return text
}

def write_report_env_file(values) {
    def fileName = ".regression-mail-env-${env.BUILD_NUMBER}.list"
    def lines = values.findAll { key, value -> value != null }.collect {
        key, value -> "${key}=${validate_env_file_value(key, value)}"
    }
    writeFile file: fileName, text: lines.join("\n") + "\n", encoding: "UTF-8"
    NGCITools().ciTools.run_sh("chmod 600 '${fileName}'")
    return [fileName, "${pwd()}/${fileName}"]
}

def echo_params(sonic_version, command, git_repository, branch, params, container_name) {
    echo "Jenkins Job Parameters"
    echo "sonic_version ${sonic_version}"
    echo "command ${command}"
    echo "git_repository ${git_repository}"
    echo "branch ${branch}"
    echo "script additional parameters ${params}"
    echo "container_name ${container_name}"
}

def run_step(name) {
    try {
          echo "Run upload_regression_results_to_msft.py script on ngts sonic-mgmt docker"
          // Active Choices Formatted HTML often appends a trailing comma
          sonic_version = (env."${name}_sonic_version" ?: "").replaceAll(/,+$/, "").trim()
          command = env."${name}_command"
          git_repository = env."${name}_git_repository"
          branch = env."${name}_branch"
          params = env."${name}_parameters"
          if (!sonic_version && command == "modify") {
            def version_match = params =~ /(?:SONiC\.)?(\d{6}_RC\.\d+-[A-Za-z0-9]+_Internal)/
            if (version_match.find()) {
              sonic_version = version_match.group(1)
              echo "modify: extracted sonic_version=${sonic_version} from user_excel_table_path"
            }
          }
          verify_sonic_version(sonic_version)
          container_name = build_container_name(
              env."${name}_container_name"
          )
          regression_mail_to = (env.regression_mail_to ?: "").trim()
          regression_mail_cc = env."${name}_regression_mail_cc" ?: env.REGRESSION_MAIL_CC
          regression_mail_github_authors = env.regression_mail_github_authors?.trim() ?:
              env."${name}_regression_mail_github_authors" ?:
              env.REGRESSION_MAIL_GITHUB_AUTHORS
          def report_settings = [
            smtp_host: env."${name}_regression_mail_smtp_host" ?: env.REGRESSION_MAIL_SMTP_HOST,
            smtp_port: env."${name}_regression_mail_smtp_port" ?: env.REGRESSION_MAIL_SMTP_PORT,
            sender: env."${name}_regression_mail_sender" ?: env.REGRESSION_MAIL_SENDER,
            api_base_url: env."${name}_regression_mail_api_base_url" ?: env.REGRESSION_MAIL_API_BASE_URL,
            http_timeout: env."${name}_regression_mail_http_timeout" ?: env.REGRESSION_MAIL_HTTP_TIMEOUT,
            rc_status_repository: env."${name}_regression_mail_rc_status_repository" ?: env.REGRESSION_MAIL_RC_STATUS_REPOSITORY,
            rc_status_ref: env."${name}_regression_mail_rc_status_ref" != null ?
                env."${name}_regression_mail_rc_status_ref" : env.REGRESSION_MAIL_RC_STATUS_REF,
            rc_status_tag: env."${name}_regression_mail_rc_status_tag" != null ?
                env."${name}_regression_mail_rc_status_tag" : env.REGRESSION_MAIL_RC_STATUS_TAG,
            github_api_token: env.GITHUB_API_TOKEN,
            redmine_api_token: env.REDMINE_API_TOKEN,
            redmine_author_id: env."${name}_regression_mail_redmine_author_id" ?: env.REGRESSION_MAIL_REDMINE_AUTHOR_ID,
            gerrit_username: env.SVC_SONIC_VER_BOT,
            gerrit_api_key: env.GERRIT_API_KEY,
            inference_api_key: env.INFERENCE_API_KEY,
            inference_base_url: env."${name}_regression_mail_inference_base_url" ?: env.REGRESSION_MAIL_INFERENCE_BASE_URL,
            inference_model: env."${name}_regression_mail_inference_model" ?: env.REGRESSION_MAIL_INFERENCE_MODEL,
            inference_batch_size: env."${name}_regression_mail_inference_batch_size" ?: env.REGRESSION_MAIL_INFERENCE_BATCH_SIZE,
            inference_timeout: env."${name}_regression_mail_inference_timeout" ?: env.REGRESSION_MAIL_INFERENCE_TIMEOUT
          ]
          modify_params = command == "modify" ? params.split(",", -1) : [] as String[]
          report_requested = command == "collect" ||
              (command == "modify" && modify_params.size() > 2 &&
               modify_params[2].trim().equalsIgnoreCase("true"))
          if (report_requested && !regression_mail_to) {
            def causes = currentBuild.getBuildCauses(
                "hudson.model.Cause\$UserIdCause"
            )
            if (causes && causes[0].userId) {
              def trigger_user = causes[0].userId.toString().trim()
              regression_mail_to = trigger_user.contains("@") ?
                  trigger_user : "${trigger_user}@nvidia.com"
            }
          }
          if (report_requested) {
            echo "${command} regression mail: recipients=${regression_mail_to}"
            verify_mail_addresses(regression_mail_to, "regression_mail_to", true)
            verify_mail_addresses(regression_mail_cc, "regression_mail_cc", false)
            verify_github_authors(regression_mail_github_authors)
          }
          echo_params(sonic_version, command, git_repository, branch, params, container_name)
          script_dir = "/root/mars/workspace/sonic-mgmt/ngts/scripts/upload_regression_results_to_msft/"
          script_name = "upload_regression_results_to_msft.py"
          script_path = "${script_dir}${script_name}"
          build_table_name = "build_mail_table.txt"
          script_params = ["--sonic_version ${sonic_version}", "--git_repository ${git_repository}", "--branch ${branch}", "${command}"]
          validate_params(params, command)
          dir("ngts/scripts/upload_regression_results_to_msft"){
            parsed_params = NGCITools().ciTools.run_sh_return_output("python ./parse_script_params.py --params \"${params}\" --command ${command}")
          }
          echo "parsed script additional params ${parsed_params}"
          script_params.add(parsed_params)
          script_params_str = script_params.join(' ')
          pythonpath = "PYTHONPATH=/root/mars/workspace/sonic-mgmt/:/devts/"
          python_int = "/ngts_venv/bin/python"
          if (report_requested) {
            requirements_path = "/root/mars/workspace/sonic-mgmt/ngts/scripts/regression_mail/requirements.txt"
            install_cmd = "sudo docker exec ${container_name} bash -c \"${python_int} -m pip install --disable-pip-version-check -r ${requirements_path}\""
            echo "Install regression mail Python dependencies"
            NGCITools().ciTools.run_sh("${install_cmd}")
          }
          script_cmd = "${pythonpath} ${python_int} ${script_path} ${script_params_str}"
          echo "script_cmd ${script_cmd}"
          def report_env_file = null
          def report_env_file_path = null
          try {
            def report_values = [:]
            if (report_settings.redmine_api_token) {
              report_values.REDMINE_API_TOKEN = report_settings.redmine_api_token
            }
            if (report_requested) {
              report_values.REGRESSION_MAIL_ENABLED = "true"
              report_values.REGRESSION_MAIL_TO = regression_mail_to
              report_values.REGRESSION_MAIL_CC = regression_mail_cc
              report_values.REGRESSION_MAIL_GITHUB_AUTHORS = regression_mail_github_authors
              report_values.INFERENCE_API_KEY = report_settings.inference_api_key
              report_values.REGRESSION_MAIL_RC_STATUS_REPOSITORY = report_settings.rc_status_repository
              report_values.REGRESSION_MAIL_RC_STATUS_REF = report_settings.rc_status_ref
              report_values.REGRESSION_MAIL_RC_STATUS_TAG = report_settings.rc_status_tag
              if (report_settings.github_api_token) {
                report_values.GITHUB_API_TOKEN = report_settings.github_api_token
              }
              if (report_settings.gerrit_username) {
                report_values.SVC_SONIC_VER_BOT = report_settings.gerrit_username
              }
              if (report_settings.gerrit_api_key) {
                report_values.GERRIT_API_KEY = report_settings.gerrit_api_key
              }
              report_values.REGRESSION_MAIL_REDMINE_AUTHOR_ID = report_settings.redmine_author_id
              report_values.REGRESSION_MAIL_SMTP_HOST = report_settings.smtp_host
              report_values.REGRESSION_MAIL_SMTP_PORT = report_settings.smtp_port
              report_values.REGRESSION_MAIL_SENDER = report_settings.sender
              report_values.REGRESSION_MAIL_API_BASE_URL = report_settings.api_base_url
              report_values.REGRESSION_MAIL_HTTP_TIMEOUT = report_settings.http_timeout
              report_values.REGRESSION_MAIL_INFERENCE_BASE_URL = report_settings.inference_base_url
              report_values.REGRESSION_MAIL_INFERENCE_MODEL = report_settings.inference_model
              report_values.REGRESSION_MAIL_INFERENCE_BATCH_SIZE = report_settings.inference_batch_size
              report_values.REGRESSION_MAIL_INFERENCE_TIMEOUT = report_settings.inference_timeout
            }
            def report_env_arg = ""
            if (!report_values.isEmpty()) {
              def report_env_file_result = write_report_env_file(report_values)
              report_env_file = report_env_file_result[0]
              report_env_file_path = report_env_file_result[1]
              report_env_arg = " --env-file '${report_env_file_path}'"
            }
            def docker_cmd = "sudo docker exec --env-file /tmp/${container_name}_env_file.sh${report_env_arg} ${container_name} bash -c \"cd /root/ && ${script_cmd}\""
            echo "docker_cmd uses protected env-file; secret values are not placed on the command line"
            NGCITools().ciTools.run_sh("${docker_cmd}")
          } finally {
            if (report_env_file) {
              NGCITools().ciTools.run_sh("rm -f '${report_env_file}'")
            }
            NGCITools().ciTools.run_sh(
              "rm -f '/tmp/${container_name}_env_file.sh'"
            )
          }
          echo "Copy build table from docker"
          currentDir = NGCITools().ciTools.run_sh_return_output("sudo pwd")
          echo "currentDir ${currentDir}"
          path = "${currentDir}/${build_table_name}"
          NGCITools().ciTools.run_sh("sudo docker cp ${container_name}:${script_dir}${build_table_name} ${path}")
          email_message(path, build_table_name)
          return true
      }
    catch (Throwable exc) {
        NGCITools().ciTools.set_error_in_env(exc, "user", name)
        return false
    }
}

def cleanup(name) {
    def container_name = build_container_name(
        env."${name}_container_name"
    )
    NGCITools().ciTools.run_sh(
        "rm -f '/tmp/${container_name}_env_file.sh'"
    )
    NGCITools().ciTools.run_sh(
        "sudo docker rm -f '${container_name}' >/dev/null 2>&1 || true"
    )
    return true
}

def headline(name) {
    return "${name} " + env."${name}_status"
}

def summary(name) {
    if (env."${name}_status" != "Success") {
        return env."${name}_status" + " - exception: " + env."${name}_error"
    } else {
        return env."${name}_status"
    }
}

return this
