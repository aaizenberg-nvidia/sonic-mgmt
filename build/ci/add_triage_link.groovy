// Adds the failure triage link banner to the final mail. Must be a "finally": true stage so
// it runs before the NGCI mail is sent; it is a no-op for successful builds.

def pre(name) {
    return true
}

String escapeHtml(String value) {
    if (value == null) {
        return ""
    }
    return value.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;')
}

// Triage page where the owner can comment on why a build failed.
// build_id is the full Jenkins build URL (e.g. https://.../job/sonic_mgmt_ci/1234).
def get_triage_link_banner_html() {
    if (!env.BUILD_URL) {
        return ""
    }
    def triage_url = "https://da-analytics.mec01-asgard.nvidia.com/bd-comment-service?build_id=${env.BUILD_URL}"
    return """
<table class="section" cellspacing="20">
    <tr class="tr-title"><td class="td-title-main">CI Failure Triage</td></tr>
    <tr><td style="padding: 10px;">
        This build failed. Please <a href="${escapeHtml(triage_url)}">open the triage page</a> to comment on why it failed.
    </td></tr>
</table>
<br/>
"""
}

def run_step(name) {
    try {
        if (currentBuild.currentResult == "SUCCESS") {
            print "Build did not fail - no triage link needed."
            return true
        }
        def triage_html = get_triage_link_banner_html()
        if (triage_html) {
            NGCITools().ciTools.add_on_the_fly_head_banner_to_mail(triage_html)
        }
        return true
    }
    catch (Throwable exc) {
        NGCITools().ciTools.set_error_in_env(exc, "devops", name)
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
    if (env."${name}_status" == "Failure") {
        return "exception: " + env."${name}_status"
    } else {
        return env."${name}_status"
    }
}

return this
