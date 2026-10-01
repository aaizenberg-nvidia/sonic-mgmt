def pre(name) {
    return true
}

def build_container_name(base_name) {
    def suffix = (env.BUILD_TAG ?: env.BUILD_NUMBER ?: "manual")
        .replaceAll("[^A-Za-z0-9_.-]", "-")
    return "${base_name}-${suffix}".take(200)
}

def run_step(name) {
    try {
          echo "Start sonic mgmt ngts docker container"
          ngts_version = env."${name}_ngts_version"
          container_name = build_container_name(
              env."${name}_container_name"
          )
          dir("ngts/scripts/upload_regression_results_to_msft"){
              NGCITools().ciTools.run_sh("python ./start_sonic_mgmt_ngts_docker.py --ngts_version ${ngts_version} --container_name ${container_name}")
          }
          return true
      }
    catch (Throwable exc) {
        NGCITools().ciTools.set_error_in_env(exc, "user", name)
        return false
    }
}

def cleanup(name) {
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
