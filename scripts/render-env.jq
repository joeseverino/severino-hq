# Render a 1Password item's env-var-shaped fields (UPPER_SNAKE labels) as
# shell-sourceable KEY='value' lines. Single-quote wrapping with embedded
# single quotes escaped, so the container entrypoint can `.` the file safely.
.fields[]
| select(((.label // "") | test("^[A-Z][A-Z0-9_]+$")) and .value != null and .value != "")
| .label + "=" + "'" + (.value | gsub("'"; "'\\''")) + "'"
