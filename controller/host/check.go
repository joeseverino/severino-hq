package host

import (
	"cmp"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"syscall"
)

// What a check of the deploy host exits with: the host is ready for this
// release, something on it is wrong, or nothing was found wrong but whether
// it is the release it runs could not be read.
const (
	CheckReady      = 0
	CheckFailed     = 1
	CheckUnverified = 3
)

// DriftUnit runs `hq-host verify` daily.
const DriftUnit = "severino-hq-script-drift.service"

// Checker is the deploy host's half of a preflight: run there by the
// operator's own account from a build of the release being checked, so what
// is checked is what that release needs. Read-only: it changes nothing, and
// prints verdicts, never file contents or sudo rules. Its sudo never prompts.
type Checker struct {
	// Lib and Systemd are where the tree and the units are installed.
	Lib, Systemd string
	// RootUID owns every program a sudo rule names, and the directories
	// above it.
	RootUID uint32
	// Release and Main are the unit directories this release and main ship:
	// a unit both ship is already installed, if the host is current.
	Release, Main string
	// FreeBytes is the free space a deploy needs on /, as Free reads it.
	FreeBytes uint64
	Free      func(path string) (uint64, error)
	Command   func(ctx context.Context, name string, args ...string) *exec.Cmd
	Out       io.Writer

	failed, unverified bool
}

func (c *Checker) ok(format string, args ...any) { fmt.Fprintf(c.Out, "ok      "+format+"\n", args...) }

func (c *Checker) bad(format string, args ...any) {
	fmt.Fprintf(c.Out, "FAIL    "+format+"\n", args...)
	c.failed = true
}

func (c *Checker) unknown(format string, args ...any) {
	fmt.Fprintf(c.Out, "UNKNOWN "+format+"\n", args...)
	c.unverified = true
}

// Check runs every check and returns the exit it adds up to.
func (c *Checker) Check(ctx context.Context) int {
	c.runner(ctx)
	c.verified(ctx)
	for _, program := range []string{DeployEntry, HostProgram, Lib + "/scripts/upgrade-container.sh"} {
		c.rootsAlone(strings.TrimPrefix(program, Lib+"/"))
	}
	c.units()
	c.room()
	switch {
	case c.failed:
		return CheckFailed
	case c.unverified:
		return CheckUnverified
	}
	return CheckReady
}

func (c *Checker) output(ctx context.Context, name string, args ...string) (string, error) {
	out, err := c.Command(ctx, name, args...).Output()
	return strings.TrimSpace(string(out)), err
}

var unrestricted = regexp.MustCompile(`(?m)(^|[\s:])ALL\s*$`)

// runner: the runner deploys through one sudo rule, and may do nothing
// broader.
func (c *Checker) runner(ctx context.Context) {
	listed, _ := c.output(ctx, "systemctl", "list-units", "--all", "--plain", "--no-legend", "actions.runner.*.service")
	var units []string
	for line := range strings.SplitSeq(listed, "\n") {
		if fields := strings.Fields(line); len(fields) > 0 {
			units = append(units, fields[0])
		}
	}
	if len(units) != 1 {
		c.bad("expected one Actions runner unit, found: %s", cmp.Or(strings.Join(units, " "), "none"))
		return
	}
	user, _ := c.output(ctx, "systemctl", "show", "-p", "User", "--value", units[0])
	if user == "" || user == "root" {
		c.bad("the Actions runner runs as '%s'; it must run as the deploy account", cmp.Or(user, "root"))
		return
	}
	rules, err := c.output(ctx, "sudo", "-n", "-l", "-U", user)
	switch {
	case err != nil:
		c.bad("could not list %s's sudo rules", user)
	case unrestricted.MatchString(rules):
		c.bad("%s may run any command through sudo", user)
	case regexp.MustCompile(`NOPASSWD:.*` + regexp.QuoteMeta(c.Lib+"/scripts/deploy-image.sh")).MatchString(rules):
		c.ok("%s may run %s through sudo", user, c.Lib+"/scripts/deploy-image.sh")
	default:
		c.bad("%s has no NOPASSWD sudo rule for %s", user, c.Lib+"/scripts/deploy-image.sh")
	}
}

// verified: the host is the release it runs, the tree, the units and nothing
// else. hq-host verify runs as root. Where sudo asks for a password, the
// evidence is the drift unit's last run, which systemd reports to anyone,
// provided it started after this release was installed: every deploy
// rewrites the tree's .source, and verify compares the two.
func (c *Checker) verified(ctx context.Context) {
	out, err := c.Command(ctx, "sudo", "-n", filepath.Join(c.Lib, "deploy", "bin", "hq-host"), "verify").CombinedOutput()
	if err == nil {
		c.ok("the host is the release it runs")
		return
	}
	drifted := false
	for line := range strings.SplitSeq(string(out), "\n") {
		if drift, ok := strings.CutPrefix(line, "drift: "); ok {
			c.bad("%s", drift)
			drifted = true
		}
	}
	if drifted {
		return
	}
	installed, err := os.Stat(filepath.Join(c.Lib, ".source"))
	if err != nil {
		c.unknown("hq-host verify needs a password here, and when this release was installed cannot be read")
		return
	}
	switch c.lastVerify(ctx, installed.ModTime().Unix()) {
	case "success":
		c.ok("the host is the release it runs (%s, since this release was installed)", DriftUnit)
	case "failure":
		c.bad("%s failed since this release was installed; 'journalctl -u %s' there says why", DriftUnit, DriftUnit)
	default:
		c.unknown("hq-host verify needs a password here, and %s has not run since this release was installed; run 'sudo systemctl start %s' there", DriftUnit, DriftUnit)
	}
}

// lastVerify is how the drift unit's last run ended, when it started after
// since and has finished: "success", "failure", or "" for no such run.
func (c *Checker) lastVerify(ctx context.Context, since int64) string {
	shown, err := c.output(ctx, "systemctl", "show", "--timestamp=unix",
		"--property=Id,ActiveState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp", "--", DriftUnit)
	if err != nil {
		return ""
	}
	value := map[string]string{}
	for line := range strings.SplitSeq(shown, "\n") {
		if key, v, ok := strings.Cut(line, "="); ok {
			value[key] = v
		}
	}
	instant := func(key string) (int64, bool) {
		seconds, err := strconv.ParseInt(strings.TrimPrefix(value[key], "@"), 10, 64)
		return seconds, err == nil
	}
	started, ok := instant("ExecMainStartTimestamp")
	exited, done := instant("ExecMainExitTimestamp")
	if !ok || !done || started <= since || exited < started || value["ActiveState"] == "activating" {
		return ""
	}
	if value["Result"] == "success" && value["ExecMainStatus"] == "0" {
		return "success"
	}
	return "failure"
}

// rootsAlone: a program a sudo rule runs as root is root's, and so is every
// directory above it in the tree, none a link and none writable by anyone
// else. A program this release does not have is not checked.
func (c *Checker) rootsAlone(relative string) {
	program := filepath.Join(c.Lib, relative)
	if _, err := os.Lstat(program); err != nil {
		return
	}
	alone := true
	path := c.Lib
	for _, part := range append([]string{""}, strings.Split(relative, "/")...) {
		path = filepath.Join(path, part)
		info, err := os.Lstat(path)
		switch {
		case err != nil:
			c.bad("%s cannot be read; a sudo rule runs %s as root", path, program)
		case info.Mode()&os.ModeSymlink != 0:
			c.bad("%s is a symlink; a sudo rule runs %s as root", path, program)
		case info.Sys().(*syscall.Stat_t).Uid != c.RootUID:
			c.bad("%s is not owned by root; a sudo rule runs %s as root", path, program)
		case info.Mode().Perm()&0o022 != 0:
			c.bad("%s is writable by its group or others; a sudo rule runs %s as root", path, program)
		default:
			continue
		}
		alone = false
	}
	if alone {
		c.ok("%s is root's alone", program)
	}
}

// units: every unit the release expects to find already installed.
func (c *Checker) units() {
	release, err := Shipped(c.Release)
	if err == nil {
		var main []string
		if main, err = Shipped(c.Main); err == nil {
			release = slices.DeleteFunc(release, func(unit string) bool { return !slices.Contains(main, unit) })
		}
	}
	if err != nil {
		c.bad("the units this release ships cannot be read: %v", err)
		return
	}
	missing := false
	for _, unit := range release {
		if info, err := os.Stat(filepath.Join(c.Systemd, unit)); err != nil || !info.Mode().IsRegular() {
			c.bad("%s is not installed", filepath.Join(c.Systemd, unit))
			missing = true
		}
	}
	if !missing {
		c.ok("every established unit is installed")
	}
}

// room: space for the deploy's pull.
func (c *Checker) room() {
	free, err := c.Free("/")
	switch {
	case err != nil:
		c.bad("the free space on / cannot be read")
	case free < c.FreeBytes:
		c.bad("%d MiB free on /; the deploy needs %d MiB", free>>20, c.FreeBytes>>20)
	default:
		c.ok("%d MiB free on /", free>>20)
	}
}
