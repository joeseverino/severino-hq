package providers

import (
	"context"
	jsonv2 "encoding/json/v2"
	"errors"
	"io"
	"io/fs"
	"os"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Whether the machine the controller runs on waits on a restart, and the
// shutdown logind has scheduled. Root reads the flag in /run and asks logind
// (hostcore.ReadReboot) and the launcher mounts the answer; this reads that
// document and asks the machine nothing.

// hostRebootMaxBytes bounds the document: a time or two and at most a page of
// package names.
const hostRebootMaxBytes = 64 << 10

var (
	hostRebootPackage         = api.MustPattern("HostRebootRecord", "properties", "packages", "items")
	hostRebootPackages        = api.MustLimit("HostRebootRecord", "properties", "packages", "maxItems")
	hostRebootAction          = api.MustPattern("HostRebootRecord", "properties", "scheduled_action")
	hostRebootInstant         = api.MustPattern("HostRebootRecord", "properties", "read_at")
	hostRebootOptionalInstant = api.MustPattern("HostRebootRecord", "properties", "since")
)

// hostReboot is the one record of the launcher's reading. The launcher names
// the file on every run, so one it could not take is a failed read, which
// leaves the last one standing. A package whose name the contract does not
// admit is passed over, and the list stops at the contract's count.
func (r *Registry) hostReboot(context.Context) ([]any, error) {
	if r.Env.HostReboot == "" {
		return nil, &ProviderError{Message: "no restart reading was mounted"}
	}
	file, err := os.Open(r.Env.HostReboot)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, &ProviderError{Message: "the launcher could not read whether the machine waits on a restart"}
	}
	if err != nil {
		return nil, &ProviderError{Message: "read the restart reading", Err: err}
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, hostRebootMaxBytes+1))
	if err != nil {
		return nil, &ProviderError{Message: "read the restart reading", Err: err}
	}
	if len(data) > hostRebootMaxBytes {
		return nil, &ProviderError{Message: "the restart reading is larger than one can be"}
	}
	var reading hostcore.RebootReading
	if err := jsonv2.Unmarshal(data, &reading, jsonv2.RejectUnknownMembers(true)); err != nil {
		return nil, &ProviderError{Message: "the restart reading is not what the launcher writes", Err: err}
	}
	if !hostRebootInstant.MatchString(reading.ReadAt) || !hostRebootOptionalInstant.MatchString(reading.Since) ||
		!hostRebootOptionalInstant.MatchString(reading.ScheduledAt) || !hostRebootAction.MatchString(reading.ScheduledAction) ||
		(reading.ScheduledAction == "") != (reading.ScheduledAt == "") {
		return nil, &ProviderError{Message: "the restart reading says a time or an action no reading can"}
	}
	packages := []string{}
	for _, name := range reading.Packages {
		if len(packages) == hostRebootPackages {
			break
		}
		if hostRebootPackage.MatchString(name) {
			packages = append(packages, name)
		}
	}
	return []any{runtime.HostRebootRecord{
		Pending:         reading.Pending,
		Since:           reading.Since,
		Packages:        packages,
		ScheduledAction: reading.ScheduledAction,
		ScheduledAt:     reading.ScheduledAt,
		ReadAt:          reading.ReadAt,
	}}, nil
}
