package host

import (
	"bytes"
	"cmp"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
)

// Verify reports where the host differs from the release HQ runs: the tree
// against that image, file for file and byte for byte; the installed units
// against the tree's; anything a release no longer installs; and the Actions
// runner's sudo rules against the ones HQ renders. It changes
// nothing. An empty answer is a host that is what its release says.
func (m *Machine) Verify(ctx context.Context) ([]string, error) {
	out, err := m.Command(ctx, "docker", "inspect", "--format", "{{.Image}}", Container).Output()
	if err != nil {
		return nil, errors.New("could not read the image HQ runs")
	}
	running := strings.TrimSpace(string(out))
	var drift []string

	source, _ := os.ReadFile(filepath.Join(m.Lib, ".source"))
	if recorded := strings.TrimPrefix(strings.TrimSpace(string(source)), "image "); recorded != running {
		drift = append(drift, fmt.Sprintf("the tree came from %q; HQ runs %s", recorded, running))
	}

	stage, err := os.MkdirTemp(m.Runtime, "severino-hq-verify.")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(stage)
	if err := m.extract(ctx, running, stage); err != nil {
		return nil, err
	}
	differs, err := compareTrees(stage, m.Lib)
	if err != nil {
		return nil, err
	}
	drift = append(drift, differs...)

	shipped, err := Shipped(m.UnitDir())
	if err != nil {
		return nil, err
	}
	for _, name := range shipped {
		want, _ := os.ReadFile(filepath.Join(m.UnitDir(), name))
		have, err := os.ReadFile(filepath.Join(m.Systemd, name))
		if err != nil || !bytes.Equal(want, have) {
			drift = append(drift, filepath.Join(m.Systemd, name)+" is not the release's")
		}
	}
	retired, err := Retired(shipped, m.Systemd)
	if err != nil {
		return nil, err
	}
	for _, name := range retired {
		drift = append(drift, filepath.Join(m.Systemd, name)+" is not shipped by the release")
	}
	leftovers, _ := filepath.Glob(filepath.Join(m.Sbin, "severino-hq-*"))
	for _, leftover := range leftovers {
		drift = append(drift, leftover+" is installed by no release")
	}
	return append(drift, m.runnerRules(ctx)...), nil
}

var unrestricted = regexp.MustCompile(`(?m)(^|[\s:])ALL\s*$`)

// runnerRules: the Actions runner runs as its own account, which deploys
// through one sudo rule and may run nothing else through sudo that HQ does
// not render. Only root can list another account's rules without a password.
func (m *Machine) runnerRules(ctx context.Context) []string {
	output := func(name string, args ...string) (string, error) {
		out, err := m.Command(ctx, name, args...).Output()
		return strings.TrimSpace(string(out)), err
	}
	listed, _ := output("systemctl", "list-units", "--all", "--plain", "--no-legend", "actions.runner.*.service")
	var units []string
	for line := range strings.SplitSeq(listed, "\n") {
		if fields := strings.Fields(line); len(fields) > 0 {
			units = append(units, fields[0])
		}
	}
	if len(units) != 1 {
		return []string{"expected one Actions runner unit, found: " + cmp.Or(strings.Join(units, " "), "none")}
	}
	user, _ := output("systemctl", "show", "-p", "User", "--value", units[0])
	if user == "" || user == "root" {
		return []string{fmt.Sprintf("the Actions runner runs as '%s'; it must run as the deploy account", cmp.Or(user, "root"))}
	}
	rules, err := output("sudo", "-n", "-l", "-U", user)
	if err != nil {
		return []string{fmt.Sprintf("could not list %s's sudo rules: %s", user, refusal(err))}
	}
	program := filepath.Join(m.Lib, hostRelative)
	deploy := program + " deploy *"
	var drift []string
	switch {
	case unrestricted.MatchString(rules):
		drift = append(drift, user+" may run any command through sudo")
	case !regexp.MustCompile(`(?m)NOPASSWD:(.*,)?\s*` + regexp.QuoteMeta(deploy) + `\s*(,|$)`).MatchString(rules):
		drift = append(drift, fmt.Sprintf("%s has no NOPASSWD sudo rule for %s", user, deploy))
	}
	return append(drift, onlyRendered(user, rules, program)...)
}

// refusal is why a command run through Output failed: what it wrote to
// stderr, which Output keeps on the error, or the error itself.
func refusal(err error) string {
	if exited, ok := errors.AsType[*exec.ExitError](err); ok {
		if said := strings.TrimSpace(string(exited.Stderr)); said != "" {
			return said
		}
	}
	return err.Error()
}

// onlyRendered: every command sudo lists for the runner is one HQ renders a
// rule for. sudo-rs resolves the aliases, groups and includes, and names no
// rule's source, so a failure says how to find it.
func onlyRendered(user, rules, program string) []string {
	rendered := []string{program + " deploy *", program + " upgrade *"}
	commands, found := listedCommands(rules)
	if !found {
		return []string{"sudo's listing of " + user + "'s rules has no list of commands"}
	}
	var drift []string
	for _, command := range commands {
		if !slices.Contains(rendered, command) {
			drift = append(drift, fmt.Sprintf("%s may run %s through sudo, which HQ does not render; 'sudo grep -rn %s /etc/sudoers /etc/sudoers.d' finds the rule", user, command, user))
		}
	}
	return drift
}

var sudoTags = regexp.MustCompile(`^(?:[A-Z_]+:\s*|[A-Z_]+=\S+\s+)*`)

// listedCommands are the commands `sudo -l -U` lists, each with its spacing
// collapsed: an entry is `(runas) TAGS: command, command`, aliases expanded.
func listedCommands(rules string) (commands []string, found bool) {
	_, entries, found := strings.Cut(rules, "may run the following commands")
	if !found {
		return nil, false
	}
	_, entries, _ = strings.Cut(entries, "\n")
	for line := range strings.SplitSeq(entries, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "(") {
			_, line, _ = strings.Cut(line, ")")
		}
		for command := range strings.SplitSeq(line, ", ") {
			if command = strings.Join(strings.Fields(sudoTags.ReplaceAllString(strings.TrimSpace(command), "")), " "); command != "" {
				commands = append(commands, command)
			}
		}
	}
	return commands, true
}

// compareTrees names every file that is in one tree and not the other, or
// in both with different bytes. The tree's own provenance files are not the
// image's and are not compared.
func compareTrees(want, have string) ([]string, error) {
	wanted, err := files(want)
	if err != nil {
		return nil, err
	}
	had, err := files(have)
	if err != nil {
		return nil, err
	}
	var drift []string
	for name, data := range wanted {
		switch held, ok := had[name]; {
		case !ok:
			drift = append(drift, name+" is missing from the tree")
		case !bytes.Equal(data, held):
			drift = append(drift, name+" differs from the image's")
		}
	}
	for name := range had {
		if _, ok := wanted[name]; !ok && name != ".source" && name != ".synced-at" {
			drift = append(drift, name+" is not the image's")
		}
	}
	slices.Sort(drift)
	return drift, nil
}

func files(dir string) (map[string][]byte, error) {
	found := map[string][]byte{}
	err := filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		found[strings.TrimPrefix(path, dir+"/")] = data
		return nil
	})
	return found, err
}
