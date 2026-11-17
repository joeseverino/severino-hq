package host

import (
	"bufio"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// treeBin is where a unit runs a program the image built.
var treeBin = Lib + "/deploy/bin/"

// unitProgram is the name of the program under the tree an ExecStart line
// runs, or "" for a line that runs another.
func unitProgram(exec string) string {
	for field := range strings.FieldsSeq(exec) {
		if name, ok := strings.CutPrefix(strings.TrimLeft(field, "@-:+!|"), treeBin); ok {
			return name
		}
	}
	return ""
}

// dropInsOf is every shipped drop-in systemd reads for unit, in the order it
// reads them (systemd.unit(5)): from the directory of each name made by
// truncating unit's after a dash, a template's, and unit's own, a file in a
// later one replacing one of the same name in an earlier, all sorted by name.
func dropInsOf(unit string, shipped []string) []string {
	base, suffix := unit[:strings.LastIndex(unit, ".")], unit[strings.LastIndex(unit, "."):]
	var dirs []string
	for i := range len(base) {
		if base[i] == '-' {
			dirs = append(dirs, base[:i+1]+suffix+".d")
		}
	}
	if template, _, instance := strings.Cut(base, "@"); instance {
		dirs = append(dirs, template+"@"+suffix+".d")
	}
	dirs = append(dirs, unit+".d")
	byName := map[string]string{}
	for _, dir := range dirs {
		for _, name := range shipped {
			if file, ok := strings.CutPrefix(name, dir+"/"); ok {
				byName[file] = name
			}
		}
	}
	files := slices.Sorted(maps.Keys(byName))
	for i, file := range files {
		files[i] = byName[file]
	}
	return files
}

// notSandbox are the [Service] keys a dry run leaves out: what the unit runs,
// how systemd waits on and restarts it, where its output goes, and the
// directories it owns, which the live unit may hold while the rehearsal runs.
// Every other key is the sandbox, and is given as the unit gives it.
var notSandbox = []string{
	"Type", "ExecCondition", "ExecStartPre", "ExecStart", "ExecStartPost", "ExecReload", "ExecStop", "ExecStopPost",
	"Restart", "RestartSec", "RemainAfterExit", "TimeoutSec", "TimeoutStartSec", "TimeoutStopSec", "RuntimeMaxSec",
	"WatchdogSec", "NotifyAccess", "StandardInput", "StandardOutput", "StandardError",
	"RuntimeDirectory", "RuntimeDirectoryMode", "StateDirectory", "StateDirectoryMode", "CacheDirectory",
	"CacheDirectoryMode", "LogsDirectory", "LogsDirectoryMode", "ConfigurationDirectory", "ConfigurationDirectoryMode",
}

// sandboxProperties is unit's sandbox as systemd-run's properties: every
// [Service] assignment of the unit's file and each drop-in that reaches it,
// in the order systemd reads them. A transient unit applies them in that
// order as a unit file does, an empty one resetting a list, so systemd itself
// resolves what the unit ends with.
func sandboxProperties(dir, unit string, shipped []string) ([]string, error) {
	var properties []string
	for _, name := range append([]string{unit}, dropInsOf(unit, shipped)...) {
		assignments, err := serviceAssignments(filepath.Join(dir, name))
		if err != nil {
			return nil, err
		}
		for _, assignment := range assignments {
			key, _, _ := strings.Cut(assignment, "=")
			if !slices.Contains(notSandbox, key) {
				properties = append(properties, "--property="+assignment)
			}
		}
	}
	return properties, nil
}

// serviceAssignments is each Key=value line of file's [Service] section, in
// order, a line continued with a backslash joined to the next
// (systemd.syntax(7)).
func serviceAssignments(file string) ([]string, error) {
	f, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var assignments []string
	section, pending := "", ""
	lines := bufio.NewScanner(f)
	for lines.Scan() {
		line := strings.TrimSpace(lines.Text())
		if pending != "" {
			line = pending + " " + line
			pending = ""
		}
		if continued, ok := strings.CutSuffix(line, `\`); ok {
			pending = strings.TrimSpace(continued)
			continue
		}
		switch {
		case line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";"):
		case strings.HasPrefix(line, "["):
			section = line
		case section == "[Service]" && strings.Contains(line, "="):
			key, value, _ := strings.Cut(line, "=")
			assignments = append(assignments, strings.TrimSpace(key)+"="+strings.TrimSpace(value))
		}
	}
	return assignments, lines.Err()
}
