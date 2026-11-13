package providers

import (
	"context"
	jsonv2 "encoding/json/v2"
	"errors"
	"io"
	"io/fs"
	"os"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/edge"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// Whether a machine waits on a restart, and the shutdown logind has
// scheduled. Root reads the flag in /run and asks logind
// (hostcore.ReadReboot): on the controller's own machine the launcher mounts
// the answer (host.reboot), and each edge's receiver answers it to the reboot
// operation (host.edge_reboot).

// hostRebootMaxBytes bounds the document: a time or two and at most a page of
// package names.
const hostRebootMaxBytes = 64 << 10

// hostRebootUnanswered is why an edge's restart state is missing when its
// receiver did not answer: one older than the version that reads it refuses.
const hostRebootUnanswered = "the edge did not say whether it waits on a restart; an edge target older than version " +
	edge.Version + " does not, so redeploy it if it is"

var (
	hostRebootPackage         = api.MustPattern("HostRebootRecord", "properties", "packages", "items")
	hostRebootPackages        = api.MustLimit("HostRebootRecord", "properties", "packages", "maxItems")
	hostRebootAction          = api.MustPattern("HostRebootRecord", "properties", "scheduled_action")
	hostRebootInstant         = api.MustPattern("HostRebootRecord", "properties", "read_at")
	hostRebootOptionalInstant = api.MustPattern("HostRebootRecord", "properties", "since")
)

// hostReboot is the one record of the launcher's reading. The launcher names
// the file on every run, so one it could not take is a failed read, which
// leaves the last one standing.
func (r *Registry) hostReboot(context.Context) ([]any, error) {
	if r.Env.HostReboot == "" {
		return nil, &ProviderError{Message: "no restart reading was mounted"}
	}
	record, err := r.ownReboot()
	if err != nil {
		return nil, err
	}
	return []any{record}, nil
}

// hostEdgeReboot is one record for each edge, named by its connection. An
// edge that does not answer is a refused part of its own, so one stale edge
// hides no other; the read fails only when none answered.
func (r *Registry) hostEdgeReboot(ctx context.Context) ([]any, error) {
	refs := r.Supplied.RoleRefs(caddyRole)
	found := []any{}
	var failures []error
	for _, ref := range refs {
		record, err := r.edgeReboot(ctx, ref)
		if err != nil {
			failures = append(failures, err)
			refuse(ctx, runtime.PartWhole, ref, ref, err)
			continue
		}
		found = append(found, record)
	}
	if len(refs) > 0 && len(failures) == len(refs) {
		return nil, failures[0]
	}
	return found, nil
}

// ownReboot is the launcher's reading of the controller's own machine.
func (r *Registry) ownReboot() (runtime.HostRebootRecord, error) {
	file, err := os.Open(r.Env.HostReboot)
	if errors.Is(err, fs.ErrNotExist) {
		return runtime.HostRebootRecord{}, &ProviderError{Message: "the launcher could not read whether the machine waits on a restart"}
	}
	if err != nil {
		return runtime.HostRebootRecord{}, &ProviderError{Message: "read the restart reading", Err: err}
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, hostRebootMaxBytes+1))
	if err != nil {
		return runtime.HostRebootRecord{}, &ProviderError{Message: "read the restart reading", Err: err}
	}
	return rebootRecord(data, "the launcher")
}

// edgeReboot is what one edge's receiver says of its machine.
func (r *Registry) edgeReboot(ctx context.Context, ref string) (runtime.HostEdgeRebootRecord, error) {
	data, err := r.commands().SSH(ctx, ref, edge.OperationReboot, nil)
	if err != nil {
		return runtime.HostEdgeRebootRecord{}, &ProviderError{Message: hostRebootUnanswered, Err: err}
	}
	record, err := rebootRecord(data, "the edge")
	if err != nil {
		return runtime.HostEdgeRebootRecord{}, err
	}
	return runtime.HostEdgeRebootRecord{
		ConnectionRef:   ref,
		Pending:         record.Pending,
		Since:           record.Since,
		Packages:        record.Packages,
		ScheduledAction: record.ScheduledAction,
		ScheduledAt:     record.ScheduledAt,
		ReadAt:          record.ReadAt,
	}, nil
}

// rebootRecord is hostcore.ReadReboot's document as the record, from whoever
// wrote it. A package whose name the contract does not admit is passed over,
// and the list stops at the contract's count.
func rebootRecord(data []byte, writer string) (runtime.HostRebootRecord, error) {
	if len(data) > hostRebootMaxBytes {
		return runtime.HostRebootRecord{}, &ProviderError{Message: "the restart reading is larger than one can be"}
	}
	var reading hostcore.RebootReading
	if err := jsonv2.Unmarshal(data, &reading, jsonv2.RejectUnknownMembers(true)); err != nil {
		return runtime.HostRebootRecord{}, &ProviderError{Message: "the restart reading is not what " + writer + " writes", Err: err}
	}
	if !hostRebootInstant.MatchString(reading.ReadAt) || !hostRebootOptionalInstant.MatchString(reading.Since) ||
		!hostRebootOptionalInstant.MatchString(reading.ScheduledAt) || !hostRebootAction.MatchString(reading.ScheduledAction) ||
		(reading.ScheduledAction == "") != (reading.ScheduledAt == "") {
		return runtime.HostRebootRecord{}, &ProviderError{Message: "the restart reading says a time or an action no reading can"}
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
	return runtime.HostRebootRecord{
		Pending:         reading.Pending,
		Since:           reading.Since,
		Packages:        packages,
		ScheduledAction: reading.ScheduledAction,
		ScheduledAt:     reading.ScheduledAt,
		ReadAt:          reading.ReadAt,
	}, nil
}
