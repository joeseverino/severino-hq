package providers

import (
	"context"
	"errors"
	"io"
	"iter"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The state of the systemd units this repository ships, and of each other
// service systemd has restarted on its own, on the machine the controller
// runs on. Root asks systemd (host.UnitProperties) and the
// launcher mounts the answer; this
// reads that text and never talks to systemd.

// hostUnitsMaxBytes bounds the answer. A unit is about a dozen short lines.
const hostUnitsMaxBytes = 1 << 20

const hostUnitsRefused = "the unit state is not what systemctl show prints"

var (
	hostUnitName      = api.MustPattern("HostUnitRecord", "properties", "unit")
	hostUnitWord      = api.MustPattern("HostUnitRecord", "properties", "active")
	hostUnitText      = api.MustPattern("HostUnitRecord", "properties", "title")
	hostUnitTextLimit = api.MustLimit("HostUnitRecord", "properties", "title", "maxLength")
)

// hostUnitProperties is every property the reading keeps, by the name
// `systemctl show` prints it under, and where it goes in the record. The
// launcher asks for exactly these (a test holds the two lists equal); a
// property not named here is dropped, whatever the file holds.
var hostUnitProperties = map[string]func(*runtime.HostUnitRecord, string) bool{
	"Id":                     func(r *runtime.HostUnitRecord, v string) bool { return unitName(&r.Unit, v) },
	"Description":            func(r *runtime.HostUnitRecord, v string) bool { return unitText(&r.Title, v) },
	"LoadState":              func(r *runtime.HostUnitRecord, v string) bool { return unitWord(&r.Load, v) },
	"UnitFileState":          func(r *runtime.HostUnitRecord, v string) bool { return unitWord(&r.FileState, v) },
	"ActiveState":            func(r *runtime.HostUnitRecord, v string) bool { return unitWord(&r.Active, v) },
	"SubState":               func(r *runtime.HostUnitRecord, v string) bool { return unitWord(&r.Sub, v) },
	"Result":                 func(r *runtime.HostUnitRecord, v string) bool { return unitWord(&r.Result, v) },
	"ExecMainCode":           func(r *runtime.HostUnitRecord, v string) bool { return unitNumber(&r.MainCode, v) },
	"ExecMainStatus":         func(r *runtime.HostUnitRecord, v string) bool { return unitNumber(&r.MainStatus, v) },
	"InactiveExitTimestamp":  func(r *runtime.HostUnitRecord, v string) bool { return unitInstant(&r.StartedAt, v) },
	"InactiveEnterTimestamp": func(r *runtime.HostUnitRecord, v string) bool { return unitInstant(&r.EndedAt, v) },
	"ConditionResult":        func(r *runtime.HostUnitRecord, v string) bool { return unitWord(&r.Condition, v) },
	"ConditionTimestamp":     func(r *runtime.HostUnitRecord, v string) bool { return unitInstant(&r.ConditionAt, v) },
	"LastTriggerUSec":        func(r *runtime.HostUnitRecord, v string) bool { return unitInstant(&r.LastTriggerAt, v) },
	"NextElapseUSecRealtime": func(r *runtime.HostUnitRecord, v string) bool { return unitInstant(&r.NextElapseAt, v) },
	"Unit":                   func(r *runtime.HostUnitRecord, v string) bool { return unitName(&r.Activates, v) },
	"NRestarts":              func(r *runtime.HostUnitRecord, v string) bool { return unitNumber(&r.Restarts, v) },
	"ActiveEnterTimestamp":   func(r *runtime.HostUnitRecord, v string) bool { return unitInstant(&r.ActiveAt, v) },
}

func unitName(field *string, value string) bool {
	*field = value
	return value == "" || hostUnitName.MatchString(value)
}

func unitWord(field *string, value string) bool {
	*field = value
	return value == "" || hostUnitWord.MatchString(value)
}

// unitText is the unit file's title, for a person to read in place of a name
// systemd escapes. A title the contract does not admit is left out, never
// cut, and the unit is still read: HQ falls back to its name.
func unitText(field *string, value string) bool {
	if len(value) <= hostUnitTextLimit && hostUnitText.MatchString(value) {
		*field = value
	}
	return true
}

func unitNumber(field *int, value string) bool {
	if value == "" {
		return true
	}
	number, err := strconv.Atoi(value)
	*field = number
	return err == nil && number >= 0
}

// systemdPrettyUTC is systemctl's default timestamp, printed where the
// process's zone is UTC.
const systemdPrettyUTC = "Mon 2006-01-02 15:04:05 UTC"

// unitInstant reads a timestamp as `systemctl show` prints one: seconds since
// the epoch after an @ (--timestamp=unix), or its default form in UTC. A
// timestamp systemd has not recorded is empty, "n/a" or zero, and is no
// instant; anything else is refused.
func unitInstant(field *string, value string) bool {
	if value == "" || value == "n/a" || value == "0" || value == "@0" {
		return true
	}
	var at time.Time
	if seconds, isUnix := strings.CutPrefix(value, "@"); isUnix {
		epoch, err := strconv.ParseInt(seconds, 10, 64)
		if err != nil || epoch < 0 {
			return false
		}
		at = time.Unix(epoch, 0)
	} else {
		parsed, err := time.Parse(systemdPrettyUTC, value)
		if err != nil {
			return false
		}
		at = parsed
	}
	*field = at.UTC().Format(time.RFC3339)
	return true
}

// hostUnits is one record per unit the launcher asked systemd about: the
// shipped set, then each service outside it that systemd restarted on its
// own. The refusal of a file that is not that answer names no line of it.
func (r *Registry) hostUnits(context.Context) ([]any, error) {
	if r.Env.HostUnits == "" {
		return nil, &ProviderError{Message: "no unit state was mounted"}
	}
	text, readAt, err := readHostUnits(r.Env.HostUnits, "the launcher could not ask systemd for the unit state")
	if err != nil {
		return nil, err
	}
	records, err := parseHostUnits(text, readAt)
	if err != nil {
		return nil, &ProviderError{Message: err.Error()}
	}
	// A launcher that names no such file asks about the shipped set alone.
	if r.Env.HostRestartingUnits != "" {
		text, readAt, err := readHostUnits(r.Env.HostRestartingUnits, "the launcher could not ask systemd which services restart")
		if err != nil {
			return nil, err
		}
		records = append(records, restartingHostUnits(text, readAt, records)...)
	}
	found := make([]any, len(records))
	for i, record := range records {
		found[i] = record
	}
	return found, nil
}

// readHostUnits is one mounted answer and when the launcher wrote it.
func readHostUnits(path, unmounted string) (text, readAt string, err error) {
	file, err := os.Open(path)
	if err != nil {
		return "", "", &ProviderError{Message: unmounted}
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "", "", &ProviderError{Message: "read the unit state", Err: err}
	}
	data, err := io.ReadAll(io.LimitReader(file, hostUnitsMaxBytes+1))
	if err != nil {
		return "", "", &ProviderError{Message: "read the unit state", Err: err}
	}
	if len(data) > hostUnitsMaxBytes {
		return "", "", &ProviderError{Message: "the unit state is larger than one can be"}
	}
	return string(data), info.ModTime().UTC().Format(time.RFC3339), nil
}

// parseHostUnits reads `systemctl show` output for several units: one block of
// Property=value lines per unit, with an empty line between blocks.
func parseHostUnits(text, readAt string) ([]runtime.HostUnitRecord, error) {
	refused := errors.New(hostUnitsRefused)
	records, seen := []runtime.HostUnitRecord{}, map[string]bool{}
	for block := range hostUnitBlocks(text) {
		record, ok := parseHostUnit(block, readAt)
		if !ok || seen[record.Unit] {
			return nil, refused
		}
		seen[record.Unit] = true
		records = append(records, record)
	}
	if len(records) == 0 {
		return nil, refused
	}
	return records, nil
}

// restartingHostUnits reads the services HQ does not ship, each marked so.
// Their names and words are another package's, so a unit the contract does
// not admit is passed over, never a refusal of the units HQ ships.
func restartingHostUnits(text, readAt string, shipped []runtime.HostUnitRecord) []runtime.HostUnitRecord {
	seen := map[string]bool{}
	for _, record := range shipped {
		seen[record.Unit] = true
	}
	records := []runtime.HostUnitRecord{}
	for block := range hostUnitBlocks(text) {
		record, ok := parseHostUnit(block, readAt)
		if !ok || seen[record.Unit] {
			continue
		}
		seen[record.Unit] = true
		record.Foreign = true
		records = append(records, record)
	}
	return records
}

func hostUnitBlocks(text string) iter.Seq[string] {
	return func(yield func(string) bool) {
		for block := range strings.SplitSeq(strings.ReplaceAll(text, "\r\n", "\n"), "\n\n") {
			if strings.TrimSpace(block) != "" && !yield(strings.Trim(block, "\n")) {
				return
			}
		}
	}
}

// parseHostUnit is one block's record, or false for a block that is not what
// systemctl show prints of a unit.
func parseHostUnit(block, readAt string) (runtime.HostUnitRecord, bool) {
	record := runtime.HostUnitRecord{ReadAt: readAt}
	for line := range strings.SplitSeq(block, "\n") {
		property, value, ok := strings.Cut(line, "=")
		if !ok {
			return record, false
		}
		if keep := hostUnitProperties[property]; keep != nil && !keep(&record, value) {
			return record, false
		}
	}
	return record, record.Unit != "" && record.Load != "" && record.Active != "" && record.Sub != ""
}
