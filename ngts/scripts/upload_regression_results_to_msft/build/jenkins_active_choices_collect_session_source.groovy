// Paste into Jenkins job parameter "collect_session_source"
//
// Parameter type: Active Choices Reactive Parameter (CascadeChoiceParameter)
//   Referenced parameters: command
//   Choice Type: Radio (PT_RADIO)
//
// Place AFTER "command" and BEFORE "parameters".
//
// Then set "parameters" Referenced parameters to:
//   command,collect_session_source
//
// Jenkins "Description" for this parameter (paste into the Description field):
//   Only used when command=collect. Pick user_sessions (default) or last_days.
//   For modify/compare/export/save/concat this control is unused — ignore the empty row.
//
// (UnoChoice always shows the parameter name; it cannot be hidden for other commands.)

if (command == "collect") {
  return ["user_sessions:selected", "last_days"]
}
return []
