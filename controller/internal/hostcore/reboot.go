package hostcore

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"math"
	"os"
	"slices"
	"strings"
	"time"
)

// RebootRequired is the flag Debian's and Ubuntu's packaging leaves when an
// installed update waits on a restart (update-notifier-common's
// notify-reboot-required writes it), and RebootRequired+".pkgs" lists the
// packages that asked for it, one a line, appended to.
const RebootRequired = "/run/reboot-required"

// rebootPackagesMaxBytes bounds what is read of the package list: a name is
// one short line, and the file only grows until the machine restarts.
const rebootPackagesMaxBytes = 64 << 10

// ScheduledShutdown asks logind for the shutdown scheduled with
// ScheduleShutdown(), as org.freedesktop.login1(5) documents the property: a
// pair of what it will do and when, in microseconds since the epoch. Nothing
// scheduled is an empty type at 0 or UINT64_MAX.
var ScheduledShutdown = []string{
	"busctl", "--json=short", "get-property",
	"org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "ScheduledShutdown",
}

// RebootReading is what root hands on: whether the machine waits on a
// restart, since when and for which packages, and the shutdown logind has
// scheduled, if any.
type RebootReading struct {
	ReadAt          string   `json:"read_at"`
	Pending         bool     `json:"pending"`
	Since           string   `json:"since"`
	Packages        []string `json:"packages"`
	ScheduledAction string   `json:"scheduled_action"`
	ScheduledAt     string   `json:"scheduled_at"`
}

// ReadReboot is the document root hands on, from the flag at flag, when born
// says it was written, and busctl's answer for ScheduledShutdown. A flag it
// cannot tell is there or not, or an answer logind did not give, is an error,
// so nothing stands in for a reading that was not taken.
func ReadReboot(flag string, born func(path string) (time.Time, error), scheduled []byte, now time.Time) ([]byte, error) {
	action, at, err := ParseScheduledShutdown(scheduled)
	if err != nil {
		return nil, err
	}
	reading := RebootReading{ReadAt: now.UTC().Format(time.RFC3339), Packages: []string{}, ScheduledAction: action}
	if !at.IsZero() {
		reading.ScheduledAt = at.UTC().Format(time.RFC3339)
	}
	info, err := os.Lstat(flag)
	if errors.Is(err, fs.ErrNotExist) {
		return json.Marshal(reading)
	}
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a file", flag)
	}
	reading.Pending = true
	if since, err := born(flag); err == nil {
		reading.Since = since.UTC().Format(time.RFC3339)
	}
	reading.Packages = rebootPackages(flag + ".pkgs")
	return json.Marshal(reading)
}

// rebootPackages is each package the list names, once, in the order they
// asked; none when there is no list to read.
func rebootPackages(path string) []string {
	packages := []string{}
	file, err := os.Open(path)
	if err != nil {
		return packages
	}
	defer file.Close()
	lines := bufio.NewScanner(io.LimitReader(file, rebootPackagesMaxBytes))
	for lines.Scan() {
		name := strings.TrimSpace(lines.Text())
		if name != "" && !slices.Contains(packages, name) {
			packages = append(packages, name)
		}
	}
	return packages
}

// ParseScheduledShutdown is what busctl --json=short says of the
// ScheduledShutdown property: the action and its time, or nothing when none
// is scheduled. A dry- action does nothing when its time comes, so it is
// nothing scheduled too.
func ParseScheduledShutdown(answer []byte) (action string, at time.Time, err error) {
	var property struct {
		Type string            `json:"type"`
		Data []json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(answer, &property); err != nil {
		return "", time.Time{}, fmt.Errorf("logind's scheduled shutdown: %w", err)
	}
	if property.Type != "(st)" || len(property.Data) != 2 {
		return "", time.Time{}, fmt.Errorf("logind's scheduled shutdown is a %q of %d", property.Type, len(property.Data))
	}
	var usec uint64
	if err := json.Unmarshal(property.Data[0], &action); err != nil {
		return "", time.Time{}, fmt.Errorf("logind's scheduled shutdown: %w", err)
	}
	if err := json.Unmarshal(property.Data[1], &usec); err != nil {
		return "", time.Time{}, fmt.Errorf("logind's scheduled shutdown: %w", err)
	}
	if action == "" || strings.HasPrefix(action, "dry-") || usec == 0 || usec > math.MaxInt64 {
		return "", time.Time{}, nil
	}
	return action, time.UnixMicro(int64(usec)), nil
}
