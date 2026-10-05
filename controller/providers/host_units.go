package providers

import (
	"context"
	"errors"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The state of the systemd units this repository ships, on the machine the
// controller runs on. Root asks systemd (`units_state` in
// scripts/lib/systemd-units.sh) and run-controller.sh mounts the answer; this
// reads that text and never talks to systemd.

// hostUnitsEnv names the file run-controller.sh mounts: what `systemctl show`
// printed for the shipped units.
const hostUnitsEnv = "SEVERINO_HOST_UNITS"

// hostUnitsMaxBytes bounds the answer. A unit is about a dozen short lines.
const hostUnitsMaxBytes = 1 << 20

const hostUnitsRefused = "the unit state is not what systemctl show prints"

var (
	hostUnitName = api.MustPattern("HostUnitRecord", "properties", "unit")
	hostUnitWord = api.MustPattern("HostUnitRecord", "properties", "active")
)

// hostUnitProperties is every property the reading keeps, by the name
// `systemctl show` prints it under, and where it goes in the record. The
// launcher asks for exactly these (a test holds the two lists equal); a
// property not named here is dropped, whatever the file holds.
var hostUnitProperties = map[string]func(*runtime.HostUnitRecord, string) bool{
	"Id":                     func(r *runtime.HostUnitRecord, v string) bool { return unitName(&r.Unit, v) },
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
}

func unitName(field *string, value string) bool {
	*field = value
	return value == "" || hostUnitName.MatchString(value)
}

func unitWord(field *string, value string) bool {
	*field = value
	return value == "" || hostUnitWord.MatchString(value)
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

// hostUnits is one record per unit the launcher asked systemd about. The
// refusal of a file that is not that answer names no line of it.
func (r *Registry) hostUnits(context.Context) ([]any, error) {
	path := r.Env[hostUnitsEnv]
	if path == "" {
		return nil, &ProviderError{Message: "no unit state was mounted"}
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, &ProviderError{Message: "the launcher could not ask systemd for the unit state"}
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, &ProviderError{Message: "read the unit state", Err: err}
	}
	data, err := io.ReadAll(io.LimitReader(file, hostUnitsMaxBytes+1))
	if err != nil {
		return nil, &ProviderError{Message: "read the unit state", Err: err}
	}
	if len(data) > hostUnitsMaxBytes {
		return nil, &ProviderError{Message: "the unit state is larger than one can be"}
	}
	records, err := parseHostUnits(string(data), info.ModTime().UTC().Format(time.RFC3339))
	if err != nil {
		return nil, &ProviderError{Message: err.Error()}
	}
	found := make([]any, len(records))
	for i, record := range records {
		found[i] = record
	}
	return found, nil
}

// parseHostUnits reads `systemctl show` output for several units: one block of
// Property=value lines per unit, with an empty line between blocks.
func parseHostUnits(text, readAt string) ([]runtime.HostUnitRecord, error) {
	refused := errors.New(hostUnitsRefused)
	records, seen := []runtime.HostUnitRecord{}, map[string]bool{}
	for block := range strings.SplitSeq(strings.ReplaceAll(text, "\r\n", "\n"), "\n\n") {
		if strings.TrimSpace(block) == "" {
			continue
		}
		record := runtime.HostUnitRecord{ReadAt: readAt}
		for line := range strings.SplitSeq(strings.Trim(block, "\n"), "\n") {
			property, value, ok := strings.Cut(line, "=")
			if !ok {
				return nil, refused
			}
			if keep := hostUnitProperties[property]; keep != nil && !keep(&record, value) {
				return nil, refused
			}
		}
		if record.Unit == "" || record.Load == "" || record.Active == "" || record.Sub == "" || seen[record.Unit] {
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
