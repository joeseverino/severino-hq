# Print the CodeQL alerts in a SARIF file and halt non-zero if any are there.
#
#   jq -r -f tools/pipeline/codeql_report.jq SARIF
#
# The analysis has already applied .github/codeql/codeql-config.yml's
# query-filters, so every result is a finding.
if (.runs // []) == [] then
  # An analysis that ran writes a run even when it finds nothing.
  "[security] CodeQL wrote no analysis run.\n" | halt_error(1)
else
  [.runs[].results[]?] as $alerts
  | ($alerts[]
     | (.locations[0].physicalLocation // {}) as $at
     | "  \(.ruleId // "?")  \($at.artifactLocation.uri // "?"):\($at.region.startLine // "?")  \(.message.text // "" | gsub("\n"; " ") | .[:96])"),
    if $alerts == [] then empty
    else "[security] CodeQL found \($alerts | length) alert\(if ($alerts | length) == 1 then "" else "s" end).\n" | halt_error(1)
    end
end
