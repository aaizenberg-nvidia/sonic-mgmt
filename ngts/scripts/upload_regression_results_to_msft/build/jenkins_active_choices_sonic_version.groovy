// Paste into Jenkins job parameter "sonic_version"
//
// Replace the plain String Parameter with:
//   Active Choices Reactive Reference Parameter
//   Name: sonic_version   (MUST be set — empty name breaks build_config ${sonic_version})
//   Referenced parameters: command
//   Choice Type: Formatted HTML (ET_FORMATTED_HTML)
//
// REQUIRED order (UnoChoice only re-renders Reactive params that appear AFTER
// the referenced parameter). Put sonic_version immediately AFTER "command":
//   git_repository
//   branch
//   command
//   sonic_version          <-- here (not at page top)
//   collect_session_source
//   parameters
//
// If sonic_version is above "command", selecting modify will NOT replace the
// text input with the read-only note.
//
// Jenkins "Description" for this parameter (paste into the Description field):
//   see jenkins_sonic_version_description.html
//

cmd = "${command}"
if (cmd == "modify") {
  return """
<em>Not required for <strong>modify</strong>.<br>
<code>sonic_version</code> is taken from <strong>user_excel_table_path</strong>:<br>
<code>.../release_results/&lt;sonic_version&gt;/&lt;excel_file&gt;</code>
</em>
"""
}

return """
<input name=\"value\" type=\"text\" class=\"setting-input\" style=\"width: 1000px;\" placeholder=\"e.g. 202605_RC.70-2e2faf00ae_Internal\">
"""
