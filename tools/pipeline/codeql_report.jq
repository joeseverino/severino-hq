# Print the CodeQL alerts in what an analysis left and halt non-zero if any
# are there. It reads either of the two answers:
#
#   jq -r -f tools/pipeline/codeql_report.jq SARIF
#   jq -r -f tools/pipeline/codeql_report.jq ALERTS
#
# A SARIF file, from scan:codeql, has had .github/codeql/codeql-config.yml's
# query-filters applied, so every result is a finding. ALERTS is GitHub's
# code-scanning alerts answer, from scan:alerts: one page, or the pages
# `gh api --paginate --slurp` gathers. Only an open alert counts there, since a
# dismissal is held on GitHub and never reaches a SARIF file.
def line($rule; $path; $line; $message):
  "  \($rule // "?")  \($path // "?"):\($line // "?")  \($message // "" | gsub("\n"; " ") | .[:96])";

def judge($alerts; $what):
  ($alerts[]),
  if $alerts == [] then empty
  else "[security] CodeQL found \($alerts | length) \($what)\(if ($alerts | length) == 1 then "" else "s" end).\n" | halt_error(1)
  end;

if type == "array" then
  [flatten(1)[] | select(.state == "open")] as $alerts
  | judge([$alerts[]
           | .most_recent_instance as $at
           | line(.rule.id; $at.location.path; $at.location.start_line; $at.message.text)];
          "open alert")
elif (.runs // []) == [] then
  # An analysis that ran writes a run even when it finds nothing.
  "[security] CodeQL wrote no analysis run.\n" | halt_error(1)
else
  judge([.runs[].results[]?
         | (.locations[0].physicalLocation // {}) as $at
         | line(.ruleId; $at.artifactLocation.uri; $at.region.startLine; .message.text)];
        "alert")
end
