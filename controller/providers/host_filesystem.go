package providers

import (
	"context"
	jsonv2 "encoding/json/v2"
	"errors"
	"io"
	"io/fs"
	"math"
	"os"

	"github.com/joeseverino/severino-hq/controller/api"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// The space on each real filesystem of the machine the controller runs on.
// Root asks statfs, since the container's own answers for its overlay, and
// the launcher mounts the answer (hostcore.ReadFilesystems); this reads that
// document and asks the kernel nothing.

// hostFilesystemsMaxBytes bounds the document: a filesystem is one short line.
const hostFilesystemsMaxBytes = 1 << 20

var (
	hostFilesystemMount      = api.MustPattern("HostFilesystemRecord", "properties", "mount")
	hostFilesystemMountLimit = api.MustLimit("HostFilesystemRecord", "properties", "mount", "maxLength")
	hostFilesystemInstant    = api.MustPattern("HostFilesystemRecord", "properties", "read_at")
)

// hostFilesystems is one record per filesystem the launcher read. The launcher
// names the file on every run, so one it could not take is a failed read,
// which leaves the last one standing. A mount whose name the contract does
// not admit is passed over, never a refusal of the rest.
func (r *Registry) hostFilesystems(context.Context) ([]any, error) {
	if r.Env.HostFilesystems == "" {
		return nil, &ProviderError{Message: "no filesystem reading was mounted"}
	}
	file, err := os.Open(r.Env.HostFilesystems)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, &ProviderError{Message: "the launcher could not read the filesystems' space"}
	}
	if err != nil {
		return nil, &ProviderError{Message: "read the filesystem reading", Err: err}
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, hostFilesystemsMaxBytes+1))
	if err != nil {
		return nil, &ProviderError{Message: "read the filesystem reading", Err: err}
	}
	if len(data) > hostFilesystemsMaxBytes {
		return nil, &ProviderError{Message: "the filesystem reading is larger than one can be"}
	}
	var reading hostcore.FilesystemReading
	if err := jsonv2.Unmarshal(data, &reading, jsonv2.RejectUnknownMembers(true)); err != nil {
		return nil, &ProviderError{Message: "the filesystem reading is not what the launcher writes", Err: err}
	}
	if !hostFilesystemInstant.MatchString(reading.ReadAt) {
		return nil, &ProviderError{Message: "the filesystem reading says no time it was read"}
	}
	found := []any{}
	for _, filesystem := range reading.Filesystems {
		if len(filesystem.Mount) > hostFilesystemMountLimit || !hostFilesystemMount.MatchString(filesystem.Mount) ||
			filesystem.TotalBytes > math.MaxInt64 || filesystem.FreeBytes > filesystem.TotalBytes {
			continue
		}
		found = append(found, runtime.HostFilesystemRecord{
			Mount:      filesystem.Mount,
			FreeBytes:  int(filesystem.FreeBytes),
			TotalBytes: int(filesystem.TotalBytes),
			ReadAt:     reading.ReadAt,
		})
	}
	return found, nil
}
