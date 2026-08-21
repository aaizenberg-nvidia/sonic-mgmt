// Paste into Jenkins job parameter "parameters" (Active Choices Dynamic Reference /
// Reactive Reference, ET_FORMATTED_HTML).
//
// REQUIRED setup:
//   1. "collect_session_source" = Active Choices Reactive Parameter
//        Referenced parameters: command
//        Choice Type: Radio
//        Description: see jenkins_active_choices_collect_session_source.groovy header
//        Groovy: jenkins_active_choices_collect_session_source.groovy
//   2. THIS "parameters" Referenced parameters:
//        command,collect_session_source
//
// Do NOT use local "def method()" helpers — Script Security sandbox often rejects them.
// Avoid "def var =" inside .each too; use plain assignment.
//
// collect + user_sessions (default): user_sessions text only (filter_* not shown)
// collect + last_days: last_days + filter_sessions_started_by + filter_platforms
//
// generate_upload_mail: ONLY shown when command=modify (not collect/compare/export/...).
// No separate Jenkins parameter. Jenkins strips <script>; CSS :has() hides redmine when
// generate_upload_mail is checked. If :has() is ignored, redmine may still show —
// the upload script ignores redmine when generate_upload_mail is set.
//
// To add a platform: append one entry to platform_list only.
// Uncheck filter_platforms to use script default MSFT_PLATFORMS (4600C 4700 2700).

collectSessionSource = "user_sessions"
try {
  css = "${collect_session_source}".toLowerCase()
  if (css.contains("last_days")) {
    collectSessionSource = "last_days"
  }
} catch (Exception e) {
  collectSessionSource = "user_sessions"
}

parameters_command_map = [
  "collect_last_days": [
    ["param_name": "last_days", "placeholder": "Query for all sessions made in the last X days", "param_type": "checkbox", "checked": "checked"],
    ["param_name": "filter_sessions_started_by", "placeholder": "by default has a value of mars official regression runs, <br>to filter other session use --filter_sessions_started_by nhe slutati", "param_type": "checkbox", "checked": ""],
  ],
  "collect_user_sessions": [
    ["param_name": "user_sessions", "placeholder": "Specific sessions provided by user i.e, 8069827 8254249 8254248.<br>Platform / started_by filters are not used.", "text_only": true],
  ],
  "modify": [
    ["param_name": "user_excel_table_path", "placeholder": "Absolute path under release_results/&lt;sonic_version&gt;/..., e.g.<br>/auto/sw_regression/system/SONIC/release_results/202605_RC.70-2e2faf00ae_Internal/collected_results_....xlsx<br>(sonic_version is extracted from this path). Format: Microsoft Excel 97-2003 (.xls) or .xlsx.<br>To remove results: add <strong>REMOVE_FAILED_TESTCASE</strong> in modify_xml column.", "param_type": "checkbox", "checked": "checked"],
    // Default unchecked. flag_only = checkbox with no text box.
    ["param_name": "generate_upload_mail", "placeholder": "<strong>Modify only.</strong> When checked, after modify finishes the job triggers <strong>regression upload mail</strong> via trigger_from_jenkins() (skip.json is also created).<br>When checked, <strong>redmine_issues_to_update_path</strong> is hidden and not used.", "param_type": "checkbox", "checked": "", "flag_only": true],
    ["param_name": "redmine_issues_to_update_path", "placeholder": "path to a JSON file with redmine issues that should be converted to other message in case they are in use in skips, <br>JSON file should be in the format of {RM_issue:new_message}. Not used when generate_upload_mail is checked.", "param_type": "checkbox", "checked": ""],
  ],
  "compare": [
    ["param_name": "compare_excel", "placeholder": "path to an Excel table with results to exported, the format must be Microsoft Excel 97-2003 Worksheet (file ends with .xls)", "param_type": "checkbox", "checked": "checked"],
    ["param_name": "msft_results_path", "placeholder": "Path to a directory containing results from MSFT database.", "param_type": "checkbox", "checked": "checked"]
  ],
  "export": [
    ["param_name": "export_excel", "placeholder": "path to an Excel table with results to exported, the format must be Microsoft Excel 97-2003 Worksheet (file ends with .xls)", "param_type": "checkbox", "checked": "checked"]
  ],
  "save": [
    ["param_name": "sessions_to_save", "placeholder": "Specific sessions provided by user i.e, 8069827 8254249 8254248. <be> the sessions will be saved to the MARS database.", "param_type": "checkbox", "checked": "checked"]
  ],
  "concat": [
    ["param_name": "--excel_tables", "placeholder": "Specific excel tables path by user i.e, path1 path2 path3 etc...", "param_type": "checkbox", "checked": "checked"]
  ],
]

if (command == "collect") {
  if (collectSessionSource == "user_sessions") {
    params_list = parameters_command_map["collect_user_sessions"]
  } else {
    params_list = parameters_command_map["collect_last_days"]
  }
} else {
  params_list = parameters_command_map[command]
}

// CSS (not JS): hide redmine row when generate_upload_mail is checked.
modify_hide_css = ""
if (command == "modify") {
  modify_hide_css = """
<style type=\"text/css\">
table.ac-modify-params:has(#generate_upload_mail:checked) #redmine_issues_to_update_path_row {
  display: none !important;
}
</style>
"""
  html_prefix = "<table class=\"ac-modify-params\">"
} else if (command == "export") {
  html_prefix = """
    <table>
    <tr>
    <td>
    <input name=\"value\" alt=\"checkbox-name\" json=\"checkbox-name\" type=\"checkbox\" class=\" \">
    <label title=\"check_box_approval\" class=\" \">  All the results in the excel were approved by Dror Prital and Liat Grozovik</label>
    </td>
    </tr>
"""
} else {
  html_prefix = "<table>"
}

html_to_be_rendered = ""
params_list.each { param ->
  row_id_attr = (param.param_name == "redmine_issues_to_update_path") ? " id=\"redmine_issues_to_update_path_row\"" : ""
  if (param.text_only) {
    html_to_be_rendered = """
    ${html_to_be_rendered}
    <tr>
    <td>
    <input name=\"value\" type=\"hidden\" value=\"${param.param_name}\">
    <label title=\"${param.param_name}\" class=\" \"><strong>${param.param_name}</strong><BR>${param.placeholder}</label>
    </td>
    <td>
    <input type=\"text\" class=\"setting-input\" name=\"value\" size=\"50\" style=\"width: 1000px;\"> </br>
    </td>
    </tr>
"""
  } else if (param.flag_only) {
    html_to_be_rendered = """
    ${html_to_be_rendered}
    <tr${row_id_attr}>
    <td>
    <input name=\"value\" alt=\"${param.param_name}\" json=\"${param.param_name}\" value=\"${param.param_name}\" id=\"${param.param_name}\" type=\"${param.param_type}\" class=\"  \"  ${param.checked}>
    <label title=\"${param.param_name}\" class=\" \"><strong>${param.param_name}</strong><BR>${param.placeholder}</label>
    </td>
    <td></td>
    </tr>
"""
  } else {
    html_to_be_rendered = """
    ${html_to_be_rendered}
    <tr${row_id_attr}>
    <td>
    <input name=\"value\" alt=\"${param.param_name}\" json=\"${param.param_name}\" value=\"${param.param_name}\" id=\"${param.param_name}\" type=\"${param.param_type}\" class=\"  \"  ${param.checked}>
    <label title=\"${param.param_name}\" class=\" \"><strong>${param.param_name}</strong><BR>${param.placeholder}</label>
    </td>
    <td>
    <input type=\"text\" class=\"setting-input\" name=\"value\" size=\"50\" style=\"width: 1000px;\"> </br>
    </td>
    </tr>
"""
  }
}

platform_list = [
  ["platform_id": "2010", "platform_internal_name": "(boxer)", "checked": ""],
  ["platform_id": "2100", "platform_internal_name": "(bulldog)", "checked": ""],
  ["platform_id": "2410", "platform_internal_name": "(spider)", "checked": ""],
  ["platform_id": "2700", "platform_internal_name": "(panther)", "checked": "checked"],
  ["platform_id": "3420", "platform_internal_name": "(lionfish)", "checked": ""],
  ["platform_id": "3700", "platform_internal_name": "(anaconda)", "checked": ""],
  ["platform_id": "3800", "platform_internal_name": "(tigris)", "checked": ""],
  ["platform_id": "4280", "platform_internal_name": "(bobcat)", "checked": "checked"],
  ["platform_id": "4410", "platform_internal_name": "(ocelot)", "checked": ""],
  ["platform_id": "4600", "platform_internal_name": "(liger)", "checked": ""],
  ["platform_id": "4600C", "platform_internal_name": "(tigon)", "checked": "checked"],
  ["platform_id": "4700", "platform_internal_name": "(leopard)", "checked": "checked"],
  ["platform_id": "5400", "platform_internal_name": "(hippo)", "checked": ""],
  ["platform_id": "5600", "platform_internal_name": "(moose)", "checked": "checked"],
  ["platform_id": "5610", "platform_internal_name": "(gaur)", "checked": "checked"],
  ["platform_id": "5640", "platform_internal_name": "(bison)", "checked": "checked"],
  ["platform_id": "6600_LD", "platform_internal_name": "(Salamandra)", "checked": "checked"],
  ["platform_id": "BMC", "platform_internal_name": "(bmc)", "checked": "checked"],
]

platform_order = platform_list.collect { it.platform_id }.join(" ")
platform_checkbox_to_be_rendered = """
 <tr>
<td>
    <input name=\"value\" type=\"hidden\" value=\"__PLATFORM_ORDER__:${platform_order}\">
    <input name=\"value\" alt=\"filter_platforms\" json=\"filter_platforms\" value=\"filter_platforms\" id=\"filter_platforms\" type=\"checkbox\" class=\"  \" checked>
    <label title=\"filter_platforms\" class=\" \"><strong>filter_platforms</strong><BR>Uncheck to use script default MSFT platforms: 4600C 4700 2700</label>
    </td>
<td>
"""

platform_list.each { platform ->
  platform_checkbox_to_be_rendered = """
    ${platform_checkbox_to_be_rendered}
    <input name=\"value\" alt=\"${platform.platform_id}\" json=\"${platform.platform_id}\" value=\"${platform.platform_id}\" id=\"${platform.platform_id}\" type=\"checkbox\" class=\"  \"  ${platform.checked}>
    <label title=\"${platform.platform_id}\" class=\" \"><strong>${platform.platform_id}</strong> ${platform.platform_internal_name}</label><br>
"""
}
platform_checkbox_to_be_rendered = "${platform_checkbox_to_be_rendered}</td></tr>"

if (command == "collect" && collectSessionSource == "last_days") {
  return "${html_prefix}${html_to_be_rendered}${platform_checkbox_to_be_rendered}</table>"
}
return "${modify_hide_css}${html_prefix}${html_to_be_rendered}</table>"
