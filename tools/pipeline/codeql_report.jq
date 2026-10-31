# Print the CodeQL alerts in a SARIF file and halt non-zero if any survive.
#
#   jq -r -f tools/pipeline/codeql_report.jq SARIF --args RULE...
#
# scan:codeql passes, as the positional arguments, the rules
# .github/codeql/codeql-config.yml excludes, so a local run and the Action
# agree on what counts as a finding.
if (.runs // []) == [] then
  # An analysis that ran writes a run even when it finds nothing.
  "[security] CodeQL wrote no analysis run.\n" | halt_error(1)
else
  [.runs[].results[]? | select(.ruleId | IN($ARGS.positional[]) | not)] as $alerts
  | ($alerts[]
     | (.locations[0].physicalLocation // {}) as $at
     | "  \(.ruleId // "?")  \($at.artifactLocation.uri // "?"):\($at.region.startLine // "?")  \(.message.text // "" | gsub("\n"; " ") | .[:96])"),
    if $alerts == [] then empty
    else "[security] CodeQL found \($alerts | length) alert\(if ($alerts | length) == 1 then "" else "s" end).\n" | halt_error(1)
    end
end
